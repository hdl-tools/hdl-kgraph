# M12 — v2.0 graph-layer spike & decision

> Milestone **M12** of the v2.0 epic ([#128]), following M11
> ([docs/v2/m11_profiling.md](m11_profiling.md)). M11 pinned the wall: a whole-design
> scan over the materialised NetworkX graph costs **~2.3× the on-disk DB in RAM**, so a
> 100 GB design needs ~225 GB and "does not load." M12 asks #128's gate question:
> *does an off-the-shelf layer hit the RAM target before we commit to a bespoke Rust core?*
>
> **Verdict (TL;DR): yes — an off-the-shelf out-of-core layer clears the wall.** Both
> **SQL-native scans** (zero new dep) and **`kuzu`** (embedded graph DB) answer a
> whole-design scan in **bounded RAM** that does *not* grow with design size (~50 MiB and
> ~110 MiB flat, vs NetworkX's 4610 B/node linear). `rustworkx` lowers the per-node
> constant (~29 %) but stays **linear**, so it pushes the ceiling out without reaching
> 100 GB. **→ A bespoke Rust core (M13) is NOT required to clear the RAM ceiling for
> whole-design analytics.** Primary: **SQL-native** for CTE-expressible scans, **`kuzu`**
> where richer graph queries (variable-length traversal) are wanted; **`rustworkx`** is
> the in-memory runner-up for the ~10 GB regime.

[#128]: https://github.com/chuanseng-ng/hdl-kgraph/issues/128

## Method

`scripts/spike_m12.py` runs one **representative whole-design scan** — a structural
summary (node-kind + edge-kind histograms + INSTANTIATES fan-in) that exercises the same
full node+edge access pattern as `load()` — on each backend, in an isolated subprocess
(clean peak RSS, per the M11 harness), then sweeps the file count and fits RSS vs node
count. Every backend's scan is **parity-checked byte-for-byte** against the NetworkX path
(the oracle, via `graph/analysis.py`); a *flat* RSS slope ⇒ bounded ⇒ reaches 100 GB.

Backends: **networkx** (the M11 baseline — materialise then scan), **sql** (pure SQLite
aggregation against `graph.db`, no graph built), **rustworkx** (in-memory `PyDiGraph`;
*full* = NetworkX-equivalent payloads, the fair comparison; *flat* = kind-only, an upper
bound), **kuzu** (one-time SQLite→kuzu conversion, then Cypher aggregation over the
on-disk DB). Machine: Linux container, 15 GiB RAM, 4 vCPU, Python 3.11.15. Counts match
the merged build path exactly.

> Scan choice: the clock/CDC/UVM scans share this whole-graph access pattern but are
> *empty* on the combinational `gen_corpus` designs, so they can't measure RAM scaling
> here. The structural summary is the faithful, exactly-portable stand-in. Porting the
> semantically complex scans (e.g. `cdc_suspects`' union-find combinational bridge) is more
> work per backend — that effort is M13 / productionisation scope — but the **RAM thesis
> below is a property of the access pattern, not the specific scan.**

Reproduce:

```bash
pip install -e '.[spike]'   # rustworkx + kuzu (eval-only extra)
python scripts/spike_m12.py --files-sweep 2000,10000,50000 --repeat 3            # light
python scripts/spike_m12.py --files-sweep 2000,10000,20000 --dense --repeat 3    # dense
```

## Results

### Resolution-light corpus

| backend | scan @ 50k | peak RSS @ 50k (352k nodes) | RSS slope | verdict |
|---|---|---|---|---|
| networkx | 14.2 s | 1 314 MiB | 3 795 B/node (R²=1.0) | linear — the M11 wall |
| **sql** | 0.66 s | **52 MiB** | **6 B/node** | **BOUNDED (flat)** |
| rustworkx (fair) | 9.9 s | 878 MiB | 2 487 B/node (R²=1.0) | linear (~35 % under nx) |
| rustworkx_flat | 2.4 s | 212 MiB | 505 B/node | linear (~7.5× under nx) |
| **kuzu** | **0.14 s** | **115 MiB** | **46 B/node** | **BOUNDED (flat)** |

### Resolution-heavy (dense) corpus — the conservative curve

| backend | scan @ 20k | peak RSS @ 20k (579k nodes) | RSS slope | verdict |
|---|---|---|---|---|
| networkx | 32.9 s | 2 587 MiB | 4 610 B/node (R²=1.0) | linear — the wall |
| **sql** | 1.4 s | **52 MiB** | **4 B/node** | **BOUNDED (flat)** |
| rustworkx (fair) | 21.8 s | 1 861 MiB | 3 291 B/node (R²=1.0) | linear (~29 % under nx) |
| rustworkx_flat | 5.0 s | 362 MiB | 579 B/node | linear (~8× under nx) |
| **kuzu** | **0.17 s** | **116 MiB** | **26 B/node** | **BOUNDED (flat)** |

## What the numbers say

- **SQL-native and kuzu are bounded.** RSS is essentially constant across an 8× node-count
  range (sql ~50→52 MiB; kuzu ~100→116 MiB), so it does not grow with design size — the
  defining property that reaches 100 GB. They answer from disk and never materialise the
  graph. kuzu is also **~125–190× faster** than the NetworkX scan at the top of the sweep.
- **rustworkx is linear.** Even with NetworkX-equivalent payloads it only trims the
  constant ~29–35 % (the Python payload dicts dominate; the win is the Rust adjacency, not
  the payload). Flattening payloads (`rustworkx_flat`) reaches ~8× but is still linear and
  no longer stores the same data — that is really a columnar-storage change, closer to M13.

### Projection to a 100 GB design (conservative dense curve)

Using M11's ~2 018 on-disk-bytes/node, a 100 GB DB ≈ 53 M nodes:

| backend | projected peak RSS at 100 GB | reaches the target? |
|---|---|---|
| networkx | **~228 GB** | ✗ "does not load" (M11) |
| rustworkx (fair) | ~163 GB | ✗ |
| rustworkx_flat | ~29 GB | borderline; still O(design) |
| **sql** | **~0.3 GB** | ✓ bounded |
| **kuzu** | **~1.4 GB** | ✓ bounded |

## Decision — answering #128's M12 gate

**An off-the-shelf out-of-core layer hits the RAM target; a bespoke PyO3 Rust core (M13)
is not required to clear the whole-design-scan ceiling.** Recommended direction:

1. **SQL-native scans (primary, zero new dep).** For scans expressible as SQL aggregation
   / recursive CTEs (histograms, fan-in/out, reachability), push them into SQLite against
   the existing `graph.db`. Bounded RAM, no dependency, no second copy of the graph. The
   lowest-risk first slice — and it composes with the existing `GraphQuery` seam.
2. **`kuzu` (primary for graph-shaped scans).** Where a scan needs variable-length
   traversal or pattern matching that is awkward in SQL (the clock/CDC/UVM family),
   `kuzu`'s Cypher over an embedded out-of-core DB stays bounded and is dramatically
   faster. Cost: one heavyweight optional dependency, plus a SQLite→kuzu conversion and a
   second on-disk copy of the graph to maintain.
3. **`rustworkx` (runner-up, in-memory).** Best when a NetworkX-compatible in-memory graph
   is wanted and the design fits the ~10 GB regime; it cuts the per-node constant and the
   scan CPU but does **not** reach 100 GB alone.
4. **Defer M13 (bespoke Rust core)** unless a future scan needs something neither SQL nor
   kuzu expresses efficiently, or the in-memory regime needs more than rustworkx's
   constant-factor win. M12 shows the off-the-shelf path is sufficient for the documented
   wall, at far lower cost/risk than a dual-language core.

## Caveats

- The benchmark scan is the *structural-summary* representative; the RAM result is a
  property of the whole-graph access pattern, but **porting the semantically complex scans
  (union-find net-aliasing, the CDC combinational bridge) to SQL/kuzu is real work** and is
  where M13 (or a careful productionisation) earns its keep — not in the memory model.
- `kuzu` adds a maintained second representation (the converted DB) and a dependency;
  SQL-native adds neither but is less expressive. The recommendation is to use each where
  it fits rather than pick one universally.
- 100 GB projections are extrapolations from designs up to ~0.58 M nodes (15 GiB box); the
  bounded-vs-linear *shape* is solid, the absolute 100 GB figures are directional.

## Trust checks

- **Parity:** every backend's scan equals the NetworkX oracle byte-for-byte across light +
  dense at 3 scales each (kuzu preserves parallel edges, so counts match).
- **Counts** match the merged build path (e.g. 2 000-file light = 14 086 nodes / 32 341
  edges). Subprocess `ru_maxrss` == live-sampled peak; bounded backends measured in an
  isolated child so a lean parent doesn't leak RSS into the high-water mark.
- Fits: linear backends R²=1.0; bounded backends near-flat with a fixed base (sql ~50 MiB,
  kuzu ~100 MiB) — the low R² on the bounded fits reflects there being essentially no slope
  to fit, which is the point.
