"""hdl-kgraph CLI: Query, status, lint, metrics, visualize, export, and tree commands."""

from __future__ import annotations

import dataclasses
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import click

from hdl_kgraph.cli._common import (
    CliError,
    _load,
    _resolve_db,
)
from hdl_kgraph.cli._options import (
    _db_option,
    _input_options,
    _json_option,
    _resolve_options,
)
from hdl_kgraph.cli.render import emit_json as _emit_json
from hdl_kgraph.config import (
    BuildConfig,
    ConfigError,
    LintWaiver,
    find_config,
    load_waivers,
)
from hdl_kgraph.discovery import SUFFIXES
from hdl_kgraph.enrich import summarize_enrichment
from hdl_kgraph.export import EXPORT_FORMATS
from hdl_kgraph.graph import analysis, lint, metrics
from hdl_kgraph.incremental import dirty_closure
from hdl_kgraph.pipeline import (
    default_db_path,
    find_db,
    scan_changes,
)
from hdl_kgraph.review import build_review_digest
from hdl_kgraph.schema import Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SchemaVersionError, SqliteStore
from hdl_kgraph.vcs import detect_vcs, detect_vcs_changes


def _echo_file_diagnostics(files: list, as_json: bool = False) -> None:
    """``status --errors``: per-file parse errors, warnings, skip reasons."""
    flagged = sorted(
        (f for f in files if f.skipped_reason is not None or f.parse_error_count or f.warnings),
        key=lambda f: f.path,
    )
    if as_json:
        _emit_json(
            [
                {
                    "path": f.path,
                    "parse_errors": f.parse_error_count,
                    "errors": f.parse_errors,
                    "skipped_reason": f.skipped_reason,
                    "warnings": f.warnings,
                }
                for f in flagged
            ]
        )
        return
    for f in flagged:
        if f.skipped_reason is not None:
            click.echo(f"{f.path}: skipped ({f.skipped_reason})")
            continue
        if f.parse_error_count:
            click.echo(f"{f.path}: {f.parse_error_count} parse error(s)")
            for detail in f.parse_errors:
                click.echo(f"  {detail}")
            if f.parse_errors and f.parse_error_count > len(f.parse_errors):
                click.echo(f"  ... and {f.parse_error_count - len(f.parse_errors)} more")
        for warning in f.warnings:
            click.echo(f"{f.path}: warning: {warning}")
    if not flagged:
        click.echo("no parse errors, preprocessor warnings, or skipped files")


