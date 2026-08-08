# Scalability: reads & writes on a 10–100+ GB graph

A large design's knowledge graph can reach tens or hundreds of GB. This page
describes how reads and incremental writes stay usable at that size, and where
the remaining ceilings are.

## The problem

`SqliteStore.load()` rebuilds the **entire** graph as a `networkx.MultiDiGraph`.
That is the right tool for a full re-link, an export, or visualization, but it
makes every query pay for the whole design — and NetworkX's in-memory form is
several times the on-disk size, so a 10 GB graph needs tens of GB of RAM and a
100 GB graph does not load at all. Before this work the MCP server loaded the
whole graph and reloaded it after every `update`, so the first AI-assistant
query after any rebuild waited on a multi-minute (eventually impossible) load.

## Reads: bounded, index-backed queries

`hdl_kgraph/storage/query.py` (`GraphQuery`) answers each MCP/CLI query by
hydrating only the **bounded subgraph** the query touches, selected through the
existing SQLite indices, then running the *same* `graph/analysis.py` function on
that small graph — so results are byte-identical to the full-graph path
(`tests/test_query.py` sweeps every name in the fixture corpus to pin this).

| Tool | How it stays bounded |
|---|---|
| `find_module`, `search_nodes` | indexed `nodes` lookup by `kind`/`name` (GLOB) / `file` |
| `who_instantiates` | name → ids, then `edges WHERE dst IN (…) AND kind='instantiates'` |
| `port_map` | unit → `DECLARES` children (+ instance `CONNECTS`), one indexed hop |
| `get_hierarchy(top)` | BFS over `DECLARES`/`INSTANTIATES`/`IMPLEMENTS`, capped by depth/nodes |
| `get_hierarchy()` (tops) | pure SQL set-difference: no incoming `INSTANTIATES` |
| `impact_of_change` | hydrate only the reverse-dependency closure `impact_radius` walks |
| `find_signal_drivers` | signals by name, then only their `DRIVES` *or* `READS` edges |
| `unresolved_stubs` | unresolved-`nodes` scan + only those stubs' referrer edges |
| `modules` | indexed `MODULE`/`ENTITY` scan + each unit's incoming `INSTANTIATES` count |

Each call opens a fresh read connection, so a concurrent `update`/`watch` swap
is always observed — no cache, no staleness window.

As of v2.2.0 **every** CLI `query` subcommand is answered through this bounded
path — `instances-of`, `modules`, `drivers`, `unresolved`, and the whole-design
reports (`clock-domains`/`cdc`/`uvm`/`reset-tree`, below) — so no `query` command
ever calls `SqliteStore.load()`. (The routing landed incrementally: the reports in
v2.0.0, `instances-of`/`drivers`/`unresolved` in v2.1.0, `modules`/`reset-tree` in
v2.2.0.)

A localized query is 1000–16000× faster than the old per-call load and its
latency tracks the *answer* size, not the design size (see
[benchmarks.md](benchmarks.md)). A query whose answer *is* the whole design
(`search_nodes("*")`, a top that contains everything) is still O(design) — that
is intrinsic, not a regression.

## Whole-design summaries: precomputed, not re-scanned

Clock-domain/CDC, reset-tree, and UVM-topology reports scan global relations
(`CLOCKED_BY`/`RESETS`/`DRIVES`/`READS`/`EXTENDS`), not a single query's local
neighbourhood. The build computes the clock/CDC and UVM summaries once — while the
graph is already in memory — and persists them to the `summaries` table
(`graph/summary.py`); the MCP tools **and the CLI `query clock-domains`/`cdc`/`uvm`
commands** read that small JSON blob through `GraphQuery` in well under a
millisecond at any design size (since v2.0.0 the CLI no longer full-loads the graph
for these reports). The build computes them on a full `build` and refreshes them on
`update`. When the persisted blob is absent — and always for `reset-tree`, which is
not persisted — the reader recomputes out-of-core (below), never via
`SqliteStore.load()`.

