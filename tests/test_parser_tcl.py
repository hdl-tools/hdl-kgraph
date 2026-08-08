"""Tcl flow-script parser + linking tests (M10 third wedge).

Covers pass-1 extraction (REFERENCES_FILE refs with `mode`, variable
substitution, path normalization) and the pass-2 file resolution: a referenced
file in the build binds to its real FILE node, one outside the build to an
unresolved stub (never shadowing a real node, never a dangling-endpoint warning).
"""

from pathlib import Path

from hdl_kgraph.ids import file_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.tcl import TclScriptParser
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore


def refs_of(ir: FileIR) -> dict[str, str]:
    """{target relpath: mode} for the REFERENCES_FILE refs in *ir*."""
    return {
        r.target_name: r.attrs["mode"]
        for r in ir.unresolved_refs
        if r.edge_kind is EdgeKind.REFERENCES_FILE
    }


# --------------------------------------------------------------------------- #
# Pass 1: extraction
# --------------------------------------------------------------------------- #
def test_tcl_extracts_file_references(fixtures_dir) -> None:
    # The fixture's `set RTL_DIR .` is designed for the script at the build root.
    ir = TclScriptParser().parse(Path("flow.tcl"), (fixtures_dir / "tcl/flow.tcl").read_text())
    assert ir.parse_error_count == 0
    flavor = next(n.attrs.get("flavor") for n in ir.nodes if n.kind is NodeKind.FILE)
    assert flavor == "flow"
    refs = refs_of(ir)
    # `set RTL_DIR .` substitutes, and `./x` normalizes to the build-root keyspace.
    assert refs == {
        "simple_counter.sv": "read",
        "top.v": "read",
        "constraints.sdc": "read",
        "helper.tcl": "source",
    }


def test_tcl_normalizes_paths_relative_to_script_dir() -> None:
    """A script in a subdir resolves `../` references into the build-root keyspace."""
    text = "set SRC ../rtl\nread_verilog $SRC/cpu.sv\nsource ./sub/setup.tcl\n"
    ir = TclScriptParser().parse(Path("scripts/build.tcl"), text)
    assert refs_of(ir) == {"rtl/cpu.sv": "read", "scripts/sub/setup.tcl": "source"}


def test_tcl_skips_flag_values_keeps_paths() -> None:
    """`-format verilog` is a flag value, not a path; the trailing file is."""
    ir = TclScriptParser().parse(Path("f.tcl"), "analyze -format verilog top.sv\n")
    assert refs_of(ir) == {"top.sv": "analyze"}


def test_tcl_parser_tolerates_garbage() -> None:
    ir = TclScriptParser().parse(Path("junk.tcl"), "read_verilog\n}}} $undef [\nnonsense {{{\n")
    assert ir.parse_error_count == 0  # malformed input is tolerated, never fatal


# --------------------------------------------------------------------------- #
# Pass 2: file resolution (resolved real node vs unresolved stub), end-to-end.
# FILE nodes for SV come from the preprocessor, so this exercises a real build.
# --------------------------------------------------------------------------- #
def _flow_build(tmp_path: Path, fixtures_dir: Path) -> Path:
    """A build root with a flow script alongside two of the three files it reads."""
    for name in ("top.v", "simple_counter.sv"):
        (tmp_path / name).write_text((fixtures_dir / name).read_text())
    # references simple_counter.sv + top.v (in build) and helper.tcl (missing)
    (tmp_path / "flow.tcl").write_text(
        "read_verilog simple_counter.sv\nread_verilog top.v\nsource helper.tcl\n"
    )
    run_build(tmp_path)
    return default_db_path(tmp_path)


def test_references_resolve_to_real_file_nodes(tmp_path: Path, fixtures_dir: Path) -> None:
    db = _flow_build(tmp_path, fixtures_dir)
    graph, _f, _m = SqliteStore(db).load()
    edges = {}
    for _u, v, d in graph.edges(data=True):
        if d["kind"] is EdgeKind.REFERENCES_FILE:
            node = graph.nodes[v]
            edges[node["name"]] = (node["attrs"].get("unresolved", False), d["attrs"]["mode"])
    # In-build sources bind to their real FILE node; the missing helper stubs.
    assert edges["simple_counter.sv"] == (False, "read")
    assert edges["top.v"] == (False, "read")
    assert edges["helper.tcl"] == (True, "source")
    # The real FILE node keeps its language (the flow stub never shadowed it).
    assert graph.nodes[file_node_id("simple_counter.sv")]["language"] is Language.SYSTEMVERILOG


def test_missing_reference_is_unresolved_stub(tmp_path: Path, fixtures_dir: Path) -> None:
    """A referenced file outside the build becomes an unresolved FILE stub."""
    db = _flow_build(tmp_path, fixtures_dir)
    graph, _f, _m = SqliteStore(db).load()
    stub_id = "unresolved:file:helper.tcl"
    assert stub_id in graph
    assert graph.nodes[stub_id]["attrs"]["unresolved"] is True
    assert graph.nodes[stub_id]["kind"] is NodeKind.FILE


def test_absolute_in_tree_reference_canonicalizes_to_real_node(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """An *absolute* file-ref inside the build root binds to the real FILE node (#164).

    The linker relativizes the in-tree absolute path onto the build-root relpath
    keyspace; an out-of-tree absolute stays verbatim and still stubs.
    """
    (tmp_path / "simple_counter.sv").write_text((fixtures_dir / "simple_counter.sv").read_text())
    in_tree_abs = (tmp_path / "simple_counter.sv").as_posix()
    out_of_tree_abs = "/nonexistent_ext_root/lib.v"
    (tmp_path / "flow.tcl").write_text(
        f"read_verilog {in_tree_abs}\nread_verilog {out_of_tree_abs}\n"
    )
    run_build(tmp_path)
    graph, _f, _m = SqliteStore(default_db_path(tmp_path)).load()

    resolved = {
        graph.nodes[v]["name"]: graph.nodes[v]["attrs"].get("unresolved", False)
        for _u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.REFERENCES_FILE
    }
    # The in-tree absolute now resolves to the real (relpath-keyed) FILE node...
    assert resolved["simple_counter.sv"] is False
    assert graph.nodes[file_node_id("simple_counter.sv")]["language"] is Language.SYSTEMVERILOG
    # ...while the out-of-tree absolute is kept verbatim -> stub (prior behavior).
    assert "unresolved:file:/nonexistent_ext_root/lib.v" in graph
