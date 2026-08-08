"""Clock-domain, reset-tree, and CDC analyses (M5).

Everything here is name-level and evidence-scored — no elaboration:

* **Aliasing.** A formal port and its actual signal are the same net when the
  connection's actual is a single identifier (``.clk(clk_b)`` merges
  ``sub.clk`` with ``top.clk_b``). Union-find over the instance dataflow
  edges the linker derived from resolved CONNECTS bindings gives every net a
  canonical id, so a two-clock design reports two domains, not one per
  module, and crossings are visible through the hierarchy.
* **Domains.** A domain is the alias-root of a CLOCKED_BY target. A process
  belongs to the unique domain its CLOCKED_BY edges name (multi-domain
  processes are skipped as ambiguous); a signal belongs to the domains of
  the processes driving it. Combinational sites (no CLOCKED_BY) bridge one
  step: a signal driven by a combinational process inherits the domains of
  the signals that process reads — a single sweep, no fixpoint (documented
  limitation: a two-deep combinational path hides the origin domain).
* **CDC suspects.** A READS edge from a process in domain B of a signal
  whose (bridged) domain is A ≠ B. Clock nets themselves are exempt.
  Confidence is the minimum along the evidence path; synchronizers are NOT
  recognized — a proper 2-flop sync still shows up, which is why these are
  *suspects* (M10's SDC ``set_clock_groups`` is the planned suppressor).
* **Reset tree.** RESETS edges grouped by alias-root: which nets reset which
  processes, async (1.0, from edge sensitivity) vs name-heuristic (0.4).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from hdl_kgraph.schema import CONFIDENCE_RESOLVED, EdgeKind, NodeKind


@dataclass
class ClockDomain:
    """One clock domain: a canonical clock net and everything on it."""

    clock_id: str  # alias-root node id of the clock net
    clock_names: list[str]  # every aliased name the net goes by
    process_ids: list[str] = field(default_factory=list)
    signal_ids: list[str] = field(default_factory=list)  # driven in this domain
    min_confidence: float = 1.0


@dataclass
class ResetGroup:
    """One reset net and the processes it resets."""

    reset_id: str
    reset_names: list[str]
    is_async: bool  # any 1.0-evidence async sensitivity term
    process_ids: list[str] = field(default_factory=list)
    min_confidence: float = 1.0


@dataclass
class CdcSuspect:
    """A signal driven in one domain and read in another."""

    signal_id: str
    signal_name: str
    file: str
    line: int
    driver_id: str
    driver_domain: str  # representative clock name
    reader_id: str
    reader_domain: str
    confidence: float
    #: True when an SDC constraint (``set_clock_groups -asynchronous`` or a
    #: ``set_false_path`` covering the crossing) declares this crossing safe;
    #: the report suppresses these from the active suspect list (M10).
    declared_safe: bool = False


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        root = x
        while self._parent.get(root, root) != root:
            root = self._parent[root]
        while self._parent.get(x, x) != x:  # path compression
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Deterministic root choice: lexicographically smaller id wins.
            if rb < ra:
                ra, rb = rb, ra
            self._parent[rb] = ra


def _edges(g: nx.MultiDiGraph, *kinds: EdgeKind) -> list[tuple[str, str, dict[str, Any]]]:
    wanted = set(kinds)
    return [(u, v, d) for u, v, d in g.edges(data=True) if d["kind"] in wanted]


def net_aliases(g: nx.MultiDiGraph) -> _UnionFind:
    """Union-find merging formal ports with single-identifier actuals."""
    uf = _UnionFind()
    for inst_id, actual, data in _edges(g, EdgeKind.READS, EdgeKind.DRIVES):
        attrs = data["attrs"]
        if attrs.get("derived") != "connects":
            continue
        expr = str(attrs.get("expr_text", "")).strip()
        actual_name = g.nodes[actual]["name"]
        if expr != actual_name and expr.lower() != actual_name:
            continue  # an expression actual is not a net alias
        formal = _formal_port(g, inst_id, str(attrs.get("via_port", "")))
        if formal is not None:
            uf.union(formal, actual)
    return uf


def _formal_port(g: nx.MultiDiGraph, inst_id: str, port_name: str) -> str | None:
    for _, target, d in g.out_edges(inst_id, data=True):
        if d["kind"] is not EdgeKind.INSTANTIATES:
            continue
        for _, child, dd in g.out_edges(target, data=True):
            if (
                dd["kind"] is EdgeKind.DECLARES
                and g.nodes[child]["kind"] is NodeKind.PORT
                and g.nodes[child]["name"] == port_name
            ):
                return child
    return None


def _clock_net_roots(g: nx.MultiDiGraph, uf: _UnionFind) -> dict[str, set[str]]:
    """CLOCK node id → alias-roots of the PORT/SIGNAL nets it constrains (M10).

    A non-virtual ``create_clock``/``create_generated_clock`` carries a
    CONSTRAINS edge to the net it is defined on; the net's alias-root is the
    domain that clock authoritatively names.
    """
    roots: dict[str, set[str]] = defaultdict(set)
    for clock_id, target, _data in _edges(g, EdgeKind.CONSTRAINS):
        if g.nodes[clock_id]["kind"] is not NodeKind.CLOCK:
            continue
        if g.nodes[target]["kind"] in (NodeKind.PORT, NodeKind.SIGNAL):
            roots[clock_id].add(uf.find(target))
    return roots


def apply_sdc_clock_evidence(g: nx.MultiDiGraph) -> None:
    """Upgrade CLOCKED_BY confidence to 1.0 for clocks an SDC ``create_clock`` names.

    SDC ``create_clock`` is authoritative clock evidence (ROADMAP M10): every
    CLOCKED_BY edge whose clock target shares an alias-root with a ``create_clock``
    net is promoted from the M5 name heuristic (0.4) to 1.0 and stamped
    ``attrs["evidence"] = "sdc_create_clock"``. Mutates *g* in place; a no-op when
    no SDC clocks are present, so it is safe to call on every link.
    """
    uf = net_aliases(g)
    backed: set[str] = set()
    for nets in _clock_net_roots(g, uf).values():
        backed |= nets
    if not backed:
        return
    for _src, clock, data in _edges(g, EdgeKind.CLOCKED_BY):
        if uf.find(clock) in backed and data["confidence"] < CONFIDENCE_RESOLVED:
            data["confidence"] = CONFIDENCE_RESOLVED
            data["attrs"] = {**data["attrs"], "evidence": "sdc_create_clock"}


def _declared_safe_crossings(
    g: nx.MultiDiGraph, uf: _UnionFind
) -> tuple[set[frozenset[str]], set[tuple[str, str]], set[str]]:
    """SDC-declared safe crossings (M10): ``(async clock-root pairs, directional
    false-path clock pairs, false-path net roots)``.

    ``set_clock_groups -asynchronous`` makes every cross-group clock-root pair
    safe in *both* directions (the bare form with no ``-group`` declares all
    clocks mutually asynchronous). A clock-to-clock ``set_false_path`` is
    *directional* — ``-from A -to B`` declares only the A→B crossing safe, not
    B→A — so its pairs are kept ordered and matched against the crossing's
    (driver, reader) direction at the call site. A port/signal ``set_false_path``
    endpoint marks that net's root safe directly.
    """
    clock_roots = _clock_net_roots(g, uf)
    name_roots: dict[str, set[str]] = defaultdict(set)
    for clock_id, nets in clock_roots.items():
        name_roots[g.nodes[clock_id]["name"]] |= nets
    all_clock_roots: set[str] = set().union(*clock_roots.values()) if clock_roots else set()

    async_pairs: set[frozenset[str]] = set()
    false_path_pairs: set[tuple[str, str]] = set()
    false_path_roots: set[str] = set()
    for tc_id, attrs in (
        (n, d["attrs"]) for n, d in g.nodes(data=True) if d["kind"] is NodeKind.TIMING_CONSTRAINT
    ):
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
            for _, tgt, data in g.out_edges(tc_id, data=True):
                if data["kind"] is not EdgeKind.CONSTRAINS:
                    continue
                role = data["attrs"].get("role")
                if g.nodes[tgt]["kind"] is NodeKind.CLOCK:
                    (from_roots if role == "from" else to_roots).update(clock_roots.get(tgt, set()))
                elif g.nodes[tgt]["kind"] in (NodeKind.PORT, NodeKind.SIGNAL):
                    false_path_roots.add(uf.find(tgt))
            false_path_pairs.update((a, b) for a in from_roots for b in to_roots)
    return async_pairs, false_path_pairs, false_path_roots


def clock_domains(g: nx.MultiDiGraph) -> list[ClockDomain]:
    """Every clock domain: nets named by CLOCKED_BY edges, alias-merged."""
    uf = net_aliases(g)
    domains: dict[str, ClockDomain] = {}
    names: dict[str, set[str]] = {}
    for src, clock, data in _edges(g, EdgeKind.CLOCKED_BY):
        root = uf.find(clock)
        domain = domains.setdefault(root, ClockDomain(clock_id=root, clock_names=[]))
        names.setdefault(root, set()).add(g.nodes[clock]["name"])
        if src not in domain.process_ids:
            domain.process_ids.append(src)
        domain.min_confidence = min(domain.min_confidence, data["confidence"])
    for domain in domains.values():
        domain.clock_names = sorted(names[domain.clock_id])
        driven: set[str] = set()
        for proc in domain.process_ids:
            if g.nodes[proc]["kind"] is not NodeKind.PROCESS:
                continue
            for _, sig, d in g.out_edges(proc, data=True):
                if d["kind"] is EdgeKind.DRIVES:
                    driven.add(uf.find(sig))
        domain.signal_ids = sorted(driven)
        domain.process_ids.sort()
    return sorted(domains.values(), key=lambda d: d.clock_names[0])


def reset_tree(g: nx.MultiDiGraph) -> list[ResetGroup]:
    """RESETS edges grouped by canonical reset net."""
    uf = net_aliases(g)
    groups: dict[str, ResetGroup] = {}
    names: dict[str, set[str]] = {}
    for src, reset, data in _edges(g, EdgeKind.RESETS):
        root = uf.find(reset)
        group = groups.setdefault(root, ResetGroup(reset_id=root, reset_names=[], is_async=False))
        names.setdefault(root, set()).add(g.nodes[reset]["name"])
        if src not in group.process_ids:
            group.process_ids.append(src)
        group.is_async = group.is_async or bool(data["attrs"].get("is_async"))
        group.min_confidence = min(group.min_confidence, data["confidence"])
    for group in groups.values():
        group.reset_names = sorted(names[group.reset_id])
        group.process_ids.sort()
    return sorted(groups.values(), key=lambda r: r.reset_names[0])


def cdc_suspects(g: nx.MultiDiGraph) -> list[CdcSuspect]:
    """Signals driven in one domain and read by a process in another.

    A crossing an SDC constraint declares safe (``set_clock_groups
    -asynchronous`` / ``set_false_path``) is still returned, flagged
    ``declared_safe`` — the report partitions it out of the active list (M10).
    """
    uf = net_aliases(g)
    async_pairs, false_path_pairs, false_path_roots = _declared_safe_crossings(g, uf)

    # Process -> its unique domain (root, confidence); ambiguous ones skipped.
    proc_domain: dict[str, tuple[str, float]] = {}
    clock_nets: set[str] = set()
    for proc, clock, data in _edges(g, EdgeKind.CLOCKED_BY):
        if g.nodes[proc]["kind"] is not NodeKind.PROCESS:
            continue
        root = uf.find(clock)
        clock_nets.add(root)
        held = proc_domain.get(proc)
        if held is None:
            proc_domain[proc] = (root, data["confidence"])
        elif held[0] != root:
            proc_domain[proc] = ("", 0.0)  # multi-clock process: ambiguous
    proc_domain = {p: d for p, d in proc_domain.items() if d[0]}

    # Canonical signal -> {domain root: (confidence, driver id)}.
    sig_domain: dict[str, dict[str, tuple[float, str]]] = {}
    reads_of_proc: dict[str, list[tuple[str, float]]] = {}
    drives_of_proc: dict[str, list[tuple[str, float]]] = {}
    for proc, sig, data in _edges(g, EdgeKind.READS):
        if g.nodes[proc]["kind"] is NodeKind.PROCESS:
            reads_of_proc.setdefault(proc, []).append((uf.find(sig), data["confidence"]))
    for proc, sig, data in _edges(g, EdgeKind.DRIVES):
        if g.nodes[proc]["kind"] is not NodeKind.PROCESS:
            continue
        drives_of_proc.setdefault(proc, []).append((uf.find(sig), data["confidence"]))
        domain = proc_domain.get(proc)
        if domain is not None:
            root, dconf = domain
            sig_domains = sig_domain.setdefault(uf.find(sig), {})
            conf = min(dconf, data["confidence"])
            if root not in sig_domains or sig_domains[root][0] < conf:
                sig_domains[root] = (conf, proc)

    # One-step combinational bridge: an undomained process hands the domains
    # of what it reads to what it drives.
    for proc, driven in drives_of_proc.items():
        if proc in proc_domain:
            continue
        inherited: dict[str, tuple[float, str]] = {}
        for read_sig, rconf in reads_of_proc.get(proc, []):
            for root, (conf, driver) in sig_domain.get(read_sig, {}).items():
                merged = min(conf, rconf)
                if root not in inherited or inherited[root][0] < merged:
                    inherited[root] = (merged, driver)
        for sig, dconf in driven:
            sig_domains = sig_domain.setdefault(sig, {})
            for root, (conf, driver) in inherited.items():
                merged = min(conf, dconf)
                if root not in sig_domains or sig_domains[root][0] < merged:
                    sig_domains[root] = (merged, driver)

    suspects: list[CdcSuspect] = []
    for proc, (reader_root, reader_conf) in sorted(proc_domain.items()):
        for sig, rconf in reads_of_proc.get(proc, []):
            if sig in clock_nets:
                continue  # reading a clock net is not a data crossing
            for root, (conf, driver) in sorted(sig_domain.get(sig, {}).items()):
                if root == reader_root:
                    continue
                node = g.nodes[sig]
                # ``root`` is the driver (launch) domain, ``reader_root`` the
                # reader (capture) domain: a directional false path must match
                # the (driver, reader) order; clock groups suppress both ways.
                declared_safe = (
                    frozenset((root, reader_root)) in async_pairs
                    or (root, reader_root) in false_path_pairs
                    or sig in false_path_roots
                )
                suspects.append(
                    CdcSuspect(
                        signal_id=sig,
                        signal_name=node["name"],
                        file=node["file"],
                        line=node["line_span"][0],
                        driver_id=driver,
                        driver_domain=g.nodes[root]["name"],
                        reader_id=proc,
                        reader_domain=g.nodes[reader_root]["name"],
                        confidence=min(conf, rconf, reader_conf),
                        declared_safe=declared_safe,
                    )
                )
    return sorted(suspects, key=lambda s: (s.signal_name, s.reader_id, s.driver_domain))