@click.command("detect-changes")
@_input_options
@_json_option
@click.option(
    "--git",
    "git_ref",
    is_flag=False,
    flag_value="HEAD",
    default=None,
    metavar="[REF]",
    help="Diff the working tree against a git ref (default HEAD) instead of "
    "the last build's content hashes.",
)
@click.option(
    "--svn",
    "svn_rev",
    is_flag=False,
    flag_value="BASE",
    default=None,
    metavar="[REV]",
    help="Diff the svn working copy against a revision (default BASE).",
)
@click.option(
    "--p4",
    "p4_rev",
    is_flag=False,
    flag_value="have",
    default=None,
    metavar="[CL]",
    help="List Perforce workspace changes (opened + reconciled local edits).",
)
@click.option(
    "--vcs",
    "use_vcs",
    is_flag=True,
    help="Diff against the repo's auto-detected VCS (git/svn/p4) vs its "
    "default ref, instead of the last build's content hashes.",
)
@click.option(
    "--closure",
    is_flag=True,
    help="Also list unchanged files dirtied through include/macro dependencies.",
)
def detect_changes(
    source: Path,
    db_path: Path | None,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
    as_json: bool,
    git_ref: str | None,
    svn_rev: str | None,
    p4_rev: str | None,
    use_vcs: bool,
    closure: bool,
    no_auto_incdir: bool,
) -> None:
    """List build inputs that changed since the last build (or a VCS ref).

    Prints one ``M``/``A``/``D`` line per modified/added/deleted file
    (``~`` for closure-dirtied files with --closure). Diff against version
    control with --git/--svn/--p4 (each takes an optional ref), or --vcs to
    auto-detect which VCS the tree uses. Exit codes follow the
    ``git diff --exit-code`` convention so scripts can tell the cases apart:
    0 nothing changed, 1 changes detected, 2 error (missing database, bad
    config, ...).
    """
    # Errors must not exit 1 — scripts read 1 as "changed", per the docstring.
    try:
        options = _resolve_options(
            source,
            filelists,
            defines,
            incdirs,
            libs,
            config_path,
            no_config,
            excludes,
            max_file_size,
            no_auto_incdir,
        )
        base = source.resolve()
        base = base.parent if base.is_file() else base
        explicit = [
            (vcs, ref)
            for vcs, ref in (("git", git_ref), ("svn", svn_rev), ("p4", p4_rev))
            if ref is not None
        ]
        if use_vcs and explicit:
            raise CliError("--vcs cannot be combined with --git/--svn/--p4")
        if len(explicit) > 1:
            raise CliError("choose only one of --git/--svn/--p4")
        if explicit or use_vcs:
            if explicit:
                vcs, ref = explicit[0]
            else:  # bare --vcs: auto-detect the tree's VCS, default ref
                detected = detect_vcs(base)
                if detected is None:
                    raise CliError(
                        "could not detect a VCS (no .git/.svn, no P4 environment); "
                        "pass --git/--svn/--p4"
                    )
                vcs, ref = detected, None
            try:
                changes = detect_vcs_changes(base, vcs, ref, SUFFIXES)
            except RuntimeError as exc:
                raise CliError(str(exc)) from exc
        else:
            if db_path is None:
                db_path = default_db_path(base)
            if not db_path.is_file():
                raise CliError(
                    f"no database at {db_path}; run `hdl-kgraph build` first, "
                    "or use --git/--svn/--p4/--vcs"
                )
            try:
                changes = scan_changes(source, db_path, options)
            except SchemaVersionError as exc:
                raise CliError(str(exc)) from exc
        dirtied: list[dict[str, str]] = []
        if closure and (changes.changed or changes.removed):
            graph, _, _ = _load(db_path if db_path is not None else find_db(base))
            seeds = {path: "" for path in (*changes.changed, *changes.removed)}
            dirtied = [
                {"path": path, "via": why}
                for path, why in sorted(dirty_closure(graph, seeds).items())
                if path not in seeds
            ]
    except CliError:
        raise
    except click.ClickException as exc:
        # Coerce click's own usage/validation errors to the exit-2 policy
        # (CliError already exits 2; re-wrap rather than mutate the instance,
        # which newer click types as a non-assignable class variable).
        raise CliError(exc.format_message()) from exc
    if as_json:
        payload: dict[str, Any] = {
            "changed": changes.changed,
            "added": changes.added,
            "removed": changes.removed,
        }
        if closure:
            payload["closure"] = dirtied
        _emit_json(payload)
    else:
        for path in changes.changed:
            click.echo(f"M {path}")
        for path in changes.added:
            click.echo(f"A {path}")
        for path in changes.removed:
            click.echo(f"D {path}")
        for entry in dirtied:
            click.echo(f"~ {entry['path']} ({entry['via']})")
    if changes:
        sys.exit(1)


