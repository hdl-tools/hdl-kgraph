"""Benchmarks for the *savings* claim, not just the latency one.

``hdl-kgraph setup`` seeds every AI assistant's instruction file with a claim
about cost and correctness — "query the graph instead of grepping the raw
RTL" (:data:`hdl_kgraph.mcp.setup.INSTRUCTIONS_BODY`). The project already
measures latency (``scripts/bench_query.py``); this package lets a *user*
check the rest of the claim against their own design:

* :mod:`~hdl_kgraph.bench.context` — how many tokens each answer costs, graph
  vs. a scripted grep-and-read baseline (deterministic, offline, free);
* :mod:`~hdl_kgraph.bench.fidelity` — where grep gets the answer *wrong*,
  against hand-written ground truth (deterministic, offline, free);
* :mod:`~hdl_kgraph.bench.agent` — a live Claude Code A/B (opt-in, spends
  money, and non-deterministic by construction).

The credibility of all of this is in the baseline, so the fairness rules are
enforced in code and printed in every report — see
:mod:`~hdl_kgraph.bench.baseline`.
"""

from __future__ import annotations

__all__ = ["FAIRNESS_RULES"]

#: Printed under every ``bench context`` report. These are the claims a
#: skeptical reader is entitled to check, so they ship with the numbers rather
#: than living only in the docs.
FAIRNESS_RULES = (
    "both arms see exactly the HDL files the graph indexed (a real agent would "
    "grep more, so this favours the baseline)",
    "the baseline reads at most --baseline-max-files hit files; rows where that "
    "cap binds are flagged `capped`",
    "every baseline recipe is printed, so the search can be reproduced by hand",
    "rows where the baseline wins or ties are reported, not suppressed",
    "cost only — correctness is not scored here, because the graph would be "
    "both contestant and judge (see `hdl-kgraph bench fidelity`)",
)
