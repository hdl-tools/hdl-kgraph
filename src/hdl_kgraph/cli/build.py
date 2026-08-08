"""hdl-kgraph CLI: build/update/watch commands."""

from __future__ import annotations

from pathlib import Path

import click

from hdl_kgraph.cli._common import (
    CliError,
    _ProgressRenderer,
    _run_pipeline,
)
from hdl_kgraph.cli._options import (
    _allow_outside_root_option,
    _enrich_option,
    _input_options,
    _jobs_option,
    _resolve_options,
    _verbose_option,
)
from hdl_kgraph.pipeline import (
    BuildReport,
    UpdateReport,
    run_build,
    run_update,
)


def _echo_build_report(report: BuildReport, verbose: bool = False) -> None:
    for warning in report.warnings:
        click.echo(f"warning: {warning}", err=True)
    if report.parsed_files == 0:
        raise CliError(f"no parseable HDL files found under {report.root}")
    click.echo(f"built {report.db_path}")
    if report.filelists_read:
        click.echo(f"  filelists:      {report.filelists_read}")
    click.echo(f"  files parsed:   {report.parsed_files}")
    if report.reused_files:
        click.echo(f"  files reused:   {report.reused_files} (re-linked without re-parsing)")
    if report.vhdl_files:
        click.echo(f"  vhdl files:     {report.vhdl_files}")
    for reason, count in sorted(report.skipped.items()):
        click.echo(f"  skipped ({reason}): {count}")
    if report.error_files:
        click.echo(f"  parse errors:   {report.parse_error_count} in {report.error_files} file(s)")
        if verbose:
            for path, count in sorted(report.file_errors.items()):
                click.echo(f"      {path}: {count} error(s)")
                details = report.file_error_details.get(path, [])
                for detail in details:
                    click.echo(f"        {detail}")
                if details and count > len(details):
                    click.echo(f"        ... and {count - len(details)} more")
    if report.macros_defined:
        click.echo(f"  macros defined: {report.macros_defined}")
    if report.includes_resolved or report.includes_unresolved:
        includes = f"  includes:       {report.includes_resolved} resolved"
        if report.includes_unresolved:
            includes += f", {report.includes_unresolved} unresolved"
        click.echo(includes)
        if verbose and report.includes_unresolved:
            search = ", ".join(report.incdirs) if report.incdirs else "(none)"
            if report.auto_incdir_count:
                search += f" + {report.auto_incdir_count} auto-discovered source dir(s)"
            click.echo(f"      `include search path: {search}")
            if report.auto_incdir_count:
                hint = "header may be outside the scanned tree; add its dir with -I DIR"
            else:
                hint = "add the header's dir with -I DIR, or drop --no-auto-incdir"
            click.echo(f"      hint: {hint}")
    if report.preproc_warning_count:
        click.echo(f"  preprocessor warnings: {report.preproc_warning_count}")
        if verbose:
            for warning in report.preproc_warnings:
                click.echo(f"      {warning}")
    if report.both_branches:
        click.echo("  both-branches mode: no defines given; `ifdef alternatives kept at 0.6")
    click.echo(f"  nodes: {report.node_count}  edges: {report.edge_count}")
    if report.unresolved_count:
        click.echo(f"  unresolved: {report.unresolved_count}")
    if report.enriched:
        backends = ", ".join(report.enrich_backends) or "(no matching files)"
        click.echo(f"  enriched via {backends}: {report.edges_upgraded} edge(s) upgraded")
        if report.discrepancy_count:
            click.echo(
                f"  discrepancies: {report.discrepancy_count} "
                "(`hdl-kgraph discrepancies` lists them)"
            )
        if verbose:
            click.echo(f"      nodes added:       {report.enrich_nodes_added}")
            click.echo(f"      generates unrolled: {report.enrich_generates_unrolled}")
            click.echo("      (full delta: `hdl-kgraph enriched`)")
            for diag in report.enrich_diagnostics:
                click.echo(f"      {diag}")
    if not verbose and (report.error_files or report.preproc_warning_count):
        click.echo("  (per-file details: re-run with -v, or `hdl-kgraph status --errors`)")


