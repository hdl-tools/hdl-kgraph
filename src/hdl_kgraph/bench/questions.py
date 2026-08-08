"""Deriving a question set from whatever design the user actually has.

The benchmark has to run on *any* graph, so the questions cannot be
hand-written. They are derived from the graph itself — the top module, the
most-instantiated module, a leaf, the hottest signal, a clock net — and then
crossed with one template per graph tool.

Selection is strictly deterministic: every candidate list is ordered by
``(-count, name)``, so two runs over the same database produce byte-identical
questions and a diff between two versions is meaningful. Nothing here samples
randomly.

Deriving the *targets* from the graph is fair even though the graph is one of
the two arms: both arms are then asked the same question about the same
target. What would not be fair is deriving the *answer* from the graph, which
is why :mod:`~hdl_kgraph.bench.context` scores cost only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from hdl_kgraph.bench import baseline
from hdl_kgraph.bench.baseline import Recipe
from hdl_kgraph.bench.corpus import Corpus
from hdl_kgraph.schema import EdgeKind, NodeKind
from hdl_kgraph.storage.query import GraphQuery

#: Question templates, in report order.
TEMPLATE_KEYS = (
    "who-instantiates",
    "port-map",
    "hierarchy",
    "find-signal-drivers",
    "impact",
    "clock-domains",
)


@dataclass(frozen=True)
class Question:
    """One question, and how each arm answers it."""

    key: str
    target: str
    prompt: str
    graph: Callable[[GraphQuery], Any]
    baseline: Callable[[Corpus, int], Recipe]


def _ranked(query: GraphQuery, sql: str, params: tuple[Any, ...]) -> list[tuple[str, int]]:
    """``(name, count)`` rows, ordered by ``(-count, name)``.

    Goes through the same private connection helper ``GraphQuery`` itself uses,
    so the reader inherits its busy-timeout and schema-version behaviour.
    """
    store = query._store
    with store._connect() as conn:
        store._check_version(conn)
        rows = [(str(name), int(count)) for name, count in conn.execute(sql, params) if name]
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows


def most_instantiated(query: GraphQuery, limit: int = 3) -> list[str]:
    """Modules with the most instantiation sites — the interesting hub cases.

    Unresolved stubs are excluded. A vendor standard cell like
    ``TIELOx1_ASAP7_75t_R`` is instantiated hundreds of times but has no
    definition in the corpus, so ``who_instantiates`` answers happily while
    ``port_map`` and ``impact_of_change`` raise "no module or entity named" —
    burning question slots on rows the benchmark then has to discard.
    """
    rows = _ranked(
        query,
        """
        SELECT n.name, COUNT(*) FROM edges e JOIN nodes n ON n.id = e.dst
        WHERE e.kind = ? AND COALESCE(json_extract(n.attrs, '$.unresolved'), 0) = 0
        GROUP BY n.name
        """,
        (EdgeKind.INSTANTIATES.value,),
    )
    return [name for name, _ in rows[:limit]]


def hottest_signals(query: GraphQuery, limit: int = 2) -> list[str]:
    """Signals/ports with the most *driver* edges.

    Ranked on DRIVES alone, not DRIVES+READS: the question asked is "what
    drives S?", and a top-level input port like ``rst_n`` can have hundreds of
    readers and zero drivers. Ranking on the combined degree picks exactly
    those nets, and the graph then correctly answers "nothing" — which would
    show up as a spectacular saving on an empty answer.
    """
    rows = _ranked(
        query,
        """
        SELECT n.name, COUNT(*) FROM edges e JOIN nodes n ON n.id = e.dst
        WHERE e.kind = ? AND n.kind IN (?, ?) GROUP BY n.name
        """,
        (EdgeKind.DRIVES.value, NodeKind.SIGNAL.value, NodeKind.PORT.value),
    )
    return [name for name, _ in rows[:limit]]


def top_module(query: GraphQuery) -> str | None:
    """The most substantial top-level unit, or ``None`` if there are no roots.

    Ranked by how many instances the unit directly contains, not
    alphabetically: a design's roots typically include several trivial
    testbench shims, and picking the alphabetically-first one benchmarks
    ``hierarchy`` against an empty tree.
    """
    tops = [t["name"] for t in query.top_modules() if t.get("name")]
    if not tops:
        return None
    ranked = dict(
        _ranked(
            query,
            """
            SELECT owner.name, COUNT(*)
            FROM edges e
            JOIN nodes inst ON inst.id = e.src
            JOIN edges d ON d.dst = inst.id AND d.kind = ?
            JOIN nodes owner ON owner.id = d.src
            WHERE e.kind = ? GROUP BY owner.name
            """,
            (EdgeKind.DECLARES.value, EdgeKind.INSTANTIATES.value),
        )
    )
    return sorted(tops, key=lambda name: (-ranked.get(name, 0), name))[0]


def derive(query: GraphQuery, limit: int) -> list[Question]:
    """Up to *limit* questions for this design, in a stable order.

    Templates with no valid target are skipped rather than faked — a design
    with no clocked processes simply has no clock-domains row.
    """
    top = top_module(query)
    hubs = most_instantiated(query)
    signals = hottest_signals(query)
    # A hub is the most informative who-instantiates/impact target; the top is
    # the only sensible hierarchy root.
    questions: list[Question] = []

    def add(key: str, target: str, prompt: str, graph: Any, base: Any) -> None:
        questions.append(
            Question(key=key, target=target, prompt=prompt, graph=graph, baseline=base)
        )

    for name in hubs:
        add(
            "who-instantiates",
            name,
            f"Where is {name} instantiated?",
            lambda q, n=name: q.who_instantiates(n, 50, 0),
            lambda c, m, n=name: baseline.instances_of(c, n, m),
        )
    for name in hubs[:2]:
        add(
            "port-map",
            name,
            f"What are the ports and parameters of {name}?",
            lambda q, n=name: q.port_map(n, None),
            lambda c, m, n=name: baseline.port_map(c, n, m),
        )
    if top is not None:
        add(
            "hierarchy",
            top,
            f"What is the design hierarchy under {top}?",
            lambda q, n=top: q.hierarchy(n, 3, 500),
            lambda c, m, n=top: baseline.hierarchy(c, n, 3, m),
        )
    for name in signals:
        add(
            "find-signal-drivers",
            name,
            f"What drives the signal {name}?",
            lambda q, n=name: q.find_signal_drivers(n, None, False, 50, 0),
            lambda c, m, n=name: baseline.signal_drivers(c, n, m),
        )
    for name in hubs[:2]:
        add(
            "impact",
            name,
            f"What breaks if {name} changes?",
            lambda q, n=name: q.impact_of_change(n, 0, 100, 0),
            lambda c, m, n=name: baseline.impact(c, n, m),
        )
    add(
        "clock-domains",
        "<whole design>",
        "What are the clock domains, and which crossings are CDC suspects?",
        lambda q: q.clock_domains(),
        lambda c, m: baseline.clock_domains(c, m),
    )

    # Interleave by template so a small --questions still covers several tools
    # rather than three who-instantiates rows.
    by_key: dict[str, list[Question]] = {key: [] for key in TEMPLATE_KEYS}
    for question in questions:
        by_key[question.key].append(question)
    ordered: list[Question] = []
    for rank in range(max((len(v) for v in by_key.values()), default=0)):
        for key in TEMPLATE_KEYS:
            if rank < len(by_key[key]):
                ordered.append(by_key[key][rank])
    return ordered[:limit]
