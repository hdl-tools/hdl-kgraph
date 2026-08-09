"""SQLite round-trip tests."""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from hdl_kgraph.enrich.base import Discrepancy
from hdl_kgraph.graph.builder import build_graph
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import (
    SCHEMA_VERSION,
    FileMeta,
    SchemaVersionError,
    SqliteStore,
    StoredUnit,
)


def _normalize(attrs: dict) -> str:
    # Tuples become JSON arrays in storage; compare canonically.
    return json.dumps(attrs, sort_keys=True, default=list)


@pytest.fixture
def store(tmp_path: Path, fixtures_dir: Path) -> tuple[SqliteStore, object, list[FileMeta]]:
    parser = SystemVerilogParser()
    irs = [
        parser.parse(Path(p.name), p.read_text())
        for p in sorted(fixtures_dir.iterdir())
        if p.suffix in parser.suffixes
    ]
    graph = build_graph(irs)
    files = [
        FileMeta(
            path=ir.path,
            language=ir.nodes[0].language,  # the FILE node carries the language
            content_hash="0" * 64,
            size_bytes=123,
            parse_error_count=ir.parse_error_count,
            parse_errors=list(ir.parse_errors),
        )
        for ir in irs
    ]
    store = SqliteStore(tmp_path / ".hdl-kgraph" / "graph.db")
    store.save(graph, files, root=tmp_path)
    return store, graph, files


def test_round_trip_preserves_nodes_and_edges(store) -> None:
    sqlite_store, graph, files = store
    loaded, loaded_files, meta = sqlite_store.load()

    assert set(loaded.nodes) == set(graph.nodes)
    assert loaded.number_of_edges() == graph.number_of_edges()
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["root"]

    for node_id, data in graph.nodes(data=True):
        got = loaded.nodes[node_id]
        assert got["kind"] is data["kind"], node_id
        assert got["language"] is data["language"]
        assert got["line_span"] == data["line_span"]
        assert _normalize(got["attrs"]) == _normalize(data["attrs"])

    want_edges = sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in graph.edges(data=True)
    )
    got_edges = sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in loaded.edges(data=True)
    )
    assert got_edges == want_edges


def test_round_trip_preserves_file_meta(store) -> None:
    sqlite_store, _, files = store
    _, loaded_files, _ = sqlite_store.load()
    assert {f.path for f in loaded_files} == {f.path for f in files}
    by_path = {f.path: f for f in loaded_files}
    broken = by_path["broken.sv"]
    assert broken.parse_error_count > 0
    assert broken.parse_errors
    assert all(e.startswith("broken.sv:") for e in broken.parse_errors)
    assert broken.content_hash == "0" * 64
    assert broken.skipped_reason is None
    assert by_path["adder.v"].language is Language.VERILOG
    assert by_path["simple_counter.sv"].language is Language.SYSTEMVERILOG


def test_save_is_a_full_rewrite(store) -> None:
    sqlite_store, graph, files = store
    sqlite_store.save(graph, files, root=Path("."))
    loaded, loaded_files, _ = sqlite_store.load()
    assert loaded.number_of_edges() == graph.number_of_edges()
    assert len(loaded_files) == len(files)


def test_save_leaves_no_temp_file(store) -> None:
    sqlite_store, _, _ = store
    assert list(sqlite_store.db_path.parent.glob("*.tmp")) == []


def test_save_overwrites_stale_temp_file(store) -> None:
    sqlite_store, graph, files = store
    tmp = sqlite_store.db_path.with_name(sqlite_store.db_path.name + ".tmp")
    tmp.write_text("garbage left by a crashed build")
    sqlite_store.save(graph, files, root=Path("."))
    assert not tmp.exists()
    loaded, _, _ = sqlite_store.load()
    assert loaded.number_of_edges() == graph.number_of_edges()


@pytest.mark.skipif(sys.platform == "win32", reason="open handles block os.replace on Windows")
def test_save_succeeds_while_reader_holds_a_transaction(store) -> None:
    """A rewrite must not raise 'database is locked' under a concurrent
    reader (the MCP server reloading mid-`update`): the swap leaves the
    reader's snapshot untouched and the next load sees the new write."""
    sqlite_store, graph, files = store
    reader = sqlite3.connect(sqlite_store.db_path)
    try:
        reader.execute("BEGIN")
        before = reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        sqlite_store.save(graph, files, root=Path("."), options_hash="during-read")
        assert reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before
    finally:
        reader.close()
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "during-read"


