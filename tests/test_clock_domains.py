"""Clock-domain / reset-tree / CDC tests (M5)."""

from pathlib import Path

import pytest

from hdl_kgraph.graph import clocks
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.vhdl import VhdlParser
from hdl_kgraph.schema import EdgeKind


@pytest.fixture(scope="module")
def graph(fixtures_dir: Path):
    sv = SystemVerilogParser()
    irs = [
        sv.parse(Path("two_clock_cdc.sv"), (fixtures_dir / "two_clock_cdc.sv").read_text()),
        VhdlParser().parse(Path("dataflow.vhd"), (fixtures_dir / "dataflow.vhd").read_text()),
    ]
    return build_graph(irs)


def _edges(g, kind):
    return [(u, v, d) for u, v, d in g.edges(data=True) if d["kind"] is kind]


def test_single_edge_sensitivity_is_definitive_clock(graph) -> None:
    edge = next(
        d
        for u, v, d in _edges(graph, EdgeKind.CLOCKED_BY)
        if u.endswith("two_clock_top.always@28") and v.endswith("two_clock_top.clk_b")
    )
    assert edge["confidence"] == 1.0
    assert edge["attrs"]["evidence"] == "sensitivity"
    assert edge["attrs"]["edge"] == "posedge"


def test_async_reset_from_sensitivity_at_full_confidence(graph) -> None:
    edge = next(
        d
        for u, v, d in _edges(graph, EdgeKind.RESETS)
        if u.endswith("two_clock_top.always@22") and v.endswith("two_clock_top.rst_n")
    )
    assert edge["confidence"] == 1.0
    assert edge["attrs"]["is_async"] is True


def test_vhdl_rising_edge_is_definitive_clock(graph) -> None:
    edge = next(d for u, v, d in _edges(graph, EdgeKind.CLOCKED_BY) if u.endswith("rtl.reg_p"))
    assert edge["confidence"] == 1.0
    assert edge["attrs"]["evidence"] == "edge_function"


def test_vhdl_reset_name_heuristic(graph) -> None:
    edge = next(d for u, v, d in _edges(graph, EdgeKind.RESETS) if u.endswith("rtl.reg_p"))
    assert edge["confidence"] == 0.4
    assert edge["attrs"]["evidence"] == "name"


def test_child_clock_port_aliases_with_top_clock(graph) -> None:
    domains = clocks.clock_domains(graph)
    sv_domains = [d for d in domains if any("clk" in n for n in d.clock_names)]
    # clk_a and clk_b only — cdc_child.clk merged into clk_b's domain.
    by_names = {tuple(d.clock_names) for d in sv_domains}
    assert ("clk", "clk_b") in by_names
    assert ("clk_a",) in by_names
    assert len([d for d in sv_domains if "clk_b" in d.clock_names]) == 1


def test_two_domains_and_exactly_one_cdc_suspect(graph) -> None:
    suspects = [s for s in clocks.cdc_suspects(graph) if s.signal_name == "data_a"]
    assert len(suspects) == 1
    suspect = suspects[0]
    assert suspect.driver_domain == "clk_a"
    assert suspect.reader_domain in ("clk_b", "clk")  # representative name
    assert suspect.confidence == 1.0
    # ...and nothing else in the fixture crosses domains.
    assert all(s.signal_name == "data_a" for s in clocks.cdc_suspects(graph))


def test_reset_tree_groups_by_net(graph) -> None:
    groups = clocks.reset_tree(graph)
    rst = next(g for g in groups if "rst_n" in g.reset_names and g.is_async)
    assert rst.process_ids  # the clk_a flop
    assert rst.min_confidence == 1.0


@pytest.mark.parametrize(
    "name",
    [
        "rst",
        "RST",
        "rst_n",
        "rstn",
        "reset",
        "resetn",
        "reset_b",
        "clr",
        "clear",
        "sys_rst",
        "cpu_rst_n",
    ],
)
def test_reset_name_re_matches_control_names(name: str) -> None:
    from hdl_kgraph.parser.base import RESET_NAME_RE

    assert RESET_NAME_RE.search(name), name


@pytest.mark.parametrize(
    "name", ["clear_count", "reset_value", "restart_addr", "cluster", "data", "rst_count"]
)
def test_reset_name_re_rejects_data_names(name: str) -> None:
    from hdl_kgraph.parser.base import RESET_NAME_RE
    from hdl_kgraph.parser.systemverilog import RESET_NAME_RE as SV_RESET_RE
    from hdl_kgraph.parser.vhdl import RESET_NAME_RE as VHDL_RESET_RE

    # The substring patterns used to misfire on these data names (#76). The
    # pattern now accepts trailing qualifiers so that `rst_n_i` reads as a
    # reset, but those qualifiers are a whitelist rather than `.*` — precisely
    # so these stay rejected. Both backends share one object, so they cannot
    # drift back apart.
    assert not RESET_NAME_RE.search(name), name
    assert SV_RESET_RE is RESET_NAME_RE
    assert VHDL_RESET_RE is RESET_NAME_RE


@pytest.mark.parametrize(
    "name,hit",
    [
        ("clk", True),
        ("clock", True),
        ("sys_clk", True),
        ("clk_b", True),
        ("clock_div", False),
        ("clk_enable", False),
        ("blockram", False),
    ],
)
def test_vhdl_clock_name_re(name: str, hit: bool) -> None:
    from hdl_kgraph.parser.vhdl import _CLOCK_NAME_RE

    assert bool(_CLOCK_NAME_RE.search(name)) is hit, name
