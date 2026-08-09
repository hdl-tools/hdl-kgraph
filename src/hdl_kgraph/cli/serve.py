"""hdl-kgraph CLI: serve and setup commands."""

from __future__ import annotations

from pathlib import Path

import click

from hdl_kgraph.cli._common import (
    CliError,
)
from hdl_kgraph.cli._options import (
    _db_option,
)
from hdl_kgraph.pipeline import (
    find_db,
)
from hdl_kgraph.storage.sqlite_store import SchemaVersionError, SqliteStore


@click.command()
@click.option(
    "--mcp",
    "mcp_mode",
    is_flag=True,
    help="Run the MCP server (the default and only mode; kept for compatibility).",
)
@_db_option
@click.option(
    "--http",
    "http_addr",
    metavar="HOST:PORT",
    default=None,
    help="Serve over streamable HTTP instead of stdio. Pass --token to require "
    "a bearer token; otherwise there is no authentication — the graph exposes "
    "your design's structure, so bind 127.0.0.1 unless every host is trusted.",
)
@click.option(
    "--token",
    "token",
    metavar="TOKEN",
    default=None,
    envvar="HDL_KGRAPH_MCP_TOKEN",
    help="Require this bearer token for the HTTP transport (clients send "
    "`Authorization: Bearer <token>`). Reads HDL_KGRAPH_MCP_TOKEN if unset; "
    "ignored for stdio.",
)
def serve(mcp_mode: bool, db_path: Path | None, http_addr: str | None, token: str | None) -> None:
    """Serve the knowledge graph to AI assistants over MCP (read-only).

    Speaks MCP on stdio by default (the transport assistant configs use);
    ``--http`` exposes the same tools over streamable HTTP instead. HTTP has
    no authentication unless you pass ``--token`` (or set
    ``HDL_KGRAPH_MCP_TOKEN``), so otherwise keep it bound to loopback (see
    docs/usage/mcp.md). The server only ever reads the database — rebuild with
    ``build``/``update`` (a running server picks up the new database
    automatically).
    """
    del mcp_mode  # MCP is the default and only mode; the flag is a no-op
    if db_path is None:
        db_path = find_db(Path.cwd())
        if db_path is None:
            raise CliError(
                "no .hdl-kgraph/graph.db found here or in any parent directory; "
                "run `hdl-kgraph build` first or pass --db"
            )
    if not db_path.is_file():
        raise CliError(f"database not found: {db_path}")
    try:
        SqliteStore(db_path).load_meta()  # schema check before the MCP handshake
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc

    from hdl_kgraph.mcp import McpUnavailableError, create_server

    # stdio is a local pipe with no network surface, so a token is meaningless
    # there; only wire auth into the HTTP transport.
    http_token = token if http_addr is not None else None
    try:
        server = create_server(db_path, token=http_token)
    except McpUnavailableError as exc:
        raise CliError(str(exc)) from exc
    if http_addr is None:
        server.run()
        return
    if http_addr.startswith("["):  # IPv6: [host]:port
        bracket = http_addr.find("]")
        if bracket == -1 or http_addr[bracket + 1 : bracket + 2] != ":":
            raise CliError(f"--http expects HOST:PORT, got {http_addr!r}")
        host, port_text = http_addr[: bracket + 1], http_addr[bracket + 2 :]
    else:  # IPv4 / hostname: host:port
        host, _, port_text = http_addr.rpartition(":")
    if not host or not port_text.isdigit() or not (1 <= int(port_text) <= 65535):
        raise CliError(f"--http expects HOST:PORT, got {http_addr!r}")
    if token is None and host not in ("127.0.0.1", "localhost", "::1", "[::1]"):
        click.echo(
            f"warning: serving on {host} exposes your design's structure to the "
            "network with no authentication; pass --token (or set "
            "HDL_KGRAPH_MCP_TOKEN), or bind 127.0.0.1 unless every host is "
            "trusted (see docs/usage/mcp.md)",
            err=True,
        )
    server.run(transport="http", host=host, port=int(port_text))