@click.command()
@click.argument("target")
@_db_option
@_json_option
@click.option(
    "--max-depth",
    type=int,
    default=0,
    help="Limit the dependency distance reported (0 = unlimited).",
)
@click.option(
    "--files",
    "show_files",
    is_flag=True,
    help="List the affected files instead of design units.",
)
def impact(
    target: str, db_path: Path | None, as_json: bool, max_depth: int, show_files: bool
) -> None:
    """Show what a change to TARGET (a file path or design unit) affects.

    Walks reverse INSTANTIATES/IMPORTS/INCLUDES/EXTENDS (plus VHDL
    USES_PACKAGE/IMPLEMENTS/BINDS and macro use) edges transitively: the
    instantiating parents, importers, includers, and subclasses that a
    change to TARGET can break.
    """
    graph, files, _ = _load(db_path)
    seeds = analysis.impact_seeds(graph, files, target)
    if not seeds:
        raise CliError(f"{target!r} matches no file or design unit in the graph")
    records = analysis.impact_radius(graph, seeds, max_depth=max_depth)
    if show_files:
        affected = sorted({r.file for r in records if r.file})
        if as_json:
            _emit_json(affected)
            return
        for path in affected:
            click.echo(path)
        return
    if as_json:
        _emit_json(records)
        return
    if not records:
        click.echo("no dependents found")
        return
    for r in records:
        location = f"{r.file}:{r.line}" if r.file and r.line else r.file
        click.echo(
            f"{r.kind.value:13} {r.name:30} {location:30} <- {r.via.value} (depth {r.depth})"
        )


@click.command()
@_db_option
@_json_option
@click.option(
    "--errors",
    "show_errors",
    is_flag=True,
    help="List files with parse errors, preprocessor warnings, and skipped "
    "files (with reasons) instead of the statistics.",
)
def status(db_path: Path | None, as_json: bool, show_errors: bool) -> None:
    """Show graph statistics for the current build."""
    graph, files, meta = _load(db_path)
    if show_errors:
        _echo_file_diagnostics(files, as_json)
        return
    # Filelists are recorded for M4 incremental rebuilds but are not parsed
    # HDL sources; report them on their own line.
    parsed = [f for f in files if f.skipped_reason is None and f.language is not Language.UNKNOWN]
    filelists = [f for f in files if f.skipped_reason is None and f.language is Language.UNKNOWN]
    skipped = Counter(f.skipped_reason for f in files if f.skipped_reason is not None)
    error_files = [f for f in parsed if f.parse_error_count]
    warning_count = sum(len(f.warnings) for f in files)
    total_errors = sum(f.parse_error_count for f in error_files)
    node_kinds = analysis.node_kind_histogram(graph)
    edge_kinds = analysis.edge_kind_histogram(graph)
    stubs = analysis.unresolved_stubs(graph)
    if as_json:
        _emit_json(
            {
                "root": meta.get("root"),
                "built_at": meta.get("built_at"),
                "tool_version": meta.get("tool_version"),
                "files": {
                    "parsed": len(parsed),
                    "filelists": len(filelists),
                    "skipped": dict(skipped),
                    "parse_errors": total_errors,
                    "error_files": len(error_files),
                    "preproc_warnings": warning_count,
                },
                "nodes": {"total": graph.number_of_nodes(), "kinds": dict(node_kinds)},
                "edges": {"total": graph.number_of_edges(), "kinds": dict(edge_kinds)},
                "unresolved": len(stubs),
            }
        )
        return
    click.echo(f"root:     {meta.get('root', '?')}")
    click.echo(f"built at: {meta.get('built_at', '?')} (hdl-kgraph {meta.get('tool_version')})")
    click.echo(f"files:    {len(parsed)} parsed")
    if filelists:
        click.echo(f"          {len(filelists)} filelist(s)")
    for reason, count in sorted(skipped.items()):
        click.echo(f"          {count} skipped ({reason})")
    if error_files:
        click.echo(f"          {total_errors} parse error(s) in {len(error_files)} file(s)")
    if warning_count:
        click.echo(f"          {warning_count} preprocessor warning(s)")
    if error_files or warning_count:
        click.echo("          (`hdl-kgraph status --errors` lists them per file)")
    click.echo(f"nodes:    {graph.number_of_nodes()}")
    for kind, count in node_kinds.most_common():
        click.echo(f"          {count:6} {kind}")
    click.echo(f"edges:    {graph.number_of_edges()}")
    for kind, count in edge_kinds.most_common():
        click.echo(f"          {count:6} {kind}")
    if stubs:
        click.echo(f"unresolved: {len(stubs)}")