When the persisted summary is **absent** — a database older than schema v8 (no
summaries table), or any build that did not persist it — the reader falls back to
recomputing the report, and both summary families now do so **out-of-core**
(`storage/summaries.py`), byte-identically to the NetworkX path
(`tests/test_summaries_sql.py` pins the parity):

- **Clock domains / CDC** (`clock_summary_sql`): computed straight from SQLite — the
  net-alias union-find reduces to connected components over the derived dataflow edges,
  reusing `clocks._UnionFind` over a SQL-derived pair list — without ever materializing
  the graph (validated on a real design in [v2/m12_real_design.md](v2/m12_real_design.md)).
- **Reset tree** (`reset_summary_sql`): `RESETS` edges grouped by canonical reset net via
  the *same* net-alias union-find as the clock report — bounded by the `RESETS` edges plus
  the alias pairs. Computed this way on every call (there is no persisted reset summary);
  the CLI resolves the reset processes' qualified names with a bounded id lookup.
- **UVM topology** (`uvm_summary_sql`): hydrates only the bounded *class* subgraph (CLASS
  nodes plus `EXTENDS`/`TEST_COVERS` edges) and runs the same `graph/uvm.py` functions on
  it — the report only ever touches the class inheritance graph, not the whole design.

## Writes

`update` writes only the changed rows, and (since the incremental-link path)
*reads* only the changed rows too. `save_incremental` UPSERT/DELETEs just the
delta; when the pipeline links incrementally it passes the dirty files and the
re-resolved clean-ref ids, and `_apply_delta` scopes the diff to exactly those —
reading only the touched files' nodes/edges (plus fileless stub nodes) through
`idx_nodes_file`/`idx_edges_src`, never the whole tables. So a one-file edit
pays a write *and read* cost proportional to the change. `bench_incremental.py`
guards both: on the 2 000-file corpus a one-leaf edit scans **~0.04 %** of the
nodes to diff (down from 100 %).

