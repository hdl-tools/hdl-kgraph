"""Auto-configuration of AI assistants for the MCP server (M6 ``setup``).

A small detection registry, not assistant SDKs: each supported assistant is
a config file we know how to find and how to merge an ``mcpServers``-style
entry into. fastmcp is *not* required here — this module only edits JSON
and TOML config files.

Safety rules: the ``hdl-kgraph`` entry is updated in place and every other
key in the file is preserved (TOML edits are textual so comments survive);
user-level files get a one-time ``.bak`` backup before the first
modification; malformed JSON/TOML aborts rather than clobbering.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

SERVER_KEY = "hdl-kgraph"

#: Sentinels delimiting the block ``setup`` manages inside an assistant's
#: instruction file. Everything between them is ours to overwrite on a re-run;
#: everything outside is the user's and is preserved.
INSTRUCTIONS_START = "<!-- hdl-kgraph:start -->"
INSTRUCTIONS_END = "<!-- hdl-kgraph:end -->"


@dataclass
class Target:
    """One configurable assistant: where its config lives and if it's here."""

    name: str
    config_path: Path
    detected: bool
    backup: bool  # user-level configs get a .bak; project-scope files don't
    servers_key: str = "mcpServers"  # VS Code uses "servers", Codex "mcp_servers"
    fmt: str = "json"  # "json" or "toml" (Codex config.toml)
    entry_extras: dict[str, object] = field(default_factory=dict)
    #: The assistant's project instruction/memory file (CLAUDE.md, AGENTS.md,
    #: …) that ``setup`` seeds with usage notes; ``None`` if it has no such
    #: convention (e.g. Claude Desktop has no project memory).
    instructions_path: Path | None = None
    #: Text prepended only when *creating* the instruction file — used for the
    #: YAML frontmatter a dedicated Cursor ``.mdc`` rule needs to auto-apply.
    instructions_frontmatter: str | None = None

    def full_entry(self, entry: dict[str, object]) -> dict[str, object]:
        """*entry* plus this assistant's required extras (e.g. VS Code's type)."""
        return {**self.entry_extras, **entry}

    def merged_config(self, entry: dict[str, object]) -> dict[str, object]:
        """The config content (as a dict) after adding/updating our entry."""
        load = load_toml if self.fmt == "toml" else load_config
        config = load(self.config_path)
        servers = config.setdefault(self.servers_key, {})
        if not isinstance(servers, dict):
            raise ValueError(f"{self.config_path}: {self.servers_key!r} is not an object")
        servers[SERVER_KEY] = self.full_entry(entry)
        return config

    def preview(self, entry: dict[str, object]) -> str:
        """The exact file content ``write_config`` would produce."""
        if self.fmt == "toml":
            return merged_toml_text(self.config_path, self.servers_key, self.full_entry(entry))
        return json.dumps(self.merged_config(entry), indent=2) + "\n"


def plan_entry(db_path: Path) -> dict[str, object]:
    """The MCP server entry every assistant config gets."""
    return {"command": "hdl-kgraph", "args": ["serve", "--mcp", "--db", str(db_path)]}


#: The usage notes ``setup`` writes into each assistant's instruction file. Tells
#: the assistant the graph exists and to prefer it over grepping raw RTL, and
#: documents both the MCP tools and the ``hdl-kgraph tools`` CLI fallback (for
#: when MCP is not available). Confidence wording mirrors ``server._INSTRUCTIONS``.
INSTRUCTIONS_BODY = """\
## Querying the hdl-kgraph design graph

This repository has a knowledge graph of its HDL design (SystemVerilog / Verilog
/ VHDL) built by [hdl-kgraph](https://github.com/hdl-tools/hdl-kgraph). For
structural questions about the design — module hierarchy, where a unit is
instantiated, what drives a signal, clock domains, the impact of a change —
**query the graph instead of grepping the raw RTL**: it resolves cross-file
references and scores each edge by confidence (1.0 resolved, 0.8 unique
cross-file match, 0.6 ambiguous, 0.4 naming heuristic). VHDL names match
case-insensitively. The graph is read-only; rebuild it with `hdl-kgraph build`
or `hdl-kgraph update`.

**Via MCP** (configured for this assistant) — use the tools `find_module`,
`get_hierarchy`, `who_instantiates`, `port_map`, `impact_of_change`,
`find_signal_drivers`, `clock_domains`, `uvm_topology`, `search_nodes`. Start
with `get_hierarchy` or `find_module` to orient. In `clock_domains`, check
`cdc_analysis` first: `"degraded"` means multi-instance clock aliasing merged
distinct nets, so `cdc_suspect_count` is a lower bound and not a clean bill of
health.

**Without MCP** (from a shell, or where MCP is not set up) — the same tools are
CLI commands that print the same JSON, for example:

```sh
hdl-kgraph tools find-module fifo
hdl-kgraph tools hierarchy                # tops; add a name for its tree
hdl-kgraph tools impact adder
hdl-kgraph tools find-signal-drivers stage --module df_top
```
"""


