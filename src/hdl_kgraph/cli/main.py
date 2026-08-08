"""hdl-kgraph CLI entry point.

The command implementations live in focused submodules (``build``, ``query``,
``analyze``, ``serve``); this module assembles them into the ``main`` group and
is the console-script / ``python -m hdl_kgraph`` entry point.
"""

from __future__ import annotations

import click

from hdl_kgraph import __version__
from hdl_kgraph.cli._common import _ProgressRenderer
from hdl_kgraph.cli.analyze import (
    detect_changes,
    discrepancies,
    enriched,
    export_cmd,
    impact,
    lint_cmd,
    metrics_cmd,
    review,
    status,
    tree,
    visualize,
)
from hdl_kgraph.cli.bench import bench, bench_link
from hdl_kgraph.cli.build import build, update, watch
from hdl_kgraph.cli.merge import merge
from hdl_kgraph.cli.query import query
from hdl_kgraph.cli.serve import serve, setup
from hdl_kgraph.cli.tools import tools

__all__ = ["main", "_ProgressRenderer"]


@click.group()
@click.version_option(version=__version__, prog_name="hdl-kgraph")
def main() -> None:
    """Build and query a knowledge graph of your HDL design.

    \b
    Exit codes (uniform across commands; text and --json agree):
      0  success — including an empty report (e.g. no CDC suspects is good news)
      1  a documented negative result: `detect-changes` found changes, or a
         name lookup (`query instances-of`, `query drivers`) matched nothing
      2  an error: bad usage, missing/foreign database, config/VCS failure, or
         an unexpected build/update failure
    """


for _cmd in (
    build,
    update,
    merge,
    watch,
    detect_changes,
    impact,
    status,
    review,
    bench_link,
    bench,
    discrepancies,
    enriched,
    lint_cmd,
    metrics_cmd,
    visualize,
    export_cmd,
    tree,
    serve,
    setup,
    query,
    tools,
):
    main.add_command(_cmd)
