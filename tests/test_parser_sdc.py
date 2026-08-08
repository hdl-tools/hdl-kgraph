"""SDC/XDC parser + linking tests (M10 first wedge, issue #25).

Covers the pass-1 extraction (CLOCK / TIMING_CONSTRAINT nodes, CONSTRAINS
refs), the pass-2 object-query resolution (exact 1.0 / glob 0.8 / ambiguous
0.6), and the M5 synergy: ``create_clock`` upgrades CLOCKED_BY to 1.0 and
``set_clock_groups -asynchronous`` suppresses the CDC suspect it covers.
"""

from pathlib import Path

import pytest

from hdl_kgraph.graph import clocks
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.tcl import SdcParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind


def parse_sdc(fixtures_dir: Path, name: str) -> FileIR:
    return SdcParser().parse(Path("tests/fixtures") / name, (fixtures_dir / name).read_text())


def parse_sv(fixtures_dir: Path, name: str) -> FileIR:
    return SystemVerilogParser().parse(
        Path("tests/fixtures") / name, (fixtures_dir / name).read_text()
    )


def nodes_of(ir: FileIR, kind: NodeKind) -> dict[str, object]:
    return {n.name: n for n in ir.nodes if n.kind is kind}


def constrains_of(graph, src_substr: str) -> dict[str, float]:
    """{target node name: confidence} for CONSTRAINS edges out of *src_substr*."""
    return {
        graph.nodes[v]["name"]: d["confidence"]
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.CONSTRAINS and src_substr in u
    }


# --------------------------------------------------------------------------- #
# Pass 1: extraction
# --------------------------------------------------------------------------- #
def test_sdc_extracts_clocks(fixtures_dir) -> None:
    ir = parse_sdc(fixtures_dir, "sdc/constraints.sdc")
    assert ir.parse_error_count == 0
    clocks_by_name = nodes_of(ir, NodeKind.CLOCK)
    assert set(clocks_by_name) == {"sys_clk", "div_clk"}
    assert clocks_by_name["sys_clk"].language is Language.TCL
    assert clocks_by_name["sys_clk"].attrs["period"] == "10.000"
    assert clocks_by_name["div_clk"].attrs["generated"] is True
    assert clocks_by_name["div_clk"].attrs["divide_by"] == "2"


def test_sdc_extracts_timing_constraints(fixtures_dir) -> None:
    ir = parse_sdc(fixtures_dir, "sdc/constraints.sdc")
    set_types = {n.attrs["set_type"] for n in ir.nodes if n.kind is NodeKind.TIMING_CONSTRAINT}
    assert set_types == {
        "clock_groups",
        "false_path",
        "multicycle_path",
        "input_delay",
        "output_delay",
    }
    groups = next(n for n in ir.nodes if n.attrs.get("set_type") == "clock_groups")
    assert groups.attrs["asynchronous"] is True
    assert groups.attrs["groups"] == [["sys_clk"], ["div_clk"]]


def test_sdc_emits_constrains_refs(fixtures_dir) -> None:
    ir = parse_sdc(fixtures_dir, "sdc/constraints.sdc")
    refs = [r for r in ir.unresolved_refs if r.edge_kind is EdgeKind.CONSTRAINS]
    # The create_clock targets its port; the clock_groups its two clocks.
    queries = {(r.attrs["query"], r.target_name) for r in refs}
    assert ("ports", "clk") in queries
    assert ("clocks", "sys_clk") in queries
    assert ("ports", "value*") in queries


def test_sdc_parser_tolerates_garbage(fixtures_dir) -> None:
    ir = SdcParser().parse(Path("junk.sdc"), "create_clock\n}}}  $undef [get_ports\nnonsense {{{\n")
    assert ir.parse_error_count == 0  # malformed input is tolerated, never fatal


# --------------------------------------------------------------------------- #
# Pass 2: object-query resolution confidence
# --------------------------------------------------------------------------- #
def test_constrains_confidence_tiers(fixtures_dir) -> None:
    graph = build_graph(
        [
            parse_sv(fixtures_dir, "top.v"),
            parse_sv(fixtures_dir, "simple_counter.sv"),
            parse_sdc(fixtures_dir, "sdc/constraints.sdc"),
        ]
    )
    # get_ports value* is a glob with a single match (top.value) -> 0.8.
    multicycle = constrains_of(graph, "timing_constraint:multicycle_path")
    assert multicycle["value"] == pytest.approx(0.8)
    # get_ports rst_n matches a port in BOTH modules -> ambiguous 0.6.
    false_path = constrains_of(graph, "timing_constraint:false_path")
    assert false_path["rst_n"] == pytest.approx(0.6)


def test_constrains_exact_unique_is_resolved(fixtures_dir) -> None:
    graph = build_graph(
        [parse_sv(fixtures_dir, "two_clock_cdc.sv"), parse_sdc(fixtures_dir, "sdc/two_clock.sdc")]
    )
    # clk_a / clk_b are unique top-level ports -> exact match at 1.0.
    assert constrains_of(graph, "clock:clk_a")["clk_a"] == pytest.approx(1.0)
    assert constrains_of(graph, "clock:clk_b")["clk_b"] == pytest.approx(1.0)


