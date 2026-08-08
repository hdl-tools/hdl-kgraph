"""hdl-kgraph CLI: The `query` subcommand group."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from hdl_kgraph.cli._common import (
    CliError,
    _resolve_db,
)
from hdl_kgraph.cli._options import (
    _db_option,
    _json_option,
)
from hdl_kgraph.cli.render import emit_json as _emit_json
from hdl_kgraph.graph import uvm
from hdl_kgraph.schema import NodeKind
from hdl_kgraph.storage.query import GraphQuery
from hdl_kgraph.storage.sqlite_store import SchemaVersionError


def _query(db_path: Path | None) -> GraphQuery:
    """A bounded reader over the resolved database (whole-design reports read it
    instead of full-loading the graph). Schema mismatch → a clean CLI error."""
    return GraphQuery(_resolve_db(db_path))


@click.group()
def query() -> None:
    """Query the knowledge graph."""


@query.command("instances-of")
@click.argument("name")
@_db_option
@_json_option
def instances_of(name: str, db_path: Path | None, as_json: bool) -> None:
    """List all instantiation sites of design units named NAME.

    Exits 1 if NAME matches nothing (a negative result, not an error).

    Answered from the bounded path (only NAME's nodes + incoming INSTANTIATES),
    never a full graph load.
    """
    try:
        records = _query(db_path).instances_of(name)
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(records)
        if not records:
            sys.exit(1)
        return
    if not records:
        click.echo(f"no instances of {name!r} found", err=True)
        sys.exit(1)
    for rec in records:
        marker = " [?]" if rec["target_unresolved"] else ""
        click.echo(
            f"{rec['qualified_name']}  {rec['file']}:{rec['line']}"
            f"  confidence={rec['confidence']:.1f}{marker}"
        )


@query.command("modules")
@_db_option
@_json_option
def modules(db_path: Path | None, as_json: bool) -> None:
    """List all modules and entities with their instantiation counts.

    Answered from the bounded path (units + their incoming INSTANTIATES counts),
    never a full graph load.
    """
    try:
        rows = _query(db_path).modules()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(rows)
        return
    for row in rows:
        marker = " [vhdl]" if row["kind"] is NodeKind.ENTITY else ""
        click.echo(
            f"{row['name'] + marker:30} {row['file']}:{row['line']}  instances={row['instances']}"
        )


@query.command("clock-domains")
@_db_option
@_json_option
def clock_domains_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report clock domains: clock nets, their processes and signals.

    Domains come from CLOCKED_BY edges (sensitivity-list evidence = 1.0,
    name heuristics = 0.4) with clock nets alias-merged across the
    hierarchy through single-identifier port connections.

    Answered from the bounded out-of-core summary (never a full graph load), so
    each domain reports its clock net name, aliases, and process/signal counts.
    """
    try:
        payload = _query(db_path).clock_domains()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(payload)
        return
    domains = payload["domains"]
    if not domains:
        click.echo("no clocked processes found")
        return
    for domain in domains:
        label = domain["clock"]
        aliases = [n for n in domain["aliases"] if n != domain["clock"]]
        if aliases:
            label += " (= " + ", ".join(aliases) + ")"
        confidence = domain["min_confidence"]
        marker = "" if confidence >= 0.8 else f"  [~{confidence:.1f}]"
        click.echo(f"{label}{marker}")
        click.echo(f"    processes: {domain['process_count']}")
        click.echo(f"    signals driven: {domain['signal_count']}")


@query.command("reset-tree")
@_db_option
@_json_option
def reset_tree_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report reset nets and the processes they reset.

    Answered from the bounded out-of-core scan (RESETS edges + net aliases), never
    a full graph load; each net is labelled by name (the reset processes' qualified
    names are resolved with a bounded lookup).
    """
    q = _query(db_path)
    try:
        groups = q.reset_tree()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(groups)
        return
    if not groups:
        click.echo("no resets found")
        return
    proc_names = q.qualified_names([proc for g in groups for proc in g["process_ids"]])
    for group in groups:
        names = group["reset_names"]
        label = names[0]
        aliases = [n for n in names if n != names[0]]
        if aliases:
            label += " (= " + ", ".join(aliases) + ")"
        flavor = "async" if group["is_async"] else "sync (name heuristic)"
        conf = group["min_confidence"]
        marker = "" if conf >= 0.8 else f"  [~{conf:.1f}]"
        click.echo(f"{label}  {flavor}{marker}")
        for proc in group["process_ids"]:
            click.echo(f"    resets {proc_names.get(proc, proc)}")


@query.command("cdc")
@_db_option
@_json_option
def cdc_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report clock-domain-crossing suspects.

    A suspect is a signal driven in one domain and read by a process in
    another. Synchronizers are not recognized — review each finding (this
    is a report, not a gate; the exit code is always 0).

    Answered from the bounded out-of-core summary (the top 50 suspects), never a
    full graph load.
    """
    query = _query(db_path)
    try:
        suspects = query.clock_domains()["cdc_suspects"]
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(suspects)
        return
    if not suspects:
        click.echo("no CDC suspects found")
        return
    readers = query.qualified_names([s["reader_id"] for s in suspects])
    for s in suspects:
        location = f"{s['file']}:{s['line']}" if s["file"] else "?"
        click.echo(
            f"{s['signal_name']:24} {s['driver_domain']} -> {s['reader_domain']}"
            f"  read by {readers.get(s['reader_id'], s['reader_id'])}"
            f"  {location}  confidence={s['confidence']:.1f}"
        )


@query.command("drivers")
@click.argument("signal")
@_db_option
@_json_option
@click.option("--readers", is_flag=True, help="List readers instead of drivers.")
@click.option("--module", default=None, help="Only signals inside this design unit.")
def drivers_cmd(
    signal: str, db_path: Path | None, as_json: bool, readers: bool, module: str | None
) -> None:
    """List what drives (or reads) signals named SIGNAL.

    Exits 1 if SIGNAL matches nothing (a negative result, not an error).

    Answered from the bounded path (only SIGNAL's nodes + their DRIVES/READS
    fanout), never a full graph load.
    """
    try:
        records = _query(db_path).signal_drivers(signal, module, readers)
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(records)
        if not records:
            # Empty is a documented negative result (exit 1), in JSON as in text —
            # matches `instances-of`; the JSON body is still emitted ([]).
            sys.exit(1)
        return
    if not records:
        verb = "reads" if readers else "drives"
        click.echo(f"nothing {verb} a signal named {signal!r}", err=True)
        sys.exit(1)
    for rec in records:
        click.echo(
            f"{rec['signal']:30} <- {rec['site_kind']} {rec['site']}"
            f"  {rec['file']}:{rec['line']}  confidence={rec['confidence']:.1f}"
        )


@query.command("uvm")
@_db_option
@_json_option
def uvm_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report UVM topology: components by role, plus TEST_COVERS links.

    Classes are classified by walking their EXTENDS chain to the first
    uvm_* base (usually an unresolved stub — UVM itself is rarely parsed).

    Answered from the bounded class subgraph (never a full graph load).
    """
    try:
        payload = _query(db_path).uvm_topology()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    components = payload["components"]
    covers = payload["test_covers"]
    if as_json:
        _emit_json(payload)
        return
    if not components and not covers:
        click.echo("no UVM components or testbench tops found")
        return
    for role in uvm.ROLE_ORDER:
        members = [c for c in components if c["role"] == role]
        if not members:
            continue
        click.echo(f"{role}:")
        for c in members:
            chain = " -> ".join(c["base_chain"])
            click.echo(f"    {c['name']:28} {c['file']}:{c['line']}  ({chain})")
    if covers:
        click.echo("test coverage (name-pattern heuristic, 0.4):")
        for cover in covers:
            click.echo(f"    {cover['test']} covers {cover['dut']}")


@query.command("power-domains")
@_db_option
@_json_option
def power_domains_cmd(db_path: Path | None, as_json: bool) -> None:
    """Report UPF power domains: elements and isolation/retention strategies.

    Each ``create_power_domain`` lists its resolved element instances and the
    strategies that name it. Answered from the bounded power-domain subgraph
    (never a full graph load).
    """
    try:
        payload = _query(db_path).power_domains()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    domains = payload["domains"]
    if as_json:
        _emit_json(payload)
        return
    if not domains:
        click.echo("no UPF power domains found")
        return
    for d in domains:
        flags = " [isolated]" if any(s["kind"] == "isolation" for s in d["strategies"]) else ""
        click.echo(f"{d['name']}{flags}  {d['file']}:{d['line']}")
        for element in d["elements"]:
            click.echo(f"    element {element}")
        for strategy in d["strategies"]:
            click.echo(f"    {strategy['kind']} {strategy.get('name', '')}".rstrip())


@query.command("unresolved")
@_db_option
@_json_option
def unresolved(db_path: Path | None, as_json: bool) -> None:
    """List unresolved stub nodes and who references them.

    Answered from the bounded path (scans for unresolved nodes + hydrates only
    their referrer edges), never a full graph load.
    """
    try:
        stubs = _query(db_path).unresolved_stubs()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json(stubs)
        return
    if not stubs:
        click.echo("no unresolved references")
        return
    for stub in stubs:
        click.echo(f"{stub['kind'].value}:{stub['name']}")
        for referrer in stub["referrers"]:
            click.echo(f"    <- {referrer}")
