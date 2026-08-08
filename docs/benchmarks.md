# Benchmarks

The first two sections measure the *tool* (how fast it builds and reads). The
last measures the *claim* — that routing design questions through the graph
costs an AI assistant fewer tokens than grepping the RTL, and answers correctly
where grep does not. That one ships as a command, `hdl-kgraph bench`, so a user
can check it against their own design instead of trusting a table here.

## M4 target: incremental update of 1 file in a 2k-file design < 1 s

Procedure (fully scripted):

```bash
pip install -e .
python scripts/bench_incremental.py --files 2000
```

`scripts/gen_corpus.py` generates a synthetic 2000-file SystemVerilog design
(a `top` → mid → leaf instantiation tree; ~10% of leaves include a shared
header, ~10% of mids import a shared package), `bench_incremental.py` times a
full `build`, edits one leaf module, and times the `update`. The script exits
non-zero when the target is missed.

### Recorded results

| version | machine | corpus | full build | update (1 leaf edited) | target |
|---|---|---|---|---|---|
| v0.4 (M4) | Linux container, Python 3.11 | 2000 files, 11 992 nodes, 18 364 edges | 1.36 s | **0.85 s** | < 1 s ✅ |
| v0.5 (M5) | Linux container, Python 3.11 | 2000 files, 14 086 nodes, 32 341 edges | 1.94 s | **1.29 s** | < 1.5 s ✅ |

**Budget bump to < 1.8 s (precomputed summaries).** The whole-design reports
(clock domains / CDC, UVM topology) cannot be answered from a bounded subgraph,
so the build computes them once — while the graph is in memory — and persists
them, letting the MCP `clock_domains`/`uvm_topology` tools read O(1) at any
design size (see [scalability.md](scalability.md)). That adds a fixed
whole-design pass to each build/update (~0.25 s on the M5 corpus), so
`bench_incremental.py` now defaults to `--target-s 1.8`.

**Why the M5 number is higher:** dataflow extraction grew the same corpus's
graph by ~76% more edges (DRIVES/READS/CLOCKED_BY/RESETS plus SIGNAL and
PROCESS nodes), and every edge is re-linked and re-saved on each update
(steps 2–4 below scale with graph size, not with the edit). The M4
acceptance (< 1 s) was met and recorded at M4; the M5 budget was < 1.5 s —
the threshold at which a `--no-dataflow` build flag or a partitioned
re-link (see the escape hatch) becomes worth its complexity. The precomputed
summaries (above) then raised the current budget to < 1.8 s, and
`bench_incremental.py` defaults to `--target-s 1.8` accordingly.

### Where the update time goes

An incremental `update` re-parses only the dirty files, but by design it
re-runs the global pass-2 link and rewrites the database transactionally
(correct by construction — no surgical node/edge deletion). The remaining
cost is therefore roughly:

1. re-hash every file to detect changes (one `discover` pass),
2. decode the stored pass-1 IR JSON for every clean unit,
3. global pass-2 link (`build_graph`),
4. transactional full rewrite of `graph.db`.

The change-detection prelude loads only the file-hash table and the
include/macro dependency subgraph from SQLite, never the full graph.

### Escape hatch if a larger design misses the target

The full-rewrite save and whole-graph re-link are the first things to
revisit: per-file `DELETE`/`INSERT` of nodes and edges plus a scoped re-link
of only the affected name partitions would cut steps 3–4 to near zero, at
the cost of real invalidation bookkeeping. Measure first — the benchmark
script accepts `--files N`.

## Read latency: bounded queries vs a full graph load

```bash
python scripts/bench_query.py --files 20000
```

`bench_query.py` builds a large synthetic design and times each MCP tool
through `GraphQuery` (`hdl_kgraph/storage/query.py`), which answers from a
*bounded subgraph* hydrated through the SQLite indices rather than loading the
whole graph. It contrasts that with one `SqliteStore.load()` — the cost the old
read path paid on *every* call.

### Recorded results

| corpus | full `load()` | find_module | port_map | get_hierarchy (subtree) | impact_of_change | clock_domains / uvm (precomputed) |
|---|---|---|---|---|---|---|
| 20 000 files, 140 940 nodes, 323 831 edges | ~5000 ms | **0.9 ms** | **0.7 ms** | **3.7 ms** | **120 ms** | **<0.5 ms** |

