"""End-to-end CLI tests: build -> status -> query -> tree on the fixtures."""

import io
import itertools
import shutil
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph import __version__
from hdl_kgraph.cli.main import _ProgressRenderer, main


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    """A tmp copy of the fixture corpus with a graph already built."""
    root = tmp_path_factory.mktemp("project")
    for path in fixtures_dir.iterdir():
        if path.is_file():  # subdirectories (e.g. preproc/) have their own tests
            shutil.copy(path, root / path.name)
    result = CliRunner().invoke(main, ["build", str(root)])
    assert result.exit_code == 0, result.output
    return root


def db_args(project: Path) -> list[str]:
    return ["--db", str(project / ".hdl-kgraph" / "graph.db")]


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_lists_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("build", "status", "query", "tree"):
        assert command in result.output


def _hdl_fixture_count(fixtures_dir: Path, *suffixes: str) -> int:
    return sum(1 for p in fixtures_dir.iterdir() if p.is_file() and p.suffix in suffixes)


def test_build_reports_summary(project: Path, fixtures_dir: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(project)])
    assert result.exit_code == 0
    parsed = _hdl_fixture_count(fixtures_dir, ".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl")
    vhdl = _hdl_fixture_count(fixtures_dir, ".vhd", ".vhdl")
    assert f"files parsed:   {parsed}" in result.output
    assert f"vhdl files:     {vhdl}" in result.output
    assert "parse errors:" in result.output  # broken.sv
    assert "unresolved:" in result.output  # ghost_mod etc.


def test_build_timings_breakdown(project: Path) -> None:
    """``build --timings`` prints the per-phase split used to gate the merge
    feature (parallelizable discover+parse vs. the serial link)."""
    result = CliRunner().invoke(main, ["build", str(project), "--timings"])
    assert result.exit_code == 0
    assert "timings:" in result.output
    for phase in ("discover", "parse (pass 0+1)", "link (pass 2)", "persist"):
        assert phase in result.output
    assert "parallelizable" in result.output
    assert "serial link" in result.output


def test_build_no_timings_by_default(project: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(project)])
    assert result.exit_code == 0
    assert "timings:" not in result.output


