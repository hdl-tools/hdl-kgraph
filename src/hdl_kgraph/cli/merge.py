"""hdl-kgraph CLI: the ``merge`` command (IP-block assembly)."""

from __future__ import annotations

from pathlib import Path

import click

from hdl_kgraph.cli._common import CliError, _ProgressRenderer
from hdl_kgraph.cli._options import _db_option
from hdl_kgraph.merge import MergeError, MergeReport, OnConflict, run_merge
from hdl_kgraph.storage.sqlite_store import SchemaVersionError


def _echo_merge_report(report: MergeReport) -> None:
    for warning in report.warnings:
        click.echo(f"warning: {warning}", err=True)
    click.echo(f"merged {report.db_path}")
    click.echo(f"  sources:        {len(report.sources)}")
    click.echo(f"  units merged:   {report.units_merged}")
    for note in report.conflicts_resolved:
        click.echo(f"  conflict:       {note}")
    click.echo(f"  nodes:          {report.node_count}")
    click.echo(f"  edges:          {report.edge_count}")
    if report.unresolved_count:
        click.echo(f"  unresolved:     {report.unresolved_count}")
    click.echo(f"  linked in {report.link_s:.2f}s ({report.elapsed_s:.2f}s total)")


@click.command()
@click.argument(
    "sources",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@_db_option
@click.option(
    "--on-conflict",
    type=click.Choice([p.value for p in OnConflict]),
    default=OnConflict.ERROR.value,
    help="When two sources hold the same path with different content: error "
    "(default), keep first, or keep last.",
)
def merge(sources: tuple[Path, ...], db_path: Path | None, on_conflict: str) -> None:
    """Merge per-block graph databases into one SoC-level graph.

    \b
    Each SOURCE is a database built independently (often by a different team or
    machine) under the *same* build root. The per-file IRs are unioned and
    re-linked once, producing a graph byte-identical to a monolithic build of
    the same files. Enriched source databases are refused — enrich the merged
    design as a whole-design step instead.

        hdl-kgraph merge blockA.db blockB.db --db soc.db
    """
    if db_path is None:
        raise CliError("merge requires an output database: --db OUT")
    renderer = _ProgressRenderer()
    try:
        report = run_merge(list(sources), db_path, OnConflict(on_conflict), progress=renderer.stage)
    except (MergeError, SchemaVersionError) as exc:
        raise CliError(str(exc)) from exc
    except click.ClickException:
        raise
    except Exception as exc:  # noqa: BLE001 — last line of defense for the CLI
        raise CliError(f"merge failed: {type(exc).__name__}: {exc}") from exc
    renderer.finish()
    _echo_merge_report(report)
