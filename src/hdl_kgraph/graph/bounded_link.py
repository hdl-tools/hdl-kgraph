"""Memory-bounded incremental Pass-2 link (#119, opt-in).

The default incremental linker (:func:`hdl_kgraph.graph.builder.link_incremental`)
loads the **whole** prior graph (`SqliteStore.load()`) to re-resolve the dirty
closure. This module does the same re-resolution **without** materialising the
prior graph: it reuses the *unchanged* :meth:`_Linker._resolve` but feeds its
indexes (`definitions`/`children`/`parent`/`node_obj`) lazily from SQLite
(`idx_nodes_kind_name` / `idx_edges_*`), and decides stub-GC over only the stub
neighbourhood. The feasibility + byte-identical parity of this approach was
proven by ``scripts/spike_m13_link.py`` (see ``docs/v2/m13_link_spike.md``); here
it is wired to produce the **partial graph** that
:func:`hdl_kgraph.storage.sqlite_store._apply_delta_scoped` consumes.

The returned graph is *not* the whole design — it carries exactly what the scoped
delta write reconciles: the dirty files' fresh nodes/edges, every re-resolved
ref's edges (dirty units + ``affected_srcs``), each affected clean src's retained
pass-1 edges, and the full surviving fileless-stub set with their kept edges.
Loading the resulting database back yields a graph byte-identical to a full
``build`` — the contract ``tests/test_incremental_equivalence.py`` pins (now
parametrized over both link paths).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.graph import builder
from hdl_kgraph.graph.builder import (
    _PASS1_EDGE_KINDS,
    _PASS2_EDGE_KINDS,
    _SCOPED_REF_KINDS,
    Node,
    RefRecord,
    _gc_orphan_stubs,
    _Linker,
)
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import NODE_COLUMNS


def link_incremental_bounded(
    db_path: Any,
    file_irs: list[FileIR],
    dirty_files: set[str],
    affected_srcs: set[str],
    warnings: list[str] | None = None,
    root: Path | None = None,
) -> tuple[nx.MultiDiGraph, list[RefRecord]]:
    """Re-resolve the dirty closure without loading the prior graph.

    Returns the partial graph for :func:`_apply_delta_scoped` plus the full
    per-unit ref records (mirroring :func:`builder.link_incremental`). *root*
    threads the build root for in-tree absolute file-ref canonicalization (#164).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        linker = _Linker([], root)
        linker.graph = nx.MultiDiGraph()
        linker.node_obj = _LazyNodeObj(conn, dirty_files)
        linker.definitions = _LazyDefs(conn, dirty_files, ci=False)  # type: ignore[assignment]
        linker.definitions_ci = _LazyDefs(conn, dirty_files, ci=True)  # type: ignore[assignment]
        linker.children = _LazyChildren(conn, dirty_files, linker.node_obj)  # type: ignore[assignment]
        linker.parent = _LazyParent(conn, dirty_files)  # type: ignore[assignment]

        # node_file maps a ref's src to its owning compilation unit (read by
        # _score/_referrer_library) — first occurrence across the IR set, exactly
        # as _Linker.__init__/link_incremental do.
        for ir in file_irs:
            for node in ir.nodes:
                linker.node_file.setdefault(node.id, ir.path)

        _splice_dirty(linker, file_irs, dirty_files)

        # Record every ref for the ref_index; re-resolve only the live ones
        # (dirty unit or affected src) — identical to link_incremental step 3.
        for ir in file_irs:
            live_unit = ir.path in dirty_files
            for ref in ir.unresolved_refs:
                linker.ref_records.append(
                    RefRecord(
                        file=ir.path,
                        src_id=ref.src_id,
                        edge_kind=ref.edge_kind,
                        target_name=ref.target_name,
                        scoped=ref.edge_kind in _SCOPED_REF_KINDS,
                    )
                )
                if live_unit or ref.src_id in affected_srcs:
                    linker._resolve(ref)

        # Assemble the rest of what the scoped delta write reconciles.
        _hydrate_affected_pass1(conn, linker.graph, affected_srcs)
        _merge_stub_neighbourhood(conn, linker.graph, dirty_files, affected_srcs)
        # Resolution emits edges to clean targets without materialising them
        # (they live in the lazy node_obj, not the graph) — hydrate those bare
        # endpoints so no attribute-less node reaches _gc_orphan_stubs / the write.
        _hydrate_bare_endpoints(conn, linker.graph)
        _gc_orphan_stubs(linker.graph)
    finally:
        conn.close()
    if warnings is not None:
        warnings.extend(linker.warnings)
    return linker.graph, linker.ref_records


def changed_target_names_bounded(
    db_path: Any, file_irs: list[FileIR], dirty_files: set[str]
) -> set[tuple[NodeKind, str]]:
    """``(kind, name)`` definitions touched by a dirty/removed file, from the DB.

    The bounded counterpart of :func:`hdl_kgraph.incremental.changed_target_names`:
    the prior side comes from the ``nodes`` table (definitions owned by a dirty
    file) instead of a materialised graph; the new side from the fresh IRs.
    """
    from hdl_kgraph.graph.builder import DEFINITION_KINDS

    changed: set[tuple[NodeKind, str]] = set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        kind_ph = ", ".join("?" for _ in DEFINITION_KINDS)
        file_ph = ", ".join("?" for _ in dirty_files) or "''"
        rows = conn.execute(
            f"SELECT kind, name FROM nodes WHERE file IN ({file_ph}) AND kind IN ({kind_ph})",
            (*dirty_files, *(k.value for k in DEFINITION_KINDS)),
        )
        for kind, name in rows:
            changed.add((NodeKind(kind), name))
    finally:
        conn.close()
    for ir in file_irs:
        for node in ir.nodes:
            if node.kind in DEFINITION_KINDS and node.file in dirty_files:
                changed.add((node.kind, node.name))
    return changed


def _splice_dirty(linker: _Linker, file_irs: list[FileIR], dirty_files: set[str]) -> None:
    """Add the dirty units' fresh nodes + local edges (link_incremental step 2b),
    deduping pass-1 edges across dirty IRs so a shared include cannot duplicate a
    DECLARES child."""
    seen_local: set[tuple[str, str, EdgeKind]] = set()
    for ir in file_irs:
        if ir.path not in dirty_files:
            continue
        for node in ir.nodes:
            if node.id in linker.node_obj:
                continue
            builder._add_node(linker.graph, node)
            linker.node_obj[node.id] = node
            linker.definitions[(node.kind, node.name)].append(node.id)
            linker.definitions_ci[(node.kind, node.name.lower())].append(node.id)
        for edge in ir.local_edges:
            key = (edge.src, edge.dst, edge.kind)
            if edge.kind in _PASS1_EDGE_KINDS:
                if key in seen_local:
                    continue
                seen_local.add(key)
            linker._ensure_endpoint(edge.src, edge.kind, edge.dst, ir.path)
            linker._ensure_endpoint(edge.dst, edge.kind, edge.src, ir.path)
            builder._add_edge(linker.graph, edge)
            if edge.kind is EdgeKind.DECLARES:
                linker.children[edge.src].append(linker.node_obj[edge.dst])
                linker.parent[edge.dst] = edge.src


def _hydrate_affected_pass1(
    conn: sqlite3.Connection, graph: nx.MultiDiGraph, affected_srcs: set[str]
) -> None:
    """Re-add each affected clean src's **pass-1** edges from the DB.

    ``link_incremental`` drops only the affected srcs' *pass-2* edges and keeps
    their pass-1 (DECLARES/…) edges; the scoped delta diffs each affected src's
    whole edge group, so the partial graph must carry those pass-1 edges too."""
    pass1 = tuple(k.value for k in _PASS1_EDGE_KINDS)
    placeholders = ", ".join("?" for _ in pass1)
    for src in affected_srcs:
        for s, d, kind, conf, attrs in conn.execute(
            f"SELECT src, dst, kind, confidence, attrs FROM edges "
            f"WHERE src = ? AND kind IN ({placeholders})",
            (src, *pass1),
        ):
            _ensure_node(conn, graph, s)
            _ensure_node(conn, graph, d)
            graph.add_edge(s, d, kind=EdgeKind(kind), confidence=conf, attrs=json.loads(attrs))


def _merge_stub_neighbourhood(
    conn: sqlite3.Connection,
    graph: nx.MultiDiGraph,
    dirty_files: set[str],
    affected_srcs: set[str],
) -> None:
    """Add every prior unresolved stub and its **surviving** incident edges.

    Surviving = not a TEST_COVERS edge, not an affected src's pass-2 edge, and not
    incident to a dirty/removed node. TEST_COVERS is dropped here and re-derived
    whole-design after the scoped write (``pipeline._refresh_test_covers_from_db``
    via ``summaries.test_covers_sql``); affected pass-2 edges are re-resolved.
    Gives ``_gc_orphan_stubs`` the full anchoring picture and ensures the partial
    graph holds the complete surviving fileless-stub set (so the scoped write
    keeps them) bounded by the stub count, not the design."""
    dirty_node_ids = {r[0] for r in conn.execute(_dirty_node_sql(dirty_files), tuple(dirty_files))}
    stub_ids: list[str] = []
    for row in conn.execute(
        f"SELECT {NODE_COLUMNS} FROM nodes WHERE json_extract(attrs, '$.unresolved') = 1"
    ):
        if row[0] in dirty_node_ids:
            continue  # a dirty-file stub was removed with its file
        if row[0] not in graph:
            builder._add_node(graph, _row_to_node(row))
        stub_ids.append(row[0])
    for stub in stub_ids:
        for s, d, kind, conf, attrs in conn.execute(
            "SELECT src, dst, kind, confidence, attrs FROM edges WHERE src = ? OR dst = ?",
            (stub, stub),
        ):
            ek = EdgeKind(kind)
            if ek is EdgeKind.TEST_COVERS or s in dirty_node_ids or d in dirty_node_ids:
                continue
            if ek in _PASS2_EDGE_KINDS and s in affected_srcs:
                continue  # re-resolved into the graph already
            _ensure_node(conn, graph, s)
            _ensure_node(conn, graph, d)
            if not _has_exact_edge(graph, s, d, ek, conf, json.loads(attrs)):
                graph.add_edge(s, d, kind=ek, confidence=conf, attrs=json.loads(attrs))


def _has_exact_edge(
    graph: nx.MultiDiGraph, s: str, d: str, kind: EdgeKind, conf: float, attrs: dict[str, Any]
) -> bool:
    """Whether *graph* already holds this exact parallel edge (avoids a dup when a
    stub's incident edge was also produced by the dirty splice / re-resolution)."""
    if not graph.has_edge(s, d):
        return False
    return any(
        data["kind"] is kind and data["confidence"] == conf and data["attrs"] == attrs
        for data in graph[s][d].values()
    )


def _hydrate_bare_endpoints(conn: sqlite3.Connection, graph: nx.MultiDiGraph) -> None:
    """Fill in any node present only as a bare edge endpoint (no ``attrs``) — the
    clean resolution targets ``_emit`` linked to via the lazy ``node_obj``."""
    for nid in [n for n in list(graph.nodes) if "attrs" not in graph.nodes[n]]:
        row = conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE id = ?", (nid,)).fetchone()
        if row is not None:
            builder._add_node(graph, _row_to_node(row))


def _ensure_node(conn: sqlite3.Connection, graph: nx.MultiDiGraph, nid: str) -> None:
    if nid in graph:
        return
    row = conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE id = ?", (nid,)).fetchone()
    if row is not None:
        builder._add_node(graph, _row_to_node(row))


def _dirty_node_sql(dirty: set[str]) -> str:
    placeholders = ", ".join("?" for _ in dirty) or "''"
    return f"SELECT id FROM nodes WHERE file IN ({placeholders})"


def _row_to_node(row: tuple[Any, ...]) -> Node:
    nid, kind, name, qualified, file, ls, le, lang, attrs = row
    return Node(
        id=nid,
        kind=NodeKind(kind),
        name=name,
        qualified_name=qualified,
        file=file,
        line_span=(ls, le),
        language=Language(lang),
        attrs=json.loads(attrs),
    )


# --------------------------------------------------------------------------- #
# Lazy SQL-backed indexes (productionised from scripts/spike_m13_link.py).
# Dirty-file nodes are excluded at the SQL level, mirroring link_incremental
# step 1 (which removes dirty/removed-file nodes and their edges before seeding).
# --------------------------------------------------------------------------- #
def _not_in_dirty(col: str, dirty: set[str]) -> tuple[str, tuple[str, ...]]:
    if not dirty:
        return "", ()
    placeholders = ", ".join("?" for _ in dirty)
    return f" AND {col} NOT IN ({placeholders})", tuple(dirty)


class _LazyNodeObj(dict):
    """``id -> Node``; misses fetch the row unless it belongs to a dirty file."""

    def __init__(self, conn: sqlite3.Connection, dirty: set[str]) -> None:
        super().__init__()
        self._conn, self._dirty = conn, dirty
        self._absent: set[str] = set()

    def _fetch(self, key: str) -> Node | None:
        if key in self._absent:
            return None
        row = self._conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE id = ?", (key,)
        ).fetchone()
        if row is None or row[4] in self._dirty:  # row[4] = file (dirty == removed)
            self._absent.add(key)
            return None
        node = _row_to_node(row)
        super().__setitem__(key, node)
        return node

    def get(self, key: str, default: Any = None) -> Any:
        if super().__contains__(key):
            return super().__getitem__(key)
        node = self._fetch(key)
        return node if node is not None else default

    def __getitem__(self, key: str) -> Node:
        if super().__contains__(key):
            return super().__getitem__(key)
        node = self._fetch(key)
        if node is None:
            raise KeyError(key)
        return node

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or (isinstance(key, str) and self._fetch(key) is not None)


