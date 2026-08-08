#!/usr/bin/env python3
"""Subtree-caching benchmark: re-parse only the changed block, link once.

Subtree caching keeps each block's database as a cached artifact. When one
block changes you rebuild *only* that block and re-merge; the unchanged blocks'
cached per-file IRs are reused instead of being re-parsed. This benchmark makes
that payoff measurable: it splits a synthetic corpus into N blocks, edits one,
rebuilds only that block, re-merges, and shows the parse cost scales with the
*changed block* while the pass-2 link is paid once.

Generates a synthetic corpus (scripts/gen_corpus.py). See docs/benchmarks.md
for the procedure and recorded results.

Usage::

    python scripts/bench_merge.py [--files 2000] [--blocks 4] [--target-ratio 0.5]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gen_corpus import generate  # noqa: E402

from hdl_kgraph.config import BuildOptions  # noqa: E402
from hdl_kgraph.merge import run_merge  # noqa: E402
from hdl_kgraph.pipeline import run_build  # noqa: E402
from hdl_kgraph.storage.sqlite_store import SqliteStore  # noqa: E402


def _signature(graph: object) -> tuple[list, list]:
    """Ordered (nodes, edges) signature for byte-identity comparison."""
    nodes = sorted(
        (
            node_id,
            data["kind"].value,
            data["name"],
            data.get("qualified_name", ""),
            data.get("file", ""),
            tuple(data.get("line_span", (0, 0))),
            data["language"].value,
            json.dumps(data["attrs"], sort_keys=True),
        )
        for node_id, data in graph.nodes(data=True)  # type: ignore[attr-defined]
    )
    edges = sorted(
        (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
        for u, v, d in graph.edges(data=True)  # type: ignore[attr-defined]
    )
    return nodes, edges


def _graph(db: Path) -> object:
    return SqliteStore(db).load()[0]


def _partition(root: Path, blocks: int) -> list[list[str]]:
    """Split the design into *blocks* groups of disjoint modules.

    The shared header/package (``defs.svh``, ``bench_pkg.sv``) are added to
    *every* block, sorted first so each block parses them standalone exactly as
    a monolithic build does — partitions must be preprocessing-self-contained
    (the same caveat the merge command documents). Their identical content
    dedups across blocks at merge time.
    """
    shared = [p.name for p in sorted(root.glob("*.svh"))]
    if (root / "bench_pkg.sv").exists():
        shared.append("bench_pkg.sv")
    main = sorted(p.name for p in root.glob("*.sv") if p.name not in shared)
    groups: list[list[str]] = [[] for _ in range(blocks)]
    for i, name in enumerate(main):
        groups[i % blocks].append(name)
    return [sorted(g + shared) for g in groups if g]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=2000)
    parser.add_argument("--blocks", type=int, default=4)
    # With N balanced blocks the changed block is ~1/N of the design, so its
    # parse should be ~1/N of the full parse. Half is a comfortable, noise-
    # tolerant ceiling for the headline "parse scales with the change" claim.
    parser.add_argument("--target-ratio", type=float, default=0.5)
    parser.add_argument(
        "--keep", type=Path, default=None, help="generate into this directory and keep it"
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-bench-merge-") as tmp:
        root = args.keep if args.keep is not None else Path(tmp)
        count = generate(root, args.files)
        groups = _partition(root, args.blocks)
        print(f"corpus:         {count} files under {root}, {len(groups)} blocks")

        # Baseline: a full monolithic build.
        started = time.perf_counter()
        mono = run_build(root, db_path=root / "mono.db")
        full_build_s = time.perf_counter() - started
        print(
            f"full build:     {full_build_s:.2f}s "
            f"({mono.parsed_files} files, {mono.node_count} nodes, {mono.edge_count} edges)"
        )

        # Build each block into its own cached database.
        block_dbs: list[Path] = []
        for i, sources in enumerate(groups):
            db = root / f"block_{i}.db"
            run_build(root, db_path=db, options=BuildOptions(sources=sources))
            block_dbs.append(db)
        merged = run_merge(block_dbs, root / "soc.db")
        print(
            f"initial merge:  linked {merged.units_merged} units in {merged.link_s:.2f}s "
            f"({merged.elapsed_s:.2f}s total)"
        )
        assert _signature(_graph(root / "soc.db")) == _signature(_graph(root / "mono.db")), (
            "merge of all blocks != monolithic build"
        )

        # Cached rebuild: edit one block-private file in block 0, rebuild ONLY
        # that block, then re-merge. The other blocks' DBs are reused untouched.
        # (Editing a *shared* file would force rebuilding every block — not the
        # caching scenario — so pick a module unique to this block.)
        shared = {p.name for p in root.glob("*.svh")} | {"bench_pkg.sv"}
        private = [f for f in groups[0] if f not in shared]
        target = root / private[0]
        target.write_text(target.read_text() + "\n// cache-bench touch\n")
        rebuilt = run_build(root, db_path=block_dbs[0], options=BuildOptions(sources=groups[0]))
        remerged = run_merge(block_dbs, root / "soc2.db")
        block_frac = len(groups[0]) / mono.parsed_files
        print(
            f"changed block:  re-parsed {rebuilt.parsed_files} file(s) in {rebuilt.parse_s:.2f}s "
            f"(block is {block_frac:.0%} of {mono.parsed_files} files; "
            f"full parse was {mono.parse_s:.2f}s)"
        )
        print(f"re-merge:       linked in {remerged.link_s:.2f}s ({remerged.elapsed_s:.2f}s total)")

        # Equivalence after the cached rebuild: a fresh monolithic build of the
        # edited tree must match the cached-then-remerged graph.
        run_build(root, db_path=root / "mono2.db")
        equivalent = _signature(_graph(root / "soc2.db")) == _signature(_graph(root / "mono2.db"))

        # The headline claim: PARSE cost scales with the changed block, not the
        # whole design (the link is paid once, reported above). End-to-end the
        # cached rebuild also wins once parse dominates — true on large designs;
        # on a tiny corpus the O(design) IR re-load can swamp the parse saving.
        parse_ratio = rebuilt.parse_s / mono.parse_s if mono.parse_s else 0.0
        reparse_ok = rebuilt.parsed_files == len(groups[0])
        parse_ok = parse_ratio < args.target_ratio
        verdict = "PASS" if equivalent and reparse_ok and parse_ok else "FAIL"
        print(
            f"target:         equivalent + re-parse only changed block + "
            f"block parse < {args.target_ratio:.0%} of full parse ({parse_ratio:.0%}) -> {verdict}"
        )
        return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
