"""M8 cocotb boundary tests: Python testbench → DUT linkage.

Fixtures in ``tests/fixtures/cocotb/``: ``counter.sv`` is the DUT,
``test_counter.py`` the cocotb testbench.
"""

from pathlib import Path

import pytest

from hdl_kgraph.discovery import check_file
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.python import PythonParser, _dut_from_filename
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind


@pytest.fixture(scope="module")
def cocotb_dir(fixtures_dir: Path) -> Path:
    return fixtures_dir / "cocotb"


def _graph(cocotb_dir: Path, tops=()):
    irs = [
        SystemVerilogParser().parse(Path("counter.sv"), (cocotb_dir / "counter.sv").read_text()),
        PythonParser().parse(
            Path("test_counter.py"), (cocotb_dir / "test_counter.py").read_text(), tops=tops
        ),
    ]
    return build_graph(irs)


@pytest.fixture(scope="module")
def graph(cocotb_dir: Path):
    return _graph(cocotb_dir)


def _edges(graph, kind, src_suffix):
    return {
        v.split("::")[-1]: d
        for u, v, d in graph.edges(data=True)
        if d["kind"] is kind and u.endswith(src_suffix)
    }


# -- pass-1 extraction ------------------------------------------------------------


def test_filename_heuristic() -> None:
    assert _dut_from_filename("test_counter") == "counter"
    assert _dut_from_filename("counter_tb") == "counter"
    assert _dut_from_filename("fifo") == "fifo"


def test_cocotb_tests_become_python_function_nodes(graph) -> None:
    tests = {
        graph.nodes[n]["name"]
        for n in graph.nodes
        if graph.nodes[n]["kind"] is NodeKind.FUNCTION
        and graph.nodes[n]["language"] is Language.PYTHON
    }
    assert {"test_count_up", "test_skipped"} <= tests
    node = graph.nodes["test_counter.py::function:test_count_up"]
    assert node["attrs"]["is_cocotb_test"] is True


# -- TEST_COVERS / READS / DRIVES -------------------------------------------------


def test_test_covers_targets_the_dut(graph) -> None:
    covers = _edges(graph, EdgeKind.TEST_COVERS, "function:test_count_up")
    assert "module:counter" in covers
    assert covers["module:counter"]["confidence"] == 0.4


def test_drives_from_value_assignment_and_setimmediatevalue(graph) -> None:
    drives = _edges(graph, EdgeKind.DRIVES, "function:test_count_up")
    # rst_n.value =, enable.value =, data.setimmediatevalue(...)
    assert {"port:counter.rst_n", "port:counter.enable", "port:counter.data"} <= set(drives)
    assert drives["port:counter.rst_n"]["confidence"] == 0.6


def test_reads_from_value_sampling_and_triggers(graph) -> None:
    reads = _edges(graph, EdgeKind.READS, "function:test_count_up")
    # dut.count.value, dut.overflow.value, RisingEdge(dut.clk)
    assert {"port:counter.count", "port:counter.overflow", "port:counter.clk"} <= set(reads)


def test_unknown_signal_is_not_stubbed(cocotb_dir: Path) -> None:
    # A dut.<signal> that is not a port/signal of the DUT must not invent a node.
    src = "import cocotb\n\n@cocotb.test()\nasync def test_x(dut):\n    dut.nonexistent.value = 1\n"
    irs = [
        SystemVerilogParser().parse(Path("counter.sv"), (cocotb_dir / "counter.sv").read_text()),
        PythonParser().parse(Path("test_counter.py"), src),
    ]
    g = build_graph(irs)
    assert not any("nonexistent" in n for n in g.nodes)


def test_configured_top_overrides_filename(cocotb_dir: Path) -> None:
    # Even with a non-matching filename, a configured top resolves the DUT.
    src = (cocotb_dir / "test_counter.py").read_text()
    irs = [
        SystemVerilogParser().parse(Path("counter.sv"), (cocotb_dir / "counter.sv").read_text()),
        PythonParser().parse(Path("tb_misnamed.py"), src, tops=["counter"]),
    ]
    g = build_graph(irs)
    covers = _edges(g, EdgeKind.TEST_COVERS, "function:test_count_up")
    assert "module:counter" in covers


# -- discovery content-sniff ------------------------------------------------------


def test_non_cocotb_python_is_skipped(tmp_path: Path) -> None:
    plain = tmp_path / "helper.py"
    plain.write_text("def add(a, b):\n    return a + b\n")
    assert check_file(plain, tmp_path).skipped_reason == "not_cocotb"


def test_cocotb_python_is_discovered(cocotb_dir: Path) -> None:
    found = check_file(cocotb_dir / "test_counter.py", cocotb_dir)
    assert found.skipped_reason is None
    assert found.language is Language.PYTHON
