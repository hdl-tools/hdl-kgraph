"""Pass-3 phase profiler wiring (enrich._profile).

These cover the profiler end to end *without* pyslang (the slang internal spans
need the `enrich` extra): a fake backend drives the runner, exercising the
top-level ``{name}:enrich`` / ``{name}:apply`` spans and a backend-emitted
detail span, plus the CLI rendering of the breakdown.
"""

from __future__ import annotations

from pathlib import Path

import networkx as nx

from hdl_kgraph.enrich import _profile, run_enrichment
from hdl_kgraph.enrich.base import Capabilities, EnrichmentInput, EnrichmentResult
from hdl_kgraph.schema import Edge, EdgeKind, Language, Node, NodeKind


class _FakeBackend:
    """A minimal EnrichmentBackend that adds one elaborated node + edge and
    self-instruments a detail phase, so the runner's spans can be asserted on a
    box without the native frontend installed."""

    name = "fake"
    suffixes = frozenset({".sv"})

    def available(self) -> bool:
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def enrich(self, inp: EnrichmentInput, graph: nx.MultiDiGraph) -> EnrichmentResult:
        result = EnrichmentResult()
        with _profile.phase("fake/work"):  # a detail child of fake:enrich
            result.new_nodes.append(
                Node(
                    id="elab:instance:top.u",
                    kind=NodeKind.INSTANCE,
                    name="u",
                    language=Language.SYSTEMVERILOG,
                )
            )
            result.new_edges.append(
                Edge(src="m", dst="elab:instance:top.u", kind=EdgeKind.DECLARES)
            )
        _profile.add("fake/segment", 0.001)  # a pre-measured raw duration
        _profile.count("fake_instances", 3)  # an integer tally
        return result


def test_run_enrichment_records_phase_timings() -> None:
    graph = nx.MultiDiGraph()
    graph.add_node("m", kind=NodeKind.MODULE, name="m", attrs={})

    report = run_enrichment(graph, EnrichmentInput(files=[Path("x.sv")]), [_FakeBackend()])

    # Top-level spans tile the pass; the backend's detail span is recorded too.
    assert "fake:enrich" in report.phase_timings
    assert "fake:apply" in report.phase_timings
    assert "fake/work" in report.phase_timings
    assert report.phase_timings["fake/segment"] >= 0.001  # add() raw duration
    assert all(v >= 0 for v in report.phase_timings.values())
    # Integer tallies travel on a separate channel.
    assert report.phase_counts["fake_instances"] == 3
    # The delta was actually applied (node added under the apply span).
    assert graph.has_node("elab:instance:top.u")


def test_profiler_active_timer_cleared_after_pass() -> None:
    # The runner must unset the module-global timer even though the pass mutated
    # it, so a later un-profiled backend call (e.g. a unit test) is a no-op.
    run_enrichment(nx.MultiDiGraph(), EnrichmentInput(files=[Path("x.sv")]), [_FakeBackend()])
    assert _profile._active is None
    # phase() outside a pass does not raise and records nothing.
    with _profile.phase("orphan"):
        pass


def test_echo_enrich_phases_rendered(capsys) -> None:
    from hdl_kgraph.cli.build import _echo_timings
    from hdl_kgraph.pipeline import BuildReport

    report = BuildReport(
        root=Path("."),
        db_path=Path("graph.db"),
        parse_s=10.0,
        link_s=2.0,
        enrich_s=8.0,
        persist_s=1.0,
        enrich_phase_s={
            "slang:enrich": 7.0,
            "slang:apply": 1.0,
            "slang/elaborate_root": 5.0,
            "slang/walk_tree": 1.5,
        },
        enrich_phase_counts={"walk_instances": 3_000_000},
    )
    _echo_timings(report)
    out = capsys.readouterr().out

    assert "enrich phases (% of pass 3)" in out
    assert "slang:enrich" in out
    assert "slang/elaborate_root" in out
    # Percentage is of the pass (5.0 / 8.0 = 62.5%), not the whole build.
    assert "62.5%" in out
    # Per-instance cost line: 1.5s / 3,000,000 = 0.50 us/instance.
    assert "walk_instances" in out
    assert "0.50 us/instance" in out
    assert "3,000,000" in out
    assert "0.50 us/instance" in out


def test_echo_enrich_phases_absent_without_enrich(capsys) -> None:
    from hdl_kgraph.cli.build import _echo_timings
    from hdl_kgraph.pipeline import BuildReport

    report = BuildReport(root=Path("."), db_path=Path("graph.db"), parse_s=5.0, link_s=1.0)
    _echo_timings(report)
    out = capsys.readouterr().out
    assert "enrich phases" not in out
