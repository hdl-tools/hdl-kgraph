"""Content-free review digest (``hdl-kgraph review``).

Assembles a single JSON digest of a built graph that is safe to copy out of an
isolated/air-gapped environment: it contains **only counts, ratios, distributions
and timings — never identifiers** (no module/clock/signal names, file paths, or
expression text). The intended workflow is to snapshot the digest per build and
*diff* snapshots to review regressions (parse health, link quality, design shape,
performance) without access to the source or the ``graph.db``.

Every field is sourced from existing surfaces — the ``meta``/``files`` tables, the
node/edge histograms (:mod:`hdl_kgraph.graph.analysis`), the persisted whole-design
``summaries`` (:mod:`hdl_kgraph.graph.summary`), optional metrics
(:mod:`hdl_kgraph.graph.metrics`), and the build-time ``build_stats`` meta row — but
this module strips them down to non-identifying aggregates. The single guarantee the
test suite enforces is that no identifier from the design appears in the output.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import networkx as nx

from hdl_kgraph.graph import analysis, metrics, summary
from hdl_kgraph.schema import Language
from hdl_kgraph.storage.sqlite_store import FileMeta

#: digest schema identifier (bump on breaking field changes).
REVIEW_SCHEMA = "hdl-kgraph.review/1"


def _corpus(files: list[FileMeta]) -> dict[str, Any]:
    """File-level counts and parse-health totals (no paths) from the ``files`` table."""
    parsed = [f for f in files if f.skipped_reason is None and f.language is not Language.UNKNOWN]
    filelists = [f for f in files if f.skipped_reason is None and f.language is Language.UNKNOWN]
    error_files = [f for f in parsed if f.parse_error_count]
    return {
        "files_total": len(files),
        "files_parsed": len(parsed),
        "filelists": len(filelists),
        "files_skipped": sum(1 for f in files if f.skipped_reason is not None),
        "languages": dict(Counter(f.language.value for f in parsed)),
        "parse_error_count": sum(f.parse_error_count for f in error_files),
        "files_with_errors": len(error_files),
        "preprocessor_warnings": sum(len(f.warnings) for f in files),
    }


def _clock_counts(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the clock-domain summary to counts (drop clock/signal *names*)."""
    domains = payload.get("domains", [])
    return {
        "clock_domains": {
            "count": len(domains),
            "per_domain": [
                {
                    "alias_count": len(d.get("aliases", [])),
                    "process_count": d.get("process_count", 0),
                    "signal_count": d.get("signal_count", 0),
                    "min_confidence": d.get("min_confidence"),
                }
                for d in domains
            ],
        },
        "cdc": {
            "suspect_count": payload.get("cdc_suspect_count", 0),
            "suppressed_count": payload.get("cdc_suppressed_count", 0),
        },
    }


def _uvm_counts(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the UVM-topology summary to component/test-cover counts (drop names)."""
    return {
        "uvm": {
            "component_count": len(payload.get("components", [])),
            "test_covers_count": len(payload.get("test_covers", [])),
        }
    }


def _power_counts(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip the power-domain summary to counts (drop domain/element *names*)."""
    return {
        "power": {
            "domain_count": payload.get("domain_count", 0),
            "isolated_count": payload.get("isolated_count", 0),
        }
    }


def _analyses(
    graph: nx.MultiDiGraph,
    clock_payload: str | None,
    uvm_payload: str | None,
    power_payload: str | None = None,
) -> dict:
    """Clock/CDC/UVM/power analysis counts. Prefers the persisted summaries (bounded
    read); falls back to computing them from the loaded graph. Either way: counts only."""
    clock = json.loads(clock_payload) if clock_payload else summary.clock_summary(graph)
    uvm = json.loads(uvm_payload) if uvm_payload else summary.uvm_summary(graph)
    power = json.loads(power_payload) if power_payload else summary.power_summary(graph)
    return {**_clock_counts(clock), **_uvm_counts(uvm), **_power_counts(power)}


def _metrics(graph: nx.MultiDiGraph, top_n: int = 5) -> dict[str, Any]:
    """Graph metrics as values only — module *names* omitted."""
    result = metrics.module_metrics(graph)
    fan_in = sorted((m.fan_in for m in result.modules), reverse=True)[:top_n]
    return {
        "module_count": len(result.modules),
        "top_fan_in": fan_in,
        "hub_betweenness_max": max((m.betweenness for m in result.modules), default=0.0),
        "betweenness_approximate": result.betweenness_approximate,
        "articulation_point_count": sum(1 for m in result.modules if m.is_articulation),
        "community_count": len(metrics.communities(graph)),
    }


def build_review_digest(
    graph: nx.MultiDiGraph,
    files: list[FileMeta],
    meta: dict[str, str],
    *,
    db_bytes: int | None = None,
    clock_summary_payload: str | None = None,
    uvm_summary_payload: str | None = None,
    power_summary_payload: str | None = None,
    with_metrics: bool = False,
) -> dict[str, Any]:
    """Assemble the content-free review digest. Pure (no I/O); the CLI supplies the
    loaded graph/files/meta, the persisted summary payloads, and the DB size."""
    build_stats = json.loads(meta["build_stats"]) if meta.get("build_stats") else None

    edge_conf: Counter[str] = Counter()
    for _u, _v, data in graph.edges(data=True):
        edge_conf[f"{data['confidence']:g}"] += 1

    node_count = graph.number_of_nodes()
    unresolved = sum(1 for _n, d in graph.nodes(data=True) if d["attrs"].get("unresolved"))

    digest: dict[str, Any] = {
        "schema": REVIEW_SCHEMA,
        "meta": {
            # NOTE: 'root' (a filesystem path) is deliberately omitted — content-free.
            "tool_version": meta.get("tool_version"),
            "schema_version": meta.get("schema_version"),
            "built_at": meta.get("built_at"),
            "options_hash": meta.get("options_hash"),
            "enriched": bool(build_stats.get("enriched")) if build_stats else None,
        },
        "corpus": {**_corpus(files), "db_bytes": db_bytes},
        "graph": {
            "node_count": node_count,
            "edge_count": graph.number_of_edges(),
            "node_kinds": dict(analysis.node_kind_histogram(graph)),
            "edge_kinds": dict(analysis.edge_kind_histogram(graph)),
        },
        "link_quality": {
            "unresolved_stub_count": unresolved,
            "unresolved_stub_ratio": round(unresolved / node_count, 6) if node_count else 0.0,
            "edge_confidence_distribution": dict(sorted(edge_conf.items(), reverse=True)),
        },
        "analyses": _analyses(
            graph, clock_summary_payload, uvm_summary_payload, power_summary_payload
        ),
        "timings_s": (
            {k: build_stats[k] for k in build_stats if k.endswith("_s")} if build_stats else None
        ),
    }
    if with_metrics:
        digest["analyses"]["metrics"] = _metrics(graph)
    return digest
