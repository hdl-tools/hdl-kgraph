#!/usr/bin/env python3
"""M12 graph-layer spike — does an off-the-shelf layer hit the RAM target?

Milestone M12 of the v2.0 epic (#128) is a decision gate: M11 showed
`SqliteStore.load()` is graph-CPU-bound and that **peak RAM from materialising the
whole NetworkX graph is the binding constraint** (~2.3x on-disk; a 100 GB design
needs ~225 GB RAM). M12 evaluates whether an off-the-shelf layer avoids that wall,
across three tracks: SQL-native scans (this PR), `rustworkx` (in-memory), and
`kuzu` (out-of-core). The written verdict lands in `docs/v2/m12_graph_layer.md`.

Method: run one **representative whole-design scan** on each available backend,
assert byte-identical parity against the NetworkX path (the oracle), then measure
peak RSS / time across a file-count sweep and report the RSS scaling slope — a
*flat* slope means the backend's RAM is bounded (reaches 100 GB); a *linear* slope
means it does not, by itself.

The representative scan is a **whole-design structural summary** — node-kind and
edge-kind histograms plus the INSTANTIATES fan-in distribution. It exercises the
same full node+edge iteration as `load()` and is exactly portable to SQL / kuzu /
rustworkx. (The clock/CDC/UVM scans share this access pattern but are empty on the
combinational `gen_corpus` designs, so they cannot measure RAM scaling here; their
full port is M13 work.)

Reuses the M11 harness primitives (`scripts/profile_v2.py`) for subprocess-isolated
peak-RSS measurement and the least-squares fit; stdlib only for the SQL track.

Usage::

    python scripts/spike_m12.py --files-sweep 2000,10000,50000 --repeat 3 [--dense]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from gen_corpus import generate, generate_dense  # noqa: E402
from profile_v2 import _cpu_seconds, _lstsq, _peak_rss_bytes, _RSSSampler  # noqa: E402

from hdl_kgraph.graph.analysis import edge_kind_histogram, node_kind_histogram  # noqa: E402
from hdl_kgraph.pipeline import default_db_path, run_build  # noqa: E402
from hdl_kgraph.schema import EdgeKind, Language, NodeKind  # noqa: E402
from hdl_kgraph.storage.sqlite_store import (  # noqa: E402
    EDGE_COLUMNS,
    NODE_COLUMNS,
    SqliteStore,
)

_RESULT_MARKER = "__SPIKE_M12_RESULT__"
_INSTANTIATES = EdgeKind.INSTANTIATES.value


# --------------------------------------------------------------------------- #
# The representative whole-design scan, computed two ways (must agree exactly).
# --------------------------------------------------------------------------- #
def _scan_from_graph(graph: Any) -> dict[str, Any]:
    """Oracle: the scan over a materialised NetworkX graph (graph/analysis.py)."""
    fanin: Counter[str] = Counter()
    for _src, dst, data in graph.edges(data=True):
        if data["kind"] is EdgeKind.INSTANTIATES:
            fanin[dst] += 1
    return {
        "node_kinds": dict(node_kind_histogram(graph)),
        "edge_kinds": dict(edge_kind_histogram(graph)),
        "inst_fanin": {
            "total_edges": sum(fanin.values()),
            "distinct_targets": len(fanin),
            "max_fanin": max(fanin.values(), default=0),
        },
    }


def _scan_via_sql(db: Path) -> tuple[dict[str, Any], int, int]:
    """Track A: the same scan as pure SQL aggregation — never builds a graph."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        node_kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind"))
        edge_kinds = dict(conn.execute("SELECT kind, COUNT(*) FROM edges GROUP BY kind"))
        fanin_rows = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = ? GROUP BY dst", (_INSTANTIATES,)
        ).fetchall()
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    finally:
        conn.close()
    result = {
        "node_kinds": node_kinds,
        "edge_kinds": edge_kinds,
        "inst_fanin": {
            "total_edges": sum(c for (c,) in fanin_rows),
            "distinct_targets": len(fanin_rows),
            "max_fanin": max((c for (c,) in fanin_rows), default=0),
        },
    }
    return result, node_count, edge_count


# --------------------------------------------------------------------------- #
# Backend stages (each runs in its own child process for a clean peak RSS).
# --------------------------------------------------------------------------- #
def _stage_networkx(db: Path) -> dict[str, Any]:
    """Baseline: materialise the whole graph, then scan it (the M11 wall)."""
    with _RSSSampler() as sampler:
        cpu0 = _cpu_seconds()
        started = time.perf_counter()
        graph, _files, _meta = SqliteStore(db).load()
        result = _scan_from_graph(graph)
        wall_s = time.perf_counter() - started
        cpu_s = _cpu_seconds() - cpu0
    return {
        "result": result,
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
    }


