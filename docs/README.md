# Documentation

Start with the [README](../README.md) for install and a first build.

## Usage — driving the tool

| | |
|---|---|
| [usage/build-inputs.md](usage/build-inputs.md) | `.f` filelists, defines, include dirs, VHDL libraries, `hdl-kgraph.toml`, per-file diagnostics |
| [usage/incremental.md](usage/incremental.md) | `update`, `watch`, `detect-changes`, `impact` — what a one-file edit costs |
| [usage/merge-design.md](usage/merge-design.md) | `merge`: assembling per-block databases into one SoC graph, and using them as a cache |
| [usage/analyses.md](usage/analyses.md) | clock domains, reset trees, CDC suspects, lint, metrics, visualization — **and what each will not tell you** |
| [usage/mcp.md](usage/mcp.md) | the MCP server and the same tools as plain JSON commands |
| [usage/review.md](usage/review.md) | the content-free review digest, safe to export from an air-gapped tree |

## Scale — how it behaves on large designs

| | |
|---|---|
| [scale/scalability.md](scale/scalability.md) | the out-of-core architecture: bounded reads, precomputed summaries, dirty-closure writes |
| [scale/benchmarks.md](scale/benchmarks.md) | measured numbers, including the tasks where **plain grep wins** |
| [scale/viz-scalability.md](scale/viz-scalability.md) | rendering strategy for graphs too large to plot directly |

## Internals — how it works

| | |
|---|---|
| [internals/extraction.md](internals/extraction.md) | what is extracted, the node/edge schema, and the confidence convention |
| [internals/enrichment.md](internals/enrichment.md) | `build --enrich`: elaboration overlays via pyslang/GHDL, and the discrepancy report |
| [internals/schema-migrations.md](internals/schema-migrations.md) | the database as a derived cache; when a schema bump migrates in place vs forces a rebuild |
| [internals/grammar-bakeoff.md](internals/grammar-bakeoff.md) | why these tree-sitter grammars, with the caveats |

## Project

| | |
|---|---|
| [project/releasing.md](project/releasing.md) | the release procedure |
| [project/v2/](project/v2/) | v2 design notes and profiling spikes (historical record, not current docs) |
| [../ROADMAP.md](../ROADMAP.md) | milestones and acceptance criteria |
| [../CHANGELOG.md](../CHANGELOG.md) | what changed, and which changes need a rebuild |
