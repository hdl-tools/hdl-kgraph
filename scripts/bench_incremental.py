#!/usr/bin/env python3
"""Incremental-update benchmark: 1 file edited in a 2k-file design.

Target: < 1.8 s (M5's dataflow edges grew the graph ~76%, lifting the budget
from the original M4 < 1 s to < 1.5 s; precomputed whole-design summaries then
added a fixed per-update pass, lifting it to < 1.8 s — see docs/scale/benchmarks.md).

Generates a synthetic corpus (scripts/gen_corpus.py), times a full
``build``, touches one leaf module, then times the ``update``. See
docs/scale/benchmarks.md for the procedure and recorded results.

Usage::

    python scripts/bench_incremental.py [--files 2000] [--target-s 1.8]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_corpus import generate  # noqa: E402

from hdl_kgraph.pipeline import run_build, run_update  # noqa: E402
from hdl_kgraph.storage.sqlite_store import SqliteStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=2000)
    # M4 measured 0.85 s against a < 1 s target; M5's dataflow edges grew the
    # graph ~76% and the budget to < 1.5 s. Precomputed whole-design summaries
    # (clock domains / UVM, so those tools read O(1) at any scale) add a fixed
    # per-update cost, bumping the budget to < 1.8 s (see docs/scale/benchmarks.md).
    parser.add_argument("--target-s", type=float, default=1.8)
    parser.add_argument(
        "--keep", type=Path, default=None, help="generate into this directory and keep it"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-bench-") as tmp:
        root = args.keep if args.keep is not None else Path(tmp)
        count = generate(root, args.files)
        print(f"corpus:        {count} files under {root}")

        started = time.perf_counter()
        build = run_build(root)
        build_s = time.perf_counter() - started
        print(
            f"full build:    {build_s:.2f}s "
            f"({build.parsed_files} files, {build.node_count} nodes, {build.edge_count} edges)"
        )

        leaf = root / "leaf_00001.sv"
        leaf.write_text(leaf.read_text().replace("+ 1", "+ 2"))

        # Capture the incremental write volume (issue #63): the delta write
        # should touch far fewer rows than the full graph.
        captured: dict[str, int] = {}
        real_save_incremental = SqliteStore.save_incremental

        def spy(self: SqliteStore, *a: object, **k: object) -> None:
            real_save_incremental(self, *a, **k)
            if self.last_write_stats is not None:
                captured.update(self.last_write_stats)

        SqliteStore.save_incremental = spy  # type: ignore[method-assign]
        try:
            started = time.perf_counter()
            update = run_update(root)
            update_s = time.perf_counter() - started
        finally:
            SqliteStore.save_incremental = real_save_incremental  # type: ignore[method-assign]
        assert update.build is not None
        print(
            f"update:        {update_s:.2f}s "
            f"(re-parsed {len(update.reparsed)}, reused {update.build.reused_files})"
        )
        # Pass-2 scoping (#64): an incremental link must re-resolve only the
        # dirty closure, not every ref — a silent re-resolve-everything
        # regression shows up here even when wall-time is noisy.
        if update.build.incremental_link:
            total = update.build.refs_total
            rr = update.build.refs_reresolved
            rr_pct = 100.0 * rr / total if total else 0.0
            print(f"refs resolved: {rr} of {total} ({rr_pct:.2f}%) re-resolved (incremental link)")
        else:
            print(f"refs resolved: full re-link ({update.build.incremental_link_skipped})")

        # Fail closed: a missing stats dict means the incremental write path
        # never ran (e.g. it fell back to a full save()), which is exactly the
        # regression this benchmark guards — never let it count as zero writes.
        stats_present = {
            "nodes_upserted",
            "nodes_deleted",
            "edge_srcs_rewritten",
        } <= captured.keys()
        node_writes = captured.get("nodes_upserted", 0) + captured.get("nodes_deleted", 0)
        pct = 100.0 * node_writes / build.node_count if build.node_count else 0.0
        print(
            f"write volume:  {node_writes} node rows + "
            f"{captured.get('edge_srcs_rewritten', 0)} edge-src buckets "
            f"({pct:.2f}% of {build.node_count} nodes)"
            + ("" if stats_present else "  [!] no incremental write stats captured")
        )

        # Phase B: the scoped delta also *reads* only the dirty closure, so its
        # memory/IO scales with the change, not the design (the old path read
        # every stored node and edge-src to diff them).
        nodes_scanned = captured.get("nodes_scanned")
        read_pct = (
            100.0 * nodes_scanned / build.node_count
            if nodes_scanned is not None and build.node_count
            else 0.0
        )
        print(
            f"read volume:   {nodes_scanned} node rows scanned "
            f"({read_pct:.2f}% of {build.node_count} nodes)"
            if nodes_scanned is not None
            else "read volume:   (full-table diff — not scoped)"
        )

        time_ok = update_s < args.target_s
        # The real point of #63: writes scale with the change, not the design.
        write_ok = stats_present and node_writes < build.node_count * 0.05
        # Phase B: reads scale with the change too (scoped diff).
        read_ok = nodes_scanned is not None and nodes_scanned < build.node_count * 0.10
        verdict = "PASS" if time_ok and write_ok and read_ok else "FAIL"
        print(
            f"target:        update < {args.target_s:.1f}s, write < 5% and "
            f"read < 10% of graph -> {verdict}"
        )
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
