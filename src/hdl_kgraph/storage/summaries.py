"""Bounded, SQL-native whole-design summaries (scalability).

The whole-design reports (clock-domain/CDC and UVM topology) scan global relations,
so the build precomputes them once and persists the result
(:mod:`hdl_kgraph.graph.summary`), which the reader serves in well under a millisecond.
This module is the **fallback** for when that persisted summary is absent (a database
migrated from a pre-v8 schema, or any build that could not materialise the whole graph):
it computes the *same* report directly from SQLite, without ever loading the whole graph
into NetworkX.

* :func:`clock_summary_sql` reduces the clock/CDC scan to SQL aggregation (see below).
* :func:`uvm_summary_sql` hydrates only the bounded *class* subgraph (CLASS nodes +
  EXTENDS/TEST_COVERS edges) and reuses the proven :mod:`hdl_kgraph.graph.uvm` functions.

The result is byte-identical to :func:`hdl_kgraph.graph.summary.clock_summary`
— ``tests/test_summaries_sql.py`` pins that parity against the NetworkX oracle.
The key reformulation that makes it possible (validated on a real design in
``scripts/spike_m12_clocks.py``, see ``docs/v2/m12_real_design.md``) is that the
union-find over net aliases assigns each node the lexicographically-smallest id
in its connected component — so reusing :class:`hdl_kgraph.graph.clocks._UnionFind`
over the SQL-derived alias pairs reproduces the oracle's roots exactly, with no
recursive CTE and no write to the read-only connection.

Bounded by design: the ``kind='process'`` filters are pushed into SQL via joins,
and node ``name``/``file``/``line_start`` are fetched only for the small set of
ids actually emitted (suspect signals + domain roots), chunked under the host-var
cap — so RAM tracks the *answer*, not the design.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from typing import Any

import networkx as nx

from hdl_kgraph.graph import clocks, summary, uvm
from hdl_kgraph.schema import Edge, EdgeKind, NodeKind
from hdl_kgraph.storage.sqlite_store import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    add_edge_row,
    add_node_row,
)

#: SQLite caps host parameters per statement; chunk ``IN (...)`` lists under it
#: (mirrors :data:`hdl_kgraph.storage.query._IN_CHUNK`).
_IN_CHUNK = 800


def clock_summary_sql(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ``clock_domains`` tool payload (domains + CDC suspects) from SQLite.

    Byte-identical to :func:`hdl_kgraph.graph.summary.clock_summary`, but reads
    directly from *conn* (a read-only connection) instead of a materialised
    graph. See the module docstring for the bounded-RAM contract.
    """
    uf = _alias_uf(conn)
    find = uf.find  # un-aliased ids resolve to themselves, as in the oracle
    suspects = _cdc_suspects(conn, find)
    active = [s for s in suspects if not s["declared_safe"]]
    suppressed = [s for s in suspects if s["declared_safe"]]
    return {
        "domains": _clock_domains(conn, find),
        "cdc_suspect_count": len(active),
        "cdc_suspects": active[:50],
        "cdc_suppressed_count": len(suppressed),
        "cdc_suppressed": suppressed[:50],
    }


