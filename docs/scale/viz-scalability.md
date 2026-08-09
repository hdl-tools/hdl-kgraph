# Visualization scalability for very large designs

**Status: Phases 1–5 delivered; Phase 6 parked — post-M6.** This work does not
gate the MVP (M1–M4) or the v0.6 (M6) release. The foundation — renderer
hygiene (Phase 1), the precomputed-layout "static" tier with auto-routing
(Phase 2), the community collapse/drill-down view (Phase 3), the inline-payload
size guard and gzip compression (Phase 4), and the GraphML/GEXF/JSON export
escape hatch (Phase 5) — has shipped; only the WebGL renderer (Phase 6) remains
parked, recorded here so the trade-off analysis is not lost should the adopted
tiers prove insufficient on a real design.

## Problem statement

`visualize` (M5) emits a single self-contained HTML file: the vendored D3 v7
bundle plus the graph JSON spliced into `src/hdl_kgraph/viz/template.html` by
`render_html()` in `src/hdl_kgraph/viz/__init__.py`. The force view runs
`d3.forceSimulation` client-side and redraws on an HTML5 canvas. Existing
mitigations already buy a lot: the module projection is the default payload
(one node per design unit), canvas was chosen over SVG ("SVG dies near 1k
nodes"), and `--full` mode defaults the noisy node/edge kinds off.

The M5 acceptance bar was "renders a 1k-node graph without freezing", and the
2000-file synthetic corpus measures ~14k nodes / ~32k edges in full mode.
Large SoCs blow past that: 10k+ design units even in projection mode,
100k–1M+ nodes in full mode. At that scale the current approach fails on four
independent axes:

1. **Layout time.** `d3.forceSimulation` runs on the browser main thread,
   O(n log n) per tick for the Barnes–Hut charge force. At 100k nodes the tab
   freezes for minutes (or forever) before the first useful frame.
2. **Frame cost.** `draw()` in the template issues one
   `beginPath`/`setLineDash`/`stroke` per edge per frame, with no viewport
   culling. Pan/zoom becomes a slideshow well before 50k edges.
3. **Payload size.** The JSON is inlined uncompressed (array-of-objects, full
   key names, float coordinates). At 100k+ nodes the artifact reaches tens to
   hundreds of MB — slow to write, slow to parse, hostile to email/review
   attachment, which is the artifact's whole point.
4. **Comprehension.** Even with infinite rendering budget, an unstructured
   100k-node hairball answers no question a human has. Past some size the
   problem is presentation, not performance.

Any fix must preserve the core constraints: one self-contained file that opens
air-gapped via `file://`, vendored JS with a compatible license (the D3
precedent is ISC), pure-Python-friendly install (heavy deps as optional
extras), and no regression for the small designs the tool serves today.

## Candidate approaches vs. the current d3 + canvas renderer

| # | Approach | Solves | Verdict |
|---|---|---|---|
| 0 | Current d3-force + canvas (baseline) | — | Keep for small graphs |
| 1 | WebGL renderer (sigma.js + graphology, PixiJS, …) | frame cost only | Defer |
| 2 | Precomputed Python-side layout | layout time | **Adopt** (tier: static) |
| 3 | Hierarchical aggregation / drill-down | comprehension, frame cost, payload | **Adopt** (tier: aggregate) |
| 4 | Hybrid tiered auto-selection | all, by routing | **Adopt** (umbrella) |
| 5 | Export to Gephi/Cytoscape (GraphML/GEXF) | extreme scale | **Adopt** (escape hatch) |
| 6 | d3-force in a Web Worker | tab freeze only | Reject |
| 7 | Canvas hygiene: culling, batching, label/edge LOD | frame cost | **Adopt** (tier: all) |

### 0. Current d3-force + canvas (baseline)

*Pros:* zero dependencies beyond the vendored ISC D3; live physics makes small
graphs pleasant to untangle by dragging; the template has accumulated
hard-won embedded-viewer fixes (canvas sizing, deferred layout, DPR guards)
that tests pin. *Cons:* the four failure modes above. *Conclusion:* remains
the right answer at small scale — nothing below replaces it there.

### 1. WebGL rendering

sigma.js + graphology (both MIT) or PixiJS (MIT) move drawing to the GPU and
comfortably render 100k+ elements at 60 fps. But this fixes only axis 2:
layout still has to come from somewhere, the payload is unchanged, and the
hairball is now merely a *fast* hairball. Costs: ~300–600 KB more vendored
JS; a full rewrite of zoom/picking/filter/search plumbing; two render paths
to maintain; and WebGL is exactly what locked-down, software-rendered, or
embedded viewers — this project's air-gapped audience — most often lack.
Cosmograph (GPU force layout) is excluded outright: the application/widget
carries a non-commercial license, and the underlying cosmos library alone
lacks the picking/label affordances we'd need. *Verdict:* defer; revisit only
if the adopted tiers prove insufficient (see Phase 6).

### 2. Precomputed Python-side layout

Compute node coordinates at `visualize` time and ship them in the payload;
the client skips simulation entirely and paints the first frame immediately.
This removes the single worst bottleneck (axis 1) and keeps the existing
canvas renderer almost unchanged — picking moves from `sim.find` to a
`d3.quadtree` (already in the vendored bundle), and drag becomes plain
repositioning.

A naive global `networkx.spring_layout` is itself too slow past ~10k nodes.
The trick is to never run one big layout: build the **community quotient
graph** (the Louvain communities are already computed, seeded, and on every
node — `metrics.communities()`), `spring_layout` that small graph to place
one supernode per community, then `spring_layout` each community's induced
subgraph independently (each is small) and offset members by their
supernode's position scaled by √(community size). Total cost is a sum of
small layouts — seconds at 50k nodes — deterministic with fixed seeds, and
the output is visually clustered in a way that *matches* the community
coloring users already see.

Dependencies: numpy + scipy (both BSD) for networkx's sparse fast path,
shipped as an optional `[layout]` extra with a graceful fallback (warn and
use the live simulation) when absent. python-igraph and fa2/fa2_modified are
rejected on license (GPL); pygraphviz/sfdp is rejected because it needs a
system Graphviz install. *Cons:* loses live physics (acceptable — at this
scale the simulation never settles anyway); adds a layout module to maintain.

### 3. Hierarchical aggregation / drill-down

Render the graph **collapsed**: one supernode per Louvain community (in full
mode, aggregate twice — leaf nodes into their owning design unit, units into
communities), sized ∝ √(member count), labeled by the community's
highest-betweenness member (`metrics.module_metrics` already ranks these).
Super-edges carry summed weights. Double-click expands a community in place
to its members at precomputed offsets; search auto-expands the community
containing a hit. The visible-entity count stays bounded in the hundreds to
low thousands, so the existing canvas renderer keeps working untouched —
and this is the only candidate that makes a 100k-node graph *humanly useful*
(axis 4), while also collapsing the payload's visual working set (axes 2–3).

*Cons:* real UI complexity (expand/collapse state interacting with kind/
community filters); detail is hidden until drilled into; Louvain partitions
are subsystem *suggestions* (their own docstring's caveat) so boundaries can
look arbitrary on some designs. Reuses `metrics.communities()`,
`metrics.module_projection()`, and `analysis.hierarchy_tree()` rather than
inventing new structure.

### 4. Hybrid tiered auto-selection

No single mode wins at every size, so route by measured node/edge counts and
keep one template that reads the mode from the payload:

| Tier | Auto trigger | Layout | Rendering |
|---|---|---|---|
| **live** | nodes ≤ 2 000 and edges ≤ 6 000 | client d3-force (today, unchanged) | canvas + hygiene fixes |
| **static** | up to ~50k nodes | precomputed in Python, shipped in payload | canvas, quadtree picking |
| **aggregate** | nodes > 20 000 or edges > 50 000 | precomputed at both levels | canvas, collapsed supernodes, expand on demand |
| **escape hatch** | ~> 250k nodes | refuse inline with guidance | `export` → Gephi/Cytoscape |

Small designs keep today's behavior bit-for-bit (existing tests pass by
construction); each tier pays only its own complexity. Proposed CLI surface:
`--layout auto|live|static` (default auto), `--collapse auto|on|off`, and
every auto decision prints one informational line
(`layout: static (14086 nodes > 2000)`) so behavior is never mysterious.
Thresholds live as module constants in `viz/__init__.py` so tests can pin
them. *Cons:* threshold tuning and more code paths to test — the price of
not regressing anyone.

Payload strategy (single-file constraint kept): quantize coordinates to
integers and intern repeated strings (`kind`/`file`/`domain`) above a size
threshold; switch to columnar arrays above ~200 KB estimated JSON; gzip +
base64 with a client-side `DecompressionStream("gzip")` decode branch above
~2 MB (supported by all evergreen browsers, works under `file://`; the
plain-JSON branch is retained for small payloads so they stay readable and
diffable); hard error above ~75 MB raw with an actionable message pointing at
`--collapse`, dropping `--full`, or `export` — overridable with
`--force-inline`.

### 5. Export to external tools (GraphML/GEXF)

`hdl-kgraph export --format graphml|gexf|json` via networkx's built-in
writers (~50 lines plus stringifying the `NodeKind`/`EdgeKind`/`Language`
enums and `attrs` dicts into GraphML-safe scalars). Gephi's OpenOrd/
ForceAtlas2 layouts handle million-node graphs; Cytoscape covers the analysis
crowd. *Pros:* near-zero cost, zero risk to the HTML artifact, the honest
answer at extreme scale. *Cons:* breaks the self-contained story (an external
tool is required), so it is an escape hatch, not the plan.

### 6. d3-force in a Web Worker — rejected

Blob-URL workers do run under `file://` and would keep the single-file
property, but a 100k-node layout still takes minutes — the freeze just moves
off-thread while the user stares at a progress bar, and the payload memory is
duplicated across threads. Entirely superseded by precomputed layout.

### 7. Canvas renderer hygiene — adopt regardless

Independent of everything above, the template's draw loop leaves easy wins on
the table: batch edges into one `Path2D` stroke per (dash, width-bucket)
group instead of per-edge `beginPath`/`stroke`; skip nodes and edges whose
endpoints fall outside the transformed viewport (cheap AABB check); cap
labels drawn per frame on top of the existing `transform.k > 0.9` gate; above
N visible edges draw a weight-prioritized sample with a "showing X of Y
edges" note in `#stats`. Tiny diffs, no dependencies, benefits every tier —
this is Phase 1 for a reason.

## Phased implementation roadmap (parked, post-M6)

- **Phase 1 — renderer hygiene** (`viz/template.html`) — **done.** Edge
  batching into one `Path2D` per (dash, width-bucket); viewport culling of
  off-screen nodes/edges; `MAX_LABELS` per-frame label cap; weight-prioritized
  edge sampling above `MAX_DRAWN_EDGES` with a "showing X of Y edges" note. No
  new deps, no API change; template behaviors pinned with string asserts in
  `tests/test_visualize.py` in the style of the existing canvas-sizing tests.
- **Phase 2 — precomputed layout** — **done.** New `viz/layout.py` with
  `compute_layout(view, comm_of, seed=42)` implementing the community-stacked
  layout (quotient-graph `spring_layout`, then per-community subgraphs offset
  by √size); `[layout]` extra (numpy ≥ 1.24, scipy ≥ 1.10) in `pyproject.toml`;
  `render_html(..., layout=...)` returning a `RenderResult` and a
  `--layout auto|live|static` flag; tier thresholds (`LIVE_MAX_NODES`,
  `LIVE_MAX_EDGES`, `STATIC_MAX_NODES`) as module constants; payload gains
  `"layout"` plus quantized integer per-node `x`/`y`; the template's `STATIC`
  branch skips the simulation and picks via `d3.quadtree`; warn-and-fall-back
  to the live tier when numpy is missing (never fail the command). Tests cover
  determinism, the fallback path, routing, and a scale smoke on a synthetic
  graph.
- **Phase 3 — aggregation/drill-down** — **done.** `viz/aggregate.py` collapses
  the module projection to one supernode per Louvain community, named after the
  community's highest-betweenness member (`metrics.module_metrics`), with
  super-edge weights summed over cross-community projection edges. The
  `--collapse` flag ships `supernodes`/`superlinks` in the payload; the template
  draws the collapsed view (supernode radius ∝ √member-count) and **double-click
  expands a community in place** to its members — visible entities and edges are
  resolved from the `expanded` set each toggle and re-fed to a live simulation
  (bounded supernode counts, so no `[layout]` extra needed). With **`--full` it
  is two-level**: communities of units, each unit expandable to its leaf nodes
  (`aggregate_full` + `metrics.unit_membership`). The client resolves every node
  to the outermost still-collapsed group in its ancestry (community, then unit);
  double-click drills in, double-click a leaf re-collapses its innermost group,
  and double-click empty space collapses everything. **Search auto-expands**
  every group on the way to a match (and re-collapses those it opened once the
  query clears), so hits hidden inside collapsed subsystems/units are reachable.
  Expand/collapse **animates in place**: entity positions are remembered across
  rebuilds (`posById`) and newly-revealed children are seeded at their parent
  group's position, so they emerge from the supernode rather than flying in from
  the origin — client-side, no precomputed layout needed. Tests: super-edge
  weights match projection sums, two-level counts/labels, representative labels,
  payload round-trips, and the template drill-down / two-level / search-expand /
  position-seeding branches. Phase 3 is complete.
- **Phase 4 — payload guards** (split for reviewability):
  - **4a — inline size guard** — **done.** `render_html` measures the raw
    payload and raises above `MAX_INLINE_BYTES` (`viz/__init__.py`) with an
    actionable message (drop `--full` or narrow with `--top`); the new
    `--force-inline` flag overrides and the `RenderResult.note` flags the
    over-cap size. Pure Python, no template change. Tests shrink the cap to
    exercise refusal/override/no-op.
  - **4b — compression** — **done.** Above `COMPRESS_OVER_BYTES` (`viz/__init__.py`)
    the payload is gzip+base64-encoded inline (`mtime=0` for byte-identical
    determinism) and decoded client-side via `DecompressionStream` in an async
    template bootstrap; smaller graphs stay plain JSON. The size guard now
    measures the *embedded* (post-compression) bytes, so compressible designs
    that 4a would have refused now embed. Old browsers lacking
    `DecompressionStream` get an in-page message pointing at `export`. Columnar
    encoding stays parked — gzip already captures most of the win, so it is not
    worth the added complexity. Tests cover the compressed round-trip,
    determinism, the plain-JSON path for small graphs, and that compression
    lets an otherwise-over-cap payload embed.
- **Phase 5 — export escape hatch** — **done.** `hdl-kgraph export
  --format graphml|gexf|json` in `src/hdl_kgraph/export.py`: `_sanitize`
  copies the graph with scalar-only attributes (enums → `.value`, the
  `line_span` tuple → `line_start`/`line_end`, the `attrs` dict
  JSON-serialized to an `attrs_json` string via `json.dumps(..., default=str)`),
  then dispatches to NetworkX's `write_graphml` / `write_gexf` writer APIs for
  those formats; the JSON branch instead converts via `node_link_data` and
  writes with `json.dumps` / `write_text`. No HTML-artifact changes. Tests
  (`tests/test_export.py`) cover the GraphML/GEXF/JSON round-trips, attr
  flattening, the unknown-format error, and a CLI smoke.
- **Phase 6 — WebGL (explicit non-goal for now)**: only if real-world
  feedback shows tiers 0–5 insufficient, vendor sigma.js + graphology (MIT,
  consistent with the ISC-D3 precedent) behind a `--renderer webgl` flag. The
  payload contract from Phases 2–4 is renderer-agnostic by design, so this
  stays a bounded add-on rather than a rewrite.

## Dependency and licensing decisions

| Decision | Choice | License | Rationale |
|---|---|---|---|
| Python layout | numpy + scipy via `[layout]` extra | BSD-3 | networkx `spring_layout` sparse fast path; pure-pip wheels everywhere |
| Rejected | python-igraph, fa2/fa2_modified | GPL-2/3 | viral license vs. this project's terms |
| Rejected | pygraphviz / sfdp | — | needs a system Graphviz install |
| JS (now) | vendored D3 v7 only, unchanged | ISC | quadtree + zoom already in the bundle |
| JS (Phase 6 only) | sigma.js + graphology | MIT | only license-clean WebGL option with built-in picking; Cosmograph app excluded (non-commercial license) |

## Verification strategy (for the phases, when picked up)

1. **No regression:** small fixture graphs stay in live mode with plain JSON,
   so every existing assert in `tests/test_visualize.py` holds by
   construction.
2. **Scale tests:** build NetworkX graphs directly (no parsing) — a
   preferential-attachment projection generator at 10k/50k nodes — and assert
   a wall-clock budget on `compute_layout` (e.g. < 20 s at 50k), guarded by
   `pytest.importorskip("numpy")`.
3. **Corpus benchmark:** `scripts/bench_viz.py` mirroring
   `bench_incremental.py`: generate the 2000-file corpus (optionally with a
   fan-out knob to push past 10k modules), build, time `render_html` for
   projection/full/collapsed, report payload bytes; record results in
   `docs/scale/benchmarks.md` with targets (e.g. full-mode 14k-node render < 10 s,
   payload < 5 MB compressed).
4. **Determinism:** same database → byte-identical HTML across runs (seeded
   Louvain + seeded layout), asserted in a test — keeps artifacts diffable in
   review workflows.
5. **Compression round-trip:** gunzip and parse the embedded payload in
   Python within the test; a manual browser smoke checklist (Chrome/Firefox
   via `file://`) documented, since the repo has no JS test harness.
