"""End-to-end M2 builds: filelists, defines, includes, config, both-branches."""

import shutil
from pathlib import Path

import networkx as nx
import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.schema import EdgeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore


@pytest.fixture()
def project(tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp copy of the vendor-style preproc fixture tree."""
    root = tmp_path / "proj"
    shutil.copytree(fixtures_dir / "preproc", root)
    monkeypatch.setenv("PREPROC_ROOT", str(root))
    return root


def build(project: Path, *args: str):
    return CliRunner().invoke(main, ["build", str(project), *args])


def load(project: Path):
    return SqliteStore(project / ".hdl-kgraph" / "graph.db").load()


def edges(g: nx.MultiDiGraph, src: str, dst: str, kind: EdgeKind) -> list[dict]:
    if not g.has_edge(src, dst):
        return []
    return [d for d in g[src][dst].values() if d["kind"] is kind]


def test_build_from_vendor_filelist(project: Path) -> None:
    result = build(project, "-f", str(project / "tb.f"))
    assert result.exit_code == 0, result.output
    assert "filelists:      2" in result.output  # tb.f + nested common.f
    assert "skipped (included): 1" in result.output  # defines.svh spliced into top.sv
    assert "macros defined:" in result.output
    assert "includes:       1 resolved" in result.output
    assert "both-branches" not in result.output  # +define+USE_FIFO selects

    graph, files, _ = load(project)

    # The macro-instantiated fifo resolves cross-file at 0.8 and spans the
    # `MAKE_FIFO invocation line.
    inst = "rtl/top.sv::instance:top.u_fifo"
    (edge,) = edges(graph, inst, "rtl/fifo.sv::module:fifo", EdgeKind.INSTANTIATES)
    assert edge["confidence"] == 0.8
    assert graph.nodes[inst]["line_span"] == (8, 8)
    assert "rtl/top.sv::instance:top.u_stack" not in graph  # `else side dropped

    # The default argument `DEPTH picked up the filelist's +define+DEPTH=4.
    (override,) = [
        d for _, _, d in graph.out_edges(inst, data=True) if d["kind"] is EdgeKind.PARAMETERIZES
    ]
    assert override["attrs"]["value_text"] == "4"

    # Preprocessor edges: include splice and macro use, attributed to files.
    assert edges(graph, "file:rtl/top.sv", "file:include/defines.svh", EdgeKind.INCLUDES)
    assert edges(
        graph, "file:rtl/top.sv", "include/defines.svh::macro:MAKE_FIFO", EdgeKind.USES_MACRO
    )

    # FILELIST nodes preserve compile order; nested list at its -f position.
    tb = graph.nodes["filelist:tb.f"]
    assert tb["attrs"]["defines"] == {"USE_FIFO": None, "DEPTH": "4"}
    (nested,) = edges(graph, "filelist:tb.f", "filelist:common.f", EdgeKind.INCLUDES)
    refs = {
        v: d["attrs"]
        for _, v, d in graph.out_edges("filelist:tb.f", data=True)
        if d["kind"] is EdgeKind.REFERENCES_FILE
    }
    assert refs["file:rtl/top.sv"]["order"] > nested["attrs"]["order"]
    assert refs["file:prims.v"]["role"] == "library"

    # status separates HDL sources from the recorded filelists.
    status = CliRunner().invoke(main, ["status", "--db", str(project / ".hdl-kgraph" / "graph.db")])
    assert status.exit_code == 0, status.output
    assert "4 parsed" in status.output
    assert "2 filelist(s)" in status.output

    # files table: the spliced header is recorded as included; the filelists
    # themselves are content-hashed for M4 incremental rebuilds.
    by_path = {f.path: f for f in files}
    assert by_path["include/defines.svh"].skipped_reason == "included"
    assert by_path["tb.f"].content_hash
    assert by_path["common.f"].content_hash
    assert by_path["rtl/top.sv"].skipped_reason is None


def test_both_branches_directory_build(project: Path) -> None:
    result = build(project, "-I", str(project / "include"))
    assert result.exit_code == 0, result.output
    assert "both-branches mode" in result.output

    graph, _, _ = load(project)
    # Both sides of `ifdef USE_FIFO are present: the non-selected fifo side
    # at 0.6, the else side (what a define-less compile elaborates) at 0.8.
    fifo_edges = [
        d
        for _, _, d in graph.out_edges("rtl/top.sv::instance:top.u_fifo", data=True)
        if d["kind"] is EdgeKind.INSTANTIATES
    ]
    assert [d["confidence"] for d in fifo_edges] == [0.6]
    assert graph.nodes["rtl/top.sv::instance:top.u_fifo"]["attrs"]["conditional"] is True
    stack_edges = [
        d
        for _, _, d in graph.out_edges("rtl/top.sv::instance:top.u_stack", data=True)
        if d["kind"] is EdgeKind.INSTANTIATES
    ]
    assert [d["confidence"] for d in stack_edges] == [0.8]

    tree = CliRunner().invoke(
        main, ["tree", "top", "--db", str(project / ".hdl-kgraph" / "graph.db")]
    )
    assert tree.exit_code == 0, tree.output
    assert "u_fifo: fifo [~0.6]" in tree.output
    assert "u_stack: stack" in tree.output


def test_define_flag_selects_branch(project: Path) -> None:
    result = build(project, "-I", str(project / "include"), "-D", "USE_FIFO")
    assert result.exit_code == 0, result.output
    assert "both-branches" not in result.output
    graph, _, _ = load(project)
    assert "rtl/top.sv::instance:top.u_fifo" in graph
    assert "rtl/top.sv::instance:top.u_stack" not in graph


def test_config_file_supplies_filelist(project: Path) -> None:
    (project / "hdl-kgraph.toml").write_text('[build]\nfilelists = ["tb.f"]\n')
    result = build(project)
    assert result.exit_code == 0, result.output
    assert "filelists:      2" in result.output

    # --no-config ignores the config; plain directory discovery instead.
    result = build(project, "--no-config", "-I", str(project / "include"))
    assert result.exit_code == 0, result.output
    assert "filelists:" not in result.output


def test_unknown_filelist_options_warn_but_build(project: Path) -> None:
    result = build(project, "-f", str(project / "tb.f"))
    assert result.exit_code == 0
    assert "unknown option '-sv' skipped" in result.output  # stderr is mixed in


def _outside_root_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Build root with a filelist pointing at a source above it; returns (root, filelist)."""
    root = tmp_path / "proj"
    shared = tmp_path / "shared"
    (shared).mkdir(parents=True)
    (shared / "foo.sv").write_text("module foo; endmodule\n")
    root.mkdir(parents=True)
    fl = root / "tb.f"
    fl.write_text("../shared/foo.sv\n")
    return root, fl


def test_outside_root_source_dropped_by_default(tmp_path: Path) -> None:
    root, fl = _outside_root_tree(tmp_path)
    result = build(root, "-f", str(fl))
    # The only listed source escapes the build root, so nothing parses (#68).
    assert "escapes the build root" in result.output
    assert result.exit_code != 0
    assert "no parseable HDL files" in result.output


def test_allow_outside_root_parses_external_source(tmp_path: Path) -> None:
    root, fl = _outside_root_tree(tmp_path)
    result = build(root, "-f", str(fl), "--allow-outside-root")
    assert result.exit_code == 0, result.output
    assert "escapes the build root" not in result.output
    assert "files parsed:   1" in result.output

    graph, _, _ = load(root)
    # The out-of-root file is recorded under a `..`-relative path.
    assert "../shared/foo.sv::module:foo" in graph