@click.command()
@_db_option
@_json_option
@click.option(
    "--metrics",
    "with_metrics",
    is_flag=True,
    help="Include graph metrics (fan-in/hubs/communities); loads the whole graph.",
)
def review(db_path: Path | None, as_json: bool, with_metrics: bool) -> None:
    """Emit a content-free review digest — counts, ratios, distributions, timings;
    no names, paths, or expressions.

    Designed to be snapshotted out of an isolated/air-gapped environment (where the
    source and graph.db cannot leave) and diffed across builds to review parse
    health, link quality, design shape, and performance. ``--json`` (recommended)
    prints the full digest; otherwise a short summary. ``--metrics`` adds
    betweenness/community metrics (loads the whole graph).
    """
    db = _resolve_db(db_path)
    store = SqliteStore(db)
    try:
        graph, files, meta = store.load()
        clock_payload = store.load_summary("clock_domains")
        uvm_payload = store.load_summary("uvm_topology")
        power_payload = store.load_summary("power_domains")
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    digest = build_review_digest(
        graph,
        files,
        meta,
        db_bytes=db.stat().st_size if db.exists() else None,
        clock_summary_payload=clock_payload,
        uvm_summary_payload=uvm_payload,
        power_summary_payload=power_payload,
        with_metrics=with_metrics,
    )
    if as_json:
        _emit_json(digest)
        return
    g = digest["graph"]
    lq = digest["link_quality"]
    a = digest["analyses"]
    click.echo(f"hdl-kgraph review (schema {digest['schema']}, content-free)")
    click.echo(
        f"  nodes {g['node_count']}  edges {g['edge_count']}  "
        f"unresolved {lq['unresolved_stub_count']} ({lq['unresolved_stub_ratio']:.2%})"
    )
    cdc_line = (
        f"  clock domains {a['clock_domains']['count']}  cdc suspects {a['cdc']['suspect_count']}"
    )
    if a["cdc"].get("suppressed_count"):
        cdc_line += f" ({a['cdc']['suppressed_count']} SDC-suppressed)"
    click.echo(cdc_line)
    if a.get("power", {}).get("domain_count"):
        p = a["power"]
        click.echo(f"  power domains {p['domain_count']}  isolated {p['isolated_count']}")
    timings = digest["timings_s"]
    if timings:
        click.echo("  timings(s): " + "  ".join(f"{k[:-2]} {v:.2f}" for k, v in timings.items()))
    click.echo("  (use --json for the full content-free digest)")


@click.command()
@_db_option
@_json_option
def discrepancies(db_path: Path | None, as_json: bool) -> None:
    """List where native-frontend elaboration disagreed with the heuristic graph.

    Populated by ``build --enrich`` (M7). Each finding names the kind of
    disagreement (e.g. ``instance_count`` for a generate loop the tree-sitter
    tier counted as one instance), the design node, and the heuristic vs
    elaborated values.
    """
    try:
        items = SqliteStore(_resolve_db(db_path)).load_discrepancies()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    if as_json:
        _emit_json([dataclasses.asdict(d) for d in items])
        return
    if not items:
        click.echo("no discrepancies (run `hdl-kgraph build --enrich` to record them)")
        return
    by_kind = Counter(d.kind for d in items)
    click.echo(f"{len(items)} discrepancy finding(s):")
    for kind, count in by_kind.most_common():
        click.echo(f"  {count:6} {kind}")
    for d in items:
        click.echo(f"[{d.kind}] {d.detail} (via {d.backend})")
        if d.heuristic or d.elaborated:
            click.echo(f"    heuristic: {d.heuristic or '-'}  elaborated: {d.elaborated or '-'}")


