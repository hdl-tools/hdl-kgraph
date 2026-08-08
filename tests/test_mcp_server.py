"""MCP server tests (M6): the ten tools over an in-memory fastmcp client.

Skipped entirely when fastmcp (the ``[mcp]`` extra) is not installed; the
underlying analysis functions have their own fastmcp-free tests.
"""

import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

fastmcp = pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402

from hdl_kgraph.mcp import create_server  # noqa: E402
from hdl_kgraph.pipeline import run_build  # noqa: E402
from hdl_kgraph.storage import sqlite_store as sqlite_store_module  # noqa: E402

EXPECTED_TOOLS = {
    "find_module",
    "get_hierarchy",
    "who_instantiates",
    "port_map",
    "impact_of_change",
    "clock_domains",
    "find_signal_drivers",
    "uvm_topology",
    "power_domains",
    "search_nodes",
}


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    root = tmp_path_factory.mktemp("mcp_project")
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    run_build(root)
    return root


@pytest.fixture(scope="module")
def server(project: Path):
    return create_server(project / ".hdl-kgraph" / "graph.db")


def _call(server: Any, tool: str, args: dict[str, Any] | None = None) -> Any:
    async def go() -> Any:
        async with Client(server) as client:
            result = await client.call_tool(tool, args or {})
            return result.data

    return asyncio.run(go())


def _list_tools(server: Any) -> set[str]:
    async def go() -> set[str]:
        async with Client(server) as client:
            return {t.name for t in await client.list_tools()}

    return asyncio.run(go())


def test_all_ten_tools_listed(server: Any) -> None:
    assert _list_tools(server) == EXPECTED_TOOLS


def test_find_module_enriched(server: Any) -> None:
    page = _call(server, "find_module", {"name": "simple_counter"})
    assert page["total"] == 1
    item = page["items"][0]
    assert item["kind"] == "module"
    assert item["port_count"] == 4
    assert item["parameter_count"] == 1


def test_find_signal_drivers_acceptance(server: Any) -> None:
    """The M6 acceptance question: what drives signal X in module Y."""
    page = _call(server, "find_signal_drivers", {"signal": "stage", "module": "df_top"})
    assert page["total"] >= 1
    assert all(r["module"] == "df_top" for r in page["items"])
    scoped_out = _call(server, "find_signal_drivers", {"signal": "o", "module": "df_top"})
    assert scoped_out["total"] == 0


def test_impact_of_change_acceptance(server: Any) -> None:
    """The M6 acceptance question: what breaks if this unit changes."""
    result = _call(server, "impact_of_change", {"target": "adder"})
    assert result["seed_count"] == 1
    assert result["summary"]["affected_units"] == result["total"]
    assert "top_positional" in [r["name"] for r in result["items"]]


def test_impact_unknown_target_is_tool_error(server: Any) -> None:
    with pytest.raises(Exception, match="matches no file or design unit"):
        _call(server, "impact_of_change", {"target": "no_such_thing"})


def test_pagination_envelope(server: Any) -> None:
    page = _call(server, "search_nodes", {"name": "*", "limit": 1})
    assert page["count"] == 1
    assert page["truncated"] is True
    assert page["total"] > 1
    next_page = _call(server, "search_nodes", {"name": "*", "limit": 1, "offset": 1})
    assert next_page["items"] != page["items"]


def test_search_nodes_bad_kind(server: Any) -> None:
    with pytest.raises(Exception, match="unknown node kind"):
        _call(server, "search_nodes", {"kinds": ["NOPE"]})


def test_get_hierarchy_tops_and_tree(server: Any) -> None:
    tops = _call(server, "get_hierarchy")
    assert "df_top" in [t["name"] for t in tops["tops"]]
    tree = _call(server, "get_hierarchy", {"top": "df_top", "depth": 1})
    assert tree["root"]["module_name"] == "df_top"
    assert [c["module_name"] for c in tree["root"]["children"]] == ["df_sub"]


def test_get_hierarchy_node_cap(server: Any) -> None:
    tree = _call(server, "get_hierarchy", {"top": "df_top", "max_nodes": 1})
    assert tree["root"]["children"] == []
    assert tree["root"]["truncated"] is True
    assert tree["nodes_omitted"] >= 1


def test_port_map_tool(server: Any) -> None:
    result = _call(server, "port_map", {"module": "adder", "instance": "u_adder"})
    (unit,) = result["units"]
    assert [p["name"] for p in unit["ports"]]
    assert unit["instances"][0]["instance_name"] == "u_adder"


def test_clock_domains_tool(server: Any) -> None:
    result = _call(server, "clock_domains")
    assert len(result["domains"]) >= 2  # two_clock_cdc.sv
    assert result["cdc_suspect_count"] >= 1


def test_uvm_topology_tool(server: Any) -> None:
    result = _call(server, "uvm_topology")
    assert any(c["role"] == "test" for c in result["components"])
    assert result["test_covers"]


def test_who_instantiates_tool(server: Any) -> None:
    page = _call(server, "who_instantiates", {"name": "adder"})
    assert page["total"] >= 1
    assert page["items"][0]["instance_name"] == "u_adder"


def test_stale_database_reloads(tmp_path: Path, fixtures_dir: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(fixtures_dir / "simple_counter.sv", root / "simple_counter.sv")
    run_build(root)
    server = create_server(root / ".hdl-kgraph" / "graph.db")
    assert _call(server, "find_module", {"name": "late_arrival"})["total"] == 0
    (root / "late_arrival.sv").write_text("module late_arrival; endmodule\n")
    run_build(root)  # rewrites the database under the running server
    assert _call(server, "find_module", {"name": "late_arrival"})["total"] == 1


def test_locked_database_is_a_clear_retriable_error(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool call against a genuinely locked database must surface a
    retriable message, not a raw sqlite3.OperationalError — and the next call
    after the lock clears must succeed (the failure is not cached).

    The database is WAL mode (so an ordinary writer never blocks readers); to
    force the locked path we hold an EXCLUSIVE *file* lock, which does block
    readers even under WAL."""
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(fixtures_dir / "simple_counter.sv", root / "simple_counter.sv")
    run_build(root)
    monkeypatch.setattr(sqlite_store_module, "_BUSY_TIMEOUT_MS", 100)
    server = create_server(root / ".hdl-kgraph" / "graph.db")
    blocker = sqlite3.connect(root / ".hdl-kgraph" / "graph.db")
    blocker.isolation_level = None
    try:
        blocker.execute("PRAGMA locking_mode = EXCLUSIVE")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE meta SET value = value WHERE key = 'root'")
        with pytest.raises(Exception, match="retry shortly"):
            _call(server, "find_module", {"name": "simple_counter"})
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert _call(server, "find_module", {"name": "simple_counter"})["total"] == 1


def test_create_server_token_configures_http_auth(project: Path) -> None:
    # A token wires a bearer-token verifier onto the server; no token leaves the
    # HTTP transport unauthenticated as before (#69). stdio ignores it either way.
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    db = project / ".hdl-kgraph" / "graph.db"
    assert create_server(db).auth is None
    authed = create_server(db, token="s3cret")
    assert isinstance(authed.auth, StaticTokenVerifier)
