# M11 — v2.0 profiling & decision gate

> Milestone **M11** of the v2.0 re-architecture epic ([#128]). M11 is a *decision
> gate*: profile the v1 architecture at scale, pin the dominant cost, and select the
> M12 path. **No Rust is written in M11** — this is measurement + a decision.
>
> **Verdict (TL;DR):** the binding constraint is **peak RAM from materialising the
> whole NetworkX graph in `SqliteStore.load()`**. The in-memory graph is **~2.3× the
> on-disk DB**, so load RSS crosses 16 GB at only a **~7 GB on-disk design** and a
> 100 GB design needs **~225 GB RAM** — it "does not load," exactly as
> [docs/scalability.md](../scalability.md) diagnosed, now quantified. `load()` is
> **graph-CPU-bound (85–90 %)**, not SQLite-I/O-bound (10–15 %); `json.loads` of
> `attrs` is a minor 11–15 %. **→ Primary M12 path: an out-of-core graph layer that
> never materialises the whole graph (evaluate `kuzu`; SQL-native whole-design scans
> as the lower-risk partial). Runner-up: a `rustworkx`/compact in-memory core** — it
> lowers the per-node constant and attacks the graph-CPU term, buying headroom for the
> ~10 GB regime, but stays in-memory and so cannot reach 100 GB alone.

[#128]: https://github.com/chuanseng-ng/hdl-kgraph/issues/128

## Method

`scripts/profile_v2.py` (the M11 harness, landed in #140) drives the synthetic-design
generator (`scripts/gen_corpus.py`) across a file-count sweep and profiles three cost
centres — `build`, the whole-design `summaries`, and `SqliteStore.load()` — for both
**time and memory**. Highlights (full rationale in the script docstring):

- **Peak RSS per stage in an isolated child process.** `resource.getrusage` reports a
  per-process high-water mark, so each stage runs in its own process; the headline
  `load()` RSS comes from the *real* streaming loader (separate from the row-list
  timing stage and the tracemalloc stage, so neither perturbs the ceiling number).
- **Two splits.** *CPU-vs-memory:* wall-clock and true CPU (`getrusage`, incl. the
  parse-pool children) beside peak RSS. *SQLite-I/O-vs-graph-CPU:* `load()` decomposed
  into row fetch vs graph construction vs `json.loads`, plus a 3-way memory
  decomposition (fetch / +graph / +json-attrs) cross-checked against `tracemalloc`.
- **Sweep + fit + extrapolate.** Normalise to bytes/node and time/node, least-squares
  fit (with R²), and extrapolate via measured DB-bytes/node toward the 10–100 GB
  regime, reporting the RAM-ceiling crossing points.
- **Two corpora.** `generate` (resolution-light) and `generate_dense`
  (resolution-heavy: package imports, wide ports, cross-leaf chaining → ~3.1 edges/node
  vs ~2.3). The decision uses the **conservative (dense)** curve.
- **Stdlib only** (`resource` / `tracemalloc` / `/proc`); no new runtime deps; Linux
  for the RSS path.

**Machine:** Linux container, **15 GiB RAM, 4 vCPU**, Python 3.11.15. Wall-clocks run
~2× the [docs/benchmarks.md](../benchmarks.md) container (e.g. 2 000-file build 3.85 s
here vs 1.94 s there) — a slower box — but graph node/edge counts are **identical**, so
the harness is faithful and the *scale-invariant* conclusions (bytes/node, the splits,
the in-memory:on-disk ratio) are machine-independent.

Reproduce:

```bash
python scripts/profile_v2.py --files-sweep 2000,10000,50000 --repeat 3 --json-out light.json
python scripts/profile_v2.py --files-sweep 2000,10000,20000 --dense --repeat 3 --json-out dense.json
```

## Results

### Resolution-light corpus

| files | nodes | edges | DB | build wall / cpu | build peak | load wall / cpu | **load peak RSS** | summaries |
|---|---|---|---|---|---|---|---|---|
| 2 000 | 14 086 | 32 341 | 22.5 MiB | 3.85 / 3.73 s | 115 MiB | 0.38 / 0.38 s | 91 MiB | 0.28 s |
| 10 000 | 70 468 | 161 911 | 112.8 MiB | 18.75 / 17.94 s | 395 MiB | 2.66 / 2.67 s | 291 MiB | 1.59 s |
| 50 000 | 352 356 | 809 591 | 564.8 MiB | 109.9 / 135.5 s | 1 957 MiB | 14.15 / 14.21 s | 1 311 MiB | 7.49 s |

### Resolution-heavy (dense) corpus — the conservative curve

| files | nodes | edges | DB | build wall / cpu | build peak | load wall / cpu | **load peak RSS** | summaries |
|---|---|---|---|---|---|---|---|---|
| 2 000 | 57 830 | 178 440 | 111.1 MiB | 12.9 / 16.4 s | 412 MiB | 2.68 / 2.69 s | 296 MiB | 1.56 s |
| 10 000 | 289 450 | 893 521 | 557.3 MiB | 68.8 / 85.5 s | 1 898 MiB | 16.18 / 16.25 s | 1 313 MiB | 6.18 s |
| 20 000 | 578 970 | 1 787 197 | 1 115.9 MiB | 138.2 / 173.4 s | 3 757 MiB | 32.05 / 32.20 s | 2 586 MiB | 15.05 s |

## The two splits

### CPU-vs-memory — *CPU-bound per call, but memory is what runs out first*

`load()` wall ≈ CPU in every row (e.g. light 50k: 14.15 s wall / 14.21 s cpu; dense
20k: 32.05 / 32.20) → it is **CPU-bound, not I/O-wait**. But that is not the constraint
that ends scaling. The **in-memory graph is ~2.3× the on-disk DB** (load RSS/DB-bytes:
3 784/1 677 = 2.26× light, 4 608/2 018 = 2.28× dense), so a design *stops fitting* in
RAM long before per-call latency alone would gate it. **Memory is the binding
constraint.**

### SQLite-I/O-vs-graph-CPU within `load()` — *graph construction dominates*

| corpus @ scale | fetch (SQLite I/O) | graph-CPU | — of which `json.loads` |
|---|---|---|---|
| light 50k | 10 % | **90 %** | 11 % |
| dense 20k | 10 % | **90 %** | 11 % |

`load()` is overwhelmingly **graph-CPU** (NetworkX node/edge insertion into the
dict-of-dicts `MultiDiGraph`), and the fetch share *shrinks* with scale. Memory mirrors
this: the **+graph** term dwarfs **+json-attrs** (dense 20k: +1 811 MiB graph vs +735
MiB attrs; light 50k: +962 vs +307), and `tracemalloc` attributes the bulk to NetworkX
frames (1 021 MiB at dense 20k). `json.loads` and SQLite fetch are both minor.

## Scaling curves & the "does not load" wall

Least-squares fits (per the harness; the load curves are the decision input):

| | light | dense (conservative) |
|---|---|---|
| load RSS slope | 3 784 B/node (R²=1.0000) | **4 608 B/node** (R²=1.0000) |
| load time slope | 40.7 µs/node (R²=1.0000) | 56.3 µs/node (R²=0.9997) |
| DB on disk | 1 677 B/node | 2 018 B/node |

Extrapolation on the **conservative dense curve** (via measured DB-bytes/node):

| on-disk DB | nodes | projected load RSS | projected load time |
|---|---|---|---|
| 10 GB | ~5.3 M | **~22.9 GB** | ~5 min |
| 100 GB | ~53 M | **~228 GB** | ~50 min |

**RAM-ceiling crossings (dense):** 16 GB at **~3.7 M nodes (~7 GB DB)** · 32 GB at
~7.4 M (~14 GB DB) · 64 GB at ~14.9 M (~28 GB DB). The light curve agrees within ~20 %
(16 GB at ~4.5 M nodes / ~7 GB DB).

> **Caveat (extrapolation honesty).** The 100 GB point is ~1 order of magnitude beyond
> the largest measured design (dense 20k = 0.58 M nodes / 1.1 GB DB; the 30k-dense run
> OOM-killed the tracemalloc stage on this 15 GB box — itself confirming the wall). R²
> is ≈1.0 across the measured range and the in-memory:on-disk ratio is stable across
> both corpora, so the *linear* projection is well-founded; treat the absolute 100 GB
> numbers as directional, the **shape** and the **crossing points** as solid.

## Decision — the M12 path

Applying the pre-stated criteria (epic §, harness docstring) to the measured dominant
cost:

1. **Binding constraint = peak RSS from materialising the whole graph.** The ceiling is
   hit at a ~7 GB on-disk design (16 GB box) and a 100 GB design needs ~225 GB — it
   does not load. Per the criteria, when RSS is the binding constraint the fix must
   **never materialise the whole graph**. A faster *in-memory* engine does not qualify
   on its own.

2. **`json.loads` (11–15 %) and SQLite fetch (10–15 %) are not the bottleneck** → an
   attrs-codec change and read-pattern/index tuning are *not* M12 levers (note them, do
   not gate on them).

3. **The dominant cost — NetworkX graph construction & footprint — is precisely what a
   compact/out-of-core representation attacks.**

### → Primary M12 path: out-of-core graph layer (off-the-shelf first)

Evaluate, behind the existing `storage`/`GraphQuery` seam:

- **`kuzu`** — embedded, on-disk, columnar graph DB with out-of-core query; the
  candidate to answer whole-design analytics at 100 GB **without** a resident
  `MultiDiGraph`, with far less custom code than a bespoke core.
- **SQL-native whole-design scans** — push clock/CDC, UVM `derive_test_covers`, and
  metrics into SQLite (recursive CTEs / indexed edge scans) so `load()` is never built
  for them. Lower-risk partial relief, zero new dependency; a good first slice and a
  fallback if `kuzu` doesn't fit the access patterns.

This is the only class of option that turns "100 GB does not load" into "loads."

### Runner-up: a `rustworkx` / compact in-memory core

A Rust CSR/arena representation replaces the dict-of-dicts and directly attacks the
dominant **graph-CPU** term and the per-node footprint — plausibly a ~3–8× constant-factor
win, pushing the RAM ceiling from ~7 GB to perhaps ~30–50 GB of on-disk design and
cutting load time. **But it is still in-memory**, so it does *not* reach the 100 GB
regime by itself. It is the high-value choice for the **10 GB regime** and complements
(does not replace) the out-of-core path — consistent with the epic's M12 (evaluate
`rustworkx` vs `kuzu`/SQL-native) → M13 (bespoke PyO3 core if needed) staging.

### What M11 settles for M12

- Spend M12's risk budget on the **out-of-core** evaluation (`kuzu` / SQL-native) —
  that is the lever for the documented wall.
- Treat `rustworkx` as the **in-memory CPU/RAM-constant** track for sub-ceiling designs,
  not as the 100 GB answer.
- Do **not** prioritise an `attrs` codec change or read-path index work for the load
  ceiling — the data shows neither is the bottleneck.

## Self-consistency / trust checks

- **Baseline parity:** every node/edge count matches the merged build path (e.g.
  2 000-file light = 14 086 nodes / 32 341 edges, the `docs/benchmarks.md` M5 corpus).
- **Split residual** (I/O + graph-CPU vs total `load()`): ≤ 90 ms across all points
  (≤ 0.6 %).
- **RSS sampling agreement:** subprocess `ru_maxrss` == live-sampled peak in every row.
- **tracemalloc vs RSS:** the NetworkX tracemalloc term tracks the +graph RSS delta as
  a lower bound (e.g. light 50k: tracemalloc nx 568 MiB under +graph 962 MiB; the gap is
  C-level/allocator overhead tracemalloc cannot see — itself a finding that the real
  footprint exceeds the Python-heap estimate).
- **Fit quality:** R² ≈ 1.0 on the measured range for both corpora; bytes/node is
  stable (mild super-linear *improvement* with scale as the fixed interpreter baseline
  amortises), so the linear extrapolation is conservative if anything.