@click.command()
@_db_option
@click.option(
    "--assistant",
    "assistants",
    multiple=True,
    help="Only configure these assistants (repeatable; default: all detected).",
)
@click.option("--list", "list_only", is_flag=True, help="Only report what is detected.")
@click.option("--dry-run", is_flag=True, help="Show what would be written without writing.")
@click.option("--yes", "assume_yes", is_flag=True, help="Configure without confirmation prompts.")
@click.option(
    "--no-instructions",
    is_flag=True,
    help="Do not seed assistant instruction files (CLAUDE.md, AGENTS.md, …) "
    "with hdl-kgraph usage notes; only write the MCP server config.",
)
def setup(
    db_path: Path | None,
    assistants: tuple[str, ...],
    list_only: bool,
    dry_run: bool,
    assume_yes: bool,
    no_instructions: bool,
) -> None:
    """Detect installed AI assistants and configure them to use this graph.

    Writes (or updates) the ``hdl-kgraph`` MCP server entry in each detected
    assistant's config — project-scope files for Claude Code (``.mcp.json``),
    Cursor (``.cursor/mcp.json``), and VS Code (``.vscode/mcp.json``);
    user-level files for Claude Desktop, Codex (``~/.codex/config.toml``),
    Windsurf, and Gemini CLI. Unless ``--no-instructions`` is given, it also
    seeds each assistant's instruction file (``CLAUDE.md``, ``AGENTS.md``,
    ``GEMINI.md``, a Cursor/Windsurf rule, or ``.github/copilot-instructions.md``)
    with notes on querying the graph. Re-running is safe: the MCP entry and the
    managed instruction block are both updated in place and everything else in
    each file is preserved.
    """
    from hdl_kgraph.mcp.setup import (
        detect_targets,
        instructions_preview,
        plan_entry,
        write_config,
        write_instructions,
    )

    targets = detect_targets()
    if assistants:
        known = {t.name for t in targets}
        unknown = [a for a in assistants if a not in known]
        if unknown:
            raise CliError(
                f"unknown assistant(s) {', '.join(unknown)}; known: {', '.join(sorted(known))}"
            )
        targets = [t for t in targets if t.name in assistants]
    detected = [t for t in targets if t.detected]
    for target in targets:
        state = "detected" if target.detected else "not detected"
        click.echo(f"{target.name:15} {state}  ({target.config_path})")
    if list_only:
        return
    if not detected:
        raise CliError("no supported AI assistant detected; see docs/usage/mcp.md for manual setup")

    if db_path is None:
        db_path = find_db(Path.cwd())
        if db_path is None:
            raise CliError(
                "no .hdl-kgraph/graph.db found here or in any parent directory; "
                "run `hdl-kgraph build` first or pass --db"
            )
    if not db_path.is_file():
        raise CliError(f"database not found: {db_path}")
    entry = plan_entry(db_path.resolve())

    try:
        import fastmcp  # noqa: F401
    except ImportError:
        click.echo(
            "warning: fastmcp is not installed — the configured server will not "
            "start until you `pip install 'hdl-kgraph[mcp]'`",
            err=True,
        )

    for target in detected:
        # MCP server config (the machine-readable .mcp.json/config.toml entry).
        if dry_run:
            click.echo(f"would write {target.config_path}:")
            try:
                click.echo(target.preview(entry), nl=False)
            except ValueError as exc:
                raise CliError(str(exc)) from exc
        elif assume_yes or click.confirm(f"configure {target.name}?", default=True):
            try:
                changed = write_config(target, entry)
            except ValueError as exc:
                raise CliError(str(exc)) from exc
            click.echo(f"{target.config_path}: {'updated' if changed else 'already up to date'}")

        # Usage notes in the assistant's instruction file (decoupled from the
        # config above — useful even where the MCP server itself isn't wired up).
        if no_instructions or target.instructions_path is None:
            continue
        ipath = target.instructions_path
        if dry_run:
            click.echo(f"would write {ipath}:")
            click.echo(instructions_preview(target), nl=False)
        elif assume_yes or click.confirm(f"add hdl-kgraph usage notes to {ipath}?", default=True):
            changed = write_instructions(target)
            click.echo(f"{ipath}: {'updated' if changed else 'already up to date'}")