def _stage_sql(db: Path) -> dict[str, Any]:
    """Track A: the scan in pure SQL — RAM should stay flat (no graph built)."""
    with _RSSSampler() as sampler:
        cpu0 = _cpu_seconds()
        started = time.perf_counter()
        result, node_count, edge_count = _scan_via_sql(db)
        wall_s = time.perf_counter() - started
        cpu_s = _cpu_seconds() - cpu0
    return {
        "result": result,
        "node_count": node_count,
        "edge_count": edge_count,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
    }


def _rustworkx_available() -> bool:
    try:
        import rustworkx  # noqa: F401
    except ImportError:
        return False
    return True


def _build_rustworkx(db: Path, full_payload: bool) -> Any:
    """Build a ``rustworkx.PyDiGraph`` from the DB. ``full_payload`` mirrors the
    NetworkX per-node/edge data (the fair, apples-to-apples comparison); otherwise
    each node/edge stores only its kind string — an illustrative *flattened-payload*
    upper bound that also flattens what `load()` keeps as Python dicts."""
    import rustworkx as rx

    g = rx.PyDiGraph()
    index: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        for node_id, kind, name, qn, file, ls, le, lang, attrs in conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes"
        ):
            payload: Any = (
                {
                    "kind": NodeKind(kind),
                    "name": name,
                    "qualified_name": qn,
                    "file": file,
                    "line_span": (ls, le),
                    "language": Language(lang),
                    "attrs": json.loads(attrs),
                }
                if full_payload
                else kind
            )
            index[node_id] = g.add_node(payload)
        for src, dst, kind, conf, attrs in conn.execute(f"SELECT {EDGE_COLUMNS} FROM edges"):
            epayload: Any = (
                {"kind": EdgeKind(kind), "confidence": conf, "attrs": json.loads(attrs)}
                if full_payload
                else kind
            )
            g.add_edge(index[src], index[dst], epayload)
    finally:
        conn.close()
    return g


def _scan_rustworkx(g: Any, full_payload: bool) -> dict[str, Any]:
    """The representative scan over a rustworkx graph (kind lives in the payload)."""
    node_kinds = Counter(p["kind"].value for p in g.nodes()) if full_payload else Counter(g.nodes())
    edge_kinds: Counter[str] = Counter()
    fanin: Counter[int] = Counter()
    for _u, v, w in g.weighted_edge_list():
        kind = w["kind"].value if full_payload else w
        edge_kinds[kind] += 1
        if kind == _INSTANTIATES:
            fanin[v] += 1
    return {
        "node_kinds": dict(node_kinds),
        "edge_kinds": dict(edge_kinds),
        "inst_fanin": {
            "total_edges": sum(fanin.values()),
            "distinct_targets": len(fanin),
            "max_fanin": max(fanin.values(), default=0),
        },
    }


def _stage_rustworkx_impl(db: Path, full_payload: bool) -> dict[str, Any]:
    with _RSSSampler() as sampler:
        cpu0 = _cpu_seconds()
        started = time.perf_counter()
        g = _build_rustworkx(db, full_payload)
        result = _scan_rustworkx(g, full_payload)
        wall_s = time.perf_counter() - started
        cpu_s = _cpu_seconds() - cpu0
    return {
        "result": result,
        "node_count": g.num_nodes(),
        "edge_count": g.num_edges(),
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
    }


def _stage_rustworkx(db: Path) -> dict[str, Any]:
    """Track B: rustworkx with NetworkX-equivalent payloads (the fair comparison)."""
    return _stage_rustworkx_impl(db, full_payload=True)


def _stage_rustworkx_flat(db: Path) -> dict[str, Any]:
    """Track B upper bound: rustworkx storing only kinds (flattened payload)."""
    return _stage_rustworkx_impl(db, full_payload=False)


def _kuzu_available() -> bool:
    try:
        import kuzu  # noqa: F401
    except ImportError:
        return False
    return True


def _kuzu_dir(db: Path) -> Path:
    """The embedded kuzu DB directory derived from the SQLite db path."""
    return db.parent / "graph_kuzu"


