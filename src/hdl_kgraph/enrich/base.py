"""Enrichment-backend interface (M7).

The tree-sitter tier is *syntactic*: it sees one ``hierarchical_instance`` per
instantiation, so a ``generate for`` loop or an instance array collapses to a
single ``INSTANCE`` node, parameter overrides stay unevaluated, and ambiguous
cross-file names resolve only by heuristic. An *enrichment backend* runs a
native HDL frontend (pyslang for SystemVerilog, GHDL/pyVHDLModel for VHDL) that
genuinely elaborates the design — resolving parameters, unrolling generates,
applying ``defparam`` — and feeds the result back as a strict *overlay* on the
heuristic graph.

Design notes (see ROADMAP.md "M7"):

* Elaboration is a **whole-design** operation (it needs every file, the top
  modules, and the defines at once), so a backend is *not* a per-file
  :class:`~hdl_kgraph.parser.base.ParserBackend`. It runs once, after pass-2
  linking, over the full :class:`~hdl_kgraph.config.BuildOptions` inputs and
  the already-linked graph.
* A backend returns *deltas* (:class:`EnrichmentResult`), never a replacement
  graph. Tree-sitter stays the baseline; if the backend is missing, fails, or
  cannot elaborate part of the design, the heuristic graph is preserved and the
  failure surfaces as a diagnostic.
* Results merge by confidence: a confirmed heuristic edge is upgraded to ``1.0``
  and stamped ``attrs["source"] = "elaborated"`` (see :mod:`hdl_kgraph.schema`);
  a disagreement becomes a :class:`Discrepancy` rather than a silent overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import networkx as nx

from hdl_kgraph.schema import CONFIDENCE_RESOLVED, Edge, EdgeKind, Node


@dataclass
class EnrichmentInput:
    """The whole-design inputs an elaboration backend needs.

    *files* are absolute paths to **raw** source (a backend runs its own
    preprocessor, so it must not be handed the tree-sitter-expanded text).
    *base* is the build root, for mapping elaborated source locations back to
    the root-relative paths node ids are keyed on.
    """

    files: list[Path] = field(default_factory=list)
    defines: dict[str, str | None] = field(default_factory=dict)
    incdirs: list[Path] = field(default_factory=list)
    tops: list[str] = field(default_factory=list)
    base: Path = field(default_factory=Path)
    #: VHDL is analysed library-scoped; maps a root-relative source path to the
    #: library it compiles into (``work`` by default). Empty for SV-only inputs;
    #: the slang backend ignores it.
    vhdl_libraries: dict[str, str] = field(default_factory=dict)


@dataclass
class Capabilities:
    """What a backend can derive — declared so callers (and the discrepancy
    report) know which facts to trust over the heuristic baseline."""

    resolves_params: bool = False
    unrolls_generates: bool = False
    resolves_types: bool = False
    resolves_defparam: bool = False


@dataclass
class EdgeUpgrade:
    """Promote an existing heuristic edge to elaboration confidence.

    Matched against the graph by ``(src, dst, kind)``; *attrs* are merged over
    the edge's existing attrs (provenance stamps live here).
    """

    src: str
    dst: str
    kind: EdgeKind
    confidence: float = CONFIDENCE_RESOLVED
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Discrepancy:
    """One place the heuristic graph disagreed with elaborated reality."""

    kind: str  # "instance_count" | "wrong_target" | "missing_edge" | "extra_edge"
    backend: str
    detail: str
    node_id: str = ""
    src: str = ""
    dst: str = ""
    heuristic: str = ""
    elaborated: str = ""


@dataclass
class EnrichmentResult:
    """A backend's delta over the heuristic graph (see module docstring)."""

    new_nodes: list[Node] = field(default_factory=list)
    new_edges: list[Edge] = field(default_factory=list)
    upgrades: list[EdgeUpgrade] = field(default_factory=list)
    # node id -> attrs merged onto an existing node (e.g. elaborated_count).
    node_annotations: dict[str, dict[str, Any]] = field(default_factory=dict)
    discrepancies: list[Discrepancy] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


@dataclass
class EnrichmentReport:
    """Aggregate of one enrichment pass, surfaced in the build report."""

    backends: list[str] = field(default_factory=list)
    edges_upgraded: int = 0
    nodes_added: int = 0
    discrepancies: list[Discrepancy] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    #: Per-phase wall-clock seconds for the pass (see :mod:`._profile`); a
    #: top-level span name tiles the pass, a ``parent/child`` name details one.
    phase_timings: dict[str, float] = field(default_factory=dict)
    #: Integer tallies recorded during the pass (e.g. ``walk_instances``), so a
    #: phase's cost can be normalized per unit of work.
    phase_counts: dict[str, int] = field(default_factory=dict)


class EnrichmentBackend(Protocol):
    """A whole-design elaboration backend (pass-3 enrichment)."""

    #: Stable short name, recorded on upgraded edges and discrepancies.
    name: str
    #: Source suffixes this backend elaborates (the runner filters the input).
    suffixes: frozenset[str]

    def available(self) -> bool:
        """Whether the backend can run (its dependency/binary is present)."""
        ...

    def capabilities(self) -> Capabilities:
        """The facts this backend derives."""
        ...

    def enrich(self, inp: EnrichmentInput, graph: nx.MultiDiGraph) -> EnrichmentResult:
        """Elaborate *inp* and return a delta over *graph*. Must not raise on
        an un-elaboratable design — degrade and report via diagnostics."""
        ...
