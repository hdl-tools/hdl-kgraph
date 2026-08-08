"""Tests for the content-free incremental-link locality metric (``bench-link``)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.linkbench import BENCH_LINK_SCHEMA, link_locality
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.storage.sqlite_store import SqliteStore

_SECRETS = ["top", "mid", "leaf", "my_pkg", "common", "u_mid", "u_leaf", "WIDTH", ".sv"]


def _chain_project(tmp: Path) -> Path:
    (tmp / "defs.svh").write_text("`define WIDTH 8\n")
    (tmp / "leaf.sv").write_text(
        '`include "defs.svh"\n'
        "module leaf(input logic [`WIDTH-1:0] a, output logic [`WIDTH-1:0] y);\n"
        "  assign y = a;\nendmodule\n"
    )
    (tmp / "my_pkg.sv").write_text("package my_pkg;\n  localparam int K = 4;\nendpackage\n")
    (tmp / "mid.sv").write_text(
        "module mid(input logic [7:0] a, output logic [7:0] y);\n"
        "  import my_pkg::*;\n  leaf u_leaf(.a(a), .y(y));\nendmodule\n"
    )
    (tmp / "top.sv").write_text(
        "module top(input logic [7:0] a, output logic [7:0] y);\n"
        "  mid u_mid(.a(a), .y(y));\nendmodule\n"
    )
    run_build(tmp)
    return default_db_path(tmp)


def _hub_project(tmp: Path) -> Path:
    (tmp / "common.sv").write_text(
        "module common(input logic a, output logic y);\n  assign y = a;\nendmodule\n"
    )
    for i in range(3):
        (tmp / f"p{i}.sv").write_text(
            f"module p{i}(input logic a, output logic y);\n  common u_c(.a(a), .y(y));\nendmodule\n"
        )
    run_build(tmp)
    return default_db_path(tmp)


def test_totals_match_db(tmp_path: Path) -> None:
    db = _chain_project(tmp_path)
    report = link_locality(db)
    assert report["schema"] == BENCH_LINK_SCHEMA
    graph, _f, _m = SqliteStore(db).load()
    assert report["totals"]["nodes"] == graph.number_of_nodes()
    assert report["totals"]["edges"] == graph.number_of_edges()
    assert report["totals"]["refs"] == len(SqliteStore(db).load_ref_index())
    assert report["full_relink_refs"] == report["totals"]["refs"]


def test_locality_ratio_bounds(tmp_path: Path) -> None:
    db = _chain_project(tmp_path)
    lr = link_locality(db)["locality_ratio"]
    assert 0.0 <= lr["p50"] <= lr["p90"] <= lr["max"] <= 1.0
    # a single-file edit must not re-resolve the entire design's refs here
    assert lr["p50"] < 1.0


def test_hub_edit_ripples_more_than_median(tmp_path: Path) -> None:
    # editing a widely-instantiated module re-resolves more than a typical edit
    db = _hub_project(tmp_path)
    rr = link_locality(db)["reresolved_refs"]
    assert rr["max"] > rr["p50"]


def test_bench_link_is_content_free(tmp_path: Path) -> None:
    db = _chain_project(tmp_path)
    blob = json.dumps(link_locality(db))
    for ident in _SECRETS:
        assert ident not in blob, f"identifier {ident!r} leaked into the bench-link report"


def test_bench_link_cli_json(tmp_path: Path) -> None:
    db = _chain_project(tmp_path)
    result = CliRunner().invoke(main, ["bench-link", "--db", str(db), "--json"])
    assert result.exit_code == 0
    report = json.loads(result.output)
    assert report["schema"] == BENCH_LINK_SCHEMA
    for ident in _SECRETS:
        assert ident not in result.output


def test_bench_link_cli_text(tmp_path: Path) -> None:
    db = _chain_project(tmp_path)
    result = CliRunner().invoke(main, ["bench-link", "--db", str(db)])
    assert result.exit_code == 0
    assert "bench-link" in result.output
    assert "locality ratio" in result.output
    for ident in _SECRETS:
        assert ident not in result.output


def test_bench_link_sample(tmp_path: Path) -> None:
    db = _hub_project(tmp_path)
    report = link_locality(db, sample=2)
    assert report["totals"]["files"] == 2
