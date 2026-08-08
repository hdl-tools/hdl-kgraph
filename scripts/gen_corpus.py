#!/usr/bin/env python3
"""Generate a synthetic SystemVerilog design for benchmarking (M4).

Layout (default 2000 files): a module tree — ``top`` instantiates mid
modules, each mid instantiates a slice of leaves — with one shared header
included by ~10% of leaves and one package imported by ~10% of mids.
Realistic enough to exercise the include/macro dirty closure without
being a real design.

Usage::

    python scripts/gen_corpus.py /tmp/corpus --files 2000
"""

from __future__ import annotations

import argparse
from pathlib import Path

HEADER_EVERY = 10  # every Nth leaf includes the shared header
IMPORT_EVERY = 10  # every Nth mid imports the shared package
LEAVES_PER_MID = 20


def generate(root: Path, files: int) -> int:
    """Write a *files*-file design under *root*; returns the file count."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "defs.svh").write_text("`define BENCH_WIDTH 8\n`define BENCH_RESET 1'b0\n")
    (root / "bench_pkg.sv").write_text(
        "package bench_pkg;\n  localparam int LANES = 4;\n  typedef logic [7:0] byte_t;\n"
        "endpackage\n"
    )
    budget = files - 3  # defs.svh + bench_pkg.sv + top.sv
    mids = max(1, budget // (LEAVES_PER_MID + 1))
    leaves = budget - mids

    for i in range(leaves):
        include = '`include "defs.svh"\n' if i % HEADER_EVERY == 0 else ""
        width = "`BENCH_WIDTH" if i % HEADER_EVERY == 0 else "8"
        (root / f"leaf_{i:05d}.sv").write_text(
            f"{include}module leaf_{i:05d}(\n"
            f"    input  logic [{width}-1:0] a,\n"
            f"    input  logic [{width}-1:0] b,\n"
            f"    output logic [{width}-1:0] y\n"
            ");\n"
            f"  assign y = a + b + {i % 7};\nendmodule\n"
        )

    for m in range(mids):
        imported = "  import bench_pkg::*;\n" if m % IMPORT_EVERY == 0 else ""
        start = m * LEAVES_PER_MID
        body = "".join(
            f"  leaf_{i:05d} u_leaf_{i:05d}(.a(a), .b(b), .y(taps[{i - start}]));\n"
            for i in range(start, min(start + LEAVES_PER_MID, leaves))
        )
        count = max(1, min(start + LEAVES_PER_MID, leaves) - start)
        (root / f"mid_{m:04d}.sv").write_text(
            f"module mid_{m:04d}(\n"
            "    input  logic [7:0] a,\n"
            "    input  logic [7:0] b,\n"
            "    output logic [7:0] y\n"
            ");\n"
            f"{imported}"
            f"  logic [7:0] taps [{count}];\n"
            f"{body}"
            "  assign y = taps[0];\nendmodule\n"
        )

    top_body = "".join(
        f"  mid_{m:04d} u_mid_{m:04d}(.a(a), .b(b), .y(ys[{m}]));\n" for m in range(mids)
    )
    (root / "top.sv").write_text(
        "module top(\n    input  logic [7:0] a,\n    input  logic [7:0] b,\n"
        "    output logic [7:0] y\n);\n"
        f"  logic [7:0] ys [{mids}];\n{top_body}  assign y = ys[0];\nendmodule\n"
    )
    return leaves + mids + 3


def generate_dense(root: Path, files: int, ports: int = 8) -> int:
    """Write a *files*-file **resolution-heavy** design under *root*.

    Unlike :func:`generate` (a resolution-light instantiation tree), this
    variant maximises pass-2 resolution work and attribute payload so the
    graph's edge:node ratio and per-edge ``attrs`` size approach a real design:
    every leaf imports the shared package (cross-unit refs), carries *ports*
    wide ports (denser CONNECTS/PARAMETERIZES), and chains its output into the
    next leaf's input (cross-leaf DRIVES/READS that must be resolved). Used by
    ``scripts/profile_v2.py`` to bound the corpus-sensitivity of the M11
    scaling curve — see ``docs/v2/m11_profiling.md``.
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "defs.svh").write_text("`define BENCH_WIDTH 8\n`define BENCH_RESET 1'b0\n")
    (root / "bench_pkg.sv").write_text(
        "package bench_pkg;\n  localparam int LANES = 4;\n  typedef logic [7:0] byte_t;\n"
        "endpackage\n"
    )
    budget = files - 3  # defs.svh + bench_pkg.sv + top.sv
    mids = max(1, budget // (LEAVES_PER_MID + 1))
    leaves = budget - mids

    port_decls = "".join(
        f"    input  bench_pkg::byte_t in_{p},\n    output bench_pkg::byte_t out_{p},\n"
        for p in range(ports)
    )
    for i in range(leaves):
        # Each leaf's outputs are a function of all its inputs -> a dense web of
        # intra-unit DRIVES/READS, plus a package import that must resolve.
        body = "".join(
            f"  assign out_{p} = in_{p} + in_{(p + 1) % ports} + {i % 7};\n" for p in range(ports)
        )
        (root / f"leaf_{i:05d}.sv").write_text(
            '`include "defs.svh"\n'
            f"module leaf_{i:05d}(\n{port_decls}    output logic [`BENCH_WIDTH-1:0] y\n);\n"
            "  import bench_pkg::*;\n"
            f"{body}  assign y = out_0;\nendmodule\n"
        )

    for m in range(mids):
        start = m * LEAVES_PER_MID
        kids = range(start, min(start + LEAVES_PER_MID, leaves))
        # Chain leaf k's outputs into leaf k+1's inputs: cross-unit references
        # the linker must resolve, raising the resolved-edge count per node.
        decls = "".join(f"  bench_pkg::byte_t net_{k} [{ports}];\n" for k in kids)
        insts = ""
        prev: int | None = None
        for k in kids:
            conns = "".join(
                f".in_{p}(net_{prev}[{p}]), " if prev is not None else f".in_{p}(a), "
                for p in range(ports)
            ) + "".join(f".out_{p}(net_{k}[{p}]), " for p in range(ports))
            insts += f"  leaf_{k:05d} u_leaf_{k:05d}({conns}.y(taps[{k - start}]));\n"
            prev = k
        count = max(1, len(list(kids)))
        (root / f"mid_{m:04d}.sv").write_text(
            f"module mid_{m:04d}(\n    input  logic [7:0] a,\n    input  logic [7:0] b,\n"
            "    output logic [7:0] y\n);\n  import bench_pkg::*;\n"
            + f"  logic [7:0] taps [{count}];\n{decls}{insts}  assign y = taps[0];\nendmodule\n"
        )

    top_body = "".join(
        f"  mid_{m:04d} u_mid_{m:04d}(.a(a), .b(b), .y(ys[{m}]));\n" for m in range(mids)
    )
    (root / "top.sv").write_text(
        "module top(\n    input  logic [7:0] a,\n    input  logic [7:0] b,\n"
        "    output logic [7:0] y\n);\n"
        f"  logic [7:0] ys [{mids}];\n{top_body}  assign y = ys[0];\nendmodule\n"
    )
    return leaves + mids + 3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="output directory")
    parser.add_argument("--files", type=int, default=2000, help="total file count")
    parser.add_argument(
        "--dense", action="store_true", help="resolution-heavy variant (generate_dense)"
    )
    args = parser.parse_args()
    count = generate_dense(args.root, args.files) if args.dense else generate(args.root, args.files)
    print(f"wrote {count} files under {args.root}")


if __name__ == "__main__":
    main()
