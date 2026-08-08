"""Parity tests for the bounded query layer (:mod:`hdl_kgraph.storage.query`).

The contract: every :class:`GraphQuery` method returns a result byte-identical
to running the corresponding full-graph analysis (the same code the MCP server
used before query push-down). These tests build one graph from the whole
fixture corpus and compare the two paths over *every* unit/signal name, so a
hydration gap (a missing edge or boundary-crossing climb) shows up as a diff.

No fastmcp here — ``GraphQuery`` and the ``_impl`` helpers are import-light.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import networkx as nx
import pytest

from hdl_kgraph.graph import analysis
from hdl_kgraph.mcp import server as srv
from hdl_kgraph.pipeline import run_build
from hdl_kgraph.schema import Language, NodeKind
from hdl_kgraph.storage.query import GraphQuery
from hdl_kgraph.storage.sqlite_store import FileMeta, SqliteStore


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path) -> Path:
    root = tmp_path_factory.mktemp("query_project")
    for path in fixtures_dir.iterdir():
        if path.is_file():
            shutil.copy(path, root / path.name)
    run_build(root)
    return root / ".hdl-kgraph" / "graph.db"


@pytest.fixture(scope="module")
def loaded(built: Path) -> tuple[nx.MultiDiGraph, list[FileMeta]]:
    graph, files, _ = SqliteStore(built).load()
    return graph, files


@pytest.fixture(scope="module")
def query(built: Path) -> GraphQuery:
    return GraphQuery(built)


def _names_of_kinds(graph: nx.MultiDiGraph, kinds: frozenset[NodeKind]) -> list[str]:
    return sorted({data["name"] for _, data in graph.nodes(data=True) if data["kind"] in kinds})


def _same(label: object, query_call: Callable[[], Any], ref_call: Callable[[], Any]) -> None:
    """Assert the query path matches the full-graph path — including when the
    full-graph path raises (a stub/unknown name must raise identically)."""
    try:
        expected = ref_call()
    except Exception as exc:  # noqa: BLE001 — error parity is part of the contract
        with pytest.raises(type(exc), match=re.escape(str(exc))):
            query_call()
        return
    assert query_call() == expected, label


def _ref_hierarchy(g: nx.MultiDiGraph, top: str, depth: int, max_nodes: int) -> dict[str, object]:
    """What the old ``_get_hierarchy_impl`` returned for a named top."""
    roots = [
        node_id
        for node_id, d in g.nodes(data=True)
        if d["kind"] in analysis.INSTANTIABLE_KINDS
        and not d["attrs"].get("unresolved")
        and d["name"] == (top.lower() if d["language"] is Language.VHDL else top)
    ]
    tree = srv._jsonable(analysis.hierarchy_tree(g, roots[0], max_depth=max(1, depth)))
    omitted = srv._prune_tree(tree, max(1, max_nodes))
    return {"root": tree, "nodes_omitted": omitted}


# -- per-name parity sweeps ----------------------------------------------------


def test_find_module_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    names = _names_of_kinds(graph, analysis.INSTANTIABLE_KINDS)
    assert names  # the corpus has units
    for name in [*names, "*", "no_such_unit", "u_*"]:
        assert query.find_module(name, 20) == srv._find_module_impl(graph, name, 20), name


def test_who_instantiates_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    for name in [*_names_of_kinds(graph, analysis.INSTANTIABLE_KINDS), "missing_child"]:
        expected = srv._page(analysis.instances_of(graph, name), 50, 0)
        assert query.who_instantiates(name, 50, 0) == expected, name


def test_port_map_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    names = _names_of_kinds(graph, analysis.INSTANTIABLE_KINDS)
    instances = _names_of_kinds(graph, frozenset({NodeKind.INSTANCE}))
    for name in names:
        _same(
            name,
            lambda n=name: query.port_map(n, None),
            lambda n=name: srv._port_map_impl(graph, n, None),
        )
        for inst in instances[:5]:
            _same(
                (name, inst),
                lambda n=name, i=inst: query.port_map(n, i),
                lambda n=name, i=inst: srv._port_map_impl(graph, n, i),
            )


def test_hierarchy_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    tops = {
        data["name"]
        for node_id, data in graph.nodes(data=True)
        if node_id in set(analysis.find_top_modules(graph))
    }
    assert tops
    for name in sorted(tops):
        for depth in (1, 3, 64):
            assert query.hierarchy(name, depth, 500) == _ref_hierarchy(graph, name, depth, 500), (
                name,
                depth,
            )
    # node-cap pruning matches too
    name = sorted(tops)[0]
    assert query.hierarchy(name, 64, 1) == _ref_hierarchy(graph, name, 64, 1)


def test_top_modules_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    expected = [
        {
            "name": graph.nodes[node_id]["name"],
            "file": graph.nodes[node_id]["file"],
            "kind": graph.nodes[node_id]["kind"].value,
        }
        for node_id in analysis.find_top_modules(graph)
    ]
    assert query.top_modules() == expected


def test_find_signal_drivers_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    signals = _names_of_kinds(graph, frozenset({NodeKind.SIGNAL, NodeKind.PORT}))
    modules = [None, *_names_of_kinds(graph, analysis.INSTANTIABLE_KINDS)]
    assert signals
    for signal in signals:
        for readers in (False, True):
            expected = srv._page(
                analysis.signal_drivers(graph, signal, module=None, readers=readers), 50, 0
            )
            assert query.find_signal_drivers(signal, None, readers, 50, 0) == expected, signal
    # a module-scoped spot check across modules
    for signal in signals[:8]:
        for module in modules:
            expected = srv._page(
                analysis.signal_drivers(graph, signal, module=module, readers=False), 50, 0
            )
            assert query.find_signal_drivers(signal, module, False, 50, 0) == expected, (
                signal,
                module,
            )


def test_instances_of_list_parity(query: GraphQuery, loaded) -> None:
    # The CLI `instances-of` routes through this unpaged list method (the bounded
    # path the MCP `who_instantiates` also wraps); it must equal the full-graph oracle.
    graph, _ = loaded
    for name in [*_names_of_kinds(graph, analysis.INSTANTIABLE_KINDS), "missing_child"]:
        assert query.instances_of(name) == analysis.instances_of(graph, name), name


def test_signal_drivers_list_parity(query: GraphQuery, loaded) -> None:
    # The CLI `drivers` routes through this unpaged list method.
    graph, _ = loaded
    signals = _names_of_kinds(graph, frozenset({NodeKind.SIGNAL, NodeKind.PORT}))
    assert signals
    for signal in signals:
        for readers in (False, True):
            assert query.signal_drivers(signal, None, readers) == analysis.signal_drivers(
                graph, signal, module=None, readers=readers
            ), signal
    # the module-scoped branch (CLI `drivers --module`) has its own filtering +
    # one-edge-kind hydration; pin it too, incl. a missing-module negative case.
    modules = [*_names_of_kinds(graph, analysis.INSTANTIABLE_KINDS), "no_such_module"]
    for signal in signals[:8]:
        for module in modules:
            assert query.signal_drivers(signal, module, False) == analysis.signal_drivers(
                graph, signal, module=module, readers=False
            ), (signal, module)


def test_modules_parity(query: GraphQuery, loaded) -> None:
    # The CLI `modules` routes through this bounded list method; it must equal the
    # full-graph computation (every MODULE/ENTITY, sorted by name, with counts).
    graph, _ = loaded
    expected = [
        {
            "name": data["name"],
            "kind": data["kind"],
            "file": data["file"],
            "line": data["line_span"][0],
            "instances": analysis.instantiation_count(graph, node_id),
        }
        for node_id, data in sorted(
            graph.nodes(data=True),
            key=lambda kv: (kv[1]["name"], kv[1]["file"], kv[1]["line_span"][0]),
        )
        if data["kind"] in (NodeKind.MODULE, NodeKind.ENTITY)
        and not data["attrs"].get("unresolved")
    ]
    assert expected
    assert query.modules() == expected


def test_reset_tree_parity(query: GraphQuery, loaded) -> None:
    # The CLI `reset-tree` routes through this bounded SQL scan; it must equal the
    # NetworkX oracle (alias-merged reset groups), dataclass→dict.
    from hdl_kgraph.graph import clocks

    graph, _ = loaded
    assert query.reset_tree() == srv._jsonable(clocks.reset_tree(graph))


def test_unresolved_stubs_parity(query: GraphQuery, loaded) -> None:
    # The CLI `unresolved` routes through this bounded scan; it must equal the
    # full-graph oracle (the corpus has unresolved stubs, e.g. uvm_* bases).
    graph, _ = loaded
    expected = analysis.unresolved_stubs(graph)
    assert expected  # corpus has unresolved references
    assert query.unresolved_stubs() == expected


def test_find_signal_drivers_scopes_edge_hydration_to_module(
    query: GraphQuery, loaded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#108: a `module`-scoped query hydrates DRIVES/READS for only that unit's
    signals, not every design-wide signal of the same name."""
    from hdl_kgraph.schema import EdgeKind

    graph, _ = loaded
    # A signal/port name owned by >= 2 distinct units (so scoping can shrink it).
    owners: dict[str, set[str]] = {}
    for nid, d in graph.nodes(data=True):
        if d["kind"] in (NodeKind.SIGNAL, NodeKind.PORT):
            _, names = analysis._signal_unit_names(graph, nid)
            owners.setdefault(d["name"], set()).update(n for n in names if n)
    multi = [(name, sorted(units)) for name, units in owners.items() if len(units) >= 2]
    assert multi, "fixture should have a signal/port name shared by >= 2 units"
    name, units = multi[0]

    captured: list[int] = []
    real = type(query)._hydrate_in_edges

    def spy(self, g, conn, ids, kinds=None):  # type: ignore[no-untyped-def]
        ids = list(ids)
        if kinds and EdgeKind.DRIVES in kinds:
            captured.append(len(ids))
        return real(self, g, conn, ids, kinds)

    monkeypatch.setattr(type(query), "_hydrate_in_edges", spy)
    query.find_signal_drivers(name, None, False, 50, 0)
    query.find_signal_drivers(name, units[0], False, 50, 0)
    unscoped, scoped = captured[0], captured[1]
    assert scoped < unscoped  # the module filter dropped the other units' fanout