class _LazyDefs:
    """``(kind, name) -> [resolved ids]`` over ``idx_nodes_kind_name`` (+ overlay)."""

    def __init__(self, conn: sqlite3.Connection, dirty: set[str], *, ci: bool) -> None:
        self._conn, self._dirty, self._ci = conn, dirty, ci
        self._overlay: dict[tuple[NodeKind, str], list[str]] = defaultdict(list)
        self._cache: dict[tuple[NodeKind, str], list[str]] = {}

    def _sql(self, key: tuple[NodeKind, str]) -> list[str]:
        if key in self._cache:
            return self._cache[key]
        kind, name = key
        col = "lower(name)" if self._ci else "name"
        rows = self._conn.execute(
            f"SELECT id, file, attrs FROM nodes WHERE kind = ? AND {col} = ?", (kind.value, name)
        )
        ids: list[str] = []
        for nid, file, attrs in rows:
            if file in self._dirty:
                continue
            if json.loads(attrs).get("unresolved"):
                continue  # definitions are parsed nodes only, never stubs
            ids.append(nid)
        self._cache[key] = ids
        return ids

    def get(self, key: tuple[NodeKind, str], default: Any = ()) -> Any:
        merged = [*self._sql(key), *self._overlay.get(key, [])]
        return merged if merged else default

    def __getitem__(self, key: tuple[NodeKind, str]) -> list[str]:
        return self._overlay[key]  # append target for spliced dirty defs