def instructions_block() -> str:
    """The managed instruction block, sentinels included (no trailing newline)."""
    note = "<!-- Managed by `hdl-kgraph setup`; edits between these markers are overwritten. -->"
    return f"{INSTRUCTIONS_START}\n{note}\n\n{INSTRUCTIONS_BODY.rstrip()}\n{INSTRUCTIONS_END}"


def merged_instructions_text(text: str, block: str) -> str:
    """*text* with our block replaced in place, or appended if not present.

    Only the region between the sentinels is touched; any content the user keeps
    around it is preserved verbatim. An empty file becomes just the block.
    """
    start = text.find(INSTRUCTIONS_START)
    if start != -1:
        end = text.find(INSTRUCTIONS_END, start)
        if end != -1:
            return text[:start] + block + text[end + len(INSTRUCTIONS_END) :]
    if not text:
        return block + "\n"
    separator = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    return text + separator + block + "\n"


def instructions_preview(target: Target) -> str:
    """The exact file content ``write_instructions`` would produce for *target*."""
    if target.instructions_path is None:
        raise ValueError(f"{target.name} has no instruction file")
    text = (
        target.instructions_path.read_text(encoding="utf-8")
        if target.instructions_path.is_file()
        else ""
    )
    block = instructions_block()
    if not text and target.instructions_frontmatter:
        return target.instructions_frontmatter + block + "\n"
    return merged_instructions_text(text, block)


