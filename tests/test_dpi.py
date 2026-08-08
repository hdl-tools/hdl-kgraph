"""M8 DPI-C boundary tests: SV import/export "DPI-C" ↔ C/C++ functions.

The fixtures live in ``tests/fixtures/dpi/``: ``dpi_top.sv`` declares the
imports/export, ``dpi_impl.c``/``dpi_impl.cpp`` define the foreign functions.
"""

from pathlib import Path

import pytest

from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.c import CParser, CppParser
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind


@pytest.fixture(scope="module")
def dpi_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "dpi"


@pytest.fixture(scope="module")
def graph(dpi_dir: Path):
    irs = [
        SystemVerilogParser().parse(Path("dpi_top.sv"), (dpi_dir / "dpi_top.sv").read_text()),
        CParser().parse(Path("dpi_impl.c"), (dpi_dir / "dpi_impl.c").read_text()),
        CppParser().parse(Path("dpi_impl.cpp"), (dpi_dir / "dpi_impl.cpp").read_text()),
    ]
    return build_graph(irs)


def _foreign_binds(graph) -> dict[str, tuple[str, dict]]:
    """src node id -> (dst id, edge data) for every FOREIGN_BINDS edge."""
    return {u: (v, d) for u, v, d in graph.edges(data=True) if d["kind"] is EdgeKind.FOREIGN_BINDS}


# -- C / C++ pass-1 extraction ----------------------------------------------------


def test_c_parser_extracts_definitions_and_prototypes(dpi_dir: Path) -> None:
    ir = CParser().parse(Path("dpi_impl.c"), (dpi_dir / "dpi_impl.c").read_text())
    assert ir.parse_error_count == 0
    funcs = {n.name: n for n in ir.nodes if n.kind is NodeKind.FUNCTION}
    assert {"my_add", "c_mult", "my_task", "helper_proto"} <= set(funcs)
    assert funcs["my_add"].attrs.get("is_definition") is True
    assert funcs["my_add"].language is Language.C
    # A header-style prototype is a FUNCTION too, marked as such.
    assert funcs["helper_proto"].attrs.get("is_prototype") is True


def test_cpp_parser_handles_extern_c_and_namespaces(dpi_dir: Path) -> None:
    ir = CppParser().parse(Path("dpi_impl.cpp"), (dpi_dir / "dpi_impl.cpp").read_text())
    assert ir.parse_error_count == 0
    funcs = {n.name: n for n in ir.nodes if n.kind is NodeKind.FUNCTION}
    assert funcs["cpp_fn"].language is Language.CPP
    # A namespaced definition is recorded under its bare name.
    assert "internal" in funcs


# -- SV DPI extraction ------------------------------------------------------------


def test_import_prototype_node_carries_dpi_attrs(graph) -> None:
    node = graph.nodes["dpi_top.sv::function:dpi_top.my_add"]
    assert node["kind"] is NodeKind.FUNCTION
    assert node["attrs"]["dpi_import"] is True
    assert node["attrs"]["linkage"] == "DPI-C"


def test_imported_task_is_a_task_node(graph) -> None:
    assert graph.nodes["dpi_top.sv::task:dpi_top.my_task"]["kind"] is NodeKind.TASK


# -- FOREIGN_BINDS resolution -----------------------------------------------------


def test_plain_import_binds_to_c_definition(graph) -> None:
    dst, data = _foreign_binds(graph)["dpi_top.sv::function:dpi_top.my_add"]
    assert dst == "dpi_impl.c::function:my_add"
    assert data["confidence"] == 0.8  # cross-file unique match


def test_aliased_import_binds_by_linkage_name(graph) -> None:
    # SV-visible name `sv_mult`, C linkage name `c_mult`.
    dst, _ = _foreign_binds(graph)["dpi_top.sv::function:dpi_top.sv_mult"]
    assert dst == "dpi_impl.c::function:c_mult"


def test_import_binds_into_cpp_extern_c(graph) -> None:
    dst, _ = _foreign_binds(graph)["dpi_top.sv::function:dpi_top.cpp_fn"]
    assert dst == "dpi_impl.cpp::function:cpp_fn"
    assert graph.nodes[dst]["language"] is Language.CPP


def test_export_binds_to_local_sv_subprogram(graph) -> None:
    dst, data = _foreign_binds(graph)["dpi_top.sv::module:dpi_top"]
    assert dst == "dpi_top.sv::function:dpi_top.sv_export"
    assert data["confidence"] == 1.0  # same-file resolution
    assert graph.nodes[dst]["language"] is Language.SYSTEMVERILOG


def test_unresolved_import_degrades_to_stub(graph) -> None:
    dst, data = _foreign_binds(graph)["dpi_top.sv::function:dpi_top.missing_c"]
    assert graph.nodes[dst]["attrs"].get("unresolved") is True
    # The reference itself is syntactically certain; the stub carries the doubt.
    assert data["confidence"] == 1.0


def test_one_connected_graph_across_three_languages(graph) -> None:
    langs = {
        graph.nodes[n]["language"]
        for n in graph.nodes
        if graph.nodes[n]["kind"] is NodeKind.FUNCTION
    }
    assert {Language.SYSTEMVERILOG, Language.C, Language.CPP} <= langs