class _LazyChildren:
    """``parent_id -> [child Node]`` over ``idx_edges_src`` DECLARES (+ overlay).

    Edges incident to a dirty-file node are excluded at the SQL level so a dirty
    parent's stale prior children never leak in (fresh ones arrive via overlay)."""

    def __init__(self, conn: sqlite3.Connection, dirty: set[str], node_obj: _LazyNodeObj) -> None:
        self._conn, self._dirty, self._nodes = conn, dirty, node_obj
        self._overlay: dict[str, list[Node]] = defaultdict(list)
        self._cache: dict[str, list[Node]] = {}

    def _sql(self, parent: str) -> list[Node]:
        if parent in self._cache:
            return self._cache[parent]
        src_clause, src_params = _not_in_dirty("ns.file", self._dirty)
        dst_clause, dst_params = _not_in_dirty("nd.file", self._dirty)
        kids: list[Node] = []
        for (dst,) in self._conn.execute(
            "SELECT e.dst FROM edges e "
            "JOIN nodes ns ON ns.id = e.src JOIN nodes nd ON nd.id = e.dst "
            f"WHERE e.src = ? AND e.kind = ?{src_clause}{dst_clause}",
            (parent, EdgeKind.DECLARES.value, *src_params, *dst_params),
        ):
            child = self._nodes.get(dst)
            if child is not None:
                kids.append(child)
        self._cache[parent] = kids
        return kids

    def get(self, key: str, default: Any = None) -> Any:
        return [*self._sql(key), *self._overlay.get(key, [])]

    def __getitem__(self, key: str) -> list[Node]:
        return self._overlay[key]


class _LazyParent:
    """``child_id -> declaring scope id`` over ``idx_edges_dst`` DECLARES (+ overlay)."""

    def __init__(self, conn: sqlite3.Connection, dirty: set[str]) -> None:
        self._conn, self._dirty = conn, dirty
        self._overlay: dict[str, str] = {}

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._overlay:
            return self._overlay[key]
        src_clause, src_params = _not_in_dirty("ns.file", self._dirty)
        dst_clause, dst_params = _not_in_dirty("nd.file", self._dirty)
        row = self._conn.execute(
            "SELECT e.src FROM edges e "
            "JOIN nodes ns ON ns.id = e.src JOIN nodes nd ON nd.id = e.dst "
            f"WHERE e.dst = ? AND e.kind = ?{src_clause}{dst_clause}",
            (key, EdgeKind.DECLARES.value, *src_params, *dst_params),
        ).fetchone()
        return row[0] if row is not None else default

    def __setitem__(self, key: str, value: str) -> None:
        self._overlay[key] = value