def test_save_retries_swap_on_permission_error(store, monkeypatch) -> None:
    """The Windows case: os.replace fails while a reader holds the file open."""
    sqlite_store, graph, files = store
    real_replace, calls = os.replace, []

    def flaky(src: object, dst: object) -> None:
        calls.append((src, dst))
        if len(calls) < 3:
            raise PermissionError("file held open by a reader")
        real_replace(src, dst)

    monkeypatch.setattr("hdl_kgraph.storage.sqlite_store.os.replace", flaky)
    monkeypatch.setattr("hdl_kgraph.storage.sqlite_store.time.sleep", lambda _s: None)
    sqlite_store.save(graph, files, root=Path("."), options_hash="retried")
    assert len(calls) == 3
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "retried"


def test_schema_version_mismatch_raises(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="999"):
        sqlite_store.load()


def test_old_database_is_refused(store) -> None:
    """``load()`` never migrates: an older (v7) database is refused with the
    rebuild message, whatever ladder path ``migrate()`` might have taken."""
    assert SCHEMA_VERSION == "9"
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="hdl-kgraph build"):
        sqlite_store.load()


def test_units_and_options_hash_round_trip(store) -> None:
    sqlite_store, graph, files = store
    units = {
        "adder.v": StoredUnit(ir='{"path": "adder.v"}', macro_events="[]", included="[]"),
        "top.v": StoredUnit(
            ir='{"path": "top.v"}', macro_events='[{"op": "undef"}]', included='["defs.svh"]'
        ),
    }
    sqlite_store.save(graph, files, root=Path("."), units=units, options_hash="abc123")
    assert sqlite_store.load_units() == units
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "abc123"


def test_save_without_units_clears_stale_units(store) -> None:
    sqlite_store, graph, files = store
    units = {"adder.v": StoredUnit(ir="{}", macro_events="[]", included="[]")}
    sqlite_store.save(graph, files, root=Path("."), units=units)
    sqlite_store.save(graph, files, root=Path("."))
    assert sqlite_store.load_units() == {}


def test_load_units_checks_schema_version(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="'1'"):
        sqlite_store.load_units()


def test_kinds_rehydrate_as_enums(store) -> None:
    sqlite_store, _, _ = store
    loaded, _, _ = sqlite_store.load()
    kinds = {d["kind"] for _, d in loaded.nodes(data=True)}
    assert NodeKind.MODULE in kinds
    edge_kinds = {d["kind"] for _, _, d in loaded.edges(data=True)}
    assert EdgeKind.DECLARES in edge_kinds
    assert EdgeKind.INSTANTIATES in edge_kinds


def test_file_warnings_round_trip(store) -> None:
    sqlite_store, graph, files = store
    files = [
        FileMeta(
            path=f.path,
            language=f.language,
            content_hash=f.content_hash,
            size_bytes=f.size_bytes,
            parse_error_count=f.parse_error_count,
            warnings=['top.sv:1: cannot resolve `include "missing.svh"']
            if f.path == "broken.sv"
            else [],
        )
        for f in files
    ]
    sqlite_store.save(graph, files, root=Path("."))
    _, loaded_files, _ = sqlite_store.load()
    by_path = {f.path: f for f in loaded_files}
    assert by_path["broken.sv"].warnings == ['top.sv:1: cannot resolve `include "missing.svh"']
    assert by_path["adder.v"].warnings == []
    assert sqlite_store.load_file_warnings() == {
        "broken.sv": ['top.sv:1: cannot resolve `include "missing.svh"']
    }


def test_load_file_warnings_checks_schema_version(store) -> None:
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    with pytest.raises(SchemaVersionError, match="'1'"):
        sqlite_store.load_file_warnings()


# -- incremental persistence (issue #63) --------------------------------------


def _edge_multiset(g) -> list:
    """Sorted edge tuples *with* multiplicity (MultiDiGraph parallel edges)."""
    return sorted(
        (u, v, d["kind"].value, d["confidence"], _normalize(d["attrs"]))
        for u, v, d in g.edges(data=True)
    )


