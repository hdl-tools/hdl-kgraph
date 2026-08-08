"""Tier 3: a live Claude Code A/B — the expensive, honest, noisy one.

Tiers 1 and 2 are deterministic and free, and they are what the recorded
numbers should rest on. This tier answers the question they cannot: given a
real task and a real model, does having the graph *actually* change what the
assistant spends? It runs the same tasks twice — once with the hdl-kgraph MCP
server, once with no MCP at all — and reports what the CLI itself says it
used.

Three things make a naive version of this meaningless, so all three are
handled here and stated in the report:

**There is no determinism.** Claude Code exposes no seed and no temperature.
A single-run delta is noise. ``--repeat`` runs each arm N times and the report
carries the spread, never a bare difference.

**Prompt caching favours whichever arm runs second.** Back-to-back runs share
cache, so the second is systematically cheaper. Arm order is therefore
alternated per repetition, and cache-creation and cache-read tokens are
reported as separate series instead of being folded into one cost number.

**The environment can dwarf the signal.** Installed slash commands, agents,
hooks, and unrelated MCP servers can cost tens of thousands of tokens before
the task starts. Both arms run under a benchmark-owned ``CLAUDE_CONFIG_DIR``
and with project settings (hence ``CLAUDE.md``) suppressed, so the delta is
attributable to the graph rather than to the user's setup. The ``init`` event
of every run is recorded, and the arms are *asserted* to differ only in MCP.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Built-in tools both arms get. Bash is deliberately excluded: blocking the
#: binary with `--disallowedTools 'Bash(hdl-kgraph *)'` is prefix-matched and
#: trivially evaded (`cd rtl && hdl-kgraph ...`, `python -m hdl_kgraph`,
#: `sh -c '...'`), so the control arm would silently regain the graph. Dropping
#: Bash is the only real boundary, and grep/read tasks do not need it.
ARM_TOOLS = "Read,Grep,Glob"

#: Gate on the result object, never on the exit code: a `--bare` auth failure
#: and a rate-limit stop both exit 0 with `is_error: true`.
_SUCCESS_REASON = "completed"


class AgentBenchError(RuntimeError):
    """The A/B could not be run, or could not be trusted."""


@dataclass
class Run:
    """One task, one arm, one repetition."""

    task: str
    arm: str
    repetition: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    num_turns: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0
    model: str = ""
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    permission_denials: list[Any] = field(default_factory=list)
    answer: str = ""
    ok: bool = False
    note: str = ""

    @property
    def billable_tokens(self) -> int:
        """Input + output only.

        Cache creation and read are reported separately: they are dominated by
        run order, not by the arm, so folding them in would let cache effects
        masquerade as a savings result.
        """
        return self.input_tokens + self.output_tokens


def claude_available() -> bool:
    return shutil.which("claude") is not None


def _mcp_config(db_path: Path | None) -> str:
    """Inline MCP config JSON for an arm (``None`` = the control arm).

    Reuses the same server entry ``hdl-kgraph setup`` writes, so the benchmark
    cannot drift from what a real user is configured with.
    """
    if db_path is None:
        return json.dumps({"mcpServers": {}})
    from hdl_kgraph.mcp.setup import plan_entry

    return json.dumps({"mcpServers": {"hdl-kgraph": plan_entry(db_path)}})


def _command(prompt_len: int, mcp_json: str, model: str | None, max_turns: int) -> list[str]:
    """The verified headless invocation.

    The prompt is *not* passed positionally — ``--mcp-config`` and ``--tools``
    are variadic and would swallow it, producing "Input must be provided either
    through stdin or as a prompt argument". It goes on stdin instead.
    """
    argv = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_json,
        "--tools",
        ARM_TOOLS,
        # Omit `project` so ./CLAUDE.md and ./.claude/CLAUDE.md are not loaded.
        # Applied to BOTH arms: suppressing it in only one would measure
        # "graph + instructions" against "nothing".
        "--setting-sources",
        "user,local",
        "--max-turns",
        str(max_turns),
    ]
    if model:
        argv += ["--model", model]
    return argv


def _parse_stream(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(init event, result event)`` from a ``stream-json`` transcript."""
    init: dict[str, Any] = {}
    result: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            result = event
    return init, result


