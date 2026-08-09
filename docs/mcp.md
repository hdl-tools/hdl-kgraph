# MCP server — AI assistants over the knowledge graph

`hdl-kgraph serve --mcp` exposes the graph to AI assistants via the
[Model Context Protocol](https://modelcontextprotocol.io). The server is
**read-only**: it answers each tool from `.hdl-kgraph/graph.db` and never builds
or updates it. Every call opens a fresh read connection and hydrates only the
bounded subgraph the query needs through the SQLite indices — it never loads the
whole graph (v0.9), so queries stay fast even on a 10–100+ GB design and a
rebuild by `hdl-kgraph build`/`update` is observed automatically with no
staleness window. The whole-design reports (`clock_domains`, `uvm_topology`) are
precomputed at build time and read as a small blob. See
[docs/scalability.md](docs/scalability.md).

Requires the `mcp` extra:

```sh
pip install 'hdl-kgraph[mcp]'
```

## One-command setup

```sh
hdl-kgraph build ./rtl        # if you haven't already
hdl-kgraph setup
```

`setup` detects installed assistants and writes the server entry into their
config:

| Assistant | Detection | Config written |
|---|---|---|
| Claude Code | `claude` CLI on PATH (or `CLAUDECODE` env) | project-scope `.mcp.json` in the current directory |
| Claude Desktop | the platform's `Claude/` config directory exists | `claude_desktop_config.json` (with a one-time `.bak` backup) |
| Cursor | `~/.cursor/` (or a project `.cursor/`) exists | project-scope `.cursor/mcp.json` |
| Codex CLI | `codex` on PATH or `~/.codex/` exists | `~/.codex/config.toml` `[mcp_servers.hdl-kgraph]` (one-time `.bak`; comments preserved) |
| Windsurf | `~/.codeium/windsurf/` exists | `~/.codeium/windsurf/mcp_config.json` (one-time `.bak`) |
| Gemini CLI | `gemini` on PATH or `~/.gemini/` exists | `~/.gemini/settings.json` (one-time `.bak`) |
| VS Code (Copilot) | `code` on PATH (or a project `.vscode/`) | project-scope `.vscode/mcp.json` (`servers` key, `"type": "stdio"`) |

Re-running is safe: the `hdl-kgraph` entry is updated in place and every
other key in the file is preserved (Codex's TOML is edited textually, so
comments survive too). Useful flags: `--list` (report detection only),
`--dry-run` (print the resulting file content without writing), `--yes`
(skip prompts), `--assistant NAME` (restrict targets), `--db PATH` (point
at a specific database), `--no-instructions` (skip the instruction-file
seeding described next).

### Instruction files

Besides the MCP config, `setup` also seeds each assistant's **instruction
file** with notes telling it the graph exists, to prefer it over grepping raw
RTL for structural questions, and how to call it — both the MCP tools and the
`hdl-kgraph tools …` CLI fallback (see [Using the graph without MCP](#using-the-graph-without-mcp)):

| Assistant | Instruction file |
|---|---|
| Claude Code | `CLAUDE.md` |
| Codex | `AGENTS.md` |
| Gemini CLI | `GEMINI.md` |
| Cursor | `.cursor/rules/hdl-kgraph.mdc` (always-applied rule) |
| Windsurf | `.windsurf/rules/hdl-kgraph.md` |
| VS Code (Copilot) | `.github/copilot-instructions.md` |
| Claude Desktop | — (no project-memory file) |

The notes live inside a managed block delimited by `<!-- hdl-kgraph:start -->`
and `<!-- hdl-kgraph:end -->`. Only that block is rewritten on a re-run —
anything you keep around it is preserved, and the file is created if absent.
Each instruction prompt is separate from the config prompt (so you can decline
one and accept the other); pass `--no-instructions` to skip them entirely.

## Manual configuration

For Claude Code, the CLI equivalent of what `setup` writes:

```sh
claude mcp add hdl-kgraph -- hdl-kgraph serve --mcp --db /path/to/repo/.hdl-kgraph/graph.db
```

For Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hdl-kgraph": {
      "command": "hdl-kgraph",
      "args": ["serve", "--mcp", "--db", "/path/to/repo/.hdl-kgraph/graph.db"]
    }
  }
}
```

Any other MCP client works with the same command line. For clients that
prefer HTTP:

```sh
hdl-kgraph serve --mcp --http 127.0.0.1:8000   # streamable HTTP at /mcp
```

### HTTP authentication

By default the HTTP transport has **no authentication**. Require a bearer
token with `--token` (or the `HDL_KGRAPH_MCP_TOKEN` environment variable so
the secret stays off the process command line):

```sh
export HDL_KGRAPH_MCP_TOKEN=$(openssl rand -hex 32)
hdl-kgraph serve --mcp --http 0.0.0.0:8000      # now requires the token
```

Clients then send it as a standard bearer credential:

```text
Authorization: Bearer <token>
```

Requests without a valid token are rejected. The token gates HTTP only;
stdio is a local pipe and ignores it.

> **Security:** the graph exposes your design's structure (module names,
> hierarchy, files). When the HTTP transport runs **without** a token, keep it
> bound to a loopback address (`127.0.0.1`, `localhost`, or `[::1]`) — the CLI
> warns when you bind any other host without `--token`. Only bind a routable
> address when every host on the network is trusted, you set a `--token`, or
> you put an authenticating reverse proxy in front.

## Tools

Every list-returning tool paginates: responses carry
`{total, offset, count, truncated, items}`, with `limit` clamped to 500.
Confidence scores follow the project convention (1.0 resolved, 0.8 unique
cross-file match, 0.6 ambiguous, 0.4 heuristic); VHDL names match
case-insensitively everywhere.

| Tool | Arguments | Answers |
|---|---|---|
| `find_module` | `name` (glob ok), `limit` | "is there a module called X?" — with port/parameter/instantiation counts |
| `get_hierarchy` | `top`, `depth` (default 3), `max_nodes` (default 500) | top-level units, or the instance tree under `top` (depth- and node-capped, omissions reported) |
| `who_instantiates` | `name`, `limit`, `offset` | every instantiation site of a unit |
| `port_map` | `module`, `instance` | ports/parameters in declaration order; with `instance`, its connection bindings |
| `impact_of_change` | `target` (file or unit), `max_depth`, `limit`, `offset` | "what breaks if this changes?" — summary first, then affected units nearest-first |
| `clock_domains` | — | clock domains with alias nets, the declaring scope (`qualified_name`/`file`/`line`, so identically-named domains stay distinguishable), process/signal counts, plus CDC suspects and `cdc_analysis` (`complete`/`degraded` — `degraded` means multi-instance clock aliasing merged distinct nets, so `cdc_suspect_count` is a lower bound and not a clean bill of health; `alias_collapses` names the ports and nets) |
| `find_signal_drivers` | `signal`, `module`, `readers`, `limit`, `offset` | "what drives signal X in module Y?" (`readers=true` for the readers) |
| `uvm_topology` | — | UVM components by role and testbench→DUT `TEST_COVERS` links |
| `search_nodes` | `name` glob, `kinds` (e.g. `module`, `signal`, `class`), `file` glob, `limit`, `offset` | anything else — the general node search |

## Using the graph without MCP

If the MCP server cannot be configured in your environment (or you'd rather an
agent shell out than speak MCP), the same nine tools are available as plain
commands under `hdl-kgraph tools`, printing the **identical** JSON envelope to
stdout. These use the same bounded, index-backed reader the MCP server does —
not a full-graph load — so they stay fast on large designs, and they need only
the base install (no `[mcp]` extra):

```sh
hdl-kgraph tools find-module 'fifo*' --limit 5
hdl-kgraph tools hierarchy                       # top-level units
hdl-kgraph tools hierarchy df_top --depth 2
hdl-kgraph tools find-signal-drivers stage --module df_top
hdl-kgraph tools impact adder | jq '.summary'
hdl-kgraph tools search-nodes '*' --kind signal --file 'rtl/*'
```

Subcommand names and options mirror the tools above
(`find-module`, `hierarchy`, `who-instantiates`, `port-map`, `impact`,
`clock-domains`, `find-signal-drivers`, `uvm-topology`, `search-nodes`); pass
`--db` to point at a specific `graph.db`. Pipe through `jq` to slice the result.

Two other MCP-free fallbacks exist:

- **HTTP transport** — `hdl-kgraph serve --mcp --http 127.0.0.1:8000` exposes the
  same tools over streamable HTTP (see [HTTP authentication](#http-authentication)).
  Still the MCP protocol, so the client must speak MCP-over-HTTP.
- **Static export** — `hdl-kgraph export --format json` dumps the whole graph to a
  node-link JSON file an agent can read directly (loads everything; best for
  whole-design-in-context rather than per-query lookups).

## Cold-checkout walkthrough

The M6 acceptance flow, from nothing to answers:

```sh
git clone <your-design-repo> && cd <your-design-repo>
pip install 'hdl-kgraph[mcp]'
hdl-kgraph build .
hdl-kgraph setup --yes
```

Then ask your assistant, e.g.:

- *"What drives signal `stage` in module `df_top`?"* →
  `find_signal_drivers(signal="stage", module="df_top")`
- *"What breaks if I change `adder`'s ports?"* →
  `impact_of_change(target="adder")` (or `port_map` first for the current
  shape)