def uvm_summary_sql(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ``uvm_topology`` tool payload (components + TEST_COVERS) from SQLite.

    Byte-identical to :func:`hdl_kgraph.graph.summary.uvm_summary`, but bounded:
    the report only touches CLASS nodes and their EXTENDS chains plus the
    already-persisted TEST_COVERS edges, so it hydrates just that small subgraph
    and runs the *same* :mod:`hdl_kgraph.graph.uvm` functions on it (the
    select-a-bounded-subgraph-then-reuse-the-analysis idiom of
    :mod:`hdl_kgraph.storage.query`), never the whole graph. ``derive_test_covers``
    (the O(design) build-time derivation) is not re-run — its edges are read back.
    """
    graph = nx.MultiDiGraph()
    # Every CLASS node, including the unresolved uvm_* stubs the chain walk reads.
    for row in conn.execute(
        f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind = ?", (NodeKind.CLASS.value,)
    ):
        add_node_row(graph, row)
    # The inheritance + coverage edges, then any endpoint (e.g. a TEST_COVERS tb/
    # DUT module) not already present as a full row — mirrors _ensure_endpoints.
    for row in conn.execute(
        f"SELECT {EDGE_COLUMNS} FROM edges WHERE kind IN (?, ?)",
        (EdgeKind.EXTENDS.value, EdgeKind.TEST_COVERS.value),
    ):
        add_edge_row(graph, row)
    missing = {n for n in graph.nodes if "kind" not in graph.nodes[n]}
    for chunk in _chunks(missing):
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE id IN ({placeholders})", chunk
        ):
            add_node_row(graph, row)
    return {
        "components": summary.jsonable(uvm.uvm_topology(graph)),
        "test_covers": summary.jsonable(uvm.test_covers(graph)),
    }


def power_summary_sql(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ``power_domains`` tool payload (UPF domains) from SQLite.

    Byte-identical to :func:`hdl_kgraph.graph.summary.power_summary`, but bounded:
    the report only touches POWER_DOMAIN nodes and the CONSTRAINS edges binding
    them to their element instances, so it hydrates just that small subgraph and
    runs the *same* :func:`hdl_kgraph.graph.power.power_domains` on it (via
    ``summary.power_summary``), never the whole graph.
    """
    graph = nx.MultiDiGraph()
    for row in conn.execute(
        f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind = ?", (NodeKind.POWER_DOMAIN.value,)
    ):
        add_node_row(graph, row)
    # Only CONSTRAINS edges out of a power domain (SDC clock/timing constraints
    # share the edge kind) plus their instance endpoints.
    for row in conn.execute(
        f"SELECT {EDGE_COLUMNS} FROM edges WHERE kind = ? "
        "AND src IN (SELECT id FROM nodes WHERE kind = ?)",
        (EdgeKind.CONSTRAINS.value, NodeKind.POWER_DOMAIN.value),
    ):
        add_edge_row(graph, row)
    missing = {n for n in graph.nodes if "kind" not in graph.nodes[n]}
    for chunk in _chunks(missing):
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE id IN ({placeholders})", chunk
        ):
            add_node_row(graph, row)
    return summary.power_summary(graph)


def test_covers_sql(conn: sqlite3.Connection) -> list[Edge]:
    """Re-derive the whole TEST_COVERS edge set out-of-core, byte-identically.

    The bounded incremental re-link produces only a src-scoped partial graph, so
    it cannot run :func:`hdl_kgraph.graph.uvm.derive_test_covers` (which reads
    every ``tb_*`` top and ``uvm_test`` class). This recomputes the full set after
    the scoped write by hydrating *only* the structural subgraph that
    ``derive_test_covers``/``uvm_topology`` read — MODULE/ENTITY/INSTANCE/CLASS
    nodes plus DECLARES/INSTANTIATES/EXTENDS edges, never the dataflow bulk — and
    running the *same* function on it. Because that function never touches
    dataflow, its output is identical to running it on the full graph. Bounded by
    the structural subgraph (the same notion of "bounded" as the SQL summaries).
    """
    graph = nx.MultiDiGraph()
    for row in conn.execute(
        f"SELECT {NODE_COLUMNS} FROM nodes WHERE kind IN (?, ?, ?, ?)",
        (
            NodeKind.MODULE.value,
            NodeKind.ENTITY.value,
            NodeKind.INSTANCE.value,
            NodeKind.CLASS.value,
        ),
    ):
        add_node_row(graph, row)
    for row in conn.execute(
        f"SELECT {EDGE_COLUMNS} FROM edges WHERE kind IN (?, ?, ?)",
        (EdgeKind.DECLARES.value, EdgeKind.INSTANTIATES.value, EdgeKind.EXTENDS.value),
    ):
        add_edge_row(graph, row)
    # Any edge endpoint not already a full row (defensive — mirrors uvm_summary_sql).
    missing = {n for n in graph.nodes if "kind" not in graph.nodes[n]}
    for chunk in _chunks(missing):
        placeholders = ", ".join("?" for _ in chunk)
        for row in conn.execute(
            f"SELECT {NODE_COLUMNS} FROM nodes WHERE id IN ({placeholders})", chunk
        ):
            add_node_row(graph, row)
    return uvm.derive_test_covers(graph)