def run_once(
    task: str,
    arm: str,
    repetition: int,
    cwd: Path,
    db_path: Path | None,
    config_dir: Path,
    model: str | None,
    max_turns: int,
    timeout_s: int,
) -> Run:
    """One headless invocation, with its provenance checked."""
    record = Run(task=task, arm=arm, repetition=repetition)
    mcp_json = _mcp_config(db_path)
    env = dict(os.environ)
    # A benchmark-owned config dir strips the user's plugins, agents, skills,
    # and hooks from both arms — the single highest-leverage noise control.
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        completed = subprocess.run(
            _command(len(task), mcp_json, model, max_turns),
            input=task,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record.note = f"invocation failed: {type(exc).__name__}: {exc}"
        return record

    init, result = _parse_stream(completed.stdout)
    if not result:
        record.note = "no result event in the transcript"
        return record

    usage = result.get("usage", {})
    record.input_tokens = int(usage.get("input_tokens", 0))
    record.output_tokens = int(usage.get("output_tokens", 0))
    record.cache_creation_tokens = int(usage.get("cache_creation_input_tokens", 0))
    record.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0))
    record.num_turns = int(result.get("num_turns", 0))
    record.duration_ms = int(result.get("duration_ms", 0))
    record.cost_usd = float(result.get("total_cost_usd", 0.0))
    record.answer = str(result.get("result", ""))
    record.permission_denials = list(result.get("permission_denials", []))
    record.model = str(init.get("model", ""))
    record.mcp_servers = list(init.get("mcp_servers", []))

    if result.get("is_error") or result.get("terminal_reason") != _SUCCESS_REASON:
        record.note = (
            f"run did not complete: terminal_reason="
            f"{result.get('terminal_reason')!r} result={record.answer[:120]!r}"
        )
        return record

    # Provenance: prove the arms actually differed, rather than trusting flags.
    connected = [s for s in record.mcp_servers if s.get("status") == "connected"]
    if arm == "graph" and not any(s.get("name") == "hdl-kgraph" for s in connected):
        record.note = "arm 'graph' had no connected hdl-kgraph server — not comparable"
        return record
    if arm == "no-graph" and connected:
        record.note = f"arm 'no-graph' had MCP servers connected: {connected} — not comparable"
        return record
    if arm == "no-graph" and record.permission_denials:
        record.note = (
            "arm 'no-graph' attempted blocked tool calls "
            f"({len(record.permission_denials)}) — it tried to reach the graph"
        )

    record.ok = True
    return record


def run(
    tasks: list[str],
    db_path: Path,
    cwd: Path,
    repeat: int = 3,
    model: str | None = None,
    max_turns: int = 12,
    timeout_s: int = 600,
) -> list[Run]:
    """Run every task through both arms *repeat* times, alternating arm order."""
    if not claude_available():
        raise AgentBenchError("the `claude` CLI is not on PATH; tier 3 needs Claude Code installed")
    runs: list[Run] = []
    with tempfile.TemporaryDirectory(prefix="hdl-kgraph-agentbench-") as tmp:
        config_dir = Path(tmp) / "claude-config"
        config_dir.mkdir()
        for task in tasks:
            for repetition in range(repeat):
                # Alternate which arm runs first: prompt cache systematically
                # favours whichever goes second, so a fixed order would bake a
                # bias straight into the result.
                order = ["graph", "no-graph"] if repetition % 2 == 0 else ["no-graph", "graph"]
                for arm in order:
                    runs.append(
                        run_once(
                            task=task,
                            arm=arm,
                            repetition=repetition,
                            cwd=cwd,
                            db_path=db_path if arm == "graph" else None,
                            config_dir=config_dir,
                            model=model,
                            max_turns=max_turns,
                            timeout_s=timeout_s,
                        )
                    )
    return runs


def summarize(runs: list[Run]) -> dict[str, Any]:
    """Per-arm medians and the caveats a reader needs to interpret them."""
    import statistics

    def series(arm: str, attribute: str) -> list[float]:
        return [float(getattr(r, attribute)) for r in runs if r.arm == arm and r.ok]

    summary: dict[str, Any] = {"arms": {}}
    for arm in ("graph", "no-graph"):
        billable = series(arm, "billable_tokens")
        summary["arms"][arm] = {
            "runs_ok": len(billable),
            "runs_total": sum(1 for r in runs if r.arm == arm),
            "median_billable_tokens": statistics.median(billable) if billable else None,
            "median_turns": statistics.median(series(arm, "num_turns")) or None,
            "median_cache_creation_tokens": (
                statistics.median(series(arm, "cache_creation_tokens")) or None
            ),
            "median_cache_read_tokens": statistics.median(series(arm, "cache_read_tokens")) or None,
            "median_cost_usd": statistics.median(series(arm, "cost_usd")) or None,
        }
    graph = summary["arms"]["graph"]["median_billable_tokens"]
    control = summary["arms"]["no-graph"]["median_billable_tokens"]
    summary["median_saved_pct"] = (
        round(100.0 * (control - graph) / control, 2) if graph is not None and control else None
    )
    summary["caveats"] = [
        "no seed or temperature control exists — these are a distribution, not a measurement",
        "arm order is alternated per repetition because prompt caching favours the second run",
        "cache tokens are reported separately; they track run order, not the arm",
        "total_cost_usd is a list-price estimate under subscription auth, not billed spend",
        "both arms ran with project CLAUDE.md suppressed and a clean CLAUDE_CONFIG_DIR",
    ]
    summary["runs"] = [
        {
            "task": r.task,
            "arm": r.arm,
            "repetition": r.repetition,
            "ok": r.ok,
            "note": r.note,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "cache_creation_tokens": r.cache_creation_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "num_turns": r.num_turns,
            "duration_ms": r.duration_ms,
            "cost_usd": r.cost_usd,
            "model": r.model,
            "answer": r.answer,
        }
        for r in runs
    ]
    return summary
