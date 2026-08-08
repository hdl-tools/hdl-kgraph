"""Tier 1: what each answer costs, graph vs. grep.

For every derived question, both arms run and both are priced with the same
estimator. The graph arm's cost is the JSON envelope an MCP tool would return
(:mod:`hdl_kgraph.storage.query` shapes it identically for MCP and for
``hdl-kgraph tools``); the baseline arm's cost is the grep output plus the
files read.

This measures **cost only**. Correctness is out of scope by construction — the
graph cannot be scored against itself. See :mod:`~hdl_kgraph.bench.fidelity`.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hdl_kgraph.bench import FAIRNESS_RULES, questions
from hdl_kgraph.bench.baseline import DEFAULT_MAX_FILES
from hdl_kgraph.bench.corpus import Corpus, load_corpus
from hdl_kgraph.bench.tokens import Estimator
from hdl_kgraph.cli.render import json_default
from hdl_kgraph.storage.query import GraphQuery


@dataclass
class Row:
    """One question, priced both ways."""

    key: str
    target: str
    prompt: str
    graph_tokens: int
    graph_bytes: int
    graph_ms: float
    baseline_tokens: int
    baseline_bytes: int
    baseline_ms: float
    baseline_hit_files: int
    baseline_files_read: int
    capped: bool
    #: The graph answered, but the answer was empty. Such a row is shown and
    #: then excluded from the headline: "0 tokens instead of 40 000" is not a
    #: saving if the 0 tokens contain no answer.
    graph_empty: bool = False
    recipe: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def scored(self) -> bool:
        """Whether this row contributes to the summary statistics."""
        return self.error is None and not self.graph_empty

    @property
    def saved_pct(self) -> float:
        """Percent of the baseline's tokens the graph avoids.

        Negative when the graph costs *more* — a real outcome for cheap
        lookups, and one the report prints rather than hides.
        """
        if self.baseline_tokens == 0:
            return 0.0
        return 100.0 * (self.baseline_tokens - self.graph_tokens) / self.baseline_tokens


@dataclass
class Report:
    """Everything a reader needs to judge the numbers."""

    db: str
    root: str
    tokenizer: str
    baseline_max_files: int
    corpus_files: int
    corpus_bytes: int
    corpus_tokens: int
    stale_missing: list[str]
    rows: list[Row]

    @property
    def median_saved_pct(self) -> float:
        scored = [r.saved_pct for r in self.rows if r.scored]
        return statistics.median(scored) if scored else 0.0

    @property
    def total_saved_pct(self) -> float:
        """Aggregate over summed tokens — deliberately reported alongside the
        median, because a token-savings distribution is extremely long-tailed
        and either statistic alone is misleading."""
        base = sum(r.baseline_tokens for r in self.rows if r.scored)
        graph = sum(r.graph_tokens for r in self.rows if r.scored)
        return 100.0 * (base - graph) / base if base else 0.0

    @property
    def unscored(self) -> list[Row]:
        """Rows excluded from the headline, and therefore owed an explanation."""
        return [r for r in self.rows if not r.scored]

    def to_json(self) -> dict[str, Any]:
        return {
            "db": self.db,
            "root": self.root,
            "tokenizer": self.tokenizer,
            "baseline_max_files": self.baseline_max_files,
            "corpus": {
                "files": self.corpus_files,
                "bytes": self.corpus_bytes,
                # The read-everything ceiling: no baseline can exceed it, so it
                # is the honest upper bound on any savings claim.
                "tokens_if_fully_read": self.corpus_tokens,
                "indexed_but_missing_on_disk": self.stale_missing,
            },
            "fairness_rules": list(FAIRNESS_RULES),
            "summary": {
                "questions": len(self.rows),
                "questions_scored": sum(1 for r in self.rows if r.scored),
                "questions_excluded": len(self.unscored),
                "median_saved_pct": round(self.median_saved_pct, 2),
                "total_saved_pct": round(self.total_saved_pct, 2),
            },
            "rows": [
                {
                    "question": row.key,
                    "target": row.target,
                    "prompt": row.prompt,
                    "graph_tokens": row.graph_tokens,
                    "graph_bytes": row.graph_bytes,
                    "graph_ms": round(row.graph_ms, 3),
                    "baseline_tokens": row.baseline_tokens,
                    "baseline_bytes": row.baseline_bytes,
                    "baseline_ms": round(row.baseline_ms, 3),
                    "baseline_hit_files": row.baseline_hit_files,
                    "baseline_files_read": row.baseline_files_read,
                    "capped": row.capped,
                    "graph_empty": row.graph_empty,
                    "scored": row.scored,
                    "saved_pct": round(row.saved_pct, 2),
                    "recipe": row.recipe,
                    "error": row.error,
                }
                for row in self.rows
            ],
        }


def _serialize(payload: Any) -> str:
    """The exact text an MCP tool would put in the model's context."""
    return json.dumps(payload, indent=2, default=json_default)


