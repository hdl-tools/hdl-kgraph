# Changelog

All notable changes to **hdl-kgraph** are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
As of `1.0`, the public CLI and graph schema are stable: breaking changes bump
the major version, and schema changes ship with a migration.

## [Unreleased]

### Added

- `hdl-kgraph bench` command group: measures the savings claim `setup` seeds
  into every assistant's instruction file, against the user's own design.
  - `bench context` prices each design question twice — the graph's JSON
    envelope versus the grep output plus the files a no-graph agent would have
    to read — and reports median *and* aggregate savings, per-question rows,
    latency, and the read-everything token ceiling. Questions are derived from
    the graph deterministically, so it runs on any design and two runs give
    identical numbers. The baseline is deliberately constrained and
    self-documenting: it searches only the files the graph indexed, caps its
    reads (`--baseline-max-files`, flagged when the cap binds), prints every
    command it ran, reports the questions where grep is *cheaper*, and excludes
    any row whose graph answer was empty from the headline. `--fail-under`
    exits 1 on a missed target for CI gating; `--tokenizer tiktoken` (new
    optional `bench` extra) replaces the default `chars/4` estimator with exact
    counts.
  - `bench fidelity` compares graph and grep answers against hand-authored
    ground truth over a bundled suite covering a macro-hidden instantiation, a
    comment/string false positive, a VHDL case-insensitive name, and one case
    grep answers correctly. `--suite PATH` runs your own cases.
  - `bench agent` runs an opt-in live Claude Code A/B (one MCP server versus
    none) and reports the CLI's own token usage. Isolates both arms
    identically, asserts per run that they differed only in MCP, and documents
    that no seed or temperature control exists — the output is a spread, not a
    measurement.
  - See [docs/benchmarks.md](docs/benchmarks.md).
- `SqliteStore.load_file_metas()`: the stored per-file records without
  hydrating the graph, so a caller needing only the source inventory does not
  pay for a whole-graph `load()`.

## [Released - pypi]

## [1.1.0] - 2026-06-16

### Added

- `hdl-kgraph tools` command group: the nine MCP tools (`find-module`,
  `hierarchy`, `who-instantiates`, `port-map`, `impact`, `clock-domains`,
  `find-signal-drivers`, `uvm-topology`, `search-nodes`) as plain commands that
  print the same JSON envelope to stdout. For environments where MCP cannot be
  configured: an agent can shell out instead. Uses the bounded, index-backed
  reader (not a full-graph load), so it stays fast on large designs, and needs
  only the base install — no `[mcp]` extra. See [docs/mcp.md](docs/mcp.md).
- `hdl-kgraph setup` now also seeds each detected assistant's instruction file
  (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, a Cursor/Windsurf rule, or
  `.github/copilot-instructions.md`) with notes on querying the graph — telling
  the assistant to prefer the graph over grepping raw RTL and documenting both
  the MCP tools and the `hdl-kgraph tools` CLI fallback. The notes live in a
  managed `<!-- hdl-kgraph:start -->`…`<!-- hdl-kgraph:end -->` block (rewritten
  in place, surrounding content preserved); `--no-instructions` skips it.

## [1.0.1] - 2026-06-16

### Changed

- Documentation updated for the v1 release: the README status, `CONTRIBUTING`,
  and the changelog versioning note now describe the project as stable rather
  than alpha, and the roadmap no longer pins v1.0 to the deferred M8
  C/C++/Python boundary work.
- PyPI `Development Status` classifier moved from `3 - Alpha` to
  `5 - Production/Stable`.

## [1.0.0] - 2026-06-15

First stable release. v1.0 is a stability/API-freeze baseline on top of the
0.16.1 surface — no new features land in this version; it marks milestones
M1–M7 as delivered with a stable CLI and graph schema.

### Delivered (M1–M7)

- **Extraction.** SystemVerilog/Verilog and VHDL parsing (tree-sitter,
  `ERROR`-tolerant) with mixed-language pass-2 linking; design hierarchy, port
  connectivity, parameters, packages, class/UVM inheritance, and the
  clock/reset/CDC dataflow graph, each cross-file edge carrying a confidence
  score.