def test_search_nodes_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    cases: list[tuple[str, list[NodeKind] | None, str | None]] = [
        ("*", None, None),
        ("*", [NodeKind.MODULE], None),
        ("*", [NodeKind.SIGNAL, NodeKind.PORT], None),
        ("u_*", None, None),
        ("*clk*", None, None),
        ("adder", None, None),
        ("*", None, "*.vhd"),
        ("[ab]*", None, None),  # char class -> Python-side fnmatch path
        ("df_top.*", None, None),  # qualified-name path
    ]
    for name, kinds, file in cases:
        expected = srv._page(analysis.search_nodes(graph, name=name, kinds=kinds, file=file), 50, 0)
        assert query.search_nodes(name, kinds, file, 50, 0) == expected, (name, kinds, file)


def test_impact_parity(query: GraphQuery, loaded) -> None:
    graph, files = loaded
    targets = _names_of_kinds(graph, analysis.IMPACT_UNIT_KINDS)
    file_targets = [f.path for f in files][:6]
    for target in [*targets, *file_targets]:
        for max_depth in (0, 1, 2):
            _same(
                (target, max_depth),
                lambda t=target, d=max_depth: query.impact_of_change(t, d, 100, 0),
                lambda t=target, d=max_depth: srv._impact_impl(graph, files, t, d, 100, 0),
            )


