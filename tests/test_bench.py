"""The ``bench`` package: token accounting, the grep baseline, and fidelity.

The contract worth pinning here is *honesty*, not throughput. The tests below
check the things that would silently turn the benchmark into marketing: that
question derivation is deterministic, that the baseline cap is reported rather
than hidden, that an empty graph answer is excluded from the headline instead
of scoring as a 100% saving, and that the fidelity suite's ground truth is
matched by the graph and missed by grep for the documented reasons.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph.bench import agent, baseline, context, corpus, fidelity, questions, report
from hdl_kgraph.bench.tokens import TOKENIZERS, HeuristicEstimator, make_estimator
from hdl_kgraph.cli.main import main
from hdl_kgraph.mcp.setup import plan_entry
from hdl_kgraph.storage.query import GraphQuery


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    root = tmp_path_factory.mktemp("bench_project")
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    result = CliRunner().invoke(main, ["build", str(root)])
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture(scope="module")
def db_path(project: Path) -> Path:
    return project / ".hdl-kgraph" / "graph.db"


# -- token estimators ---------------------------------------------------------


def test_heuristic_estimator_is_chars_over_four() -> None:
    estimator = HeuristicEstimator()
    assert estimator.count("") == 0
    assert estimator.count("abcd") == 1
    # Rounds up, so a short answer is never priced at zero tokens.
    assert estimator.count("a") == 1
    assert estimator.count("a" * 401) == 101


def test_make_estimator_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown tokenizer"):
        make_estimator("gpt2")


def test_every_advertised_tokenizer_is_constructible_or_explains_itself() -> None:
    for name in TOKENIZERS:
        try:
            estimator = make_estimator(name)
        except ValueError as exc:
            # The only acceptable failure is a missing optional dependency,
            # and it must name the extra that fixes it.
            assert "hdl-kgraph[bench]" in str(exc)
            continue
        assert estimator.name
        assert estimator.count("module foo; endmodule") > 0


def test_tiktoken_estimator_when_installed() -> None:
    pytest.importorskip("tiktoken")
    estimator = make_estimator("tiktoken")
    assert "tiktoken" in estimator.name
    # Special-token text must not raise — HDL source can legitimately contain it.
    assert estimator.count("<|endoftext|> module foo; endmodule") > 0


# -- corpus -------------------------------------------------------------------


def test_corpus_is_only_indexed_unskipped_hdl(db_path: Path) -> None:
    loaded = corpus.load_corpus(db_path)
    assert loaded.files, "the fixture build should index some HDL"
    for source in loaded.files:
        assert source.language in corpus.HDL_LANGUAGES
        assert source.path.is_file()
    # Sorted, so two runs are comparable.
    assert [f.relpath for f in loaded.files] == sorted(f.relpath for f in loaded.files)


def test_corpus_reports_indexed_files_missing_from_disk(tmp_path: Path, fixtures_dir: Path) -> None:
    """A stale database must announce itself rather than silently shrink the
    baseline's search space — otherwise the grep arm quietly gets an easier
    job than the one the graph was built for."""
    root = tmp_path / "stale"
    root.mkdir()
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    assert CliRunner().invoke(main, ["build", str(root)]).exit_code == 0
    stale_db = root / ".hdl-kgraph" / "graph.db"

    before = corpus.load_corpus(stale_db)
    victim = before.files[0]
    (root / victim.relpath).unlink()

    after = corpus.load_corpus(stale_db)
    assert victim.relpath in after.missing
    assert len(after.files) == len(before.files) - 1


# -- baseline -----------------------------------------------------------------


def test_grep_returns_path_line_text(db_path: Path) -> None:
    loaded = corpus.load_corpus(db_path)
    lines, hits = baseline.grep(loaded, re.compile(r"\bmodule\b"))
    assert lines and hits
    for line in lines:
        relpath, number, _text = line.split(":", 2)
        assert number.isdigit()
        assert relpath in {f.relpath for f in loaded.files}


def test_baseline_cap_is_enforced_and_flagged(db_path: Path) -> None:
    loaded = corpus.load_corpus(db_path)
    uncapped = baseline.instances_of(loaded, "adder", max_files=1000)
    capped = baseline.instances_of(loaded, "adder", max_files=1)

    assert len(capped.files_read) <= 1
    assert capped.bytes <= uncapped.bytes
    if len(uncapped.files_read) > 1:
        # The whole point: when the cap bites, the row says so.
        assert capped.capped is True


def test_baseline_recipe_records_every_step(db_path: Path) -> None:
    loaded = corpus.load_corpus(db_path)
    recipe = baseline.instances_of(loaded, "adder")
    assert recipe.commands[0].startswith("rg -n ")
    assert all(
        any(command == f"read {relpath}" for command in recipe.commands)
        for relpath in recipe.files_read
    )
    # Evidence text is the grep output plus each file read, nothing else.
    assert recipe.bytes == len(recipe.text)


def test_baseline_is_deterministic(db_path: Path) -> None:
    loaded = corpus.load_corpus(db_path)
    first = baseline.instances_of(loaded, "adder")
    second = baseline.instances_of(loaded, "adder")
    assert first.text == second.text
    assert first.commands == second.commands


# -- question derivation ------------------------------------------------------


def test_question_derivation_is_deterministic(db_path: Path) -> None:
    query = GraphQuery(db_path)
    first = [(q.key, q.target) for q in questions.derive(query, 12)]
    second = [(q.key, q.target) for q in questions.derive(query, 12)]
    assert first == second
    assert first, "the fixture design should yield questions"


def test_question_derivation_respects_the_limit(db_path: Path) -> None:
    query = GraphQuery(db_path)
    assert len(questions.derive(query, 3)) <= 3
    # A small limit still spreads across templates rather than returning three
    # rows of the same question.
    keys = {q.key for q in questions.derive(query, 3)}
    assert len(keys) > 1


def test_signal_targets_are_ranked_by_drivers_not_readers(db_path: Path) -> None:
    """Ranking on readers picks top-level inputs, which have no drivers at all —
    and a zero-result answer would otherwise look like a huge saving."""
    query = GraphQuery(db_path)
    for name in questions.hottest_signals(query, limit=2):
        result = query.find_signal_drivers(name, None, False, 50, 0)
        assert result["total"] > 0, f"{name} was chosen but has no drivers"


# -- empty-answer accounting --------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"total": 0, "items": []}, True),
        ({"total": 3, "items": [1, 2, 3]}, False),
        ({"root": {"children": []}}, True),
        ({"root": {"children": [{"module_name": "x"}]}}, False),
        ({"ports": [], "parameters": []}, True),
        ({"ports": [{"name": "clk"}], "parameters": []}, False),
    ],
)
def test_empty_answer_detection(payload: dict, expected: bool) -> None:
    assert context._is_empty(payload) is expected


def test_empty_graph_answers_are_excluded_from_the_headline() -> None:
    """A cheap non-answer is not a saving. This is the single most important
    guard in the benchmark: without it, a question the graph cannot answer
    scores ~100%."""
    real = context.Row(
        key="who-instantiates",
        target="a",
        prompt="",
        graph_tokens=100,
        graph_bytes=400,
        graph_ms=1.0,
        baseline_tokens=1000,
        baseline_bytes=4000,
        baseline_ms=5.0,
        baseline_hit_files=1,
        baseline_files_read=1,
        capped=False,
    )
    empty = context.Row(
        key="find-signal-drivers",
        target="b",
        prompt="",
        graph_tokens=20,
        graph_bytes=80,
        graph_ms=1.0,
        baseline_tokens=40_000,
        baseline_bytes=160_000,
        baseline_ms=5.0,
        baseline_hit_files=40,
        baseline_files_read=10,
        capped=True,
        graph_empty=True,
    )
    built = context.Report(
        db="db",
        root="root",
        tokenizer="heuristic(chars/4)",
        baseline_max_files=10,
        corpus_files=1,
        corpus_bytes=1,
        corpus_tokens=1,
        stale_missing=[],
        rows=[real, empty],
    )
    assert empty.scored is False
    assert built.unscored == [empty]
    # 90%, from the real row alone — not the ~99.9% the empty row would add.
    assert built.median_saved_pct == pytest.approx(90.0)
    assert built.total_saved_pct == pytest.approx(90.0)
    assert built.to_json()["summary"]["questions_excluded"] == 1


def test_negative_savings_are_reported_not_clamped() -> None:
    row = context.Row(
        key="port-map",
        target="a",
        prompt="",
        graph_tokens=2000,
        graph_bytes=8000,
        graph_ms=1.0,
        baseline_tokens=1000,
        baseline_bytes=4000,
        baseline_ms=1.0,
        baseline_hit_files=1,
        baseline_files_read=1,
        capped=False,
    )
    assert row.saved_pct == pytest.approx(-100.0)


# -- end-to-end context run ---------------------------------------------------


def test_context_run_prices_both_arms(db_path: Path) -> None:
    built = context.run(db_path, HeuristicEstimator(), limit=6, max_files=3)
    assert built.rows
    assert built.tokenizer == "heuristic(chars/4)"
    assert built.baseline_max_files == 3
    # The read-everything ceiling must bound every baseline measurement.
    for row in built.rows:
        assert row.baseline_tokens <= built.corpus_tokens
        assert row.recipe
    payload = built.to_json()
    assert payload["fairness_rules"]
    assert payload["summary"]["questions"] == len(built.rows)


def test_context_run_is_reproducible(db_path: Path) -> None:
    first = context.run(db_path, HeuristicEstimator(), limit=6, max_files=3)
    second = context.run(db_path, HeuristicEstimator(), limit=6, max_files=3)
    assert [(r.key, r.target, r.graph_tokens, r.baseline_tokens) for r in first.rows] == [
        (r.key, r.target, r.graph_tokens, r.baseline_tokens) for r in second.rows
    ]


def test_context_report_renders_the_fairness_rules(db_path: Path) -> None:
    text = report.render_context(context.run(db_path, HeuristicEstimator(), limit=4, max_files=2))
    assert "How the baseline is kept honest" in text
    assert "ceiling no baseline can exceed" in text


# -- fidelity -----------------------------------------------------------------


def test_bundled_fidelity_suite_behaves_as_documented() -> None:
    built = fidelity.run()
    by_name = {case.name: case for case in built.cases}
    assert set(by_name) == {
        "macro-hidden-instantiation",
        "comment-and-string-false-positive",
        "vhdl-case-insensitive-name",
        "ambiguous-duplicate-definition",
    }

    # The graph must be right on every hand-authored case, or the suite is
    # documenting a bug rather than a capability.
    assert all(case.graph_correct for case in built.cases)

    # A macro-hidden instantiation is invisible to a word-boundary search.
    macro = by_name["macro-hidden-instantiation"]
    assert "macro_top" in macro.graph_answer
    assert "macro_top" not in macro.grep_answer

    # A comment/string mention is a grep false positive, and nothing else.
    comment = by_name["comment-and-string-false-positive"]
    assert comment.graph_answer == []
    assert comment.grep_answer == ["commented"]

    # VHDL names match case-insensitively; a literal search does not.
    vhdl = by_name["vhdl-case-insensitive-name"]
    assert vhdl.graph_answer == ["vhdl_user"]
    assert vhdl.grep_answer == []

    # And the control: one case grep gets exactly right, kept so the suite is
    # not a rigged scoreboard.
    assert by_name["ambiguous-duplicate-definition"].grep_correct is True


def test_fidelity_suite_ground_truth_is_not_taken_from_the_graph() -> None:
    """The manifest must state expectations itself; a suite that read them back
    from the graph would prove nothing."""
    cases = fidelity.load_suite(fidelity.BUNDLED_SUITE)
    assert cases
    for case in cases:
        assert "expect" in case
        assert case.get("why", "").strip(), f"{case['name']} must explain the failure mode"


def test_fidelity_run_over_a_user_suite(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "leaf.sv").write_text("module leaf(input logic a); endmodule\n")
    (suite / "top.sv").write_text("module top; leaf u_leaf(.a(1'b0)); endmodule\n")
    (suite / "suite.toml").write_text(
        '[[case]]\nname = "user"\ntarget = "leaf"\nexpect = ["top"]\n'
        'forbid = []\nwhy = "a plain instantiation"\n'
    )
    built = fidelity.run(suite)
    assert len(built.cases) == 1
    assert built.cases[0].graph_answer == ["top"]
    assert built.cases[0].graph_correct


def test_fidelity_rejects_a_directory_without_a_manifest(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="suite.toml"):
        fidelity.load_suite(tmp_path)


# -- tier 3 wiring (no live model runs) ---------------------------------------


def test_arm_configs_differ_only_in_mcp() -> None:
    graph_arm = json.loads(agent._mcp_config(Path("/tmp/x/.hdl-kgraph/graph.db")))
    control_arm = json.loads(agent._mcp_config(None))
    assert list(graph_arm["mcpServers"]) == ["hdl-kgraph"]
    assert control_arm["mcpServers"] == {}
    # Reuses the entry `setup` writes, so the benchmark cannot drift from what
    # a real user is configured with.
    assert graph_arm["mcpServers"]["hdl-kgraph"] == plan_entry(Path("/tmp/x/.hdl-kgraph/graph.db"))


def test_command_isolates_the_environment_identically_for_both_arms() -> None:
    argv = agent._command(0, "{}", model="claude-opus-5", max_turns=7)
    joined = " ".join(argv)
    # Every other MCP source must be ignored, or the control arm inherits the
    # user's servers.
    assert "--strict-mcp-config" in argv
    # Project settings are dropped so ./CLAUDE.md cannot tell the control arm
    # to prefer a tool it does not have.
    assert "--setting-sources" in argv and "user,local" in argv
    # Bash is excluded outright: a `Bash(hdl-kgraph *)` deny rule is prefix
    # matched and trivially evaded.
    assert "--tools" in argv and agent.ARM_TOOLS == "Read,Grep,Glob"
    assert "Bash" not in agent.ARM_TOOLS
    assert "--permission-mode dontAsk" in joined
    assert "--max-turns 7" in joined
    # The prompt must never be positional: --tools/--mcp-config are variadic
    # and would swallow it.
    assert argv[-2:] == ["--model", "claude-opus-5"]


def _stream(init: dict, result: dict) -> str:
    return "\n".join([json.dumps(init), json.dumps(result)])


def _result_event(**overrides: object) -> dict:
    event = {
        "type": "result",
        "is_error": False,
        "terminal_reason": "completed",
        "num_turns": 3,
        "duration_ms": 1000,
        "total_cost_usd": 0.1,
        "result": "answer",
        "permission_denials": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 50,
        },
    }
    event.update(overrides)
    return event


def _init_event(servers: list[dict]) -> dict:
    return {"type": "system", "subtype": "init", "model": "claude-opus-5", "mcp_servers": servers}


def test_parse_stream_picks_the_init_and_result_events() -> None:
    init, result, graph_calls = agent._parse_stream(
        "not json\n" + _stream(_init_event([]), _result_event()) + "\n{bad json"
    )
    assert init["subtype"] == "init"
    assert result["type"] == "result"
    assert graph_calls == 0


def test_parse_stream_counts_graph_tool_calls() -> None:
    """The only evidence that the graph arm used the graph."""
    assistant = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__hdl-kgraph__who_instantiates"},
                    {"type": "tool_use", "name": "mcp__hdl-kgraph__port_map"},
                    {"type": "tool_use", "name": "Grep"},
                    {"type": "text", "text": "thinking"},
                ]
            },
        }
    )
    stdout = "\n".join([json.dumps(_init_event([])), assistant, json.dumps(_result_event())])
    _init, _result, graph_calls = agent._parse_stream(stdout)
    assert graph_calls == 2


def test_graph_tools_are_explicitly_allowed_only_for_the_graph_arm() -> None:
    """`--permission-mode dontAsk` auto-denies an unlisted MCP tool, so without
    an allow rule the graph arm silently degrades to grep and the A/B compares
    two identical arms."""
    graph_argv = agent._command(0, "{}", model=None, max_turns=3, allow_graph_tools=True)
    control_argv = agent._command(0, "{}", model=None, max_turns=3, allow_graph_tools=False)
    assert "--allowedTools" in graph_argv
    assert agent.MCP_ALLOW_RULE in graph_argv
    assert "--allowedTools" not in control_argv


def _run_with(monkeypatch: pytest.MonkeyPatch, arm: str, stdout: str) -> agent.Run:
    class _Completed:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = ""

    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: _Completed())
    return agent.run_once(
        task="q",
        arm=arm,
        repetition=0,
        cwd=Path("."),
        db_path=Path("/tmp/x.db") if arm == "graph" else None,
        config_dir=Path("/tmp/cfg"),
        model=None,
        max_turns=3,
        timeout_s=10,
    )


def test_graph_arm_is_rejected_when_the_server_did_not_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this check the benchmark silently compares two no-graph arms."""
    stdout = _stream(_init_event([{"name": "hdl-kgraph", "status": "failed"}]), _result_event())
    record = _run_with(monkeypatch, "graph", stdout)
    assert record.ok is False
    assert "no connected hdl-kgraph server" in record.note


