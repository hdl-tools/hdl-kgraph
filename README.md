# hdl-kgraph

[![CI](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml/badge.svg)](https://github.com/chuanseng-ng/hdl-kgraph/actions/workflows/ci.yml)

**A knowledge graph for your HDL design.** hdl-kgraph parses SystemVerilog,
Verilog, and VHDL sources (and links SV `DPI-C` imports/exports to their C/C++
function definitions and cocotb Python testbenches to the DUTs they drive) and
builds a queryable graph of modules, entities, instances, ports, parameters,
signals, classes, packages, and the relationships between them — design
hierarchy, port connectivity, package imports, class inheritance, clock
domains, and more.

> **Status: v2.4.0 — stable.** Milestones M1–M7 are in: SystemVerilog/Verilog
> and VHDL extraction with mixed-language linking, the SV preprocessor and
> real-world build inputs (`.f` filelists, defines, `hdl-kgraph.toml`),
> incremental rebuilds and watch mode, the clock/reset/CDC/lint/metrics
> analyses with an interactive visualization, and an MCP server so AI
> assistants can query the design. M7 adds opt-in semantic enrichment
> (`build --enrich`, `pip install 'hdl-kgraph[enrich]'`): the pyslang frontend
> elaborates the design — unrolling parameterized generates so instance counts
> match reality — and records a
> [discrepancy report](docs/enrichment.md). `hdl-kgraph enriched` summarizes
> exactly what enrichment changed vs the default build. **v2.0 makes it scale to
> large (10–100+ GB) designs:** the out-of-core, bounded-RAM architecture is
> fully delivered — every MCP/CLI query answers from a bounded, index-backed
> subgraph instead of loading the whole graph (a localized query is
> sub-millisecond and tracks the *answer* size, not the design size, and **no
> `query` command full-loads the graph**), the whole-design clock/reset/CDC/UVM
> reports are precomputed at build with an SQL-native out-of-core fallback, and
> an incremental `update` re-links and re-writes only the changed rows through
> the memory-bounded linker (now the default). See
> [docs/scalability.md](docs/scalability.md) and the
> [roadmap](#roadmap-at-a-glance). M8's C/C++/Python boundary is in: SystemVerilog
> `DPI-C` imports/exports linked to their C/C++ function definitions
> (`FOREIGN_BINDS`, v2.3), and cocotb Python testbenches linked to the DUTs they
> drive (`TEST_COVERS` + `dut.<signal>` `READS`/`DRIVES`, v2.4).

## Why

HDL codebases are graphs — hierarchy, connectivity, clock domains — but the
tools that understand them are locked inside simulators and synthesis flows.
hdl-kgraph extracts that structure into a local-first SQLite database you can
query from the CLI, scripts, or AI assistants via MCP. The architecture
follows [code-review-graph](https://github.com/tirth8205/code-review-graph),
adapted for hardware.

## How this differs

The closest free, mature alternatives are *full elaborators* — **Verible** (SV
linter/indexer with a Kythe knowledge-graph export), **Surelog/UHDM** (IEEE-1800
elaboration to a queryable model), **Verilator** (`--xml-only` elaborated AST).
They're excellent, but they need a complete, compilable design. hdl-kgraph's
default tier is *syntactic* (tree-sitter): it parses files with `ERROR` nodes
and missing dependencies, so it builds a graph from incomplete or in-progress
RTL that an elaborator would reject — then `--enrich` overlays an actual
frontend (pyslang/GHDL) for elaborated precision where one is available.

| | Verible | Surelog/UHDM | Verilator `--xml-only` | hdl-kgraph |
|---|---|---|---|---|
| Parses incomplete / broken / missing-dep sources | partial (error-recovering) | no (elaborates) | no (elaborates) | **yes** (tree-sitter, ERROR-tolerant) |
| Dependency footprint | C++ build | C++ build | C++ build | **pip, pure-Python default** |
| Output store | Kythe graph | UHDM model | XML AST | **local SQLite, queryable** |
| AI / MCP query surface | — | — | — | **yes** |
| Full elaboration (params, generates) | — | yes | yes | opt-in overlay (`--enrich`) |

So hdl-kgraph sits *alongside* these tools rather than against them — the
syntactic tier is the always-works floor for partial designs, and a native
elaborator can plug in as an [enrichment backend](docs/enrichment.md) for the
precision overlay.

## Quickstart

```bash
pip install hdl-kgraph

hdl-kgraph build ./rtl            # parse sources -> ./rtl/.hdl-kgraph/graph.db
hdl-kgraph build -f sim/tb.f      # or drive the build from a vendor-style filelist
hdl-kgraph merge a.db b.db --db soc.db  # assemble per-block DBs into one SoC graph
hdl-kgraph status                 # files, parse errors, node/edge counts
hdl-kgraph tree soc_top           # print the design hierarchy from a top module
hdl-kgraph query instances-of fifo
hdl-kgraph query unresolved       # what couldn't be resolved (vendor IP, macros)
```

Most commands take `--json` for scripting. Unresolved targets render as
`[?]` and ambiguous matches as `[~0.6]`: every cross-file edge carries a
confidence score — the graph's honest contract about what was proven
syntactically vs inferred by name matching
([docs/extraction.md](docs/extraction.md)).

## Features

**Real-world build inputs.** `.f` filelists (`+incdir+`/`+define+`, nested
`-f`, `$VAR` expansion), preprocessor defines and include dirs, VHDL library
mapping, `--exclude`/`--max-file-size` for vendored IP, and an
`hdl-kgraph.toml` config. Per-file diagnostics (`build -v`,
`status --errors`) tell you which files have parse errors or preprocessor
warnings and why files were skipped.
→ [docs/build-inputs.md](docs/build-inputs.md)

**Incremental updates.** `update` re-parses only changed files plus their
include/macro dependents — one edit in a 2000-file design lands in about 1.5 s
(budget < 1.8 s) — and `watch` does it on every save burst. The pass-2 link is
incremental too, and as of v2.0 the **memory-bounded linker is the default**:
for SystemVerilog/Verilog it re-resolves only the references whose target
changed, reading just the dirty closure from SQLite (selective IR decode,
out-of-core `TEST_COVERS`) instead of loading the prior graph — byte-identical
to a full re-link; VHDL, binds, and `--enrich` fall back. The database write is
scoped to the dirty closure too, so a one-file edit reads and writes ~0.04 % of
the rows, not the whole graph. `detect-changes`
(exit codes: 0 clean, 1 dirty, 2 error; diffs against git, svn, or Perforce)
and `impact` answer "what changed, and what does it affect?" in CI.
→ [docs/incremental.md](docs/incremental.md)

**Database merge & subtree caching.** `merge` assembles independently-built
block databases into one SoC graph by unioning their per-file IRs and re-linking
once — byte-identical to a monolithic build of the same files. Keep each block's
DB as a cache: when one block changes, rebuild only it and re-merge, reusing the
unchanged blocks' cached IRs instead of re-parsing them (parse cost scales with
the change; the link is paid once).
→ [docs/merge-design.md](docs/merge-design.md)

**Mixed Verilog/VHDL designs link into one hierarchy.** `tree` and `query`
cross the language boundary in both directions; cross-language matches are
scored ≤0.8, never 1.0.
→ [docs/extraction.md](docs/extraction.md)

**Analyses.** Clock domains, reset trees, CDC suspects, signal
drivers/readers, UVM topology, graph-level lint checks, fan-in/out and
hub/bridge metrics, and a self-contained interactive HTML visualization
(`visualize`: hierarchy + force-directed views, community filters,
`--kinds`/`--exclude-kinds` to plot only the categories of interest,
`--collapse` for subsystem supernodes, `--layout` tiers for large designs).
→ [docs/analyses.md](docs/analyses.md)

**AI assistants over MCP.** `hdl-kgraph setup` detects installed assistants
(Claude Code/Desktop, Cursor, Codex, Windsurf, Gemini CLI, VS Code), writes
their MCP config, and seeds their instruction files (`CLAUDE.md`, `AGENTS.md`,
…) with notes on querying the graph; `hdl-kgraph serve` exposes nine read-only,
paginated tools (`pip install 'hdl-kgraph[mcp]'`). Each tool answers from the
bounded subgraph it needs (v0.9), so queries stay fast on very large designs.
Can't run MCP? The same nine tools are available as plain JSON-printing
commands under `hdl-kgraph tools …` (no `[mcp]` extra needed).
→ [docs/mcp.md](docs/mcp.md)

**Review digest & link metrics.** `review` emits a content-free digest — parse
health, graph shape, link quality, and analysis counts only, never identifiers
— so it's safe to export from an air-gapped environment and diffs cleanly across
builds. `bench-link` quantifies incremental-link locality (what fraction of
references a typical edit re-resolves), the metric behind the bounded linker.
→ [docs/review.md](docs/review.md)

**Check the claim yourself.** `hdl-kgraph bench` measures what that
"query the graph instead of grepping" advice is actually worth *on your
design*, rather than asking you to take it on faith. `bench context` prices
each question both ways — the graph's JSON envelope against the grep output
plus the files a no-graph agent would read — and `bench fidelity` compares
both against hand-written ground truth. The baseline searches only the files
the graph indexed, caps and flags its own reads, prints every command it ran,
and reports the questions where **grep wins** (`port-map` on hand-written RTL
does). On an RV32I SoC the offline saving is 97.9% over 84 RTL files, and on a
39 KB toy design it correctly reports that the graph is *not worth it*.

`bench agent` then checks that against reality by running the same tasks
through Claude Code twice, with and without the MCP server. Measured live over
32 runs: **median 51.6% faster wall-clock** (14.4 s vs 29.9 s) and 46.8% fewer
tokens — real, but half the offline figure, because a live agent greps far more
cleverly than any scripted baseline. **Two of the four tasks lose**, including
`port-map`, exactly as the offline run predicts. The offline number is an upper
bound, not a prediction, and the docs say so.
→ [docs/benchmarks.md](docs/benchmarks.md)

## What gets extracted

- **Design units:** modules, interfaces, packages, programs; VHDL entities,
  architectures, packages, configurations
- **Structure:** instances with port connections and parameter overrides,
  `include`/`define` relationships, filelists
- **Verification:** SV classes (UVM hierarchies via inheritance chains),
  constraints, covergroups, assertions/properties/sequences, clocking blocks
- **Dataflow:** signal drivers/readers (process-, assign-, and
  instance-level), clock and reset trees, CDC-suspect crossings

Modports, checkers, UDPs, and generate blocks are *not* extracted yet. The
full list, the confidence convention, and the schema pointers live in
[docs/extraction.md](docs/extraction.md).

## Roadmap at a glance

| Milestone | Theme |
|---|---|
| M1 (v0.1) | SystemVerilog/Verilog structural graph + CLI |
| M2 (v0.2) | Preprocessor, `.f` filelists, includes |
| M3 (v0.3) | VHDL + mixed-language linking |
| M4 (v0.4) | Incremental updates, watch mode, impact analysis (v0.8: incremental pass-2 link) |
| M5 (v0.5) | Clock/reset/CDC analyses, lint checks, visualization |
| M6 (v0.6) | MCP server for AI assistants |
| M7 (v0.7) | Semantic enrichment via native frontends (pyslang + GHDL elaboration) |
| M8 (v1.x) | C/C++/Python boundary (DPI-C, cocotb) |
| M9 (v1.x) | Chisel/FIRRTL, Amaranth, SpinalHDL |
| M10 (v1.x) | Tcl/SDC/UPF constraints, Perl scripting, SLN portable stimulus |

*Cross-cutting (v0.9–v2.2, the v2.0 epic): bounded index-backed reads,
precomputed whole-design summaries with an SQL-native out-of-core fallback, and
a dirty-closure-scoped incremental link and write — delivered, so no query
full-loads the graph and the tool scales to 10–100+ GB designs —
[docs/scalability.md](docs/scalability.md).*

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