def test_build_empty_dir_fails(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code != 0
    assert "no parseable HDL files" in result.output


def test_failed_build_preserves_existing_db(project: Path, tmp_path: Path) -> None:
    db = project / ".hdl-kgraph" / "graph.db"
    before = db.read_bytes()
    result = CliRunner().invoke(main, ["build", str(tmp_path), "--db", str(db)])
    assert result.exit_code != 0
    assert db.read_bytes() == before


def test_status(project: Path, fixtures_dir: Path) -> None:
    result = CliRunner().invoke(main, ["status", *db_args(project)])
    assert result.exit_code == 0, result.output
    parsed = _hdl_fixture_count(fixtures_dir, ".sv", ".svh", ".v", ".vh", ".vhd", ".vhdl")
    assert f"{parsed} parsed" in result.output
    assert "parse error(s)" in result.output
    assert "module" in result.output
    assert "instantiates" in result.output
    assert "unresolved:" in result.output


def test_status_without_db_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code != 0
    assert "hdl-kgraph build" in result.output


def test_query_instances_of(project: Path) -> None:
    result = CliRunner().invoke(
        main, ["query", "instances-of", "simple_counter", *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    assert "top.u_counter" in result.output
    assert "top.v:" in result.output
    assert "confidence=0.8" in result.output


def test_query_instances_of_unknown_name(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "instances-of", "nonexistent", *db_args(project)])
    assert result.exit_code == 1


def test_query_modules(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "modules", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "simple_counter" in result.output
    assert "instances=1" in result.output


def test_query_unresolved(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "unresolved", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "module:ghost_mod" in result.output
    assert "class:uvm_test" in result.output


def test_query_clock_domains(project: Path) -> None:
    import re

    result = CliRunner().invoke(main, ["query", "clock-domains", *db_args(project)])
    assert result.exit_code == 0, result.output
    # The bounded report labels the clock net by *name*, on its own line — not the
    # qualified_name the pre-v2 full-load report showed.
    assert re.search(r"(?m)^clk_a\b", result.output), result.output
    assert ".clk_a" not in result.output  # no qualified label leaked through
    assert "processes:" in result.output


def test_query_clock_domains_json_is_bounded_payload(project: Path) -> None:
    import json as json_mod

    from hdl_kgraph.pipeline import default_db_path
    from hdl_kgraph.storage.query import GraphQuery

    result = CliRunner().invoke(main, ["query", "clock-domains", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    # The CLI now emits the bounded summary payload (counts, not O(design) id-lists),
    # byte-identical to the GraphQuery path the MCP server uses.
    assert set(payload) == {
        "domains",
        "cdc_suspect_count",
        "cdc_suspects",
        "cdc_suppressed_count",
        "cdc_suppressed",
    }
    assert payload["domains"]
    for domain in payload["domains"]:
        assert set(domain) == {
            "clock",
            "aliases",
            "process_count",
            "signal_count",
            "min_confidence",
        }
    assert payload == GraphQuery(default_db_path(project)).clock_domains()


def test_query_cdc_finds_the_planted_crossing(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "cdc", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "data_a" in result.output
    assert "clk_a ->" in result.output


def test_query_cdc_json(project: Path) -> None:
    import json as json_mod

    result = CliRunner().invoke(main, ["query", "cdc", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert any(item["signal_name"] == "data_a" for item in payload)


def test_query_reset_tree(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "reset-tree", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "rst_n" in result.output
    assert "async" in result.output


def test_query_drivers(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "drivers", "data_a", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "two_clock_top.always@22" in result.output


def test_query_drivers_readers(project: Path) -> None:
    result = CliRunner().invoke(
        main, ["query", "drivers", "data_a", "--readers", *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    assert "two_clock_top.always@28" in result.output


def test_lint_reports_planted_findings(project: Path) -> None:
    result = CliRunner().invoke(main, ["lint", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "unconnected-port" in result.output
    assert "undriven-signal" in result.output
    assert "redundant-override" in result.output


def test_lint_check_filter_and_json(project: Path) -> None:
    import json as json_mod

    result = CliRunner().invoke(main, ["lint", "--check", "open-port", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert payload["findings"]
    assert all(item["check"] == "open-port" for item in payload["findings"])
    assert payload["waived"] == []
    assert payload["counts"] == {"findings": len(payload["findings"]), "waived": 0}


def test_lint_unknown_check_fails(project: Path) -> None:
    result = CliRunner().invoke(main, ["lint", "--check", "bogus", *db_args(project)])
    assert result.exit_code != 0
    assert "unknown lint check" in result.output


def _write_open_port_waiver(directory: Path) -> Path:
    path = directory / "waivers.toml"
    path.write_text(
        '[[lint.waivers]]\ncheck  = "open-port"\nfile   = "lint_case.sv"\n'
        'reason = "intentional tie-off"\n'
    )
    return path


def test_lint_waiver_file_filters_findings(project: Path, tmp_path: Path) -> None:
    waiver = _write_open_port_waiver(tmp_path)
    args = ["lint", "--check", "open-port", "--waiver-file", str(waiver), *db_args(project)]
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output
    assert "no findings (1 waived)" in result.output
    assert "explicitly left open" not in result.output

    shown = CliRunner().invoke(main, [*args, "--show-waived"])
    assert shown.exit_code == 0, shown.output
    assert "[waived: intentional tie-off]" in shown.output
    assert "no findings (1 waived)" in shown.output


def test_lint_waiver_json_shape(project: Path, tmp_path: Path) -> None:
    import json as json_mod

    waiver = _write_open_port_waiver(tmp_path)
    result = CliRunner().invoke(
        main,
        ["lint", "--check", "open-port", "--json", "--waiver-file", str(waiver), *db_args(project)],
    )
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert payload["findings"] == []
    assert payload["waived"][0]["reason"] == "intentional tie-off"
    assert payload["waived"][0]["finding"]["check"] == "open-port"
    assert payload["waived"][0]["finding"]["unit"] == "lint_leaf"
    assert payload["unused_waivers"] == []
    assert payload["counts"] == {"findings": 0, "waived": 1}


def test_lint_stale_waiver_warns(project: Path, tmp_path: Path) -> None:
    waiver = tmp_path / "waivers.toml"
    waiver.write_text(
        '[[lint.waivers]]\ncheck = "open-port"\nname = "no_such.*"\nreason = "stale"\n'
        '[[lint.waivers]]\ncheck = "gone-check"\nname = "*"\nreason = "stale"\n'
    )
    result = CliRunner().invoke(main, ["lint", "--waiver-file", str(waiver), *db_args(project)])
    assert result.exit_code == 0, result.output  # a report, not a gate
    stale_warning = "warning: lint waiver #1 (check=open-port, name=no_such.*) matched nothing"
    assert stale_warning in result.output
    assert "warning: lint waiver #2 names unknown check 'gone-check'" in result.output

    # The waiver is not stale when its check is not selected.
    scoped = CliRunner().invoke(
        main,
        ["lint", "--check", "dead-module", "--waiver-file", str(waiver), *db_args(project)],
    )
    assert scoped.exit_code == 0, scoped.output
    assert "matched nothing" not in scoped.output


def test_lint_discovers_config_waivers_and_build_top(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copy(fixtures_dir / "lint_case.sv", tmp_path / "lint_case.sv")
    (tmp_path / "hdl-kgraph.toml").write_text(
        """
        [build]
        top = ["lint_top"]

        [[lint.waivers]]
        check  = "open-port"
        name   = "lint_top.u_leaf"
        reason = "documented tie-off"
        """
    )
    build = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert build.exit_code == 0, build.output
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main, ["lint"])
    assert result.exit_code == 0, result.output
    dead = [line.split() for line in result.output.splitlines() if line.startswith("dead-module")]
    assert [row[1] for row in dead] == ["lint_dead"]  # [build].top exempts lint_top
    assert "explicitly left open" not in result.output
    assert "1 waived" in result.output

    ignored = CliRunner().invoke(main, ["lint", "--no-config"])
    assert ignored.exit_code == 0, ignored.output
    assert "explicitly left open" in ignored.output
    assert [
        row.split()[1] for row in ignored.output.splitlines() if row.startswith("dead-module")
    ] == ["lint_top", "lint_dead"]


def test_query_uvm(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "uvm", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "test:" in result.output
    assert "verif_smoke_test" in result.output
    assert "covers verif_dut" in result.output


def test_query_uvm_json_is_bounded_payload(project: Path) -> None:
    import json as json_mod

    from hdl_kgraph.pipeline import default_db_path
    from hdl_kgraph.storage.query import GraphQuery

    result = CliRunner().invoke(main, ["query", "uvm", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert set(payload) == {"components", "test_covers"}
    # CLI ≡ the bounded GraphQuery payload the MCP server serves.
    assert payload == GraphQuery(default_db_path(project)).uvm_topology()


def _upf_project(tmp_path: Path, fixtures_dir: Path) -> Path:
    """The counter design plus its UPF, flattened into one build root."""
    for name in ("top.v", "simple_counter.sv"):
        (tmp_path / name).write_text((fixtures_dir / name).read_text())
    (tmp_path / "power.upf").write_text((fixtures_dir / "upf" / "power.upf").read_text())
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_query_power_domains(tmp_path: Path, fixtures_dir: Path) -> None:
    root = _upf_project(tmp_path, fixtures_dir)
    result = CliRunner().invoke(main, ["query", "power-domains", *db_args(root)])
    assert result.exit_code == 0, result.output
    assert "PD_COUNTER [isolated]" in result.output
    assert "element top.u_counter" in result.output
    assert "isolation iso_counter" in result.output


def test_review_reports_power_domains(tmp_path: Path, fixtures_dir: Path) -> None:
    root = _upf_project(tmp_path, fixtures_dir)
    result = CliRunner().invoke(main, ["review", *db_args(root)])
    assert result.exit_code == 0, result.output
    assert "power domains 2  isolated 1" in result.output


def test_visualize_writes_html(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "g.html"
    result = CliRunner().invoke(main, ["visualize", "-o", str(out), *db_args(project)])
    assert result.exit_code == 0, result.output
    html = out.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert "simple_counter" in html


def test_visualize_force_inline_flag_accepted(project: Path, tmp_path: Path) -> None:
    # The Phase-4a size-guard override flag (the cap itself is unit-tested in
    # test_visualize.py against a shrunk limit); here just confirm the CLI
    # surface exists and writes the normal small artifact.
    out = tmp_path / "g.html"
    result = CliRunner().invoke(
        main, ["visualize", "--force-inline", "-o", str(out), *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    assert out.read_text().startswith("<!DOCTYPE html>")


def test_visualize_collapse_writes_aggregated_html(project: Path, tmp_path: Path) -> None:
    # Phase-3 collapsed view: the artifact embeds the supernode aggregation
    # (payload shape is unit-tested in test_visualize.py).
    out = tmp_path / "g.html"
    result = CliRunner().invoke(
        main, ["visualize", "--collapse", "-o", str(out), *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    html = out.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert '"collapse": true' in html or '"collapse":true' in html


def test_visualize_collapse_full_two_level(project: Path, tmp_path: Path) -> None:
    # --collapse --full now produces the two-level aggregation (payload shape is
    # unit-tested in test_visualize.py); here just confirm the CLI writes it.
    out = tmp_path / "g.html"
    result = CliRunner().invoke(
        main, ["visualize", "--collapse", "--full", "-o", str(out), *db_args(project)]
    )
    assert result.exit_code == 0, result.output
    html = out.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert '"unitnodes"' in html


def test_metrics_lists_hubs(project: Path) -> None:
    result = CliRunner().invoke(main, ["metrics", "--limit", "0", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "fan-in" in result.output
    assert "alu" in result.output


def test_metrics_communities_json(project: Path) -> None:
    import json as json_mod

    result = CliRunner().invoke(main, ["metrics", "--communities", "--json", *db_args(project)])
    assert result.exit_code == 0, result.output
    payload = json_mod.loads(result.output)
    assert payload["modules"]
    assert payload["communities"]
    assert payload["betweenness_approximate"] is False


def test_metrics_notes_sampled_betweenness(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hdl_kgraph.graph import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "BETWEENNESS_EXACT_MAX_NODES", 1)
    result = CliRunner().invoke(main, ["metrics", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "note: betweenness sampled" in result.output
    json_result = CliRunner().invoke(main, ["metrics", "--json", *db_args(project)])
    import json as json_mod

    assert json_mod.loads(json_result.output)["betweenness_approximate"] is True


def test_tree_from_top(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "top"
    assert "u_counter: simple_counter" in result.output


def test_tree_marks_unresolved_and_ambiguous(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "u_ghost: ghost_mod [?]" in result.output
    assert "u_leaf: dup_leaf [~0.6]" in result.output


def test_tree_unknown_module(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "nonexistent", *db_args(project)])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_db_discovery_walks_up(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subdir = project / "sub" / "dir"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    result = CliRunner().invoke(main, ["status"])
    assert result.exit_code == 0, result.output


def test_tree_mixed_sv_top_shows_vhdl_leaf(project: Path) -> None:
    """Verilog-top / VHDL-leaf acceptance: one connected hierarchy."""
    result = CliRunner().invoke(main, ["tree", "mixed_sv_top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "mixed_sv_top"
    assert "u_alu: alu(rtl)" in result.output


def test_tree_vhdl_top_shows_sv_leaves(project: Path) -> None:
    """VHDL-top / Verilog-leaf acceptance, both instantiation styles."""
    result = CliRunner().invoke(main, ["tree", "vhdl_top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "vhdl_top(rtl)"
    assert "u_counter: simple_counter" in result.output
    assert "u_fifo: FIFO" in result.output  # case-folded component match
    assert "u_alu: alu(rtl)" in result.output


def test_tree_honors_configuration_override(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "cfg_top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "u_leaf: leaf_special(rtl)" in result.output
    assert "leaf_default" not in result.output


def test_tree_vhdl_name_is_case_insensitive(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "VHDL_Top", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == "vhdl_top(rtl)"


def test_query_instances_of_spans_languages(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "instances-of", "alu", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "mixed_sv_top.sv:" in result.output
    assert "vhdl_top.vhd:" in result.output
    assert "confidence=0.8" in result.output


def test_query_modules_lists_entities(project: Path) -> None:
    result = CliRunner().invoke(main, ["query", "modules", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert "alu [vhdl]" in result.output


def test_build_lib_flag(tmp_path: Path, fixtures_dir: Path) -> None:
    from hdl_kgraph.storage.sqlite_store import SqliteStore

    libdir = tmp_path / "mylib_src"
    libdir.mkdir()
    shutil.copy(fixtures_dir / "alu.vhd", libdir / "alu.vhd")
    result = CliRunner().invoke(main, ["build", str(tmp_path), "--lib", f"mylib={libdir}"])
    assert result.exit_code == 0, result.output
    graph, _, _ = SqliteStore(tmp_path / ".hdl-kgraph" / "graph.db").load()
    lib = graph.nodes["library:mylib"]
    assert lib["attrs"]["path"] == str(libdir)
    entity = graph.nodes["mylib_src/alu.vhd::entity:alu"]
    assert entity["attrs"]["library"] == "mylib"


def test_build_lib_flag_malformed(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(tmp_path), "--lib", "nopath"])
    assert result.exit_code != 0
    assert "NAME=PATH" in result.output


# -- M4: update / detect-changes / impact ------------------------------------


@pytest.fixture
def small_project(tmp_path: Path) -> Path:
    (tmp_path / "leaf.sv").write_text(
        "module leaf(input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    )
    (tmp_path / "top.sv").write_text(
        "module top(input logic a, output logic y);\n  leaf u_leaf(.a(a), .y(y));\nendmodule\n"
    )
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_build_jobs_flag(small_project: Path) -> None:
    serial = CliRunner().invoke(main, ["build", str(small_project), "--jobs", "1"])
    assert serial.exit_code == 0, serial.output
    parallel = CliRunner().invoke(main, ["build", str(small_project), "-j", "2"])
    assert parallel.exit_code == 0, parallel.output
    nodes_line = next(ln for ln in serial.output.splitlines() if "nodes:" in ln)
    assert nodes_line in parallel.output


def test_help_lists_m4_commands() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    for command in ("update", "detect-changes", "impact", "watch"):
        assert command in result.output


def test_update_up_to_date(small_project: Path) -> None:
    result = CliRunner().invoke(main, ["update", str(small_project)])
    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_update_reports_reparsed_files(small_project: Path) -> None:
    leaf = small_project / "leaf.sv"
    leaf.write_text(leaf.read_text() + "// touched\n")
    result = CliRunner().invoke(main, ["update", str(small_project)])
    assert result.exit_code == 0, result.output
    assert "re-parsed: leaf.sv (changed)" in result.output
    assert "files reused:   1" in result.output


def test_update_without_db_does_full_build(tmp_path: Path) -> None:
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    result = CliRunner().invoke(main, ["update", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "full rebuild: no existing database" in result.output


def test_update_full_flag(small_project: Path) -> None:
    result = CliRunner().invoke(main, ["update", str(small_project), "--full"])
    assert result.exit_code == 0, result.output
    assert "full rebuild: forced with --full" in result.output


def test_detect_changes_exit_codes(small_project: Path) -> None:
    clean = CliRunner().invoke(main, ["detect-changes", str(small_project)])
    assert clean.exit_code == 0, clean.output
    assert clean.output == ""

    (small_project / "leaf.sv").write_text("module leaf;\nendmodule\n")
    (small_project / "new.sv").write_text("module new_mod;\nendmodule\n")
    dirty = CliRunner().invoke(main, ["detect-changes", str(small_project)])
    assert dirty.exit_code == 1
    assert "M leaf.sv" in dirty.output
    assert "A new.sv" in dirty.output


def test_detect_changes_reports_deletions(small_project: Path) -> None:
    (small_project / "leaf.sv").unlink()
    result = CliRunner().invoke(main, ["detect-changes", str(small_project)])
    assert result.exit_code == 1
    assert "D leaf.sv" in result.output


def test_detect_changes_without_db_exits_2(tmp_path: Path) -> None:
    # git diff --exit-code convention: 0 clean, 1 dirty, 2 error — scripts
    # must be able to tell "changed" from "broken".
    (tmp_path / "a.sv").write_text("module a;\nendmodule\n")
    result = CliRunner().invoke(main, ["detect-changes", str(tmp_path)])
    assert result.exit_code == 2, result.output
    assert "hdl-kgraph build" in result.output


def test_detect_changes_closure(tmp_path: Path) -> None:
    (tmp_path / "defs.svh").write_text("`define W 8\n")
    (tmp_path / "leaf.sv").write_text(
        '`include "defs.svh"\nmodule leaf(output logic [`W-1:0] y);\nendmodule\n'
    )
    assert CliRunner().invoke(main, ["build", str(tmp_path)]).exit_code == 0
    (tmp_path / "defs.svh").write_text("`define W 16\n")
    db = str(tmp_path / ".hdl-kgraph" / "graph.db")
    result = CliRunner().invoke(main, ["detect-changes", str(tmp_path), "--closure", "--db", db])
    assert result.exit_code == 1
    assert "M defs.svh" in result.output
    assert "~ leaf.sv (includes defs.svh)" in result.output


def test_impact_module(small_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(small_project)
    result = CliRunner().invoke(main, ["impact", "leaf"])
    assert result.exit_code == 0, result.output
    assert "top" in result.output
    assert "instantiates (depth 1)" in result.output


def test_impact_file_target_and_files_flag(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    result = CliRunner().invoke(main, ["impact", "leaf.sv", *db])
    assert result.exit_code == 0, result.output
    assert "leaf" in result.output and "top" in result.output

    files = CliRunner().invoke(main, ["impact", "leaf.sv", "--files", *db])
    assert files.exit_code == 0, files.output
    assert "top.sv" in files.output


def test_impact_unknown_target(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    result = CliRunner().invoke(main, ["impact", "ghost", *db])
    assert result.exit_code != 0
    assert "matches no file or design unit" in result.output


def test_impact_top_has_no_dependents(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    result = CliRunner().invoke(main, ["impact", "top", *db])
    assert result.exit_code == 0, result.output
    assert "no dependents found" in result.output


def test_serve_defaults_to_mcp(tmp_path: Path) -> None:
    # MCP is the default mode: without --mcp the command proceeds far enough
    # to notice the missing database instead of demanding the flag.
    result = CliRunner().invoke(main, ["serve", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code != 0
    assert "pass --mcp" not in result.output
    assert "database not found" in result.output


def test_serve_missing_db(tmp_path: Path) -> None:
    result = CliRunner().invoke(main, ["serve", "--mcp", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code != 0
    assert "database not found" in result.output


def test_serve_bad_http_address(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fastmcp")
    result = CliRunner().invoke(
        main, ["serve", "--mcp", "--http", "not-an-address", *db_args(project)]
    )
    assert result.exit_code != 0
    assert "HOST:PORT" in result.output


def test_serve_without_fastmcp(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "fastmcp", None)  # forces ImportError
    result = CliRunner().invoke(main, ["serve", "--mcp", *db_args(project)])
    assert result.exit_code != 0
    assert "hdl-kgraph[mcp]" in result.output


def test_drivers_module_filter(project: Path) -> None:
    scoped = CliRunner().invoke(
        main, ["query", "drivers", "o", "--module", "df_sub", *db_args(project)]
    )
    assert scoped.exit_code == 0, scoped.output
    assert "df_sub" in scoped.output

    empty = CliRunner().invoke(
        main, ["query", "drivers", "o", "--module", "df_top", *db_args(project)]
    )
    assert empty.exit_code != 0


# -- diagnostics: -v / status --errors ----------------------------------------


@pytest.fixture
def diag_project(tmp_path: Path, fixtures_dir: Path) -> Path:
    """A project with a parse error (broken.sv) and an unresolved `include."""
    shutil.copy(fixtures_dir / "broken.sv", tmp_path / "broken.sv")
    (tmp_path / "top.sv").write_text('`include "missing.svh"\nmodule top;\nendmodule\n')
    return tmp_path


def test_build_verbose_reports_stages_and_per_file_diagnostics(diag_project: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(diag_project), "-v"])
    assert result.exit_code == 0, result.output
    assert "discovering source files" in result.output
    assert "pass 0+1" in result.output
    assert "pass 2" in result.output
    assert "writing" in result.output
    assert "broken.sv:" in result.output  # per-file parse-error count
    assert "broken.sv:6: syntax error near `" in result.output  # exact error location
    assert 'cannot resolve `include "missing.svh"' in result.output
    # No explicit -I, but auto-incdir adds the source dir; the search path line
    # reflects that and a hint points at the fix.
    assert "`include search path: (none) + 1 auto-discovered source dir(s)" in result.output
    assert "hint:" in result.output


def test_build_verbose_lists_incdirs_for_unresolved_includes(diag_project: Path) -> None:
    (diag_project / "inc").mkdir()
    result = CliRunner().invoke(
        main, ["build", str(diag_project), "-I", str(diag_project / "inc"), "-v"]
    )
    assert result.exit_code == 0, result.output
    assert "`include search path:" in result.output
    assert "inc" in result.output


def test_build_without_verbose_hints_at_details(diag_project: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(diag_project)])
    assert result.exit_code == 0, result.output
    assert "cannot resolve" not in result.output
    assert "status --errors" in result.output


def test_build_reports_stages_by_default(diag_project: Path) -> None:
    result = CliRunner().invoke(main, ["build", str(diag_project)])
    assert result.exit_code == 0, result.output
    assert "discovering source files" in result.output
    assert "pass 0+1" in result.output
    assert "pass 2" in result.output
    assert "writing" in result.output


def test_build_non_tty_counter_milestones(tmp_path: Path) -> None:
    for i in range(30):
        (tmp_path / f"m{i:02}.sv").write_text(f"module m{i:02};\nendmodule\n")
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "pass 0+1: parsing 25/30 file(s)" in result.output
    assert "pass 0+1: parsing 30/30 file(s)" in result.output
    assert "\r" not in result.output  # CliRunner streams are not TTYs


def test_build_non_tty_counter_small_build_final_only(tmp_path: Path) -> None:
    for i in range(3):
        (tmp_path / f"m{i}.sv").write_text(f"module m{i};\nendmodule\n")
    result = CliRunner().invoke(main, ["build", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "pass 0+1: parsing 3/3 file(s)" in result.output
    assert "parsing 1/3" not in result.output


def test_update_reports_progress_by_default(diag_project: Path) -> None:
    build = CliRunner().invoke(main, ["build", str(diag_project)])
    assert build.exit_code == 0, build.output
    (diag_project / "newer.sv").write_text("module newer;\nendmodule\n")
    result = CliRunner().invoke(main, ["update", str(diag_project)])
    assert result.exit_code == 0, result.output
    assert "scanning for changed files" in result.output


class _TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_progress_renderer_tty_rewrites_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = _TtyStringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    clock = itertools.count(start=1)
    monkeypatch.setattr(time, "monotonic", lambda: float(next(clock)))
    renderer = _ProgressRenderer()
    renderer.stage("pass 0+1: preprocessing and parsing 2 file(s)")
    renderer.tick(1, 2)
    renderer.tick(2, 2)
    renderer.stage("pass 2: linking")
    out = stream.getvalue()
    assert "\rpass 0+1: parsing 1/2 file(s)..." in out
    # The live line is terminated before the next stage line prints.
    assert "pass 0+1: parsing 2/2 file(s)...\npass 2: linking\n" in out


def test_progress_renderer_tty_throttles_but_always_draws_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _TtyStringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)  # frozen clock
    renderer = _ProgressRenderer()
    renderer.tick(1, 3)
    renderer.tick(2, 3)  # within MIN_INTERVAL_S of the last draw: skipped
    renderer.tick(3, 3)  # done == total always draws
    out = stream.getvalue()
    assert "1/3" in out
    assert "2/3" not in out
    assert "3/3" in out


def test_progress_renderer_non_tty_milestones(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    renderer = _ProgressRenderer()
    for done in range(1, 31):
        renderer.tick(done, 30)
    renderer.finish()  # no live line pending in milestone mode: no-op
    out = stream.getvalue()
    assert "\r" not in out
    assert "pass 0+1: parsing 25/30 file(s)\n" in out
    assert "pass 0+1: parsing 30/30 file(s)\n" in out
    assert "parsing 1/30" not in out


def test_status_errors_lists_per_file_diagnostics(diag_project: Path) -> None:
    build = CliRunner().invoke(main, ["build", str(diag_project)])
    assert build.exit_code == 0, build.output
    result = CliRunner().invoke(main, ["status", "--errors", *db_args(diag_project)])
    assert result.exit_code == 0, result.output
    assert "broken.sv:" in result.output
    assert "parse error(s)" in result.output
    assert "broken.sv:6: syntax error near `" in result.output  # exact error location
    assert 'cannot resolve `include "missing.svh"' in result.output


def test_status_errors_reports_skipped_files(diag_project: Path) -> None:
    (diag_project / "huge.sv").write_text("module huge;\nendmodule\n" + "// filler\n" * 200)
    build = CliRunner().invoke(main, ["build", str(diag_project), "--max-file-size", "1"])
    assert build.exit_code == 0, build.output
    result = CliRunner().invoke(main, ["status", "--errors", *db_args(diag_project)])
    assert result.exit_code == 0, result.output
    assert "huge.sv: skipped (size)" in result.output


def test_status_errors_clean_project(small_project: Path) -> None:
    result = CliRunner().invoke(main, ["status", "--errors", *db_args(small_project)])
    assert result.exit_code == 0, result.output
    assert "no parse errors" in result.output


def test_status_summary_hints_at_errors_listing(diag_project: Path) -> None:
    build = CliRunner().invoke(main, ["build", str(diag_project)])
    assert build.exit_code == 0, build.output
    result = CliRunner().invoke(main, ["status", *db_args(diag_project)])
    assert result.exit_code == 0, result.output
    assert "preprocessor warning(s)" in result.output
    assert "status --errors" in result.output


def test_update_verbose_keeps_warnings_of_reused_files(diag_project: Path) -> None:
    build = CliRunner().invoke(main, ["build", str(diag_project)])
    assert build.exit_code == 0, build.output
    (diag_project / "other.sv").write_text("module other;\nendmodule\n")
    result = CliRunner().invoke(main, ["update", str(diag_project), "-v"])
    assert result.exit_code == 0, result.output
    assert "re-parsed: other.sv (added)" in result.output
    # top.sv was re-linked, not re-preprocessed; its warning must survive.
    assert 'cannot resolve `include "missing.svh"' in result.output
    # broken.sv was reused too; its parse-error details come from the stored IR.
    assert "broken.sv:6: syntax error near `" in result.output


# -- issue #22: --json coverage, exit codes, serve/visualize polish -----------


def _json_out(result) -> object:
    import json as json_mod

    assert result.exit_code in (0, 1), result.output
    return json_mod.loads(result.output)


def test_status_json(project: Path) -> None:
    payload = _json_out(CliRunner().invoke(main, ["status", "--json", *db_args(project)]))
    assert payload["files"]["parsed"] > 0
    assert payload["files"]["parse_errors"] > 0  # broken.sv
    assert payload["nodes"]["kinds"]["module"] > 0
    assert payload["edges"]["total"] > 0
    assert payload["unresolved"] > 0


def test_status_errors_json(project: Path) -> None:
    payload = _json_out(
        CliRunner().invoke(main, ["status", "--errors", "--json", *db_args(project)])
    )
    broken = next(f for f in payload if f["path"] == "broken.sv")
    assert broken["parse_errors"] > 0
    assert any("syntax error near" in e for e in broken["errors"])


def test_tree_json(project: Path) -> None:
    result = CliRunner().invoke(main, ["tree", "mixed_sv_top", "--json", *db_args(project)])
    payload = _json_out(result)
    assert payload[0]["module_name"] == "mixed_sv_top"
    assert payload[0]["children"]


def test_impact_json(small_project: Path) -> None:
    db = ["--db", str(small_project / ".hdl-kgraph" / "graph.db")]
    payload = _json_out(CliRunner().invoke(main, ["impact", "leaf", "--json", *db]))
    assert any(r["name"] == "top" and r["depth"] == 1 for r in payload)

    files = _json_out(CliRunner().invoke(main, ["impact", "leaf", "--files", "--json", *db]))
    assert files == ["top.sv"]


def test_detect_changes_json_keeps_exit_codes(small_project: Path) -> None:
    clean = CliRunner().invoke(main, ["detect-changes", str(small_project), "--json"])
    assert clean.exit_code == 0, clean.output
    assert _json_out(clean) == {"changed": [], "added": [], "removed": []}

    (small_project / "leaf.sv").write_text("module leaf;\nendmodule\n")
    dirty = CliRunner().invoke(main, ["detect-changes", str(small_project), "--json"])
    assert dirty.exit_code == 1
    assert _json_out(dirty)["changed"] == ["leaf.sv"]


def test_query_modules_json(project: Path) -> None:
    payload = _json_out(CliRunner().invoke(main, ["query", "modules", "--json", *db_args(project)]))
    by_name = {m["name"]: m for m in payload}
    assert by_name["simple_counter"]["instances"] >= 1
    assert by_name["alu"]["kind"] == "entity"


def test_query_instances_of_json(project: Path) -> None:
    payload = _json_out(
        CliRunner().invoke(
            main, ["query", "instances-of", "simple_counter", "--json", *db_args(project)]
        )
    )
    assert payload and all("confidence" in rec for rec in payload)

    empty = CliRunner().invoke(
        main, ["query", "instances-of", "no_such_unit", "--json", *db_args(project)]
    )
    assert empty.exit_code == 1
    assert _json_out(empty) == []


def test_query_unresolved_json(project: Path) -> None:
    payload = _json_out(
        CliRunner().invoke(main, ["query", "unresolved", "--json", *db_args(project)])
    )
    assert any(stub["referrers"] for stub in payload)


def test_metrics_limit_short_flag(project: Path) -> None:
    result = CliRunner().invoke(main, ["metrics", "-n", "1", *db_args(project)])
    assert result.exit_code == 0, result.output
    assert len([line for line in result.output.splitlines() if line]) == 2  # header + 1 row


def test_visualize_unknown_top_fails(project: Path, tmp_path: Path) -> None:
    out = tmp_path / "graph.html"
    result = CliRunner().invoke(
        main, ["visualize", "--top", "no_such_module", "-o", str(out), *db_args(project)]
    )
    assert result.exit_code != 0
    assert "module or entity 'no_such_module' not found" in result.output
    assert not out.exists()  # no silently empty page


def test_serve_http_warns_on_non_loopback_bind(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    class DummyServer:
        def run(self, **kwargs: object) -> None:
            calls.append(dict(kwargs))

    import hdl_kgraph.mcp

    seen: list[str | None] = []

    def fake_create_server(db_path: Path, *, token: str | None = None) -> DummyServer:
        seen.append(token)
        return DummyServer()

    monkeypatch.setattr(hdl_kgraph.mcp, "create_server", fake_create_server)

    public = CliRunner().invoke(main, ["serve", "--http", "0.0.0.0:8123", *db_args(project)])
    assert public.exit_code == 0, public.output
    assert "no authentication" in public.output
    assert calls[-1]["host"] == "0.0.0.0"
    assert seen[-1] is None  # no token configured

    loopback = CliRunner().invoke(main, ["serve", "--http", "127.0.0.1:8123", *db_args(project)])
    assert loopback.exit_code == 0, loopback.output
    assert "no authentication" not in loopback.output

    # With a token the non-loopback bind is authenticated, so the warning is
    # suppressed and the token is threaded into create_server (#69).
    tokened = CliRunner().invoke(
        main, ["serve", "--http", "0.0.0.0:8123", "--token", "s3cret", *db_args(project)]
    )
    assert tokened.exit_code == 0, tokened.output
    assert "no authentication" not in tokened.output
    assert seen[-1] == "s3cret"


def test_serve_http_ipv6_and_port_validation(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    class DummyServer:
        def run(self, **kwargs: object) -> None:
            calls.append(dict(kwargs))

    import hdl_kgraph.mcp

    monkeypatch.setattr(
        hdl_kgraph.mcp, "create_server", lambda db_path, *, token=None: DummyServer()
    )

    # IPv6 [host]:port parses (rpartition would have dropped the bracket); being
    # loopback with no token it binds [::1] and emits no exposure warning.
    ipv6 = CliRunner().invoke(main, ["serve", "--http", "[::1]:8123", *db_args(project)])
    assert ipv6.exit_code == 0, ipv6.output
    assert calls[-1]["host"] == "[::1]" and calls[-1]["port"] == 8123
    assert "no authentication" not in ipv6.output

    # Out-of-range / zero ports are now rejected (int() used to accept them).
    for bad in ("127.0.0.1:99999", "127.0.0.1:0", "[::1]:70000"):
        result = CliRunner().invoke(main, ["serve", "--http", bad, *db_args(project)])
        assert result.exit_code == 2, (bad, result.output)
        assert "HOST:PORT" in result.output