@click.command()
@_db_option
@_json_option
def enriched(db_path: Path | None, as_json: bool) -> None:
    """Report exactly what ``build --enrich`` added vs the default build.

    Reconstructed from the stored graph's elaboration stamps (M7): heuristic
    edges promoted to elaboration-accurate facts, elaborated nodes/edges added
    by unrolling generate loops and instance arrays, and the discrepancies where
    elaboration disagreed with the heuristic graph. Read-only — no rebuild.
    """
    try:
        store = SqliteStore(_resolve_db(db_path))
        graph, _files, _meta = store.load()
        items = store.load_discrepancies()
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc
    summary = summarize_enrichment(graph)
    if as_json:
        _emit_json(
            {
                "summary": dataclasses.asdict(summary),
                "discrepancies": [dataclasses.asdict(d) for d in items],
            }
        )
        return
    if not summary.enriched and not items:
        click.echo("not enriched (run `hdl-kgraph build --enrich`)")
        return
    backends = ", ".join(summary.backends) or "(none)"
    click.echo(f"enrichment via {backends}:")
    click.echo(f"  edges upgraded:     {summary.edges_upgraded}")
    click.echo(f"  edges added:        {summary.edges_added}")
    click.echo(f"  nodes added:        {summary.nodes_added}")
    click.echo(f"  generates unrolled: {summary.generates_unrolled}")
    click.echo(f"  discrepancies:      {len(items)}")
    if not items:
        return
    by_kind = Counter(d.kind for d in items)
    for kind, count in by_kind.most_common():
        click.echo(f"    {count:6} {kind}")
    for d in items:
        click.echo(f"[{d.kind}] {d.detail} (via {d.backend})")
        if d.heuristic or d.elaborated:
            click.echo(f"    heuristic: {d.heuristic or '-'}  elaborated: {d.elaborated or '-'}")


def _lint_config(
    db_path: Path | None, meta: dict[str, str], config_path: Path | None, no_config: bool
) -> BuildConfig:
    """The config whose ``[lint]`` section (and ``[build].top``) lint honors."""
    if no_config:
        return BuildConfig()
    if config_path is None:
        root = Path(meta.get("root") or "")
        if not root.is_dir():  # project moved since the build: the DB is ground truth
            root = db_path.parent.parent if db_path is not None else Path.cwd()
        config_path = find_config(root)
        if config_path is None:
            return BuildConfig()
    try:
        return BuildConfig.load(config_path)
    except ConfigError as exc:
        raise CliError(str(exc)) from exc


def _waiver_desc(waiver: LintWaiver) -> str:
    parts = [f"check={waiver.check}"]
    parts += [
        f"{key}={getattr(waiver, key)}"
        for key in ("name", "module", "file", "line")
        if getattr(waiver, key) is not None
    ]
    return ", ".join(parts)


def _lint_row(f: lint.LintFinding, suffix: str = "") -> str:
    location = f"{f.file}:{f.line}" if f.file else "?"
    return f"{f.check:18} {f.name:32} {location:28} {f.message}{suffix}"