`link_incremental` (#64) guarantees this is safe: the only rows that can differ
from the stored build are those owned by a touched/removed file, fileless nodes
it may add/drop, and the `affected_srcs` clean refs it re-resolved; every other
row is byte-identical. The byte-identical-rebuild fuzz suite
(`tests/test_incremental_equivalence.py`) pins that a scoped `update` loads back
identical to a full `build`. When the link falls back to a full re-link (VHDL,
binds, enrich), the scope sets are omitted and `_apply_delta` diffs the whole
tables as before — correct for any graph.

### Remaining ceiling (known)

Parts of the `update` *pipeline* were O(design) in memory:

1. the incremental linker loaded the full prior graph (`SqliteStore.load()`) to
   re-resolve the dirty closure — **addressed: bounded re-link is the default
   since v1.13.0 (`--no-bounded-link` opts out), below**;
2. `update` decoded *every* clean unit's stored IR (`pipeline._reuse_unit`), not
   just the dirty/affected ones — **addressed: selective IR decode is the default
   since v1.14.0, below**;
3. the precomputed summaries are recomputed over the whole graph each update —
   the bounded path refreshes them out-of-core via the M12.5 SQL scans instead.

The delta *diff* is bounded (above); item (1) — re-resolving the dirty closure
without holding the entire prior graph — was the dominant remaining work for true
100 GB incremental `update`. With items (1) and (2) both addressed on the default
path, the whole `update` pipeline — reads, summaries, linker re-resolution, and IR
decode — is now bounded.

**Bounded incremental re-link — the default since v1.13.0.**
`graph/bounded_link.py` re-resolves the dirty closure **without
`SqliteStore.load()`**: the *unchanged* `_Linker._resolve` is fed lazy SQL-backed
indexes (`idx_nodes_kind_name`/`idx_edges_*`), `_gc_orphan_stubs` runs over just
the stub neighbourhood, the result is written as the existing scoped delta
(`_apply_delta_scoped`), and the whole-design summaries + report counts are read
back from the DB (M12.5 SQL scans). It is **byte-identical** to a full `build` —
`tests/test_incremental_equivalence.py` is parametrized over both link paths
(in-memory and bounded), including the randomized fuzz. On the real RV32I SoC a
single-file edit re-resolves ~1.9 k rows vs a 14 k-row full load; `hdl-kgraph
bench-link` reports the per-design locality (a median edit re-resolves ~0.4 % of
refs there). The dev spike (`scripts/spike_m13_link.py`,
[v2/m13_link_spike.md](v2/m13_link_spike.md)) proved the kernels first.
`hdl-kgraph update` now takes this path by default; `--no-bounded-link` falls back
to the in-memory re-link. So item (1) is bounded on the default path. Scope is
the SV incremental path (`incremental_link_safe`); VHDL / binds / enrich fall back
to a full re-link, flag or not.

**Selective IR decode — the default since v1.14.0 (item (2)).** The bounded path
no longer decodes every clean unit's stored IR. `run_update` loads only the small
`macro_events`/`included` columns for clean units (`SqliteStore.load_macro_events`,
**not** the big `ir` blob); the compile-order loop *replays each clean unit's macros*
into the shared `MacroTable` — the prerequisite for dirty re-parses to see earlier
`` `define``s — but skips `ir_from_json`. Only the dirty units (parsed fresh) and
the *affected* clean units the bounded linker re-resolves have their full IR decoded
(`SqliteStore.load_units_for`, fetched on demand in compile order). The affected set
is bounded by the dirty closure, so the resident IR set is O(closure), not O(design),
and `link_incremental_bounded` reads `node_file`/`ref_records` only for the live refs'
srcs — byte-identical to the full-decode path for every key actually read. Clean units'
preprocessor warnings and parse-error telemetry are carried forward from the preserved
`files` rows (`load_file_warnings`/`load_file_errors`) rather than re-derived. Bind/
configuration directives need every unit's IR for a full re-link, so that case raises
`_SelectiveLinkUnavailable` and transparently retries with the legacy full-decode path;
`--no-bounded-link`, VHDL, and enrich keep the full-decode flow. The decode-count is
pinned by `tests/test_bounded_link.py` and the byte-identical gate by
`tests/test_incremental_equivalence.py` (both link paths, incl. fuzz). With items (1)
and (2) both bounded on the default path, the v2 RAM goal is met without a Rust core.

**Why it had to land as one architecture (not a cheap slice).** You cannot simply
load a lighter prior graph (e.g. nodes + structural edges, dropping the dataflow-edge
bulk). Several steps read the *whole* graph and are entangled:

- `_gc_orphan_stubs` (`graph/builder.py`) keeps an unresolved stub alive iff it
  has **any non-`DECLARES` edge of any kind** — including `DRIVES`/`READS`/
  `CLOCKED_BY`/… So dropping the dataflow edges would make a stub anchored only
  by a clean dataflow edge look orphaned and get deleted — a non-byte-identical,
  corrupt result.
- `derive_test_covers` (`graph/uvm.py`) is a whole-design, cross-file relation
  (a `tb_*` top / `uvm_test` class covers DUTs anywhere), so the src-scoped delta
  write cannot keep it consistent. Since v1.15.0 the incremental paths re-derive
  the **whole** TEST_COVERS set out-of-core after the scoped write
  (`storage/summaries.py:test_covers_sql` hydrates only the structural subgraph —
  MODULE/ENTITY/INSTANCE/CLASS + DECLARES/INSTANTIATES/EXTENDS, never the dataflow
  bulk — and runs the same `derive_test_covers`), then reconcile it
  (`SqliteStore.replace_test_covers`). Byte-identical, bounded by the structural
  subgraph.
- the definitions/`children` seeding and `report.edge_count` read all
  nodes/edges.

So the memory-bounded linker landed as one architecture — SQL-backed name
resolution (`idx_nodes_kind_name`), SQL-aware stub-GC (over only the stub
neighbourhood), out-of-core summaries + counts, selective IR decode, an
out-of-core TEST_COVERS re-derivation, and a delta-only output (which the existing
`_apply_delta_scoped` consumes) — all gated by the byte-identical fuzz suite. It
shipped incrementally (opt-in `--bounded-link` in v1.12.0, default in v1.13.0,
selective IR decode in v1.14.0, bounded TEST_COVERS in v1.15.0); reads and the
write *diff* were already bounded before it.
