"""Subtree caching: cached rebuild equals a full monolithic rebuild (#132).

Subtree caching is the merge machinery (#131) viewed incrementally: keep each
block's database, rebuild only the block that changed, and re-merge — reusing
the unchanged blocks' cached per-file IRs instead of re-parsing them. These
tests are the correctness gate: after a block changes and is rebuilt in
isolation, the re-merged graph must be byte-identical (``_signature``) to a
fresh monolithic build of the same final tree, and the unchanged block's
cached database must be reused untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from hdl_kgraph.config import BuildOptions
from hdl_kgraph.merge import run_merge
from hdl_kgraph.pipeline import run_build
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
    return SqliteStore(db).load()[0]


def _write_blocks(root: Path) -> None:
    """Two blocks under one root: block A defines a/c, block B defines b.

    b (block B) instantiates a (block A); c (block A) instantiates a and b —
    so a cached rebuild has to keep resolving across the block boundary.
    """
    (root / "a.sv").write_text("module a(input logic x);\nendmodule\n")
    (root / "b.sv").write_text("module b(input logic x);\n  a u_a(.x(x));\nendmodule\n")
    (root / "c.sv").write_text(
        "module c(input logic x);\n  a u_a(.x(x));\n  b u_b(.x(x));\nendmodule\n"
    )


def _build_blocks(root: Path) -> tuple[Path, Path]:
    """Build block A={a,c} and block B={b} into their own cached databases."""
    db_a, db_b = root / "blockA.db", root / "blockB.db"
    run_build(root, db_path=db_a, options=BuildOptions(sources=["a.sv", "c.sv"]))
    run_build(root, db_path=db_b, options=BuildOptions(sources=["b.sv"]))
    return db_a, db_b


def test_cached_rebuild_equals_full_rebuild(tmp_path: Path) -> None:
    """Change one block, rebuild only it, re-merge -> equals a monolithic build."""
    _write_blocks(tmp_path)
    db_a, db_b = _build_blocks(tmp_path)
    run_merge([db_a, db_b], tmp_path / "soc.db")

    # Edit a block-A source; rebuild ONLY block A's cached database.
    (tmp_path / "c.sv").write_text(
        "module c(input logic x);\n  a u_a(.x(x));\n  b u_b(.x(x));\n  wire extra;\nendmodule\n"
    )
    rebuilt = run_build(tmp_path, db_path=db_a, options=BuildOptions(sources=["a.sv", "c.sv"]))
    assert rebuilt.parsed_files == 2  # only block A's two files were re-parsed

    # Re-merge the freshly rebuilt block A with block B's *cached* database.
    run_merge([db_a, db_b], tmp_path / "soc2.db")

    # A fresh monolithic build of the edited tree is the reference.
    run_build(tmp_path, db_path=tmp_path / "mono.db")
    assert _signature(_graph(tmp_path / "soc2.db")) == _signature(_graph(tmp_path / "mono.db"))


def test_unchanged_block_db_reused_untouched(tmp_path: Path) -> None:
    """Rebuilding the changed block must not touch the sibling's cached DB."""
    _write_blocks(tmp_path)
    db_a, db_b = _build_blocks(tmp_path)
    run_merge([db_a, db_b], tmp_path / "soc.db")

    before = db_b.read_bytes()
    (tmp_path / "a.sv").write_text("module a(input logic x);\n  wire extra;\nendmodule\n")
    run_build(tmp_path, db_path=db_a, options=BuildOptions(sources=["a.sv", "c.sv"]))
    run_merge([db_a, db_b], tmp_path / "soc2.db")

    assert db_b.read_bytes() == before  # block B's cached artifact reused as-is


def test_cross_block_edit_still_resolves(tmp_path: Path) -> None:
    """Renaming a module that a sibling instantiates re-resolves after re-merge."""
    _write_blocks(tmp_path)
    db_a, db_b = _build_blocks(tmp_path)

    # Rename module a -> a2 in block A and update its references; block B still
    # instantiates `a`, which must now become an unresolved stub in both the
    # cached re-merge and a monolithic build (identical signatures).
    (tmp_path / "a.sv").write_text("module a2(input logic x);\nendmodule\n")
    (tmp_path / "c.sv").write_text(
        "module c(input logic x);\n  a2 u_a(.x(x));\n  b u_b(.x(x));\nendmodule\n"
    )
    run_build(tmp_path, db_path=db_a, options=BuildOptions(sources=["a.sv", "c.sv"]))
    run_merge([db_a, db_b], tmp_path / "soc2.db")

    run_build(tmp_path, db_path=tmp_path / "mono.db")
    assert _signature(_graph(tmp_path / "soc2.db")) == _signature(_graph(tmp_path / "mono.db"))
