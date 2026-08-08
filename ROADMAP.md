# hdl-kgraph Roadmap

`hdl-kgraph` builds a queryable knowledge graph from hardware description language
(HDL) source code — SystemVerilog, Verilog, and VHDL first, with C/C++/Python,
emerging HDLs (Chisel, Amaranth, SpinalHDL), and EDA flow languages (Tcl/SDC
constraints, UPF power intent, Perl scripting, SLN portable stimulus) as future
targets. The architecture is
modeled on [code-review-graph](https://github.com/tirth8205/code-review-graph):
Python 3.10+, tree-sitter parsing, NetworkX graph algorithms, SQLite persistence,
a CLI, and (later) an MCP server for AI assistants. Distribution is via pip/PyPI.

**Status (v2.4.0):** Milestones M1–M7 are shipped, and the v2.0 scalability epic
(M11–M13a — bounded reads, out-of-core whole-design summaries, and a
memory-bounded incremental linker) is delivered. M8–M10 (EDA flow languages,
DPI-C, emerging HDLs) remain an exploratory/community-contribution track, but
two wedges are now in. M8's C/C++/Python boundary: DPI-C linking (SV
`import`/`export "DPI-C"` ↔ C/C++ functions, v2.3) and cocotb testbench scanning
(Python `dut.<signal>` → `READS`/`DRIVES`, test discovery → `TEST_COVERS`, v2.4).
M10's first wedge: SDC/XDC timing constraints (`create_clock` → `CLOCK` nodes and
authoritative `CLOCKED_BY` evidence, `set_clock_groups`/`set_false_path` → CDC
suppression) — see M10 below. The Rust core (M13) is deferred — profiling (M12)
showed it isn't needed for the RAM goal.

---

## Knowledge graph schema

The schema is the core of the project. It lives in `src/hdl_kgraph/schema.py` from
day one so every milestone extends rather than reworks it.

### Node kinds

Every node carries: `id`, `kind`, `name`, `qualified_name`, `file`, `line_span`,
`language`, and a free-form `attrs` dict.

| Group | Kinds | Notable attrs |
|---|---|---|
| Structure | `FILE`, `FILELIST`, `LIBRARY` | path, content hash, language, work library |
| Verilog/SV design units | `MODULE`, `PROGRAM`, `INTERFACE`, `MODPORT`, `PACKAGE`, `CHECKER`, `PRIMITIVE` (UDP) | `is_macromodule`, `is_celldefine` |
| VHDL design units | `ENTITY`, `ARCHITECTURE`, `VHDL_PACKAGE`, `PACKAGE_BODY`, `CONFIGURATION`, `CONTEXT` | library, original casing (names normalized lowercase) |
| Behavioral | `FUNCTION`, `TASK`, `PROCESS` (VHDL process / SV always block), `GENERATE_BLOCK` | `always_ff`/`always_comb`/`always_latch`, sensitivity list |
| OOP / verification | `CLASS`, `CONSTRAINT`, `COVERGROUP`, `COVERPOINT`, `PROPERTY`, `SEQUENCE`, `ASSERTION`, `CLOCKING_BLOCK` | `is_virtual`, UVM base (via EXTENDS chain) |
| Data | `PORT`, `PARAMETER` (param/localparam/generic), `SIGNAL` (net/variable/VHDL signal), `TYPEDEF`, `STRUCT`, `ENUM`, `ENUM_MEMBER` | direction, width expression, data type, default |
| Elaboration | `INSTANCE` | instance name, target name, parameter overrides |
| Preprocessor | `MACRO` (`` `define ``), `INCLUDE_FILE` | body, arity, guard macro |
| Constraints / scenarios | `CLOCK`, `TIMING_CONSTRAINT`, `POWER_DOMAIN`, `SCENARIO`, `ACTION` | period/waveform, virtual/generated clock master, constraint command, supply/isolation strategies, scenario resources |

### Edge kinds

Every edge carries: `src`, `dst`, `kind`, `confidence`, and `attrs`.

| Kind | Meaning |
|---|---|
| `DECLARES` | scope → declaration (file→module, module→port/signal/param, class→method, …) |
| `INSTANTIATES` | instance → target module/entity (parent scope `DECLARES` the instance) |
| `CONNECTS` | instance → port binding (named / positional / `.*` wildcard) |
| `PARAMETERIZES` | instance → parameter override |
| `IMPORTS` | scope → SV package (wildcard vs explicit symbol) |
| `INCLUDES` | file → file (`` `include ``) |
| `DEFINES_MACRO` / `USES_MACRO` | file/scope ↔ macro |
| `EXTENDS` | SV class inheritance |
| `IMPLEMENTS` | VHDL architecture → entity |
| `BINDS` | SV `bind` directive / VHDL configuration → target |
| `USES_PACKAGE` | VHDL `library`/`use` clause |
| `DRIVES` / `READS` | process/assign/instance port → signal (dataflow) |
| `CLOCKED_BY` / `RESETS` | process/module → clock/reset signal |
| `ASSERTS_ON` / `COVERS` | assertion/covergroup → signal/property |
| `TEST_COVERS` | testbench/UVM test/SLN scenario → DUT module |
| `FOREIGN_BINDS` | SV DPI-C import/export ↔ C function (M8) |
| `GENERATED_FROM` | generated HDL → generator source (M9 Chisel/Amaranth/SpinalHDL, M10 Perl codegen) |
| `CONSTRAINS` | timing constraint/clock/power domain → port/signal/instance/clock (M10) |
| `REFERENCES_FILE` | Perl/Tcl script → HDL file it reads/compiles/generates (M10) |
| `INVOKES` | SLN/PSS action → sub-action it does, same-file (M10) |