@click.command("lint")
@_db_option
@_json_option
@click.option(
    "--check",
    "checks",
    multiple=True,
    metavar="NAME",
    help=f"Run only this check (repeatable). Available: {', '.join(sorted(lint.CHECKS))}.",
)
@click.option(
    "--top",
    "tops",
    multiple=True,
    metavar="NAME",
    help="Treat NAME as an intended top module (repeatable, additive with "
    "[build].top; exempts it from dead-module).",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Path to hdl-kgraph.toml (default: nearest one from the build root upward).",
)
@click.option("--no-config", is_flag=True, help="Ignore any hdl-kgraph.toml.")
@click.option(
    "--waiver-file",
    "waiver_files",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help="Apply the [[lint.waivers]] entries of this TOML file too "
    "(repeatable; after config waivers).",
)
@click.option("--show-waived", is_flag=True, help="List the waived findings as well.")
def lint_cmd(
    db_path: Path | None,
    as_json: bool,
    checks: tuple[str, ...],
    tops: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    waiver_files: tuple[Path, ...],
    show_waived: bool,
) -> None:
    """Report graph-level lint findings (always exits 0 — a report, not a gate).

    Signal-level checks skip files with parse errors and implicit-net stubs
    so a finding is worth reading; confidences below 1.0 mark heuristics.
    Known-benign findings can be waived via [[lint.waivers]] in
    hdl-kgraph.toml or a --waiver-file; waivers that match nothing are
    reported stale on stderr.
    """
    graph, files, meta = _load(db_path)
    config = _lint_config(db_path, meta, config_path, no_config)
    waiver_warnings = list(config.warnings)
    waivers = list(config.lint_waivers)
    for path in waiver_files:
        try:
            waivers.extend(load_waivers(path, waiver_warnings))
        except ConfigError as exc:
            raise CliError(str(exc)) from exc
    for warning in waiver_warnings:
        click.echo(f"warning: {warning}", err=True)
    error_files = frozenset(f.path for f in files if f.parse_error_count)
    try:
        findings = lint.run_checks(
            graph,
            names=checks or None,
            tops=frozenset(tops) | frozenset(config.top),
            error_files=error_files,
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    result = lint.apply_waivers(findings, waivers, checks or None)
    for i in result.unknown:
        click.echo(
            f"warning: lint waiver #{i + 1} names unknown check '{waivers[i].check}'", err=True
        )
    for i in result.unused:
        click.echo(
            f"warning: lint waiver #{i + 1} ({_waiver_desc(waivers[i])}) matched nothing",
            err=True,
        )
    if as_json:
        _emit_json(
            {
                "findings": result.kept,
                "waived": result.waived,
                "unused_waivers": result.unused,
                "counts": {"findings": len(result.kept), "waived": len(result.waived)},
            }
        )
        return
    for f in result.kept:
        marker = "" if f.confidence >= 0.8 else f"  [~{f.confidence:.1f}]"
        click.echo(_lint_row(f, marker))
    if show_waived and result.waived:
        click.echo("waived:")
        for wf in result.waived:
            click.echo(_lint_row(wf.finding, f"  [waived: {wf.reason}]"))
    if waivers:
        if result.kept:
            click.echo(f"{len(result.kept)} finding(s), {len(result.waived)} waived")
        else:
            click.echo(f"no findings ({len(result.waived)} waived)")
    elif not result.kept:
        click.echo("no findings")


@click.command("metrics")
@_db_option
@_json_option
# --limit, not --top: --top means "top module" in lint/visualize, and this is
# a row count. Renamed before 1.0 freezes the surface (issue #22).
@click.option(
    "-n",
    "--limit",
    "top_n",
    type=int,
    default=10,
    show_default=True,
    metavar="N",
    help="How many units to list (0 = all).",
)
@click.option(
    "--communities",
    "show_communities",
    is_flag=True,
    help="Also report Louvain communities (subsystem suggestions).",
)
def metrics_cmd(db_path: Path | None, as_json: bool, top_n: int, show_communities: bool) -> None:
    """Module fan-in/fan-out, hub/bridge detection, community discovery.

    Metrics are computed on the module-level instantiation projection;
    units are listed hubs-first (descending betweenness centrality).
    """
    graph, _, _ = _load(db_path)
    result = metrics.module_metrics(graph)
    records = result.modules
    parts = metrics.communities(graph) if show_communities else []
    if as_json:
        payload: dict[str, Any] = {
            "modules": records,
            "betweenness_approximate": result.betweenness_approximate,
        }
        if show_communities:
            payload["communities"] = parts
        _emit_json(payload)
        return
    if not records:
        click.echo("no design units in the graph")
        return
    shown = records if top_n <= 0 else records[:top_n]
    click.echo(f"{'unit':30} {'fan-in':>6} {'fan-out':>7} {'betweenness':>12}")
    for m in shown:
        markers = ""
        if m.is_articulation:
            markers += " [bridge]"
        if m.unresolved:
            markers += " [?]"
        click.echo(f"{m.name:30} {m.fan_in:>6} {m.fan_out:>7} {m.betweenness:>12.4f}{markers}")
    if result.betweenness_approximate:
        click.echo(
            f"note: betweenness sampled (k={metrics.BETWEENNESS_SAMPLES}, "
            f"seed {metrics.BETWEENNESS_SEED}) — graph exceeds "
            f"{metrics.BETWEENNESS_EXACT_MAX_NODES} units"
        )
    if show_communities:
        click.echo(f"communities: {len(parts)}")
        for i, part in enumerate(parts):
            names = ", ".join(sorted(graph.nodes[n]["name"] for n in part))
            click.echo(f"    [{i}] {names}")


@click.command("visualize")
@_db_option
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(path_type=Path),
    default=Path("graph.html"),
    show_default=True,
    help="Output HTML file.",
)
@click.option(
    "--full",
    is_flag=True,
    help="Embed every node and edge (default: the module-level projection, "
    "which stays responsive on large designs).",
)
@click.option(
    "--top",
    "top",
    default=None,
    help="Root the hierarchy and graph views at this module (constrains both to its subtree).",
)
@click.option("--title", default=None, help="Page title (default: the build root's name).")
@click.option(
    "--layout",
    type=click.Choice(["auto", "live", "static"]),
    default="auto",
    show_default=True,
    help="Layout tier: 'live' runs the in-browser force simulation; 'static' "
    "ships precomputed coordinates (needs the [layout] extra); 'auto' routes "
    "by graph size.",
)
@click.option(
    "--force-inline",
    "force_inline",
    is_flag=True,
    help="Write the HTML even if the payload exceeds the inline size limit.",
)
@click.option(
    "--collapse",
    is_flag=True,
    help="Aggregate into one supernode per community (double-click to expand in "
    "the browser). With --full it is two-level: communities of units, each "
    "expandable to its leaf nodes.",
)
@click.option(
    "--kinds",
    "include_kinds",
    multiple=True,
    metavar="KIND",
    help="Plot only these node kinds (repeatable, e.g. --kinds module --kinds "
    "instance); layout is solved over just these. Most useful with --full.",
)
@click.option(
    "--exclude-kinds",
    "exclude_kinds",
    multiple=True,
    metavar="KIND",
    help="Drop these node kinds before plotting (repeatable, e.g. "
    "--exclude-kinds signal --exclude-kinds port).",
)
@click.option("--open", "open_browser", is_flag=True, help="Open the result in a browser.")
def visualize(
    db_path: Path | None,
    output: Path,
    full: bool,
    top: str | None,
    title: str | None,
    layout: str,
    force_inline: bool,
    collapse: bool,
    include_kinds: tuple[str, ...],
    exclude_kinds: tuple[str, ...],
    open_browser: bool,
) -> None:
    """Render a self-contained interactive HTML view of the graph.

    The file embeds D3 and the graph data — no network access needed to
    open it. Two views: a collapsible hierarchy and a force-directed graph
    with node-kind / edge-kind / community filters (and colour-by-community).
    Large designs route to
    a precomputed 'static' layout so the graph view paints without a
    client-side simulation freeze; ``--collapse`` shows one supernode per
    subsystem instead of every unit (see docs/viz-scalability.md).

    ``--kinds`` / ``--exclude-kinds`` restrict the plot to the node kinds of
    interest so the layout is solved over a smaller, more compact graph (e.g.
    ``--kinds module --kinds instance`` or ``--exclude-kinds signal``).
    """
    from hdl_kgraph.viz import render_html

    valid_kinds = {k.value for k in NodeKind}
    unknown = sorted({k for k in (*include_kinds, *exclude_kinds) if k not in valid_kinds})
    if unknown:
        raise CliError(
            f"unknown node kind(s): {', '.join(unknown)}. "
            f"Valid kinds: {', '.join(sorted(valid_kinds))}"
        )

    graph, _, meta = _load(db_path)
    if title is None:
        root = meta.get("root", "")
        title = f"hdl-kgraph: {Path(root).name}" if root else "hdl-kgraph"
    try:
        result = render_html(
            graph,
            output,
            full=full,
            top=top,
            title=title,
            layout=layout,
            force_inline=force_inline,
            collapse=collapse,
            include_kinds=frozenset(include_kinds) if include_kinds else None,
            exclude_kinds=frozenset(exclude_kinds),
        )
    except ValueError as exc:  # --top names nothing: error out, like `tree`
        raise CliError(str(exc)) from exc
    if result.note:
        click.echo(result.note, err=True)
    click.echo(f"wrote {result.path}")
    if open_browser:
        import webbrowser

        webbrowser.open(result.path.resolve().as_uri())


