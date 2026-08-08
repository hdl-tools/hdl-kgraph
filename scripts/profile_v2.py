#!/usr/bin/env python3
"""M11 profiling harness — memory + CPU profile of build / summaries / load().

Milestone M11 of the v2.0 epic (GitHub #128) is a *decision gate*: it must pin
the dominant cost of the v1 architecture and select the M12 path (rustworkx vs
kuzu vs SQL-native scans). This script is the measurement tool; the written
verdict lives in ``docs/v2/m11_profiling.md`` (PR 2).

It profiles three cost centres at a sweep of design sizes and quantifies two
splits the gate needs:

* **CPU-vs-memory** — wall-clock and true CPU seconds beside peak RSS, so we can
  say whether a design stops scaling because it is too slow or because it no
  longer fits.
* **SQLite-I/O-vs-graph-CPU** — ``SqliteStore.load()`` decomposed into row fetch
  vs graph construction vs ``json.loads`` of the per-node/edge ``attrs``.

Peak RSS is attributed cleanly by running each stage in its own child process
(``resource.getrusage`` reports a per-process high-water mark, so stages in one
process contaminate each other). Memory is split three ways — fetch-only vs
graph-with-raw-attrs vs full load — and cross-checked against ``tracemalloc``.
The sweep is normalised to bytes/node and time/node and extrapolated (with R²)
to the 10-100 GB regime that ``docs/scalability.md`` says "does not load".

Stdlib only (``resource``/``tracemalloc``/``/proc``); no new runtime deps.
Linux-only for the RSS numbers (``ru_maxrss`` in KiB, ``/proc/self/statm``),
matching the ``docs/benchmarks.md`` container baselines.

Usage::

    python scripts/profile_v2.py --files-sweep 2000,10000,50000 [--dense] [--repeat 2]
    python scripts/profile_v2.py --files 2000          # single point, self-check
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from gen_corpus import generate, generate_dense  # noqa: E402

from hdl_kgraph.graph.summary import build_summaries  # noqa: E402
from hdl_kgraph.pipeline import default_db_path, run_build  # noqa: E402
from hdl_kgraph.storage.sqlite_store import (  # noqa: E402
    EDGE_COLUMNS,
    NODE_COLUMNS,
    SqliteStore,
    add_edge_row,
    add_node_row,
)

_PAGESIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
_RESULT_MARKER = "__PROFILE_V2_RESULT__"


# --------------------------------------------------------------------------- #
# Low-level measurement helpers
# --------------------------------------------------------------------------- #
def _peak_rss_bytes() -> int:
    """Process peak RSS so far (``ru_maxrss`` is KiB on Linux)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024


def _cpu_seconds() -> float:
    """True CPU time (user+system) for this process *and* its reaped children —
    the latter captures the parallel parse pool's work during ``build``."""
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime


def _statm_rss_bytes() -> int:
    """Current (not peak) RSS from ``/proc/self/statm`` — for live sampling."""
    with open("/proc/self/statm") as fh:
        return int(fh.read().split()[1]) * _PAGESIZE


class _RSSSampler:
    """Background thread polling current RSS to catch a transient peak."""

    def __init__(self, interval: float = 0.05) -> None:
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.peak = 0

    def _run(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, _statm_rss_bytes())
            self._stop.wait(self._interval)

    def __enter__(self) -> _RSSSampler:
        self.peak = _statm_rss_bytes()
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.peak = max(self.peak, _statm_rss_bytes())
        self._stop.set()
        self._thread.join()


