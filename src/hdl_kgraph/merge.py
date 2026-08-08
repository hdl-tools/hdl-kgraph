"""Database merge: IP-block assembly (``hdl-kgraph merge``).

Assemble several independently-built block databases into one SoC-level
knowledge graph. Each block is built separately (often by a different team or
on a different machine), then merged here.

The merge point is the pass-2 linker, which is a pure function of the per-file
IRs persisted in ``file_irs``. Cross-file resolution is by *name*, not path, so
a module defined in block A resolves to an instance in block B for free once
both IRs are in one list. Therefore:

    merge = union the per-file IRs across the source DBs, run ``link_graph``
    once, save.

The result is byte-identical to a monolithic build of the same files under the
same ``--root`` (Mode A — all sources must share the build root). FILELIST and
VHDL ``library`` adapter nodes are not in ``file_irs`` (they are generated fresh
at build time); they are recovered directly from each source graph and unioned
back in as synthetic adapter IRs, so VHDL-library and filelist designs merge
faithfully too. See ``docs/merge-design.md``.

Out of scope: enrichment (whole-design; enriched sources are refused) and
``update`` on a merged database (it falls back to a full rebuild).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from hdl_kgraph.graph.builder import link_graph
from hdl_kgraph.graph.summary import build_summaries
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.schema import Edge, Node, NodeKind
from hdl_kgraph.storage import ir_codec
from hdl_kgraph.storage.ir_codec import IR_CODEC_VERSION
from hdl_kgraph.storage.sqlite_store import (
    FileMeta,
    SchemaVersionError,
    SqliteStore,
    StoredUnit,
)

ProgressFn = Callable[[str], None]

#: Prefix stamped into a merged database's ``options_hash`` so a later
#: ``update`` recognizes it and falls back to a full rebuild.
MERGED_SENTINEL_PREFIX = "merged:"

#: Adapter (FILELIST / LIBRARY) node kinds recovered from the source graph and
#: re-emitted as synthetic IRs (they are never persisted in ``file_irs``).
_ADAPTER_KINDS = (NodeKind.FILELIST, NodeKind.LIBRARY)


class OnConflict(str, Enum):
    """Policy for two sources holding the same path with different content."""

    ERROR = "error"  # default: refuse, naming the file
    FIRST = "first"  # keep the earlier source's version
    LAST = "last"  # keep the later source's version


class MergeError(Exception):
    """A merge could not proceed (gating failure or an unresolved conflict)."""


@dataclass
class MergeReport:
    """Outcome of a :func:`run_merge` call."""

    db_path: Path
    root: Path
    sources: list[Path]
    units_merged: int = 0
    conflicts_resolved: list[str] = field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    unresolved_count: int = 0
    warnings: list[str] = field(default_factory=list)
    # Wall-clock of the single pass-2 link over the unioned IRs, and of the
    # whole merge. The link is "paid once" no matter how many blocks merge —
    # the value subtree caching trades a re-parse for (see docs/benchmarks.md).
    link_s: float = 0.0
    elapsed_s: float = 0.0


@dataclass
class _Kept:
    """A merged compilation unit and the bookkeeping for conflict reporting."""

    ir: FileIR
    unit: StoredUnit
    meta: FileMeta
    source: Path


def _node_from_graph(graph: object, node_id: str) -> Node:
    """Reconstruct a :class:`Node` from a persisted graph node, verbatim."""
    data = graph.nodes[node_id]  # type: ignore[attr-defined]
    return Node(
        id=node_id,
        kind=data["kind"],
        name=data["name"],
        qualified_name=data["qualified_name"],
        file=data["file"],
        line_span=tuple(data["line_span"]),
        language=data["language"],
        attrs=dict(data["attrs"]),
    )


def _gate_source(db_path: Path) -> tuple[SqliteStore, dict[str, str]]:
    """Open *db_path* read-only and refuse anything merge can't safely union.

    Returns the store and its meta. Raises :class:`MergeError` for a schema /
    codec / enrichment problem so the CLI surfaces a clean message instead of a
    silent fallback.
    """
    store = SqliteStore(db_path)
    # Bring an older but in-place-upgradable schema forward, mirroring update;
    # an un-migratable database still raises SchemaVersionError on load below.
    store.migrate()
    try:
        meta = store.load_meta()
    except SchemaVersionError as exc:
        raise MergeError(str(exc)) from exc
    if meta.get("ir_codec_version") != IR_CODEC_VERSION:
        raise MergeError(
            f"{db_path} has IR codec version {meta.get('ir_codec_version')!r}; this "
            f"hdl-kgraph expects {IR_CODEC_VERSION!r}. Re-run `hdl-kgraph build` on the source."
        )
    if store.load_discrepancies():
        raise MergeError(
            f"{db_path} is an enriched build; merge operates on the syntactic graph only. "
            "Build the source without --enrich, then enrich the merged design as a "
            "whole-design step."
        )
    return store, meta


def _node_fingerprint(node: Node) -> str:
    """A stable identity for an adapter node, to detect cross-source divergence."""
    return json.dumps(
        [
            node.kind.value,
            node.name,
            node.qualified_name,
            node.file,
            list(node.line_span),
            node.language.value,
            node.attrs,
        ],
        sort_keys=True,
        default=list,
    )


class _AdapterUnion:
    """Accumulates FILELIST / LIBRARY adapter material across the source graphs.

    These nodes are not in ``file_irs`` — they are generated fresh at build time
    — so the only faithful source is each persisted graph. A node id appearing
    in two sources must be identical; divergence is a real conflict (the same
    relpath under a shared root cannot describe two different filelists).
    """

    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.edges: dict[tuple[str, str, object], Edge] = {}
        self.target_files: dict[str, Node] = {}  # FILE endpoints, kept as stubs

    def absorb(self, graph: object, source: Path) -> None:
        adapter_ids = {
            nid
            for nid, data in graph.nodes(data=True)  # type: ignore[attr-defined]
            if data["kind"] in _ADAPTER_KINDS
        }
        for nid in adapter_ids:
            node = _node_from_graph(graph, nid)
            existing = self.nodes.get(nid)
            if existing is None:
                self.nodes[nid] = node
            elif _node_fingerprint(existing) != _node_fingerprint(node):
                raise MergeError(
                    f"conflicting {node.kind.value} node {nid!r} across sources "
                    f"(differs in {source}); merge requires consistent inputs under a shared root."
                )
        for src in adapter_ids:
            for _, dst, data in graph.out_edges(src, data=True):  # type: ignore[attr-defined]
                key = (src, dst, data["kind"])
                if key not in self.edges:
                    self.edges[key] = Edge(
                        src=src,
                        dst=dst,
                        kind=data["kind"],
                        confidence=data["confidence"],
                        attrs=dict(data["attrs"]),
                    )
                if dst not in adapter_ids and dst not in self.target_files:
                    self.target_files[dst] = _node_from_graph(graph, dst)

    def to_ir(self, parser_node_ids: set[str]) -> FileIR:
        """One synthetic IR; FILE endpoints already provided by a parser IR are
        dropped so the parser's rich node wins the linker's first-occurrence dedup."""
        ir = FileIR(path="<merged-adapters>")
        ir.nodes.extend(self.nodes.values())
        ir.nodes.extend(
            node
            for nid, node in self.target_files.items()
            if nid not in self.nodes and nid not in parser_node_ids
        )
        ir.local_edges.extend(self.edges.values())
        return ir


def _resolve_conflict(
    kept: dict[str, _Kept],
    path: str,
    incoming: _Kept,
    on_conflict: OnConflict,
    report: MergeReport,
) -> None:
    """Insert or reconcile *incoming* for *path* under the conflict policy.

    Dedup is authoritative and happens before ``link_graph`` because the linker
    keeps the first occurrence of a node id and silently drops a later same-id
    node — so two divergent versions of one path must be reduced to one here.
    """
    existing = kept.get(path)
    if existing is None:
        kept[path] = incoming
        return
    if existing.meta.content_hash == incoming.meta.content_hash:
        return  # same file in two blocks — keep one, no conflict
    if on_conflict is OnConflict.ERROR:
        raise MergeError(
            f"conflicting content for {path!r}: {existing.source} and {incoming.source} hold "
            "different versions of the same file. Re-run with --on-conflict first|last to pick one."
        )
    if on_conflict is OnConflict.LAST:
        kept[path] = incoming
        report.conflicts_resolved.append(f"{path} (kept {incoming.source})")
    else:  # FIRST
        report.conflicts_resolved.append(f"{path} (kept {existing.source})")


def run_merge(
    sources: list[Path],
    db_path: Path,
    on_conflict: OnConflict = OnConflict.ERROR,
    progress: ProgressFn | None = None,
) -> MergeReport:
    """Merge per-block databases *sources* into one graph at *db_path*."""
    if not sources:
        raise MergeError("merge needs at least one source database")

    started = time.perf_counter()

    def emit(line: str) -> None:
        if progress is not None:
            progress(line)

    report = MergeReport(db_path=db_path, root=db_path, sources=list(sources))
    kept: dict[str, _Kept] = {}
    extra_files: dict[str, FileMeta] = {}
    source_option_hashes: list[str] = []
    adapters = _AdapterUnion()
    merged_root: str | None = None

    for src in sources:
        emit(f"opening {src}")
        store, meta = _gate_source(src)

        root = meta.get("root", "")
        if merged_root is None:
            merged_root = root
        elif root != merged_root:
            raise MergeError(
                f"build root mismatch: {sources[0]} built at {merged_root!r}, {src} at {root!r}. "
                "Merge requires all sources to share the same --root (Mode A)."
            )
        source_option_hashes.append(meta.get("options_hash", ""))

        graph, files, _ = store.load()
        if any(str(nid).startswith("elab:") for nid in graph.nodes):
            raise MergeError(
                f"{src} contains elaborated (enriched) nodes; merge operates on the "
                "syntactic graph only."
            )
        units = store.load_units()
        meta_by_path = {m.path: m for m in files}

        for path, unit in units.items():
            file_meta = meta_by_path.get(path)
            if file_meta is None:  # defensive: a unit with no files row
                continue
            incoming = _Kept(
                ir=ir_codec.ir_from_json(unit.ir),
                unit=unit,
                meta=file_meta,
                source=src,
            )
            _resolve_conflict(kept, path, incoming, on_conflict, report)

        # Non-unit file records (skipped files, filelist .f metadata) carry no
        # IR but belong in the merged files table; union them under the same
        # content-hash policy so the merged DB's status/queries stay faithful.
        for path, file_meta in meta_by_path.items():
            if path in units:
                continue
            prior = extra_files.get(path)
            if prior is None:
                extra_files[path] = file_meta
            elif prior.content_hash != file_meta.content_hash and on_conflict is OnConflict.ERROR:
                raise MergeError(
                    f"conflicting content for {path!r} across sources; re-run with "
                    "--on-conflict first|last."
                )
            elif on_conflict is OnConflict.LAST:
                extra_files[path] = file_meta

        # Recover FILELIST / LIBRARY adapter nodes from this source's graph.
        adapters.absorb(graph, src)

    if not kept:
        raise MergeError("no compilation units found in the source databases")

    # Combined IR list: parser IRs first, adapter IR last, so the parser's rich
    # FILE nodes win the linker's first-occurrence dedup over adapter stubs (the
    # same ordering contract as a monolithic build).
    parser_node_ids = {node.id for rec in kept.values() for node in rec.ir.nodes}
    combined_irs = [rec.ir for rec in kept.values()] + [adapters.to_ir(parser_node_ids)]
    emit(f"linking {len(combined_irs)} unit(s) into the graph")
    warnings: list[str] = []
    _t_link = time.perf_counter()
    # The shared build root (consistent across sources, asserted above) lets the
    # linker canonicalize in-tree absolute file-refs onto the relpath keyspace (#164).
    graph, ref_records = link_graph(
        combined_irs, warnings=warnings, root=Path(merged_root) if merged_root else None
    )
    report.link_s = time.perf_counter() - _t_link

    summaries = {name: json.dumps(payload) for name, payload in build_summaries(graph).items()}

    # A path kept as a compilation unit owns its files-table row; drop any
    # non-unit record for the same path (parsed in one source, skipped in
    # another) so the files table never gets a duplicate primary key.
    files_out = [rec.meta for rec in kept.values()] + [
        meta for path, meta in extra_files.items() if path not in kept
    ]
    units_out = {path: rec.unit for path, rec in kept.items()}
    sentinel = (
        MERGED_SENTINEL_PREFIX
        + hashlib.sha256(",".join(sorted(source_option_hashes)).encode()).hexdigest()[:16]
    )
    root_path = Path(merged_root) if merged_root else db_path.parent

    emit(f"writing {db_path}")
    SqliteStore(db_path).save(
        graph,
        files_out,
        root=root_path,
        units=units_out,
        options_hash=sentinel,
        ref_records=ref_records,
        summaries=summaries,
    )

    report.root = root_path
    report.units_merged = len(units_out)
    report.node_count = graph.number_of_nodes()
    report.edge_count = graph.number_of_edges()
    report.unresolved_count = sum(
        1 for _, data in graph.nodes(data=True) if data["attrs"].get("unresolved")
    )
    report.warnings = warnings
    report.elapsed_s = time.perf_counter() - started
    return report