A localized query is **1000–16000×** faster than the old per-call load, and —
crucially — its latency tracks the *answer* size, not the design size, so it
does not grow as the graph scales toward 10–100+ GB (where a full load no
longer fits in memory at all).

**Whole-design queries stay O(design).** A query whose answer *is* most of the
graph — `search_nodes("*")`, `get_hierarchy` of a top that directly contains
the whole design, `find_signal_drivers` of a net present in every module —
necessarily touches the whole graph and is not faster than a load. These are
reported separately by the script; the target covers the localized tools.

## Context savings: what an assistant pays per question

```bash
hdl-kgraph bench context              # human table
hdl-kgraph bench context --json       # machine-readable, includes every recipe
hdl-kgraph bench fidelity             # where grep gets the answer wrong
```

`hdl-kgraph setup` writes a claim into every assistant's instruction file —
"query the graph instead of grepping the raw RTL" — and until now nothing in
the project measured it. `bench context` prices each design question twice:
once as the JSON envelope an MCP tool returns, once as the grep output plus
the files a no-graph agent would have to read.

Questions are derived from the graph itself (the biggest top-level unit, the
most-instantiated modules, the most-driven signals), ordered by `(-count,
name)`, so the command runs on any design with no hand-written fixtures and
two runs over the same database produce identical numbers.

### The baseline, and why you can trust it

The result is only as honest as the thing the graph is compared against. Five
rules are enforced in code and reprinted under every report:

1. **Same corpus, both arms.** The baseline searches exactly the HDL files the
   graph indexed, read from the `files` table. A real agent greps the whole
   tree — in the SoC below that is 1.2 GB of Verilator objects and P&R
   netlists next to 807 KB of RTL — so this makes the baseline *stronger* than
   reality.
2. **Bounded reads.** `--baseline-max-files` (default 10) caps how many hit
   files the baseline ingests. Rows where the cap binds are flagged `capped`,
   because those rows flatter the baseline.
3. **Published recipe.** Every `rg` command and every file read is listed in
   `--json`, so the search can be reproduced or disputed by hand.
4. **Losses are shown.** Questions where grep is *cheaper* are printed, not
   suppressed — `port-map` is consistently one of them.
5. **Cost only.** Correctness is not scored here; the graph would be both
   contestant and judge. That is what `bench fidelity` is for.

A sixth guard is applied per row: if the graph's answer is **empty**, the row
is shown but excluded from the headline. Pricing a 20-token non-answer against
a 40 000-token grep would manufacture a 99.9% "saving" out of a failure.

Two further notes. The scan is **pure Python, never `rg`**, even where ripgrep
is installed — a dual implementation would make the byte counts depend on the
machine, and ripgrep is not universally present. And the default token count is
a `chars/4` heuristic, named in every report; since the same estimator prices
both arms the *ratio* is robust, and `--tokenizer tiktoken`
(`pip install 'hdl-kgraph[bench]'`) gives exact counts.

### Recorded results

Design: an RV32I + GPU-lite SoC (`claude_verilog_test`). Heuristic tokenizer,
`--baseline-max-files 10`. Two regimes are recorded because the answer differs
between them, and both commands are reproducible as written.

**A. Hand-written RTL and testbenches only** — 84 indexed files, 1 018 929
bytes. Reading the whole design costs ~248 300 tokens; no baseline can exceed
that.

```bash
hdl-kgraph build . --exclude 'pnr/**' --exclude 'micro_p/**' \
                   --exclude 'sim/**' --exclude 'slpp_all/**'
hdl-kgraph bench context
```