def write_instructions(target: Target) -> bool:
    """Seed *target*'s instruction file with the usage notes. True if it changed.

    Project-scope and self-contained (only our sentinel block is ever rewritten),
    so unlike user-level configs it needs no ``.bak``.
    """
    path = target.instructions_path
    if path is None:
        raise ValueError(f"{target.name} has no instruction file")
    new_text = instructions_preview(target)
    if path.is_file() and new_text == path.read_text(encoding="utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True


def _home() -> Path:
    return Path.home()


def _claude_desktop_dir() -> Path:
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / "Claude"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", str(_home()))) / "Claude"
    return _home() / ".config" / "Claude"


def detect_targets(project_dir: Path | None = None) -> list[Target]:
    """All known assistant targets with their detection state.

    Extending support is one entry here: name, config path, detection test,
    plus ``servers_key``/``fmt``/``entry_extras`` when the config shape
    deviates from the common ``mcpServers`` JSON object.
    """
    project_dir = project_dir or Path.cwd()
    home = _home()
    desktop_dir = _claude_desktop_dir()
    return [
        Target(
            name="claude-code",
            # Project-scope .mcp.json, which Claude Code auto-discovers.
            config_path=project_dir / ".mcp.json",
            detected=bool(shutil.which("claude") or os.environ.get("CLAUDECODE")),
            backup=False,
            instructions_path=project_dir / "CLAUDE.md",
        ),
        Target(
            name="claude-desktop",
            config_path=desktop_dir / "claude_desktop_config.json",
            detected=desktop_dir.is_dir(),
            backup=True,
            # Claude Desktop is a chat app with no per-project memory file.
            instructions_path=None,
        ),
        Target(
            name="cursor",
            # Project-scope .cursor/mcp.json, which Cursor auto-discovers.
            config_path=project_dir / ".cursor" / "mcp.json",
            detected=(home / ".cursor").is_dir() or (project_dir / ".cursor").is_dir(),
            backup=False,
            # A dedicated, always-applied Cursor rule (its own .mdc file).
            instructions_path=project_dir / ".cursor" / "rules" / "hdl-kgraph.mdc",
            instructions_frontmatter=(
                "---\ndescription: Query the hdl-kgraph design graph\nalwaysApply: true\n---\n\n"
            ),
        ),
        Target(
            name="codex",
            config_path=home / ".codex" / "config.toml",
            detected=bool(shutil.which("codex")) or (home / ".codex").is_dir(),
            backup=True,
            servers_key="mcp_servers",
            fmt="toml",
            instructions_path=project_dir / "AGENTS.md",
        ),
        Target(
            name="windsurf",
            config_path=home / ".codeium" / "windsurf" / "mcp_config.json",
            detected=(home / ".codeium" / "windsurf").is_dir(),
            backup=True,
            instructions_path=project_dir / ".windsurf" / "rules" / "hdl-kgraph.md",
        ),
        Target(
            name="gemini-cli",
            config_path=home / ".gemini" / "settings.json",
            detected=bool(shutil.which("gemini")) or (home / ".gemini").is_dir(),
            backup=True,
            instructions_path=project_dir / "GEMINI.md",
        ),
        Target(
            name="vscode",
            # Project-scope .vscode/mcp.json (VS Code / GitHub Copilot agent mode).
            config_path=project_dir / ".vscode" / "mcp.json",
            detected=bool(shutil.which("code")) or (project_dir / ".vscode").is_dir(),
            backup=False,
            servers_key="servers",
            entry_extras={"type": "stdio"},
            instructions_path=project_dir / ".github" / "copilot-instructions.md",
        ),
    ]


def load_config(path: Path) -> dict[str, object]:
    """Existing config JSON, ``{}`` if absent; ValueError if unusable."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: not valid JSON ({exc}); fix or remove it first") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at the top level")
    return data


def load_toml(path: Path) -> dict[str, object]:
    """Existing config TOML, ``{}`` if absent; ValueError if unusable."""
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: not valid TOML ({exc}); fix or remove it first") from exc


def _toml_value(value: object) -> str:
    # json.dumps string escaping (\", \\, \n, \uXXXX) is valid in TOML
    # basic strings too, so strings and lists of strings come for free.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + items + " }"
    raise ValueError(f"cannot render {type(value).__name__} as TOML")


def _toml_section(servers_key: str, entry: dict[str, object]) -> str:
    lines = [f"[{servers_key}.{SERVER_KEY}]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in entry.items()]
    return "\n".join(lines) + "\n"


def merged_toml_text(path: Path, servers_key: str, entry: dict[str, object]) -> str:
    """File text with our server section appended or replaced in place.

    The edit is textual — everything outside the ``[<servers_key>.hdl-kgraph]``
    section, comments included, is preserved verbatim. The result is parsed
    before being returned, so a merge that would corrupt the file aborts.
    """
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    section = _toml_section(servers_key, entry)
    header = re.compile(
        rf"^[ \t]*\[[ \t]*{re.escape(servers_key)}[ \t]*\.[ \t]*"
        rf"(?:\"{re.escape(SERVER_KEY)}\"|{re.escape(SERVER_KEY)})[ \t]*\][ \t]*(?:#.*)?$",
        re.MULTILINE,
    )
    match = header.search(text)
    if match:
        next_table = re.compile(r"^[ \t]*\[", re.MULTILINE).search(text, match.end())
        end = next_table.start() if next_table else len(text)
        new_text = text[: match.start()] + section + ("\n" if next_table else "") + text[end:]
    elif text:
        new_text = text + ("\n" if text.endswith("\n") else "\n\n") + section
    else:
        new_text = section
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"{path}: could not merge the {SERVER_KEY} entry automatically ({exc}); "
            f"add this section manually:\n{section}"
        ) from exc
    return new_text


def write_config(target: Target, entry: dict[str, object]) -> bool:
    """Merge *entry* into the target's config file. True if the file changed."""
    if target.fmt == "toml":
        existing = load_toml(target.config_path)
        servers = existing.get(target.servers_key)
        up_to_date = isinstance(servers, dict) and servers.get(SERVER_KEY) == target.full_entry(
            entry
        )
        if up_to_date and target.config_path.is_file():
            return False
        content = merged_toml_text(target.config_path, target.servers_key, target.full_entry(entry))
    else:
        existing = load_config(target.config_path)
        merged = target.merged_config(entry)
        if existing == merged and target.config_path.is_file():
            return False
        content = json.dumps(merged, indent=2) + "\n"
    if target.backup and target.config_path.is_file():
        backup_path = target.config_path.with_suffix(target.config_path.suffix + ".bak")
        if not backup_path.exists():
            shutil.copy2(target.config_path, backup_path)
    target.config_path.parent.mkdir(parents=True, exist_ok=True)
    target.config_path.write_text(content, encoding="utf-8")
    return True