def _add_module(graph, node_id: str, name: str, **attrs) -> None:
    graph.add_node(
        node_id,
        kind=NodeKind.MODULE,
        name=name,
        qualified_name=name,
        file=f"{name}.sv",
        line_span=(1, 2),
        language=Language.SYSTEMVERILOG,
        attrs=attrs,
    )


def _node_rowids(db_path) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT id, rowid FROM nodes"))
    finally:
        conn.close()


def test_save_incremental_matches_full_save(store, tmp_path) -> None:
    """The headline contract: an incremental write loads identically to a full
    save() of the same graph, across add/delete/edit and parallel edges."""
    sqlite_store, graph, files = store  # already saved (the stored baseline)

    _add_module(graph, "added.sv::module:added", "added", x=1)
    victim = next(n for n, d in graph.nodes(data=True) if d["kind"] is NodeKind.PORT)
    graph.remove_node(victim)  # drops its incident edges too
    u, v, k, _ = next(iter(graph.edges(keys=True, data=True)))
    graph[u][v][k]["confidence"] = 0.42
    graph.add_edge(u, v, kind=EdgeKind.DECLARES, confidence=0.5, attrs={"dup": True})
    graph.add_edge(u, v, kind=EdgeKind.DECLARES, confidence=0.5, attrs={"dup": True})

    sqlite_store.save_incremental(graph, files, root=Path("."))
    inc, _, _ = sqlite_store.load()

    full = SqliteStore(tmp_path / "full" / "graph.db")
    full.save(graph, files, root=Path("."))
    ref, _, _ = full.load()

    assert set(inc.nodes) == set(ref.nodes)
    assert _edge_multiset(inc) == _edge_multiset(ref)
    for node_id, data in ref.nodes(data=True):
        assert _normalize(inc.nodes[node_id]["attrs"]) == _normalize(data["attrs"])


def test_save_incremental_touches_only_changed_node_rows(store) -> None:
    sqlite_store, graph, files = store
    before = _node_rowids(sqlite_store.db_path)
    target = next(iter(graph.nodes))
    graph.nodes[target]["attrs"] = {"touched": True}

    sqlite_store.save_incremental(graph, files, root=Path("."))

    after = _node_rowids(sqlite_store.db_path)
    for node_id, rowid in before.items():
        if node_id != target:
            assert after[node_id] == rowid, node_id  # untouched rows not rewritten
    assert sqlite_store.last_write_stats == {
        "nodes_upserted": 1,
        "nodes_deleted": 0,
        "edge_srcs_rewritten": 0,
    }


def test_save_incremental_bounds_edge_writes_by_src(store) -> None:
    sqlite_store, graph, files = store
    u, v, k, _ = next(iter(graph.edges(keys=True, data=True)))
    graph[u][v][k]["confidence"] = 0.33

    sqlite_store.save_incremental(graph, files, root=Path("."))

    assert sqlite_store.last_write_stats["edge_srcs_rewritten"] == 1
    loaded, _, _ = sqlite_store.load()
    assert any(abs(d["confidence"] - 0.33) < 1e-9 for _, _, d in loaded.edges(data=True))


def test_save_incremental_deletes_stale_nodes_and_edges(store) -> None:
    sqlite_store, graph, files = store
    victim = next(n for n, d in graph.nodes(data=True) if d["kind"] is NodeKind.MODULE)
    graph.remove_node(victim)

    sqlite_store.save_incremental(graph, files, root=Path("."))

    loaded, _, _ = sqlite_store.load()
    assert victim not in loaded.nodes
    assert all(victim not in (u, v) for u, v in loaded.edges())


def test_save_incremental_preserves_parallel_edges(store) -> None:
    sqlite_store, graph, files = store
    nodes = list(graph.nodes)
    u, v = nodes[0], nodes[1]
    for _ in range(2):
        graph.add_edge(u, v, kind=EdgeKind.DECLARES, confidence=0.5, attrs={"p": 1})

    sqlite_store.save_incremental(graph, files, root=Path("."))

    loaded, _, _ = sqlite_store.load()
    dup = [
        1 for a, b, d in loaded.edges(data=True) if a == u and b == v and d["attrs"].get("p") == 1
    ]
    assert len(dup) == 2  # multiplicity preserved (Counter, not set)


