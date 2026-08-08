"""Bounded, index-backed reads over the knowledge graph (scalability).

``SqliteStore.load`` rebuilds the *entire* graph as a NetworkX object — fine
for a full re-link, export, or visualization, but it makes every MCP/CLI query
pay for the whole design (and at 10–100+ GB it does not fit in memory at all).

``GraphQuery`` answers each query by hydrating only the *bounded subgraph* the
query touches — selected through the existing SQLite indices
(``idx_nodes_kind_name``, ``idx_edges_src``, ``idx_edges_dst``,
``idx_nodes_file``) — and then runs the **same** :mod:`hdl_kgraph.graph.analysis`
function on that small graph. Reusing the analysis functions (rather than
re-deriving each query in SQL) keeps the results byte-identical to the
full-graph path by construction; ``tests/test_query.py`` pins that parity.

Reads are connection-per-call (like :meth:`SqliteStore._connect`): a fresh
read connection is cheap, never pins the inode a concurrent ``update`` swaps
out via ``os.replace``, and inherits the busy-timeout/retry behaviour. The
genuinely whole-design analyses (clock domains, UVM topology) cannot be
bounded; they are served from precomputed summary tables when present and fall
back to a full load otherwise (see :meth:`clock_domains`/:meth:`uvm_topology`).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.graph import analysis
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    SqliteStore,
    add_edge_row,
    add_node_row,
)

#: SQLite caps host parameters per statement (``SQLITE_MAX_VARIABLE_NUMBER``,
#: historically 999). Chunk ``IN (...)`` id lists well under that.
_IN_CHUNK = 800

#: Hierarchy roots and instantiable units, mirroring :mod:`analysis`.
_HIERARCHY_ROOT_KINDS = (NodeKind.MODULE.value, NodeKind.ENTITY.value)
_INSTANTIABLE_KINDS = tuple(k.value for k in analysis.INSTANTIABLE_KINDS)
_INSTANCE_TARGET_KINDS = (
    NodeKind.MODULE.value,
    NodeKind.INTERFACE.value,
    NodeKind.PROGRAM.value,
    NodeKind.PRIMITIVE.value,
    NodeKind.ENTITY.value,
)


def _glob_is_sql_safe(pattern: str) -> bool:
    """Whether SQLite ``GLOB`` matches Python ``fnmatchcase`` for *pattern*.

    ``*`` and ``?`` agree, but character classes diverge (fnmatch ``[!x]`` vs
    GLOB ``[^x]``), so a pattern with a ``[`` is finished in Python instead.
    """
    return "[" not in pattern


class GraphQuery:
    """Index-backed reader that never materializes the whole graph."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._store = SqliteStore(db_path)

    # -- low-level row fetching ------------------------------------------------

    def _nodes_by_ids(self, conn: Any, ids: Iterable[str]) -> Iterator[tuple[object, ...]]:
        yield from _select_in(conn, f"SELECT {NODE_COLUMNS} FROM nodes WHERE id", ids)

    def _hydrate_nodes(self, graph: nx.MultiDiGraph, conn: Any, ids: Iterable[str]) -> None:
        for row in self._nodes_by_ids(conn, ids):
            add_node_row(graph, row)

    def _hydrate_out_edges(
        self,
        graph: nx.MultiDiGraph,
        conn: Any,
        ids: Iterable[str],
        kinds: tuple[EdgeKind, ...] | None = None,
    ) -> set[str]:
        """Load ``WHERE src IN ids`` edges (optionally of *kinds*); return dst ids."""
        return self._hydrate_edges(graph, conn, "src", ids, kinds)

    def _hydrate_in_edges(
        self,
        graph: nx.MultiDiGraph,
        conn: Any,
        ids: Iterable[str],
        kinds: tuple[EdgeKind, ...] | None = None,
    ) -> set[str]:
        """Load ``WHERE dst IN ids`` edges (optionally of *kinds*); return src ids."""
        return self._hydrate_edges(graph, conn, "dst", ids, kinds)

    def _hydrate_edges(
        self,
        graph: nx.MultiDiGraph,
        conn: Any,
        column: str,
        ids: Iterable[str],
        kinds: tuple[EdgeKind, ...] | None,
    ) -> set[str]:
        kind_clause = ""
        extra: tuple[object, ...] = ()
        if kinds is not None:
            placeholders = ", ".join("?" for _ in kinds)
            kind_clause = f" AND kind IN ({placeholders})"
            extra = tuple(k.value for k in kinds)
        other = "dst" if column == "src" else "src"
        reached: set[str] = set()
        for row in _select_in(
            conn, f"SELECT {EDGE_COLUMNS} FROM edges WHERE {column}", ids, kind_clause, extra
        ):
            add_edge_row(graph, row)
            reached.add(str(row[1] if other == "dst" else row[0]))
        return reached

    def _ensure_endpoints(self, graph: nx.MultiDiGraph, conn: Any) -> None:
        """Hydrate any node that exists only as a bare edge endpoint.

        ``graph.add_edge(src, dst, ...)`` silently creates ``src``/``dst`` with
        no attributes, so a node can be *present* yet have no ``kind`` — which
        the analysis functions index by. Load full rows for every such node.
        """
        missing = [n for n in graph.nodes if "kind" not in graph.nodes[n]]
        if missing:
            self._hydrate_nodes(graph, conn, missing)

    # -- name resolution (indexed) ---------------------------------------------

    def _ids_by_name(self, conn: Any, kinds: tuple[str, ...], name: str) -> list[str]:
        """Node ids of the given *kinds* named *name*, honouring VHDL casing.

        Uses ``idx_nodes_kind_name``. VHDL names are stored lowercase, so a
        VHDL row matches the lowercased *name*; everything else matches as-is.
        """
        kind_ph = ", ".join("?" for _ in kinds)
        # ``name IN (name, name.lower())`` is an indexed superset; the per-row
        # check then enforces the rule (VHDL: lowercased; others: exact case).
        rows = conn.execute(
            f"SELECT id, language, name FROM nodes WHERE kind IN ({kind_ph}) AND name IN (?, ?)",
            (*kinds, name, name.lower()),
        )
        out: list[str] = []
        for node_id, language, row_name in rows:
            wanted = name.lower() if Language(language) is Language.VHDL else name
            if row_name == wanted:
                out.append(str(node_id))
        return out

    # -- tools -----------------------------------------------------------------

    def find_module(self, name: str, limit: int) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _find_module_impl, _page

        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            unit_ids = self._glob_ids(conn, _INSTANTIABLE_KINDS, name)
            self._hydrate_nodes(graph, conn, unit_ids)
            # find_module needs DECLARES children (to count ports/params) and a
            # count of incoming INSTANTIATES — load both, plus the child nodes.
            self._hydrate_out_edges(graph, conn, unit_ids, (EdgeKind.DECLARES,))
            self._hydrate_in_edges(graph, conn, unit_ids, (EdgeKind.INSTANTIATES,))
            self._ensure_endpoints(graph, conn)
            if not unit_ids:
                return _page([], limit, 0)
            return _find_module_impl(graph, name, limit)

    def search_nodes(
        self,
        name: str,
        kinds: list[NodeKind] | None,
        file: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _page

        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            for row in self._search_rows(conn, name, kinds, file):
                add_node_row(graph, row)
            results = analysis.search_nodes(graph, name=name, kinds=kinds, file=file)
            return _page(results, limit, offset)

    def instances_of(self, name: str) -> list[dict[str, Any]]:
        """Bounded `instances-of`: every instantiation site of *name* (full list,
        no pagination) — hydrates only *name*'s nodes and their incoming
        INSTANTIATES edges, never the whole graph."""
        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            target_ids = self._ids_by_name(conn, _INSTANCE_TARGET_KINDS, name)
            self._hydrate_nodes(graph, conn, target_ids)
            self._hydrate_in_edges(graph, conn, target_ids, (EdgeKind.INSTANTIATES,))
            self._ensure_endpoints(graph, conn)
            return analysis.instances_of(graph, name)

    def who_instantiates(self, name: str, limit: int, offset: int) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _page

        return _page(self.instances_of(name), limit, offset)

    def port_map(self, module: str, instance: str | None) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _port_map_impl

        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            unit_ids = self._ids_by_name(conn, _INSTANTIABLE_KINDS, module)
            self._hydrate_nodes(graph, conn, unit_ids)
            self._hydrate_out_edges(graph, conn, unit_ids, (EdgeKind.DECLARES,))
            if instance is not None:
                inst_ids = self._hydrate_in_edges(graph, conn, unit_ids, (EdgeKind.INSTANTIATES,))
                self._hydrate_nodes(graph, conn, inst_ids)
                # _port_map_impl reports CONNECTS only for the instance whose
                # name/qualified_name matches, so hydrate that fanout for the
                # matching instances alone — a unit reused thousands of times
                # otherwise loads every instance's bindings to return one.
                matching = [
                    i
                    for i in inst_ids
                    if instance in (graph.nodes[i]["name"], graph.nodes[i]["qualified_name"])
                ]
                self._hydrate_out_edges(graph, conn, matching, (EdgeKind.CONNECTS,))
            self._ensure_endpoints(graph, conn)
            return _port_map_impl(graph, module, instance)

    def signal_drivers(
        self, signal: str, module: str | None, readers: bool
    ) -> list[dict[str, Any]]:
        """Bounded `drivers`: what drives (or reads) *signal* (full list, no
        pagination) — hydrates only the named signals and their DRIVES/READS
        fanout, never the whole graph."""
        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            sig_ids = self._ids_by_name(conn, (NodeKind.SIGNAL.value, NodeKind.PORT.value), signal)
            self._hydrate_nodes(graph, conn, sig_ids)
            # Each signal's owning unit (reached by climbing reverse DECLARES)
            # must be present before we can scope by module.
            self._climb_declares(graph, conn, sig_ids)
            # The DRIVES/READS fanout is the expensive part. When a module is
            # given, hydrate it only for signals that unit actually owns —
            # `analysis.signal_drivers` skips the rest before touching their
            # edges, so the result is identical (reuse its exact unit-name rule).
            if module is None:
                drivers_of = sig_ids
            else:
                drivers_of = [
                    sid
                    for sid in sig_ids
                    if (module.lower() if graph.nodes[sid]["language"] is Language.VHDL else module)
                    in analysis._signal_unit_names(graph, sid)[1]
                ]
            # analysis.signal_drivers reads only one edge kind (READS iff readers,
            # else DRIVES), so hydrate just that one — halves the fanout scan on
            # the hot path, byte-identical.
            edge_kinds = (EdgeKind.READS,) if readers else (EdgeKind.DRIVES,)
            self._hydrate_in_edges(graph, conn, drivers_of, edge_kinds)
            self._ensure_endpoints(graph, conn)
            return analysis.signal_drivers(graph, signal, module=module, readers=readers)

    def find_signal_drivers(
        self, signal: str, module: str | None, readers: bool, limit: int, offset: int
    ) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _page

        return _page(self.signal_drivers(signal, module, readers), limit, offset)

    def unresolved_stubs(self) -> list[dict[str, Any]]:
        """Bounded `unresolved`: every unresolved stub and its referrer ids.

        Scans the nodes table for unresolved stubs and hydrates only their
        incoming edges — `analysis.unresolved_stubs` reads each stub's own attrs
        and its referrers' *ids* (not their attributes), so the whole graph is
        never materialised. `_ensure_endpoints` fills the bare referrer nodes so
        the reused analysis can iterate every node's attrs safely."""
        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            for row in conn.execute(
                f"SELECT {NODE_COLUMNS} FROM nodes WHERE json_extract(attrs, '$.unresolved') = 1"
            ):
                add_node_row(graph, row)
            self._hydrate_in_edges(graph, conn, list(graph.nodes))
            self._ensure_endpoints(graph, conn)
            return analysis.unresolved_stubs(graph)

    def modules(self) -> list[dict[str, Any]]:
        """Bounded `modules`: every MODULE/ENTITY with its instantiation count
        (full list) — hydrates only those units and their incoming INSTANTIATES
        edges, never the whole graph. Sorted by ``(name, file, line)`` so the order
        is deterministic even for same-named units (e.g. a VHDL entity and an SV
        module sharing a name), independent of row/load order."""
        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            ids = [
                add_node_row(graph, row)
                for row in conn.execute(
                    f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind IN (?, ?)",
                    (NodeKind.MODULE.value, NodeKind.ENTITY.value),
                )
            ]
            self._hydrate_in_edges(graph, conn, ids, (EdgeKind.INSTANTIATES,))
            return [
                {
                    "name": graph.nodes[nid]["name"],
                    "kind": graph.nodes[nid]["kind"],
                    "file": graph.nodes[nid]["file"],
                    "line": graph.nodes[nid]["line_span"][0],
                    "instances": analysis.instantiation_count(graph, nid),
                }
                for nid in sorted(
                    ids,
                    key=lambda i: (
                        graph.nodes[i]["name"],
                        graph.nodes[i]["file"],
                        graph.nodes[i]["line_span"][0],
                    ),
                )
                if not graph.nodes[nid]["attrs"].get("unresolved")
            ]

    def reset_tree(self) -> list[dict[str, Any]]:
        """Bounded `reset-tree`: RESETS edges grouped by reset net, out-of-core.

        There is no persisted reset summary, so this always recomputes from SQLite
        via the same net-alias union-find the clock report uses — bounded by the
        RESETS edges + alias pairs, never a full graph load."""
        from hdl_kgraph.storage import summaries

        with self._store._connect() as conn:
            self._store._check_version(conn)
            return summaries.reset_summary_sql(conn)

    def top_modules(self) -> list[dict[str, Any]]:
        """MODULE/ENTITY nodes with no incoming INSTANTIATES (indexed)."""
        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph = nx.MultiDiGraph()
            kind_ph = ", ".join("?" for _ in _HIERARCHY_ROOT_KINDS)
            rows = conn.execute(
                f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind IN ({kind_ph}) "
                f"AND id NOT IN (SELECT dst FROM edges WHERE kind = ?)",
                (*_HIERARCHY_ROOT_KINDS, EdgeKind.INSTANTIATES.value),
            )
            for row in rows:
                add_node_row(graph, row)
            return [
                {
                    "name": graph.nodes[node_id]["name"],
                    "file": graph.nodes[node_id]["file"],
                    "kind": graph.nodes[node_id]["kind"].value,
                }
                for node_id in analysis.find_top_modules(graph)
            ]

    def hierarchy(self, top: str, depth: int, max_nodes: int) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _jsonable, _prune_tree

        with self._store._connect() as conn:
            self._store._check_version(conn)
            roots = self._ids_by_name(conn, _INSTANTIABLE_KINDS, top)
            roots = [r for r in roots if not self._is_unresolved(conn, r)]
            if not roots:
                raise ValueError(f"no module or entity named {top!r} in the graph")
            graph = self._hierarchy_subgraph(conn, roots[0], max(1, depth))
            tree = _jsonable(analysis.hierarchy_tree(graph, roots[0], max_depth=max(1, depth)))
            omitted = _prune_tree(tree, max(1, max_nodes))
            return {"root": tree, "nodes_omitted": omitted}

    def impact_of_change(
        self, target: str, max_depth: int, limit: int, offset: int
    ) -> dict[str, Any]:
        from hdl_kgraph.mcp.server import _impact_impl

        with self._store._connect() as conn:
            self._store._check_version(conn)
            graph, files, _seeds = self._impact_subgraph(conn, target, max_depth)
            # _impact_impl re-resolves the seeds against the hydrated subgraph
            # (identical result) and runs the real impact_radius on it.
            return _impact_impl(graph, files, target, max_depth, limit, offset)

    # -- genuinely-global tools (precomputed summary, else recompute) ----------

    def clock_domains(self) -> dict[str, Any]:
        """The ``clock_domains`` payload: the persisted summary when present,
        else a **bounded SQL-native scan** (never a full graph load).

        The clock/CDC report cannot be answered from a bounded subgraph, but it
        *can* be computed straight from SQLite without materialising the whole
        graph (:func:`hdl_kgraph.storage.summaries.clock_summary_sql`, byte-
        identical to the NetworkX path), so the missing-summary fallback stays
        out-of-core."""
        import json

        from hdl_kgraph.storage import summaries

        payload = self._store.load_summary("clock_domains")
        if payload is not None:
            return dict(json.loads(payload))
        with self._store._connect() as conn:
            self._store._check_version(conn)
            return summaries.clock_summary_sql(conn)

    def uvm_topology(self) -> dict[str, Any]:
        """The ``uvm_topology`` payload: the persisted summary when present, else a
        **bounded subgraph scan** (only the class graph, never a full graph load).

        Like clock/CDC, the UVM report is whole-design but bounded by the class
        inheritance graph, so :func:`hdl_kgraph.storage.summaries.uvm_summary_sql`
        serves the missing-summary fallback out-of-core (byte-identical to the
        NetworkX path)."""
        import json

        from hdl_kgraph.storage import summaries

        payload = self._store.load_summary("uvm_topology")
        if payload is not None:
            return dict(json.loads(payload))
        with self._store._connect() as conn:
            self._store._check_version(conn)
            return summaries.uvm_summary_sql(conn)

    def power_domains(self) -> dict[str, Any]:
        """The ``power_domains`` payload: the persisted summary when present, else a
        **bounded subgraph scan** (only POWER_DOMAIN nodes + their CONSTRAINS edges).

        Like clock/UVM, the UPF power-domain report is whole-design but bounded by
        the small power-domain subgraph, so
        :func:`hdl_kgraph.storage.summaries.power_summary_sql` serves the
        missing-summary fallback out-of-core (byte-identical to the NetworkX path)."""
        import json

        from hdl_kgraph.storage import summaries

        payload = self._store.load_summary("power_domains")
        if payload is not None:
            return dict(json.loads(payload))
        with self._store._connect() as conn:
            self._store._check_version(conn)
            return summaries.power_summary_sql(conn)

    def qualified_names(self, ids: list[str]) -> dict[str, str]:
        """``node id -> qualified_name`` for *ids*, bounded (indexed PK lookup).

        For label-resolving a handful of nodes (e.g. the CDC reader processes in
        the ``cdc`` report) without materialising the whole graph."""
        with self._store._connect() as conn:
            self._store._check_version(conn)
            return {
                str(row[0]): str(row[1])
                for row in _select_in(conn, "SELECT id, qualified_name FROM nodes WHERE id", ids)
            }

    # -- subgraph builders -----------------------------------------------------

    def _glob_ids(self, conn: Any, kinds: tuple[str, ...], name: str) -> list[str]:
        """Instantiable-unit ids whose name matches the *name* glob (indexed).

        Mirrors ``analysis.search_nodes`` name semantics: VHDL rows match the
        lowercased pattern; ``*``/``?`` push to ``GLOB`` when safe, otherwise the
        kind-narrowed rows are fnmatch-filtered in Python.
        """
        from fnmatch import fnmatchcase

        kind_ph = ", ".join("?" for _ in kinds)
        if _glob_is_sql_safe(name):
            rows = conn.execute(
                f"SELECT id FROM nodes WHERE kind IN ({kind_ph}) "
                f"AND ((language = ? AND name GLOB ?) OR (language != ? AND name GLOB ?))",
                (*kinds, Language.VHDL.value, name.lower(), Language.VHDL.value, name),
            )
            return [str(r[0]) for r in rows]
        out: list[str] = []
        for node_id, language, row_name in conn.execute(
            f"SELECT id, language, name FROM nodes WHERE kind IN ({kind_ph})", kinds
        ):
            pattern = name.lower() if Language(language) is Language.VHDL else name
            if fnmatchcase(row_name, pattern):
                out.append(str(node_id))
        return out

    def _search_rows(
        self, conn: Any, name: str, kinds: list[NodeKind] | None, file: str | None
    ) -> Iterator[tuple[object, ...]]:
        """``nodes`` rows that could match a ``search_nodes`` query.

        Pushes the kind filter and, when safe, the name/file globs to SQL; the
        final (fnmatch, qualified-name, VHDL-case) decision is left to
        ``analysis.search_nodes`` so semantics stay identical.
        """
        clauses: list[str] = []
        params: list[object] = []
        if kinds is not None:
            clauses.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params.extend(k.value for k in kinds)
        # A name glob without a '.' and SQL-safe can prefilter (search_nodes also
        # checks qualified_name when the pattern contains '.', so skip then).
        if name != "*" and "." not in name and _glob_is_sql_safe(name):
            clauses.append("((language = ? AND name GLOB ?) OR (language != ? AND name GLOB ?))")
            params.extend((Language.VHDL.value, name.lower(), Language.VHDL.value, name))
        if file is not None and _glob_is_sql_safe(file):
            clauses.append("file GLOB ?")
            params.append(file)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        yield from conn.execute(f"SELECT {NODE_COLUMNS} FROM nodes{where}", params)

    def _hierarchy_subgraph(self, conn: Any, root_id: str, depth: int) -> nx.MultiDiGraph:
        """Load the instance subtree under *root_id*, bounded by *depth*.

        Walks the same DECLARES + INSTANTIATES (+ reverse IMPLEMENTS for VHDL
        architectures) relation as ``analysis.hierarchy_tree``, loading one level
        past *depth* so its lookahead for the depth-cap ``truncated`` flag still
        sees whether a capped unit had children.

        Note (#108): this loads the whole subtree to *depth*, not just the first
        ``max_nodes``. An early-stop cannot be byte-identical because the tool's
        ``nodes_omitted`` is the *full* count of pruned nodes (``_prune_tree`` →
        ``_count_tree`` over each cut subtree), which requires the entire
        unfolded tree. Bounding it would change that output contract.
        """
        graph = nx.MultiDiGraph()
        self._hydrate_nodes(graph, conn, [root_id])
        frontier = {root_id}
        visited: set[str] = set()
        for _ in range(depth + 1):
            frontier -= visited
            if not frontier:
                break
            visited |= frontier
            # Holders: the unit itself plus its architectures (reverse IMPLEMENTS).
            arch_ids = self._hydrate_in_edges(graph, conn, frontier, (EdgeKind.IMPLEMENTS,))
            self._hydrate_nodes(graph, conn, arch_ids)
            holders = frontier | arch_ids
            inst_ids = self._hydrate_out_edges(graph, conn, holders, (EdgeKind.DECLARES,))
            self._hydrate_nodes(graph, conn, inst_ids)
            children = self._hydrate_out_edges(graph, conn, inst_ids, (EdgeKind.INSTANTIATES,))
            self._hydrate_nodes(graph, conn, children)
            frontier = children
        self._ensure_endpoints(graph, conn)
        return graph

    def _impact_subgraph(
        self, conn: Any, target: str, max_depth: int
    ) -> tuple[nx.MultiDiGraph, list[Any], list[str]]:
        """Hydrate the reverse-dependency closure ``impact_radius`` will walk.

        Expands only along the edge kinds ``analysis._impact_dependents``
        follows, so the closure is bounded by the eventual answer; the real
        ``impact_radius`` then runs on it for an identical result.
        """
        files = self._load_file_metas(conn)  # only the files table; not the graph
        graph = nx.MultiDiGraph()
        # Resolve seeds: file path first, else unit name. Mirrors impact_seeds,
        # but file seeds need only the FILE node and unit seeds an indexed lookup.
        seeds = self._impact_seeds(conn, graph, files, target)
        frontier = set(seeds)
        visited = set(seeds)
        steps = max_depth if max_depth > 0 else 1 << 30
        for _ in range(steps):
            if not frontier:
                break
            self._hydrate_impact_neighbors(graph, conn, frontier)
            self._ensure_endpoints(graph, conn)
            # The next frontier is every newly-pulled-in node we have not expanded.
            newly = set(graph.nodes) - visited
            visited |= newly
            frontier = newly
        self._ensure_endpoints(graph, conn)
        return graph, files, seeds

    def _hydrate_impact_neighbors(self, graph: nx.MultiDiGraph, conn: Any, ids: set[str]) -> None:
        """One BFS step of the impact closure: load everything one
        ``_impact_dependents`` call would follow out of *ids* (over-loading edge
        kinds is harmless — ``impact_radius`` filters by kind)."""
        # Dependents arrive via reverse INSTANTIATES/IMPORTS/USES_PACKAGE/BINDS/
        # EXTENDS/IMPLEMENTS (and reverse INCLUDES for FILE nodes); the source of
        # each is then resolved to its enclosing unit by climbing reverse
        # DECLARES — so a full climb must happen within this single step.
        dep_srcs = self._hydrate_in_edges(
            graph,
            conn,
            ids,
            (
                EdgeKind.INSTANTIATES,
                EdgeKind.IMPORTS,
                EdgeKind.USES_PACKAGE,
                EdgeKind.BINDS,
                EdgeKind.EXTENDS,
                EdgeKind.IMPLEMENTS,
                EdgeKind.INCLUDES,
            ),
        )
        # ARCHITECTURE change -> its entity; FILE -> the units it DECLARES.
        dst_units = self._hydrate_out_edges(
            graph, conn, ids, (EdgeKind.IMPLEMENTS, EdgeKind.DECLARES)
        )
        # FILE macro two-hop: DEFINES_MACRO -> macro, then reverse USES_MACRO ->
        # the user files (one _impact_dependents call covers both hops).
        macros = self._hydrate_out_edges(graph, conn, ids, (EdgeKind.DEFINES_MACRO,))
        self._hydrate_nodes(graph, conn, macros)
        macro_users = self._hydrate_in_edges(graph, conn, macros, (EdgeKind.USES_MACRO,))
        self._hydrate_nodes(graph, conn, dep_srcs | dst_units | macro_users)
        # Resolve every dependent source to its enclosing unit now: _enclosing_unit
        # may climb several reverse-DECLARES levels (e.g. through a generate block).
        self._climb_declares(graph, conn, dep_srcs)

    def _impact_seeds(
        self, conn: Any, graph: nx.MultiDiGraph, files: list[Any], target: str
    ) -> list[str]:
        from hdl_kgraph.discovery import SUFFIXES

        known_paths = {f.path for f in files}
        candidate = target.replace("\\", "/").lstrip("./")
        if candidate in known_paths or "/" in candidate or Path(candidate).suffix in SUFFIXES:
            matches = [
                f"file:{p}" for p in known_paths if p == candidate or p.endswith("/" + candidate)
            ]
            present = [
                node_id
                for node_id in matches
                if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
            ]
            self._hydrate_nodes(graph, conn, present)
            return present
        ids = self._ids_by_name(conn, tuple(k.value for k in analysis.IMPACT_UNIT_KINDS), target)
        ids = [i for i in ids if not self._is_unresolved(conn, i)]
        self._hydrate_nodes(graph, conn, ids)
        return ids

    def _climb_declares(self, graph: nx.MultiDiGraph, conn: Any, ids: Iterable[str]) -> None:
        """Load the reverse-DECLARES chain from each id up to its enclosing unit
        so ``_enclosing_unit`` can resolve the owning module/architecture."""
        frontier = set(ids)
        seen: set[str] = set()
        while frontier:
            frontier -= seen
            if not frontier:
                break
            seen |= frontier
            parents = self._hydrate_in_edges(graph, conn, frontier, (EdgeKind.DECLARES,))
            self._hydrate_nodes(graph, conn, parents)
            # A VHDL architecture's signals belong to its entity via IMPLEMENTS.
            entities = self._hydrate_out_edges(graph, conn, parents, (EdgeKind.IMPLEMENTS,))
            self._hydrate_nodes(graph, conn, entities)
            frontier = parents | entities

    # -- small helpers ---------------------------------------------------------

    def _is_unresolved(self, conn: Any, node_id: str) -> bool:
        import json

        row = conn.execute("SELECT attrs FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return bool(row and json.loads(row[0]).get("unresolved"))

    def _load_file_metas(self, conn: Any) -> list[Any]:
        import json

        from hdl_kgraph.storage.sqlite_store import FileMeta

        return [
            FileMeta(
                path=row[0],
                language=Language(row[1]),
                content_hash=row[2],
                size_bytes=row[3],
                parse_error_count=row[4],
                skipped_reason=row[5],
                warnings=json.loads(row[6]),
                parse_errors=json.loads(row[7]),
            )
            for row in conn.execute("SELECT * FROM files")
        ]


def _select_in(
    conn: Any,
    sql_prefix: str,
    ids: Iterable[str],
    sql_suffix: str = "",
    extra: tuple[object, ...] = (),
) -> Iterator[tuple[object, ...]]:
    """``SELECT … WHERE <col> IN (chunk)`` over *ids*, chunked under the host-var cap."""
    id_list = list(dict.fromkeys(ids))  # de-dup, preserve order
    for start in range(0, len(id_list), _IN_CHUNK):
        chunk = id_list[start : start + _IN_CHUNK]
        placeholders = ", ".join("?" for _ in chunk)
        yield from conn.execute(f"{sql_prefix} IN ({placeholders}){sql_suffix}", (*chunk, *extra))