def _is_empty(payload: Any) -> bool:
    """Whether a graph answer is an answer at all.

    A tool that legitimately finds nothing still returns a small envelope, and
    pricing that against a 40 000-token grep would manufacture a 99.9%
    "saving" out of a non-answer. Covers the three envelope shapes the tools
    produce: the pagination wrapper, the hierarchy tree, and the whole-design
    summaries.
    """
    if not isinstance(payload, dict):
        return not payload
    if "total" in payload:
        return not payload.get("total")
    if "items" in payload:
        return not payload.get("items")
    if "root" in payload:
        root = payload.get("root") or {}
        return not (isinstance(root, dict) and root.get("children"))
    # port_map and the summaries: empty when every list they carry is empty.
    lists = [v for v in payload.values() if isinstance(v, list)]
    return bool(lists) and not any(lists)


def _run_row(
    question: questions.Question,
    query: GraphQuery,
    corpus: Corpus,
    estimator: Estimator,
    max_files: int,
) -> Row:
    started = time.perf_counter()
    error: str | None = None
    empty = False
    try:
        payload = question.graph(query)
        graph_text = _serialize(payload)
        empty = _is_empty(payload)
    except (ValueError, KeyError) as exc:
        # A template whose target vanished (or a graph with no such analysis)
        # is a skipped row, not a crashed benchmark.
        graph_text = ""
        error = f"{type(exc).__name__}: {exc}"
    graph_ms = (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    recipe = question.baseline(corpus, max_files)
    baseline_ms = (time.perf_counter() - started) * 1000

    return Row(
        key=question.key,
        target=question.target,
        prompt=question.prompt,
        graph_tokens=estimator.count(graph_text),
        graph_bytes=len(graph_text),
        graph_ms=graph_ms,
        baseline_tokens=estimator.count(recipe.text),
        baseline_bytes=recipe.bytes,
        baseline_ms=baseline_ms,
        baseline_hit_files=len(recipe.hit_files),
        baseline_files_read=len(recipe.files_read),
        capped=recipe.capped,
        graph_empty=empty,
        recipe=list(recipe.commands),
        error=error,
    )


def run(
    db_path: Path,
    estimator: Estimator,
    limit: int = 12,
    max_files: int = DEFAULT_MAX_FILES,
) -> Report:
    """Price every derived question both ways."""
    corpus = load_corpus(db_path)
    query = GraphQuery(db_path)
    rows = [
        _run_row(question, query, corpus, estimator, max_files)
        for question in questions.derive(query, limit)
    ]
    return Report(
        db=str(db_path),
        root=str(corpus.root),
        tokenizer=estimator.name,
        baseline_max_files=max_files,
        corpus_files=len(corpus.files),
        corpus_bytes=corpus.total_bytes,
        corpus_tokens=sum(estimator.count(corpus.read(f.relpath)) for f in corpus.files),
        stale_missing=list(corpus.missing),
        rows=rows,
    )
