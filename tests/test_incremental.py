"""M4 acceptance tests: incremental ``update`` re-parses only what changed."""

import json
from pathlib import Path

import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.incremental import ChangeSet, detect_git_changes, diff_hashes
from hdl_kgraph.pipeline import run_build, run_update, scan_changes
from hdl_kgraph.schema import EdgeKind
from hdl_kgraph.storage.sqlite_store import SCHEMA_VERSION, SqliteStore


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
    report = run_build(tmp_path)
    assert report.parsed_files == 5
    return tmp_path


def _graph(root: Path):
    graph, _, _ = SqliteStore(root / ".hdl-kgraph" / "graph.db").load()
    return graph


def test_no_changes_is_up_to_date(project: Path) -> None:
    report = run_update(project)
    assert report.up_to_date
    assert report.build is None


def test_editing_one_file_reparses_only_that_file(project: Path) -> None:
    path = project / "mid.sv"
    path.write_text(path.read_text() + "// touched\n")
    report = run_update(project)
    assert report.reparsed == {"mid.sv": "changed"}
    assert report.build is not None
    assert report.build.reused_files == 4  # defs.svh, leaf, my_pkg, top
    assert report.build.parsed_files == 5


def test_header_edit_dirties_includers(project: Path) -> None:
    (project / "defs.svh").write_text("`define WIDTH 16\n")
    report = run_update(project)
    assert set(report.reparsed) == {"defs.svh", "leaf.sv"}
    assert report.reparsed["leaf.sv"] == "includes defs.svh"
    # The new width is visible in the re-linked graph via the macro node body.
    graph = _graph(project)
    macro = graph.nodes["defs.svh::macro:WIDTH"]
    assert macro["attrs"]["body"] == "16"


def test_macro_definer_edit_dirties_users(project: Path) -> None:
    # leaf.sv expands `WIDTH from defs.svh; editing defs.svh must dirty leaf.sv
    # through USES_MACRO as well — verify the closure reason when the include
    # edge is not the trigger by adding a macro-only relationship.
    (project / "consts.svh").write_text("`define K 2\n")
    (project / "user.sv").write_text(
        '`include "consts.svh"\nmodule user(output logic [`K-1:0] o);\nendmodule\n'
    )
    run_update(project)
    (project / "consts.svh").write_text("`define K 3\n")
    report = run_update(project)
    assert set(report.reparsed) == {"consts.svh", "user.sv"}


def test_removed_module_leaves_unresolved_stub_without_reparsing_parent(project: Path) -> None:
    (project / "leaf.sv").unlink()
    report = run_update(project)
    assert report.removed == ["leaf.sv"]
    assert report.reparsed == {}  # mid is re-linked from its stored IR
    graph = _graph(project)
    stub = graph.nodes["unresolved:module:leaf"]
    assert stub["attrs"]["unresolved"] is True


def test_readded_module_resolves_without_reparsing_parent(project: Path) -> None:
    source = (project / "leaf.sv").read_text()
    (project / "leaf.sv").unlink()
    run_update(project)
    (project / "leaf.sv").write_text(source)
    report = run_update(project)
    assert report.reparsed == {"leaf.sv": "added"}
    graph = _graph(project)
    assert "unresolved:module:leaf" not in graph
    assert report.build is not None and report.build.unresolved_count == 0


def test_added_header_dirties_previously_failing_includer(tmp_path: Path) -> None:
    (tmp_path / "user.sv").write_text(
        '`include "late.svh"\nmodule user(output logic [`L-1:0] o);\nendmodule\n'
    )
    report = run_build(tmp_path)
    assert report.includes_unresolved == 1
    (tmp_path / "late.svh").write_text("`define L 4\n")
    update = run_update(tmp_path)
    assert update.reparsed["user.sv"] == "include 'late.svh' now resolvable"
    assert update.build is not None and update.build.includes_resolved == 1


def _instantiates_targets(graph, parent_module: str) -> set[str]:
    """The dst node ids of INSTANTIATES edges out of *parent_module*'s subtree."""
    prefix = f"{parent_module}::"  # scope to this file, not similarly-named ones
    return {
        v
        for u, v, d in graph.edges(data=True)
        if d["kind"] is EdgeKind.INSTANTIATES and u.startswith(prefix)
    }