@click.command("export")
@_db_option
@click.option(
    "-o",
    "--output",
    "output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file (default: graph.<format>).",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(EXPORT_FORMATS),
    default="graphml",
    show_default=True,
    help="Interchange format: 'graphml'/'gexf' for Gephi & Cytoscape, 'json' for node-link data.",
)
def export_cmd(db_path: Path | None, output: Path | None, fmt: str) -> None:
    """Export the graph to GraphML/GEXF/JSON for external tools.

    The escape hatch for designs too large for the inline HTML artifact:
    Gephi (OpenOrd/ForceAtlas2) and Cytoscape handle graphs the browser
    cannot (see docs/viz-scalability.md).
    """
    from hdl_kgraph.export import export_graph

    graph, _, _ = _load(db_path)
    if output is None:
        output = Path(f"graph.{fmt}")
    try:
        path = export_graph(graph, output, fmt)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    click.echo(f"wrote {path}")


@click.command()
@click.argument("top", required=False)
@click.option("--depth", type=int, default=64, show_default=True, help="Maximum tree depth.")
@_db_option
@_json_option
def tree(top: str | None, depth: int, db_path: Path | None, as_json: bool) -> None:
    """Print the design hierarchy from TOP (default: every top module)."""
    graph, _, _ = _load(db_path)
    if top is not None:
        roots = analysis.resolve_unit(graph, top)
        if not roots:
            raise CliError(f"module or entity {top!r} not found in the graph")
    else:
        roots = analysis.find_top_modules(graph)
        if not roots:
            raise CliError("no top modules found")

    trees = [analysis.hierarchy_tree(graph, root, max_depth=depth) for root in roots]
    if as_json:
        _emit_json(trees)
        return
    for hierarchy in trees:
        _print_tree(hierarchy, prefix="", is_last=True)


def _print_tree(node: analysis.HierarchyNode, prefix: str, is_last: bool) -> None:
    # A VHDL entity shows the architecture its children came from: alu(rtl).
    unit = node.module_name + (f"({node.architecture})" if node.architecture else "")
    if node.instance_name is None:
        label = unit
    else:
        connector = "`-- " if is_last else "|-- "
        label = f"{prefix}{connector}{node.instance_name}: {unit}"
    if node.unresolved:
        label += " [?]"
    elif node.confidence < 0.8:
        label += f" [~{node.confidence:.1f}]"
    if node.truncated:
        label += " (...)"
    click.echo(label)
    child_prefix = "" if node.instance_name is None else prefix + ("    " if is_last else "|   ")
    for i, child in enumerate(node.children):
        _print_tree(child, child_prefix, i == len(node.children) - 1)
