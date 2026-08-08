"""Graph builder: pass-2 linker (M1).

Consumes per-file IRs from the parser backends and produces the global
knowledge graph as a NetworkX ``MultiDiGraph`` (persisted via
:mod:`hdl_kgraph.storage.sqlite_store`).

Resolution and confidence (see :mod:`hdl_kgraph.schema`):

* a candidate in the same file resolves at 1.0; a unique cross-file match at
  0.8; multiple candidates get one 0.6 edge each — unless exactly one is in
  the referring file, or (both-branches mode) exactly one is outside a
  non-selected ``\\`ifdef`` arm, in which case it wins at full confidence
* a name with no definition gets a stub node (``attrs["unresolved"] = True``)
  shared by every referrer. The *edge* to a stub keeps confidence 1.0 — the
  reference itself is syntactically certain; the stub node carries the
  uncertainty.
* CONNECTS/PARAMETERIZES resolve against the target's PORT/PARAMETER children
  (positional bindings via declaration-order ``attrs["index"]``) at the
  instantiation confidence; bindings that match no declared child of a
  resolved target fall back to an edge to the target itself at <= 0.6 so the
  mismatch stays visible.

Graph conventions: nodes are keyed by :class:`~hdl_kgraph.schema.Node` id and
carry ``kind``/``name``/``qualified_name``/``file``/``line_span``/
``language``/``attrs`` as data; edges carry ``kind``/``confidence``/``attrs``.

M3 — VHDL and mixed-language linking:

* Same-language candidates are tried first under the existing rules. When a
  name finds none, a **case-insensitive cross-language fallback** runs (VHDL
  instantiation → SV MODULE; SV instantiation → VHDL ENTITY) capped at
  ``CONFIDENCE_UNIQUE_MATCH`` (0.8) — a name match across languages is never
  syntactic, even within one file. Vendor tools may bind cross-language names
  differently (case folding, library prefixes, extended/escaped
  identifiers); the ≤0.8 confidence is the honest contract.
* VHDL→VHDL resolution prefers candidates whose ``attrs["library"]`` matches
  the reference's library (``work`` resolves to the *referrer's* library).
* BINDS refs resolve **first**: each CONFIGURATION's component bindings are
  recorded, then a component-style instantiation inside a configured
  entity/architecture resolves through the matching binding (specific label
  beats ``all`` beats ``others``) with ``attrs["bound_by"]`` naming the
  configuration. Without a binding, default binding applies: a like-named
  entity, then the cross-language fallback. Direct-entity instantiation
  resolves normally; configuration instantiation resolves to the named
  CONFIGURATION's configured entity (``attrs["via_configuration"]``).
* CONNECTS/PARAMETERIZES port and generic names match case-insensitively
  when either side is VHDL.

M5 — dataflow, clocks, and verification refs:

* DRIVES/READS/CLOCKED_BY/RESETS/ASSERTS_ON/COVERS refs are **scoped**: the
  name resolves against the referring node's *enclosing design unit's*
  PORT/SIGNAL children (an ARCHITECTURE also searches its entity's ports),
  never by global name. A unique match in scope resolves at 1.0 (× the ref's
  own evidence confidence); multiple matches follow the global ambiguity
  contract (non-``conditional`` declaration wins, a non-ANSI port
  redeclaration resolves to the PORT, a genuine tie emits every candidate
  at 0.6). Names matching a PARAMETER/ENUM_MEMBER (or any other
  non-signal child) are dropped — constants are not dataflow. Unmatched
  names become SIGNAL stub children of the unit at ≤ 0.6, so implicit nets
  and typos stay visible. ASSERTS_ON prefers a sibling PROPERTY/SEQUENCE
  (``assert property (p_handshake)``) before falling back to signals. A ref
  with no enclosing unit (e.g. a covergroup inside a class) is dropped.
* Resolved CONNECTS bindings additionally derive instance-level dataflow
  from the formal port's direction: ``INSTANCE --READS--> actual`` for
  inputs, ``--DRIVES-->`` for outputs/buffers, both (≤ 0.6) for inouts.
  Actual-expression identifiers are re-lexed from ``expr_text`` (a regex
  word lexer — the honest approximation); a multi-identifier actual caps at
  0.6. Edge attrs carry ``via_port``/``derived``/``expr_text``.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import networkx as nx

from hdl_kgraph.graph.clocks import apply_sdc_clock_evidence
from hdl_kgraph.graph.uvm import derive_test_covers
from hdl_kgraph.ids import file_node_id, parse_node_id, stub_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef, within_root
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_RESOLVED,
    CONFIDENCE_UNIQUE_MATCH,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

# Node kinds an INSTANTIATES target name may resolve to, per language family.
_SV_INSTANTIABLE = (NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM, NodeKind.PRIMITIVE)
_VHDL_INSTANTIABLE = (NodeKind.ENTITY,)

# (same-language kinds, cross-language fallback kinds) per edge kind, from
# the perspective of an SV/Verilog referrer; a VHDL referrer swaps the
# instantiable pair. Kinds with no cross-language meaning have an empty
# fallback.
_REF_TARGET_KINDS: dict[EdgeKind, tuple[tuple[NodeKind, ...], tuple[NodeKind, ...]]] = {
    EdgeKind.INSTANTIATES: (_SV_INSTANTIABLE, _VHDL_INSTANTIABLE),
    EdgeKind.CONNECTS: (_SV_INSTANTIABLE, _VHDL_INSTANTIABLE),
    EdgeKind.PARAMETERIZES: (_SV_INSTANTIABLE, _VHDL_INSTANTIABLE),
    EdgeKind.IMPORTS: ((NodeKind.PACKAGE,), ()),
    EdgeKind.EXTENDS: ((NodeKind.CLASS,), ()),
    EdgeKind.IMPLEMENTS: ((NodeKind.ENTITY,), ()),
    EdgeKind.USES_PACKAGE: ((NodeKind.VHDL_PACKAGE,), ()),
    EdgeKind.BINDS: ((NodeKind.ENTITY,), (NodeKind.MODULE,)),
    # DPI-C: an SV import/export name resolves to a FUNCTION/TASK; the
    # language filter in ``_resolve_target`` keeps a C definition (import) or
    # the SV subprogram (export) and discards same-named candidates of the
    # wrong side.
    EdgeKind.FOREIGN_BINDS: ((NodeKind.FUNCTION, NodeKind.TASK), ()),
}

_STUB_KIND: dict[EdgeKind, NodeKind] = {
    EdgeKind.INSTANTIATES: NodeKind.MODULE,
    EdgeKind.CONNECTS: NodeKind.MODULE,
    EdgeKind.PARAMETERIZES: NodeKind.MODULE,
    EdgeKind.IMPORTS: NodeKind.PACKAGE,
    EdgeKind.EXTENDS: NodeKind.CLASS,
    EdgeKind.IMPLEMENTS: NodeKind.ENTITY,
    EdgeKind.USES_PACKAGE: NodeKind.VHDL_PACKAGE,
    EdgeKind.BINDS: NodeKind.ENTITY,
    EdgeKind.FOREIGN_BINDS: NodeKind.FUNCTION,
}

_VHDL_DEFAULT_LIBRARY = "work"

#: Refs resolved against the enclosing unit's children, not by global name.
_SCOPED_REF_KINDS = frozenset(
    {
        EdgeKind.DRIVES,
        EdgeKind.READS,
        EdgeKind.CLOCKED_BY,
        EdgeKind.RESETS,
        EdgeKind.ASSERTS_ON,
        EdgeKind.COVERS,
    }
)

#: Unit kinds that own the port/signal namespace scoped refs resolve in.
_SCOPED_UNIT_KINDS = frozenset(
    {NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM, NodeKind.ARCHITECTURE, NodeKind.ENTITY}
)

#: Edge kinds synthesized by pass-2 resolution (``link()`` / ``derive_test_covers``).
#: The incremental linker (issue #64) deletes and re-emits these by src-node
#: ownership; the rest come straight from a unit's IR (``local_edges``) and are
#: replaced wholesale when that unit is re-ingested.
_PASS2_EDGE_KINDS = frozenset(
    {
        EdgeKind.INSTANTIATES,
        EdgeKind.CONNECTS,
        EdgeKind.PARAMETERIZES,
        EdgeKind.IMPORTS,
        EdgeKind.EXTENDS,
        EdgeKind.IMPLEMENTS,
        EdgeKind.BINDS,
        EdgeKind.USES_PACKAGE,
        EdgeKind.DRIVES,
        EdgeKind.READS,
        EdgeKind.CLOCKED_BY,
        EdgeKind.RESETS,
        EdgeKind.ASSERTS_ON,
        EdgeKind.COVERS,
        EdgeKind.TEST_COVERS,
        EdgeKind.FOREIGN_BINDS,
        EdgeKind.CONSTRAINS,
        EdgeKind.REFERENCES_FILE,
        EdgeKind.GENERATED_FROM,
        EdgeKind.INVOKES,
    }
)
#: Edge kinds that come directly from a unit's IR, not from resolution.
_PASS1_EDGE_KINDS = frozenset(
    {EdgeKind.DECLARES, EdgeKind.INCLUDES, EdgeKind.DEFINES_MACRO, EdgeKind.USES_MACRO}
)


@dataclass(frozen=True)
class RefRecord:
    """One pass-2 reference, persisted in the ``ref_index`` table so an
    incremental link can find which units reference a name without decoding
    their IRs. ``file`` is the unit (``ir.path``) whose re-parse regenerates
    this ref — the deletion key, mirroring ``file_irs``."""

    file: str
    src_id: str
    edge_kind: EdgeKind
    target_name: str
    scoped: bool


def ref_target_kinds(edge_kind: EdgeKind) -> frozenset[NodeKind]:
    """Every node kind a ref of *edge_kind* could resolve to, across both
    language perspectives (a superset — the safe basis for the reverse index)."""
    same, cross = _REF_TARGET_KINDS.get(edge_kind, ((), ()))
    return frozenset(same) | frozenset(cross)


#: Node kinds that global-name pass-2 resolution can target (a name whose
#: definition set among these changes can flip a clean ref's resolution).
DEFINITION_KINDS: frozenset[NodeKind] = frozenset(
    k for same, cross in _REF_TARGET_KINDS.values() for k in (*same, *cross)
)

_SIGNAL_KINDS = (NodeKind.PORT, NodeKind.SIGNAL)
_ASSERTABLE_KINDS = (NodeKind.PROPERTY, NodeKind.SEQUENCE)

#: SDC ``get_*`` query kind → the design NodeKinds a CONSTRAINS ref may resolve
#: to (M10). ``pins`` is best-effort: a hierarchical pin path degrades to its
#: leaf signal/port name.
_CONSTRAINS_QUERY_KINDS: dict[str, tuple[NodeKind, ...]] = {
    "ports": (NodeKind.PORT,),
    "clocks": (NodeKind.CLOCK,),
    "cells": (NodeKind.INSTANCE,),
    "pins": (NodeKind.PORT, NodeKind.SIGNAL),
}
_CONSTRAINS_DEFAULT_KINDS = (NodeKind.PORT, NodeKind.SIGNAL, NodeKind.CLOCK, NodeKind.INSTANCE)


def _pin_leaf(pattern: str) -> str:
    """Leaf signal/port name of a hierarchical pin path (``u_counter/count[0]``
    → ``count``); a non-hierarchical name is returned unchanged."""
    leaf = pattern.rsplit("/", 1)[-1]
    return leaf.split("[", 1)[0]


def _is_glob(pattern: str) -> bool:
    """True if *pattern* carries an SDC/shell glob metacharacter."""
    return any(ch in pattern for ch in "*?[")


#: Port directions → derived instance dataflow ("buffer" is a VHDL output).
_PORT_DIRECTION_FLOW: dict[str, tuple[EdgeKind, ...]] = {
    "input": (EdgeKind.READS,),
    "in": (EdgeKind.READS,),
    "output": (EdgeKind.DRIVES,),
    "out": (EdgeKind.DRIVES,),
    "buffer": (EdgeKind.DRIVES,),
    "inout": (EdgeKind.READS, EdgeKind.DRIVES),
}

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
#: Sized/based literals and char/bit-string literals, stripped before lexing
#: identifiers out of an actual expression (``2'b00`` is not a read of b00).
_LITERAL_RE = re.compile(r"'s?[bBoOdDhH][0-9a-fA-FxXzZ?_]+|'.'?|\"[^\"]*\"")
_EXPR_KEYWORDS = frozenset(
    {
        # SV operators/keywords plausible inside a connection expression
        "posedge",
        "negedge",
        "signed",
        "unsigned",
        "inside",
        "with",
        # VHDL expression keywords (actuals are lowercased VHDL text)
        "open",
        "others",
        "null",
        "true",
        "false",
        "when",
        "else",
        "and",
        "or",
        "not",
        "xor",
        "nand",
        "nor",
        "xnor",
        "abs",
        "mod",
        "rem",
        "sll",
        "srl",
        "sla",
        "sra",
        "rol",
        "ror",
        "downto",
        "to",
    }
)


def _lex_identifiers(expr_text: str) -> list[str]:
    """Identifier-shaped tokens of an actual expression, in order, deduped."""
    seen: set[str] = set()
    out: list[str] = []
    for token in _IDENT_RE.findall(_LITERAL_RE.sub(" ", expr_text)):
        if token.lower() in _EXPR_KEYWORDS or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _add_node(g: nx.MultiDiGraph, node: Node) -> None:
    g.add_node(
        node.id,
        kind=node.kind,
        name=node.name,
        qualified_name=node.qualified_name,
        file=node.file,
        line_span=node.line_span,
        language=node.language,
        attrs=node.attrs,
    )


def _add_edge(g: nx.MultiDiGraph, edge: Edge) -> None:
    g.add_edge(edge.src, edge.dst, kind=edge.kind, confidence=edge.confidence, attrs=edge.attrs)


def ensure_node(g: nx.MultiDiGraph, node: Node) -> None:
    """Add *node* if absent, preserving the existing one otherwise.

    The public entry point for post-link stages (M7 enrichment) that add
    nodes the parsers never emitted, keeping the "no attribute-less networkx
    node" invariant — every node added through here carries the full data set.
    """
    if node.id not in g:
        _add_node(g, node)


def add_or_upgrade_edge(g: nx.MultiDiGraph, edge: Edge, *, upgrade: bool = True) -> bool:
    """Add *edge*, or (when *upgrade*) upgrade a matching existing edge in place.

    "Matching" is same ``(src, dst, kind)``. An upgrade raises the stored
    edge's ``confidence`` to ``edge.confidence`` (never lowers it) and merges
    ``edge.attrs`` over the existing attrs — the mechanism M7 uses to promote
    a heuristic edge to elaboration confidence and stamp its provenance.
    Endpoints must already exist (enrichment calls :func:`ensure_node` first);
    returns ``True`` when an existing edge was upgraded, ``False`` when a new
    edge was added.
    """
    if upgrade and g.has_edge(edge.src, edge.dst):
        for _key, data in g[edge.src][edge.dst].items():
            if data.get("kind") is edge.kind:
                data["confidence"] = max(data.get("confidence", 0.0), edge.confidence)
                data["attrs"] = {**data.get("attrs", {}), **edge.attrs}
                return True
    _add_edge(g, edge)
    return False


class _Linker:
    def __init__(self, file_irs: list[FileIR], root: Path | None = None) -> None:
        # The build root (the dir relpaths are taken against), so a script's
        # in-tree *absolute* file-ref can be canonicalized onto the relpath
        # keyspace before lookup (#164). None preserves the legacy behavior:
        # an absolute target stays verbatim and resolves to an out-of-tree stub.
        self.root: Path | None = root
        self.graph = nx.MultiDiGraph()
        # (kind, name) -> definition node ids, across all files
        self.definitions: defaultdict[tuple[NodeKind, str], list[str]] = defaultdict(list)
        # (kind, lowercased name) -> ids, for the cross-language fallback only
        self.definitions_ci: defaultdict[tuple[NodeKind, str], list[str]] = defaultdict(list)
        # parent id -> child nodes, for PORT/PARAMETER lookup under a target
        self.children: defaultdict[str, list[Node]] = defaultdict(list)
        self.parent: dict[str, str] = {}  # child id -> declaring scope id
        self.node_file: dict[str, str] = {}
        self.node_obj: dict[str, Node] = {}
        # (entity, architecture | None, component) -> configuration bindings,
        # recorded while resolving BINDS refs (which run first).
        self.bindings: defaultdict[tuple[str, str | None, str], list[dict[str, Any]]] = defaultdict(
            list
        )
        # The same header spliced into several compilation units duplicates
        # its refs across IRs; emitted-edge identity keeps one of each.
        self._emitted: set[tuple[str, str, EdgeKind, float, tuple[tuple[str, str], ...]]] = set()
        # unit id -> name -> child nodes, built lazily for scoped resolution
        self._scope_indices: dict[str, dict[str, list[Node]]] = {}
        # Edge endpoints no parser emitted as nodes (materialized as stubs).
        self.warnings: list[str] = []
        # One record per pass-2 ref, keyed by owning unit, for the ref_index.
        self.ref_records: list[RefRecord] = []

        seen_local: set[tuple[str, str, EdgeKind]] = set()
        for ir in file_irs:
            for node in ir.nodes:
                if node.id in self.node_obj:
                    # The same header spliced into several compilation units
                    # (or a FILE node emitted by both the parser and the
                    # preprocessor/filelist adapters): first occurrence wins.
                    continue
                _add_node(self.graph, node)
                self.node_obj[node.id] = node
                self.node_file[node.id] = ir.path
                self.definitions[(node.kind, node.name)].append(node.id)
                self.definitions_ci[(node.kind, node.name.lower())].append(node.id)
        for ir in file_irs:
            for edge in ir.local_edges:
                key = (edge.src, edge.dst, edge.kind)
                if key in seen_local:
                    continue
                seen_local.add(key)
                self._ensure_endpoint(edge.src, edge.kind, edge.dst, ir.path)
                self._ensure_endpoint(edge.dst, edge.kind, edge.src, ir.path)
                _add_edge(self.graph, edge)
                if edge.kind is EdgeKind.DECLARES:
                    self.children[edge.src].append(self.node_obj[edge.dst])
                    self.parent[edge.dst] = edge.src

    def _ensure_endpoint(
        self, node_id: str, edge_kind: EdgeKind, other: str, source: str = ""
    ) -> None:
        """Materialize a stub for an edge endpoint no parser emitted as a node.

        ``MultiDiGraph.add_edge`` would otherwise auto-create the endpoint as
        an attribute-less node, and every ``data["kind"]`` consumer downstream
        would crash on it. The warning names the id so the emitting parser
        gap can be found and closed.
        """
        if node_id in self.node_obj:
            return
        parsed = parse_node_id(node_id)
        attrs: dict[str, Any] = {"unresolved": True, "dangling": True}
        if parsed is None:
            kind, name = NodeKind.SIGNAL, node_id
            attrs["kind_unknown"] = True
        else:
            kind, name = parsed
        stub = Node(id=node_id, kind=kind, name=name, qualified_name=node_id, attrs=attrs)
        _add_node(self.graph, stub)
        self.node_obj[node_id] = stub
        origin = f" in {source}" if source else ""
        self.warnings.append(
            f"dangling edge endpoint '{node_id}' ({edge_kind.value} edge with '{other}'"
            f"{origin}) materialized as unresolved stub"
        )

    def link(self, file_irs: list[FileIR]) -> None:
        # BINDS first: configuration bindings must be on record before the
        # component instantiations they override resolve.
        deferred: list[UnresolvedRef] = []
        for ir in file_irs:
            for ref in ir.unresolved_refs:
                self.ref_records.append(
                    RefRecord(
                        file=ir.path,
                        src_id=ref.src_id,
                        edge_kind=ref.edge_kind,
                        target_name=ref.target_name,
                        scoped=ref.edge_kind in _SCOPED_REF_KINDS,
                    )
                )
                if ref.edge_kind is EdgeKind.BINDS:
                    self._resolve(ref)
                else:
                    deferred.append(ref)
        for ref in deferred:
            self._resolve(ref)

    # -- language helpers ------------------------------------------------------

    def _src_language(self, ref: UnresolvedRef) -> Language:
        src = self.node_obj.get(ref.src_id)
        return src.language if src is not None else Language.UNKNOWN

    def _referrer_library(self, src_id: str) -> str:
        """The VHDL library the referring node's file compiles into."""
        relpath = self.node_file.get(src_id, "")
        file_node = self.node_obj.get(file_node_id(relpath))
        if file_node is not None:
            return str(file_node.attrs.get("library", _VHDL_DEFAULT_LIBRARY))
        return _VHDL_DEFAULT_LIBRARY

    def _filter_foreign(self, ref: UnresolvedRef, candidates: list[str]) -> list[str]:
        """Pick the right side of a DPI-C binding (M8).

        An ``import`` binds to a C/C++ function — drop same-named SV candidates
        (including the import prototype itself) and prefer a definition over a
        header prototype. An ``export`` binds to the SV subprogram it names, so
        keep only the SV/Verilog candidates.
        """
        if ref.attrs.get("dpi_export"):
            langs = (Language.SYSTEMVERILOG, Language.VERILOG)
            return [c for c in candidates if self.node_obj[c].language in langs]
        foreign = [c for c in candidates if self.node_obj[c].language in (Language.C, Language.CPP)]
        defs = [c for c in foreign if self.node_obj[c].attrs.get("is_definition")]
        return defs or foreign

    def _filter_library(self, candidates: list[str], library: str | None, src_id: str) -> list[str]:
        """Narrow ambiguous VHDL candidates to the named library, if that helps."""
        if library is None or len(candidates) <= 1:
            return candidates
        if library == _VHDL_DEFAULT_LIBRARY:
            library = self._referrer_library(src_id)
        filtered = [
            c for c in candidates if self.node_obj[c].attrs.get("library", library) == library
        ]
        return filtered or candidates

    # -- target resolution ---------------------------------------------------

    def _resolve_target(self, ref: UnresolvedRef) -> tuple[list[str], float, dict[str, Any]]:
        """Return (target ids, confidence, extra edge attrs); stubs if unresolved."""
        src = self.node_obj.get(ref.src_id)
        src_lang = src.language if src is not None else Language.UNKNOWN
        if (
            src is not None
            and src.kind is NodeKind.INSTANCE
            and src_lang is Language.VHDL
            and ref.edge_kind in (EdgeKind.INSTANTIATES, EdgeKind.CONNECTS, EdgeKind.PARAMETERIZES)
        ):
            return self._resolve_vhdl_instance(ref, src)

        same_kinds, cross_kinds = _REF_TARGET_KINDS[ref.edge_kind]
        if src_lang is Language.VHDL and cross_kinds:
            same_kinds, cross_kinds = _VHDL_INSTANTIABLE, _SV_INSTANTIABLE

        candidates: list[str] = []
        for kind in same_kinds:
            candidates.extend(self.definitions.get((kind, ref.target_name), ()))
        if ref.edge_kind is EdgeKind.EXTENDS and ref.attrs.get("package"):
            # Prefer a class declared inside the named package.
            prefix = f"{ref.attrs['package']}."
            scoped = [c for c in candidates if self.node_obj[c].qualified_name.startswith(prefix)]
            if scoped:
                candidates = scoped
        if ref.edge_kind in (EdgeKind.USES_PACKAGE, EdgeKind.BINDS, EdgeKind.IMPLEMENTS):
            candidates = self._filter_library(candidates, ref.attrs.get("library"), ref.src_id)
        if ref.edge_kind is EdgeKind.FOREIGN_BINDS:
            candidates = self._filter_foreign(ref, candidates)
        if candidates:
            return (*self._score(candidates, ref.src_id), {})
        cross = self._cross_language(cross_kinds, ref.target_name)
        if cross is not None:
            return (*cross, {})
        return [self._stub(ref)], CONFIDENCE_RESOLVED, {}

    def _score(self, candidates: list[str], src_id: str) -> tuple[list[str], float]:
        """The M1 same-language confidence rules."""
        ref_file = self.node_file.get(src_id, "")
        if len(candidates) == 1:
            same_file = self.node_obj[candidates[0]].file == ref_file
            return candidates, CONFIDENCE_RESOLVED if same_file else CONFIDENCE_UNIQUE_MATCH
        local = [c for c in candidates if self.node_obj[c].file == ref_file]
        if len(local) == 1:
            return local, CONFIDENCE_RESOLVED
        # Both-branches mode can define the same unit in several arms of an
        # undefined `ifdef. The arm normal evaluation selects is not stamped
        # `conditional`; when it is the only such candidate it wins at full
        # confidence instead of dragging every reference down to 0.6.
        selected = [
            c for c in (local or candidates) if not self.node_obj[c].attrs.get("conditional")
        ]
        if len(selected) == 1:
            same_file = self.node_obj[selected[0]].file == ref_file
            return selected, CONFIDENCE_RESOLVED if same_file else CONFIDENCE_UNIQUE_MATCH
        return candidates, CONFIDENCE_AMBIGUOUS

    def _cross_language(
        self, kinds: tuple[NodeKind, ...], name: str
    ) -> tuple[list[str], float] | None:
        """Case-insensitive cross-language name match, capped at 0.8."""
        candidates: list[str] = []
        for kind in kinds:
            candidates.extend(self.definitions_ci.get((kind, name.lower()), ()))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates, CONFIDENCE_UNIQUE_MATCH
        return candidates, CONFIDENCE_AMBIGUOUS

    # -- VHDL instantiation styles ---------------------------------------------

    def _resolve_vhdl_instance(
        self, ref: UnresolvedRef, inst: Node
    ) -> tuple[list[str], float, dict[str, Any]]:
        style = inst.attrs.get("style", "component")
        library = inst.attrs.get("library")
        if style == "configuration":
            return self._resolve_via_configuration(ref, library)
        if style == "component":
            binding = self._binding_for(inst)
            if binding is not None:
                targets, confidence = self._resolve_entity_name(
                    str(binding["target"]), ref, binding.get("library")
                )
                return targets, confidence, {"bound_by": binding["config"]}
        # Direct entity instantiation, or a component's default binding:
        # a like-named entity, then the cross-language fallback.
        targets, confidence = self._resolve_entity_name(ref.target_name, ref, library)
        return targets, confidence, {}

    def _resolve_entity_name(
        self, name: str, ref: UnresolvedRef, library: str | None
    ) -> tuple[list[str], float]:
        candidates = list(self.definitions.get((NodeKind.ENTITY, name), ()))
        candidates = self._filter_library(candidates, library, ref.src_id)
        if candidates:
            return self._score(candidates, ref.src_id)
        cross = self._cross_language(_SV_INSTANTIABLE, name)
        if cross is not None:
            return cross
        return [self._ensure_stub(NodeKind.ENTITY, name, name)], CONFIDENCE_RESOLVED

    def _resolve_via_configuration(
        self, ref: UnresolvedRef, library: str | None
    ) -> tuple[list[str], float, dict[str, Any]]:
        """``u : configuration work.cfg`` resolves to cfg's configured entity."""
        configs = list(self.definitions.get((NodeKind.CONFIGURATION, ref.target_name), ()))
        configs = self._filter_library(configs, library, ref.src_id)
        if len(configs) == 1:
            cfg = self.node_obj[configs[0]]
            entity_name = cfg.attrs.get("of_entity")
            if entity_name:
                targets, confidence = self._resolve_entity_name(
                    str(entity_name), ref, cfg.attrs.get("library")
                )
                return targets, confidence, {"via_configuration": cfg.id}
            return configs, CONFIDENCE_RESOLVED, {}
        if configs:  # ambiguous configuration name: edges to each, 0.6
            return configs, CONFIDENCE_AMBIGUOUS, {}
        stub = self._ensure_stub(NodeKind.CONFIGURATION, ref.target_name, ref.target_name)
        return [stub], CONFIDENCE_RESOLVED, {}

    def _binding_for(self, inst: Node) -> dict[str, Any] | None:
        """The configuration binding governing *inst*, if any.

        Looks up the instance's enclosing architecture (and its entity), then
        matches component bindings: a specific label beats ``all`` beats
        ``others``.
        """
        arch = self.node_obj.get(self.parent.get(inst.id, ""))
        if arch is None or arch.kind is not NodeKind.ARCHITECTURE:
            return None
        entity = str(arch.attrs.get("of_entity", ""))
        component = str(inst.attrs.get("target", ""))
        if not entity or not component:
            return None
        rows: list[dict[str, Any]] = []
        for block in (arch.name, None):
            rows.extend(self.bindings.get((entity, block, component), ()))
        by_rank: dict[int, dict[str, Any]] = {}
        for row in rows:
            instances = row["instances"]
            if isinstance(instances, list) and inst.name in instances:
                rank = 0
            elif instances == "all":
                rank = 1
            elif instances == "others":
                rank = 2
            else:
                continue
            by_rank.setdefault(rank, row)
        for rank in (0, 1, 2):
            if rank in by_rank:
                return by_rank[rank]
        return None

    def _record_binding(self, ref: UnresolvedRef) -> None:
        entity = ref.attrs.get("of_entity")
        component = ref.attrs.get("component")
        if not entity or not component:
            return
        block = ref.attrs.get("block")
        key = (str(entity), str(block) if block is not None else None, str(component))
        self.bindings[key].append(
            {
                "instances": ref.attrs.get("instances", "all"),
                "target": ref.target_name,
                "library": ref.attrs.get("library"),
                "architecture": ref.attrs.get("architecture"),
                "config": ref.src_id,
            }
        )

    def _stub(self, ref: UnresolvedRef) -> str:
        kind = _STUB_KIND[ref.edge_kind]
        qualified = ref.target_name
        if ref.edge_kind is EdgeKind.USES_PACKAGE:
            # Qualify by library so e.g. two libraries' like-named packages
            # never merge; ieee/std packages stay stubs by design.
            library = ref.attrs.get("library") or self._referrer_library(ref.src_id)
            qualified = f"{library}.{ref.target_name}"
        return self._ensure_stub(kind, qualified, ref.target_name)

    def _stub_child(self, parent_id: str, kind: NodeKind, name: str) -> str:
        parent_name = self.node_obj[parent_id].name
        stub_id = self._ensure_stub(kind, f"{parent_name}.{name}", name)
        if not self.graph.has_edge(parent_id, stub_id):
            _add_edge(self.graph, Edge(src=parent_id, dst=stub_id, kind=EdgeKind.DECLARES))
            self.children[parent_id].append(self.node_obj[stub_id])
        return stub_id

    def _ensure_stub(self, kind: NodeKind, qualified: str, name: str) -> str:
        stub_id = stub_node_id(kind, qualified)
        if stub_id not in self.graph:
            stub = Node(
                id=stub_id,
                kind=kind,
                name=name,
                qualified_name=qualified,
                attrs={"unresolved": True},
            )
            _add_node(self.graph, stub)
            self.node_obj[stub_id] = stub
        return stub_id

    # -- scoped (dataflow) resolution --------------------------------------------

    def _enclosing_unit_id(self, node_id: str) -> str | None:
        """Climb DECLARES parents to the design unit that scopes *node_id*."""
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current not in seen:
            seen.add(current)
            node = self.node_obj.get(current)
            if node is not None and node.kind in _SCOPED_UNIT_KINDS:
                return current
            current = self.parent.get(current)
        return None

    def _scope_index_for(self, unit_id: str) -> dict[str, list[Node]]:
        """name -> declared children visible to scoped refs inside *unit_id*
        (an ARCHITECTURE sees its entity's ports/generics; ENUM members are
        flattened into the unit's namespace, as in both languages)."""
        index = self._scope_indices.get(unit_id)
        if index is not None:
            return index
        index = defaultdict(list)
        scopes = [unit_id]
        unit = self.node_obj[unit_id]
        if unit.kind is NodeKind.ARCHITECTURE:
            entity_name = unit.attrs.get("of_entity")
            if entity_name:
                candidates = self._filter_library(
                    list(self.definitions.get((NodeKind.ENTITY, str(entity_name)), ())),
                    unit.attrs.get("library"),
                    unit_id,
                )
                scopes.extend(candidates)
        for scope in scopes:
            for child in self.children.get(scope, []):
                index[child.name].append(child)
                if child.kind is NodeKind.ENUM:
                    for member in self.children.get(child.id, []):
                        index[member.name].append(member)
        self._scope_indices[unit_id] = dict(index)
        return self._scope_indices[unit_id]

    def _scoped_signal_target(self, unit_id: str, name: str) -> tuple[list[str], float]:
        """(target ids, confidence) for *name* in *unit_id*'s namespace.

        ([], 0) means the name is a constant or some other non-signal
        declaration — not dataflow. An undeclared name gets a SIGNAL stub
        child (implicit nets / typos) at ≤ 0.6. Multiple matches follow the
        same confidence contract as global resolution: the declaration the
        preprocessor did not stamp ``conditional`` (the `ifdef arm normal
        evaluation selects) wins at 1.0, a non-ANSI port redeclaration
        (``output y; wire y;``) resolves to the PORT — one net, not an
        ambiguity — and a genuine tie emits every candidate at 0.6.
        """
        matches = self._scope_index_for(unit_id).get(name, [])
        signals = [n for n in matches if n.kind in _SIGNAL_KINDS]
        if signals:
            selected = [n for n in signals if not n.attrs.get("conditional")] or signals
            if len(selected) == 1:
                return [selected[0].id], CONFIDENCE_RESOLVED
            ports = [n for n in selected if n.kind is NodeKind.PORT]
            if len(ports) == 1:
                return [ports[0].id], CONFIDENCE_RESOLVED
            return [n.id for n in selected], CONFIDENCE_AMBIGUOUS
        if matches:
            return [], 0.0
        return [self._stub_child(unit_id, NodeKind.SIGNAL, name)], CONFIDENCE_AMBIGUOUS

    def _resolve_scoped(self, ref: UnresolvedRef) -> None:
        unit_id = self._enclosing_unit_id(ref.src_id)
        if unit_id is None:
            return  # e.g. a covergroup in a class: no design-unit namespace
        if ref.edge_kind is EdgeKind.ASSERTS_ON:
            matches = self._scope_index_for(unit_id).get(ref.target_name, [])
            assertables = [n for n in matches if n.kind in _ASSERTABLE_KINDS]
            if assertables:
                self._emit(ref, assertables[0].id, CONFIDENCE_RESOLVED)
                return
        targets, confidence = self._scoped_signal_target(unit_id, ref.target_name)
        for target in targets:
            self._emit(ref, target, confidence)

    # -- cocotb (M8): resolve against the heuristically-chosen DUT module ---------

    def _modules_named(self, name: str) -> list[str]:
        """MODULE/ENTITY definitions matching *name* (exact, then case-insensitive)."""
        exact = list(self.definitions.get((NodeKind.MODULE, name), ())) + list(
            self.definitions.get((NodeKind.ENTITY, name), ())
        )
        if exact:
            return exact
        lowered = name.lower()
        return list(self.definitions_ci.get((NodeKind.MODULE, lowered), ())) + list(
            self.definitions_ci.get((NodeKind.ENTITY, lowered), ())
        )

    def _resolve_cocotb(self, ref: UnresolvedRef) -> None:
        """Resolve a cocotb ref against the DUT module (chosen by config top or
        filename heuristic in the parser). Unresolved DUT/signal names are
        skipped, not stubbed — the DUT is a name guess, so a miss should not
        invent module/signal nodes."""
        if ref.edge_kind is EdgeKind.TEST_COVERS:
            for module_id in self._modules_named(ref.target_name):
                if not self.node_obj[module_id].attrs.get("unresolved"):
                    self._emit(ref, module_id, ref.confidence)
            return
        # READS / DRIVES: resolve dut.<signal> against the DUT module's namespace,
        # collapsing the same ambiguities the scoped resolver does (a both-branches
        # arm wins over its alternatives; a non-ANSI ``output y; wire y;`` is one
        # net, the PORT — not two edges).
        dut_name = str(ref.attrs.get("dut_module", ""))
        for module_id in self._modules_named(dut_name):
            if self.node_obj[module_id].attrs.get("unresolved"):
                continue
            matches = self._scope_index_for(module_id).get(ref.target_name, [])
            signals = [n for n in matches if n.kind in _SIGNAL_KINDS]
            if not signals:
                continue
            selected = [n for n in signals if not n.attrs.get("conditional")] or signals
            ports = [n for n in selected if n.kind is NodeKind.PORT]
            if len(ports) == 1:
                selected = ports
            for sig in selected:
                self._emit(ref, sig.id, ref.confidence)

    def _resolve_sln(self, ref: UnresolvedRef) -> None:
        """Resolve an SLN action invocation (M10), skip-don't-stub.

        INVOKES binds to a same-file ACTION; TEST_COVERS binds to a design
        MODULE/ENTITY/INSTANCE the invoked name matches (the coverage signal).
        Most invoked names are testbench sequences matching neither — kept only
        on the action's ``attrs["invokes"]`` — so they resolve to no edge.
        """
        name = ref.target_name
        if ref.edge_kind is EdgeKind.INVOKES:
            src_file = self.node_file.get(ref.src_id)
            for action_id in self.definitions.get((NodeKind.ACTION, name), ()):
                if action_id != ref.src_id and self.node_file.get(action_id) == src_file:
                    self._emit(ref, action_id, CONFIDENCE_RESOLVED)
            return
        # TEST_COVERS: a design module/entity (reuse _modules_named) or instance.
        # Cross-file name match, so score it by the normal contract (unique 0.8,
        # ambiguous 0.6) rather than forcing full confidence.
        targets = [
            t
            for t in self._modules_named(name)
            + list(self.definitions.get((NodeKind.INSTANCE, name), ()))
            if not self.node_obj[t].attrs.get("unresolved")
        ]
        if not targets:
            return
        scored, confidence = self._score(targets, ref.src_id)
        for target in scored:
            self._emit(ref, target, confidence)

    # -- SDC/XDC constraints (M10): resolve get_ports/pins/cells/clocks ----------

    def _glob_targets(self, kinds: tuple[NodeKind, ...], pattern: str) -> list[str]:
        """Definition ids of *kinds* whose name matches the SDC glob *pattern*."""
        wanted = set(kinds)
        out: list[str] = []
        for (kind, name), ids in self.definitions.items():
            if kind in wanted and fnmatch.fnmatchcase(name, pattern):
                out.extend(ids)
        return out

    def _resolve_constrains(self, ref: UnresolvedRef) -> None:
        """Resolve a CONSTRAINS ref (an SDC object query) to design nodes.

        Exact unique match resolves at 1.0; a glob's unique match at 0.8 and a
        glob/exact tie at 0.6 (ROADMAP M10). An object the design never declares
        is skipped, not stubbed — a constraint may legitimately name a pin that
        this RTL slice does not contain, and inventing a node would mislead.
        """
        query = str(ref.attrs.get("query", ""))
        kinds = _CONSTRAINS_QUERY_KINDS.get(query, _CONSTRAINS_DEFAULT_KINDS)
        name = _pin_leaf(ref.target_name) if query == "pins" else ref.target_name
        if _is_glob(name):
            matches = self._glob_targets(kinds, name)
            if not matches:
                return
            confidence = CONFIDENCE_UNIQUE_MATCH if len(matches) == 1 else CONFIDENCE_AMBIGUOUS
            for target in matches:
                self._emit(ref, target, confidence)
            return
        candidates: list[str] = []
        for kind in kinds:
            candidates.extend(self.definitions.get((kind, name), ()))
        if not candidates:
            return  # constraint names an object absent from this design: skip
        confidence = CONFIDENCE_RESOLVED if len(candidates) == 1 else CONFIDENCE_AMBIGUOUS
        for target in candidates:
            self._emit(ref, target, confidence)

    def _canonical_file_target(self, target: str) -> str:
        """Map a file-ref target onto the build-root relpath keyspace (#164).

        Relative targets are already normalized by the parser. An *absolute*
        target inside the build root is relativized so it binds to the real
        FILE node (whose id is ``file:{relpath}``); an out-of-tree absolute (or
        when no root is known) is returned verbatim, so it still resolves to an
        ``unresolved:file:`` stub — the prior behavior.

        Absoluteness is judged with the platform-native ``os.path.isabs`` so a
        Windows drive path (``D:/proj/top.v``) — which ``posixpath`` does not
        treat as absolute — is canonicalized too, not just POSIX ``/...`` paths.
        """
        if self.root is None or not os.path.isabs(target):
            return target
        path = Path(target)
        if within_root(path, self.root):
            return path.resolve().relative_to(self.root.resolve()).as_posix()
        return target

    def _resolve_file_ref(self, ref: UnresolvedRef) -> None:
        """Resolve a script file reference (M10) to a FILE node by relpath.

        ``target_name`` is normalized to the build-root POSIX relpath keyspace
        (an in-tree absolute path is canonicalized by
        :meth:`_canonical_file_target`, #164). It binds to the existing
        ``file:`` node when that file is part of the build; otherwise the script
        references a file outside the analyzed set (a generated or out-of-tree
        source), which materializes as an ``unresolved:file:`` stub — a distinct
        id, so it never shadows a real FILE node.

        ``REFERENCES_FILE`` points script → file. ``GENERATED_FROM`` points the
        *generated* file → its generator (the script), so it is emitted reversed.
        """
        rel = self._canonical_file_target(ref.target_name)
        file_id = file_node_id(rel)
        if file_id not in self.node_obj:
            file_id = self._ensure_stub(NodeKind.FILE, rel, rel.rsplit("/", 1)[-1])
        if ref.edge_kind is EdgeKind.GENERATED_FROM:
            attrs = {k: v for k, v in ref.attrs.items() if v is not None}
            attrs["line_span"] = ref.line_span
            self._emit_edge(file_id, ref.src_id, ref.edge_kind, CONFIDENCE_RESOLVED, attrs)
        else:
            self._emit(ref, file_id, CONFIDENCE_RESOLVED)

    def _derive_port_dataflow(
        self, ref: UnresolvedRef, port: Node, confidence: float, actual_text: str
    ) -> None:
        """Instance-level DRIVES/READS from a resolved CONNECTS binding."""
        direction = str(port.attrs.get("direction", ""))
        flows = _PORT_DIRECTION_FLOW.get(direction)
        if flows is None or not actual_text or actual_text == ".*":
            return
        identifiers = _lex_identifiers(actual_text)
        if not identifiers:
            return  # a constant actual: no dataflow
        unit_id = self._enclosing_unit_id(ref.src_id)
        if unit_id is None:
            return
        base = confidence if len(identifiers) == 1 else min(confidence, CONFIDENCE_AMBIGUOUS)
        if len(flows) > 1:  # inout: direction is a guess either way
            base = min(base, CONFIDENCE_AMBIGUOUS)
        vhdl = self._src_language(ref) is Language.VHDL
        for name in identifiers:
            targets, cap = self._scoped_signal_target(unit_id, name.lower() if vhdl else name)
            for target in targets:
                for kind in flows:
                    self._emit_edge(
                        ref.src_id,
                        target,
                        kind,
                        min(base, cap, ref.confidence),
                        {
                            "via_port": port.name,
                            "derived": "connects",
                            "expr_text": actual_text,
                            "line_span": ref.line_span,
                        },
                    )

    def _emit_edge(
        self, src: str, dst: str, kind: EdgeKind, confidence: float, attrs: dict[str, Any]
    ) -> None:
        key = (src, dst, kind, confidence, tuple(sorted((k, str(v)) for k, v in attrs.items())))
        if key in self._emitted:
            return
        self._emitted.add(key)
        self._ensure_endpoint(src, kind, dst)
        self._ensure_endpoint(dst, kind, src)
        _add_edge(self.graph, Edge(src=src, dst=dst, kind=kind, confidence=confidence, attrs=attrs))

    # -- per-kind resolution ---------------------------------------------------

    def _resolve(self, ref: UnresolvedRef) -> None:
        if ref.attrs.get("cocotb"):
            self._resolve_cocotb(ref)
            return
        if ref.attrs.get("sln"):
            self._resolve_sln(ref)
            return
        if ref.edge_kind is EdgeKind.CONSTRAINS:
            self._resolve_constrains(ref)
            return
        if ref.attrs.get("file_ref"):
            self._resolve_file_ref(ref)
            return
        if ref.edge_kind in _SCOPED_REF_KINDS:
            self._resolve_scoped(ref)
            return
        if ref.edge_kind is EdgeKind.BINDS and ref.attrs.get("role") == "binding":
            self._record_binding(ref)
        targets, confidence, extra = self._resolve_target(ref)
        if ref.edge_kind in (EdgeKind.CONNECTS, EdgeKind.PARAMETERIZES):
            child_kind = NodeKind.PORT if ref.edge_kind is EdgeKind.CONNECTS else NodeKind.PARAMETER
            for target in targets:
                self._resolve_binding(ref, target, confidence, child_kind, extra)
        else:
            for target in targets:
                self._emit(ref, target, confidence, extra=extra)

    def _resolve_binding(
        self,
        ref: UnresolvedRef,
        target: str,
        confidence: float,
        child_kind: NodeKind,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Resolve one CONNECTS/PARAMETERIZES ref against one target's children."""
        target_node = self.node_obj[target]
        unresolved_target = target_node.attrs.get("unresolved", False)
        kids = [n for n in self.children.get(target, []) if n.kind is child_kind]
        if child_kind is NodeKind.PARAMETER:
            kids = [n for n in kids if not n.attrs.get("is_localparam")]
        kids.sort(key=lambda n: n.attrs.get("index", 0))

        if ref.attrs.get("wildcard"):
            # .* — connect every port of a resolved target; for a stub the
            # port list is unknown, so emit one marker edge to the target.
            if kids and not unresolved_target:
                for kid in kids:
                    self._emit(ref, kid.id, confidence, port_name=kid.name, extra=extra)
                    if ref.edge_kind is EdgeKind.CONNECTS:
                        # .* binds the like-named signal of the parent scope.
                        self._derive_port_dataflow(ref, kid, confidence, kid.name)
            else:
                self._emit(ref, target, confidence, extra=extra)
            return

        name = ref.attrs.get("port_name") or ref.attrs.get("param_name")
        position = ref.attrs.get("position")
        dst: str | None = None
        if name is not None:
            dst = next((k.id for k in kids if k.name == name), None)
            if dst is None and Language.VHDL in (
                self._src_language(ref),
                target_node.language,
            ):
                # VHDL names are case-insensitive: a VHDL formal must match an
                # SV port (and vice versa) regardless of casing.
                lowered = str(name).lower()
                dst = next((k.id for k in kids if k.name.lower() == lowered), None)
            if dst is None and unresolved_target:
                dst = self._stub_child(target, child_kind, str(name))
        elif position is not None and isinstance(position, int) and position < len(kids):
            dst = kids[position].id
        if dst is None:
            # Named binding matching no declared child of a resolved target,
            # or positional overflow: point at the target itself so the
            # mismatch stays visible in the graph.
            self._emit(ref, target, min(confidence, CONFIDENCE_AMBIGUOUS), extra=extra)
        else:
            self._emit(ref, dst, confidence, extra=extra)
            port = self.node_obj.get(dst)
            if (
                ref.edge_kind is EdgeKind.CONNECTS
                and port is not None
                and (port.kind is NodeKind.PORT)
            ):
                self._derive_port_dataflow(
                    ref, port, confidence, str(ref.attrs.get("expr_text") or "")
                )

    def _emit(
        self,
        ref: UnresolvedRef,
        dst: str,
        confidence: float,
        port_name: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        attrs: dict[str, object] = {k: v for k, v in ref.attrs.items() if v is not None}
        if extra:
            attrs.update(extra)
        if port_name is not None:
            attrs["port_name"] = port_name
        attrs["line_span"] = ref.line_span
        # A reference from a non-selected both-branches region caps the edge
        # at the site's own confidence.
        effective = min(confidence, ref.confidence)
        key = (
            ref.src_id,
            dst,
            ref.edge_kind,
            effective,
            tuple(sorted((k, str(v)) for k, v in attrs.items())),
        )
        if key in self._emitted:
            return
        self._emitted.add(key)
        self._ensure_endpoint(ref.src_id, ref.edge_kind, dst)
        self._ensure_endpoint(dst, ref.edge_kind, ref.src_id)
        _add_edge(
            self.graph,
            Edge(src=ref.src_id, dst=dst, kind=ref.edge_kind, confidence=effective, attrs=attrs),
        )


def link_graph(
    file_irs: list[FileIR], warnings: list[str] | None = None, root: Path | None = None
) -> tuple[nx.MultiDiGraph, list[RefRecord]]:
    """Link per-file IRs into the global knowledge graph (pass 2).

    Returns the graph plus the per-unit pass-2 reference records (for the
    persisted ``ref_index``). *warnings* (when given) collects diagnostics for
    edge endpoints that no parser emitted as a node — each is materialized as
    an unresolved stub so the graph never carries attribute-less nodes. *root*
    is the build root, used to canonicalize in-tree absolute file-refs (#164).
    """
    linker = _Linker(file_irs, root)
    linker.link(file_irs)
    for edge in derive_test_covers(linker.graph):
        # The guarded path, so a derived edge can never reintroduce the
        # attribute-less nodes networkx auto-creates for unknown endpoints.
        linker._emit_edge(edge.src, edge.dst, edge.kind, edge.confidence, edge.attrs)
    # SDC create_clock is authoritative clock evidence: promote the CLOCKED_BY
    # edges it backs to 1.0 once every CONSTRAINS edge is resolved (M10). A no-op
    # when no SDC clocks are present.
    apply_sdc_clock_evidence(linker.graph)
    if warnings is not None:
        warnings.extend(linker.warnings)
    return linker.graph, linker.ref_records


def build_graph(
    file_irs: list[FileIR], warnings: list[str] | None = None, root: Path | None = None
) -> nx.MultiDiGraph:
    """Link per-file IRs into the global knowledge graph (pass 2).

    Thin wrapper over :func:`link_graph` that discards the ref records, for
    callers that only need the graph.
    """
    return link_graph(file_irs, warnings, root)[0]


def _node_from_data(node_id: str, data: Mapping[str, Any]) -> Node:
    """Reconstruct a :class:`Node` from a stored graph node's attribute dict."""
    return Node(
        id=node_id,
        kind=data["kind"],
        name=data["name"],
        qualified_name=data.get("qualified_name", ""),
        file=data.get("file", ""),
        line_span=data.get("line_span", (0, 0)),
        language=data["language"],
        attrs=dict(data["attrs"]),
    )


def link_incremental(
    file_irs: list[FileIR],
    prior_graph: nx.MultiDiGraph,
    dirty_files: set[str],
    affected_srcs: set[str],
    warnings: list[str] | None = None,
    root: Path | None = None,
) -> tuple[nx.MultiDiGraph, list[RefRecord]]:
    """Pass-2 link that re-resolves only the dirty closure + its neighborhood (#64).

    The *prior_graph* (the previous resolved graph, loaded once) is **mutated in
    place** rather than rebuilt: the clean units' nodes and reused pass-2 edges
    already live in it, so the only work is to drop what changed and re-resolve
    it. Concretely:

    1. drop every node owned by a dirty/removed file (taking its incident edges
       with it), every ``TEST_COVERS`` edge (re-derived globally below), and the
       pass-2 edges of each *affected* clean src (``affected_srcs`` — clean refs
       whose target name's definition set changed);
    2. seed the resolution indexes from the surviving (clean) nodes and splice
       the dirty units' fresh nodes/local edges back in;
    3. re-resolve only the *live* refs (those in a dirty unit or owned by an
       affected src) — every other src keeps its prior edges untouched;
    4. garbage-collect stubs left with no referrer, then re-derive TEST_COVERS.

    The result is byte-identical to :func:`link_graph` (the contract the #64-C
    equivalence matrix + fuzz pin). Candidate *ordering* in ``definitions`` need
    not match a full build: a multi-candidate name resolves to every candidate
    (set-keyed via ``_emitted``) or to its unique local/non-conditional one, so
    the emitted edge set is order-independent. Callers fall back to
    :func:`link_graph` for cases this does not model (VHDL, binds/config,
    enrichment).
    """
    linker = _Linker([], root)  # initialize the resolution indexes against an empty graph
    linker.graph = prior_graph  # ...then bind and mutate the prior graph in place

    # 1. Drop the changed surface: dirty/removed-file nodes (with their edges),
    #    all TEST_COVERS, and the affected clean srcs' pass-2 edges.
    prior_graph.remove_nodes_from(
        [n for n, d in prior_graph.nodes(data=True) if d.get("file", "") in dirty_files]
    )
    stale_edges = [
        (u, v, k)
        for u, v, k, d in prior_graph.edges(keys=True, data=True)
        if d["kind"] is EdgeKind.TEST_COVERS
        or (d["kind"] in _PASS2_EDGE_KINDS and u in affected_srcs)
    ]
    prior_graph.remove_edges_from(stale_edges)

    # 2a. Seed the indexes from the surviving clean nodes. Stubs (synthesized at
    #     resolution time) carry no definition, matching a full build's
    #     ``definitions`` (built only from parsed IR nodes).
    for node_id, data in prior_graph.nodes(data=True):
        node = _node_from_data(node_id, data)
        linker.node_obj[node_id] = node
        if not node.attrs.get("unresolved"):
            linker.definitions[(node.kind, node.name)].append(node_id)
            linker.definitions_ci[(node.kind, node.name.lower())].append(node_id)
    for src, dst, data in prior_graph.edges(data=True):
        if data["kind"] is EdgeKind.DECLARES:
            linker.children[src].append(linker.node_obj[dst])
            linker.parent[dst] = src
    # node_file maps a ref's src to its *owning compilation unit* (ir.path),
    # which _score()/_referrer_library() read for same-file/library context —
    # not the node's own `file` attr, which differs for a node declared in a
    # spliced include. Reconstruct it by first occurrence over the full IR set,
    # exactly as _Linker.__init__ does, so re-resolved refs match a full build
    # even when their src lives in an included header (#99 review).
    for ir in file_irs:
        for node in ir.nodes:
            linker.node_file.setdefault(node.id, ir.path)

    # 2b. Splice the dirty units' fresh nodes + local edges back in.
    for ir in file_irs:
        if ir.path not in dirty_files:
            continue
        for node in ir.nodes:
            if node.id in linker.node_obj:
                continue
            _add_node(linker.graph, node)
            linker.node_obj[node.id] = node
            linker.definitions[(node.kind, node.name)].append(node.id)
            linker.definitions_ci[(node.kind, node.name.lower())].append(node.id)
    # Pass-1 edges that survived in the prior graph were already contributed by
    # a clean unit (e.g. a header spliced into several units). Seed the dedup
    # set with them so a dirty unit that re-splices the same clean include does
    # not add a duplicate DECLARES/INCLUDES/macro edge — mirroring the
    # first-occurrence dedup _Linker.__init__ does across all IRs (#99 review).
    seen_local: set[tuple[str, str, EdgeKind]] = {
        (u, v, d["kind"])
        for u, v, d in prior_graph.edges(data=True)
        if d["kind"] in _PASS1_EDGE_KINDS
    }
    for ir in file_irs:
        if ir.path not in dirty_files:
            continue
        for edge in ir.local_edges:
            key = (edge.src, edge.dst, edge.kind)
            if key in seen_local:
                continue
            seen_local.add(key)
            linker._ensure_endpoint(edge.src, edge.kind, edge.dst, ir.path)
            linker._ensure_endpoint(edge.dst, edge.kind, edge.src, ir.path)
            _add_edge(linker.graph, edge)
            if edge.kind is EdgeKind.DECLARES:
                linker.children[edge.src].append(linker.node_obj[edge.dst])
                linker.parent[edge.dst] = edge.src

    # 3. Record every ref for the ref_index; re-resolve only the live ones.
    for ir in file_irs:
        live_unit = ir.path in dirty_files
        for ref in ir.unresolved_refs:
            linker.ref_records.append(
                RefRecord(
                    file=ir.path,
                    src_id=ref.src_id,
                    edge_kind=ref.edge_kind,
                    target_name=ref.target_name,
                    scoped=ref.edge_kind in _SCOPED_REF_KINDS,
                )
            )
            if live_unit or ref.src_id in affected_srcs:
                linker._resolve(ref)

    # 4. GC orphaned stubs, then re-derive TEST_COVERS over the spliced graph.
    _gc_orphan_stubs(prior_graph)
    for edge in derive_test_covers(linker.graph):
        linker._emit_edge(edge.src, edge.dst, edge.kind, edge.confidence, edge.attrs)
    if warnings is not None:
        warnings.extend(linker.warnings)
    return linker.graph, linker.ref_records


def _gc_orphan_stubs(graph: nx.MultiDiGraph) -> None:
    """Remove unresolved stubs no surviving/re-emitted ref points at any more.

    A stub exists in a full build only because a ref resolved to it; after the
    incremental re-resolution some prior stubs (e.g. for a module that was just
    re-added, so its instances now resolve to the real node) are left with no
    non-``DECLARES`` edge. Such a stub — and the stub-child subtree it hosts —
    is dropped so the node set matches a full build exactly.
    """
    stubs = {n for n, d in graph.nodes(data=True) if d["attrs"].get("unresolved")}

    def _anchored(node_id: str) -> bool:
        return any(
            d["kind"] is not EdgeKind.DECLARES
            for _, _, d in (
                *graph.in_edges(node_id, data=True),
                *graph.out_edges(node_id, data=True),
            )
        )

    keep: set[str] = set()
    pending = [n for n in stubs if _anchored(n)]
    while pending:  # a kept stub keeps its DECLARES parent/children (hosting chain)
        node_id = pending.pop()
        if node_id in keep:
            continue
        keep.add(node_id)
        for u, v, d in (
            *graph.in_edges(node_id, data=True),
            *graph.out_edges(node_id, data=True),
        ):
            if d["kind"] is EdgeKind.DECLARES:
                other = u if v == node_id else v
                if other in stubs and other not in keep:
                    pending.append(other)
    graph.remove_nodes_from(stubs - keep)
