"""Incremental-link locality metric (``hdl-kgraph bench-link``).

A full re-link re-resolves *every* pass-2 reference; an incremental ``update``
re-resolves only the refs a single-file edit touches — that file's own refs plus
the *affected clean refs* whose target name is a definition the edit changed
(:func:`hdl_kgraph.incremental.affected_clean_refs`), expanded over the
include/macro dirty closure (:func:`hdl_kgraph.incremental.dirty_closure`).

This module quantifies that locality from a built ``graph.db`` alone — the
persisted ``ref_index`` plus the include/macro dependency subgraph — so an
installed package can evaluate, per design, how much a memory-bounded
incremental linker (#119) would save: the distribution of
``reresolved_refs(file) / total_refs`` across single-file edits. The output is
**content-free** (counts and ratios only, no identifiers), like ``review`` — the
test suite pins that.

It does *not* re-run resolution (no second linker); it measures the *work set*
(refs touched) from the index. The byte-identical correctness of an actual
bounded re-link is proven separately by ``scripts/spike_m13_link.py``.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from hdl_kgraph.graph.builder import DEFINITION_KINDS, RefRecord, ref_target_kinds
from hdl_kgraph.incremental import dirty_closure
from hdl_kgraph.schema import Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore

#: digest schema identifier (bump on breaking field changes).
BENCH_LINK_SCHEMA = "hdl-kgraph.bench-link/1"


def link_locality(db_path: Path, *, sample: int | None = None) -> dict[str, Any]:
    """Per-single-file-edit re-resolution work distribution for a built graph.

    Returns a content-free digest (see module docstring). *sample* caps the
    number of files evaluated (evenly strided over the sorted file list) for a
    quick estimate on very large designs; ``None`` evaluates every source file.
    """
    store = SqliteStore(db_path)
    refs = store.load_ref_index()
    deps = store.load_dependency_graph()
    with store._connect() as conn:
        store._check_version(conn)
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        source_files = [
            row[0]
            for row in conn.execute(
                "SELECT path FROM files WHERE skipped_reason IS NULL AND language != ?",
                (Language.UNKNOWN.value,),
            )
        ]
        defs_by_file = _defs_by_file(conn)

    total_refs = len(refs)
    # reverse indexes built once: O(refs), bounded by refs (not nodes+edges)
    by_target: dict[str, list[RefRecord]] = defaultdict(list)
    own_by_file: dict[str, list[RefRecord]] = defaultdict(list)
    for rec in refs:
        by_target[rec.target_name].append(rec)
        own_by_file[rec.file].append(rec)

    files = sorted(source_files)
    if sample is not None and 0 < sample < len(files):
        step = len(files) / sample
        files = [files[int(i * step)] for i in range(sample)]

    reresolved = [_reresolved_count(f, deps, defs_by_file, by_target, own_by_file) for f in files]

    ratios = [r / total_refs for r in reresolved] if total_refs else [0.0] * len(reresolved)
    return {
        "schema": BENCH_LINK_SCHEMA,
        "totals": {
            "files": len(files),
            "refs": total_refs,
            "nodes": node_count,
            "edges": edge_count,
        },
        "reresolved_refs": _summary(reresolved),
        "locality_ratio": _ratio_summary(ratios),
        "full_relink_refs": total_refs,
    }


def _defs_by_file(conn: Any) -> dict[str, set[tuple[NodeKind, str]]]:
    """``file -> {(kind, name)}`` for every resolution-target definition node."""
    placeholders = ", ".join("?" for _ in DEFINITION_KINDS)
    out: dict[str, set[tuple[NodeKind, str]]] = defaultdict(set)
    for file, kind, name in conn.execute(
        f"SELECT file, kind, name FROM nodes WHERE kind IN ({placeholders}) AND file != ''",
        tuple(k.value for k in DEFINITION_KINDS),
    ):
        out[file].add((NodeKind(kind), name))
    return out


def _reresolved_count(
    edited: str,
    deps: Any,
    defs_by_file: dict[str, set[tuple[NodeKind, str]]],
    by_target: dict[str, list[RefRecord]],
    own_by_file: dict[str, list[RefRecord]],
) -> int:
    """Distinct refs an incremental re-link re-resolves when *edited* changes.

    The closure (the edit + its include/macro dependents) re-resolves all its own
    refs; plus every clean ref whose target name is a definition the closure
    changed and whose kind could resolve to it — the exact ``affected_clean_refs``
    rule, keyed by ``(file, src_id, edge_kind)`` so a ref counted as own is not
    double-counted as affected.
    """
    closure = dirty_closure(deps, {edited: "edit"})
    changed: set[tuple[NodeKind, str]] = set()
    keys: set[tuple[str, str, Any]] = set()
    for unit in closure:
        changed |= defs_by_file.get(unit, set())
        for rec in own_by_file.get(unit, ()):
            keys.add((rec.file, rec.src_id, rec.edge_kind))

    kinds_by_name: dict[str, set[NodeKind]] = defaultdict(set)
    for kind, name in changed:
        kinds_by_name[name].add(kind)
    for name, kinds in kinds_by_name.items():
        for rec in by_target.get(name, ()):
            if ref_target_kinds(rec.edge_kind) & kinds:
                keys.add((rec.file, rec.src_id, rec.edge_kind))
    return len(keys)


def _percentile(values: list[int] | list[float], q: float) -> float:
    """Nearest-rank percentile of *values* (q in [0, 1]); 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return float(ordered[idx])


def _summary(values: list[int]) -> dict[str, float]:
    return {
        "p50": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "max": float(max(values)) if values else 0.0,
        "mean": round(sum(values) / len(values), 3) if values else 0.0,
    }


def _ratio_summary(values: list[float]) -> dict[str, float]:
    return {
        "p50": round(_percentile(values, 0.5), 6),
        "p90": round(_percentile(values, 0.9), 6),
        "max": round(max(values), 6) if values else 0.0,
    }