def test_save_incremental_refreshes_discrepancies(store) -> None:
    sqlite_store, graph, files = store
    disc = Discrepancy(kind="instance_count", backend="pyslang", detail="x")
    sqlite_store.save_incremental(graph, files, root=Path("."), discrepancies=[disc])
    assert sqlite_store.load_discrepancies() == [disc]
    sqlite_store.save_incremental(graph, files, root=Path("."), discrepancies=[])
    assert sqlite_store.load_discrepancies() == []


def test_save_incremental_falls_back_on_schema_mismatch(store) -> None:
    sqlite_store, graph, files = store
    conn = sqlite3.connect(sqlite_store.db_path)
    conn.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    _add_module(graph, "fallback.sv::module:fb", "fb")

    sqlite_store.save_incremental(graph, files, root=Path("."))  # must not raise

    loaded, _, meta = sqlite_store.load()
    assert meta["schema_version"] == SCHEMA_VERSION  # rewritten by the full save() fallback
    assert "fallback.sv::module:fb" in loaded.nodes


def test_foreign_file_is_refused_as_schema_error(tmp_path) -> None:
    """A non-SQLite file at the db path raises sqlite3.DatabaseError on read;
    it must be remapped to SchemaVersionError so callers rebuild instead of
    crashing."""
    db = tmp_path / ".hdl-kgraph" / "graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"this is plainly not a sqlite database\n" * 4)
    with pytest.raises(SchemaVersionError, match="not an hdl-kgraph database"):
        SqliteStore(db).load()


def test_save_incremental_falls_back_on_foreign_file(store) -> None:
    sqlite_store, graph, files = store
    sqlite_store.db_path.write_bytes(b"garbage where a database should be\n")
    _add_module(graph, "foreign.sv::module:fr", "fr")
    sqlite_store.save_incremental(graph, files, root=Path("."))  # must not raise
    loaded, _, _ = sqlite_store.load()
    assert "foreign.sv::module:fr" in loaded.nodes


def _downgrade_to_v7(db_path: Path) -> None:
    """Make a current (v8) database look like a genuine pre-migration v7 one:
    drop the summaries table, set the schema version back, and remove the
    ir_codec_version key (which v7 databases never carried)."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS summaries")
        conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
        conn.execute("DELETE FROM meta WHERE key = 'ir_codec_version'")


def test_migrate_v7_to_v8_in_place(store) -> None:
    """A registered, IR-compatible step upgrades in place — no full reparse."""
    assert SCHEMA_VERSION == "9"
    sqlite_store, _, _ = store
    _downgrade_to_v7(sqlite_store.db_path)

    assert sqlite_store.migrate() == "migrated"

    # The database is current again, the summaries table is back (empty, so the
    # reader falls back to on-the-fly computation), and load() no longer raises.
    _, _, meta = sqlite_store.load()
    assert meta["schema_version"] == SCHEMA_VERSION
    assert meta["ir_codec_version"]  # stamped by the migration
    assert sqlite_store.load_summary("clock_domains") is None


def test_migrate_v8_to_v9_drops_the_stale_clock_summary(store) -> None:
    """v8 payloads predate ``cdc_analysis``; the row must go, not be trusted.

    A retained v8 blob would report a collapsed design as ``complete`` once a
    reader defaults the missing status — the false clean bill #176 removes.
    """
    sqlite_store, _, _ = store
    sqlite_store.save_summaries({"clock_domains": json.dumps({"domains": []})})
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '8' WHERE key = 'schema_version'")

    assert sqlite_store.migrate() == "migrated"

    # Gone, so the reader recomputes it out-of-core with the new fields.
    assert sqlite_store.load_summary("clock_domains") is None
    _, _, meta = sqlite_store.load()
    assert meta["schema_version"] == SCHEMA_VERSION


def test_migrate_current_database_is_a_noop(store) -> None:
    sqlite_store, _, _ = store
    assert sqlite_store.migrate() == "current"


def test_migrate_absent_database(tmp_path) -> None:
    assert SqliteStore(tmp_path / "nope.db").migrate() == "absent"


def test_migrate_unregistered_version_requests_rebuild(store) -> None:
    """A version with no contiguous ladder path is left untouched for rebuild."""
    sqlite_store, _, _ = store
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("UPDATE meta SET value = '5' WHERE key = 'schema_version'")
    assert sqlite_store.migrate() == "rebuild"
    with pytest.raises(SchemaVersionError):
        sqlite_store.load()  # still refused; the caller rebuilds


def test_migrate_ir_codec_change_requests_rebuild(store) -> None:
    """An IR-encoding change can't be migrated in place even with a DDL path."""
    sqlite_store, _, _ = store
    _downgrade_to_v7(sqlite_store.db_path)
    with sqlite3.connect(sqlite_store.db_path) as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('ir_codec_version', '999')")
    assert sqlite_store.migrate() == "rebuild"