# --------------------------------------------------------------------------- #
# Alias components (union-find ≡ transitive closure + MIN reachable id)
# --------------------------------------------------------------------------- #
def _alias_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(formal_port_id, actual_id) pairs, mirroring ``clocks.net_aliases`` exactly.

    A formal port and its actual signal are the same net when a derived
    READS/DRIVES connection's actual is a single identifier equal to the actual
    node's name (VHDL casefold included). The join walks INSTANTIATES → DECLARES
    to the formal PORT named by ``via_port``.
    """
    conn.create_function("strip", 1, lambda s: s.strip() if s else None)
    return conn.execute(
        """
        WITH a AS (
          SELECT e.src inst, e.dst actual,
                 json_extract(e.attrs,'$.expr_text') expr,
                 json_extract(e.attrs,'$.via_port')  via
          FROM edges e
          WHERE e.kind IN (?, ?) AND json_extract(e.attrs,'$.derived')='connects'
        ),
        matched AS (
          SELECT a.inst, a.actual, a.via, na.name actual_name
          FROM a JOIN nodes na ON na.id=a.actual
          WHERE a.expr IS NOT NULL AND (strip(a.expr)=na.name OR lower(strip(a.expr))=na.name)
        )
        SELECT p.id formal, m.actual
        FROM matched m
        JOIN edges i ON i.src=m.inst AND i.kind=?
        JOIN edges d ON d.src=i.dst AND d.kind=?
        JOIN nodes p ON p.id=d.dst AND p.kind=? AND p.name=m.via
        """,
        (
            EdgeKind.READS.value,
            EdgeKind.DRIVES.value,
            EdgeKind.INSTANTIATES.value,
            EdgeKind.DECLARES.value,
            NodeKind.PORT.value,
        ),
    ).fetchall()


def _alias_uf(conn: sqlite3.Connection) -> clocks._UnionFind:
    """Union-find over the alias pairs; reuses the oracle's lex-min root choice."""
    uf = clocks._UnionFind()
    for formal, actual in _alias_pairs(conn):
        uf.union(formal, actual)
    return uf


# --------------------------------------------------------------------------- #
# clock_domains
# --------------------------------------------------------------------------- #
def _clock_domains(conn: sqlite3.Connection, find: Any) -> list[dict[str, Any]]:
    """Domains keyed by alias-root, mirroring ``clocks.clock_domains`` + the
    ``summary.clock_summary`` shaping (names stripped to counts upstream)."""
    names: dict[str, set[str]] = defaultdict(set)
    process_ids: dict[str, list[str]] = defaultdict(list)
    min_conf: dict[str, float] = defaultdict(lambda: 1.0)
    for src, clock, conf, clock_name in conn.execute(
        "SELECT e.src, e.dst, e.confidence, n.name FROM edges e "
        "JOIN nodes n ON n.id = e.dst WHERE e.kind = ?",
        (EdgeKind.CLOCKED_BY.value,),
    ):
        root = find(clock)
        names[root].add(clock_name)
        if src not in process_ids[root]:  # dedup, preserve order (oracle parity)
            process_ids[root].append(src)
        min_conf[root] = min(min_conf[root], conf)

    # signal_count: driven nets of the PROCESS-kind procs only (the rest of the
    # process_ids list counts toward process_count but drives nothing relevant).
    all_procs = {p for procs in process_ids.values() for p in procs}
    proc_kind = _kinds_of(conn, all_procs)
    domains: list[dict[str, Any]] = []
    for root, aliases_set in names.items():
        driven: set[str] = set()
        for proc in process_ids[root]:
            if proc_kind.get(proc) != NodeKind.PROCESS.value:
                continue
            for (sig,) in conn.execute(
                "SELECT dst FROM edges WHERE kind = ? AND src = ?",
                (EdgeKind.DRIVES.value, proc),
            ):
                driven.add(find(sig))
        aliases = sorted(aliases_set)
        domains.append(
            {
                "clock": aliases[0],
                "aliases": aliases,
                "process_count": len(process_ids[root]),
                "signal_count": len(driven),
                "min_confidence": min_conf[root],
            }
        )
    domains.sort(key=lambda d: d["aliases"][0])
    return domains


