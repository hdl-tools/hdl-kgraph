# M13 — memory-bounded incremental linker: feasibility spike + locality metric

**Status:** feasibility proven (dev spike) + a shipped locality metric (`hdl-kgraph bench-link`).
The bespoke Rust core (original M13) stays deferred; this is the SQL-bounded path for the one
remaining O(design)-RAM cost — the `update` **write** path (#119).

## The problem (recap)

Reads (v1) and both whole-design summaries (M12.5: #147/#148) are out-of-core. The last
O(design)-RAM cost is `update`: `pipeline._link_pass2` calls `SqliteStore.load()` to materialise
the **entire** prior graph so `builder.link_incremental` can re-resolve the dirty closure.
`docs/scalability.md` calls this "all-or-nothing" — the in-memory graph is consumed by entangled
passes: name resolution (`_Linker.definitions`/`children` seeded from every prior node/edge),
`_gc_orphan_stubs` (keeps a stub iff it has **any** non-`DECLARES` edge), `derive_test_covers`
(whole-graph scan), and the report counts.

## Spike: the prior graph need never be materialised

`scripts/spike_m13_link.py` proves the two genuinely-doubted kernels run **bounded by the dirty
closure** with **byte-identical** results, without `SqliteStore.load()`:

- **Resolution kernel.** A lazy SQL-backed `_Linker` reuses the **unchanged** `_resolve` (parity by
  construction) but feeds its `definitions`/`definitions_ci`/`children`/`parent`/`node_obj` indexes
  from `idx_nodes_kind_name` / `idx_edges_src|dst` on demand — so it reads only the names and scopes
  the *live* refs (dirty units + `affected_srcs`) touch. Dirty-file nodes are excluded at the SQL
  level (mirroring `link_incremental` step 1, which removes dirty nodes + their edges).
- **Stub-GC kernel.** The bounded GC assembles only the *stub neighbourhood* (prior stubs + their
  surviving incident edges + the re-resolved delta) and runs the **real** `_gc_orphan_stubs` on it
  — same "any non-`DECLARES` edge" + `DECLARES`-hosting-chain rule, bounded by stub count, not the
  whole graph.

Method: a real `run_update` runs with `builder.link_incremental` monkeypatched to capture its exact
inputs (so the pipeline's discovery / dirty-closure / `affected_srcs` computation is reused
verbatim) and the oracle result; the bounded kernels then run over a snapshot of the *prior* DB and
are compared.

### Result — byte-identical, bounded

| case | resolve | stub-GC | live srcs | edges | stubs | bounded rows | full load |
|---|---|---|---|---|---|---|---|
| `rename_instance` | ✅ | ✅ | 1 | 5 | 0 | 16 | 50 |
| `add_module` | ✅ | ✅ | 4 | 13 | 0 | 26 | 50 |
| `edit_header` | ✅ | ✅ | 2 | 7 | 0 | 20 | 50 |
| `resolve_stub` | ✅ | ✅ | 1 | 3 | 0 | 22 | 57 |
| **real SoC** (RV32I) | ✅ | ✅ | 14 | 96 | **189** | **1 886** | **14 126** |

Re-resolution is byte-identical to the in-memory `link_incremental` on every
`tests/test_incremental_equivalence.py` edit shape and on the real SoC (`chuanseng-ng/
claude_verilog_test`, 3 774 nodes / 10 352 edges, 189 unresolved stubs). The bounded read set
scales with the **edit**, not the design (~7.5× fewer rows than a full load on the SoC, and the gap
widens with design size). Run it on any built design with `--design <root>`.

## Shipped metric: `hdl-kgraph bench-link`

The spike lives in `scripts/` (not in the installed wheel), so it can't be run post-install. The
shipped, **content-free** `bench-link` command (`src/hdl_kgraph/linkbench.py`) quantifies the same
locality from a built `graph.db` alone — the persisted `ref_index` + include/macro dependency
graph, no source tree, no second resolver. It reports, across single-file edits, the distribution
of refs an incremental re-link re-resolves vs a full re-link (the exact `affected_clean_refs` +
`dirty_closure` rule):

```text
$ hdl-kgraph bench-link --json        # on the RV32I SoC
{ "totals": {"files": 54, "refs": 5462, "nodes": 3774, "edges": 10352},
  "reresolved_refs": {"p50": 23, "p90": 85, "max": 116, "mean": 33.8},
  "locality_ratio":  {"p50": 0.0042, "p90": 0.0156, "max": 0.0212} }
```

A median single-file edit re-resolves **0.42 %** of the design's refs (max 2.1 %) — the concrete
per-design payoff a memory-bounded linker would realise. Output is numbers only (a content-free
test pins it).

## Productionisation requirements surfaced (the next slice)

Porting the spike into the default `update` path needs, as one architecture (gated by the
byte-identical fuzz suite):

1. **Lazy SQL-backed resolution indexes** in `src/` (the spike's `_Lazy*`), with `node_file` for
   affected clean srcs sourced from `ref_index` (`RefRecord.file`) so clean IRs need not be decoded
   (selective `_reuse_unit`).
2. **Bounded `_gc_orphan_stubs`** over the stub neighbourhood (proven here) — note its candidate
   universe is the stub set; only closure-incident stubs can flip, the rest survive unchanged.
3. **Incremental `derive_test_covers`** — **done (v1.15.0).** TEST_COVERS is cross-file, so the
   src-scoped delta write can't keep it consistent; both incremental paths now re-derive the whole
   set out-of-core after the scoped write (`summaries.test_covers_sql` hydrates only the structural
   subgraph — MODULE/ENTITY/INSTANCE/CLASS + DECLARES/INSTANTIATES/EXTENDS — and runs the same
   `derive_test_covers`), then reconcile via `SqliteStore.replace_test_covers`. Bounded by the
   structural subgraph; byte-identical. A tb-top/uvm-test index could later make it closure-scoped.
4. **Incremental summary refresh** — now free via M12.5's `clock_summary_sql` / `uvm_summary_sql`,
   computed from the written DB instead of the in-memory graph.
5. **Report counts** without `number_of_nodes/edges()` on a full graph — from `_apply_delta_scoped`
   write stats + `SELECT COUNT(*)`.

Recommended rollout: land the bounded linker behind an opt-in `--bounded-link`, parametrise
`tests/test_incremental_equivalence.py` over both paths, then flip the default and retire the
in-memory path; add a read-locality gate to `scripts/bench_incremental.py`. At that point
`bench-link` measures the real shipped path rather than the index-derived estimate.

## Caveats

The real design is small (3.7 k nodes): the spike validates **parity + the bounded access pattern**
and surfaces the productionisation requirements; 100 GB RAM remains the M11/M12 extrapolation
(bounded-vs-linear *shape*, not absolute numbers). `bench-link` reads the `ref_index` (O(refs)) to
build the distribution — bounded by refs, not nodes+edges. Scope is the SV incremental path
`link_incremental` already supports (VHDL / binds / enrich fall back to a full re-link).