def _echo_timings(report: BuildReport) -> None:
    """Per-phase wall-clock breakdown (``build --timings``).

    Capacity-planning aid for the parallel-build/merge question: phases above
    the rule are *per-partition parallelizable* (a distributed build runs them
    independently on each partition), while the link is *serial* and a merge
    pays it once over the whole union. When parse dominates, splitting the build
    and merging pays off; when the link dominates, it does not.
    """
    phases = [
        ("discover", report.discover_s, True),
        ("parse (pass 0+1)", report.parse_s, True),
        ("link (pass 2)", report.link_s, False),
        ("enrich (pass 3)", report.enrich_s, False),
        ("persist", report.persist_s, False),
    ]
    measured = sum(s for _, s, _ in phases)
    if measured <= 0:
        return
    click.echo("  timings:")
    parallelizable = 0.0
    for name, secs, is_parallel in phases:
        if secs <= 0 and name == "enrich (pass 3)":
            continue  # only ran with --enrich
        pct = 100 * secs / measured
        click.echo(f"      {name:<18} {secs:7.3f}s  ({pct:5.1f}%)")
        if is_parallel:
            parallelizable += secs
    share = 100 * parallelizable / measured
    click.echo(
        f"      {'parallelizable':<18} {parallelizable:7.3f}s  ({share:5.1f}%)  "
        "[discover+parse: split across partitions]"
    )
    click.echo(
        f"      {'serial link':<18} {report.link_s:7.3f}s  "
        f"({100 * report.link_s / measured:5.1f}%)  [paid once at merge]"
    )
    _echo_enrich_phases(report)


def _echo_enrich_phases(report: BuildReport) -> None:
    """Break the ``enrich (pass 3)`` line into its internal phases.

    Surfaces the pass-3 profiler (:mod:`hdl_kgraph.enrich._profile`) so it is
    clear which part of elaboration dominates — slang parse vs ``getRoot``
    elaboration vs the Python-side tree walk vs the graph delta-apply. Top-level
    spans (no ``/``) tile the pass; ``parent/child`` spans detail one of them.
    Percentages are of the enrichment pass, not the whole build.
    """
    timings = report.enrich_phase_s
    if not timings or report.enrich_s <= 0:
        return
    total = report.enrich_s
    top = sorted(((n, s) for n, s in timings.items() if "/" not in n), key=lambda x: -x[1])
    detail = sorted(((n, s) for n, s in timings.items() if "/" in n), key=lambda x: -x[1])
    click.echo("  enrich phases (% of pass 3):")
    for name, secs in top:
        click.echo(f"      {name:<22} {secs:8.3f}s  ({100 * secs / total:5.1f}%)")
    for name, secs in detail:
        click.echo(f"        {name:<20} {secs:8.3f}s  ({100 * secs / total:5.1f}%)")
    # Per-instance cost of the elaborated-tree walk — the line that says whether
    # the walk is super-linear (cost/instance rising with design size).
    instances = report.enrich_phase_counts.get("walk_instances", 0)
    walk_s = timings.get("slang/walk_tree", 0.0)
    if instances and walk_s > 0:
        per_us = 1_000_000 * walk_s / instances
        click.echo(f"        {'walk_instances':<20} {instances:>9,}  ({per_us:.2f} us/instance)")


@click.command()
@_input_options
@_verbose_option
@_jobs_option
@_allow_outside_root_option
@_enrich_option
@click.option(
    "--timings",
    is_flag=True,
    help="Print a per-phase wall-clock breakdown (parse vs link vs persist).",
)
def build(
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
    verbose: bool,
    jobs: int | None,
    allow_outside_root: bool,
    enrich: bool,
    no_auto_incdir: bool,
    timings: bool,
) -> None:
    """Build the knowledge graph from HDL sources under SOURCE."""
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
    options.jobs = jobs
    options.allow_outside_root = allow_outside_root
    options.enrich = options.enrich or enrich
    renderer = _ProgressRenderer()
    report = _run_pipeline(
        lambda: run_build(
            source, db_path=db_path, options=options, progress=renderer.stage, tick=renderer.tick
        ),
        "build",
    )
    renderer.finish()
    _echo_build_report(report, verbose=verbose)
    if timings:
        _echo_timings(report)


