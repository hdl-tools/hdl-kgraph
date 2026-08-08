"""Whole-design summaries, precomputed at build time (scalability).

Clock-domain/CDC and UVM-topology reports are genuinely global — they scan
every CLOCKED_BY/DRIVES/READS/EXTENDS edge — so, unlike the structural queries,
they cannot be answered from a bounded subgraph. Instead the build computes
them once (the full graph is already in memory there) and persists the result;
the MCP tools then read a small JSON blob rather than re-loading the whole
graph per call. The functions here shape exactly the dicts those tools return,
so :mod:`hdl_kgraph.mcp.server` and the persisted summary share one source.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Any

import networkx as nx

from hdl_kgraph.graph import clocks, power, uvm


def jsonable(value: Any) -> Any:
    """Recursively convert enums/dataclasses/tuples to JSON-safe values."""
    if isinstance(value, enum.Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((jsonable(v) for v in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def clock_summary(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """The ``clock_domains`` tool payload: domains plus CDC suspects."""
    domains = [
        {
            "clock": d.clock_names[0] if d.clock_names else d.clock_id,
            "aliases": d.clock_names,
            "process_count": len(d.process_ids),
            "signal_count": len(d.signal_ids),
            "min_confidence": d.min_confidence,
        }
        for d in clocks.clock_domains(graph)
    ]
    suspects = clocks.cdc_suspects(graph)
    active = [s for s in suspects if not s.declared_safe]
    suppressed = [s for s in suspects if s.declared_safe]
    return {
        "domains": domains,
        "cdc_suspect_count": len(active),
        "cdc_suspects": jsonable(active[:50]),
        # Crossings an SDC constraint declares safe — reported, not silently
        # dropped, so a suppressed crossing stays visible (M10).
        "cdc_suppressed_count": len(suppressed),
        "cdc_suppressed": jsonable(suppressed[:50]),
    }


def power_summary(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """The ``power_domains`` tool payload: UPF domains, elements, strategies (M10)."""
    domains = power.power_domains(graph)
    return {
        "domain_count": len(domains),
        "isolated_count": sum(1 for d in domains if d.isolated),
        "domains": jsonable(domains[:50]),
    }


def uvm_summary(graph: nx.MultiDiGraph) -> dict[str, Any]:
    """The ``uvm_topology`` tool payload: components and TEST_COVERS links."""
    return {
        "components": jsonable(uvm.uvm_topology(graph)),
        "test_covers": jsonable(uvm.test_covers(graph)),
    }


#: name -> builder, the single registry the build iterates and the reader keys on.
BUILDERS = {
    "clock_domains": clock_summary,
    "power_domains": power_summary,
    "uvm_topology": uvm_summary,
}


def build_summaries(graph: nx.MultiDiGraph) -> dict[str, dict[str, Any]]:
    """Compute every persisted whole-design summary from *graph*.

    The result is passed straight to ``json.dumps`` by the pipeline, so run it
    through :func:`jsonable` here — one place — rather than trusting every
    builder to return only JSON-native types.
    """
    return {name: jsonable(builder(graph)) for name, builder in BUILDERS.items()}
