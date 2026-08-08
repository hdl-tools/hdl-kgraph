"""VHDL parser backend (M3).

Implementation notes:

* Grammar: ``jpt13653903/tree-sitter-vhdl`` (the PyPI ``tree-sitter-vhdl``
  package); see docs/grammar-bakeoff.md for the decision and its caveats.
  The tree is walked manually with a ``node.type`` dispatch table, mirroring
  :mod:`hdl_kgraph.parser.systemverilog`; node-type names and subtree shapes
  were confirmed with ``scripts/grammar_bakeoff.py --dump-tree``.
* VHDL identifiers are case-insensitive: every extracted name is normalized
  to lowercase in this layer (not the grammar), with the original casing
  preserved in ``attrs["original_name"]`` when it differed. Node ids and
  qualified names are therefore all-lowercase too.
* The grammar lexes identifiers it knows from the standard libraries as
  ``library_type``/``library_namespace``/``library_constant_*`` rather than
  ``identifier`` (e.g. a generic named ``WIDTH``), so names are extracted
  from *named children's text*, never by node type alone.
* M3 extracts ENTITY, ARCHITECTURE (+IMPLEMENTS ref), VHDL_PACKAGE /
  PACKAGE_BODY, CONFIGURATION (+BINDS refs), CONTEXT, generics→PARAMETER,
  ports, signals, processes, functions/procedures, and all three
  instantiation styles (component / direct entity / configuration), with
  USES_PACKAGE refs from ``use`` clauses.
* Component declarations are deliberately **not** graph nodes (the schema has
  no COMPONENT kind): an instantiation via a component records
  ``attrs["style"] = "component"`` on its INSTANTIATES ref and the pass-2
  linker resolves it — through a CONFIGURATION's BINDS data when one
  applies, else by default binding (a like-named entity, then a
  case-insensitive cross-language match). Their subtrees are skipped so a
  component's ports never masquerade as the architecture's.
* Library/work mapping happens in the pipeline: :meth:`VhdlParser.parse`
  takes the file's library name and stamps it on every design-unit node as
  ``attrs["library"]``.
* Files with tree-sitter ERROR nodes still yield partial results; the error
  count is reported in ``FileIR.parse_error_count``.
* M5 — dataflow and clocks: process bodies emit DRIVES refs for signal
  assignment targets and READS for every other referenced name (root
  identifier only; ``rec.field`` reads ``rec``; an indexed/called name
  ``f(x)`` reads ``f`` — indexing and calls are syntactically identical in
  VHDL, a documented approximation). Sensitivity-list names are READS with
  ``role="sensitivity"``. ``rising_edge(x)``/``falling_edge(x)`` anywhere in
  the body → CLOCKED_BY at 1.0 (``evidence: "edge_function"``); without one,
  a two-name sensitivity list with a unique clk/clock-pattern name →
  CLOCKED_BY at 0.4. Reset-pattern names (rst/reset/clr/clear) in the
  sensitivity list or reads → RESETS at 0.4 (``evidence: "name"``).
  Architecture-level concurrent signal assignments become PROCESS nodes
  named ``assign@<line>`` (``style="concurrent_assignment"``) with the same
  DRIVES/READS treatment. Process variables (``:=``) are local and carry no
  graph dataflow (documented limitation). These refs resolve in pass 2
  against the enclosing unit's PORT/SIGNAL children.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tree_sitter_vhdl
from tree_sitter import Language as TSLanguage
from tree_sitter import Node as TSNode
from tree_sitter import Parser as TSParser

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import (
    RESET_NAME_RE,
    FileIR,
    UnresolvedRef,
    _WalkerBase,
    error_snippet,
    validate_grammar,
)
from hdl_kgraph.schema import (
    CONFIDENCE_HEURISTIC,
    CONFIDENCE_RESOLVED,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

SUFFIXES = frozenset({".vhd", ".vhdl"})

VHDL_LANGUAGE = TSLanguage(tree_sitter_vhdl.language())

DEFAULT_LIBRARY = "work"

# Anchored to whole `_`-delimited tokens (optionally with an `_n`/`_b` polarity
# suffix) so control names like ``rst``/``rst_n``/``sys_clk`` match but data names
# that merely contain the substring (``clear_count``, ``reset_value``,
# ``restart_addr``) do not. See issue #76.
_CLOCK_NAME_RE = re.compile(r"(?:^|_)(?:clk|clock)(?:_?n|_?b)?$", re.IGNORECASE)


def _line_span(node: TSNode) -> tuple[int, int]:
    return (node.start_point[0] + 1, node.end_point[0] + 1)


@dataclass
class _Scope:
    """One level of the declaration-scope stack."""

    node_id: str
    path: str  # dotted qualified-name prefix ("" for file scope)
    port_index: int = 0
    param_index: int = 0

    def child_path(self, name: str) -> str:
        return f"{self.path}.{name}" if self.path else name


@dataclass
class _UseClause:
    """A pending ``use`` clause awaiting its design unit."""

    library: str | None
    package: str
    symbol: str
    span: tuple[int, int]


class _Walker(_WalkerBase[_Scope]):
    #: VHDL descends into an ERROR subtree: its grammar wraps whole intact
    #: regions (often the entire design_file) in a single ERROR node, so
    #: skipping — the SV strategy — would discard salvageable declarations.
    ERROR_POLICY = "descend"

    def __init__(self, ir: FileIR, relpath: str, source: bytes, library: str) -> None:
        self.ir = ir
        self.relpath = relpath
        self.source = source
        self.library = library
        self.scopes: list[_Scope] = []
        self._used_ids: set[str] = set()
        # ``use`` clauses precede the design unit they contextualize; they are
        # buffered here and flushed onto the next unit node (VHDL context
        # clauses apply to the following library unit only).
        self._pending_uses: list[_UseClause] = []
        # Libraries made visible by ``library`` clauses, to tell ``use
        # lib.pkg`` apart from ``use pkg.symbol``.
        self._known_libraries: set[str] = {DEFAULT_LIBRARY, "std", "ieee", library}

    # -- small helpers -------------------------------------------------------

    def _norm(self, node: TSNode) -> str:
        return self._text(node).lower()

    def _named_texts(self, node: TSNode) -> list[str]:
        """Texts of named children — identifiers regardless of how they lexed."""
        return [self._text(c) for c in node.children if c.is_named]

    def _unit_name(self, node: TSNode) -> str:
        """The declared name of a design unit: its first named leaf child."""
        for child in node.children:
            if child.is_named and child.child_count == 0:
                return self._text(child)
        return ""

    def _new_node(self, kind: NodeKind, name: str, ts_node: TSNode, **attrs: object) -> Node:
        """Create a node (name lowercased) in the current scope + DECLARES edge."""
        lowered = name.lower()
        if lowered != name:
            attrs["original_name"] = name
        qualified = self.scope.child_path(lowered)
        node_id = decl_node_id(self.relpath, kind, qualified)
        if node_id in self._used_ids:
            node_id = f"{node_id}@{ts_node.start_point[0] + 1}"
        self._used_ids.add(node_id)
        node = Node(
            id=node_id,
            kind=kind,
            name=lowered,
            qualified_name=qualified,
            file=self.relpath,
            line_span=_line_span(ts_node),
            language=Language.VHDL,
            attrs={k: v for k, v in attrs.items() if v is not None},
        )
        self.ir.nodes.append(node)
        self.ir.local_edges.append(
            Edge(src=self.scope.node_id, dst=node.id, kind=EdgeKind.DECLARES)
        )
        return node

    def _ref(
        self,
        kind: EdgeKind,
        src_id: str,
        target: str,
        ts_node: TSNode,
        confidence: float = CONFIDENCE_RESOLVED,
        **attrs: object,
    ) -> None:
        self.ir.unresolved_refs.append(
            UnresolvedRef(
                edge_kind=kind,
                src_id=src_id,
                target_name=target.lower(),
                line_span=_line_span(ts_node),
                attrs={k: v for k, v in attrs.items() if v is not None},
                confidence=confidence,
            )
        )

    # -- traversal -----------------------------------------------------------

    def _record_parse_error(self, node: TSNode) -> None:
        line = node.start_point[0] + 1
        if node.is_missing:
            message = f"missing `{node.type}`"
        else:
            message = f"syntax error near `{error_snippet(self._text(node))}`"
        self.ir.record_parse_error(f"{self.relpath}:{line}: {message}")

    def _visit_in_scope(self, node: TSNode, scope_node: Node) -> None:
        self.scopes.append(_Scope(node_id=scope_node.id, path=scope_node.qualified_name))
        try:
            self._visit_children(node)
        finally:
            self.scopes.pop()

    # -- context clauses -----------------------------------------------------

    def _on_library_clause(self, node: TSNode) -> None:
        names = self._child(node, "logical_name_list")
        if names is not None:
            self._known_libraries.update(t.lower() for t in self._named_texts(names))

    def _on_use_clause(self, node: TSNode) -> None:
        name_list = self._child(node, "selected_name_list") or node
        for selected in self._children(name_list, "selected_name", "name"):
            parts = [t.lower() for t in self._named_texts(selected)]
            if not parts:
                continue
            library: str | None = None
            if len(parts) >= 3:
                library, package, symbol = parts[0], parts[1], parts[-1]
            elif len(parts) == 2 and parts[0] in self._known_libraries:
                # ``use work.util_pkg;`` — the package itself, no symbol.
                library, package, symbol = parts[0], parts[1], "all"
            elif len(parts) == 2:
                # ``use util_pkg.foo;`` — already-visible package + symbol.
                package, symbol = parts[0], parts[1]
            else:
                package, symbol = parts[0], "all"
            self._pending_uses.append(
                _UseClause(
                    library=library,
                    package=package,
                    symbol=symbol,
                    span=_line_span(selected),
                )
            )

    def _flush_uses(self, unit: Node, ts_node: TSNode) -> None:
        for use in self._pending_uses:
            library = use.library or self.library
            # ``work`` means the referrer's own library.
            if library == DEFAULT_LIBRARY:
                library = self.library
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.USES_PACKAGE,
                    src_id=unit.id,
                    target_name=use.package,
                    line_span=use.span,
                    attrs={"library": library, "symbol": use.symbol},
                )
            )
        self._pending_uses.clear()

    # -- design units ----------------------------------------------------------

    def _on_entity(self, node: TSNode) -> None:
        name = self._unit_name(node)
        if not name:
            self._visit_children(node)
            return
        entity = self._new_node(NodeKind.ENTITY, name, node, library=self.library)
        self._flush_uses(entity, node)
        self._visit_in_scope(node, entity)

    def _on_architecture(self, node: TSNode) -> None:
        name = self._unit_name(node)
        entity_node = self._child(node, "name")
        of_entity = self._text(entity_node).lower() if entity_node is not None else ""
        if not name:
            self._visit_children(node)
            return
        arch = self._new_node(
            NodeKind.ARCHITECTURE,
            name,
            node,
            library=self.library,
            of_entity=of_entity or None,
        )
        self._flush_uses(arch, node)
        if of_entity:
            self._ref(EdgeKind.IMPLEMENTS, arch.id, of_entity, node, library=self.library)
        self._visit_in_scope(node, arch)

    def _on_package(self, node: TSNode) -> None:
        # ``package_declaration`` is a package header; the *body* is the
        # grammar's ``package_definition`` (it carries the ``body`` keyword).
        is_body = node.type == "package_definition"
        name = self._unit_name(node)
        if not name:
            self._visit_children(node)
            return
        kind = NodeKind.PACKAGE_BODY if is_body else NodeKind.VHDL_PACKAGE
        attrs: dict[str, object] = {"library": self.library}
        if is_body:
            attrs["of_package"] = name.lower()
        pkg = self._new_node(kind, name, node, **attrs)
        self._flush_uses(pkg, node)
        self._visit_in_scope(node, pkg)

    def _on_context(self, node: TSNode) -> None:
        name = self._unit_name(node)
        if not name:
            return
        ctx = self._new_node(NodeKind.CONTEXT, name, node, library=self.library)
        self._flush_uses(ctx, node)

    # -- entity items ----------------------------------------------------------

    def _on_generic_clause(self, node: TSNode) -> None:
        self._interface_items(node, NodeKind.PARAMETER)

    def _on_port_clause(self, node: TSNode) -> None:
        self._interface_items(node, NodeKind.PORT)

    def _interface_items(self, clause: TSNode, kind: NodeKind) -> None:
        interface_list = self._child(clause, "interface_list")
        if interface_list is None:
            return
        for decl in interface_list.children:
            if not decl.is_named:
                continue
            names = self._child(decl, "identifier_list")
            mode_ind = self._child(decl, "simple_mode_indication", "mode_indication")
            if names is None:
                continue
            direction = None
            type_text = None
            default = None
            if mode_ind is not None:
                mode = self._child(mode_ind, "mode")
                # VHDL's default port mode is ``in``.
                direction = self._text(mode).lower() if mode is not None else "in"
                subtype = self._child(mode_ind, "subtype_indication")
                type_text = self._text(subtype) if subtype is not None else None
                init = self._child(mode_ind, "initialiser")
                if init is not None:
                    default = self._text(init).removeprefix(":=").strip()
            for name in self._named_texts(names):
                if kind is NodeKind.PORT:
                    self._new_node(
                        NodeKind.PORT,
                        name,
                        decl,
                        direction=direction,
                        type_text=type_text,
                        index=self.scope.port_index,
                    )
                    self.scope.port_index += 1
                else:
                    self._new_node(
                        NodeKind.PARAMETER,
                        name,
                        decl,
                        is_generic=True,
                        type_text=type_text,
                        default=default,
                        index=self.scope.param_index,
                    )
                    self.scope.param_index += 1

    # -- architecture items ------------------------------------------------------

    def _on_signal(self, node: TSNode) -> None:
        names = self._child(node, "identifier_list")
        if names is None:
            return
        subtype = self._child(node, "subtype_indication")
        type_text = self._text(subtype) if subtype is not None else None
        for name in self._named_texts(names):
            self._new_node(NodeKind.SIGNAL, name, node, type_text=type_text)

    def _on_component_declaration(self, node: TSNode) -> None:
        # Deliberately not a graph node, and not descended into: a component's
        # ports must not be attributed to the enclosing architecture. The
        # linker resolves component instantiations by name / configuration.
        return

    def _on_process(self, node: TSNode) -> None:
        label_decl = self._child(node, "label_declaration")
        label = self._child(label_decl, "label") if label_decl is not None else None
        name = self._text(label) if label is not None else f"process@{node.start_point[0] + 1}"
        sensitivity = None
        spec = self._child(node, "sensitivity_specification")
        if spec is not None:
            sens_list = self._child(spec, "sensitivity_list")
            if sens_list is not None:
                sensitivity = [self._text(n).lower() for n in sens_list.children if n.is_named]
        proc = self._new_node(NodeKind.PROCESS, name, node, sensitivity=sensitivity)
        body = self._child(node, "sequential_block")
        if body is not None:
            self._process_dataflow(proc, body, sensitivity or [])
        self._visit_in_scope(node, proc)

    # -- dataflow (M5) ------------------------------------------------------------

    def _collect_names(self, node: TSNode, out: list[tuple[str, TSNode]]) -> None:
        """(root name, name node) pairs for every signal-shaped reference.

        A ``name`` whose first named child is an ``identifier`` is a
        reference; ``library_function``/``library_constant_*`` heads (e.g.
        ``rising_edge``, ``'0'``) are not, but their argument lists still are.
        """
        if node.type == "name":
            first = next((c for c in node.children if c.is_named), None)
            if first is not None and first.type == "identifier":
                out.append((self._text(first).lower(), node))
            for child in node.children:
                if child.is_named and child is not first:
                    self._collect_names(child, out)
            return
        for child in node.children:
            self._collect_names(child, out)

    def _edge_function_clocks(self, body: TSNode) -> list[tuple[str, str, TSNode]]:
        """(clock name, edge, site) for rising_edge/falling_edge calls."""
        clocks: list[tuple[str, str, TSNode]] = []

        def walk(n: TSNode) -> None:
            if n.type == "name":
                first = next((c for c in n.children if c.is_named), None)
                if first is not None and first.type == "library_function":
                    fn = self._text(first).lower()
                    if fn in ("rising_edge", "falling_edge"):
                        args: list[tuple[str, TSNode]] = []
                        self._collect_names(n, args)
                        if args:
                            edge = "posedge" if fn == "rising_edge" else "negedge"
                            clocks.append((args[0][0], edge, n))
            for child in n.children:
                walk(child)

        walk(body)
        return clocks

    def _process_dataflow(self, proc: Node, body: TSNode, sensitivity: list[str]) -> None:
        targets: list[TSNode] = []

        def find_assignments(n: TSNode) -> None:
            if n.type == "simple_waveform_assignment":
                target = self._child(n, "name")
                if target is not None:
                    targets.append(target)
            for child in n.children:
                find_assignments(child)

        find_assignments(body)
        target_ids = {t.id for t in targets}
        names: list[tuple[str, TSNode]] = []
        self._collect_names(body, names)

        emitted: set[tuple[EdgeKind, str]] = set()

        def emit(
            kind: EdgeKind,
            name: str,
            site: TSNode,
            confidence: float = CONFIDENCE_RESOLVED,
            **attrs: object,
        ) -> None:
            if not name or (kind, name) in emitted:
                return
            emitted.add((kind, name))
            self._ref(kind, proc.id, name, site, confidence, **attrs)

        for target in targets:
            root = next((c for c in target.children if c.is_named), None)
            if root is not None and root.type == "identifier":
                emit(EdgeKind.DRIVES, self._text(root).lower(), target, role="lhs")
        for name, site in names:
            if site.id in target_ids:
                continue
            emit(EdgeKind.READS, name, site, role="rhs")
        for name in sensitivity:
            emit(EdgeKind.READS, name, body, role="sensitivity")

        # Clocks: rising_edge/falling_edge is definitive; otherwise a unique
        # clk-pattern name in a short sensitivity list is a 0.4 heuristic.
        clocks = self._edge_function_clocks(body)
        clock_names = set()
        for name, edge, site in clocks:
            if name not in clock_names:
                clock_names.add(name)
                emit(EdgeKind.CLOCKED_BY, name, site, evidence="edge_function", edge=edge)
        if not clocks and len(sensitivity) <= 2:
            candidates = [n for n in sensitivity if _CLOCK_NAME_RE.search(n)]
            if len(candidates) == 1:
                clock_names.add(candidates[0])
                emit(
                    EdgeKind.CLOCKED_BY,
                    candidates[0],
                    body,
                    confidence=CONFIDENCE_HEURISTIC,
                    evidence="name",
                )
        reads = {name for kind, name in emitted if kind is EdgeKind.READS}
        for name in sorted(reads | set(sensitivity)):
            if RESET_NAME_RE.search(name) and name not in clock_names:
                emit(
                    EdgeKind.RESETS,
                    name,
                    body,
                    confidence=CONFIDENCE_HEURISTIC,
                    evidence="name",
                )

    def _on_concurrent_assignment(self, node: TSNode) -> None:
        assign_op = self._child(node, "signal_assignment")
        proc = self._new_node(
            NodeKind.PROCESS,
            f"assign@{node.start_point[0] + 1}",
            node,
            style="concurrent_assignment",
        )
        target: TSNode | None = None
        for child in node.children:
            if child.type == "name" and (
                assign_op is None or child.end_byte <= assign_op.start_byte
            ):
                target = child  # last name before the <= is the target
        if target is not None:
            root = next((c for c in target.children if c.is_named), None)
            if root is not None and root.type == "identifier":
                self._ref(EdgeKind.DRIVES, proc.id, self._text(root).lower(), target, role="lhs")
        names: list[tuple[str, TSNode]] = []
        self._collect_names(node, names)
        seen: set[str] = set()
        for name, site in names:
            if target is not None and site.id == target.id:
                continue
            if name not in seen:
                seen.add(name)
                self._ref(EdgeKind.READS, proc.id, name, site, role="rhs")

    def _on_subprogram(self, node: TSNode) -> None:
        spec = self._child(node, "function_specification", "procedure_specification")
        if spec is None:
            return
        name = self._unit_name(spec)
        if not name:
            return
        kind = NodeKind.FUNCTION if spec.type == "function_specification" else NodeKind.TASK
        self._new_node(kind, name, node)

    # -- instantiations ------------------------------------------------------------

    def _on_instantiation(self, node: TSNode) -> None:
        label_decl = self._child(node, "label_declaration")
        label = self._child(label_decl, "label") if label_decl is not None else None
        if label is None:
            return
        style, target, library, architecture = self._instantiated_unit(node)
        if not target:
            return
        inst = self._new_node(
            NodeKind.INSTANCE,
            self._text(label),
            node,
            target=target,
            style=style,
            library=library,
            architecture=architecture,
        )
        self._ref(
            EdgeKind.INSTANTIATES,
            inst.id,
            target,
            node,
            style=style,
            library=library,
            architecture=architecture,
        )
        self._collect_map(node, "generic_map_aspect", EdgeKind.PARAMETERIZES, inst, target)
        self._collect_map(node, "port_map_aspect", EdgeKind.CONNECTS, inst, target)

    def _instantiated_unit(self, node: TSNode) -> tuple[str, str, str | None, str | None]:
        """Classify an instantiation: (style, target, library, architecture)."""
        unit = self._child(node, "instantiated_unit")
        if unit is None:
            # Bare component name: ``u1 : fifo``.
            name = self._child(node, "name")
            return ("component", self._norm(name) if name is not None else "", None, None)
        style = "configuration" if self._child(unit, "configuration") is not None else "entity"
        return (style, *self._parse_unit_name(unit))

    def _parse_unit_name(self, unit: TSNode) -> tuple[str, str | None, str | None]:
        """(target, library, architecture) from an instantiated_unit /
        entity_aspect — the grammar shapes ``work.alu(rtl)`` two ways."""
        library = None
        lib_ns = self._child(unit, "library_namespace")
        if lib_ns is not None:
            library = self._norm(lib_ns)
        name = self._child(unit, "name")
        target = self._norm(name) if name is not None else ""
        if "." in target:
            library, _, target = target.rpartition(".")
        # A trailing identifier outside the name is the architecture:
        # ``entity work.alu(rtl)``.
        architecture = None
        if name is not None:
            siblings = [c for c in unit.children if c.is_named and c.start_byte > name.end_byte]
            if siblings:
                architecture = self._norm(siblings[0])
        return (target, library, architecture)

    def _collect_map(
        self, node: TSNode, aspect_type: str, kind: EdgeKind, inst: Node, target: str
    ) -> None:
        aspect = self._child(node, aspect_type)
        assoc_list = self._child(aspect, "association_list") if aspect is not None else None
        if assoc_list is None:
            return
        name_key = "port_name" if kind is EdgeKind.CONNECTS else "param_name"
        value_key = "expr_text" if kind is EdgeKind.CONNECTS else "value_text"
        position = 0
        for element in self._children(assoc_list, "association_element"):
            # Named ``formal => actual`` has a direct ``name`` child for the
            # formal; a positional actual's expression is the only child.
            formal = self._child(element, "name")
            expr = self._child(element, "conditional_expression", "open")
            attrs: dict[str, Any] = {value_key: self._text(expr) if expr is not None else ""}
            if formal is not None:
                attrs[name_key] = self._norm(formal)
                attrs["position"] = None
            else:
                attrs[name_key] = None
                attrs["position"] = position
                position += 1
            if kind is EdgeKind.CONNECTS:
                attrs["wildcard"] = False
            self._ref(kind, inst.id, target, element, **attrs)

    # -- configurations ------------------------------------------------------------

    def _on_configuration(self, node: TSNode) -> None:
        name = self._unit_name(node)
        entity_node = self._child(node, "name")
        of_entity = self._text(entity_node).lower() if entity_node is not None else ""
        if not name:
            return
        cfg = self._new_node(
            NodeKind.CONFIGURATION,
            name,
            node,
            library=self.library,
            of_entity=of_entity or None,
        )
        self._flush_uses(cfg, node)
        if of_entity:
            self._ref(
                EdgeKind.BINDS, cfg.id, of_entity, node, role="configures", library=self.library
            )
        for block in self._children(node, "block_configuration"):
            arch_node = self._child(block, "name")
            arch = self._text(arch_node).lower() if arch_node is not None else None
            for comp_cfg in self._children(block, "component_configuration"):
                self._component_configuration(cfg, of_entity, arch, comp_cfg)

    def _component_configuration(
        self, cfg: Node, of_entity: str, arch: str | None, node: TSNode
    ) -> None:
        spec = self._child(node, "component_specification")
        binding = self._child(node, "binding_indication")
        if spec is None or binding is None:
            return
        inst_list = self._child(spec, "instantiation_list")
        component_node = self._child(spec, "name")
        if inst_list is None or component_node is None:
            return
        labels = [self._text(c).lower() for c in inst_list.children if c.is_named]
        instances: object = labels
        if len(labels) == 1 and labels[0] in ("all", "others"):
            instances = labels[0]
        aspect = self._child(binding, "entity_aspect")
        if aspect is None:
            return
        target, library, bound_arch = self._parse_unit_name(aspect)
        if not target:
            return
        self._ref(
            EdgeKind.BINDS,
            cfg.id,
            target,
            node,
            role="binding",
            of_entity=of_entity or None,
            block=arch,
            component=self._norm(component_node),
            instances=instances,
            library=library,
            architecture=bound_arch,
        )

    _DISPATCH = {
        "library_clause": _on_library_clause,
        "use_clause": _on_use_clause,
        "entity_declaration": _on_entity,
        "architecture_definition": _on_architecture,
        "package_declaration": _on_package,
        "package_definition": _on_package,  # package body
        "context_declaration": _on_context,
        "configuration_declaration": _on_configuration,
        "generic_clause": _on_generic_clause,
        "port_clause": _on_port_clause,
        "signal_declaration": _on_signal,
        "component_declaration": _on_component_declaration,
        "process_statement": _on_process,
        "subprogram_declaration": _on_subprogram,
        "subprogram_definition": _on_subprogram,
        "component_instantiation_statement": _on_instantiation,
        "concurrent_simple_signal_assignment": _on_concurrent_assignment,
        "concurrent_conditional_signal_assignment": _on_concurrent_assignment,
        "concurrent_selected_signal_assignment": _on_concurrent_assignment,
    }


#: Validate the grammar's node-type surface once (the names the walker
#: dispatches on); a rename upstream would otherwise silently under-extract.
_grammar_validated = False


def _validate_vhdl_grammar() -> None:
    global _grammar_validated
    if _grammar_validated:
        return
    validate_grammar(
        VHDL_LANGUAGE,
        set(_Walker._DISPATCH) | {"identifier"},
        grammar="tree-sitter-vhdl grammar",
    )
    _grammar_validated = True


class VhdlParser:
    """Tree-sitter based VHDL pass-1 parser (M3)."""

    suffixes = SUFFIXES

    def __init__(self) -> None:
        _validate_vhdl_grammar()
        self._parser = TSParser(VHDL_LANGUAGE)

    def parse(self, path: Path, text: str, library: str = DEFAULT_LIBRARY) -> FileIR:
        """Parse one file into its per-file IR.

        *path* should be relative to the build root. *library* is the VHDL
        library this file compiles into (from the ``--lib``/config mapping;
        default ``work``) and is stamped on every design-unit node.
        """
        relpath = path.as_posix()
        ir = FileIR(path=relpath)
        file_node = Node(
            id=file_node_id(relpath),
            kind=NodeKind.FILE,
            name=path.name,
            qualified_name=relpath,
            file=relpath,
            language=Language.VHDL,
            attrs={"library": library},
        )
        ir.nodes.append(file_node)
        source = text.encode()
        try:
            tree = self._parser.parse(source)
            walker = _Walker(ir, relpath, source, library)
            walker.scopes.append(_Scope(node_id=file_node.id, path=""))
            walker.visit(tree.root_node)
        except Exception as exc:  # defensive: a walker bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
        return ir
