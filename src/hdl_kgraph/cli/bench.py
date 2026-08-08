"""hdl-kgraph CLI: the ``bench`` subcommand group.

Measures the claim ``hdl-kgraph setup`` writes into every assistant's
instruction file — "query the graph instead of grepping the raw RTL" — against
the user's own design, rather than asking them to take it on faith. The three
subcommands are three different kinds of evidence, with three different costs:
``context`` and ``fidelity`` are deterministic, offline, and free; ``agent``
spends real money on live model runs and is non-deterministic by construction.
"""

from __future__ import annotations

from pathlib import Path

import click

from hdl_kgraph.bench import agent as agent_bench
from hdl_kgraph.bench import context as context_bench
from hdl_kgraph.bench import fidelity as fidelity_bench
from hdl_kgraph.bench import report as bench_report
from hdl_kgraph.bench.baseline import DEFAULT_MAX_FILES
from hdl_kgraph.bench.tokens import TOKENIZERS, make_estimator
from hdl_kgraph.cli._common import CliError, _resolve_db
from hdl_kgraph.cli._options import _db_option, _json_option
from hdl_kgraph.cli.render import emit_json
from hdl_kgraph.storage.sqlite_store import SchemaVersionError


@click.group()
def bench() -> None:
    """Measure what the graph saves versus grepping the RTL.

    \b
      context   token + latency cost per question, graph vs grep (free, offline)
      fidelity  where grep gets the answer wrong (free, offline)
      agent     live Claude Code A/B (opt-in; spends API budget)

    `context` and `fidelity` are deterministic — same database, same numbers.
    `agent` is not, and says so.
    """


@bench.command("context")
@_db_option
@click.option(
    "--tokenizer",
    type=click.Choice(TOKENIZERS),
    default="heuristic",
    help="How to price text. 'heuristic' is chars/4 and needs nothing; "
    "'tiktoken' is exact and needs `pip install 'hdl-kgraph[bench]'` "
    "[default: heuristic].",
)
@click.option(
    "--questions",
    type=int,
    default=12,
    help="Max questions to derive from the graph [default: 12].",
)
@click.option(
    "--baseline-max-files",
    type=int,
    default=DEFAULT_MAX_FILES,
    help="How many hit files the grep baseline may read before it is capped "
    f"[default: {DEFAULT_MAX_FILES}].",
)
@click.option(
    "--fail-under",
    type=float,
    default=None,
    metavar="PCT",
    help="Exit 1 if the median saving is below PCT (for CI gating).",
)
@_json_option
def context_cmd(
    db_path: Path | None,
    tokenizer: str,
    questions: int,
    baseline_max_files: int,
    fail_under: float | None,
    as_json: bool,
) -> None:
    """Price each design question twice: via the graph, and via grep+read.

    Questions are derived from the graph deterministically (the top module, the
    most-instantiated modules, the hottest signals), so this runs on any design
    with no hand-written fixtures. Both arms search exactly the files the graph
    indexed, and the baseline's recipe is printed so it can be reproduced.

    Cost only — correctness is `bench fidelity`, because a graph cannot score
    its own answers.
    """
    if questions < 1:
        raise CliError("--questions must be at least 1")
    if baseline_max_files < 1:
        raise CliError("--baseline-max-files must be at least 1")
    try:
        estimator = make_estimator(tokenizer)
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    resolved = _resolve_db(db_path)
    try:
        report = context_bench.run(
            resolved, estimator, limit=questions, max_files=baseline_max_files
        )
    except SchemaVersionError as exc:
        raise CliError(str(exc)) from exc

    if not report.rows:
        raise CliError(
            "no questions could be derived from this graph — it has no modules "
            "or entities; run `hdl-kgraph build` over your RTL first"
        )

    if as_json:
        emit_json(report.to_json())
    else:
        bench_report.echo(bench_report.render_context(report))

    if fail_under is not None and report.median_saved_pct < fail_under:
        # A missed target is a documented negative result (exit 1), not an
        # error (exit 2) — the CLI's uniform exit-code policy.
        raise SystemExit(1)


