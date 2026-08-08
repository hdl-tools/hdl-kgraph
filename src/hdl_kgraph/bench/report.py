"""Text rendering for the benchmark reports (``--json`` is the other surface)."""

from __future__ import annotations

import click

from hdl_kgraph.bench import FAIRNESS_RULES
from hdl_kgraph.bench.context import Report
from hdl_kgraph.bench.fidelity import FidelityReport

_HEADER = f"{'question':<22}{'target':<26}{'graph':>9}{'grep':>11}{'saved':>9}  notes"


def _thousands(value: int) -> str:
    return f"{value:,}"


def render_context(report: Report) -> str:
    """The human table for ``bench context``."""
    lines = [
        f"database:   {report.db}",
        f"corpus:     {report.corpus_files} indexed HDL files, "
        f"{_thousands(report.corpus_bytes)} bytes "
        f"({_thousands(report.corpus_tokens)} tokens if fully read — the ceiling "
        f"no baseline can exceed)",
        f"tokenizer:  {report.tokenizer}",
        f"baseline:   grep the indexed HDL, then read up to {report.baseline_max_files} hit files",
        "",
        _HEADER,
        "-" * len(_HEADER),
    ]
    for row in report.rows:
        notes = []
        if row.graph_empty:
            notes.append("EXCLUDED: graph answer was empty")
        if row.capped:
            notes.append(f"capped at {row.baseline_files_read}/{row.baseline_hit_files} files")
        if row.error:
            notes.append(f"EXCLUDED: graph {row.error}")
        if row.scored and row.saved_pct < 0:
            notes.append("baseline cheaper")
        saved = "     --  " if not row.scored else f"{row.saved_pct:>8.1f}%"
        lines.append(
            f"{row.key:<22}{row.target[:25]:<26}"
            f"{_thousands(row.graph_tokens):>9}"
            f"{_thousands(row.baseline_tokens):>11}"
            f"{saved}  {'; '.join(notes)}".rstrip()
        )
    scored = len(report.rows) - len(report.unscored)
    excluded = f", {len(report.unscored)} excluded" if report.unscored else ""
    lines += [
        "-" * len(_HEADER),
        f"median saving {report.median_saved_pct:.1f}%   "
        f"aggregate saving {report.total_saved_pct:.1f}%   "
        f"({scored} of {len(report.rows)} questions scored{excluded})",
        "",
        "Token counts are estimates from the named tokenizer; the same estimator",
        "prices both arms, so the ratio holds even where the absolute count does not.",
        "",
        "How the baseline is kept honest:",
    ]
    lines += [f"  - {rule}" for rule in FAIRNESS_RULES]
    if report.unscored:
        lines += [
            f"  - a row whose graph answer was empty or errored is shown but left out "
            f"of the headline ({len(report.unscored)} here) — a cheap non-answer is "
            f"not a saving",
        ]
    if report.stale_missing:
        lines += [
            "",
            f"WARNING: {len(report.stale_missing)} indexed file(s) are no longer on disk — "
            "the database is stale. Run `hdl-kgraph update` and re-run.",
        ]
    return "\n".join(lines)


def render_fidelity(report: FidelityReport) -> str:
    """The correctness table for ``bench fidelity``."""
    lines = [
        f"suite:  {report.suite}",
        f"cases:  {len(report.cases)}",
        "",
    ]
    for case in report.cases:
        lines.append(f"{case.name}  [{case.verdict}]")
        lines.append(f"  question:     {case.question}")
        lines.append(f"  ground truth: {case.expected}")
        lines.append(f"  graph:        {case.graph_answer}")
        lines.append(f"  grep:         {case.grep_answer}")
        lines.append(f"  why grep fails: {case.why}")
        lines.append("")
    graph_ok = sum(1 for c in report.cases if c.graph_correct)
    grep_ok = sum(1 for c in report.cases if c.grep_correct)
    lines += [
        f"graph correct: {graph_ok}/{len(report.cases)}    "
        f"grep correct: {grep_ok}/{len(report.cases)}",
        "",
        "These are hand-authored cases with human-written ground truth, chosen to",
        "cover the failure modes a line-oriented search genuinely has. They are a",
        "qualitative catalogue, not a benchmark score — a different case mix would",
        "give a different ratio.",
    ]
    return "\n".join(lines)


def echo(text: str) -> None:
    click.echo(text)