def test_constrains_unresolved_object_is_skipped(fixtures_dir) -> None:
    """A constraint naming an object this design lacks adds no edge and no stub."""
    sdc = SdcParser().parse(Path("x.sdc"), "create_clock -name c [get_ports nonexistent_pin]\n")
    graph = build_graph([parse_sv(fixtures_dir, "two_clock_cdc.sv"), sdc])
    assert not any(d["kind"] is EdgeKind.CONSTRAINS for _, _, d in graph.edges(data=True))


# --------------------------------------------------------------------------- #
# M5 synergy: create_clock evidence upgrade
# --------------------------------------------------------------------------- #
def _clocked_by(graph) -> dict[str, tuple[float, object]]:
    return {
        graph.nodes[v]["name"]: (d["confidence"], d["attrs"].get("evidence"))
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.CLOCKED_BY
    }


def test_create_clock_upgrades_clocked_by(fixtures_dir) -> None:
    sv = parse_sv(fixtures_dir, "sdc/sdc_gated.sv")
    assert _clocked_by(build_graph([sv]))["gclk"][0] == pytest.approx(0.4)
    upgraded = _clocked_by(build_graph([sv, parse_sdc(fixtures_dir, "sdc/sdc_gated.sdc")]))
    assert upgraded["gclk"] == (pytest.approx(1.0), "sdc_create_clock")
    # altclk is not named by an SDC create_clock — it stays a 0.4 heuristic.
    assert upgraded["altclk"][0] == pytest.approx(0.4)


# --------------------------------------------------------------------------- #
# M5 synergy: CDC suppression (the ROADMAP acceptance)
# --------------------------------------------------------------------------- #
def test_set_clock_groups_suppresses_cdc_suspect(fixtures_dir) -> None:
    sv = parse_sv(fixtures_dir, "two_clock_cdc.sv")
    baseline = clocks.cdc_suspects(build_graph([sv]))
    assert [s.signal_name for s in baseline] == ["data_a"]
    assert baseline[0].declared_safe is False

    suspects = clocks.cdc_suspects(build_graph([sv, parse_sdc(fixtures_dir, "sdc/two_clock.sdc")]))
    assert [s.signal_name for s in suspects] == ["data_a"]
    assert suspects[0].declared_safe is True  # set_clock_groups -asynchronous covers it


_CREATE_CLOCKS = (
    "create_clock -name clk_a -period 10.0 [get_ports clk_a]\n"
    "create_clock -name clk_b -period 7.0 [get_ports clk_b]\n"
)


def test_false_path_suppression_is_directional(fixtures_dir) -> None:
    """``set_false_path -from A -to B`` suppresses the A->B crossing only.

    The crossing in two_clock_cdc.sv is clk_a (driver) -> clk_b (reader), so a
    forward false path declares it safe but the reverse one must not — a
    frozenset would wrongly suppress both directions.
    """
    sv = parse_sv(fixtures_dir, "two_clock_cdc.sv")

    forward = SdcParser().parse(
        Path("fwd.sdc"),
        _CREATE_CLOCKS + "set_false_path -from [get_clocks clk_a] -to [get_clocks clk_b]\n",
    )
    fwd_suspects = clocks.cdc_suspects(build_graph([sv, forward]))
    assert [s.signal_name for s in fwd_suspects] == ["data_a"]
    assert fwd_suspects[0].declared_safe is True

    reverse = SdcParser().parse(
        Path("rev.sdc"),
        _CREATE_CLOCKS + "set_false_path -from [get_clocks clk_b] -to [get_clocks clk_a]\n",
    )
    rev_suspects = clocks.cdc_suspects(build_graph([sv, reverse]))
    assert [s.signal_name for s in rev_suspects] == ["data_a"]
    assert rev_suspects[0].declared_safe is False  # reverse exception does not cover clk_a -> clk_b


def test_bare_clock_groups_async_suppresses_all(fixtures_dir) -> None:
    """``set_clock_groups -asynchronous`` with no -group marks all clocks async."""
    sv = parse_sv(fixtures_dir, "two_clock_cdc.sv")
    sdc = SdcParser().parse(Path("bare.sdc"), _CREATE_CLOCKS + "set_clock_groups -asynchronous\n")
    suspects = clocks.cdc_suspects(build_graph([sv, sdc]))
    assert [s.signal_name for s in suspects] == ["data_a"]
    assert suspects[0].declared_safe is True


def test_clock_summary_partitions_suppressed(fixtures_dir) -> None:
    from hdl_kgraph.graph.summary import clock_summary

    sv = parse_sv(fixtures_dir, "two_clock_cdc.sv")
    payload = clock_summary(build_graph([sv, parse_sdc(fixtures_dir, "sdc/two_clock.sdc")]))
    assert payload["cdc_suspect_count"] == 0  # the only crossing is declared safe
    assert payload["cdc_suppressed_count"] == 1
    assert payload["cdc_suppressed"][0]["signal_name"] == "data_a"