### Confidence convention

| Score | Meaning |
|---|---|
| `1.0` | syntactically resolved within the compilation unit |
| `0.8` | cross-file name match, unique candidate |
| `0.6` | ambiguous name match (multiple candidates; all edges emitted) |
| `0.4` | heuristic (e.g. `CLOCKED_BY` inferred from `clk`/`clock` naming) |

Unresolved targets become stub nodes with `attrs["unresolved"] = True` so the graph
is always connected and queries never dead-end silently.

### Two-pass build architecture

- **Pass 1 (parse):** each file is parsed independently into a per-file IR of
  declarations and unresolved references. Parallelizable by design (currently
  run serially; parallel execution is tracked in issue #26).
- **Pass 2 (link):** cross-file resolution — instance→definition, package imports,
  VHDL library/work scoping, bind/configuration resolution — with confidence scoring.

This split makes incremental updates (M4) cheap: re-run pass 1 only for changed
files, then re-run the fast global pass 2.

---

## M1 — v0.1: SystemVerilog/Verilog structural graph (MVP)

**Goal:** `pip install -e . && hdl-kgraph build ./rtl` produces a queryable design
hierarchy graph for a Verilog/SystemVerilog codebase.

- [x] Schema module: `NodeKind`/`EdgeKind` enums, `Node`/`Edge` dataclasses,
      confidence convention documented in docstrings
- [x] Grammar bake-off: evaluate `gmlarumbe/tree-sitter-systemverilog` vs
      `tree-sitter/tree-sitter-verilog` against the fixture corpus; pick one grammar
      for both `.v` and `.sv` (see Risks) — **winner: `tree-sitter-systemverilog`;
      results in docs/grammar-bakeoff.md**
- [x] tree-sitter SV parser extracting: `MODULE`, `INTERFACE`, `PACKAGE`, `PROGRAM`,
      `FUNCTION`/`TASK`, `PORT`, `PARAMETER`, `INSTANCE`, `TYPEDEF`/`STRUCT`/`ENUM`,
      `CLASS` (declaration + `EXTENDS` only)
- [x] Edges: `DECLARES`, `INSTANTIATES`, `CONNECTS` (named + positional),
      `PARAMETERIZES`, `IMPORTS`, `EXTENDS`
- [x] Pass-2 linker with confidence scoring and unresolved stub nodes
- [x] NetworkX in-memory graph + SQLite persistence (`nodes`, `edges`, `files`
      tables; content-hash column added now for M4)
- [x] CLI: `build`, `status`, `query` (e.g. `hdl-kgraph query instances-of fifo`),
      `tree` (print design hierarchy from a top module)
- [x] Error tolerance: files with tree-sitter ERROR nodes still yield partial
      results; parse-error count surfaces in `status`
- [x] File-size guards and exclude-glob config (huge generated netlists,
      `` `pragma protect `` encrypted IP)
- [x] Test corpus: 10–15 small fixtures (plain Verilog, SV interfaces, a class,
      an unresolved instance)
- [x] Claim the `hdl-kgraph` name on PyPI with a 0.1 release (release workflow
      and docs/releasing.md are in; publishing is a maintainer action)

**Acceptance:** builds a graph from a real OSS design (e.g. ibex-class repo);
`tree` prints the correct hierarchy; ≥90% of fixture constructs extracted;
CI green on Python 3.10–3.13.

## M2 — v0.2: Real-world inputs — preprocessor, filelists, includes (MVP)

**Goal:** works on projects as they actually exist: `` `define ``/`` `ifdef `` soup,
`.f` filelists, include directories.

- [x] Filelist parser: `.f`/`.vc` (`+incdir+`, `+define+`, nested `-f`, `-y`/`-v`
      library dirs, env-var expansion); `FILELIST` nodes; file order preserved
      (`-y` dirs are recorded on the FILELIST node only; on-demand library
      module lookup is M3+ territory)
- [x] Lightweight SV preprocessor: `` `define `` (with arguments),
      `` `ifdef ``/`` `ifndef ``/`` `elsif `` branch selection from configured
      defines, `` `include `` resolution → `INCLUDES`/`DEFINES_MACRO`/`USES_MACRO`
      edges; line map back to original source for accurate spans —
      **`` `" `` stringification / ``` `` ``` pasting are best-effort textual,
      and macro arguments must close on the invocation line (documented)**
- [x] "Both branches" mode when no define set is given (emit both sides of
      `` `ifdef ``; the branch a define-less compile would select keeps full
      confidence, alternatives are emitted at 0.6 — this keeps `` `ifndef ``
      include guards and default-define fallbacks at 1.0)
- [x] Config file `hdl-kgraph.toml`: source globs, filelists, defines, include
      dirs, VHDL library map (carried; consumed in M3), top modules
- [x] CLI: `build -f tb.f`, `--define`, `--incdir`

**Acceptance:** builds cleanly from an unmodified vendor-style `.f` file;
macro-instantiated modules resolve after expansion; line mapping verified by tests.

## M3 — v0.3: VHDL + mixed-language designs (MVP)

**Goal:** first-class VHDL extraction and Verilog↔VHDL linking — completes the
"HDL" promise of the project name.

- [x] tree-sitter VHDL parser: `ENTITY`, `ARCHITECTURE` (+`IMPLEMENTS`),
      `VHDL_PACKAGE`/`PACKAGE_BODY`, `CONFIGURATION` (+`BINDS`),
      generics→`PARAMETER`, ports, signals, processes, component and direct
      entity instantiation — **grammar: `jpt13653903/tree-sitter-vhdl` (PyPI
      `tree-sitter-vhdl`); caveats in docs/grammar-bakeoff.md. Component
      declarations are deliberately not graph nodes: instantiations carry the
      style and the linker resolves through configuration/default binding**
- [x] Case-insensitive name normalization (original casing kept in attrs)
- [x] Library/work mapping (`--lib work=./src` style config); `LIBRARY` nodes;
      `USES_PACKAGE` edges; component-vs-entity binding resolution (specific
      label > `all` > `others`; `ieee`/`std` packages stay library-qualified
      stubs by design)
- [x] Cross-language pass-2 linking: VHDL component instantiating an SV module and
      vice versa (case-insensitive name match, capped at confidence 0.8 even
      within one file; vendor name-mangling caveats documented in README)
- [x] `tree` and `query` work across language boundaries (entities expand
      through their architectures, printed as `name(arch)`)

**Acceptance:** mixed Verilog-top/VHDL-leaf and VHDL-top/Verilog-leaf fixtures both
produce a single connected hierarchy; a VHDL configuration overriding a default
binding is honored.

## M4 — v0.4: Incremental updates, watch mode, impact analysis (MVP)

**Goal:** fast enough to live alongside an editor; answers "what does my change
affect?"

- [x] Content-hash incremental rebuild: `update` re-parses changed/added/removed
      files plus their preprocessor-dependent files (reverse `INCLUDES` /
      `USES_MACRO` closure; a changed `.f` define or incdir dirties all files in
      that filelist — via the build-options fingerprint, which falls back to a
      full rebuild), then re-links pass 2 — **per-unit pass-1 IRs (plus macro
      event logs) persist in the `file_irs` table; unchanged units re-link
      without re-parsing**
- [x] **Incremental pass-2 link (#64-B, v0.8): `update` re-resolves only the
      dirty closure plus its resolution neighborhood (refs whose target name's
      definition set changed, via the persisted `ref_index`), mutating the prior
      resolved graph in place and reusing every other ref's edges. Byte-identical
      to a full re-link (the #64-C equivalence matrix + fuzz gate it); SV/Verilog
      only — VHDL / binds / `--enrich` fall back to a full re-link. Scales by
      change, not design size (e.g. 7 of 2689 refs re-resolved on a 1-file edit);
      a prior-graph read is the fixed cost, so on resolution-light designs it is
      scale-headroom rather than a wall-time win.**
- [x] `watch` via watchdog (debounced); `detect-changes` (vs git/svn/Perforce or last build)
- [x] Impact radius: `impact <file|module>` → transitively affected modules via
      `INSTANTIATES`/`IMPORTS`/`INCLUDES`/`EXTENDS` (reverse `` `include `` and
      macro edges included — a header change dirties all users; VHDL
      `USES_PACKAGE`/`IMPLEMENTS`/`BINDS` covered too)
- [x] SQLite schema versioning + migration guard (schema v2; the database is a
      derived cache, so the migration path is a rebuild — read commands refuse
      with a clear message, `update`/`watch` fall back to a full rebuild)
- [x] Documented benchmark target: incremental update of 1 file in a 2k-file
      design < 1 s — **0.85 s measured; procedure and results in
      docs/benchmarks.md (`scripts/bench_incremental.py`)**

**Acceptance:** editing one file and running `update` re-parses only that file;
`impact` correctly flags parents/importers/includers in fixtures; watch mode
survives rapid save bursts.

## M5 — v0.5: HDL analyses + visualization

**Goal:** insights, not just structure.

- [x] Dataflow edges: `DRIVES`/`READS` from continuous assigns, always/process
      blocks, and instance port directions — **always blocks / assigns become
      PROCESS nodes (`always@<line>` / `assign@<line>`); refs resolve against
      the enclosing unit's PORT/SIGNAL children, never by global name;
      undeclared names become implicit SIGNAL stubs at ≤ 0.6; instance-level
      flow is derived from resolved CONNECTS bindings + port directions;
      `query drivers <signal>` pre-stages M6's `find_signal_drivers`**
- [x] `CLOCKED_BY`/`RESETS` extraction (sensitivity-list evidence = 1.0;
      name-pattern heuristic = 0.4) → clock-domain report, reset tree, and
      CDC-suspect crossings (signal driven in domain A, read in domain B) —
      **`query clock-domains` / `reset-tree` / `cdc`; clock nets alias-merge
      across the hierarchy through single-identifier port connections; VHDL
      `rising_edge()` is 1.0 evidence; combinational paths bridge one step
      (no fixpoint); synchronizers are not recognized — these are suspects,
      not violations (M10's SDC `set_clock_groups` is the planned suppressor)**
- [x] Lint-flavored analyses: unconnected/dangling ports, undriven/unread signals,
      never-instantiated modules (dead code), parameter overrides equal to defaults
      — **`hdl-kgraph lint [--check NAME] [--top NAME] [--json]`, always exits 0;
      signal checks skip parse-error files and implicit-net stubs; explicitly
      open `.x()` bindings reported separately from unconnected ports**
- [x] Graph metrics: module fan-in/fan-out, hub/bridge detection (betweenness),
      community detection (Louvain via NetworkX) for subsystem discovery —
      **`hdl-kgraph metrics [--limit N] [--communities]` over the module-level
      instantiation projection (entities absorb their architectures); Louvain
      seeded for run-to-run determinism; articulation points flag true bridges**
- [x] `visualize` → self-contained D3.js HTML (hierarchy view + force-directed
      view; filter by node kind, edge kind, Louvain community) — **d3 v7 vendored
      (ISC; `viz/static/LICENSE.d3`) so the artifact opens air-gapped; the
      force view renders on canvas (SVG dies near 1k nodes) and defaults to
      the module projection, `--full` embeds everything; < 0.8-confidence
      edges drawn dashed**
- [x] SV verification constructs: `ASSERTION`/`PROPERTY`/`SEQUENCE`,
      `COVERGROUP`/`COVERPOINT`, `CONSTRAINT`, `CLOCKING_BLOCK` nodes;
      `ASSERTS_ON`/`COVERS` edges; UVM topology report (`EXTENDS` chains to
      `uvm_*` bases, `TEST_COVERS`) — **`query uvm`; `ASSERTS_ON` resolves to a
      sibling PROPERTY/SEQUENCE before signals; `TEST_COVERS` is a 0.4
      tb-name-pattern heuristic (tb tops → instantiated DUTs, uvm_test
      subclasses → the same DUTs); immediate (procedural) assertions deferred.
      Schema is v3 — pass-1 IRs changed, so the first `update` after upgrading
      falls back to one full rebuild**

**Acceptance:** the clock-domain report on a two-clock fixture identifies both
domains and the CDC point; visualization renders a 1k-node graph without freezing;
a UVM example testbench yields a component-tree report.

## M6 — v0.6: MCP server + AI-assistant integration

**Goal:** AI assistants can query the design directly.

- [x] fastmcp server (`hdl-kgraph serve --mcp`), shipped as the `[mcp]` extra —
      **lazy import with a clear install hint; the CI lint job intentionally
      runs without the extra so the core never grows a hard fastmcp dependency**
- [x] Tools: `find_module`, `get_hierarchy`, `who_instantiates`, `port_map`,
      `impact_of_change`, `clock_domains`, `find_signal_drivers`, `uvm_topology`,
      `search_nodes` — **thin wrappers over `graph/analysis.py` (the drivers
      query and impact-seed resolution moved out of the CLI; `port_map` and
      `search_nodes` are new analysis functions the CLI can reuse);
      `find_signal_drivers` takes the module scope the acceptance question
      needs, with VHDL architectures answering for their entities**
- [x] Read-only stdio and HTTP modes; responses sized for LLM context windows
      (pagination, summaries) — **stdio default, `--http HOST:PORT` for
      streamable HTTP; every list tool returns a
      `{total, offset, count, truncated, items}` envelope (limit clamped to
      500), hierarchy defaults to depth 3 with a 500-node cap, impact leads
      with a summary so truncated pages still answer "what breaks"; each tool
      answers from a bounded, index-backed subgraph and never loads the whole
      graph (v0.9), so a concurrent `update`/`watch` rewrite is picked up with
      no staleness window**
- [x] Docs: Claude Code / Claude Desktop configuration snippets — **docs/mcp.md:
      tool reference, transports, cold-checkout walkthrough**
- [x] `hdl-kgraph setup`: detect installed assistants and write their MCP
      config — **Claude Code via project-scope `.mcp.json`, Claude Desktop via
      its platform config file; idempotent merge that preserves other servers,
      one-time `.bak` backups for user-level files, `--list`/`--dry-run`/
      `--yes`; extensible one-entry-per-assistant registry in `mcp/setup.py`**

**Acceptance:** from a cold checkout, an AI assistant can answer "what drives
signal X in module Y" and "what breaks if I change this port" using MCP tools only.

**Post-M6 (does not gate MVP or v0.6):** visualization scalability for very
large designs — tiered precomputed-layout / aggregation / export strategy.
Phases 1–2 (canvas renderer hygiene; precomputed "static" layout tier with
`--layout auto|live|static` auto-routing) are delivered; Phases 3–6
(aggregation/drill-down, payload compression, GraphML/GEXF export, WebGL)
remain parked. Analysis and phased plan in
[docs/viz-scalability.md](docs/viz-scalability.md).

## M7 — v0.7: Semantic enrichment via native frontends (stretch)

**Goal:** elaboration-accurate facts where native parsers are available;
tree-sitter remains the always-works baseline.

- [x] Enrichment plugin interface: backends declare capabilities; results merge
      with higher confidence — **`hdl_kgraph.enrich`: an `EnrichmentBackend`
      runs after pass-2 linking over the whole-design inputs and the linked
      graph, returning deltas (edge upgrades, new elaborated nodes/edges,
      discrepancies); the runner merges them via `graph.builder`'s
      `add_or_upgrade_edge`/`ensure_node`, stamping `attrs["source"] =
      "elaborated"`. Opt-in via `build --enrich`; whole-design elaboration so it
      re-runs on every `update`. Backends ship in the core install, not extras
      (`pyslang`, `pyVHDLModel` are core dependencies); elaboration stays opt-in
      at runtime**
- [x] pyslang backend: parameterized generate elaboration, `defparam`, accurate
      symbol binding → edges upgraded to 1.0 — **`enrich/slang_backend.py`
      unrolls generate loops/instance arrays (the headline acceptance case) and
      confirms `INSTANTIATES` bindings; an un-elaboratable design degrades to the
      heuristic graph with diagnostics. Full type/width and
      `CONNECTS`/`PARAMETERIZES` value upgrades are a documented follow-on**
- [x] GHDL analysis backend: VHDL binding resolution — **`enrich/ghdl_backend.py`
      drives GHDL (via its `pyGHDL`/`libghdl` bindings) to confirm
      component/entity/configuration bindings (`INSTANTIATES` → 1.0), flag a
      `wrong_target` where a configuration rebinds an instance the heuristic
      guessed by name, and unroll `for ... generate` over static ranges. GHDL is
      a system binary, not a pip package, so the backend probes for it and is
      silently skipped when absent (its tests skip-guard accordingly); the
      heuristic VHDL graph is the always-works baseline. Generic-bounded
      generate ranges and `CONNECTS`/`PARAMETERIZES` value upgrades are a
      documented follow-on, paralleling slang's own**
- [x] Discrepancy report: where heuristic edges disagreed with elaboration —
      **`hdl-kgraph discrepancies` over a new `discrepancies` SQLite table
      (schema v6); `instance_count` (generate multiplicity) and `wrong_target`
      findings, with `--json`**

**Acceptance:** on fixtures with parameterized generates, instance counts match
elaborated reality (`tests/test_enrich.py`, `tests/fixtures/param_generate.sv`);
a plain `build` (enrichment off) is unchanged. *(pyslang/pyVHDLModel are now
core dependencies rather than optional extras — see the interface note above.)*

## Scalability hardening — v0.9–v0.10 (cross-cutting)

**Goal:** the graph can reach 10–100+ GB; reads and incremental writes must
scale with the *query/change*, not the design size, so an AI assistant never
waits on (or runs out of memory loading) the whole graph.

- [x] **Bounded, index-backed reads (v0.9):** `GraphQuery`
      (`storage/query.py`) answers each MCP/CLI tool by hydrating only the
      subgraph it touches through the existing indices, then runs the same
      `graph.analysis` function on it — byte-identical to the full-graph path
      (`tests/test_query.py` sweeps every name). A localized query is
      1000–16000× faster than the old per-call `SqliteStore.load()`.
- [x] **Precomputed whole-design summaries (v0.9, schema v8):** clock-domain/CDC
      and UVM-topology reports are computed at build into the `summaries` table,
      so those tools read O(1) instead of scanning the graph.
- [x] **Dirty-closure-scoped incremental write (v0.10):** `save_incremental`
      reads and rewrites only the changed rows (a one-file edit touches ~0.04 %
      of the corpus), not the whole `nodes`/`edges` tables.
- [x] **Memory-bounded incremental linker (v2.0, was the last O(design) cost):**
      the incremental linker no longer loads the full prior graph — it re-resolves
      the dirty closure straight from SQLite (SQL-backed resolution + bounded
      stub-GC + out-of-core TEST_COVERS + selective IR decode), byte-identical to
      the in-memory path. Landed opt-in as `--bounded-link` (v1.12), became the
      default (v1.13+), formalized in v2.0 — see
      [docs/scalability.md](docs/scalability.md).

**Acceptance:** `tests/test_query.py` (read parity + no-full-load proof),
`tests/test_incremental_equivalence.py` (byte-identical scoped writes),
`scripts/bench_query.py` and `scripts/bench_incremental.py` (latency + bounded
read/write volume). Detail in [docs/scalability.md](docs/scalability.md).

---

## v2.0 — Rust-cored re-architecture for the 10–100 GB regime ([#128])

The in-memory `MultiDiGraph` is an architectural ceiling, not a config knob: the
profiling below shows it is **~2.3× the on-disk DB**, so a 100 GB design needs
~225 GB RAM and "does not load." v2 is a deliberate major-version break — an
out-of-core / compact core behind the stable `storage`/`GraphQuery` seam.

**Delivered in 2.0.0 (out-of-core, without a Rust core).** The RAM goal landed
incrementally behind the existing Python `storage` seam (releases 1.8 → 1.15,
formalised as 2.0.0): reads (`GraphQuery`), whole-design summaries, the
incremental linker, IR decode, and TEST_COVERS are all bounded; a 100 GB design
loads via the out-of-core path. The bespoke Rust core (M13) is **deferred** — off
the critical path for the RAM goal.

- [x] **M11 — profile & decision gate:** memory + CPU profile of `build` /
      summaries / `load()` across a scale sweep (`scripts/profile_v2.py`),
      pinning the dominant cost and selecting the M12 path —
      [docs/v2/m11_profiling.md](docs/v2/m11_profiling.md). Finding: `load()` is
      graph-CPU-bound (85–90 %), not SQLite-I/O-bound, and **peak RAM from
      materialising the whole graph is the binding constraint**.
- [x] **M12 — graph-layer spike:** evaluated an out-of-core layer and a compact
      in-memory core via `scripts/spike_m12.py` —
      [docs/v2/m12_graph_layer.md](docs/v2/m12_graph_layer.md). Finding: an
      **off-the-shelf out-of-core layer hits the RAM target** — SQL-native scans
      (zero dep) and `kuzu` (embedded graph DB) answer a whole-design scan in
      **bounded RAM** (~50 MiB / ~110 MiB flat, vs NetworkX's 4610 B/node linear →
      ~228 GB at 100 GB). `rustworkx` lowers the constant (~29 %) but stays linear
      (runner-up, ~10 GB regime). **A bespoke Rust core is not required to clear
      the RAM ceiling**, so M13 is deferred.
- [x] **M12.5 — productionise the out-of-core whole-design summaries** behind
      `GraphQuery`: clock-domains/CDC (1.9.0) and UVM topology (1.10.0) compute
      from SQLite when the persisted summary is absent, never `SqliteStore.load()`
      (byte-identical to the NetworkX oracle). The CLI report commands then routed
      through the same bounded path: `clock-domains`/`cdc`/`uvm` (2.0.0), then the
      remaining single-target commands `instances-of`/`drivers`/`unresolved` (2.1.0)
      and `modules`/`reset-tree` (2.2.0, adding an out-of-core `reset_summary_sql`).
      **As of 2.2.0 no `query` command full-loads the graph.**
- [x] **M13a — memory-bounded incremental linker (#119):** the `update` re-link
      re-resolves only the dirty closure straight from SQLite (lazy
      `idx_nodes_kind_name`/`idx_edges_*`, bounded stub-GC), byte-identical to a
      full build. Shipped opt-in (1.12.0) → default (1.13.0), with selective IR
      decode (1.14.0) and out-of-core TEST_COVERS re-derivation (1.15.0). The
      whole `update` pipeline is now bounded by the dirty closure.
- [ ] **M13 — PyO3 Rust core (deferred; only if M12's off-the-shelf path proves
      insufficient):** compact streaming graph + pass-2 link + whole-design scans;
      subsumes the memory-bounded linker (#119). Per M12, the off-the-shelf
      out-of-core path clears the documented wall, so this is no longer on the
      critical path — revisit only if a scan needs what neither SQL nor kuzu
      expresses efficiently.
- [ ] **M14 — native tree-sitter walk → `FileIR` (optional):** remove per-node FFI
      from the parse hot path.

[#128]: https://github.com/chuanseng-ng/hdl-kgraph/issues/128

---

## M8–M10 are an exploratory track, not a delivery commitment

M8–M10 span roughly a dozen languages and ecosystems (C/C++, Python/cocotb,
Chisel/FIRRTL, Amaranth, SpinalHDL, Tcl/SDC, UPF, Perl, SLN). For a
single-maintainer project, bus-factor is the dominant risk, so these milestones
are scoped as an **exploratory / community-contribution track** rather than a
committed delivery schedule. The plan is to deepen **one wedge at a time**; the
**SDC/XDC slice (issue #25)** is the chosen first wedge — it has the highest
analysis-quality payoff per line of code (it upgrades the M5 clock heuristics to
authoritative `create_clock` evidence and lets `set_clock_groups`/`set_false_path`
suppress CDC suspects), and its schema, parser scaffold, and fixtures are already
staged.

Bus-factor is held down by the existing levers: the schema contract in
`schema.py`, parser isolation behind `parser/base.py`, and the "smallest file that
breaks extraction" fixture funnel.

**v1.0 has shipped as a stable-API + schema baseline** once its prerequisites
landed — the SQLite migration ladder (issue #74) and the unified CLI/exit-code
contract (issue #73) — rather than on a fixed feature count. The C/C++/Python
boundary work that M8 originally bundled with v1.0 is now a post-v1 (v1.x)
target on the exploratory track above.

## M8 — v1.x: C/C++/Python boundary (stretch)

**Goal:** the full system picture — DPI, cosim, testbench scripting.

- [x] DPI-C linking: SV `import "DPI-C"`/`export "DPI-C"` ↔ C/C++ function
      definitions (tree-sitter-c/cpp) via `FOREIGN_BINDS` edges — **C/C++
      pass-1 parsers (`parser/c.py`) emit a `FUNCTION` node per top-level
      definition/prototype; the SV parser extracts `import`/`export "DPI-C"`
      declarations (alias `c_name =` form, pure/context properties, imported
      tasks); pass 2 binds them by linkage name, filtered to C/CPP candidates
      (a unique cross-file match is 0.8, an unresolved name degrades to a
      stub). C/C++ bypass the SV preprocessor; bare-name matching is the tier
      (no C++ mangling, no C preprocessor). Schema unchanged — `FOREIGN_BINDS`
      and the `C`/`CPP` languages already existed. See docs/extraction.md**
- [x] Python testbench scanning: cocotb `dut.signal` attribute access →
      `READS`/`DRIVES` (confidence 0.6); pytest/cocotb test discovery →
      `TEST_COVERS` — **`parser/python.py` (tree-sitter-python) extracts
      `@cocotb.test` functions as PYTHON `FUNCTION` nodes; `dut.sig.value =`/
      `setimmediatevalue` → DRIVES, other `dut.sig` reads → READS, resolved
      against the DUT module's ports/signals; `TEST_COVERS` to the DUT (0.4).
      The DUT is heuristic — configured `[build].top` else a filename guess
      (`test_fifo.py` → `fifo`) — so a `.py` is only a source when it mentions
      `cocotb` (content-sniffed), and `update` re-links cocotb designs fully.
      See docs/extraction.md**
- [x] Stable public CLI + graph schema, semver commitment, documented
      migration policy — **shipped in v1.0** once its prerequisites landed: the
      SQLite schema migration ladder (#74) so a version bump no longer forces a
      full re-parse, and the unified CLI exit-code / empty-result contract (#73)
      so the scripting surface is stable. A stable public Python API
      (`hdl_kgraph.api`) remains a v1.x follow-up.
- [ ] PyPI 1.0 release — the package is published at
      https://pypi.org/project/hdl-kgraph/ and the code is at 1.0; pushing the
      `v1.0.x` tag fires the publish workflow (see docs/releasing.md). A
      documentation site is a v1.x follow-up.

**Acceptance:** a cocotb-driven SV design with DPI-C calls shows one connected
graph spanning all three languages.

## M9 — v1.x: Emerging HDLs (stretch)

**Goal:** Chisel/FIRRTL, Amaranth, SpinalHDL, Bluespec support.

- [ ] Pragmatic first step: parse their **generated Verilog** and link back to
      sources via emitted locators (Chisel `// @[Foo.scala 42:11]`, SpinalHDL
      comments, FIRRTL source locators) → `GENERATED_FROM` edges
- [ ] Direct FIRRTL parsing as a second step (FIRRTL is a small, well-specified IR)
- [ ] Amaranth: Python AST scan of `m.submodules` structure
- [ ] Each generator shipped as an optional extra

## M10 — v1.x: EDA flow languages — Tcl/SDC/UPF, Perl, SLN (stretch)

**Goal:** capture the flow *around* the RTL: timing constraints as authoritative
clock evidence, power intent, legacy script codegen lineage, and portable-stimulus
scenario coverage.

- [x] SDC/XDC parsing (Tcl subset): `create_clock`/`create_generated_clock` →
      `CLOCK` nodes (virtual and generated clocks supported); `set_false_path`,
      `set_multicycle_path`, `set_input_delay`/`set_output_delay`,
      `set_clock_groups` → `TIMING_CONSTRAINT` nodes with `CONSTRAINS` edges;
      `get_ports`/`get_pins`/`get_cells`/`get_clocks` object queries resolved to
      design nodes (exact match 1.0; glob patterns 0.8/0.6) — **`parser/tcl.py`'s
      `SdcParser` (hand-written Tcl-subset scanner, literal `set` substitution
      only) and a `_resolve_constrains` pass-2 branch; wired into discovery and
      the pipeline. See docs/extraction.md ([#25])**
- [x] M5 synergy: `create_clock` is authoritative `CLOCKED_BY` evidence — upgrades
      the 0.4 name heuristic to 1.0; `set_clock_groups -asynchronous` and
      `set_false_path` feed the CDC report as declared-safe crossings —
      **`graph.clocks.apply_sdc_clock_evidence` (called from `link_graph`) bumps
      the backed CLOCKED_BY edges to 1.0; `cdc_suspects` flags suppressed
      crossings `declared_safe` and the report partitions them out**
- [x] UPF (IEEE 1801) power intent: `create_power_domain` → `POWER_DOMAIN` nodes
      with `CONSTRAINS` edges to their elements; supply nets/sets and isolation/
      retention/level-shifter strategies in attrs; power-domain report (domains,
      strategies) analogous to the CDC report — **`UpfParser` shares the SDC
      Tcl-subset base; `-elements` reuse the `cells` query resolution; the
      `power_domains` report ships as a query/MCP tool + persisted summary (with an
      out-of-core SQL fallback) + `analyze` digest line. Domain-crossing suspects
      are a follow-on. See docs/extraction.md, docs/analyses.md**
- [x] Tcl flow scripts: `read_verilog`/`read_vhdl`/`analyze`/`add_files` →
      `REFERENCES_FILE` edges; `source` chains; literal `set` variable
      substitution only — Tcl is never evaluated (see Risks) — **`TclScriptParser`
      shares the SDC/UPF Tcl-subset base; read/analyze/add/source commands all
      emit `REFERENCES_FILE` (one edge kind, `attrs["mode"]` distinguishes them —
      simpler and uniform for incremental than splitting `source` onto
      `INCLUDES`); a new pass-2 `_resolve_file_ref` binds each path to its real
      `FILE` node or a non-shadowing `unresolved:file:` stub. See
      docs/extraction.md**
- [x] Perl legacy scripting: detect HDL files a script reads/writes/generates
      (`open()` of `.v`/`.sv` paths, heredoc-embedded Verilog) →
      `REFERENCES_FILE` + `GENERATED_FROM` lineage for generated RTL;
      expectations modest — codegen patterns, not Perl semantics — **`PerlParser`
      is a line/regex scan: parenthesized `open()` of an HDL path → REFERENCES_FILE
      (read/write); a `module`…`endmodule` body flags the script a generator, and
      each written HDL file → GENERATED_FROM (reusing the flow-script
      `_resolve_file_ref`, now handling the reversed generated→generator
      direction). See docs/extraction.md**
- [x] SLN (Cadence Perspec System Level Notation) portable stimulus:
      actions → `ACTION` nodes; `>`-invocations → `INVOKES` (same-file action) and
      `TEST_COVERS` (design module/instance) — **the real format is the `e`/Specman
      dialect (not the PSS-like guess), so `SlnParser` scans `action`/`>sub_action`/
      constraints best-effort; `INVOKES` is a new additive `EdgeKind`. Accellera PSS
      (`.pss`) remains the natural open-sibling follow-on. See docs/extraction.md**
- [x] `.sln` disambiguation: content-sniff the Visual Studio solution header and
      skip non-SLN files (`skipped_reason="visual_studio_solution"`)
- [ ] Fixtures: an SDC and a UPF for the counter fixtures, a flow `.tcl`, a Perl
      heredoc codegen script, a minimal SLN scenario

**Acceptance:** an SDC on the two-clock M5 fixture upgrades both clock domains to
confidence 1.0 and the declared false path suppresses the CDC suspect; the UPF
fixture yields a power-domain report listing the domain and its isolated
instances; the Perl codegen fixture yields a `GENERATED_FROM` edge from its
emitted Verilog; the SLN scenario fixture links to the DUT module via
`TEST_COVERS`; a Visual Studio `.sln` is recognized and skipped.

---

## Risks

1. **SystemVerilog tree-sitter grammar quality — the #1 risk.** The original
   `tree-sitter/tree-sitter-verilog` grammar covers IEEE 1800 poorly (classes,
   constraints, assertions, and many SV-2017 constructs produce ERROR nodes). The
   actively maintained `gmlarumbe/tree-sitter-systemverilog` (used by Emacs
   `verilog-ts-mode`, validated against the sv-tests suite) is the strong
   candidate and can serve both `.v` and `.sv`. Mitigation: the grammar choice is
   isolated behind `parser/base.py`, so swapping costs one module; the first M1
   task is a grammar bake-off against the fixture corpus.
2. **VHDL grammar:** `alemuller/tree-sitter-vhdl` is unmaintained;
   `jpt13653903/tree-sitter-vhdl` is the maintained option. Case-insensitivity and
   library/work scoping must be handled in *our* layer, not the grammar.
3. **The preprocessor is the hard problem.** tree-sitter cannot expand macros;
   heavily `` `ifdef ``'d code parses to garbage, and macro-defined module bodies
   are invisible without expansion. M2's preprocessor with line mapping is the
   difference between a toy and real-world usability — budget it generously.
4. **No elaboration in the tree-sitter tier:** parameterized generates,
   `defparam`, configurations, and instance arrays mean instance counts/bindings
   are approximations until M7. Confidence scoring is the honest contract with
   users — document it prominently.
5. **py-tree-sitter API churn:** the 0.21→0.23 transition broke the `Language`
   constructor and query APIs. Pin the floor at the version actually coded
   against; CI on 3.13 catches wheel-availability gaps.
6. **PyPI name:** verify `hdl-kgraph` is unclaimed and claim it early with a
   placeholder release.
7. **Real-world inputs:** encrypted IP (`` `pragma protect ``), megabyte-scale
   generated netlists, and vendor primitive libraries can hang naive parsers —
   file-size guards and exclude globs ship in M1.
8. **Tcl and Perl are full programming languages** — static extraction is
   best-effort by design. SDC and UPF are constrained Tcl subsets and tractable;
   arbitrary flow scripts (loops, procs, `eval`) are out of scope, with only
   literal `set` substitution attempted. Confidence scoring and
   `REFERENCES_FILE` attrs are the honest contract.
9. **`.sln` name collision and proprietary syntax:** `.sln` is overwhelmingly
   Visual Studio solution files in the wild — content sniffing is mandatory
   before parsing. Perspec SLN has no public grammar; the parser targets the
   documented subset, with Accellera PSS (`.pss`, openly specified) as the
   safer long-term sibling target.
