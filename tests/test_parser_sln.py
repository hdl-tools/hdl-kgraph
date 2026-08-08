"""SLN (Cadence Perspec, ``e``-dialect) parser + linking tests (M10 final wedge).

Covers pass-1 extraction (ACTION nodes, the ``>``-invocation list, the
commented-out invocation being ignored, the ``extend`` unit, constraints) and
pass-2 resolution: a same-file ``>``-invocation becomes an INVOKES edge, an
invocation matching a design module becomes TEST_COVERS, testbench sequences
that match nothing are skipped, and a Visual Studio ``.sln`` is content-sniffed
out of discovery.
"""

from pathlib import Path

from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.sln import SlnParser
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore


def parse_sln(fixtures_dir: Path, name: str) -> FileIR:
    return SlnParser().parse(Path(name), (fixtures_dir / "sln" / name).read_text())


def actions(ir: FileIR) -> dict[str, object]:
    return {n.name: n for n in ir.nodes if n.kind is NodeKind.ACTION}


# --------------------------------------------------------------------------- #
# Pass 1: extraction
# --------------------------------------------------------------------------- #
def test_sln_extracts_actions_and_invocations(fixtures_dir) -> None:
    ir = parse_sln(fixtures_dir, "scenario.sln")
    assert ir.parse_error_count == 0
    acts = actions(ir)
    assert set(acts) == {"low_power_scen", "prep_phase"}
    assert acts["low_power_scen"].language is Language.SLN
    invokes = acts["low_power_scen"].attrs["invokes"]
    # The same-file action, the design module, and a tb sequence are all recorded;
    # the commented-out >tb_call_pmic_voltage_change_seq is NOT.
    assert "prep_phase" in invokes
    assert "top" in invokes
    assert "tb_call_wait_power_ack_seq" in invokes
    assert "tb_call_pmic_voltage_change_seq" not in invokes


def test_sln_records_extend_unit_and_constraints(fixtures_dir) -> None:
    ir = parse_sln(fixtures_dir, "scenario.sln")
    file_node = next(n for n in ir.nodes if n.kind is NodeKind.FILE)
    assert file_node.attrs["units"] == ["DVE"]
    constraints = actions(ir)["low_power_scen"].attrs.get("constraints", [])
    assert ".status == 0" in constraints


def test_sln_parser_tolerates_garbage() -> None:
    ir = SlnParser().parse(Path("junk.sln"), "<'\nextend {{{ action >>> }} ;;\n'>")
    assert ir.parse_error_count == 0  # malformed input is tolerated, never fatal


def test_sln_relational_gt_is_not_an_invocation() -> None:
    """A bare ``>`` inside a constraint is the relational operator, not a "do".

    Only ``>`` at statement position (body start, or after ``;``/``}``) invokes;
    ``.field > limit`` must not record ``limit`` as an invocation or emit a ref.
    """
    src = "<'\nextend DVE {\n  action a {\n    >real_call { .credits > spare; };\n  };\n};\n'>"
    ir = SlnParser().parse(Path("rel.sln"), src)
    acts = actions(ir)
    assert acts["a"].attrs["invokes"] == ["real_call"]  # the real do, and only it
    assert {r.target_name for r in ir.unresolved_refs} == {"real_call"}  # no `spare` ref


# --------------------------------------------------------------------------- #
# Pass 2: INVOKES + TEST_COVERS resolution (the ROADMAP acceptance), end-to-end.
# --------------------------------------------------------------------------- #
def _sln_build(tmp_path: Path, fixtures_dir: Path) -> Path:
    for name in ("top.v", "simple_counter.sv"):
        (tmp_path / name).write_text((fixtures_dir / name).read_text())
    (tmp_path / "scenario.sln").write_text((fixtures_dir / "sln" / "scenario.sln").read_text())
    run_build(tmp_path)
    return default_db_path(tmp_path)


def test_sln_links_invokes_and_test_covers(tmp_path: Path, fixtures_dir: Path) -> None:
    graph, _f, _m = SqliteStore(_sln_build(tmp_path, fixtures_dir)).load()
    edges = {
        (d["kind"], graph.nodes[u]["name"], graph.nodes[v]["name"])
        for u, v, d in graph.edges(data=True)
        if d["kind"] in (EdgeKind.INVOKES, EdgeKind.TEST_COVERS)
    }
    # Same-file action composition, and the scenario's coverage of the DUT module.
    assert (EdgeKind.INVOKES, "low_power_scen", "prep_phase") in edges
    assert (EdgeKind.TEST_COVERS, "low_power_scen", "top") in edges
    # The TEST_COVERS target is the real module node (kind MODULE), not a stub.
    cover_dst = next(v for u, v, d in graph.edges(data=True) if d["kind"] is EdgeKind.TEST_COVERS)
    assert graph.nodes[cover_dst]["kind"] is NodeKind.MODULE


def test_sln_unmatched_invocations_are_not_edged(tmp_path: Path, fixtures_dir: Path) -> None:
    """tb_* sequences that match no design object or same-file action produce no edge/stub."""
    graph, _f, _m = SqliteStore(_sln_build(tmp_path, fixtures_dir)).load()
    names = {graph.nodes[n]["name"] for n in graph}
    assert "tb_call_wait_power_ack_seq" not in names  # recorded in attrs only, never a node


def test_visual_studio_solution_is_skipped(tmp_path: Path, fixtures_dir: Path) -> None:
    from hdl_kgraph.discovery import check_file

    vs = fixtures_dir / "sln" / "vs_solution.sln"
    found = check_file(vs, vs.parent)
    assert found.language is Language.SLN  # routed by suffix...
    assert found.skipped_reason == "visual_studio_solution"  # ...but sniffed out by header