def _lstsq(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """Least-squares fit y = slope*x + intercept; returns (slope, intercept, R²)."""
    n = len(xs)
    if n < 2:
        return 0.0, ys[0] if ys else 0.0, 0.0
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx if sxx else 0.0
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 1.0
    return slope, intercept, r2


# --------------------------------------------------------------------------- #
# Child-process stages (one stage per process for a clean peak RSS)
# --------------------------------------------------------------------------- #
def _stage_build(root: Path) -> dict[str, Any]:
    """Full build; report the existing per-phase wall-clock plus peak RSS/CPU."""
    cpu0 = _cpu_seconds()
    started = time.perf_counter()
    report = run_build(root)
    elapsed = time.perf_counter() - started
    cpu_s = _cpu_seconds() - cpu0
    db = default_db_path(root)
    return {
        "node_count": report.node_count,
        "edge_count": report.edge_count,
        "parsed_files": report.parsed_files,
        "discover_s": report.discover_s,
        "parse_s": report.parse_s,
        "link_s": report.link_s,
        "persist_s": report.persist_s,
        "build_s": elapsed,
        "db_bytes": db.stat().st_size if db.exists() else 0,
        "peak_rss": _peak_rss_bytes(),
        "cpu_s": cpu_s,
    }


def _profiled_load(db: Path) -> dict[str, Any]:
    """Mirror of ``SqliteStore.load`` (sqlite_store.py:707) instrumented for the
    SQLite-I/O-vs-graph-CPU split. Read-only: it reuses the public
    ``add_node_row``/``add_edge_row`` and column lists so it stays byte-faithful
    to the real loader."""
    import networkx as nx

    store = SqliteStore(db)
    timings: dict[str, float] = {}
    with store._connect() as conn:  # noqa: SLF001 — harness mirrors the loader
        t = time.perf_counter()
        node_rows = list(conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes"))
        timings["nodes_fetch_s"] = time.perf_counter() - t

        t = time.perf_counter()
        edge_rows = list(conn.execute(f"SELECT {EDGE_COLUMNS} FROM edges"))
        timings["edges_fetch_s"] = time.perf_counter() - t

    # Graph construction (json.loads + enum + networkx insert) on already-fetched
    # rows, so the fetch cost above is excluded from the graph-CPU term.
    graph = nx.MultiDiGraph()
    t = time.perf_counter()
    for row in node_rows:
        add_node_row(graph, row)
    timings["nodes_build_s"] = time.perf_counter() - t

    t = time.perf_counter()
    for row in edge_rows:
        add_edge_row(graph, row)
    timings["edges_build_s"] = time.perf_counter() - t

    # Isolate the json.loads term: re-parse every attrs blob alone, the same work
    # add_*_row does inline (sqlite_store.py:292/300).
    t = time.perf_counter()
    for row in node_rows:
        json.loads(row[8])
    for row in edge_rows:
        json.loads(row[4])
    timings["json_loads_s"] = time.perf_counter() - t

    fetch_s = timings["nodes_fetch_s"] + timings["edges_fetch_s"]
    build_s = timings["nodes_build_s"] + timings["edges_build_s"]
    timings["load_s"] = fetch_s + build_s
    timings["io_s"] = fetch_s
    timings["graph_cpu_s"] = build_s
    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "timings": timings,
    }


def _tracemalloc_breakdown(db: Path) -> dict[str, int]:
    """Python-heap attribution of a full load: json attrs vs graph vs sqlite."""
    gc.collect()
    tracemalloc.start()
    graph, _files, _meta = SqliteStore(db).load()
    snapshot = tracemalloc.take_snapshot()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    buckets = {"json_attrs": 0, "networkx": 0, "sqlite_store": 0, "other": 0}
    for stat in snapshot.statistics("lineno"):
        frame = stat.traceback[0]
        fn = frame.filename
        if fn.endswith("sqlite_store.py") and frame.lineno in (292, 300):
            buckets["json_attrs"] += stat.size
        elif "networkx" in fn:
            buckets["networkx"] += stat.size
        elif fn.endswith("sqlite_store.py"):
            buckets["sqlite_store"] += stat.size
        else:
            buckets["other"] += stat.size
    buckets["traced_peak"] = peak
    del graph
    return buckets


def _stage_load_timing(db: Path) -> dict[str, Any]:
    """The SQLite-I/O-vs-graph-CPU timing split only (no memory headline — this
    process materialises row-lists *and* the graph, so its RSS is not the real
    loader's; ``load_full`` owns the ceiling number)."""
    return _profiled_load(db)


def _stage_load_full(db: Path) -> dict[str, Any]:
    """Variant C / the headline: the *real* streaming ``SqliteStore.load`` alone,
    so peak RSS is the true whole-graph ceiling and cpu-vs-wall is comparable."""
    with _RSSSampler() as sampler:
        cpu0 = _cpu_seconds()
        started = time.perf_counter()
        graph, _files, _meta = SqliteStore(db).load()
        wall_s = time.perf_counter() - started
        cpu_s = _cpu_seconds() - cpu0
    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "wall_s": wall_s,
        "cpu_s": cpu_s,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
    }