def _echo_update_report(report: UpdateReport, verbose: bool = False) -> None:
    if report.up_to_date:
        click.echo(f"up to date ({report.elapsed_s:.2f}s)")
        return
    if report.full_rebuild_reason is not None:
        click.echo(f"full rebuild: {report.full_rebuild_reason}")
    else:
        for path in report.removed:
            click.echo(f"  removed:  {path}")
        for path, why in sorted(report.reparsed.items()):
            click.echo(f"  re-parsed: {path} ({why})")
        if not report.reparsed:
            click.echo("  no files re-parsed (re-linked only)")
    if report.build is None:
        raise CliError("internal error: update reported neither up-to-date nor a build")
    _echo_build_report(report.build, verbose=verbose)
    click.echo(f"  updated in {report.elapsed_s:.2f}s")


@click.command()
@_input_options
@_verbose_option
@_jobs_option
@_allow_outside_root_option
@_enrich_option
@click.option("--full", is_flag=True, help="Force a full rebuild.")
@click.option(
    "--bounded-link/--no-bounded-link",
    default=True,
    show_default=True,
    help="Re-link incrementally without loading the whole prior graph (#119). "
    "Default; --no-bounded-link uses the legacy in-memory re-link.",
)
def update(
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
    verbose: bool,
    jobs: int | None,
    allow_outside_root: bool,
    enrich: bool,
    full: bool,
    bounded_link: bool,
    no_auto_incdir: bool,
) -> None:
    """Incrementally update the graph: re-parse only changed files.

    Changed/added/removed files and their include/macro dependents are
    re-parsed; everything else is re-linked from stored parse results.
    Falls back to a full rebuild when the database is missing or the build
    options changed.
    """
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
    options.jobs = jobs
    options.allow_outside_root = allow_outside_root
    options.enrich = options.enrich or enrich
    options.bounded_link = bounded_link
    renderer = _ProgressRenderer()
    report = _run_pipeline(
        lambda: run_update(
            source,
            db_path=db_path,
            options=options,
            full=full,
            progress=renderer.stage,
            tick=renderer.tick,
        ),
        "update",
    )
    renderer.finish()
    _echo_update_report(report, verbose=verbose)


@click.command()
@_input_options
@_verbose_option
@_jobs_option
@_allow_outside_root_option
@click.option(
    "--debounce",
    type=int,
    default=300,
    show_default=True,
    metavar="MS",
    help="Quiet period after the last filesystem event before updating.",
)
def watch(
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
    verbose: bool,
    jobs: int | None,
    allow_outside_root: bool,
    debounce: int,
    no_auto_incdir: bool,
) -> None:
    """Watch SOURCE and incrementally update the graph on every save burst."""
    from hdl_kgraph.watch import WatchUnavailableError, run_watch

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
    options.jobs = jobs
    options.allow_outside_root = allow_outside_root

    renderer = _ProgressRenderer()

    def on_report(report: UpdateReport) -> None:
        renderer.finish()
        if report.up_to_date or report.build is None:
            click.echo(f"up to date ({report.elapsed_s:.2f}s)")
            return
        if report.full_rebuild_reason is not None:
            click.echo(
                f"full rebuild ({report.full_rebuild_reason}): "
                f"{report.build.parsed_files} file(s), {report.build.node_count} nodes "
                f"({report.elapsed_s:.2f}s)"
            )
        else:
            click.echo(
                f"updated: re-parsed {len(report.reparsed)} file(s), "
                f"{report.build.node_count} nodes, {report.build.edge_count} edges "
                f"({report.elapsed_s:.2f}s)"
            )
        if verbose:
            for path, count in sorted(report.build.file_errors.items()):
                click.echo(f"    parse errors: {path}: {count}")
                for detail in report.build.file_error_details.get(path, []):
                    click.echo(f"      {detail}")
            for warning in report.build.preproc_warnings:
                click.echo(f"    warning: {warning}")

    def on_error(exc: BaseException) -> None:
        renderer.finish()
        click.echo(f"update failed: {exc} (still watching)", err=True)

    click.echo(f"watching {source.resolve()} (Ctrl-C to stop)")
    try:
        run_watch(
            source,
            db_path=db_path,
            options=options,
            quiet_s=debounce / 1000,
            on_report=on_report,
            progress=renderer.stage,
            tick=renderer.tick,
            on_error=on_error,
        )
    except WatchUnavailableError as exc:
        raise CliError(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("stopped")
