# `hdl-kgraph review` — content-free review digest

`hdl-kgraph review` emits a single JSON digest of a built graph that is **safe to copy out
of an isolated / air-gapped environment**. It contains **only counts, ratios, distributions,
and build timings — never identifiers** (no module/clock/signal names, file paths, or
expression text). The goal: when the source and `graph.db` cannot leave the env, a person
inside can run one command, and the *output snapshot* carries enough signal to review the
build — and to **diff across builds** for regressions — without disclosing the design.

```bash
hdl-kgraph review --json > review.json        # the snapshot to carry out / track
hdl-kgraph review                             # short human summary
hdl-kgraph review --json --metrics            # + fan-in/hub/community metrics (loads the graph)
```

## What's in it (and how to read it)

| Section | Fields | Use |
|---|---|---|
| `meta` | `tool_version`, `schema_version`, `built_at`, `options_hash`, `enriched` | provenance (`root` path is deliberately omitted) |
| `corpus` | file/lang counts, `db_bytes` | size / scope |
| `corpus` (parse health) | `parse_error_count`, `files_with_errors`, `preprocessor_warnings` | **is the parse sound?** |
| `graph` | `node_count`, `edge_count`, `node_kinds`, `edge_kinds` | design shape; dataflow present (non-zero `clocked_by`/`drives`/`reads`) |
| `link_quality` | `unresolved_stub_count` + ratio, `edge_confidence_distribution` | **how much resolved / how confident** |
| `analyses` | `clock_domains` (per-domain counts), `cdc.suspect_count`, `uvm` counts, optional `metrics` | analysis results as numbers |
| `timings_s` | per-phase build wall-clock (`discover/parse/link/enrich/persist`) | performance; `null` on pre-1.8 DBs |

Because the schema is stable (`"schema": "hdl-kgraph.review/1"`), two snapshots **diff**
cleanly: a jump in `link_s`, a rise in `unresolved_stub_ratio`, or a change in
`cdc.suspect_count` is visible without ever seeing the RTL.

## Disclosure note

Everything emitted is an aggregate (counts/ratios/distributions/timings). Identifiers are
stripped: clock-domain summaries become per-domain *counts*, `metrics` reports *values* not
module names, and the build `root` path is omitted. The test suite enforces this — a
content-free assertion checks that no design identifier appears anywhere in the digest.

`timings_s` comes from a `build_stats` row that `build`/`update` persist into the `meta`
table (per-phase timings + the `enriched` flag); it's the same data as `build --timings`,
made available from a static database so `review` needs no rebuild.