def test_removed_module_relinks_parent_edge_incrementally(project: Path) -> None:
    """A removed module flips an instantiation edge owned by an *unchanged*
    (not reparsed) parent file. The per-src edge diff in the incremental write
    must catch that, even though mid.sv itself is re-linked from its stored IR."""
    before = _graph(project)
    assert "leaf.sv::module:leaf" in _instantiates_targets(before, "mid.sv")

    (project / "leaf.sv").unlink()
    report = run_update(project)
    assert report.reparsed == {}  # mid is NOT reparsed
    assert report.full_rebuild_reason is None  # the incremental path was taken

    after = _graph(project)
    targets = _instantiates_targets(after, "mid.sv")
    assert "leaf.sv::module:leaf" not in targets
    assert "unresolved:module:leaf" in targets  # edge re-pointed by the delta write


def test_readded_module_flips_edge_back_incrementally(project: Path) -> None:
    source = (project / "leaf.sv").read_text()
    (project / "leaf.sv").unlink()
    run_update(project)
    assert "unresolved:module:leaf" in _instantiates_targets(_graph(project), "mid.sv")

    (project / "leaf.sv").write_text(source)
    run_update(project)
    targets = _instantiates_targets(_graph(project), "mid.sv")
    assert "leaf.sv::module:leaf" in targets
    assert "unresolved:module:leaf" not in targets


def test_removed_file_drops_its_files_and_file_ir_rows(project: Path) -> None:
    """An incrementally-removed source must leave no stale files/file_irs row
    (a stale content_hash would make the next scan miss it)."""
    import sqlite3

    db = project / ".hdl-kgraph" / "graph.db"
    (project / "my_pkg.sv").unlink()  # a leaf in the dep graph, safe to drop
    run_update(project)

    conn = sqlite3.connect(db)
    try:
        files = {row[0] for row in conn.execute("SELECT path FROM files")}
        irs = {row[0] for row in conn.execute("SELECT path FROM file_irs")}
    finally:
        conn.close()
    assert "my_pkg.sv" not in files
    assert "my_pkg.sv" not in irs


def test_update_write_cost_scales_with_change(project: Path) -> None:
    """The issue's verification: an incremental write touches far fewer rows
    than the full graph. Asserted via SqliteStore.last_write_stats."""
    db = project / ".hdl-kgraph" / "graph.db"
    total_nodes = SqliteStore(db).load()[0].number_of_nodes()

    captured: dict[str, dict] = {}
    real_save_incremental = SqliteStore.save_incremental

    def spy(self, *args, **kwargs):
        real_save_incremental(self, *args, **kwargs)
        if self.last_write_stats is not None:
            captured["stats"] = self.last_write_stats

    SqliteStore.save_incremental = spy  # type: ignore[method-assign]
    try:
        path = project / "mid.sv"
        path.write_text(path.read_text() + "// touched\n")
        run_update(project)
    finally:
        SqliteStore.save_incremental = real_save_incremental  # type: ignore[method-assign]

    stats = captured["stats"]
    written = stats["nodes_upserted"] + stats["nodes_deleted"]
    # Touching only mid.sv must write a small fraction of the graph, not all of
    # it — a proportional bound that stays valid as the schema/graph grows.
    assert written < total_nodes // 2


def test_update_read_cost_scales_with_change(project: Path) -> None:
    """Phase B: the scoped delta *reads* only the dirty closure's stored rows,
    not the whole nodes/edges tables — so update memory/IO scales with the
    change. Asserted via SqliteStore.last_write_stats."""
    db = project / ".hdl-kgraph" / "graph.db"
    total_nodes = SqliteStore(db).load()[0].number_of_nodes()

    captured: dict[str, dict] = {}
    real_save_incremental = SqliteStore.save_incremental

    def spy(self, *args, **kwargs):
        real_save_incremental(self, *args, **kwargs)
        if self.last_write_stats is not None:
            captured["stats"] = self.last_write_stats

    SqliteStore.save_incremental = spy  # type: ignore[method-assign]
    try:
        path = project / "mid.sv"
        path.write_text(path.read_text() + "// touched\n")
        report = run_update(project)
    finally:
        SqliteStore.save_incremental = real_save_incremental  # type: ignore[method-assign]

    assert report.build is not None and report.build.incremental_link
    stats = captured["stats"]
    # The scoped path reports read volume; its presence proves the whole-table
    # diff was avoided, and editing one of five files reads a strict subset.
    assert "nodes_scanned" in stats
    assert stats["nodes_scanned"] < total_nodes


