#!/usr/bin/env python3
"""M13 spike — memory-bounded incremental linker feasibility (#119 / #128).

M12.5 (#147/#148) put the whole-design *reads* out-of-core. The last O(design)-RAM
cost is the `update` **write** path: `pipeline._link_pass2` calls `SqliteStore.load()`
to materialise the *entire* prior graph, which `builder.link_incremental` mutates in
place. `docs/scalability.md` calls this "all-or-nothing" because the in-memory graph is
consumed by entangled passes — name resolution (`_Linker` indexes seeded from every
prior node/edge), `_gc_orphan_stubs` (keeps a stub iff it has any non-DECLARES edge),
`derive_test_covers` (whole-graph scan), and the report counts.

This spike proves the two genuinely-doubted kernels can run **bounded by the dirty
closure** with **byte-identical** results, without ever loading the prior graph:

* **resolution kernel** — re-resolve the live refs (dirty units + `affected_srcs`)
  through the *unchanged* `_Linker._resolve`, but feed it **lazy SQL-backed** indexes
  (`definitions`/`children`/`parent`/`node_obj`, via `idx_nodes_kind_name`/`idx_edges_*`)
  that only read the names/scopes those refs touch. Parity by construction on the logic;
  the risk is the lazy seeding, which the byte-identical assert pins.
* **stub-GC kernel** — decide each *closure-incident* candidate stub's survival with the
  identical "any non-DECLARES edge" + DECLARES-hosting-chain rule, querying just that
  node's edges (`idx_edges_src`/`idx_edges_dst`) — bounded, not a whole-graph walk.

Method: run a real `run_update` with `builder.link_incremental` monkeypatched to capture
its exact inputs (so the pipeline's discovery/closure/`affected_srcs` computation is
reused verbatim), and the oracle result. Then run the bounded kernels over a snapshot of
the *prior* DB and compare. Reports parity + rows-read (bounded) vs nodes+edges (the
oracle's full load) + peak RSS, across the equivalence-suite edit shapes and, if present,
the real RV32I SoC.

Usage::

    uv run python scripts/spike_m13_link.py [--soc /path/to/claude_verilog_test]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.graph import builder
from hdl_kgraph.graph.builder import (
    _PASS1_EDGE_KINDS,
    _PASS2_EDGE_KINDS,
    Node,
    _Linker,
    link_incremental,
)
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.pipeline import default_db_path, run_build, run_update
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import NODE_COLUMNS

# ---- a read counter so we can prove "bounded by the closure, not the design" ----


@dataclass
class _Counter:
    node_rows: int = 0
    edge_rows: int = 0


# --------------------------------------------------------------------------- #
# Lazy SQL-backed indexes — same shape the in-memory _Linker seeds from the
# whole prior graph, but read on demand and with dirty-file nodes excluded
# (mirrors link_incremental step 1, which removes dirty/removed-file nodes).
# --------------------------------------------------------------------------- #
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


class _LazyNodeObj(dict):
    """``id -> Node``; misses fetch the row (unless it belongs to a dirty file)."""

    def __init__(self, conn: sqlite3.Connection, dirty: set[str], ctr: _Counter) -> None:
        super().__init__()
        self._conn, self._dirty, self._ctr = conn, dirty, ctr
        self._absent: set[str] = set()

    def _fetch(self, key: str) -> Node | None:
        if key in self._absent:
            return None
        row = self._conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE id = ?", (key,)
        ).fetchone()
        self._ctr.node_rows += 1
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

    def __init__(
        self, conn: sqlite3.Connection, dirty: set[str], ctr: _Counter, *, ci: bool
    ) -> None:
        self._conn, self._dirty, self._ctr, self._ci = conn, dirty, ctr, ci
        self._overlay: dict[tuple[NodeKind, str], list[str]] = defaultdict(list)
        self._cache: dict[tuple[NodeKind, str], list[str]] = {}

    def _sql(self, key: tuple[NodeKind, str]) -> list[str]:
        if key in self._cache:
            return self._cache[key]
        kind, name = key
        if self._ci:
            sql = "SELECT id, file, attrs FROM nodes WHERE kind = ? AND lower(name) = ?"
            rows = self._conn.execute(sql, (kind.value, name))
        else:
            sql = "SELECT id, file, attrs FROM nodes WHERE kind = ? AND name = ?"
            rows = self._conn.execute(sql, (kind.value, name))
        ids: list[str] = []
        for nid, file, attrs in rows:
            self._ctr.node_rows += 1
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


def _not_in_dirty(col: str, dirty: set[str]) -> tuple[str, tuple[str, ...]]:
    """SQL fragment + params excluding rows whose *col* file is dirty (== removed)."""
    if not dirty:
        return "", ()
    placeholders = ", ".join("?" for _ in dirty)
    return f" AND {col} NOT IN ({placeholders})", tuple(dirty)


class _LazyChildren:
    """``parent_id -> [child Node]`` over ``idx_edges_src`` DECLARES (+ overlay).

    Edges incident to a dirty-file node are skipped at the SQL level — mirroring
    ``link_incremental`` step 1, which removes dirty nodes *and their edges*
    before re-seeding — so a dirty parent's stale prior children never leak in
    (the fresh ones arrive via the overlay during the splice).
    """

    def __init__(
        self, conn: sqlite3.Connection, dirty: set[str], ctr: _Counter, node_obj: _LazyNodeObj
    ) -> None:
        self._conn, self._dirty, self._ctr, self._nodes = conn, dirty, ctr, node_obj
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
            self._ctr.edge_rows += 1
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

    def __init__(self, conn: sqlite3.Connection, dirty: set[str], ctr: _Counter) -> None:
        self._conn, self._dirty, self._ctr = conn, dirty, ctr
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
        self._ctr.edge_rows += 1
        return row[0] if row is not None else default

    def __setitem__(self, key: str, value: str) -> None:
        self._overlay[key] = value


# --------------------------------------------------------------------------- #
# Bounded re-resolution: the unchanged _Linker._resolve over lazy indexes.
# --------------------------------------------------------------------------- #
def bounded_resolve(
    db_prior: Path,
    file_irs: list[FileIR],
    dirty_files: set[str],
    affected_srcs: set[str],
) -> tuple[set[tuple[Any, ...]], _Counter, nx.MultiDiGraph]:
    """Re-resolve the live refs (dirty + affected) without loading the prior graph.

    Returns three values: the emitted pass-2 edge tuples (excluding TEST_COVERS;
    the same edges the oracle produces for those srcs), the rows-read counter, and
    the delta graph (consumed by :func:`bounded_stub_gc`).
    """
    conn = sqlite3.connect(f"file:{db_prior}?mode=ro", uri=True)
    ctr = _Counter()
    try:
        linker = _Linker([])
        linker.graph = nx.MultiDiGraph()
        linker.node_obj = _LazyNodeObj(conn, dirty_files, ctr)  # type: ignore[assignment]
        linker.definitions = _LazyDefs(conn, dirty_files, ctr, ci=False)  # type: ignore[assignment]
        linker.definitions_ci = _LazyDefs(conn, dirty_files, ctr, ci=True)  # type: ignore[assignment]
        linker.children = _LazyChildren(conn, dirty_files, ctr, linker.node_obj)  # type: ignore[assignment]
        linker.parent = _LazyParent(conn, dirty_files, ctr)  # type: ignore[assignment]

        # node_file: a ref's owning compilation unit. For dirty units it comes from
        # the fresh IR; for affected clean srcs a production linker reads it from the
        # ref_index (RefRecord.file) — here we take it from the captured IRs.
        for ir in file_irs:
            for node in ir.nodes:
                linker.node_file.setdefault(node.id, ir.path)

        # Splice the dirty units' fresh nodes + local edges (link_incremental step 2b).
        # Pass-1 edges are deduped across dirty IRs, exactly as link_incremental does,
        # so a shared include spliced into several dirty units cannot duplicate a
        # DECLARES child (which would skew scope resolution vs the oracle).
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

        # Re-resolve only the live refs (link_incremental step 3).
        for ir in file_irs:
            live_unit = ir.path in dirty_files
            for ref in ir.unresolved_refs:
                if live_unit or ref.src_id in affected_srcs:
                    linker._resolve(ref)
    finally:
        conn.close()
    return _pass2_edges(linker.graph), ctr, linker.graph


def bounded_stub_gc(
    db_prior: Path,
    dirty_files: set[str],
    affected_srcs: set[str],
    delta: nx.MultiDiGraph,
    ctr: _Counter,
) -> set[str]:
    """Surviving unresolved stubs, decided WITHOUT the full prior graph.

    Assembles only the *stub neighbourhood* — every prior stub plus its incident
    edges that survive the re-link (drop TEST_COVERS, the affected srcs' pass-2
    edges, and everything incident to a dirty/removed node), merged with the
    freshly re-resolved ``delta`` — then runs the **real** ``_gc_orphan_stubs`` on
    it (parity by construction). Bounded by the stub count + their edges (the same
    set the in-memory GC examines), never the whole graph.
    """
    conn = sqlite3.connect(f"file:{db_prior}?mode=ro", uri=True)
    try:
        dirty_node_ids = {
            r[0] for r in conn.execute(_dirty_node_sql(dirty_files), tuple(dirty_files))
        }
        g = nx.MultiDiGraph()
        # every prior stub (unresolved node) — the GC's candidate universe
        stub_ids: list[str] = []
        for row in conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE json_extract(attrs, '$.unresolved') = 1"
        ):
            ctr.node_rows += 1
            if row[0] in dirty_node_ids:
                continue  # a dirty-file stub was removed with its file
            builder._add_node(g, _row_to_node(row))
            stub_ids.append(row[0])
        # their surviving incident edges (+ the other endpoint node)
        for stub in stub_ids:
            for src, dst, kind, conf, attrs in conn.execute(
                "SELECT src, dst, kind, confidence, attrs FROM edges WHERE src = ? OR dst = ?",
                (stub, stub),
            ):
                ctr.edge_rows += 1
                ek = EdgeKind(kind)
                if ek is EdgeKind.TEST_COVERS or src in dirty_node_ids or dst in dirty_node_ids:
                    continue
                if ek in _PASS2_EDGE_KINDS and src in affected_srcs:
                    continue  # re-resolved below from the delta
                _ensure_node(conn, g, src, ctr)
                _ensure_node(conn, g, dst, ctr)
                g.add_edge(src, dst, kind=ek, confidence=conf, attrs=json.loads(attrs))
        # merge the freshly re-resolved delta (new edges + any new stubs). The
        # delta materialises only stub endpoints (clean targets are bare edge
        # endpoints with no attrs), so hydrate any non-stub endpoint from the DB.
        for nid, data in delta.nodes(data=True):
            if nid in g:
                continue
            if "attrs" in data:
                g.add_node(nid, **data)
            else:
                _ensure_node(conn, g, nid, ctr)
        for u, v, data in delta.edges(data=True):
            _ensure_node(conn, g, u, ctr)
            _ensure_node(conn, g, v, ctr)
            g.add_edge(u, v, **data)
    finally:
        conn.close()
    builder._gc_orphan_stubs(g)
    return {n for n, d in g.nodes(data=True) if d["attrs"].get("unresolved")}


def _dirty_node_sql(dirty: set[str]) -> str:
    placeholders = ", ".join("?" for _ in dirty) or "''"
    return f"SELECT id FROM nodes WHERE file IN ({placeholders})"


def _ensure_node(conn: sqlite3.Connection, g: nx.MultiDiGraph, nid: str, ctr: _Counter) -> None:
    if nid in g:
        return
    row = conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes WHERE id = ?", (nid,)).fetchone()
    ctr.node_rows += 1
    if row is not None:
        builder._add_node(g, _row_to_node(row))


def _pass2_edges(graph: nx.MultiDiGraph) -> set[tuple[Any, ...]]:
    """Emitted pass-2 edges (excluding TEST_COVERS) as comparable tuples."""
    return {
        (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
        for u, v, d in graph.edges(data=True)
        if d["kind"] in _PASS2_EDGE_KINDS and d["kind"] is not EdgeKind.TEST_COVERS
    }


# --------------------------------------------------------------------------- #
# Oracle capture: run the real update, grabbing link_incremental's exact inputs.
# --------------------------------------------------------------------------- #
@dataclass
class _Capture:
    file_irs: list[FileIR]
    dirty_files: set[str]
    affected_srcs: set[str]
    result_graph: nx.MultiDiGraph


def _run_update_capturing(root: Path) -> _Capture | None:
    """Run ``run_update`` with ``link_incremental`` wrapped to capture its I/O.

    Returns None if the pipeline took the full-relink fallback (no incremental
    link happened — e.g. VHDL/binds/enrich), which this spike does not model.
    """
    captured: dict[str, Any] = {}
    real = link_incremental

    def _spy(
        file_irs: list[FileIR],
        prior_graph: nx.MultiDiGraph,
        dirty_files: set[str],
        affected_srcs: set[str],
        warnings: list[str] | None = None,
    ) -> Any:
        graph, refs = real(file_irs, prior_graph, dirty_files, set(affected_srcs), warnings)
        captured["cap"] = _Capture(
            file_irs=list(file_irs),
            dirty_files=set(dirty_files),
            affected_srcs=set(affected_srcs),
            result_graph=graph,
        )
        return graph, refs

    builder.link_incremental = _spy  # type: ignore[assignment]
    # pipeline imported the symbol into its namespace; patch there too.
    import hdl_kgraph.pipeline as pl

    pl_real = getattr(pl, "link_incremental", None)
    if pl_real is not None:
        pl.link_incremental = _spy  # type: ignore[assignment]
    try:
        run_update(root)
    finally:
        builder.link_incremental = real  # type: ignore[assignment]
        if pl_real is not None:
            pl.link_incremental = pl_real  # type: ignore[assignment]
    return captured.get("cap")


def _oracle_pass2_edges(cap: _Capture) -> set[tuple[Any, ...]]:
    return _pass2_edges(cap.result_graph)


def _live_srcs(cap: _Capture) -> set[str]:
    live: set[str] = set(cap.affected_srcs)
    for ir in cap.file_irs:
        if ir.path in cap.dirty_files:
            for ref in ir.unresolved_refs:
                live.add(ref.src_id)
    return live


# --------------------------------------------------------------------------- #
# Edit shapes (mirroring tests/test_incremental_equivalence.py).
# --------------------------------------------------------------------------- #
def _make_project(tmp: Path) -> Path:
    (tmp / "defs.svh").write_text("`define WIDTH 8\n")
    (tmp / "leaf.sv").write_text(
        '`include "defs.svh"\n'
        "module leaf(input logic [`WIDTH-1:0] a, output logic [`WIDTH-1:0] y);\n"
        "  assign y = a;\nendmodule\n"
    )
    (tmp / "my_pkg.sv").write_text("package my_pkg;\n  localparam int K = 4;\nendpackage\n")
    (tmp / "mid.sv").write_text(
        "module mid(input logic [7:0] a, output logic [7:0] y);\n"
        "  import my_pkg::*;\n  leaf u_leaf(.a(a), .y(y));\nendmodule\n"
    )
    (tmp / "top.sv").write_text(
        "module top(input logic [7:0] a, output logic [7:0] y);\n"
        "  mid u_mid(.a(a), .y(y));\nendmodule\n"
    )
    return tmp


def _edit_rename_instance(root: Path) -> None:
    p = root / "top.sv"
    p.write_text(p.read_text().replace("u_mid", "u_mid2"))


def _edit_add_module(root: Path) -> None:
    p = root / "mid.sv"
    p.write_text(p.read_text().replace("leaf u_leaf", "leaf2 u_leaf").rstrip())
    (root / "leaf2.sv").write_text(
        "module leaf2(input logic [7:0] a, output logic [7:0] y);\n  assign y = ~a;\nendmodule\n"
    )


def _edit_remove_addee(root: Path) -> None:
    # Re-point mid back to leaf, drop the just-added leaf2 (exercises stub churn).
    p = root / "mid.sv"
    p.write_text(p.read_text().replace("leaf2 u_leaf", "leaf u_leaf"))
    (root / "leaf2.sv").unlink(missing_ok=True)


def _edit_header(root: Path) -> None:
    (root / "defs.svh").write_text("`define WIDTH 16\n")


def _make_project_with_stub(tmp: Path) -> Path:
    """A project whose `top` instantiates an undefined `gizmo` (an unresolved stub)."""
    _make_project(tmp)
    (tmp / "top.sv").write_text(
        "module top(input logic [7:0] a, output logic [7:0] y);\n"
        "  mid u_mid(.a(a), .y(y));\n  gizmo u_gz(.a(a));\nendmodule\n"
    )
    return tmp


def _edit_resolve_stub(root: Path) -> None:
    """Add `gizmo.sv`, resolving `top.u_gz` — the prior gizmo stub must be dropped."""
    (root / "gizmo.sv").write_text("module gizmo(input logic [7:0] a);\nendmodule\n")


_FIXTURE_EDITS = [
    ("rename_instance", _make_project, _edit_rename_instance),
    ("add_module", _make_project, _edit_add_module),
    ("edit_header", _make_project, _edit_header),
    ("resolve_stub", _make_project_with_stub, _edit_resolve_stub),
]


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def _run_case(label: str, root: Path, edit: Any) -> bool:
    run_build(root)
    db = default_db_path(root)
    db_prior = db.with_name(db.name + ".prior")
    edit(root)
    # snapshot the prior DB (+ wal sidecars) before update overwrites it
    for suffix in ("", "-wal", "-shm"):
        s = db.with_name(db.name + suffix)
        if s.exists():
            shutil.copy2(s, db_prior.with_name(db_prior.name + suffix))

    cap = _run_update_capturing(root)
    if cap is None:
        print(f"  {label:16s} SKIPPED (full-relink fallback; not modelled)")
        return True

    oracle_all = _oracle_pass2_edges(cap)
    live = _live_srcs(cap)
    oracle = {e for e in oracle_all if e[0] in live}

    edges, ctr, delta = bounded_resolve(db_prior, cap.file_irs, cap.dirty_files, cap.affected_srcs)
    bounded = {e for e in edges if e[0] in live}

    # stub-GC kernel: surviving stubs, bounded vs the oracle's whole-graph GC
    oracle_stubs = {n for n, d in cap.result_graph.nodes(data=True) if d["attrs"].get("unresolved")}
    bounded_stubs = bounded_stub_gc(db_prior, cap.dirty_files, cap.affected_srcs, delta, ctr)

    total_nodes, total_edges = _db_counts(db_prior)
    res_ok = bounded == oracle
    gc_ok = bounded_stubs == oracle_stubs
    ok = res_ok and gc_ok
    print(
        f"  {label:16s} resolve={'ok' if res_ok else 'FAIL':4s} "
        f"stub_gc={'ok' if gc_ok else 'FAIL':4s} "
        f"live_srcs={len(live):4d} edges={len(oracle):4d} stubs={len(oracle_stubs):4d}  "
        f"bounded_rows={ctr.node_rows + ctr.edge_rows:6d} "
        f"(full load {total_nodes + total_edges:6d})"
    )
    if not res_ok:
        _diff(oracle, bounded)
    if not gc_ok:
        print(f"      stub only-oracle : {sorted(oracle_stubs - bounded_stubs)[:6]}")
        print(f"      stub only-bounded: {sorted(bounded_stubs - oracle_stubs)[:6]}")
    for suffix in ("", "-wal", "-shm"):
        db_prior.with_name(db_prior.name + suffix).unlink(missing_ok=True)
    return ok


def _db_counts(db: Path) -> tuple[int, int]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return n, e
    finally:
        conn.close()


def _diff(oracle: set[tuple[Any, ...]], bounded: set[tuple[Any, ...]]) -> None:
    only_o = sorted(oracle - bounded)[:8]
    only_b = sorted(bounded - oracle)[:8]
    for e in only_o:
        print(f"      only-oracle : {e}")
    for e in only_b:
        print(f"      only-bounded: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--soc", type=Path, default=None, help="path to a cloned real SoC repo")
    ap.add_argument(
        "--design",
        type=Path,
        default=None,
        help="run the bounded-vs-full parity/locality check on any built design root",
    )
    ap.add_argument("--keep", action="store_true", help="keep temp dirs")
    args = ap.parse_args()

    import tempfile

    ok = True
    print("=== fixture edit shapes ===")
    for label, make, edit in _FIXTURE_EDITS:
        tmp = Path(tempfile.mkdtemp(prefix=f"m13_{label}_"))
        try:
            make(tmp)
            ok &= _run_case(label, tmp, edit)
        finally:
            if not args.keep:
                shutil.rmtree(tmp, ignore_errors=True)

    if args.soc and args.soc.exists():
        print(f"\n=== real SoC: {args.soc} ===")
        work = Path(tempfile.mkdtemp(prefix="m13_soc_"))
        try:
            src = args.soc / "rtl" if (args.soc / "rtl").is_dir() else args.soc
            shutil.copytree(src, work / "rtl")
            ok &= _run_case("soc_touch_top", work / "rtl", _soc_touch_first_module)
        finally:
            if not args.keep:
                shutil.rmtree(work, ignore_errors=True)
    else:
        print("\n(no --soc given; skipping the real-design case)")

    if args.design and args.design.exists():
        print(f"\n=== design: {args.design} ===")
        work = Path(tempfile.mkdtemp(prefix="m13_design_"))
        try:
            shutil.copytree(args.design, work / "design")
            ok &= _run_case("design_touch", work / "design", _soc_touch_first_module)
        finally:
            if not args.keep:
                shutil.rmtree(work, ignore_errors=True)

    print(f"\n{'ALL PARITY OK' if ok else 'PARITY FAILURES'}")
    return 0 if ok else 1


def _soc_touch_first_module(root: Path) -> None:
    """Append a harmless comment to the first .sv file (a content-changing edit)."""
    svs = sorted(root.rglob("*.sv"))
    if svs:
        svs[0].write_text(svs[0].read_text() + "\n// m13 spike touch\n")


if __name__ == "__main__":
    sys.exit(main())