def _stage_load_trace(db: Path) -> dict[str, Any]:
    """Python-heap attribution of a full load (tracemalloc perturbs memory, so it
    runs in its own process and never feeds the RSS ceiling)."""
    return {"tracemalloc": _tracemalloc_breakdown(db)}


def _stage_load_fetch(db: Path) -> dict[str, Any]:
    """Memory variant A: fetch + iterate all rows, build no graph."""
    store = SqliteStore(db)
    with store._connect() as conn:  # noqa: SLF001
        nodes = sum(1 for _ in conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes"))
        edges = sum(1 for _ in conn.execute(f"SELECT {EDGE_COLUMNS} FROM edges"))
    return {"node_count": nodes, "edge_count": edges, "peak_rss": _peak_rss_bytes()}


def _stage_load_rawattrs(db: Path) -> dict[str, Any]:
    """Memory variant B: build the graph but keep attrs as the raw JSON string
    (skip json.loads), isolating the deserialised-attrs memory term."""
    import networkx as nx

    from hdl_kgraph.schema import EdgeKind, Language, NodeKind

    store = SqliteStore(db)
    graph = nx.MultiDiGraph()
    with store._connect() as conn:  # noqa: SLF001
        for r in conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes"):
            graph.add_node(
                r[0],
                kind=NodeKind(r[1]),
                name=r[2],
                qualified_name=r[3],
                file=r[4],
                line_span=(r[5], r[6]),
                language=Language(r[7]),
                attrs=r[8],
            )
        for r in conn.execute(f"SELECT {EDGE_COLUMNS} FROM edges"):
            graph.add_edge(r[0], r[1], kind=EdgeKind(r[2]), confidence=r[3], attrs=r[4])
    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "peak_rss": _peak_rss_bytes(),
    }


def _stage_summaries(db: Path) -> dict[str, Any]:
    """Profile the whole-design summaries (clock/CDC + UVM) standalone, on top of
    a loaded graph — the transient RSS above the resident graph is the cost."""
    graph, _files, _meta = SqliteStore(db).load()
    rss_after_load = _statm_rss_bytes()
    with _RSSSampler() as sampler:
        started = time.perf_counter()
        build_summaries(graph)
        summaries_s = time.perf_counter() - started
    return {
        "node_count": graph.number_of_nodes(),
        "edge_count": graph.number_of_edges(),
        "summaries_s": summaries_s,
        "rss_after_load": rss_after_load,
        "peak_rss": _peak_rss_bytes(),
        "sampled_peak_rss": sampler.peak,
        "cpu_s": _cpu_seconds(),
    }


_CHILD_STAGES: dict[str, Callable[[Path], dict[str, Any]]] = {
    "build": lambda p: _stage_build(p),  # p = root
    "load_timing": _stage_load_timing,
    "load_full": _stage_load_full,
    "load_fetch": _stage_load_fetch,
    "load_rawattrs": _stage_load_rawattrs,
    "load_trace": _stage_load_trace,
    "summaries": _stage_summaries,
}