| question | target | graph | grep | saved tok | saved % | capped |
|---|---|---|---|---|---|---|
| `hierarchy` | tb_axi_lite_interconnect | 247 | 49 077 | 48 830 | 99.5% | 10/23 |
| `who-instantiates` | rv32i_clock_gate | 456 | 48 944 | 48 488 | 99.1% | |
| `impact` | cdc_2ff_sync | 562 | 58 193 | 57 631 | 99.0% | 10/13 |
| `clock-domains` | *(whole design)* | 401 | 38 277 | 37 876 | 99.0% | 10/48 |
| `who-instantiates` | cdc_2ff_sync | 722 | 37 122 | 36 400 | 98.1% | |
| `who-instantiates` | apb4_register_bank | 634 | 29 566 | 28 932 | 97.9% | |
| `find-signal-drivers` | SLV_PERIPH | 1 015 | 37 104 | 36 089 | 97.3% | |
| `find-signal-drivers` | prdata | 1 132 | 38 739 | 37 607 | 97.1% | 10/17 |
| `impact` | apb4_register_bank | 925 | 29 566 | 28 641 | 96.9% | |
| `port-map` | cdc_2ff_sync | 572 | 1 283 | 711 | 55.4% | |
| `port-map` | apb4_register_bank | 1 947 | 1 466 | **−481** | **−32.8%** | |
| **TOTAL** | | **8 613** | **369 337** | **360 724** | **97.7%** | 4/11 |
| **MEAN / question** | | 783 | 33 576 | 32 793 | **82.4%** | |
| **MEDIAN / question** | | 634 | 37 122 | 36 400 | **97.9%** | |

- **Worst single question by absolute cost avoided:** `impact cdc_2ff_sync` —
  562 vs 58 193, i.e. **57 631 tokens** saved on one question (99.0%).
- **Worst single question by percentage:** `port-map apb4_register_bank` —
  1 947 vs 1 466, i.e. the graph costs **32.8% more**.

**Mean-% and median-% diverge sharply here — 82.4% against 97.9%.** One row
(`port-map`) drags the mean down fifteen points. Neither statistic is wrong,
which is exactly why the tool prints both and refuses to pick: a token-savings
distribution is long-tailed, and any single headline number hides something.
For context budgeting the **aggregate (97.7%)** is the one that matters,
because it is token-weighted.

**What that means in practice.** Those 11 questions cost 8 613 tokens through
the graph and 369 337 without — more than a 200 k context window holds. So the
un-graphed session cannot even ask all 11 questions; the graphed one spends
~4% of its context on them. Put differently: reading the *entire* RTL corpus
costs ~248 300 tokens, so past roughly seven grep-and-read questions you would
have been better off pasting in every file.

**B. The whole tree, including P&R netlists** — 453 indexed files, 12 537 314
bytes, ~3 123 600 tokens to read in full (`hdl-kgraph build`, no exclusions).

| question | target | graph | grep | saved tok | saved % |
|---|---|---|---|---|---|
| `find-signal-drivers` | regs_o | 6 468 | 665 104 | 658 636 | 99.0% |
| `impact` | sky130_fd_sc_hd__mux2_1 | 2 058 | 170 003 | 167 945 | 98.8% |
| `find-signal-drivers` | s_axil_arready | 6 735 | 412 857 | 406 122 | 98.4% |
| `impact` | sky130_fd_sc_hd__fa_1 | 3 046 | 89 748 | 86 702 | 96.6% |
| `who-instantiates` | sky130_fd_sc_hd__mux2_1 | 6 722 | 170 003 | 163 281 | 96.0% |
| `who-instantiates` | sky130_fd_sc_hd__conb_1 | 6 759 | 169 913 | 163 154 | 96.0% |
| `hierarchy` | csa32_8 | 50 806 | 1 178 791 | 1 127 985 | 95.7% |
| `who-instantiates` | sky130_fd_sc_hd__fa_1 | 6 572 | 89 748 | 83 176 | 92.7% |
| `port-map` | sky130_fd_sc_hd__mux2_1 | 50 384 | 227 862 | 177 478 | 77.9% |
| `port-map` | sky130_fd_sc_hd__fa_1 | 55 911 | 227 953 | 172 042 | 75.5% |
| `clock-domains` | *(whole design)* | 13 039 | 40 761 | 27 722 | 68.0% |
| **TOTAL** | | **208 500** | **3 442 743** | **3 234 243** | **93.9%** |
| **MEAN / question** | | 18 955 | 312 977 | 294 022 | **90.4%** |
| **MEDIAN / question** | | 6 735 | 170 003 | 167 945 | **96.0%** |

- **Worst single question by absolute cost avoided:** `hierarchy csa32_8` —
  50 806 vs 1 178 791, i.e. **1 127 985 tokens** saved on one question (95.7%).
- **Worst single question by percentage:** `clock-domains` at 68.0% — the
  graph's weakest row here is still a better-than-3× win.
- **All 11 rows hit the read cap**, so the grep column is a floor and the real
  un-graphed cost is higher than shown.