def test_migrate_foreign_file_requests_rebuild(tmp_path) -> None:
    db = tmp_path / ".hdl-kgraph" / "graph.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite database\n" * 4)
    assert SqliteStore(db).migrate() == "rebuild"


def test_ref_index_round_trips_and_writes_incrementally(store) -> None:
    from hdl_kgraph.graph.builder import RefRecord

    sqlite_store, graph, files = store
    recs = [
        RefRecord("a.sv", "a.sv::instance:u_b", EdgeKind.INSTANTIATES, "b", False),
        RefRecord("a.sv", "a.sv::process:p", EdgeKind.DRIVES, "clk", True),
        RefRecord("b.sv", "b.sv::module:b", EdgeKind.IMPORTS, "pkg", False),
    ]
    sqlite_store.save(graph, files, root=Path("."), ref_records=recs)
    assert sorted(sqlite_store.load_ref_index(), key=lambda r: (r.file, r.src_id)) == sorted(
        recs, key=lambda r: (r.file, r.src_id)
    )

    # An incremental write that drops a.sv's refs and adds c.sv's rewrites only
    # those files' ref rows, leaving b.sv's untouched.
    recs2 = [
        RefRecord("b.sv", "b.sv::module:b", EdgeKind.IMPORTS, "pkg", False),
        RefRecord("c.sv", "c.sv::instance:u_d", EdgeKind.INSTANTIATES, "d", False),
    ]
    sqlite_store.save_incremental(graph, files, root=Path("."), ref_records=recs2)
    assert {(r.file, r.target_name) for r in sqlite_store.load_ref_index()} == {
        ("b.sv", "pkg"),
        ("c.sv", "d"),
    }


def test_save_incremental_creates_database_when_missing(tmp_path) -> None:
    parser = SystemVerilogParser()
    graph = build_graph([parser.parse(Path("m.sv"), "module m; endmodule\n")])
    files = [FileMeta(path="m.sv", language=Language.SYSTEMVERILOG, content_hash="0", size_bytes=1)]
    store = SqliteStore(tmp_path / ".hdl-kgraph" / "graph.db")
    store.save_incremental(graph, files, root=tmp_path)  # no DB yet -> full save()
    loaded, _, _ = store.load()
    assert set(loaded.nodes) == set(graph.nodes)


@pytest.mark.skipif(sys.platform == "win32", reason="open handles block os.replace on Windows")
def test_save_incremental_succeeds_while_reader_holds_a_transaction(store) -> None:
    """The WAL no-block guarantee: an incremental write does not raise
    'database is locked' under a concurrent reader, and the reader's snapshot
    is untouched until its transaction ends."""
    sqlite_store, graph, files = store
    reader = sqlite3.connect(sqlite_store.db_path)
    try:
        reader.execute("BEGIN")
        before = reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        _add_module(graph, "reader.sv::module:z", "z")
        sqlite_store.save_incremental(graph, files, root=Path("."), options_hash="during-read")
        assert reader.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == before
    finally:
        reader.close()
    _, _, meta = sqlite_store.load()
    assert meta["options_hash"] == "during-read"


def test_save_incremental_leaves_no_wal_frames(store) -> None:
    sqlite_store, graph, files = store
    _add_module(graph, "wal.sv::module:w", "w")
    sqlite_store.save_incremental(graph, files, root=Path("."))
    wal = sqlite_store.db_path.with_name(sqlite_store.db_path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_full_save_after_incremental_leaves_clean_single_file(store) -> None:
    sqlite_store, graph, files = store
    sqlite_store.save_incremental(graph, files, root=Path("."))  # DB now in WAL mode
    sqlite_store.save(graph, files, root=Path("."))  # full rewrite must clean sidecars
    for suffix in ("-wal", "-shm"):
        assert not sqlite_store.db_path.with_name(sqlite_store.db_path.name + suffix).exists()
    loaded, _, _ = sqlite_store.load()
    assert loaded.number_of_edges() == graph.number_of_edges()
