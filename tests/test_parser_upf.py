"""UPF parser + linking + power-domain report tests (M10 second wedge).

Covers pass-1 extraction (POWER_DOMAIN nodes, strategy attrs, CONSTRAINS refs),
the pass-2 element resolution (reusing the SDC ``cells`` query), and the
power-domain report (domains, resolved element instances, isolation strategies).
"""

from pathlib import Path

from hdl_kgraph.graph import power
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.graph.summary import power_summary
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.tcl import UpfParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind


def parse_upf(fixtures_dir: Path, name: str) -> FileIR:
    return UpfParser().parse(Path("tests/fixtures") / name, (fixtures_dir / name).read_text())


def parse_sv(fixtures_dir: Path, name: str) -> FileIR:
    return SystemVerilogParser().parse(
        Path("tests/fixtures") / name, (fixtures_dir / name).read_text()
    )


def nodes_of(ir: FileIR, kind: NodeKind) -> dict[str, object]:
    return {n.name: n for n in ir.nodes if n.kind is kind}


# --------------------------------------------------------------------------- #
# Pass 1: extraction
# --------------------------------------------------------------------------- #
def test_upf_extracts_power_domains(fixtures_dir) -> None:
    ir = parse_upf(fixtures_dir, "upf/power.upf")
    assert ir.parse_error_count == 0
    domains = nodes_of(ir, NodeKind.POWER_DOMAIN)
    assert set(domains) == {"PD_TOP", "PD_COUNTER"}
    assert domains["PD_COUNTER"].language is Language.TCL
    assert domains["PD_COUNTER"].attrs["elements"] == ["u_counter"]
    assert domains["PD_TOP"].attrs["elements"] == ["."]


def test_upf_attaches_strategies_to_domain(fixtures_dir) -> None:
    ir = parse_upf(fixtures_dir, "upf/power.upf")
    pd = nodes_of(ir, NodeKind.POWER_DOMAIN)["PD_COUNTER"]
    assert pd.attrs["supply"] == "{primary ss_main}"  # -supply folded in
    kinds = {s["kind"] for s in pd.attrs["strategies"]}
    assert kinds == {"isolation", "retention"}
    iso = next(s for s in pd.attrs["strategies"] if s["kind"] == "isolation")
    assert iso["applies_to"] == "outputs"
    assert iso["isolation_signal"] == "iso_en"
    assert iso["clamp_value"] == "0"


def test_upf_folds_level_shifter_strategy() -> None:
    """``set_level_shifter`` (not in the shared fixture) folds onto its -domain."""
    upf = (
        "create_power_domain PD_X -elements {u_x}\n"
        "set_level_shifter ls_x -domain PD_X -applies_to outputs -location parent\n"
    )
    ir = UpfParser().parse(Path("ls.upf"), upf)
    pd = nodes_of(ir, NodeKind.POWER_DOMAIN)["PD_X"]
    (ls,) = pd.attrs["strategies"]
    assert ls["kind"] == "level_shifter"
    assert ls["name"] == "ls_x"
    assert ls["applies_to"] == "outputs"
    assert ls["location"] == "parent"


def test_upf_emits_element_constrains_refs(fixtures_dir) -> None:
    ir = parse_upf(fixtures_dir, "upf/power.upf")
    refs = [r for r in ir.unresolved_refs if r.edge_kind is EdgeKind.CONSTRAINS]
    # The ``.`` element (the design root) is not a child, so it emits no ref.
    assert {(r.attrs["query"], r.target_name) for r in refs} == {("cells", "u_counter")}


def test_upf_parser_tolerates_garbage() -> None:
    garbage = "create_power_domain\n}}}  -elements {\n garbage {{{\n"
    ir = UpfParser().parse(Path("junk.upf"), garbage)
    assert ir.parse_error_count == 0  # malformed input is tolerated, never fatal


# --------------------------------------------------------------------------- #
# Pass 2: element resolution + power-domain report (the ROADMAP acceptance)
# --------------------------------------------------------------------------- #
def test_power_domain_elements_resolve_to_instances(fixtures_dir) -> None:
    graph = build_graph(
        [
            parse_sv(fixtures_dir, "top.v"),
            parse_sv(fixtures_dir, "simple_counter.sv"),
            parse_upf(fixtures_dir, "upf/power.upf"),
        ]
    )
    constrains = [
        (graph.nodes[u]["name"], graph.nodes[v]["kind"], d["confidence"])
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.CONSTRAINS
    ]
    # u_counter is a unique top-level instance -> exact match at 1.0.
    assert ("PD_COUNTER", NodeKind.INSTANCE, 1.0) in constrains


def test_power_domains_report_lists_isolated_instances(fixtures_dir) -> None:
    graph = build_graph(
        [
            parse_sv(fixtures_dir, "top.v"),
            parse_sv(fixtures_dir, "simple_counter.sv"),
            parse_upf(fixtures_dir, "upf/power.upf"),
        ]
    )
    domains = {d.name: d for d in power.power_domains(graph)}
    assert set(domains) == {"PD_TOP", "PD_COUNTER"}
    counter = domains["PD_COUNTER"]
    assert counter.elements == ["top.u_counter"]
    assert counter.isolated is True
    # PD_TOP's only element is ``.`` (the design root), which stays unresolved.
    assert domains["PD_TOP"].elements == []
    assert domains["PD_TOP"].unresolved_elements == ["."]
    assert domains["PD_TOP"].isolated is False


def test_glob_element_is_not_misreported_unresolved(fixtures_dir) -> None:
    """A glob ``-elements`` token that resolves must not show up as unresolved.

    Regression guard: basename-only matching left ``u_*`` ``unresolved`` even
    though it resolved to ``top.u_counter``.
    """
    upf = UpfParser().parse(Path("g.upf"), "create_power_domain PD_G -elements {u_*}\n")
    graph = build_graph(
        [parse_sv(fixtures_dir, "top.v"), parse_sv(fixtures_dir, "simple_counter.sv"), upf]
    )
    (domain,) = power.power_domains(graph)
    assert domain.elements == ["top.u_counter"]
    assert domain.unresolved_elements == []


def test_power_summary_counts(fixtures_dir) -> None:
    graph = build_graph(
        [
            parse_sv(fixtures_dir, "top.v"),
            parse_sv(fixtures_dir, "simple_counter.sv"),
            parse_upf(fixtures_dir, "upf/power.upf"),
        ]
    )
    payload = power_summary(graph)
    assert payload["domain_count"] == 2
    assert payload["isolated_count"] == 1