@bench.command("fidelity")
@click.option(
    "--suite",
    "suite_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory with your own HDL plus a suite.toml (default: the bundled suite).",
)
@_json_option
def fidelity_cmd(suite_dir: Path | None, as_json: bool) -> None:
    """Compare graph and grep answers against hand-written ground truth.

    Builds a throwaway graph over the suite and asks both arms the same
    question ("which design units instantiate X?"). The bundled cases cover a
    macro-hidden instantiation, a comment/string false positive, a VHDL
    case-insensitive name, and one case grep answers perfectly.

    A qualitative catalogue, not a score: a different case mix gives a
    different ratio, and the report says so.
    """
    try:
        report = fidelity_bench.run(suite_dir)
    except FileNotFoundError as exc:
        raise CliError(str(exc)) from exc

    if as_json:
        emit_json(report.to_json())
    else:
        bench_report.echo(bench_report.render_fidelity(report))


@bench.command("agent")
@_db_option
@click.option(
    "--tasks",
    "tasks_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="File with one task prompt per line (blank lines and # comments ignored).",
)
@click.option("--repeat", type=int, default=3, help="Repetitions per arm [default: 3].")
@click.option("--model", default=None, help="Model to pin for both arms (recommended).")
@click.option("--max-turns", type=int, default=12, help="Turn cap per run [default: 12].")
@click.option("--timeout", "timeout_s", type=int, default=600, help="Per-run timeout seconds.")
@click.option("--yes", is_flag=True, help="Skip the spend confirmation.")
@_json_option
def agent_cmd(
    db_path: Path | None,
    tasks_file: Path,
    repeat: int,
    model: str | None,
    max_turns: int,
    timeout_s: int,
    yes: bool,
    as_json: bool,
) -> None:
    """Run tasks through Claude Code twice — with the graph, and without.

    Arm A gets exactly one MCP server (hdl-kgraph); arm B gets none, and
    neither gets Bash, so the control cannot reach `hdl-kgraph tools` behind
    your back. Both run with project CLAUDE.md suppressed and a clean config
    directory, so the delta is the graph rather than your local setup.

    This spends API budget and is NOT deterministic: no seed or temperature
    control exists. Report the spread, never a single-run delta.
    """
    if repeat < 1:
        raise CliError("--repeat must be at least 1")
    resolved = _resolve_db(db_path)
    tasks = [
        line.strip()
        for line in tasks_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not tasks:
        raise CliError(f"no task prompts found in {tasks_file}")

    total_runs = len(tasks) * repeat * 2
    if not yes:
        click.confirm(
            f"This runs {total_runs} live Claude Code sessions "
            f"({len(tasks)} tasks x {repeat} repetitions x 2 arms) and will "
            f"consume API budget. Continue?",
            abort=True,
        )

    try:
        runs = agent_bench.run(
            tasks,
            db_path=resolved,
            cwd=resolved.parent.parent,
            repeat=repeat,
            model=model,
            max_turns=max_turns,
            timeout_s=timeout_s,
        )
    except agent_bench.AgentBenchError as exc:
        raise CliError(str(exc)) from exc

    summary = agent_bench.summarize(runs)
    if as_json:
        emit_json(summary)
        return

    for arm, stats in summary["arms"].items():
        click.echo(
            f"{arm:<10} ok {stats['runs_ok']}/{stats['runs_total']}   "
            f"median billable tokens {stats['median_billable_tokens']}   "
            f"median turns {stats['median_turns']}   "
            f"median cost ${stats['median_cost_usd']}"
        )
    click.echo(f"median saving: {summary['median_saved_pct']}%")
    click.echo("")
    for caveat in summary["caveats"]:
        click.echo(f"  - {caveat}")
    for run_row in summary["runs"]:
        if run_row["note"]:
            click.echo(f"  ! {run_row['arm']} rep {run_row['repetition']}: {run_row['note']}")