def test_impact_unknown_target_raises(query: GraphQuery) -> None:
    with pytest.raises(ValueError, match="matches no file or design unit"):
        query.impact_of_change("definitely_not_here", 0, 100, 0)


def test_global_tools_parity(query: GraphQuery, loaded) -> None:
    graph, _ = loaded
    assert query.clock_domains() == srv._clock_domains_impl(graph)
    assert query.uvm_topology() == srv._uvm_impl(graph)
    assert query.power_domains() == srv._power_domains_impl(graph)


def test_no_full_graph_load(query: GraphQuery, loaded, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: no tool may fall back to SqliteStore.load() on a current
    (summary-bearing) database — that is the unbounded path this work removes."""
    import hdl_kgraph.storage.query as query_mod

    graph, files = loaded
    tops = query.top_modules()
    signals = _names_of_kinds(graph, frozenset({NodeKind.SIGNAL, NodeKind.PORT}))
    assert tops, "expected at least one top module in the fixture corpus"
    assert signals, "expected at least one signal/port in the fixture corpus"
    assert files, "expected at least one source file in the fixture corpus"
    top = tops[0]["name"]
    signal = signals[0]
    file_target = files[0].path

    def boom(self: object) -> None:
        raise AssertionError("the query path loaded the whole graph")

    monkeypatch.setattr(query_mod.SqliteStore, "load", boom)

    query.find_module("*", 20)
    query.search_nodes("*", [NodeKind.MODULE], None, 50, 0)
    query.who_instantiates(top, 50, 0)
    query.port_map(top, None)
    query.hierarchy(top, 3, 500)
    query.top_modules()
    query.find_signal_drivers(signal, None, False, 50, 0)
    query.impact_of_change(file_target, 0, 100, 0)
    # precomputed -> read the summary blob, never the graph
    query.clock_domains()
    query.uvm_topology()