def _build_kuzu(db: Path) -> None:
    """Convert the SQLite graph into an embedded kuzu DB (one-time, like the build
    that produces graph.db) — streamed via CSV so the conversion stays bounded."""
    import csv
    import shutil

    import kuzu

    # Idempotent: a stale DB would fail CREATE TABLE. kuzu may store the database
    # as a single file (recent versions) or a directory (older), plus a .wal
    # sidecar — clear whatever is there.
    kuzu_dir = _kuzu_dir(db)
    for stale in (kuzu_dir, kuzu_dir.with_name(kuzu_dir.name + ".wal")):
        if stale.is_dir():
            shutil.rmtree(stale)
        elif stale.exists():
            stale.unlink()
    nodes_csv = db.parent / "kuzu_nodes.csv"
    edges_csv = db.parent / "kuzu_edges.csv"
    conn_sq = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        with open(nodes_csv, "w", newline="") as fh:
            csv.writer(fh).writerows(conn_sq.execute("SELECT id, kind FROM nodes"))
        with open(edges_csv, "w", newline="") as fh:
            csv.writer(fh).writerows(conn_sq.execute("SELECT src, dst, kind FROM edges"))
    finally:
        conn_sq.close()
    conn = kuzu.Connection(kuzu.Database(str(kuzu_dir)))
    conn.execute("CREATE NODE TABLE Node(id STRING, kind STRING, PRIMARY KEY(id))")
    conn.execute("CREATE REL TABLE Edge(FROM Node TO Node, kind STRING)")
    conn.execute(f"COPY Node FROM '{nodes_csv}'")
    conn.execute(f"COPY Edge FROM '{edges_csv}'")


def _kuzu_one(conn: Any, query: str, params: dict[str, Any] | None = None) -> list[Any]:
    res = conn.execute(query, params) if params else conn.execute(query)
    rows = []
    while res.has_next():
        rows.append(res.get_next())
    return rows


def _stage_kuzu(db: Path) -> dict[str, Any]:
    """Track C: out-of-core query over the prebuilt embedded kuzu DB — RAM should
    stay flat (kuzu answers from disk, never materialising the whole graph)."""
    import kuzu

    with _RSSSampler() as sampler:
        cpu0 = _cpu_seconds()
        started = time.perf_counter()
        conn = kuzu.Connection(kuzu.Database(str(_kuzu_dir(db))))
        node_kinds = {k: c for k, c in _kuzu_one(conn, "MATCH (n:Node) RETURN n.kind, COUNT(*)")}
        edge_kinds = {
            k: c for k, c in _kuzu_one(conn, "MATCH ()-[e:Edge]->() RETURN e.kind, COUNT(*)")
        }
        d, m, tot = _kuzu_one(
            conn,
            "MATCH ()-[e:Edge {kind:$k}]->(n:Node) WITH n, COUNT(*) AS c "
            "RETURN COUNT(n), MAX(c), SUM(c)",
            {"k": _INSTANTIATES},
        )[0]
        node_count = _kuzu_one(conn, "MATCH (n:Node) RETURN COUNT(*)")[0][0]
        edge_count = _kuzu_one(conn, "MATCH ()-[e:Edge]->() RETURN COUNT(*)")[0][0]
        wall_s = time.perf_counter() - started
        cpu_s = _cpu_seconds() - cpu0
    result = {
        "node_kinds": node_kinds,
        "edge_kinds": edge_kinds,
        "inst_fanin": {
            "total_edges": int(tot or 0),
            "distinct_targets": int(d or 0),
            "max_fanin": int(m or 0),
        },
    }
    return {
        "result": result,
        "node_count": node_count,
        "edge_count": edge_count,
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
    }


#: backend name -> (stage fn, availability probe).
_BACKENDS: dict[str, tuple[Callable[[Path], dict[str, Any]], Callable[[], bool]]] = {
    "networkx": (_stage_networkx, lambda: True),
    "sql": (_stage_sql, lambda: True),
    "rustworkx": (_stage_rustworkx, _rustworkx_available),
    "rustworkx_flat": (_stage_rustworkx_flat, _rustworkx_available),
    "kuzu": (_stage_kuzu, _kuzu_available),
}


