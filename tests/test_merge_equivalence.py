"""Byte-identity gate for database merge (#131).

``merge(build(P0), build(P1), …)`` over partitions of a tree built with the
same ``--root`` must produce a graph whose signature equals a monolithic
``build`` of the whole tree. Mirrors ``test_incremental_equivalence.py``: the
``_signature`` helper is the ordered (nodes, edges) tuple used for byte-identity.

Plus the surrounding contract: cross-partition resolution at the right
confidence, overlap dedup + conflict policy, stub convergence, order
independence, the merged-DB update refusal, enriched/schema/codec gating, and
the VHDL library/filelist boundary (the corner that fails first if FILELIST /
``library`` adapter IRs are not reconstructed).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import networkx as nx
import pytest

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.merge import (
    MERGED_SENTINEL_PREFIX,
    MergeError,
    OnConflict,
    _AdapterUnion,
    run_merge,
)
from hdl_kgraph.pipeline import run_build, run_update
from hdl_kgraph.schema import Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore


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


def _graph(db: Path):
    graph, _, _ = SqliteStore(db).load()
    return graph


def _write_design(root: Path) -> None:
    """a; b instantiates a; c instantiates a and b (cross-references)."""
    (root / "a.sv").write_text("module a(input logic x);\nendmodule\n")
    (root / "b.sv").write_text("module b(input logic x);\n  a u_a(.x(x));\nendmodule\n")
    (root / "c.sv").write_text(
        "module c(input logic x);\n  a u_a(.x(x));\n  b u_b(.x(x));\nendmodule\n"
    )


def _build_partitions(root: Path) -> tuple[Path, Path, Path]:
    """Build P0={a,c}, P1={b} and a monolithic DB, all under the same root."""
    db0, db1, mono = root / "p0.db", root / "p1.db", root / "mono.db"
    run_build(root, db_path=db0, options=BuildOptions(sources=["a.sv", "c.sv"]))
    run_build(root, db_path=db1, options=BuildOptions(sources=["b.sv"]))
    run_build(root, db_path=mono)
    return db0, db1, mono


# -- headline equivalence -----------------------------------------------------


def test_headline_equivalence(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, mono = _build_partitions(tmp_path)
    out = tmp_path / "out.db"
    run_merge([db0, db1], out)

    merged_nodes, merged_edges = _signature(_graph(out))
    full_nodes, full_edges = _signature(_graph(mono))
    assert merged_nodes == full_nodes
    assert merged_edges == full_edges


def test_merged_options_hash_is_sentinel(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, _ = _build_partitions(tmp_path)
    out = tmp_path / "out.db"
    run_merge([db0, db1], out)
    _, _, meta = SqliteStore(out).load()
    assert meta["options_hash"].startswith(MERGED_SENTINEL_PREFIX)


# -- cross-partition resolution + confidence ----------------------------------


def test_cross_partition_resolution_confidence(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, mono = _build_partitions(tmp_path)
    out = tmp_path / "out.db"
    run_merge([db0, db1], out)
    merged = _graph(out)

    module_a = "a.sv::module:a"
    assert module_a in merged.nodes
    assert not merged.nodes[module_a]["attrs"].get("unresolved")
    # b (P1) instantiates a (P0): a resolved cross-partition unique match (0.8).
    inst_edges = [
        d
        for _, dst, d in merged.edges(data=True)
        if dst == module_a and d["kind"].value == "instantiates"
    ]
    assert inst_edges, "expected INSTANTIATES edges into module a"
    assert all(d["confidence"] == 0.8 for d in inst_edges)
    # Same edges, same confidence as the monolithic build.
    full = _graph(mono)
    full_inst = sorted(
        (u, v, d["confidence"])
        for u, v, d in full.edges(data=True)
        if v == module_a and d["kind"].value == "instantiates"
    )
    merged_inst = sorted(
        (u, v, d["confidence"])
        for u, v, d in merged.edges(data=True)
        if v == module_a and d["kind"].value == "instantiates"
    )
    assert merged_inst == full_inst


# -- overlap dedup + conflict policy ------------------------------------------


def test_overlap_dedup_same_hash(tmp_path: Path) -> None:
    """A file present in both partitions with identical content dedups cleanly."""
    _write_design(tmp_path)
    db0, db1, mono = _build_partitions(tmp_path)
    # Rebuild db1 to also include a.sv (identical content to db0's a.sv).
    run_build(tmp_path, db_path=db1, options=BuildOptions(sources=["a.sv", "b.sv"]))
    out = tmp_path / "out.db"
    report = run_merge([db0, db1], out)
    assert report.conflicts_resolved == []
    assert _signature(_graph(out)) == _signature(_graph(mono))


def _build_two_versions(root: Path) -> tuple[Path, Path]:
    """Two DBs under the same root holding different content for a.sv."""
    db_first, db_second = root / "first.db", root / "second.db"
    (root / "a.sv").write_text("module a(input logic x);\nendmodule\n")
    run_build(root, db_path=db_first, options=BuildOptions(sources=["a.sv"]))
    (root / "a.sv").write_text("module a(input logic x);\n  wire extra;\nendmodule\n")
    run_build(root, db_path=db_second, options=BuildOptions(sources=["a.sv"]))
    return db_first, db_second


def test_conflict_error(tmp_path: Path) -> None:
    db_first, db_second = _build_two_versions(tmp_path)
    out = tmp_path / "out.db"
    with pytest.raises(MergeError, match="a.sv"):
        run_merge([db_first, db_second], out, OnConflict.ERROR)


def test_conflict_first_keeps_earlier(tmp_path: Path) -> None:
    db_first, db_second = _build_two_versions(tmp_path)
    # A reference monolithic build of the *first* version.
    (tmp_path / "a.sv").write_text("module a(input logic x);\nendmodule\n")
    ref = tmp_path / "ref_first.db"
    run_build(tmp_path, db_path=ref, options=BuildOptions(sources=["a.sv"]))

    out = tmp_path / "out.db"
    report = run_merge([db_first, db_second], out, OnConflict.FIRST)
    assert any("a.sv" in note for note in report.conflicts_resolved)
    assert _signature(_graph(out)) == _signature(_graph(ref))


def test_conflict_last_keeps_later(tmp_path: Path) -> None:
    db_first, db_second = _build_two_versions(tmp_path)
    # The current a.sv on disk is already the *second* version.
    ref = tmp_path / "ref_last.db"
    run_build(tmp_path, db_path=ref, options=BuildOptions(sources=["a.sv"]))

    out = tmp_path / "out.db"
    report = run_merge([db_first, db_second], out, OnConflict.LAST)
    assert any("a.sv" in note for note in report.conflicts_resolved)
    assert _signature(_graph(out)) == _signature(_graph(ref))


# -- stub convergence ---------------------------------------------------------


def test_stub_convergence_across_partitions(tmp_path: Path) -> None:
    """An undefined module referenced from both partitions shares one stub."""
    (tmp_path / "a.sv").write_text("module a(input logic x);\n  ghost u_g();\nendmodule\n")
    (tmp_path / "b.sv").write_text("module b(input logic x);\n  ghost u_g();\nendmodule\n")
    db0, db1 = tmp_path / "p0.db", tmp_path / "p1.db"
    run_build(tmp_path, db_path=db0, options=BuildOptions(sources=["a.sv"]))
    run_build(tmp_path, db_path=db1, options=BuildOptions(sources=["b.sv"]))
    out = tmp_path / "out.db"
    run_merge([db0, db1], out)
    merged = _graph(out)

    stub = "unresolved:module:ghost"
    assert stub in merged.nodes
    assert sum(1 for n in merged.nodes if n == stub) == 1
    referrers = {u for u, v, d in merged.edges(data=True) if v == stub}
    assert len(referrers) == 2  # both a's and b's instance reach the one stub


# -- order independence -------------------------------------------------------


def test_order_independence(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, _ = _build_partitions(tmp_path)
    out_ab, out_ba = tmp_path / "ab.db", tmp_path / "ba.db"
    run_merge([db0, db1], out_ab)
    run_merge([db1, db0], out_ba)
    assert _signature(_graph(out_ab)) == _signature(_graph(out_ba))


# -- merged DB refuses update -------------------------------------------------


def test_merged_db_refuses_update(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, _ = _build_partitions(tmp_path)
    out = tmp_path / "out.db"
    run_merge([db0, db1], out)
    report = run_update(tmp_path, db_path=out)
    assert report.full_rebuild_reason is not None
    assert "merge" in report.full_rebuild_reason


# -- gating: enriched / schema / codec ----------------------------------------


def _exec(db: Path, sql: str, params: tuple = ()) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def test_enriched_source_refused(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, _ = _build_partitions(tmp_path)
    # Simulate an enriched build: a recorded discrepancy.
    _exec(
        db0,
        "INSERT INTO discrepancies (kind, backend, detail) VALUES (?, ?, ?)",
        ("instance_count", "slang", "elaboration disagreed"),
    )
    out = tmp_path / "out.db"
    with pytest.raises(MergeError, match="enriched"):
        run_merge([db0, db1], out)


def test_codec_version_gate(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, _ = _build_partitions(tmp_path)
    _exec(db0, "UPDATE meta SET value = '999' WHERE key = 'ir_codec_version'")
    out = tmp_path / "out.db"
    with pytest.raises(MergeError, match="IR codec version"):
        run_merge([db0, db1], out)


def test_schema_version_gate(tmp_path: Path) -> None:
    _write_design(tmp_path)
    db0, db1, _ = _build_partitions(tmp_path)
    _exec(db0, "UPDATE meta SET value = '999' WHERE key = 'schema_version'")
    out = tmp_path / "out.db"
    with pytest.raises(MergeError, match="schema version"):
        run_merge([db0, db1], out)


def test_root_mismatch_refused(tmp_path: Path) -> None:
    a = tmp_path / "blockA"
    b = tmp_path / "blockB"
    a.mkdir()
    b.mkdir()
    (a / "a.sv").write_text("module a; endmodule\n")
    (b / "b.sv").write_text("module b; endmodule\n")
    db0, db1 = tmp_path / "p0.db", tmp_path / "p1.db"
    run_build(a, db_path=db0)
    run_build(b, db_path=db1)
    with pytest.raises(MergeError, match="root mismatch"):
        run_merge([db0, db1], tmp_path / "out.db")


# -- VHDL library / filelist boundary -----------------------------------------


def _write_vhdl(root: Path) -> None:
    (root / "pkg.vhd").write_text(
        "library ieee;\npackage mypkg is\n  constant K : integer := 4;\nend package;\n"
    )
    (root / "ent.vhd").write_text(
        "library work;\nuse work.mypkg.all;\n"
        "entity ent is end entity;\n"
        "architecture rtl of ent is begin end architecture;\n"
    )


def test_vhdl_filelist_boundary_equivalence(tmp_path: Path) -> None:
    """FILELIST and VHDL LIBRARY adapter nodes survive a merge byte-identically."""
    _write_vhdl(tmp_path)
    (tmp_path / "f0.f").write_text("pkg.vhd\n")
    (tmp_path / "f1.f").write_text("ent.vhd\n")
    db0, db1, mono = tmp_path / "p0.db", tmp_path / "p1.db", tmp_path / "mono.db"
    run_build(tmp_path, db_path=db0, options=BuildOptions(filelists=[tmp_path / "f0.f"]))
    run_build(tmp_path, db_path=db1, options=BuildOptions(filelists=[tmp_path / "f1.f"]))
    # The monolithic comparison build uses the *same* two filelists, so both
    # sides carry the same pair of FILELIST nodes.
    run_build(
        tmp_path,
        db_path=mono,
        options=BuildOptions(filelists=[tmp_path / "f0.f", tmp_path / "f1.f"]),
    )
    out = tmp_path / "out.db"
    run_merge([db0, db1], out)

    merged = _graph(out)
    # The adapter nodes are present (the trap-2 corner).
    assert any(d["kind"].value == "filelist" for _, d in merged.nodes(data=True))
    assert any(d["kind"].value == "library" for _, d in merged.nodes(data=True))
    assert _signature(merged) == _signature(_graph(mono))


def test_vhdl_filelist_divergent_inputs_refused(tmp_path: Path) -> None:
    """Two sources sharing a filelist relpath but with divergent content (here a
    differing ``+define``, which also changes the FILELIST node's attrs) are an
    inconsistent-inputs conflict and are refused under the same root."""
    _write_vhdl(tmp_path)
    db0, db1 = tmp_path / "p0.db", tmp_path / "p1.db"
    fl = tmp_path / "f.f"
    fl.write_text("+define+FOO=1\npkg.vhd\n")
    run_build(tmp_path, db_path=db0, options=BuildOptions(filelists=[fl]))
    fl.write_text("+define+FOO=2\npkg.vhd\n")
    run_build(tmp_path, db_path=db1, options=BuildOptions(filelists=[fl]))
    with pytest.raises(MergeError, match="f.f"):
        run_merge([db0, db1], tmp_path / "out.db")


def _adapter_filelist_graph(defines: dict[str, str]) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    graph.add_node(
        "filelist:f.f",
        kind=NodeKind.FILELIST,
        name="f.f",
        qualified_name="f.f",
        file="f.f",
        line_span=(0, 0),
        language=Language.UNKNOWN,
        attrs={"defines": defines},
    )
    return graph


def test_adapter_union_rejects_divergent_node_attrs() -> None:
    """The adapter guard: one FILELIST id with different attrs across sources."""
    union = _AdapterUnion()
    union.absorb(_adapter_filelist_graph({"FOO": "1"}), Path("a.db"))
    with pytest.raises(MergeError, match="conflicting filelist"):
        union.absorb(_adapter_filelist_graph({"FOO": "2"}), Path("b.db"))