- **Real-world build inputs.** `.f` filelists, the SV preprocessor
  (defines, includes, `` `ifdef `` branch selection), VHDL library mapping, and
  `hdl-kgraph.toml` config, with per-file diagnostics.
- **Incremental rebuilds.** `update`/`watch` re-parse and re-link only the
  changed files and their dependents; `detect-changes`/`impact` answer "what
  changed and what does it affect?" in CI.
- **Analyses & visualization.** Clock domains, reset trees, CDC suspects,
  signal drivers/readers, UVM topology, graph lint, fan-in/out and hub/bridge
  metrics, plus a self-contained interactive HTML visualization.
- **MCP server.** `setup`/`serve` expose read-only, paginated tools so AI
  assistants can query the design.
- **Semantic enrichment.** Opt-in `build --enrich` overlays the pyslang and
  GHDL frontends for elaborated precision, with a discrepancy report.

### Stability

- Scalability hardening for 10–100+ GB designs: bounded, index-backed reads,
  precomputed whole-design summaries, and dirty-closure-scoped incremental
  writes.
- Stable public CLI with a unified exit-code / empty-result contract, and a
  versioned SQLite schema with a migration ladder (no forced full re-parse on
  a schema bump).

## [0.16.1] - 2026-06-15

### Fixed

- `serve --http` now parses IPv6 `[host]:port` addresses (e.g. `[::1]:8123`)
  instead of mangling them, and rejects out-of-range ports (only `1`–`65535`).
- `setup --db <path>` validates the database exists before writing assistant
  configs, matching `serve`, so a typo no longer points assistants at a missing
  database. (Both were pre-existing issues surfaced while reviewing the CLI split.)

## [0.16.0] - 2026-06-15

### Changed

- Split the 1.8k-LOC `cli/main.py` god module into focused submodules
  (`cli/_options.py`, `cli/_common.py`, `cli/build.py`, `cli/query.py`,
  `cli/analyze.py`, `cli/serve.py`); `cli/main.py` is now a ~70-line assembler
  that registers the commands. Entry points and the `main`/`_ProgressRenderer`
  import paths are unchanged. No behavior change — completes #70 ([#70]).

## [0.15.0] - 2026-06-15

### Changed

- Move the graph traversals that were inlined in CLI command handlers into
  `graph.analysis` (`resolve_unit`, `instantiation_count`, `node_kind_histogram`,
  `edge_kind_histogram`) and share one JSON/pagination renderer (`cli.render`)
  between the CLI and the MCP server, so neither the `status`/`modules`/`tree`
  commands nor the MCP tools re-implement graph logic or serialization. No
  behavior change ([#70]).
  *(First step of #70; the per-command file split of `cli/main.py` remains.)*

## [0.14.0] - 2026-06-15

### Changed

- Extract a shared `_WalkerBase` for the SystemVerilog and VHDL tree-sitter
  parsers (node-text/child helpers, the dispatch-driven `visit`, and parse-error
  counting), so cross-cutting traversal fixes are made once instead of twice.
  The ERROR-node policy is now an explicit, documented `ERROR_POLICY` per
  language (`skip` for SV, `descend` for VHDL) rather than two independently
  drifting `visit` implementations ([#72]).

## [0.13.1] - 2026-06-15

### Added

- Tests for previously-unexercised degradation paths: a corrupt stored IR row
  falling back to a fresh parse, an internal parser-walker exception being
  caught, non-UTF-8 sources tolerated via `errors="replace"`, and pass-1
  `FileIR`/`UnresolvedRef` pickling (the cross-process worker contract) ([#75]).
- Branch coverage (`[tool.coverage.run] branch = true`) so resilience/fallback
  branches count toward the gate instead of being half-covered by a line touch,
  and the CI test leg installs the `layout` extra (numpy/scipy) so those viz
  branches run instead of silently skipping ([#75]).

### Fixed

- Harden the VHDL-enrichment test skip-guard: `find_spec("pyGHDL.libghdl")`
  raises `ModuleNotFoundError` when the binary is present but the bindings are
  not (e.g. a distro `ghdl` package), so guard it instead of letting collection
  fail ([#75]).

## [0.13.0] - 2026-06-15

### Added

- Parsers now validate the loaded tree-sitter grammar at construction
  (`validate_grammar` / `GrammarMismatchError`): if the grammar is missing a
  node type the SystemVerilog/VHDL walker dispatches on, hdl-kgraph fails loudly
  with an actionable message instead of silently under-extracting after an
  upstream grammar rename ([#71]).

### Fixed

- Keep `parse_error_count` honest for the SystemVerilog parameter, typedef,
  instantiation, and package-import subtrees: these handlers consume their
  subtree without re-dispatching, so syntax errors inside them were previously
  uncounted ([#71]).

## [0.12.0] - 2026-06-15

### Changed

- Unify the CLI exit-code contract so scripts and CI can rely on it (`git
  diff --exit-code` style): `0` success — including an empty report; `1` a
  documented negative result (`detect-changes` found changes, or a name lookup
  matched nothing); `2` any error. Application/usage errors now exit `2`
  (previously `1`). The policy is documented in `hdl-kgraph --help` ([#73]).

### Fixed

- `query drivers --json` now exits `1` (not `0`) when the signal matches
  nothing, matching its text mode and `query instances-of` ([#73]).
- `build`/`update` convert an unexpected pipeline failure into a clean exit-`2`
  error instead of leaking a raw traceback, and `update` no longer trips a bare
  `assert` when it produces no build report ([#73]).

## [0.11.0] - 2026-06-15

### Added

- SQLite schema migration ladder: `update`/`watch` now upgrade an older database
  in place when a registered, additive, IR-compatible step exists (e.g. the
  `v7 → v8` summaries table) instead of forcing a full re-parse; transitions with
  no registered path — or a change to the persisted IR encoding, now versioned
  explicitly via `ir_codec.IR_CODEC_VERSION` — still fall back to a rebuild. Read
  commands stay read-only. Policy documented in `docs/schema-migrations.md` ([#74]).



### Changed

- Clarify the v1 scope: re-frame M8–M10 as an exploratory / community-contribution
  track (one wedge at a time, SDC/XDC first) and defer the M8 API/schema freeze
  until the migration ladder ([#74]) and the unified CLI exit-code contract
  ([#73]) land and the surface proves stable on real designs ([#81]).

## [0.10.1] - 2026-06-15

### Changed

- Bound the incremental linker to the dirty closure for large designs: scope the
  incremental delta write to the changed set and hydrate `find_signal_drivers`
  edges per module instead of across the whole graph ([#108]).

### Fixed

- Correct `` `include `` handling in the incremental parse path.

## [0.9.0] - 2026-06-15

### Added

- Precompute whole-design summaries and add a read-latency benchmark.
- Optional bearer-token authentication for the MCP HTTP transport ([#69]).

### Changed

- Serve MCP queries from bounded subgraphs instead of loading the full graph.
- Auto-resolve `` `include `` directives against discovered source directories,
  and confine filelist and `` `include `` path resolution to the build root
  ([#68]).

### Security

- Pin and verify the integrity of the vendored `d3.v7.min.js`, and mark the
  bundle as binary so Windows checkouts preserve its hash ([#78]).

## [0.8.2] - 2026-06-15

Maintenance release — version bump only, no functional changes.

## [0.8.1] - 2026-06-15

### Added

- `visualize`: highlight a searched node's neighbors and relationship lines.
- `visualize`: `--kinds` / `--exclude-kinds` node-category filters.

### Changed

- SystemVerilog incremental pass-2 linking foundation: mutate the prior graph in
  place instead of rebuild-and-splice ([#64]).
- Isolate parser-worker failures and add incremental-link scoping telemetry
  ([#65]).

### Fixed

- Incremental-link include-splicing edge duplication and `node_file` context
  ([#64]).
- Anchor reset/clock name regexes ([#76]) and harden unsupported-suffix routing
  ([#77]).

## [0.7.5] - 2026-06-14

### Added

- `ref_index` substrate for incremental linking ([#64]).
- Incremental-equals-full byte-identity gate: edit matrix and fuzz tests ([#64]).

### Changed

- Move `pyslang` / `pyVHDLModel` to an optional `enrich` extra ([#67]).
- Incremental update persistence writes only the changed rows ([#63]).

### Fixed

- Constrain the `visualize` graph view to the `--top` subtree ([#59]).

### Security

- Guard the VCS ref/revision against argument injection ([#66]).

## [0.7.4] - 2026-06-14

### Added

- `enriched` report command summarizing the `--enrich` delta.

## [0.7.2] - 2026-06-14

### Added

- Semantic enrichment via native frontends: pyslang for SystemVerilog and a
  GHDL backend for VHDL (M7).

### Changed

- Parse function-call size casts such as `` $clog2(N)'(v) ``.

### Fixed

- `visualize`: static-tier crash from a zoom reference used before declaration;
  stop fitting the graph on tab reveal; keep the canvas off the hierarchy tab.

## [0.6.5] - 2026-06-13

### Added

- `visualize`: collapsed community view with drill-down, two-level
  `--collapse --full` aggregation, and search-driven auto-expand
  (viz-scalability P3).
- `visualize`: precomputed-layout tier with a canvas renderer (P1–P2) and an
  export escape hatch — GraphML / GEXF / JSON (P5).
- Perforce and SVN support in `detect-changes`.
- Lint waivers via `[[lint.waivers]]`.

### Changed

- `visualize`: gzip-compress large inline payloads, with an inline-payload size
  guard and `--force-inline` override (P4).

## [0.6.4] - 2026-06-13

### Added

- Issue, feature-request, and pull-request templates.

### Changed

- Parallelize pass-1 parsing over a process pool.
- `detect-changes`: diff hashes without rehydrating the full graph.
- `metrics`: sample betweenness centrality above a node-count threshold.

### Fixed

- Linker: consistent confidence for multi-match scoped signals and
  both-branches duplicates.

## [0.6.3] - 2026-06-13

### Added

- Show build progress by default with a live per-file counter.
- List exact parse errors with `file:line` locations instead of just counts.

### Changed

- CLI polish: `--json` coverage, `detect-changes` exit codes, and
  serve / visualize / watch fixes.
- Upper-bound the tree-sitter dependencies, exercise the `[watch]` extra in CI,
  and gate PyPI publishing on the test suite.

---

Releases before `0.6.3` predate this changelog; their history lives in the git
log.

[Unreleased]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.16.1...v1.0.0
[0.16.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.16.0...v0.16.1
[0.16.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.13.1...v0.14.0
[0.13.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.10.2...v0.11.0
[0.10.2]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.10.1...v0.10.2
[0.10.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.9.0...v0.10.1
[0.9.0]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.8.2...v0.9.0
[0.8.2]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.8.1...v0.8.2
[0.8.1]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.7.5...v0.8.1
[0.7.5]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.7.4...v0.7.5
[0.7.4]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.7.2...v0.7.4
[0.7.2]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.6.5...v0.7.2
[0.6.5]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/chuanseng-ng/hdl-kgraph/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/chuanseng-ng/hdl-kgraph/releases/tag/v0.6.3

[#59]: https://github.com/chuanseng-ng/hdl-kgraph/pull/59
[#63]: https://github.com/chuanseng-ng/hdl-kgraph/pull/63
[#64]: https://github.com/chuanseng-ng/hdl-kgraph/pull/64
[#65]: https://github.com/chuanseng-ng/hdl-kgraph/pull/65
[#66]: https://github.com/chuanseng-ng/hdl-kgraph/pull/66
[#67]: https://github.com/chuanseng-ng/hdl-kgraph/pull/67
[#68]: https://github.com/chuanseng-ng/hdl-kgraph/pull/68
[#69]: https://github.com/chuanseng-ng/hdl-kgraph/pull/69
[#76]: https://github.com/chuanseng-ng/hdl-kgraph/pull/76
[#77]: https://github.com/chuanseng-ng/hdl-kgraph/pull/77
[#70]: https://github.com/chuanseng-ng/hdl-kgraph/issues/70
[#71]: https://github.com/chuanseng-ng/hdl-kgraph/issues/71
[#72]: https://github.com/chuanseng-ng/hdl-kgraph/issues/72
[#73]: https://github.com/chuanseng-ng/hdl-kgraph/issues/73
[#74]: https://github.com/chuanseng-ng/hdl-kgraph/issues/74
[#75]: https://github.com/chuanseng-ng/hdl-kgraph/issues/75
[#78]: https://github.com/chuanseng-ng/hdl-kgraph/pull/78
[#81]: https://github.com/chuanseng-ng/hdl-kgraph/issues/81
[#108]: https://github.com/chuanseng-ng/hdl-kgraph/pull/108
