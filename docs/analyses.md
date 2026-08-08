# Analyses

The M5 analyses turn structure into insight. Dataflow (`DRIVES`/`READS`) is
extracted from always/process blocks, continuous assigns, and instance port
directions; clocks and resets carry evidence scores (sensitivity-list proof
= 1.0, name-pattern heuristics = 0.4).

```bash
hdl-kgraph query clock-domains     # clock nets, alias-merged across hierarchy
hdl-kgraph query cdc               # signals driven in domain A, read in domain B
hdl-kgraph query reset-tree        # async vs (heuristic) sync resets
hdl-kgraph query drivers ready     # what drives signal 'ready' (--readers flips it)
hdl-kgraph query uvm               # UVM components by role + TEST_COVERS links
hdl-kgraph query power-domains     # UPF power domains, elements + strategies
hdl-kgraph lint                    # unconnected ports, undriven/unread signals,
                                   #   dead modules, redundant parameter overrides
hdl-kgraph metrics --communities   # fan-in/out, hubs/bridges, Louvain subsystems
hdl-kgraph visualize -o graph.html # self-contained interactive HTML
hdl-kgraph export --format graphml # GraphML/GEXF/JSON for Gephi, Cytoscape
```

The `query`, `lint`, and `metrics` reporting commands take `--json` for
scripting. `visualize` and `export` write files instead (`export --format
graphml|gexf|json`).

## Caveats — reports, not gates

- **CDC findings are *suspects*, not violations** — synchronizers are not
  recognized, so a proper 2-flop sync still shows up. An SDC `set_clock_groups
  -asynchronous` or `set_false_path` covering a crossing marks it
  `declared_safe`, and the report partitions it out of the active list (a
  `cdc_suppressed_count` keeps it visible); everything else is worth reviewing.
- **Power domains** (`query power-domains`, UPF M10) list each
  `create_power_domain` with its resolved element instances and its
  isolation/retention strategies — the power-intent analogue of the
  clock-domain report. A `.upf` element the design lacks (or the `.`
  design-root element) is reported unresolved rather than invented.
- **`lint` always exits 0**; it is a report, not a gate. Signal-level
  checks skip files with parse errors and implicit-net stubs so a finding
  is worth reading; confidences below 1.0 mark heuristics.
- **`metrics`** computes fan-in/fan-out and betweenness centrality on the
  module-level instantiation projection; `--limit/-n` caps the listing,
  `--communities` adds Louvain community (subsystem) suggestions.

## Waiving lint findings

Known-benign findings (an intentional open port, a module only a future
top instantiates, a signal driven through a construct the graph does not
model) can be waived so the remaining report stays worth reading. Waivers
live in `hdl-kgraph.toml` — discovered from the build root, `--config PATH`
/ `--no-config` override — or in extra files passed with `--waiver-file`:

```toml
[[lint.waivers]]
check  = "open-port"             # required: exact check name
name   = "soc_top.u_dbg"         # glob on the finding name
reason = "debug port, tied off"  # required: the reviewable justification

[[lint.waivers]]
check  = "unconnected-port"
module = "fifo_*"                # glob on the owning module: one waiver
reason = "status outputs unused" # covers every instantiation
```

- `check` plus at least one of `name`/`module`/`file` is required; all
  given criteria must match. `reason` is mandatory — a waiver is a
  reviewed decision, not a mute button.
- `name` and `module` are case-sensitive globs (like `query search`); a
  dotted `name` pattern matches the qualified name, a plain one the last
  segment. `file` globs the root-relative path, like `[build].exclude`.
- Waived findings are dropped from the report; the footer counts them
  (`3 finding(s), 2 waived`) and `--show-waived` lists them with reasons.
  `--json` returns `{"findings", "waived", "unused_waivers", "counts"}`.
- A waiver that matches nothing is reported stale on stderr (the exit
  code stays 0), so waiver lists do not rot as the design evolves.

`lint` also honors `[build].top` from the config as `--top` exemptions
for `dead-module` (CLI flags are additive).

## Visualization

`visualize` writes a single self-contained HTML file — D3 is vendored and
the graph data embedded, so it opens air-gapped and can be attached to a
review or bug report as-is. The page opens on a collapsible hierarchy view;
a second tab is a force-directed graph with node-kind / edge-kind / community
filters and a "colour by community" toggle (Louvain subsystems). Searching by
name highlights the matched node, its neighbors out to the chosen number of
relationship hops (the "hops" selector next to the search box, default 1), and
the relationship lines between them, dimming the rest of the graph so a node's
local context stands out.

- The default payload is the module-level projection, which stays
  responsive on large designs; `--full` embeds every node and edge.
- `--top NAME` roots the hierarchy view at a module (an unknown name is an
  error, like `tree`).
- `--title TEXT` sets the page title (defaults to the build root's name);
  `--open` launches the result in a browser after writing it.
- `--layout auto|live|static` (default `auto`) picks the layout tier: `live`
  runs the in-browser force simulation, `static` ships precomputed coordinates
  so the graph view paints without a client-side freeze (needs the `[layout]`
  extra — `pip install 'hdl-kgraph[layout]'`), and `auto` routes by graph size.
  A missing `[layout]` extra falls back to `live`, never an error.
- `--kinds KIND` / `--exclude-kinds KIND` (both repeatable) restrict the plot to
  the node kinds of interest *before* the layout is solved, so positions are
  computed over the smaller graph for a more compact view — e.g.
  `--kinds module --kinds instance` or `--exclude-kinds signal --exclude-kinds port`.
  Edges to a dropped node fall away with it. Most useful with `--full` (the
  default projection is already module-level); an unknown kind is an error that
  lists the valid kinds.
- `--collapse` shows one supernode per subsystem (Louvain community) instead of
  every unit; double-click a supernode in the browser to expand it in place, and
  searching auto-expands the subsystem(s) containing a match. Adding `--full`
  makes it two-level — communities of units, each expandable to its leaf nodes.
- Large payloads are gzip-compressed inline automatically (decoded in the
  browser via `DecompressionStream`); small graphs stay plain JSON.
- A payload still past the inline size limit *after compression* is refused
  with guidance (drop `--full`, narrow with `--top`, or `export`);
  `--force-inline` writes it anyway.
- Scaling strategy for very large designs: [viz-scalability.md](viz-scalability.md).

`export` is the escape hatch for designs too large for the inline HTML
artifact: `--format graphml|gexf|json` writes the graph for Gephi
(OpenOrd/ForceAtlas2) or Cytoscape, which handle graphs the browser cannot.
Enums, the line span, and free-form `attrs` are flattened to scalar
attributes (`attrs` is serialized to JSON as an `attrs_json` string —
non-JSON values are stringified via `json.dumps(..., default=str)`).
