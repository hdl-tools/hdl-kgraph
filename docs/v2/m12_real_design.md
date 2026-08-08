# M12 real-design validation — porting the clock/CDC scan to SQL + kuzu

> Follows [m12_graph_layer.md](m12_graph_layer.md). M12 proved on the *synthetic*
> `gen_corpus` that off-the-shelf out-of-core layers answer a whole-design scan in bounded
> RAM. But `gen_corpus` is purely combinational, so the **real** whole-design scans
> (`clock_domains` + `cdc_suspects` in `graph/clocks.py`) were *empty* on it — M12
> benchmarked a structural-summary stand-in. This page closes that gap: it ports the
> **real** scans — including the genuinely hard parts (`net_aliases` union-find /
> connected-components, and the CDC combinational bridge) — to SQL-native and kuzu, and
> checks **byte-identical parity** against `graph/clocks.py` on a real design.
>
> **Verdict:** the M12 conclusion **holds for the real scans — via SQL-native.** SQL
> reproduces `clock_domains` (real design) and `cdc_suspects` (fixture) **byte-identically
> and in bounded RAM**. kuzu matches `clock_domains` too, but its Cypher variable-length
> connected-components is **pathological (20.6 s / 2.5 GB on a 3.7 k-node design — worse
> than NetworkX)** because it matches *walks*, not a reachable set. **So for the clock/CDC
> family SQL-native is the clear primary; kuzu would need a WCC algorithm extension.** A
> bespoke Rust core (M13) is still not required to clear the RAM ceiling.

## Design under test

`chuanseng-ng/claude_verilog_test` — a SystemVerilog **RV32I RISC-V SoC** (pipelined CPU,
caches, GPU-lite, SoC integration). Built its `rtl/` with the stock tree-sitter heuristic
pipeline (`run_build`, no `--enrich`):

- 54 SV/V files, **0 parse errors**; **3 774 nodes / 10 352 edges**; 8.1 MiB DB.
- Edge kinds incl. **87 `clocked_by`**, 1 759 `drives`, 3 190 `reads`, 1 362 `connects`.
- `clock_domains` → **3 domains**, with `net_aliases` union-find **genuinely active** (it
  merged `clk`/`clk0`/`clk_i` across the hierarchy into one domain). **The hard
  connected-components logic is exercised.**
- `cdc_suspects` → **0** (the aliasing collapses the clocks into one effective domain, so
  there are no cross-domain crossings). The CDC *bridge* is therefore exercised separately
  on the repo's `tests/fixtures/two_clock_cdc.sv` (**1 suspect**).

> The design is *small* (3.7 k nodes), so this validates **parity/correctness of the real
> scans on real RTL** and surfaces backend behaviour — it does **not** re-measure RAM
> *scaling*, which remains the M11/M12 synthetic extrapolation.

## Method

`scripts/spike_m12_clocks.py` runs the scan on each backend in an isolated subprocess
(clean peak RSS), parity-checked against `graph/clocks.py` via `summary.clock_summary`:

- **networkx** — the oracle (materialise graph, run `clock_domains`/`cdc_suspects`).
- **sql** — pure SQLite: the `net_aliases` union-find reformulated as **transitive closure
  + `MIN(reachable id)`** (a recursive CTE — exact, since `_UnionFind` assigns each node
  its component's lex-min id), then domain/CDC aggregation. Computes **domains *and* cdc**.
- **kuzu** — projected attrs + a materialised `Alias` rel + **variable-length path + MIN**
  for components, then domain aggregation. Computes **domains** (the union-find core).

## Results

| design | backend | scope | parity | scan | peak RSS |
|---|---|---|---|---|---|
| real SoC (3 774 nodes) | networkx | domains+cdc | ✅ | 176 ms | 57 MiB |
| real SoC | **sql** | **domains+cdc** | **✅** | **131 ms** | **47 MiB** |
| real SoC | kuzu | domains | ✅ | **20 580 ms** | **2 546 MiB** |
| two-clock fixture (1 suspect) | networkx | domains+cdc | ✅ | 2 ms | 45 MiB |
| two-clock fixture | **sql** | **domains+cdc** | **✅** | 1 ms | 45 MiB |
| two-clock fixture | kuzu | domains | ✅ | 97 ms | 99 MiB |

## Findings

1. **SQL-native reproduces the real scans byte-identically and bounded.** `clock_domains`
   (incl. union-find net-aliasing) on the real SoC and `cdc_suspects` (incl. the
   combinational bridge) on the fixture both match the oracle exactly, at ~47 MiB / ~130 ms.
   The union-find → recursive-CTE reformulation is the key: SQLite's `UNION` recursive CTE
   computes the *reachable set* (deduped), so it stays cheap and bounded.

2. **kuzu's off-the-shelf connected-components is the wrong tool.** Cypher variable-length
   (`-[:Alias*1..k]->`) matches **walks**, not a reachable set, so on the cyclic/symmetric
   alias graph cost explodes with the depth bound: `*1..30` *hangs*; even `*1..10` costs
   **20.6 s and 2.5 GB RSS** on a 3.7 k-node design (worse than NetworkX). It is correct
   (parity ✅) but **not bounded**. A production kuzu port would need a real **weakly-
   connected-components algorithm** (kuzu's `algo` extension), not raw traversal. kuzu's CSV
   bulk-load also needed `expr_text` sanitisation (real RTL concatenations carry
   commas/newlines that break `COPY`) — extra friction vs SQLite's native JSON.

3. **CDC was not exercised on the real design** (0 suspects); its bridge is validated on the
   two-clock fixture (SQL ✅, 1 suspect). A *bounded* CDC port is additional work: the
   bridge's `sig_domain` propagation state is O(design), so a true out-of-core version must
   keep it in SQL temp tables (done in-process here for the parity proof) — a
   productionisation task, not a memory-model blocker.

## What this means for the v2 direction

- The M12 verdict **survives contact with real RTL**: an off-the-shelf out-of-core layer
  answers the real clock/CDC scans with byte-identical parity and bounded RAM — **provided
  it's SQL-native**. The union-find that looked like the hardest risk ports cleanly to a
  recursive CTE.
- **Sharpened recommendation:** for the clock/CDC/UVM scan family (which lean on
  connected-components / multi-hop traversal), **SQL-native is the primary**; **kuzu is
  only viable here with its WCC algorithm extension** (raw Cypher walks don't scale).
  rustworkx remains the in-memory option for the ~10 GB regime.
- **M13 (bespoke Rust core) is still not required** to clear the documented "100 GB does
  not load" wall. The remaining real work is productionising these scans on SQL-native
  (esp. a bounded CDC bridge) — an engineering task on the off-the-shelf path, not a new
  core.

## Reproduce

```bash
git clone https://github.com/chuanseng-ng/claude_verilog_test /tmp/cvt
python -m hdl_kgraph build /tmp/cvt/rtl            # or run_build(Path("/tmp/cvt/rtl"))
pip install -e '.[spike]'
python scripts/spike_m12_clocks.py --db /tmp/cvt/rtl/.hdl-kgraph/graph.db
# CDC bridge (non-empty) on the repo fixture:
python -m hdl_kgraph build tests/fixtures   # build two_clock_cdc.sv
python scripts/spike_m12_clocks.py --db tests/fixtures/.hdl-kgraph/graph.db
```
