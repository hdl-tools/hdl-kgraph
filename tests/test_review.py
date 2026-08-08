"""Tests for the content-free ``review`` digest (``hdl-kgraph review``)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.review import REVIEW_SCHEMA, build_review_digest
from hdl_kgraph.storage.sqlite_store import SqliteStore

# Identifiers from two_clock_cdc.sv that must NEVER appear in a content-free digest.
_SECRETS = [
    "two_clock_top",
    "cdc_child",
    "clk_a",
    "clk_b",
    "rst_n",
    "data_a",
    "data_b",
    "out_b",
    "child_q",
    "two_clock_cdc",
]


def _build(tmp_path: Path, fixtures_dir: Path) -> Path:
    (tmp_path / "two_clock_cdc.sv").write_text((fixtures_dir / "two_clock_cdc.sv").read_text())
    run_build(tmp_path)
    return default_db_path(tmp_path)


def _digest(db: Path, *, with_metrics: bool = True) -> dict:
    store = SqliteStore(db)
    graph, files, meta = store.load()
    return build_review_digest(
        graph,
        files,
        meta,
        db_bytes=db.stat().st_size,
        clock_summary_payload=store.load_summary("clock_domains"),
        uvm_summary_payload=store.load_summary("uvm_topology"),
        with_metrics=with_metrics,
    )


def test_review_schema_and_counts(tmp_path: Path, fixtures_dir: Path) -> None:
    db = _build(tmp_path, fixtures_dir)
    graph, _f, _m = SqliteStore(db).load()
    digest = _digest(db)
    assert digest["schema"] == REVIEW_SCHEMA
    assert set(digest) >= {"meta", "corpus", "graph", "link_quality", "analyses", "timings_s"}
    assert digest["graph"]["node_count"] == graph.number_of_nodes()
    assert digest["graph"]["edge_count"] == graph.number_of_edges()
    # the two-clock fixture: 2 domains, 1 CDC crossing
    assert digest["analyses"]["clock_domains"]["count"] == 2
    assert digest["analyses"]["cdc"]["suspect_count"] == 1
    assert digest["analyses"]["metrics"]["module_count"] >= 2  # --metrics path
    assert digest["meta"]["tool_version"]
    assert "root" not in digest["meta"]  # filesystem path deliberately omitted
    # build_stats was persisted -> timings present
    assert digest["timings_s"] is not None
    assert set(digest["timings_s"]) >= {"discover_s", "parse_s", "link_s", "persist_s"}


def test_review_is_content_free(tmp_path: Path, fixtures_dir: Path) -> None:
    db = _build(tmp_path, fixtures_dir)
    blob = json.dumps(_digest(db))
    for ident in _SECRETS:
        assert ident not in blob, f"identifier {ident!r} leaked into the review digest"
    assert ".sv" not in blob  # no filenames
    assert str(tmp_path) not in blob  # no paths


def test_review_timings_null_without_build_stats(tmp_path: Path, fixtures_dir: Path) -> None:
    db = _build(tmp_path, fixtures_dir)
    # Simulate a DB built before build_stats existed (back-compat) — also exercises
    # the summary-recompute fallback by passing no persisted payloads.
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM meta WHERE key = 'build_stats'")
    conn.commit()
    conn.close()
    graph, files, meta = SqliteStore(db).load()
    digest = build_review_digest(graph, files, meta)  # no payloads -> recompute summaries
    assert digest["timings_s"] is None
    assert digest["meta"]["enriched"] is None
    assert digest["analyses"]["clock_domains"]["count"] == 2  # recompute path matches


def test_review_cli_json_is_content_free(tmp_path: Path, fixtures_dir: Path) -> None:
    db = _build(tmp_path, fixtures_dir)
    result = CliRunner().invoke(main, ["review", "--db", str(db), "--json"])
    assert result.exit_code == 0
    digest = json.loads(result.output)
    assert digest["schema"] == REVIEW_SCHEMA
    for ident in _SECRETS:
        assert ident not in result.output
    assert ".sv" not in result.output  # no filenames via the CLI path either
    assert str(tmp_path) not in result.output  # no paths