# --------------------------------------------------------------------------- #
# Parent-process orchestration
# --------------------------------------------------------------------------- #
def _run_child(stage: str, target: Path) -> dict[str, Any]:
    """Run one stage in a fresh process; parse its single result line."""
    proc = subprocess.run(
        [sys.executable, __file__, "--_child-stage", stage, "--_child-target", str(target)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"child stage {stage!r} failed:\n{proc.stderr}")
    for line in proc.stdout.splitlines():
        if line.startswith(_RESULT_MARKER):
            return json.loads(line[len(_RESULT_MARKER) :])
    raise RuntimeError(f"child stage {stage!r} produced no result:\n{proc.stdout}\n{proc.stderr}")


def _run_child_repeated(stage: str, target: Path, repeat: int) -> dict[str, Any]:
    """Repeat a stage: median of every *_s timing, max of every *rss."""
    runs = [_run_child(stage, target) for _ in range(repeat)]
    return _aggregate(runs)


def _aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in runs[0]:
        vals = [r[key] for r in runs]
        if isinstance(vals[0], dict):
            out[key] = _aggregate(vals)
        elif isinstance(vals[0], (int, float)):
            if "rss" in key:
                out[key] = max(vals)
            elif key.endswith("_s"):
                out[key] = statistics.median(vals)
                out.setdefault("_spread", {})[key] = (min(vals), max(vals))
            else:
                out[key] = vals[0]  # counts: stable across runs
        else:
            out[key] = vals[0]
    return out


def _profile_point(root: Path, dense: bool, files: int, repeat: int) -> dict[str, Any]:
    """Generate one corpus and profile every stage against it."""
    (generate_dense if dense else generate)(root, files)
    db = default_db_path(root)
    return {
        "files": files,
        "dense": dense,
        "build": _run_child("build", root),  # build also writes the DB; run once
        "load_full": _run_child_repeated("load_full", db, repeat),
        "load_timing": _run_child_repeated("load_timing", db, repeat),
        "load_fetch": _run_child("load_fetch", db),
        "load_rawattrs": _run_child("load_rawattrs", db),
        "load_trace": _run_child("load_trace", db),
        "summaries": _run_child_repeated("summaries", db, repeat),
    }


def _print_point(p: dict[str, Any]) -> None:
    b, ld, sm = p["build"], p["load_full"], p["summaries"]
    nodes, edges = b["node_count"], b["edge_count"]
    mib = 1024 * 1024
    print(
        f"\n=== {p['files']} files{' [dense]' if p['dense'] else ''}: "
        f"{nodes} nodes / {edges} edges, DB {b['db_bytes'] / mib:.1f} MiB ==="
    )
    print(
        f"  build:     {b['build_s']:.2f}s wall / {b['cpu_s']:.2f}s cpu  "
        f"(discover {b['discover_s']:.2f} | parse {b['parse_s']:.2f} | "
        f"link {b['link_s']:.2f} | persist {b['persist_s']:.2f})  "
        f"peak {b['peak_rss'] / mib:.0f} MiB"
    )

    t = p["load_timing"]["timings"]
    io_pct = 100 * t["io_s"] / t["load_s"] if t["load_s"] else 0
    json_pct = 100 * t["json_loads_s"] / t["load_s"] if t["load_s"] else 0
    print(
        f"  load():    {ld['wall_s']:.2f}s wall / {ld['cpu_s']:.2f}s cpu  "
        f"peak {ld['peak_rss'] / mib:.0f} MiB (sampled {ld['sampled_peak_rss'] / mib:.0f})"
    )
    # SQLite-I/O-vs-graph-CPU split (from the timing-only stage) + residual.
    residual = abs((t["io_s"] + t["graph_cpu_s"]) - t["load_s"])
    print(
        f"             split: fetch {io_pct:.0f}% | graph-CPU {100 - io_pct:.0f}% "
        f"(json {json_pct:.0f}%)  [timed {t['load_s']:.2f}s, residual {residual * 1000:.1f} ms]"
    )

    # 3-way memory decomposition (streaming variants A/B/C).
    fetch_rss = p["load_fetch"]["peak_rss"]
    raw_rss = p["load_rawattrs"]["peak_rss"]
    full_rss = ld["peak_rss"]
    print(
        f"  mem split: fetch {fetch_rss / mib:.0f} | +graph {(raw_rss - fetch_rss) / mib:.0f} | "
        f"+json-attrs {(full_rss - raw_rss) / mib:.0f} MiB  "
        f"(bytes/node {full_rss / nodes:.0f}, bytes/edge {full_rss / edges:.0f})"
    )
    tm = p["load_trace"]["tracemalloc"]
    print(
        f"             tracemalloc: json {tm['json_attrs'] / mib:.0f} | "
        f"nx {tm['networkx'] / mib:.0f} | peak {tm['traced_peak'] / mib:.0f} MiB"
    )
    print(
        f"  summaries: {sm['summaries_s']:.2f}s  "
        f"transient +{(sm['sampled_peak_rss'] - sm['rss_after_load']) / mib:.0f} MiB over graph"
    )


def _report_scaling(points: list[dict[str, Any]]) -> None:
    """Fit bytes/node and load-time/node, extrapolate to the 10/100 GB regime."""
    if len(points) < 2:
        print("\n(need >=2 sweep points to fit a scaling curve)")
        return
    nodes = [float(p["build"]["node_count"]) for p in points]
    load_rss = [float(p["load_full"]["peak_rss"]) for p in points]
    load_s = [float(p["load_full"]["wall_s"]) for p in points]
    db_per_node = statistics.fmean(
        p["build"]["db_bytes"] / p["build"]["node_count"] for p in points
    )

    rss_slope, rss_int, rss_r2 = _lstsq(nodes, load_rss)
    s_slope, s_int, s_r2 = _lstsq(nodes, load_s)
    gib = 1024**3
    print("\n=== scaling fit (load peak RSS & time vs node count) ===")
    print(f"  RSS  = {rss_slope:.0f} B/node * N + {rss_int / 1e6:.0f} MB   (R²={rss_r2:.4f})")
    print(f"  time = {s_slope * 1e6:.2f} us/node * N + {s_int:.2f} s        (R²={s_r2:.4f})")
    print(f"  DB-on-disk ~ {db_per_node:.0f} bytes/node")

    print("\n=== extrapolation to the 'does not load' regime ===")
    for db_gb in (10, 100):
        n = db_gb * gib / db_per_node
        proj_rss = (rss_slope * n + rss_int) / gib
        proj_s = s_slope * n + s_int
        print(
            f"  {db_gb:>3} GB DB  -> ~{n / 1e6:.1f}M nodes, "
            f"load RSS ~{proj_rss:.1f} GB, load ~{proj_s:.0f}s"
        )
    for ceiling in (16, 32, 64):
        if rss_slope > 0:
            n = (ceiling * gib - rss_int) / rss_slope
            print(
                f"  RSS hits {ceiling} GB at ~{n / 1e6:.1f}M nodes "
                f"(~{n * db_per_node / gib:.0f} GB DB on disk)"
            )
    print(
        "\n  NOTE: 100 GB is ~1 order of magnitude beyond the largest measured "
        "point — treat as directional. Decide M12 on the conservative (dense) curve."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=None, help="single-point profile")
    parser.add_argument(
        "--files-sweep",
        type=str,
        default=None,
        help="comma-separated file counts, e.g. 2000,10000,50000",
    )
    parser.add_argument("--dense", action="store_true", help="resolution-heavy corpus")
    parser.add_argument("--repeat", type=int, default=2, help="repeats for timing-sensitive stages")
    parser.add_argument("--keep", type=Path, default=None, help="keep corpora under this dir")
    parser.add_argument("--json-out", type=Path, default=None, help="dump raw results as JSON")
    # Hidden: re-entrant child mode (one stage in an isolated process).
    parser.add_argument("--_child-stage", dest="child_stage", default=None, help=argparse.SUPPRESS)
    parser.add_argument(
        "--_child-target", dest="child_target", default=None, help=argparse.SUPPRESS
    )
    args = parser.parse_args()

    if args.child_stage:
        result = _CHILD_STAGES[args.child_stage](Path(args.child_target))
        print(_RESULT_MARKER + json.dumps(result))
        return 0

    if args.files_sweep:
        counts = [int(c) for c in args.files_sweep.split(",")]
    elif args.files:
        counts = [args.files]
    else:
        counts = [2000]

    points: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-prof-") as tmp:
        base = args.keep if args.keep is not None else Path(tmp)
        for files in counts:
            root = base / f"corpus_{files}{'_dense' if args.dense else ''}"
            point = _profile_point(root, args.dense, files, args.repeat)
            _print_point(point)
            points.append(point)

    _report_scaling(points)
    if args.json_out:
        args.json_out.write_text(json.dumps(points, indent=2))
        print(f"\nraw results -> {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
