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


# --------------------------------------------------------------------------- #
# #176: multi-instance clock aliasing collapses distinct domains
# --------------------------------------------------------------------------- #
def _sv_graph(fixtures_dir: Path, *names: str):
    sv = SystemVerilogParser()
    return build_graph([sv.parse(Path(n), (fixtures_dir / n).read_text()) for n in names])


def test_swapped_instantiation_collapses_and_is_flagged(fixtures_dir: Path) -> None:
    """The motivating bug: one dual-clock module, two instances, actuals swapped.

    The collapse itself is not fixed — name-level aliasing cannot separate the
    nets — but it must no longer be silent.
    """
    g = _sv_graph(fixtures_dir, "multi_instance_clock.sv")
    domains = clocks.clock_domains(g)

    # Still one merged domain, and still no crossings: this is the false zero.
    assert len(domains) == 1
    assert clocks.cdc_suspects(g) == []
    # ...but it is now reported as such.
    assert domains[0].collapsed is True
    assert domains[0].collapse_sites


def test_collapse_names_the_conflicting_nets(fixtures_dir: Path) -> None:
    g = _sv_graph(fixtures_dir, "multi_instance_clock.sv")
    (domain,) = clocks.clock_domains(g)

    ports = {g.nodes[s.formal_id]["name"] for s in domain.collapse_sites}
    assert ports == {"wr_clk_i", "rd_clk_i"}
    for site in domain.collapse_sites:
        assert {g.nodes[n]["name"] for n in site.net_ids} == {"s_clk_i", "m_clk_i"}


def test_collapse_ignores_data_ports(fixtures_dir: Path) -> None:
    """`d_i`/`q_o` also bind two actuals; only clock-domain roots may be flagged.

    Without that gate the predicate fires on every data port of every multiply
    instantiated leaf — i.e. on every design.
    """
    g = _sv_graph(fixtures_dir, "multi_instance_clock.sv")
    flagged = {
        g.nodes[s.formal_id]["name"] for d in clocks.clock_domains(g) for s in d.collapse_sites
    }
    assert not flagged & {"d_i", "q_o"}


def test_shared_leaf_across_parents_is_not_flagged(fixtures_dir: Path) -> None:
    """A leaf instantiated under two parents binds one formal to two distinct
    actuals — but both trace to one grandparent clock through single-actual
    bindings, so the merge is corroborated and must not be reported."""
    g = _sv_graph(fixtures_dir, "shared_leaf_clock.sv")
    assert [d.collapsed for d in clocks.clock_domains(g)] == [False]


def test_ordinary_hierarchy_is_not_flagged(graph) -> None:
    """The canonical fixture instantiates its child once: nothing to collapse."""
    assert not any(d.collapsed for d in clocks.clock_domains(graph))


def test_reset_groups_collapse_the_same_way(fixtures_dir: Path) -> None:
    """Resets alias through the same shared formals, so they collapse too.

    The clock in this fixture is bound to the same actual by both instances,
    so it must stay unflagged — the marking is per net, not per module.
    """
    g = _sv_graph(fixtures_dir, "multi_instance_reset.sv")

    (group,) = clocks.reset_tree(g)
    assert group.reset_names == ["a_rst_n_i", "b_rst_n_i"]
    assert group.collapsed is True
    assert [d.collapsed for d in clocks.clock_domains(g)] == [False]


def test_ordinary_reset_tree_is_not_flagged(graph) -> None:
    assert not any(r.collapsed for r in clocks.reset_tree(graph))


def test_alias_collapses_detects_the_swapped_binding_cycle() -> None:
    """Swapped bindings make the formals and actuals a 4-cycle (K2,2).

    Neither formal is then a cut vertex, so an articulation-point test reports
    nothing here — the whole point of corroborating with single-actual edges.
    """
    pairs = [("F_wr", "clk_a"), ("F_rd", "clk_b"), ("F_wr", "clk_b"), ("F_rd", "clk_a")]
    uf = clocks.alias_uf(pairs)
    root = uf.find("F_wr")

    collapses = clocks.alias_collapses(pairs, uf, {root})

    assert {s.formal_id for s in collapses[root]} == {"F_wr", "F_rd"}
    assert collapses[root][0].net_ids == ("clk_a", "clk_b")


def test_alias_collapses_stays_silent_on_corroborated_and_single_bindings() -> None:
    # Same leaf, same actual net twice: nothing was aggregated.
    same = [("F", "clk"), ("F", "clk")]
    uf = clocks.alias_uf(same)
    assert clocks.alias_collapses(same, uf, {uf.find("F")}) == {}

    # The leaf's formal F binds two distinct actuals (the two parents' clock
    # ports), but each parent port is itself bound singly to one grandparent
    # clock — unaggregated evidence that they are the same net after all.
    corroborated = [("F", "pa_clk"), ("F", "pb_clk"), ("pa_clk", "top_clk"), ("pb_clk", "top_clk")]
    uf = clocks.alias_uf(corroborated)
    assert clocks.alias_collapses(corroborated, uf, {uf.find("F")}) == {}


def test_alias_collapses_respects_the_domain_root_gate() -> None:
    pairs = [("F", "a"), ("F", "b")]
    uf = clocks.alias_uf(pairs)
    assert clocks.alias_collapses(pairs, uf, set()) == {}  # not a clock root
    assert clocks.alias_collapses(pairs, uf, {uf.find("F")})  # is one


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


def test_clock_summary_reports_where_each_domain_is_declared(fixtures_dir: Path) -> None:
    """Domains are keyed by alias-root but reported by *name*, and a design
    routinely has several unrelated nets called ``clk`` that were never aliased
    together. Without a declaring scope the payload shows several entries all
    headed ``"clk"`` with nothing to tell them apart — unusable to a reader and
    to an assistant."""
    from hdl_kgraph.graph.summary import clock_summary

    sv = SystemVerilogParser()
    names = ["two_clock_cdc.sv", "simple_counter.sv", "dataflow.sv"]
    graph = build_graph([sv.parse(Path(n), (fixtures_dir / n).read_text()) for n in names])
    domains = clock_summary(graph)["domains"]
    assert domains

    for domain in domains:
        assert set(domain) >= {"clock", "aliases", "qualified_name", "file", "line"}
        # The scope must actually locate the net, not just be present.
        assert domain["file"] in names
        assert isinstance(domain["line"], int)
        assert domain["qualified_name"]

    # The property that matters: no two domains are indistinguishable.
    identities = [(d["qualified_name"], d["file"]) for d in domains]
    assert len(set(identities)) == len(identities), identities


def test_clock_summary_scope_survives_a_missing_node(fixtures_dir: Path) -> None:
    """A domain whose alias-root is absent from the graph must degrade to nulls
    rather than raise — the summary is precomputed at build time and must never
    be the thing that fails a build."""
    from hdl_kgraph.graph.summary import _domain_scope

    sv = SystemVerilogParser()
    graph = build_graph(
        [sv.parse(Path("two_clock_cdc.sv"), (fixtures_dir / "two_clock_cdc.sv").read_text())]
    )
    assert _domain_scope(graph, "no-such-node") == {
        "qualified_name": None,
        "file": None,
        "line": None,
    }
