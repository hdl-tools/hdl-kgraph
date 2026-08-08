"""Shared CLI option decorators and build-input resolution."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

from hdl_kgraph.cli._common import CliError
from hdl_kgraph.config import (
    BuildConfig,
    BuildOptions,
    ConfigError,
    find_config,
    resolve_build_options,
)
from hdl_kgraph.discovery import DEFAULT_MAX_FILE_SIZE_KB

_db_option = click.option(
    "--db",
    "db_path",
    type=click.Path(path_type=Path),
    default=None,
    help="Path to the graph database (default: nearest .hdl-kgraph/graph.db).",
)

_json_option = click.option(
    "--json", "as_json", is_flag=True, help="Emit JSON instead of the text report."
)

_verbose_option = click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Also report per-file parse errors, preprocessor warnings, and "
    "unresolved includes (stage progress is always shown).",
)

_jobs_option = click.option(
    "-j",
    "--jobs",
    type=int,
    default=None,
    metavar="N",
    help="Parse with N worker processes (default: auto — serial for small "
    "builds, otherwise one per CPU up to a cap; 1 = always serial).",
)

_allow_outside_root_option = click.option(
    "--allow-outside-root",
    is_flag=True,
    help="Honor filelist source/-v/-y/-f and +incdir+ tokens that resolve "
    "outside the build root instead of dropping them. Relaxes the default "
    "containment (#68); only use with filelists you trust.",
)

_enrich_option = click.option(
    "--enrich",
    is_flag=True,
    help="Run native-frontend elaboration (pyslang for SV/Verilog, ghdl for "
    "VHDL) to upgrade heuristic edges to elaboration-accurate facts and record "
    "discrepancies (M7). Slower; re-runs whole-design elaboration on every "
    "update. `hdl-kgraph enriched` reports the delta vs the default build.",
)


def _input_options(f: Callable) -> Callable:
    """The build-input flags shared by ``build``, ``update``, and ``watch``."""
    decorators = [
        click.argument(
            "source", type=click.Path(exists=True, path_type=Path), default=".", required=False
        ),
        _db_option,
        click.option(
            "-f",
            "--filelist",
            "filelists",
            multiple=True,
            type=click.Path(exists=True, path_type=Path),
            help="Compile the sources listed in this .f/.vc filelist (repeatable); "
            "SOURCE then only sets the build root.",
        ),
        click.option(
            "-D",
            "--define",
            "defines",
            multiple=True,
            metavar="NAME[=VALUE]",
            help="Preprocessor define (repeatable; overrides config and filelist defines).",
        ),
        click.option(
            "-I",
            "--incdir",
            "incdirs",
            multiple=True,
            type=click.Path(path_type=Path),
            help="`include search directory, searched before auto-discovered dirs (repeatable).",
        ),
        click.option(
            "--no-auto-incdir",
            is_flag=True,
            help="Do not auto-search discovered source directories for "
            "`` `include``s; resolve against -I/+incdir+ dirs only.",
        ),
        click.option(
            "--lib",
            "libs",
            multiple=True,
            metavar="NAME=PATH",
            help="Map a VHDL library name to a source directory (repeatable; "
            "overrides [vhdl.libraries] config entries; default library is 'work').",
        ),
        click.option(
            "--config",
            "config_path",
            type=click.Path(exists=True, path_type=Path),
            default=None,
            help="Path to hdl-kgraph.toml (default: nearest one from SOURCE upward).",
        ),
        click.option("--no-config", is_flag=True, help="Ignore any hdl-kgraph.toml."),
        click.option(
            "--exclude",
            "excludes",
            multiple=True,
            metavar="GLOB",
            help="Skip files whose root-relative path matches GLOB (repeatable).",
        ),
        click.option(
            "--max-file-size",
            type=int,
            default=None,
            metavar="KB",
            help="Skip files larger than this many kilobytes. "
            f"[default: {DEFAULT_MAX_FILE_SIZE_KB}]",
        ),
    ]
    for decorator in reversed(decorators):
        f = decorator(f)
    return f


def _resolve_options(
    source: Path,
    filelists: tuple[Path, ...],
    defines: tuple[str, ...],
    incdirs: tuple[Path, ...],
    libs: tuple[str, ...],
    config_path: Path | None,
    no_config: bool,
    excludes: tuple[str, ...],
    max_file_size: int | None,
    no_auto_incdir: bool = False,
) -> BuildOptions:
    """Merge config-file and CLI build inputs (the ``build`` precedence rules)."""
    if no_config:
        config = BuildConfig()
    else:
        if config_path is None:
            config_path = find_config(source)
        try:
            config = BuildConfig.load(config_path) if config_path is not None else BuildConfig()
        except ConfigError as exc:
            raise CliError(str(exc)) from exc
    try:
        return resolve_build_options(
            config,
            cli_filelists=[p.resolve() for p in filelists],
            cli_defines=defines,
            cli_incdirs=[p.resolve() for p in incdirs],
            cli_exclude=excludes,
            cli_max_file_size_kb=max_file_size,
            cli_libs=libs,
            cli_no_auto_incdir=no_auto_incdir,
        )
    except ConfigError as exc:
        raise CliError(str(exc)) from exc