# --------------------------------------------------------------------------- #
# reset_tree
# --------------------------------------------------------------------------- #
def reset_summary_sql(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """``clocks.reset_tree`` from SQLite — RESETS edges grouped by canonical reset
    net, byte-identical to the NetworkX oracle (reuses the same net-alias
    union-find). Bounded: scans only RESETS edges + the alias pairs, never the
    whole graph. Keys match ``ResetGroup`` field order so the CLI ``--json`` is
    identical to the dataclass path."""
    find = _alias_uf(conn).find
    names: dict[str, set[str]] = defaultdict(set)
    process_ids: dict[str, list[str]] = defaultdict(list)
    is_async: dict[str, bool] = defaultdict(bool)
    min_conf: dict[str, float] = defaultdict(lambda: 1.0)
    for src, reset, conf, async_flag, reset_name in conn.execute(
        "SELECT e.src, e.dst, e.confidence, json_extract(e.attrs, '$.is_async'), n.name "
        "FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.kind = ?",
        (EdgeKind.RESETS.value,),
    ):
        root = find(reset)
        names[root].add(reset_name)
        if src not in process_ids[root]:  # dedup, preserve order (oracle parity)
            process_ids[root].append(src)
        is_async[root] = is_async[root] or bool(async_flag)
        min_conf[root] = min(min_conf[root], conf)
    groups: list[dict[str, Any]] = [
        {
            "reset_id": root,
            "reset_names": sorted(nameset),
            "is_async": is_async[root],
            "process_ids": sorted(process_ids[root]),
            "min_confidence": min_conf[root],
        }
        for root, nameset in names.items()
    ]
    groups.sort(key=lambda g: g["reset_names"][0])
    return groups


# --------------------------------------------------------------------------- #
# cdc_suspects (the combinational-bridge logic, mirroring clocks.cdc_suspects)
# --------------------------------------------------------------------------- #
def _sdc_clock_roots(
    conn: sqlite3.Connection, find: Any
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """``(clock_id -> net alias-roots, clock_id -> name)`` from CLOCK CONSTRAINS edges
    (M10), mirroring :func:`hdl_kgraph.graph.clocks._clock_net_roots`."""
    id_roots: dict[str, set[str]] = defaultdict(set)
    id_name: dict[str, str] = {}
    for cid, name, dst, dkind in conn.execute(
        "SELECT e.src, sn.name, e.dst, dn.kind FROM edges e "
        "JOIN nodes sn ON sn.id = e.src JOIN nodes dn ON dn.id = e.dst "
        "WHERE e.kind = ? AND sn.kind = ?",
        (EdgeKind.CONSTRAINS.value, NodeKind.CLOCK.value),
    ):
        if dkind in (NodeKind.PORT.value, NodeKind.SIGNAL.value):
            id_roots[cid].add(find(dst))
            id_name[cid] = name
    return id_roots, id_name


def _declared_safe_crossings(
    conn: sqlite3.Connection, find: Any
) -> tuple[set[frozenset[str]], set[tuple[str, str]], set[str]]:
    """SDC-declared safe crossings from SQLite (M10), mirroring
    :func:`hdl_kgraph.graph.clocks._declared_safe_crossings`: ``(async clock-root
    pairs, directional false-path clock pairs, false-path net roots)``. Clock
    groups suppress both directions; a clock-to-clock ``set_false_path`` is
    directional (``-from A -to B`` declares only A→B safe)."""
    id_roots, id_name = _sdc_clock_roots(conn, find)
    name_roots: dict[str, set[str]] = defaultdict(set)
    for cid, roots in id_roots.items():
        name_roots[id_name[cid]] |= roots
    all_clock_roots: set[str] = set().union(*id_roots.values()) if id_roots else set()

    async_pairs: set[frozenset[str]] = set()
    false_path_pairs: set[tuple[str, str]] = set()
    false_path_roots: set[str] = set()
    for tc_id, attrs_json in conn.execute(
        "SELECT id, attrs FROM nodes WHERE kind = ?", (NodeKind.TIMING_CONSTRAINT.value,)
    ).fetchall():
        attrs = json.loads(attrs_json) if attrs_json else {}
        set_type = attrs.get("set_type")
        if set_type == "clock_groups" and attrs.get("asynchronous"):
            groups = [
                set().union(*(name_roots.get(name, set()) for name in grp)) if grp else set()
                for grp in (attrs.get("groups") or [])
            ]
            if not groups:
                # Bare ``-asynchronous`` (no -group): all clocks mutually async.
                groups = [{root} for root in sorted(all_clock_roots)]
            for i in range(len(groups)):
                for j in range(i + 1, len(groups)):
                    async_pairs.update(frozenset((a, b)) for a in groups[i] for b in groups[j])
        elif set_type == "false_path":
            from_roots: set[str] = set()
            to_roots: set[str] = set()
            for dst, dkind, role in conn.execute(
                "SELECT e.dst, dn.kind, json_extract(e.attrs, '$.role') FROM edges e "
                "JOIN nodes dn ON dn.id = e.dst WHERE e.kind = ? AND e.src = ?",
                (EdgeKind.CONSTRAINS.value, tc_id),
            ):
                if dkind == NodeKind.CLOCK.value:
                    (from_roots if role == "from" else to_roots).update(id_roots.get(dst, set()))
                elif dkind in (NodeKind.PORT.value, NodeKind.SIGNAL.value):
                    false_path_roots.add(find(dst))
            false_path_pairs.update((a, b) for a in from_roots for b in to_roots)
    return async_pairs, false_path_pairs, false_path_roots


def _cdc_suspects(conn: sqlite3.Connection, find: Any) -> list[dict[str, Any]]:
    """CDC suspects (a signal driven in one domain and read in another), mirroring
    ``clocks.cdc_suspects``: per-process domains, the one-step combinational bridge,
    then the crossing list sorted by ``(signal_name, reader_id, driver_domain)``.
    Each suspect carries ``declared_safe`` (SDC-suppressed crossing, M10)."""
    async_pairs, false_path_pairs, false_path_roots = _declared_safe_crossings(conn, find)
    # Process -> its unique domain (root, confidence); ambiguous ones skipped.
    proc_domain: dict[str, tuple[str, float]] = {}
    clock_nets: set[str] = set()
    for proc, clock, conf in _process_edges(conn, EdgeKind.CLOCKED_BY):
        root = find(clock)
        clock_nets.add(root)
        held = proc_domain.get(proc)
        if held is None:
            proc_domain[proc] = (root, conf)
        elif held[0] != root:
            proc_domain[proc] = ("", 0.0)  # multi-clock process: ambiguous
    proc_domain = {p: d for p, d in proc_domain.items() if d[0]}

    sig_domain: dict[str, dict[str, tuple[float, str]]] = defaultdict(dict)
    reads: dict[str, list[tuple[str, float]]] = defaultdict(list)
    drives: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for proc, sig, conf in _process_edges(conn, EdgeKind.READS):
        reads[proc].append((find(sig), conf))
    for proc, sig, conf in _process_edges(conn, EdgeKind.DRIVES):
        drives[proc].append((find(sig), conf))
        domain = proc_domain.get(proc)
        if domain is not None:
            root, dconf = domain
            sig_domains = sig_domain[find(sig)]
            conf_m = min(dconf, conf)
            if root not in sig_domains or sig_domains[root][0] < conf_m:
                sig_domains[root] = (conf_m, proc)

    # One-step combinational bridge: an undomained process hands the domains of
    # what it reads to what it drives (a single sweep, no fixpoint).
    for proc, driven_list in drives.items():
        if proc in proc_domain:
            continue
        inherited: dict[str, tuple[float, str]] = {}
        for read_sig, rconf in reads.get(proc, []):
            for root, (conf, driver) in sig_domain.get(read_sig, {}).items():
                merged = min(conf, rconf)
                if root not in inherited or inherited[root][0] < merged:
                    inherited[root] = (merged, driver)
        for sig, dconf in driven_list:
            sig_domains = sig_domain[sig]
            for root, (conf, driver) in inherited.items():
                merged = min(conf, dconf)
                if root not in sig_domains or sig_domains[root][0] < merged:
                    sig_domains[root] = (merged, driver)

    # Build suspects as id-tuples, then fetch name/file/line for just the ids we
    # emit (bounded) before shaping + sorting.
    raw: list[tuple[str, str, str, str, float]] = []  # sig, driver, driver_root, reader, conf
    for proc, (reader_root, reader_conf) in sorted(proc_domain.items()):
        for sig, rconf in reads.get(proc, []):
            if sig in clock_nets:
                continue  # reading a clock net is not a data crossing
            for root, (conf, driver) in sorted(sig_domain.get(sig, {}).items()):
                if root == reader_root:
                    continue
                raw.append((sig, driver, root, proc, min(conf, rconf, reader_conf)))

    # Attrs are needed only for the suspect signal (name/file/line), the driver
    # domain root and the reader domain root (names) — a bounded set.
    need = (
        {sig for sig, *_ in raw}
        | {root for _s, _d, root, *_ in raw}
        | {proc_domain[proc][0] for *_, proc, _c in raw}
    )
    attrs = _node_attrs(conn, need)
    suspects = [
        {
            "signal_id": sig,
            "signal_name": attrs[sig][0],
            "file": attrs[sig][1],
            "line": attrs[sig][2],
            "driver_id": driver,
            "driver_domain": attrs[root][0],
            "reader_id": proc,
            "reader_domain": attrs[proc_domain[proc][0]][0],
            "confidence": conf,
            "declared_safe": (
                frozenset((root, proc_domain[proc][0])) in async_pairs
                or (root, proc_domain[proc][0]) in false_path_pairs
                or sig in false_path_roots
            ),
        }
        for sig, driver, root, proc, conf in raw
    ]
    suspects.sort(key=lambda s: (s["signal_name"], s["reader_id"], s["driver_domain"]))
    return suspects


# --------------------------------------------------------------------------- #
# bounded node-attribute fetches
# --------------------------------------------------------------------------- #
def _process_edges(conn: sqlite3.Connection, kind: EdgeKind) -> Iterator[tuple[str, str, float]]:
    """``(src, dst, confidence)`` for edges of *kind* whose src is a PROCESS node.

    Pushes the ``kind='process'`` filter into SQL (a join), so the oracle's
    ``if g.nodes[proc]["kind"] is not PROCESS: continue`` never needs every
    node's kind loaded into memory.
    """
    yield from conn.execute(
        "SELECT e.src, e.dst, e.confidence FROM edges e JOIN nodes n ON n.id = e.src "
        "WHERE e.kind = ? AND n.kind = ?",
        (kind.value, NodeKind.PROCESS.value),
    )


def _kinds_of(conn: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """``id -> kind`` for *ids* (chunked under the host-var cap)."""
    out: dict[str, str] = {}
    for chunk in _chunks(ids):
        placeholders = ", ".join("?" for _ in chunk)
        out.update(conn.execute(f"SELECT id, kind FROM nodes WHERE id IN ({placeholders})", chunk))
    return out


def _node_attrs(conn: sqlite3.Connection, ids: set[str]) -> dict[str, tuple[str, str, int]]:
    """``id -> (name, file, line_start)`` for *ids* (chunked)."""
    out: dict[str, tuple[str, str, int]] = {}
    for chunk in _chunks(ids):
        placeholders = ", ".join("?" for _ in chunk)
        for nid, name, file, line in conn.execute(
            f"SELECT id, name, file, line_start FROM nodes WHERE id IN ({placeholders})", chunk
        ):
            out[nid] = (name, file, line)
    return out


def _chunks(ids: set[str]) -> Iterator[list[str]]:
    """Yield *ids* in lists no larger than the SQLite host-var cap (:data:`_IN_CHUNK`)."""
    seq = list(ids)
    for start in range(0, len(seq), _IN_CHUNK):
        yield seq[start : start + _IN_CHUNK]