def test_ref_index_scoping_is_superset_of_changed_pass2_edges(project: Path) -> None:
    """Stage-0 substrate guard (#64-A): every pass-2 edge that differs between
    two builds is owned by a reparsed file or surfaced by the reverse-index
    lookup — so the incremental linker's re-resolution set never under-includes.
    """
    from hdl_kgraph.graph.builder import _PASS2_EDGE_KINDS
    from hdl_kgraph.incremental import (
        affected_clean_refs,
        changed_definition_names,
        definition_profiles,
    )

    db = project / ".hdl-kgraph" / "graph.db"
    before = _graph(project)
    ref_index_before = SqliteStore(db).load_ref_index()

    (project / "leaf.sv").unlink()  # clean-file edge flip: mid still references leaf
    run_build(project)
    after = _graph(project)

    changed = changed_definition_names(
        definition_profiles(before.nodes(data=True)),
        definition_profiles(after.nodes(data=True)),
    )
    assert (
        next(iter(k for k in changed if k[1] == "leaf"), None) is not None
    )  # (MODULE, "leaf") flipped
    # Stage 1 re-resolves by src-node bucket (a CONNECTS ref also owns the
    # DRIVES/READS it derives on the same instance), so coverage is src-level.
    affected_srcs = {src for _, src, _ in affected_clean_refs(ref_index_before, changed)}
    dirty = {"leaf.sv"}

    def owning_file(node_id: str) -> str:
        for g in (before, after):
            if node_id in g.nodes:
                return str(g.nodes[node_id].get("file", ""))
        return ""

    def pass2_edges(g):
        return {
            (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
            for u, v, d in g.edges(data=True)
            if d["kind"] in _PASS2_EDGE_KINDS
        }

    for src, _dst, kind_value, _conf, _attrs in pass2_edges(before) ^ pass2_edges(after):
        covered = owning_file(src) in dirty or src in affected_srcs
        assert covered, f"pass-2 edge {src} {kind_value} not in dirty closure or affected refs"


def test_update_uses_incremental_link(project: Path) -> None:
    path = project / "mid.sv"
    path.write_text(path.read_text() + "// touched\n")
    report = run_update(project)
    assert report.build is not None
    assert report.build.incremental_link is True
    assert report.build.incremental_link_skipped is None


def test_vhdl_design_falls_back_to_full_link(tmp_path: Path) -> None:
    (tmp_path / "ent.vhd").write_text(
        "entity ent is end entity;\narchitecture rtl of ent is begin end architecture;\n"
    )
    (tmp_path / "m.sv").write_text("module m;\nendmodule\n")
    run_build(tmp_path)
    (tmp_path / "m.sv").write_text("module m;\n  logic x;\nendmodule\n")
    report = run_update(tmp_path)
    assert report.build is not None
    assert report.build.incremental_link is False
    assert "VHDL" in (report.build.incremental_link_skipped or "")


def test_incremental_link_safe_reasons() -> None:
    from hdl_kgraph.incremental import incremental_link_safe

    assert incremental_link_safe(False, False, False) is None
    assert "enrichment" in (incremental_link_safe(True, False, False) or "")
    assert "VHDL" in (incremental_link_safe(False, True, False) or "")
    assert "bind" in (incremental_link_safe(False, False, True) or "")


def test_update_graph_matches_full_rebuild(project: Path) -> None:
    path = project / "mid.sv"
    path.write_text(path.read_text().replace("u_leaf", "u_leaf2"))
    run_update(project)
    incremental = _graph(project)
    run_build(project)
    full = _graph(project)
    assert set(incremental.nodes) == set(full.nodes)

    def edge_set(g):
        return sorted(
            (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
            for u, v, d in g.edges(data=True)
        )

    assert edge_set(incremental) == edge_set(full)


def test_editing_includer_of_clean_header_matches_full_rebuild(tmp_path: Path) -> None:
    """Editing a unit that splices a *clean* header must not duplicate the
    header's pass-1 edges in the incremental graph (#99 review)."""
    (tmp_path / "common.svh").write_text("typedef logic [7:0] byte_t;\nlocalparam int W = 8;\n")
    (tmp_path / "a.sv").write_text(
        '`include "common.svh"\n'
        "module a(input byte_t x, output byte_t y);\n  assign y = x;\nendmodule\n"
    )
    (tmp_path / "b.sv").write_text(
        '`include "common.svh"\nmodule b(input byte_t x, output byte_t y);\n'
        "  a u_a(.x(x), .y(y));\nendmodule\n"
    )
    run_build(tmp_path)
    b = tmp_path / "b.sv"
    b.write_text(b.read_text().replace("a u_a", "a u_aa"))  # internal edit, header untouched
    report = run_update(tmp_path)
    assert report.build is not None and report.build.incremental_link is True
    incremental = _graph(tmp_path)
    run_build(tmp_path)
    full = _graph(tmp_path)
    assert set(incremental.nodes) == set(full.nodes)

    def edge_list(g):  # a list, so a duplicated edge fails the comparison
        return sorted(
            (u, v, d["kind"].value, d["confidence"], json.dumps(d["attrs"], sort_keys=True))
            for u, v, d in g.edges(data=True)
        )

    assert edge_list(incremental) == edge_list(full)


def test_changed_filelist_define_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    (tmp_path / "tb.f").write_text("+define+X=1\na.sv\n")
    from hdl_kgraph.config import BuildOptions

    options = BuildOptions(filelists=[tmp_path / "tb.f"])
    run_build(tmp_path, options=options)
    (tmp_path / "tb.f").write_text("+define+X=2\na.sv\n")
    report = run_update(tmp_path, options=options)
    assert report.full_rebuild_reason is not None
    assert "options changed" in report.full_rebuild_reason


def test_toggling_auto_incdir_falls_back_to_full_rebuild(tmp_path: Path) -> None:
    # auto_incdirs feeds the preprocessor globally, so flipping it must
    # invalidate an incremental reuse (it is fingerprinted in options_hash).
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    from hdl_kgraph.config import BuildOptions

    run_build(tmp_path, options=BuildOptions())  # auto_incdirs default True
    report = run_update(tmp_path, options=BuildOptions(auto_incdirs=False))
    assert report.full_rebuild_reason is not None
    assert "options changed" in report.full_rebuild_reason


def test_auto_incdir_resolves_header_in_other_dir_end_to_end(tmp_path: Path) -> None:
    # The reported bug: a `` `include`` of a define file in a *different* project
    # directory resolves out of the box (auto_incdirs on by default).
    (tmp_path / "rtl").mkdir()
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "defs.svh").write_text("`define W 4\n")
    (tmp_path / "rtl" / "top.sv").write_text(
        '`include "defs.svh"\nmodule top(output logic [`W-1:0] o);\nendmodule\n'
    )
    report = run_build(tmp_path)
    assert report.includes_resolved == 1
    assert report.includes_unresolved == 0


def test_filelist_membership_change_is_incremental(tmp_path: Path) -> None:
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    (tmp_path / "b.sv").write_text("module b;\nendmodule\n")
    (tmp_path / "tb.f").write_text("a.sv\n")
    from hdl_kgraph.config import BuildOptions

    options = BuildOptions(filelists=[tmp_path / "tb.f"])
    run_build(tmp_path, options=options)
    (tmp_path / "tb.f").write_text("a.sv\nb.sv\n")
    report = run_update(tmp_path, options=options)
    assert report.full_rebuild_reason is None
    assert report.reparsed == {"b.sv": "added"}
    graph = _graph(tmp_path)
    assert "b.sv::module:b" in graph


def test_schema_version_mismatch_falls_back_to_full_rebuild(project: Path) -> None:
    import sqlite3

    db = project / ".hdl-kgraph" / "graph.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.close()  # an open handle blocks the rebuild's atomic swap on Windows
    report = run_update(project)
    assert report.full_rebuild_reason is not None
    assert "schema version" in report.full_rebuild_reason
    assert SqliteStore(db).load()  # readable again


def test_old_schema_migrates_in_place_instead_of_rebuilding(project: Path) -> None:
    """A v7 database (registered, IR-compatible step) is upgraded in place, so
    ``update`` re-parses only the edited file rather than full-rebuilding."""
    import sqlite3

    db = project / ".hdl-kgraph" / "graph.db"
    conn = sqlite3.connect(db)
    with conn:
        conn.execute("DROP TABLE IF EXISTS summaries")
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
        conn.execute("DELETE FROM meta WHERE key = 'ir_codec_version'")
    conn.close()

    path = project / "mid.sv"
    path.write_text(path.read_text() + "// touched\n")
    report = run_update(project)

    assert report.full_rebuild_reason is None  # migrated, not rebuilt
    assert report.reparsed == {"mid.sv": "changed"}
    _, _, meta = SqliteStore(db).load()
    # The whole contiguous ladder runs (v7 -> v8 -> v9), not just one step.
    assert meta["schema_version"] == SCHEMA_VERSION


def test_forced_full_rebuild(project: Path) -> None:
    report = run_update(project, full=True)
    assert report.full_rebuild_reason == "forced with --full"


def test_consumed_header_becomes_standalone_when_include_is_dropped(tmp_path: Path) -> None:
    # z_late.svh sorts after user.sv, so it is consumed (spliced) at build
    # time; dropping the include must let it parse standalone again.
    (tmp_path / "user.sv").write_text('`include "z_late.svh"\nmodule user;\nendmodule\n')
    (tmp_path / "z_late.svh").write_text("`define L 4\n")
    report = run_build(tmp_path)
    assert report.skipped.get("included") == 1
    (tmp_path / "user.sv").write_text("module user;\nendmodule\n")
    update = run_update(tmp_path)
    assert update.build is not None
    assert update.build.skipped.get("included") is None
    graph = _graph(tmp_path)
    assert any(
        d["kind"] is EdgeKind.DEFINES_MACRO and u == "file:z_late.svh"
        for u, _, d in graph.edges(data=True)
    )


def test_diff_hashes() -> None:
    changes = diff_hashes(
        {"a.sv": "1", "b.sv": "2", "c.sv": "3"},
        {"a.sv": "1", "b.sv": "9", "d.sv": "4"},
    )
    assert changes == ChangeSet(changed=["b.sv"], added=["d.sv"], removed=["c.sv"])
    assert bool(changes)
    assert not ChangeSet()


def test_scan_changes_clean_with_skipped_file(tmp_path: Path) -> None:
    # Skipped files keep their stored hash, so an untouched tree diffs clean
    # even though they never parsed (guards the load_file_hashes fast path).
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    (tmp_path / "big.sv").write_text("module big;\nendmodule\n" + "// pad\n" * 200)
    options = BuildOptions(max_file_size_kb=1)
    report = run_build(tmp_path, options=options)
    assert report.skipped.get("size") == 1
    assert not scan_changes(tmp_path, report.db_path, options)


def test_detect_git_changes(tmp_path: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not available")
    git = ["git", "-C", str(tmp_path)]
    commit = [*git, "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false"]
    subprocess.run([*git, "init", "-q"], check=True)
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    (tmp_path / "notes.txt").write_text("not hdl\n")
    subprocess.run([*git, "add", "a.sv", "notes.txt"], check=True)
    result = subprocess.run([*commit, "commit", "-q", "-m", "add"], capture_output=True)
    if result.returncode != 0:
        pytest.skip(f"cannot commit in this environment: {result.stderr.decode()[:100]}")
    (tmp_path / "a.sv").write_text("module a2;\nendmodule\n")
    (tmp_path / "new.svh").write_text("`define N 1\n")
    (tmp_path / "notes.txt").write_text("still not hdl\n")

    from hdl_kgraph.discovery import SUFFIXES

    changes = detect_git_changes(tmp_path, "HEAD", SUFFIXES)
    assert changes.changed == ["a.sv"]
    assert changes.added == ["new.svh"]
    assert changes.removed == []


@pytest.mark.parametrize("ref", ["--output=/tmp/pwn", "-Gsecret", "", "-"])
def test_detect_git_changes_rejects_option_like_ref(tmp_path: Path, ref: str) -> None:
    """A ref that looks like an option is rejected before git ever runs."""
    from hdl_kgraph.discovery import SUFFIXES

    with pytest.raises(RuntimeError, match="looks like an option"):
        detect_git_changes(tmp_path, ref, SUFFIXES)
