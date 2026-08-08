"""The ``bench`` CLI group: output shapes, option validation, and exit codes.

``test_bench.py`` pins the measurement logic; this module pins the surface —
in particular the exit-code policy documented in ``cli.main`` (0 success,
1 documented negative, 2 error), because ``--fail-under`` is meant to gate CI
and a benchmark that exits 2 on a missed target would break the pipeline
instead of failing it.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    root = tmp_path_factory.mktemp("cli_bench_project")
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    result = CliRunner().invoke(main, ["build", str(root)])
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture(scope="module")
def db(project: Path) -> str:
    return str(project / ".hdl-kgraph" / "graph.db")


def test_bench_group_lists_its_three_tiers() -> None:
    result = CliRunner().invoke(main, ["bench", "--help"])
    assert result.exit_code == 0
    for tier in ("context", "fidelity", "agent"):
        assert tier in result.output


def test_context_text_report(db: str) -> None:
    result = CliRunner().invoke(main, ["bench", "context", "--db", db, "--questions", "4"])
    assert result.exit_code == 0, result.output
    assert "median saving" in result.output
    assert "How the baseline is kept honest" in result.output
    # The estimator must never be separated from the numbers it produced.
    assert "heuristic(chars/4)" in result.output


def test_context_json_report(db: str) -> None:
    result = CliRunner().invoke(
        main, ["bench", "context", "--db", db, "--questions", "4", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["tokenizer"] == "heuristic(chars/4)"
    assert payload["fairness_rules"]
    assert payload["corpus"]["tokens_if_fully_read"] > 0
    for row in payload["rows"]:
        # Rule 3: the baseline must be reproducible from the report alone.
        assert row["recipe"]
        assert {"graph_tokens", "baseline_tokens", "saved_pct", "scored"} <= set(row)


def test_context_fail_under_exits_one_when_missed(db: str) -> None:
    """A missed target is a documented negative result (exit 1), not an error
    (exit 2) — so CI can tell "regressed" from "broken"."""
    result = CliRunner().invoke(
        main, ["bench", "context", "--db", db, "--questions", "4", "--fail-under", "100"]
    )
    assert result.exit_code == 1


def test_context_fail_under_passes_when_met(db: str) -> None:
    result = CliRunner().invoke(
        main, ["bench", "context", "--db", db, "--questions", "4", "--fail-under", "-1000"]
    )
    assert result.exit_code == 0, result.output


def test_context_rejects_nonsense_options(db: str) -> None:
    for flag, value in (("--questions", "0"), ("--baseline-max-files", "0")):
        result = CliRunner().invoke(main, ["bench", "context", "--db", db, flag, value])
        assert result.exit_code == 2, result.output


def test_context_missing_database_exits_two(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["bench", "context", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code == 2
    assert "not found" in result.output


def test_context_unknown_tokenizer_is_a_usage_error(db: str) -> None:
    result = CliRunner().invoke(main, ["bench", "context", "--db", db, "--tokenizer", "gpt2"])
    assert result.exit_code == 2


def test_fidelity_text_report() -> None:
    result = CliRunner().invoke(main, ["bench", "fidelity"])
    assert result.exit_code == 0, result.output
    assert "graph correct:" in result.output
    assert "grep correct:" in result.output
    # The report must disclaim itself as a catalogue, not a score.
    assert "not a benchmark score" in result.output


def test_fidelity_json_report() -> None:
    result = CliRunner().invoke(main, ["bench", "fidelity", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["graph_correct"] == payload["summary"]["cases"]
    assert "not derived from the graph" in payload["ground_truth"]
    for case in payload["cases"]:
        assert case["why"]


def test_agent_requires_a_task_file(db: str) -> None:
    result = CliRunner().invoke(main, ["bench", "agent", "--db", db])
    assert result.exit_code == 2
    assert "--tasks" in result.output


def test_agent_rejects_an_empty_task_file(db: str, tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("# only a comment\n\n")
    result = CliRunner().invoke(
        main, ["bench", "agent", "--db", db, "--tasks", str(tasks), "--yes"]
    )
    assert result.exit_code == 2
    assert "no task prompts" in result.output


def test_agent_confirms_before_spending(db: str, tmp_path: Path) -> None:
    """Tier 3 costs real money, so it must not start on a bare invocation."""
    tasks = tmp_path / "tasks.txt"
    tasks.write_text("Which module instantiates adder?\n")
    result = CliRunner().invoke(
        main, ["bench", "agent", "--db", db, "--tasks", str(tasks)], input="n\n"
    )
    assert result.exit_code != 0
    assert "consume API budget" in result.output
