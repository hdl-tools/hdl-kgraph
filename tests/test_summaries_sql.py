"""Parity tests for the bounded SQL-native summaries (#128 v2).

``storage/summaries.clock_summary_sql`` / ``uvm_summary_sql`` must be byte-identical
to the NetworkX oracles ``graph/summary.clock_summary`` / ``uvm_summary`` — that
equivalence is the whole contract (it lets the out-of-core fallback replace a full
graph load). These tests pin it on the fixtures and exercise the ``GraphQuery`` seam
end-to-end.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hdl_kgraph.graph import clocks, summary
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.storage.query import GraphQuery
from hdl_kgraph.storage.sqlite_store import SqliteStore
from hdl_kgraph.storage.summaries import (
    clock_summary_sql,
    power_summary_sql,
    reset_summary_sql,
    uvm_summary_sql,
)

# Fixture sets that exercise clocks/CDC, incl. cross-language net aliasing.
_FIXTURE_SETS = [
    ["two_clock_cdc.sv"],
    ["dataflow.sv"],
    ["two_clock_cdc.sv", "dataflow.sv", "dataflow.vhd"],
]


def _build(tmp_path: Path, fixtures_dir: Path, names: list[str]) -> Path:
    for name in names:
        (tmp_path / name).write_text((fixtures_dir / name).read_text())
    run_build(tmp_path)
    return default_db_path(tmp_path)


@pytest.mark.parametrize("names", _FIXTURE_SETS, ids=lambda n: "+".join(n))
def test_sql_clock_summary_matches_oracle(
    tmp_path: Path, fixtures_dir: Path, names: list[str]
) -> None:
    db = _build(tmp_path, fixtures_dir, names)
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.clock_summary(graph)
    with SqliteStore(db)._connect() as conn:
        sql = clock_summary_sql(conn)
    assert sql == oracle  # byte-identical: domains, cdc_suspect_count, cdc_suspects


@pytest.mark.parametrize("names", _FIXTURE_SETS, ids=lambda n: "+".join(n))
def test_sql_reset_summary_matches_oracle(
    tmp_path: Path, fixtures_dir: Path, names: list[str]
) -> None:
    db = _build(tmp_path, fixtures_dir, names)
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.jsonable(clocks.reset_tree(graph))
    with SqliteStore(db)._connect() as conn:
        sql = reset_summary_sql(conn)
    assert sql == oracle  # byte-identical reset groups (alias-merged), via SQL


def test_two_clock_fixture_shape(tmp_path: Path, fixtures_dir: Path) -> None:
    # Sanity: the canonical fixture is two domains + exactly one CDC suspect.
    db = _build(tmp_path, fixtures_dir, ["two_clock_cdc.sv"])
    with SqliteStore(db)._connect() as conn:
        sql = clock_summary_sql(conn)
    assert len(sql["domains"]) == 2
    assert sql["cdc_suspect_count"] == 1


def test_graphquery_falls_back_to_sql_without_summary(tmp_path: Path, fixtures_dir: Path) -> None:
    # Drop the persisted summary so GraphQuery takes the out-of-core SQL path
    # (simulates a pre-v8 / migrated database), and assert it equals the oracle.
    db = _build(tmp_path, fixtures_dir, ["two_clock_cdc.sv", "dataflow.vhd"])
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.clock_summary(graph)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM summaries WHERE name = 'clock_domains'")
    conn.commit()
    conn.close()

    assert SqliteStore(db).load_summary("clock_domains") is None  # fallback armed
    assert GraphQuery(db).clock_domains() == oracle


def test_graphquery_prefers_persisted_summary(tmp_path: Path, fixtures_dir: Path) -> None:
    # With the summary present, GraphQuery serves it directly and still matches.
    db = _build(tmp_path, fixtures_dir, ["two_clock_cdc.sv"])
    graph, _f, _m = SqliteStore(db).load()
    assert GraphQuery(db).clock_domains() == summary.clock_summary(graph)


# --- UVM topology -----------------------------------------------------------

# Fixture sets exercising the EXTENDS-chain role classification + TEST_COVERS.
_UVM_FIXTURE_SETS = [
    ["uvm_tb.sv", "verif_constructs.sv"],
    ["ext_uvm.sv"],
]


@pytest.mark.parametrize("names", _UVM_FIXTURE_SETS, ids=lambda n: "+".join(n))
def test_sql_uvm_summary_matches_oracle(
    tmp_path: Path, fixtures_dir: Path, names: list[str]
) -> None:
    """The bounded subgraph scan equals the NetworkX UVM oracle, byte for byte."""
    db = _build(tmp_path, fixtures_dir, names)
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.uvm_summary(graph)
    with SqliteStore(db)._connect() as conn:
        sql = uvm_summary_sql(conn)
    assert sql == oracle  # byte-identical: components + test_covers


def test_graphquery_falls_back_to_sql_uvm_without_summary(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """With the persisted UVM summary deleted, GraphQuery serves it via the bounded
    subgraph fallback (no full load) and still equals the oracle."""
    # Drop the persisted summary so GraphQuery takes the out-of-core subgraph path.
    db = _build(tmp_path, fixtures_dir, ["uvm_tb.sv", "verif_constructs.sv"])
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.uvm_summary(graph)

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM summaries WHERE name = 'uvm_topology'")
    conn.commit()
    conn.close()

    assert SqliteStore(db).load_summary("uvm_topology") is None  # fallback armed
    assert GraphQuery(db).uvm_topology() == oracle


# --- UPF power domains (M10 second wedge) -----------------------------------


def _build_upf(tmp_path: Path, fixtures_dir: Path) -> Path:
    """The counter design plus its UPF, flattened into one build root."""
    for name in ("top.v", "simple_counter.sv"):
        (tmp_path / name).write_text((fixtures_dir / name).read_text())
    (tmp_path / "power.upf").write_text((fixtures_dir / "upf" / "power.upf").read_text())
    run_build(tmp_path)
    return default_db_path(tmp_path)


def test_sql_power_summary_matches_oracle(tmp_path: Path, fixtures_dir: Path) -> None:
    """The bounded power-domain subgraph scan equals the NetworkX oracle, byte for byte."""
    db = _build_upf(tmp_path, fixtures_dir)
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.power_summary(graph)
    with SqliteStore(db)._connect() as conn:
        sql = power_summary_sql(conn)
    assert sql == oracle  # byte-identical: domain_count, isolated_count, domains
    assert sql["domain_count"] == 2 and sql["isolated_count"] == 1


def test_graphquery_falls_back_to_sql_power_without_summary(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """With the persisted power summary deleted, GraphQuery serves it via the bounded
    subgraph fallback (no full load) and still equals the oracle."""
    db = _build_upf(tmp_path, fixtures_dir)
    graph, _f, _m = SqliteStore(db).load()
    oracle = summary.power_summary(graph)
    assert SqliteStore(db).load_summary("power_domains") is not None  # actually persisted

    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM summaries WHERE name = 'power_domains'")
    conn.commit()
    conn.close()

    assert SqliteStore(db).load_summary("power_domains") is None  # fallback armed
    assert GraphQuery(db).power_domains() == oracle
