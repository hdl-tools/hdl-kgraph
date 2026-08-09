# hdl-kgraph

[![CI](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml)

**A knowledge graph for your HDL design.** hdl-kgraph parses SystemVerilog,
Verilog, and VHDL into a local SQLite graph of modules, instances, ports,
signals, classes, and packages — plus the relationships between them: design
hierarchy, connectivity, clock domains, and more. Query it from the CLI, from
scripts, or from an AI assistant over MCP.

It also links SV `DPI-C` imports/exports to their C/C++ definitions, and cocotb
testbenches to the DUTs they drive.

> **v2.4.0 — stable.** M1–M8 shipped. v2.0 made it scale to 10–100+ GB designs:
> no `query` command loads the whole graph, whole-design reports are precomputed
> at build with an out-of-core SQL fallback, and `update` re-links and rewrites
> only the rows a change touched.
> [Roadmap](#roadmap) · [Docs](docs/README.md)

## Why

HDL codebases *are* graphs, but the tools that understand them are locked inside
simulators and synthesis flows. hdl-kgraph extracts that structure into a
local-first database you can actually query. The architecture follows
[code-review-graph](https://github.com/tirth8205/code-review-graph), adapted for
hardware.

## Quickstart

```bash
pip install hdl-kgraph

hdl-kgraph build ./rtl            # parse sources -> ./rtl/.hdl-kgraph/graph.db
hdl-kgraph build -f sim/tb.f      # or drive the build from a vendor filelist
hdl-kgraph status                 # files, parse errors, node/edge counts
hdl-kgraph tree soc_top           # design hierarchy from a top module
hdl-kgraph query instances-of fifo
hdl-kgraph query unresolved       # what couldn't be resolved (vendor IP, macros)
```

Most commands take `--json`. Unresolved targets render as `[?]` and ambiguous
matches as `[~0.6]`: every cross-file edge carries a confidence score — the
graph's honest contract about what was *proven* syntactically versus *inferred*
by name ([extraction](docs/internals/extraction.md)).

## Benchmark

A 69-file RV32I SoC (~25k lines of RTL), single laptop core:

| | |
|---|---|
| Full build | **3.1 s** → 5,013 nodes, 13,403 edges, 11 MB database |
| Incremental `update` after a one-file edit | **0.9 s** (68 files re-linked, not re-parsed) |
| `find_module` / `who_instantiates` | **1–2 ms** |
| `clock_domains`, whole-design | **0.6 ms** (precomputed at build) |

Reproduce with `hdl-kgraph build <rtl>` and `hdl-kgraph status`. Localized
queries track the size of the *answer*, not of the design — which is what makes
the tool usable well beyond this size.

**Is querying the graph actually cheaper than grepping it?** `hdl-kgraph bench`
measures that on *your* design rather than asking you to take it on faith. Run
live through Claude Code over 32 runs, the graph was **51.6% faster** with 46.8%
fewer tokens at the median — but **two of the four tasks lost**, and the docs say
which and why. → [benchmarks](docs/scale/benchmarks.md)

## How this differs

The closest free alternatives are *full elaborators*: **Verible**,
**Surelog/UHDM**, **Verilator `--xml-only`**. They are excellent, and they need
a complete, compilable design. hdl-kgraph's default tier is *syntactic*
(tree-sitter), so it builds a graph from incomplete or in-progress RTL that an
elaborator would reject — then `--enrich` overlays a real frontend
(pyslang/GHDL) where one is available.

| | Verible | Surelog/UHDM | Verilator | hdl-kgraph |
|---|---|---|---|---|
| Parses incomplete / broken sources | partial | no | no | **yes** |
| Dependencies | C++ build | C++ build | C++ build | **pip, pure-Python** |
| Output store | Kythe graph | UHDM model | XML AST | **local SQLite** |
| AI / MCP query surface | — | — | — | **yes** |
| Full elaboration | — | yes | yes | opt-in (`--enrich`) |

So it sits *alongside* these rather than against them, and a native elaborator
can plug in as an [enrichment backend](docs/internals/enrichment.md).

## Features

- **Real-world build inputs** — `.f` filelists, defines, include dirs, VHDL
  library mapping, `hdl-kgraph.toml`, per-file parse diagnostics.
  → [build-inputs](docs/usage/build-inputs.md)
- **Incremental updates** — `update` re-parses only changed files and their
  include/macro dependents; `watch` does it on every save burst.
  `detect-changes` and `impact` answer "what changed, and what does it affect?"
  in CI. → [incremental](docs/usage/incremental.md)
- **Database merge & subtree caching** — assemble per-block databases into one
  SoC graph, byte-identical to a monolithic build, and rebuild only the block
  that changed. → [merge](docs/usage/merge-design.md)
- **Mixed Verilog/VHDL** links into one hierarchy in both directions;
  cross-language matches score ≤0.8, never 1.0.
  → [extraction](docs/internals/extraction.md)
- **Analyses** — clock domains, reset trees, CDC suspects, signal
  drivers/readers, UVM topology, lint checks, metrics, and a self-contained
  interactive HTML visualization. → [analyses](docs/usage/analyses.md)
- **AI assistants over MCP** — `hdl-kgraph setup` configures installed
  assistants and seeds their instruction files; `serve` exposes nine read-only,
  paginated tools. No MCP? The same tools are JSON-printing commands under
  `hdl-kgraph tools …`. → [mcp](docs/usage/mcp.md)
- **Review digest** — `review` emits counts only, never identifiers, so it is
  safe to export from an air-gapped tree and diffs cleanly across builds.
  → [review](docs/usage/review.md)

## What gets extracted

- **Design units** — modules, interfaces, packages, programs; VHDL entities,
  architectures, packages, configurations
- **Structure** — instances with port connections and parameter overrides,
  `include`/`define` relationships, filelists
- **Verification** — SV classes (UVM hierarchies via inheritance), constraints,
  covergroups, assertions/properties/sequences, clocking blocks
- **Dataflow** — signal drivers/readers (process-, assign-, and instance-level),
  clock and reset trees, CDC-suspect crossings

Modports, checkers, UDPs, and generate blocks are *not* extracted yet.

Where an analysis is blind, it says so rather than staying silent: a design that
instantiates a module on more than one clock is reported `degraded`, not as
having no clock crossings
([#176](https://github.com/hdl-tools/hdl-kgraph/issues/176)). Full list, schema,
and the confidence convention: [extraction](docs/internals/extraction.md).

## Roadmap

| Milestone | Theme |
|---|---|
| M1–M3 (v0.1–0.3) | SV/Verilog graph + CLI; preprocessor and `.f` filelists; VHDL + mixed-language linking |
| M4–M6 (v0.4–0.6) | Incremental updates and watch mode; clock/reset/CDC analyses, lint, visualization; MCP server |
| M7–M8 (v0.7–v1.x) | Semantic enrichment (pyslang + GHDL); C/C++/Python boundary (DPI-C, cocotb) |
| M9–M10 (v1.x) | Chisel/FIRRTL, Amaranth, SpinalHDL; Tcl/SDC/UPF constraints, Perl, SLN |
| v2.0–v2.2 | Out-of-core reads, precomputed summaries, dirty-closure incremental link — delivered |

Details and acceptance criteria: [ROADMAP.md](ROADMAP.md).

## Development

```bash
git clone https://github.com/chuanseng-ng/hdl-kgraph
cd hdl-kgraph
pip install -e .[dev]
ruff check . && ruff format --check . && mypy && pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md). The single most useful contribution
right now: the smallest HDL file that breaks extraction.

## License

[MIT](LICENSE)