Absolute numbers grow on both sides across a 12× jump in corpus size, but the
ratio barely moves. That is the point: the graph's cost tracks the *answer*,
the baseline's tracks the *design*.

**Where the graph loses.** `port-map` costs *more* than grep. The
question localizes perfectly — `rg '^\s*module foo\b'` finds one file, and
reading it is cheap — while the graph returns every port and parameter as
structured JSON, which is more verbose than the declaration it came from. The
graph wins where an answer is *scattered* (who instantiates this, what drives
that, what breaks if this changes) and loses where it is already in one place.
That is the honest shape of the result. Note it flips in regime B, where the
same question wins ~77% — not because the graph got better, but because a P&R
netlist is a large file to read for one port list.

**The saving scales with the design, and on a small one it inverts.** Run
against a 200-file synthetic corpus from `scripts/gen_corpus.py` — 39 KB
total, ~9 800 tokens to read *in full* — the same command reports a median of
58% but an **aggregate of −35%**: the graph's `hierarchy` answer for that
design is ~15 500 tokens, larger than the entire source. When a design fits
comfortably in context, reading it beats querying it, and this tool is not
worth its build step. The numbers above come from a real SoC because that is
the regime the graph is for; if your design is small, run the benchmark and
expect it to tell you so.

## Answer fidelity: what grep gets wrong

```bash
hdl-kgraph bench fidelity
```

Cost is only half the claim. `bench fidelity` asks both arms the same question
("which design units instantiate X?") against **hand-authored** ground truth —
written by reading the sources, never read back from the graph — over a small
suite bundled in `hdl_kgraph/bench/suite/`. `--suite PATH` runs your own cases.

| case | graph | grep | why |
|---|---|---|---|
| macro-hidden instantiation | correct | **incomplete** | the call site reads `` `MAKE_FIFO(u_fifo) ``; `_` is a word character, so no `\bfifo\b` search matches it |
| comment / string mention | correct | **false positive** | nothing instantiates `sram_ctrl`; grep reports the module that names it in a TODO and a `$display` |
| VHDL case-insensitive name | correct | **incomplete** | the entity written `alu` is legally `ALU`; a literal search for `ALU` matches nothing |
| plain instantiation | correct | correct | the control case, kept so the suite is not a rigged scoreboard |

Graph 4/4, grep 1/4 — but this is a **qualitative catalogue, not a score**. A
different case mix gives a different ratio, and the ratio is not the point: the
four failure modes are.

## Live agent A/B (opt-in, spends API budget)

```bash
hdl-kgraph bench agent --tasks tasks.txt --repeat 3 --model claude-opus-5
```

Runs each task through Claude Code twice — arm A with exactly one MCP server
(hdl-kgraph), arm B with none — and reports the token usage the CLI itself
returns. Verified against Claude Code 2.1.224.

Both arms are isolated identically: `--strict-mcp-config` with an inline
per-arm config, `--setting-sources user,local` so the project `CLAUDE.md`
cannot tell the control arm to prefer a tool it does not have, a
benchmark-owned `CLAUDE_CONFIG_DIR`, and `--tools 'Read,Grep,Glob'`. Bash is
dropped entirely rather than deny-listed: `Bash(hdl-kgraph *)` is prefix
matched and trivially evaded (`cd rtl && hdl-kgraph …`, `python -m hdl_kgraph`,
`sh -c '…'`), so the control arm would silently regain the graph. Every run's
`init` event is recorded and the arms are *asserted* to differ only in MCP.

Read the caveats before quoting any number from this tier:

- **No determinism exists.** Claude Code exposes no seed and no temperature.
  A single-run delta is noise; use `--repeat` and read the spread.
- **Prompt caching favours whichever arm runs second.** Arm order is
  alternated per repetition, and cache-creation/cache-read tokens are reported
  as separate series rather than folded into one cost number.
- **The environment floor can dwarf the signal.** On a machine with many
  installed slash commands, agents, and hooks, a trivial prompt cost ~22 k
  tokens before the task began — hence the clean config directory.
- **`total_cost_usd` is a list-price estimate** under subscription auth, not
  billed spend. It is reported as an estimate, never as "dollars saved".
- Exit code is not a success signal: an auth failure exits 0 with
  `is_error: true`. The runner gates on the result object instead.

No numbers are recorded here: they would be a snapshot of one machine, one
model version, and one task list. Run it on yours.