def test_control_arm_is_rejected_when_an_mcp_server_leaked_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _stream(_init_event([{"name": "other", "status": "connected"}]), _result_event())
    record = _run_with(monkeypatch, "no-graph", stdout)
    assert record.ok is False
    assert "not comparable" in record.note


def test_control_arm_flags_attempts_to_reach_the_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = _stream(_init_event([]), _result_event(permission_denials=[{"tool": "Bash"}]))
    record = _run_with(monkeypatch, "no-graph", stdout)
    assert "tried to reach the graph" in record.note


def test_graph_arm_is_rejected_when_the_graph_was_never_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected server proves the plumbing, not the usage. If the model
    never called a graph tool the two arms were identical."""
    stdout = _stream(_init_event([{"name": "hdl-kgraph", "status": "connected"}]), _result_event())
    record = _run_with(monkeypatch, "graph", stdout)
    assert record.ok is False
    assert "never called a graph tool" in record.note


def test_control_arm_is_rejected_when_it_called_a_graph_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assistant = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "mcp__hdl-kgraph__port_map"}]},
        }
    )
    stdout = "\n".join([json.dumps(_init_event([])), assistant, json.dumps(_result_event())])
    record = _run_with(monkeypatch, "no-graph", stdout)
    assert record.ok is False
    assert "control contaminated" in record.note


def test_design_root_comes_from_the_database_not_the_path(db_path: Path) -> None:
    """`--db` may point outside the project it indexes; the grandparent of the
    db path would then drop the agent somewhere with nothing to grep."""
    assert agent.design_root(db_path) == db_path.parent.parent


def test_a_run_that_did_not_complete_is_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """An auth or rate-limit failure exits 0 with is_error true — gating on the
    exit code would count it as a free, zero-token win."""
    stdout = _stream(
        _init_event([]),
        _result_event(is_error=True, terminal_reason="api_error", result="Not logged in"),
    )
    record = _run_with(monkeypatch, "no-graph", stdout)
    assert record.ok is False
    assert "did not complete" in record.note


def test_successful_control_run_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _run_with(monkeypatch, "no-graph", _stream(_init_event([]), _result_event()))
    assert record.ok is True
    assert record.billable_tokens == 15
    # Cache tokens are tracked but deliberately kept out of the billable total.
    assert record.cache_creation_tokens == 100
    assert record.cache_read_tokens == 50


def test_summarize_reports_a_spread_and_its_caveats() -> None:
    runs = [
        agent.Run(task="t", arm="graph", repetition=i, input_tokens=100, output_tokens=0, ok=True)
        for i in range(3)
    ] + [
        agent.Run(
            task="t", arm="no-graph", repetition=i, input_tokens=1000, output_tokens=0, ok=True
        )
        for i in range(3)
    ]
    summary = agent.summarize(runs)
    assert summary["median_saved_pct"] == pytest.approx(90.0)
    assert summary["arms"]["graph"]["runs_ok"] == 3
    joined = " ".join(summary["caveats"])
    assert "no seed or temperature" in joined
    assert "prompt caching" in joined


def test_agent_run_requires_the_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "claude_available", lambda: False)
    with pytest.raises(agent.AgentBenchError, match="not on PATH"):
        agent.run(["task"], db_path=Path("/tmp/x.db"), cwd=Path("."))


def test_fidelity_verdicts_name_the_failure_mode() -> None:
    def case(graph: list[str], grep: list[str], expected: list[str]) -> fidelity.Case:
        return fidelity.Case(
            name="c",
            target="t",
            question="q",
            expected=expected,
            forbidden=[],
            why="",
            graph_answer=graph,
            grep_answer=grep,
        )

    assert case(["a"], ["a"], ["a"]).verdict == "both correct"
    assert case(["a"], [], ["a"]).verdict == "grep incomplete"
    assert case([], ["a"], []).verdict == "grep false-positive"
    assert case(["a"], ["b"], ["a"]).verdict == "grep wrong (missed and over-reported)"
    assert case(["b"], ["a"], ["a"]).verdict == "graph wrong"
    assert case(["c"], ["b"], ["a"]).verdict == "both wrong"
