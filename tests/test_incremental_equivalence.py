"""Byte-identity gate for incremental linking (#64-C).

Every incremental ``update`` must produce a graph identical to a fresh
``build`` of the same sources. These tests are the correctness net the
SystemVerilog incremental linker (#64-B) is developed against: a parametrized
matrix of edit shapes plus a randomized fuzz of edit *sequences*. They pass
today (``update`` re-links fully) and become the real gate once the linker
reuses the prior graph.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from pathlib import Path

import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.pipeline import default_db_path, run_build, run_update
from hdl_kgraph.storage.sqlite_store import SqliteStore

# Every equivalence/fuzz case runs under BOTH link paths — the default in-memory
# `link_incremental` and the opt-in memory-bounded re-link (#119) — so the
# byte-identical gate covers the bounded path too. Set by the autouse fixture.
_BOUNDED_LINK = False


@pytest.fixture(params=[False, True], ids=["inmem", "bounded"], autouse=True)
def _link_mode(request: pytest.FixtureRequest):
    """Run each test once per incremental-link path."""
    global _BOUNDED_LINK
    _BOUNDED_LINK = request.param
    yield
    _BOUNDED_LINK = False


def _update(root: Path) -> None:
    run_update(root, options=BuildOptions(bounded_link=_BOUNDED_LINK))


def _signature(graph) -> tuple[list, list]:
    """A fully-ordered (nodes, edges) signature for byte-identity comparison."""
    nodes = sorted(
        (
            node_id,
            data["kind"].value,
            data["name"],
            data.get("qualified_name", ""),
            data.get("file", ""),
            tuple(data.get("line_span", (0, 0))),
            data["language"].value,
            json.dumps(data["attrs"], sort_keys=True),
        )
        for node_id, data in graph.nodes(data=True)
    )
    edges = sorted(
        (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
        for u, v, d in graph.edges(data=True)
    )
    return nodes, edges


def _graph(root: Path):
    graph, _, _ = SqliteStore(default_db_path(root)).load()
    return graph


def _assert_incremental_matches_full(root: Path, label: str = "") -> None:
    """``update`` then a fresh ``build`` of the same tree must be identical."""
    _update(root)
    inc_nodes, inc_edges = _signature(_graph(root))
    run_build(root)
    full_nodes, full_edges = _signature(_graph(root))
    assert inc_nodes == full_nodes, f"node mismatch after {label}"
    assert inc_edges == full_edges, f"edge mismatch after {label}"


# -- edit-shape matrix --------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """top -> mid -> leaf; leaf includes a header; mid imports a package."""
    (tmp_path / "defs.svh").write_text("`define WIDTH 8\n")
    (tmp_path / "leaf.sv").write_text(
        '`include "defs.svh"\n'
        "module leaf(input logic [`WIDTH-1:0] a, output logic [`WIDTH-1:0] y);\n"
        "  assign y = a;\n"
        "endmodule\n"
    )
    (tmp_path / "my_pkg.sv").write_text("package my_pkg;\n  localparam int K = 4;\nendpackage\n")
    (tmp_path / "mid.sv").write_text(
        "module mid(input logic [7:0] a, output logic [7:0] y);\n"
        "  import my_pkg::*;\n"
        "  leaf u_leaf(.a(a), .y(y));\n"
        "endmodule\n"
    )
    (tmp_path / "top.sv").write_text(
        "module top(input logic [7:0] a, output logic [7:0] y);\n"
        "  mid u_mid(.a(a), .y(y));\n"
        "endmodule\n"
    )
    assert run_build(tmp_path).parsed_files == 5
    return tmp_path


def _rename_instance(p: Path) -> None:
    f = p / "mid.sv"
    f.write_text(f.read_text().replace("u_leaf", "u_leaf2"))


def _touch_comment(p: Path) -> None:
    f = p / "top.sv"
    f.write_text(f.read_text() + "// touched\n")


def _add_sibling(p: Path) -> None:
    (p / "sib.sv").write_text(
        "module sib(input logic [7:0] a, output logic [7:0] y);\n"
        "  leaf u(.a(a), .y(y));\n"
        "endmodule\n"
    )


def _remove_leaf(p: Path) -> None:
    (p / "leaf.sv").unlink()


def _duplicate_leaf_name(p: Path) -> None:
    (p / "leaf_dup.sv").write_text(
        "module leaf(input logic [7:0] a, output logic [7:0] y);\n  assign y = a;\nendmodule\n"
    )


def _header_width(p: Path) -> None:
    (p / "defs.svh").write_text("`define WIDTH 16\n")


def _remove_package(p: Path) -> None:
    (p / "my_pkg.sv").unlink()


def _change_import(p: Path) -> None:
    (p / "new_pkg.sv").write_text("package new_pkg;\n  localparam int J = 9;\nendpackage\n")
    f = p / "mid.sv"
    f.write_text(f.read_text().replace("my_pkg", "new_pkg"))


_EDITS: list[tuple[str, Callable[[Path], None]]] = [
    ("rename_instance", _rename_instance),
    ("touch_comment", _touch_comment),
    ("add_sibling_referencing_leaf", _add_sibling),
    ("remove_leaf_to_stub", _remove_leaf),
    ("duplicate_leaf_name_to_ambiguity", _duplicate_leaf_name),
    ("edit_header_width", _header_width),
    ("remove_imported_package", _remove_package),
    ("retarget_import_to_new_package", _change_import),
]


@pytest.mark.parametrize("edit", [e for _, e in _EDITS], ids=[i for i, _ in _EDITS])
def test_incremental_matches_full_across_edit_shapes(
    project: Path, edit: Callable[[Path], None]
) -> None:
    edit(project)
    _assert_incremental_matches_full(project, edit.__name__)


# -- UVM / TEST_COVERS edit shapes (#119 bounded derive_test_covers) ----------
# The bounded re-link produces only a partial graph, so TEST_COVERS is re-derived
# whole-design out-of-core after the scoped write. These edits change the derived
# set; the byte-identical gate (run under both link paths) pins that the bounded
# path matches a full build — the `project` fixture above has no UVM, so without
# these the gap was invisible.


@pytest.fixture
def uvm_project(tmp_path: Path) -> Path:
    """A tb_* top instantiating a resolved DUT, plus a uvm_test class chain."""
    (tmp_path / "dut.sv").write_text(
        "module verif_dut(input logic clk, output logic gnt);\n  assign gnt = clk;\nendmodule\n"
    )
    (tmp_path / "dut2.sv").write_text(
        "module verif_dut2(input logic clk, output logic gnt);\n  assign gnt = ~clk;\nendmodule\n"
    )
    (tmp_path / "tb.sv").write_text(
        "module tb_verif_top;\n"
        "  logic clk, gnt;\n"
        "  verif_dut u_dut(.clk(clk), .gnt(gnt));\n"
        "endmodule\n"
        "class verif_base_test extends uvm_test;\n"
        "endclass\n"
    )
    run_build(tmp_path)
    return tmp_path


def _tb_add_dut_instance(p: Path) -> None:
    f = p / "tb.sv"
    old = f.read_text()
    new = old.replace(
        "  verif_dut u_dut(.clk(clk), .gnt(gnt));\n",
        "  verif_dut u_dut(.clk(clk), .gnt(gnt));\n  verif_dut2 u_dut2(.clk(clk), .gnt(gnt));\n",
    )
    assert new != old, "expected to add verif_dut2 instance in tb.sv"
    f.write_text(new)


def _tb_remove_dut_instance(p: Path) -> None:
    f = p / "tb.sv"
    old = f.read_text()
    new = old.replace("  verif_dut u_dut(.clk(clk), .gnt(gnt));\n", "")
    assert new != old, "expected to remove verif_dut instance from tb.sv"
    f.write_text(new)


def _add_uvm_test_class(p: Path) -> None:
    f = p / "tb.sv"
    f.write_text(f.read_text() + "class verif_smoke_test extends verif_base_test;\nendclass\n")


def _remove_dut_module(p: Path) -> None:
    (p / "dut.sv").unlink()


def _add_second_tb_top(p: Path) -> None:
    (p / "tb2.sv").write_text(
        "module tb_other;\n  logic clk, gnt;\n  verif_dut2 u(.clk(clk), .gnt(gnt));\nendmodule\n"
    )


_UVM_EDITS: list[tuple[str, Callable[[Path], None]]] = [
    ("tb_add_dut_instance", _tb_add_dut_instance),
    ("tb_remove_dut_instance", _tb_remove_dut_instance),
    ("add_uvm_test_class", _add_uvm_test_class),
    ("remove_dut_module_to_stub", _remove_dut_module),
    ("add_second_tb_top", _add_second_tb_top),
]


@pytest.mark.parametrize("edit", [e for _, e in _UVM_EDITS], ids=[i for i, _ in _UVM_EDITS])
def test_incremental_matches_full_uvm_test_covers(
    uvm_project: Path, edit: Callable[[Path], None]
) -> None:
    edit(uvm_project)
    _assert_incremental_matches_full(uvm_project, edit.__name__)


# -- randomized edit-sequence fuzz --------------------------------------------

_MAX_FILES = 7


def _materialize(root: Path, model: list[tuple[str, str, list[str]]]) -> None:
    """Write *model* (filename, module name, instance targets) and drop stale files."""
    wanted = {fn for fn, _, _ in model}
    for existing in root.glob("*.sv"):
        if existing.name not in wanted:
            existing.unlink()
    for fn, mod, targets in model:
        body = "".join(f"  {t} u_{i}();\n" for i, t in enumerate(targets))
        (root / fn).write_text(f"module {mod};\n{body}endmodule\n")


def _random_edit(rng: random.Random, model: list[tuple[str, str, list[str]]], counter: int) -> int:
    """Apply one random in-place edit to *model*; return the next id counter."""
    names = [mod for _, mod, _ in model]
    op = rng.choice(["add", "remove", "add_inst", "remove_inst", "rename", "duplicate"])
    if op == "add" and len(model) < _MAX_FILES:
        target = [rng.choice(names)] if names and rng.random() < 0.7 else []
        model.append((f"m{counter}.sv", f"m{counter}", target))
        return counter + 1
    if op == "remove" and len(model) > 1:
        del model[rng.randrange(len(model))]
    elif op == "add_inst" and names:
        i = rng.randrange(len(model))
        fn, mod, targets = model[i]
        model[i] = (fn, mod, [*targets, rng.choice(names)])
    elif op == "remove_inst":
        candidates = [i for i, (_, _, t) in enumerate(model) if t]
        if candidates:
            i = rng.choice(candidates)
            fn, mod, targets = model[i]
            targets.pop(rng.randrange(len(targets)))
            model[i] = (fn, mod, targets)
    elif op == "rename":
        i = rng.randrange(len(model))
        fn, _, targets = model[i]
        model[i] = (fn, f"m{counter}", targets)
        return counter + 1
    elif op == "duplicate" and names and len(model) < _MAX_FILES:
        model.append((f"dup{counter}.sv", rng.choice(names), []))
        return counter + 1
    return counter


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_incremental_equals_full_fuzz(tmp_path: Path, seed: int) -> None:
    """Random edit *sequences*: a chain of incremental updates stays identical
    to a fresh full build after every step. Seed-logged for reproducibility."""
    rng = random.Random(seed)
    inc = tmp_path / "inc"
    inc.mkdir()
    model: list[tuple[str, str, list[str]]] = [
        ("m0.sv", "m0", []),
        ("m1.sv", "m1", ["m0"]),
        ("m2.sv", "m2", ["m0", "m1"]),
    ]
    counter = 3
    _materialize(inc, model)
    run_build(inc)

    for step in range(8):
        counter = _random_edit(rng, model, counter)
        _materialize(inc, model)
        _update(inc)
        inc_sig = _signature(_graph(inc))

        ref = tmp_path / f"ref_{step}"
        ref.mkdir()
        _materialize(ref, model)
        run_build(ref)
        ref_sig = _signature(_graph(ref))

        assert inc_sig[0] == ref_sig[0], f"seed={seed} step={step}: node mismatch"
        assert inc_sig[1] == ref_sig[1], f"seed={seed} step={step}: edge mismatch"