# --------------------------------------------------------------------------- #
# Parent orchestration.
# --------------------------------------------------------------------------- #
def _run_child(stage: str, target: Path) -> dict[str, Any]:
    """Run one stage in a fresh process. Isolation matters twice over: it gives a
    clean per-stage peak RSS, *and* it keeps the parent lean — a heavy parent would
    leak its RSS into every fork+exec child's ``ru_maxrss`` high-water mark and mask
    the very thing we measure (so the build runs in a child too, never in-parent)."""
    proc = subprocess.run(
        [sys.executable, __file__, "--_child-stage", stage, "--_child-target", str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"stage {stage!r} failed:\n{proc.stderr}")
    for line in proc.stdout.splitlines():
        if line.startswith(_RESULT_MARKER):
            return json.loads(line[len(_RESULT_MARKER) :])
    raise RuntimeError(f"stage {stage!r} produced no result:\n{proc.stdout}\n{proc.stderr}")


def _agg(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Median of timings, max of RSS; result/counts from the first (deterministic) run."""
    return {
        "result": runs[0]["result"],
        "node_count": runs[0]["node_count"],
        "edge_count": runs[0]["edge_count"],
        "wall_s": statistics.median(r["wall_s"] for r in runs),
        "cpu_s": statistics.median(r["cpu_s"] for r in runs),
        "peak_rss": max(r["peak_rss"] for r in runs),
        "sampled_peak_rss": max(r["sampled_peak_rss"] for r in runs),
        "wall_spread": (min(r["wall_s"] for r in runs), max(r["wall_s"] for r in runs)),
    }


def _profile_point(
    root: Path, dense: bool, files: int, repeat: int, backends: list[str]
) -> dict[str, Any]:
    (generate_dense if dense else generate)(root, files)
    _run_child("build", root)  # build the DB in a child so the parent stays lean
    db = default_db_path(root)
    if "kuzu" in backends:
        _run_child("kuzu_build", db)  # one-time SQLite→kuzu conversion (not measured)
    measured = {b: _agg([_run_child(b, db) for _ in range(repeat)]) for b in backends}

    # Parity gate: every backend must compute the byte-identical scan.
    oracle = measured["networkx"]["result"]
    parity = {b: (measured[b]["result"] == oracle) for b in backends}
    return {"files": files, "dense": dense, "backends": measured, "parity": parity}


def _print_point(p: dict[str, Any]) -> None:
    mib = 1024 * 1024
    nx = p["backends"]["networkx"]
    print(
        f"\n=== {p['files']} files{' [dense]' if p['dense'] else ''}: "
        f"{nx['node_count']} nodes / {nx['edge_count']} edges ==="
    )
    for name, m in p["backends"].items():
        ok = "ok" if p["parity"][name] else "PARITY-FAIL"
        print(
            f"  {name:9s} scan {m['wall_s'] * 1000:8.1f} ms  "
            f"peak {m['peak_rss'] / mib:7.0f} MiB  "
            f"({m['peak_rss'] / m['node_count']:6.0f} B/node)  [{ok}]"
        )


def _report(points: list[dict[str, Any]], backends: list[str]) -> None:
    mib = 1024 * 1024
    if any(not all(p["parity"].values()) for p in points):
        print("\n[!] PARITY FAILURE — a backend disagrees with the NetworkX oracle.")
    if len(points) < 2:
        return
    nodes = [float(p["backends"]["networkx"]["node_count"]) for p in points]
    print("\n=== peak-RSS scaling vs node count (flat slope ⇒ bounded ⇒ reaches 100 GB) ===")
    for b in backends:
        rss = [float(p["backends"][b]["peak_rss"]) for p in points]
        slope, intercept, r2 = _lstsq(nodes, rss)
        verdict = "BOUNDED (flat)" if abs(slope) < 50 else f"linear @ {slope:.0f} B/node"
        print(
            f"  {b:9s} RSS = {slope:7.1f} B/node * N + {intercept / mib:6.0f} MiB  "
            f"(R²={r2:.4f})  → {verdict}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=None)
    parser.add_argument("--files-sweep", type=str, default=None)
    parser.add_argument("--dense", action="store_true")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--keep", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--_child-stage", dest="child_stage", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--_child-target", dest="child_target", default=None, help=argparse.SUPPRESS
    )
    args = parser.parse_args()

    if args.child_stage == "build":
        report = run_build(Path(args.child_target))
        print(
            _RESULT_MARKER
            + json.dumps({"node_count": report.node_count, "edge_count": report.edge_count})
        )
        return 0
    if args.child_stage == "kuzu_build":
        _build_kuzu(Path(args.child_target))
        print(_RESULT_MARKER + json.dumps({"ok": True}))
        return 0
    if args.child_stage:
        stage_fn, _probe = _BACKENDS[args.child_stage]
        print(_RESULT_MARKER + json.dumps(stage_fn(Path(args.child_target))))
        return 0

    backends = [name for name, (_fn, probe) in _BACKENDS.items() if probe()]
    skipped = [name for name in _BACKENDS if name not in backends]
    if skipped:
        print(f"skipped (dependency not installed): {', '.join(skipped)}")

    counts = (
        [int(c) for c in args.files_sweep.split(",")] if args.files_sweep else [args.files or 2000]
    )
    points: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-m12-") as tmp:
        base = args.keep if args.keep is not None else Path(tmp)
        for files in counts:
            root = base / f"corpus_{files}{'_dense' if args.dense else ''}"
            point = _profile_point(root, args.dense, files, args.repeat, backends)
            _print_point(point)
            points.append(point)

    _report(points, backends)
    if args.json_out:
        args.json_out.write_text(json.dumps(points, indent=2))
        print(f"\nraw results -> {args.json_out}")
    return 0 if all(all(p["parity"].values()) for p in points) else 1


if __name__ == "__main__":
    raise SystemExit(main())
