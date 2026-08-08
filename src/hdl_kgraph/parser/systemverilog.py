"""SystemVerilog / Verilog parser backend (M1).

Implementation notes:

* One grammar serves both ``.v`` and ``.sv``: ``tree-sitter-systemverilog``
  (gmlarumbe), chosen by the bake-off recorded in docs/grammar-bakeoff.md.
* The tree is walked manually with a ``node.type`` dispatch table (the Query
  API churned across py-tree-sitter releases; see ROADMAP Risk #5). Node-type
  names follow the IEEE 1800 BNF and were confirmed with
  ``scripts/grammar_bakeoff.py --dump-tree``.
* M1 extracts MODULE, INTERFACE, PACKAGE, PROGRAM, FUNCTION/TASK, PORT,
  PARAMETER, INSTANCE, TYPEDEF/STRUCT/ENUM, and CLASS (declaration + EXTENDS),
  with DECLARES edges locally and INSTANTIATES / CONNECTS / PARAMETERIZES /
  IMPORTS / EXTENDS recorded as :class:`UnresolvedRef` for the pass-2 linker.
* Files containing tree-sitter ERROR nodes still yield partial results; the
  error count is reported in ``FileIR.parse_error_count``.
* M2: ``parse`` accepts the preprocessor's line map. Spans and node ids then
  attribute to the *original* file and line — declarations spliced from a
  ``\\`include`` belong to the header, and nodes from non-selected
  both-branches regions carry ``attrs["conditional"]`` with their DECLARES
  edge and refs at ``CONFIDENCE_AMBIGUOUS``. A node straddling an include
  boundary keeps the start line's file with a collapsed span (documented
  limitation).
* M5 — dataflow, clocks, and verification constructs:

  - Module/interface/program-scope net and variable declarations become
    SIGNAL nodes (``attrs``: ``net_type``, ``type_text``, ``is_net``).
    Class properties and function/task locals deliberately do not.
  - ``assign`` statements and always blocks become PROCESS nodes named
    ``assign@<line>`` / ``always@<line>`` (``attrs["style"]``; explicit
    sensitivity lists in ``attrs["sensitivity"]`` as ``{"edge", "name"}``
    dicts — lists, never tuples, so cached IRs round-trip identically).
    A declaration initializer (``wire x = ...``) is its own ``assign@<line>``
    PROCESS with ``attrs["decl_init"]``, per the LRM equivalence.
  - DRIVES/READS refs go from the PROCESS to *root identifiers*:
    ``mem[addr] <= w`` drives ``mem`` and reads ``addr``/``w``;
    ``top.sub.sig`` reads ``top``. Subroutine callee names, ``pkg::`` scope
    prefixes, system tasks, and macro usages are excluded; names declared
    inside the block (for-loop variables, automatic locals) are too. These
    refs resolve in pass 2 against the *enclosing unit's* PORT/SIGNAL
    children (see graph/builder.py), never by global name.
  - CLOCKED_BY/RESETS: one edge term in the event control → CLOCKED_BY at
    1.0 (``evidence: "sensitivity"``); several → reset-named terms
    (rst/reset/clr/clear) become RESETS at 1.0 (``async: True``) and a
    unique remaining term CLOCKED_BY at 1.0, else each unclassified term
    CLOCKED_BY at 0.4. A reset-named signal *read* in a clocked block is a
    sync-reset RESETS at 0.4 (``evidence: "name"``).
  - PROPERTY/SEQUENCE/ASSERTION (labeled or ``assert@<line>``; statement
    flavor in ``attrs["statement"]``), COVERGROUP/COVERPOINT, CONSTRAINT,
    and CLOCKING_BLOCK nodes; ASSERTS_ON refs from assertion/property/
    sequence bodies (resolved to a sibling PROPERTY/SEQUENCE first, then
    PORT/SIGNAL), COVERS refs from coverpoint expressions, CLOCKED_BY from
    their clocking events. Immediate (procedural) assertions are out of
    scope for M5.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_systemverilog
from tree_sitter import Language as TSLanguage
from tree_sitter import Node as TSNode
from tree_sitter import Parser as TSParser

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import (
    FileIR,
    UnresolvedRef,
    _WalkerBase,
    error_snippet,
    validate_grammar,
)
from hdl_kgraph.parser.preprocessor import LineOrigin
from hdl_kgraph.parser.sv_normalize import normalize_sv_source
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_HEURISTIC,
    CONFIDENCE_RESOLVED,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})
SYSTEMVERILOG_SUFFIXES = frozenset({".sv", ".svh"})

SV_LANGUAGE = TSLanguage(tree_sitter_systemverilog.language())

_HEADER_TYPES = frozenset(
    {
        "module_ansi_header",
        "module_nonansi_header",
        "interface_ansi_header",
        "interface_nonansi_header",
        "program_ansi_header",
        "program_nonansi_header",
    }
)

_IDENTIFIER_TYPES = ("simple_identifier", "escaped_identifier")

#: Scopes whose nets/variables are design signals (dataflow endpoints).
_DATAFLOW_SCOPE_KINDS = frozenset({NodeKind.MODULE, NodeKind.INTERFACE, NodeKind.PROGRAM})

#: Procedural/continuous assignment node types (lvalue child + RHS).
_ASSIGNMENT_TYPES = frozenset({"nonblocking_assignment", "operator_assignment", "net_assignment"})

# Anchored to whole `_`-delimited tokens (optionally with an `_n`/`_b` polarity
# suffix) so a control name like ``rst``/``rst_n``/``sys_rst`` matches but a data
# name that merely contains the substring (``clear_count``, ``reset_value``,
# ``restart_addr``) does not. See issue #76.
_RESET_NAME_RE = re.compile(r"(?:^|_)(?:rst|reset|clr|clear)(?:_?n|_?b)?$", re.IGNORECASE)

_ASSERT_STATEMENT_TYPES = {
    "assert_property_statement": "assert",
    "assume_property_statement": "assume",
    "cover_property_statement": "cover",
    "cover_sequence_statement": "cover",
    "restrict_property_statement": "restrict",
}


def _line_span(node: TSNode) -> tuple[int, int]:
    return (node.start_point[0] + 1, node.end_point[0] + 1)


@dataclass
class _Scope:
    """One level of the declaration-scope stack."""

    node_id: str
    path: str  # dotted qualified-name prefix ("" for file scope)
    kind: NodeKind | None = None
    port_index: int = 0
    param_index: int = 0
    last_port_direction: str = ""
    # name -> PORT node, for non-ANSI direction back-fill
    ports: dict[str, Node] = field(default_factory=dict)

    def child_path(self, name: str) -> str:
        return f"{self.path}.{name}" if self.path else name


class _Walker(_WalkerBase[_Scope]):
    #: SV skips an ERROR subtree (partial results from the siblings).
    ERROR_POLICY = "skip"

    def __init__(
        self,
        ir: FileIR,
        relpath: str,
        language: Language,
        source: bytes,
        line_map: Sequence[LineOrigin] | None = None,
    ) -> None:
        self.ir = ir
        self.relpath = relpath
        self.language = language
        self.source = source
        self.line_map = line_map
        self.scopes: list[_Scope] = []
        self._used_ids: set[str] = set()
        self._header_file_ids: set[str] = set()

    # -- small helpers -------------------------------------------------------

    def _origin(self, node: TSNode) -> LineOrigin:
        return self._origin_at(node.start_point[0])

    def _origin_at(self, row: int) -> LineOrigin:
        if not self.line_map:
            return LineOrigin(file=self.relpath, line=row + 1)
        return self.line_map[min(row, len(self.line_map) - 1)]

    def _span(self, node: TSNode) -> tuple[int, int]:
        if self.line_map is None:
            return _line_span(node)
        start = self._origin(node)
        end = self._origin_at(node.end_point[0])
        if end.file != start.file:  # straddles an include boundary
            return (start.line, start.line)
        return (start.line, max(start.line, end.line))

    def _ref_confidence(self, node: TSNode) -> float:
        return CONFIDENCE_AMBIGUOUS if self._origin(node).ambiguous else CONFIDENCE_RESOLVED

    def _find_first(self, node: TSNode, type_: str, max_depth: int = 3) -> TSNode | None:
        if max_depth < 0:
            return None
        for child in node.children:
            if child.type == type_:
                return child
            found = self._find_first(child, type_, max_depth - 1)
            if found is not None:
                return found
        return None

    def _identifier(self, node: TSNode) -> str:
        ident = self._child(node, *_IDENTIFIER_TYPES)
        return self._text(ident) if ident is not None else ""

    def _new_node(self, kind: NodeKind, name: str, ts_node: TSNode, **attrs: object) -> Node:
        """Create a node in the current scope and emit its DECLARES edge."""
        origin = self._origin(ts_node)
        qualified = self.scope.child_path(name)
        node_id = decl_node_id(origin.file, kind, qualified)
        if node_id in self._used_ids:
            node_id = f"{node_id}@{origin.line}"
            if node_id in self._used_ids:  # e.g. the same header spliced twice
                node_id = f"{node_id}.{ts_node.start_point[0] + 1}"
        self._used_ids.add(node_id)
        if origin.ambiguous:
            attrs["conditional"] = True
        node = Node(
            id=node_id,
            kind=kind,
            name=name,
            qualified_name=qualified,
            file=origin.file,
            line_span=self._span(ts_node),
            language=self.language,
            attrs={k: v for k, v in attrs.items() if v is not None},
        )
        self.ir.nodes.append(node)
        # A file-scope declaration spliced from a header belongs to the
        # header's FILE node, not the including unit's. Emit a minimal FILE
        # node for the header so the IR stands alone even when the
        # preprocessor's include-event stub is absent; the linker keeps the
        # richer node by first occurrence.
        src = self.scope.node_id
        if len(self.scopes) == 1 and origin.file != self.relpath:
            src = file_node_id(origin.file)
            if src not in self._header_file_ids:
                self._header_file_ids.add(src)
                self.ir.nodes.append(
                    Node(
                        id=src,
                        kind=NodeKind.FILE,
                        name=origin.file.rsplit("/", 1)[-1],
                        qualified_name=origin.file,
                        file=origin.file,
                        language=self.language,
                    )
                )
        self.ir.local_edges.append(
            Edge(
                src=src,
                dst=node.id,
                kind=EdgeKind.DECLARES,
                confidence=(CONFIDENCE_AMBIGUOUS if origin.ambiguous else CONFIDENCE_RESOLVED),
            )
        )
        return node

    # -- traversal -----------------------------------------------------------

    def _record_parse_error(self, node: TSNode) -> None:
        origin = self._origin(node)
        if node.is_missing:
            message = f"missing `{node.type}`"
        else:
            message = f"syntax error near `{error_snippet(self._text(node))}`"
        self.ir.record_parse_error(f"{origin.file}:{origin.line}: {message}")

    def _visit_in_scope(self, node: TSNode, scope_node: Node) -> None:
        self.scopes.append(
            _Scope(node_id=scope_node.id, path=scope_node.qualified_name, kind=scope_node.kind)
        )
        try:
            self._visit_children(node)
        finally:
            self.scopes.pop()

    def _in_dataflow_scope(self) -> bool:
        return self.scope.kind in _DATAFLOW_SCOPE_KINDS

    # -- design units ----------------------------------------------------------

    def _on_design_unit(self, node: TSNode) -> None:
        kind = {
            "module_declaration": NodeKind.MODULE,
            "interface_declaration": NodeKind.INTERFACE,
            "program_declaration": NodeKind.PROGRAM,
        }[node.type]
        header = self._child(node, *_HEADER_TYPES) or node
        name = self._identifier(header)
        if not name:
            self._visit_children(node)
            return
        attrs: dict[str, object] = {}
        keyword = self._child(header, "module_keyword")
        if keyword is not None and self._text(keyword) == "macromodule":
            attrs["is_macromodule"] = True
        unit = self._new_node(kind, name, node, **attrs)
        self._visit_in_scope(node, unit)

    def _on_package(self, node: TSNode) -> None:
        name = self._identifier(node)
        if not name:
            self._visit_children(node)
            return
        pkg = self._new_node(NodeKind.PACKAGE, name, node)
        self._visit_in_scope(node, pkg)

    def _on_class(self, node: TSNode) -> None:
        name = self._identifier(node)
        if not name:
            self._visit_children(node)
            return
        is_virtual = self._child(node, "virtual") is not None
        cls = self._new_node(NodeKind.CLASS, name, node, is_virtual=is_virtual or None)
        if self._child(node, "extends") is not None:
            base = self._child(node, "class_type")
            if base is not None:
                self._record_extends(cls, base)
        self._visit_in_scope(node, cls)

    def _record_extends(self, cls: Node, base: TSNode) -> None:
        text = self._text(base)
        param_args = None
        if "#" in text:
            text, _, args = text.partition("#")
            param_args = "#" + args.strip()
        package = None
        if "::" in text:
            package, _, text = text.rpartition("::")
        self.ir.unresolved_refs.append(
            UnresolvedRef(
                edge_kind=EdgeKind.EXTENDS,
                src_id=cls.id,
                target_name=text.strip(),
                line_span=self._span(base),
                attrs={"package": package, "param_args_text": param_args},
                confidence=self._ref_confidence(base),
            )
        )

    # -- ports and parameters ----------------------------------------------------

    def _on_ansi_port(self, node: TSNode) -> None:
        name = ""
        name_node: TSNode | None = None
        for child in reversed(node.children):
            if child.type in _IDENTIFIER_TYPES:
                name_node = child
                name = self._text(child)
                break
        if not name or name_node is None:
            return
        direction_node = self._find_first(node, "port_direction")
        direction = self._text(direction_node) if direction_node is not None else ""
        if direction:
            self.scope.last_port_direction = direction
        else:
            # `input logic a, b` continues the direction of the previous port.
            direction = self.scope.last_port_direction
        type_node = self._child(
            node, "net_port_header", "variable_port_header", "interface_port_header"
        )
        port = self._new_node(
            NodeKind.PORT,
            name,
            node,
            direction=direction or None,
            type_text=self._text(type_node) if type_node is not None else None,
            index=self.scope.port_index,
        )
        self.scope.port_index += 1
        self.scope.ports[name] = port

    def _on_nonansi_port(self, node: TSNode) -> None:
        # `port` inside a non-ANSI header's list_of_ports: name + index now;
        # direction is back-filled by the body's port_declaration.
        name = self._identifier(node) or self._text(node).strip()
        if not name or name in self.scope.ports:
            return
        port = self._new_node(NodeKind.PORT, name, node, index=self.scope.port_index)
        self.scope.port_index += 1
        self.scope.ports[name] = port

    def _on_port_declaration(self, node: TSNode) -> None:
        decl = self._child(node, "input_declaration", "output_declaration", "inout_declaration")
        if decl is None:
            return
        direction = decl.type.split("_", 1)[0]  # input | output | inout
        names = self._child(decl, "list_of_port_identifiers", "list_of_variable_identifiers")
        if names is None:
            return
        for ident in self._children(names, *_IDENTIFIER_TYPES):
            name = self._text(ident)
            port = self.scope.ports.get(name)
            if port is not None:
                port.attrs["direction"] = direction
            else:
                port = self._new_node(
                    NodeKind.PORT, name, node, direction=direction, index=self.scope.port_index
                )
                self.scope.port_index += 1
                self.scope.ports[name] = port

    def _on_parameter_declaration(self, node: TSNode) -> None:
        self._count_subtree_errors(node)
        is_localparam = node.type == "local_parameter_declaration"
        assignments = self._child(node, "list_of_param_assignments", "list_of_type_assignments")
        if assignments is None:
            return
        for assignment in self._children(assignments, "param_assignment", "type_assignment"):
            name = self._identifier(assignment)
            if not name:
                continue
            default = self._child(assignment, "constant_param_expression")
            index = None
            if not is_localparam:
                index = self.scope.param_index
                self.scope.param_index += 1
            self._new_node(
                NodeKind.PARAMETER,
                name,
                assignment,
                is_localparam=is_localparam,
                default=self._text(default) if default is not None else None,
                index=index,
            )

    # -- typedefs ------------------------------------------------------------------

    def _on_type_declaration(self, node: TSNode) -> None:
        self._count_subtree_errors(node)
        name = self._identifier(node)
        if not name:
            return
        data_type = self._child(node, "data_type")
        kind = NodeKind.TYPEDEF
        if data_type is not None:
            if self._child(data_type, "enum") is not None:
                kind = NodeKind.ENUM
            elif self._child(data_type, "struct_union") is not None:
                kind = NodeKind.STRUCT
        type_node = self._new_node(kind, name, node)
        if kind is NodeKind.ENUM and data_type is not None:
            self.scopes.append(_Scope(node_id=type_node.id, path=type_node.qualified_name))
            try:
                for member in self._children(data_type, "enum_name_declaration"):
                    member_name = self._identifier(member)
                    if member_name:
                        self._new_node(NodeKind.ENUM_MEMBER, member_name, member)
            finally:
                self.scopes.pop()

    # -- functions and tasks ----------------------------------------------------------

    def _on_function(self, node: TSNode) -> None:
        body = self._child(node, "function_body_declaration")
        name = self._identifier(body) if body is not None else ""
        if not name:
            return
        fn = self._new_node(NodeKind.FUNCTION, name, node)
        self._visit_in_scope(node, fn)

    def _on_task(self, node: TSNode) -> None:
        body = self._child(node, "task_body_declaration")
        name = self._identifier(body) if body is not None else ""
        if not name:
            return
        task = self._new_node(NodeKind.TASK, name, node)
        self._visit_in_scope(node, task)

    def _on_constructor(self, node: TSNode) -> None:
        self._new_node(NodeKind.FUNCTION, "new", node, is_constructor=True)

    # -- DPI-C foreign bindings (M8) --------------------------------------------------

    def _on_dpi(self, node: TSNode) -> None:
        """``import``/``export "DPI-C"`` → a FUNCTION/TASK node + FOREIGN_BINDS ref.

        An *import* declares an SV-side prototype that the linker binds to a
        C/C++ function definition (by the linkage name — the ``c_name =`` alias
        if present, else the SV name). An *export* makes an existing SV
        subprogram callable from C; its ref resolves locally to that subprogram.
        """
        self._count_subtree_errors(node)
        c_ident = self._child(node, "c_identifier")
        c_name = self._text(c_ident) if c_ident is not None else ""
        if self._child(node, "export") is not None:
            name = self._identifier(node)  # `export "DPI-C" function|task <name>;`
            if not name:
                return
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.FOREIGN_BINDS,
                    src_id=self.scope.node_id,
                    target_name=name,
                    line_span=self._span(node),
                    attrs={"dpi_export": True, "linkage": "DPI-C", "c_identifier": c_name or None},
                    confidence=self._ref_confidence(node),
                )
            )
            return
        task_proto = self._child(node, "dpi_task_proto")
        proto = task_proto if task_proto is not None else self._child(node, "dpi_function_proto")
        if proto is None:
            return
        name_node = self._find_first(proto, "simple_identifier")
        name = self._text(name_node) if name_node is not None else ""
        if not name:
            return
        kind = NodeKind.TASK if task_proto is not None else NodeKind.FUNCTION
        prop = self._child(node, "dpi_function_import_property", "dpi_task_import_property")
        fn = self._new_node(
            kind,
            name,
            node,
            dpi_import=True,
            linkage="DPI-C",
            c_identifier=c_name or None,
            dpi_property=self._text(prop).strip() if prop is not None else None,
        )
        self.ir.unresolved_refs.append(
            UnresolvedRef(
                edge_kind=EdgeKind.FOREIGN_BINDS,
                src_id=fn.id,
                target_name=c_name or name,
                line_span=self._span(node),
                attrs={"linkage": "DPI-C"},
                confidence=self._ref_confidence(node),
            )
        )

    # -- instantiations ------------------------------------------------------------------

    def _on_instantiation(self, node: TSNode) -> None:
        self._count_subtree_errors(node)
        target = self._identifier(node)
        if not target:
            return
        param_overrides = self._collect_param_overrides(node)
        for hier in self._children(node, "hierarchical_instance"):
            name_node = self._child(hier, "name_of_instance")
            inst_name = self._identifier(name_node) if name_node is not None else ""
            if not inst_name:
                continue
            inst = self._new_node(NodeKind.INSTANCE, inst_name, hier, target=target)
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.INSTANTIATES,
                    src_id=inst.id,
                    target_name=target,
                    line_span=self._span(hier),
                    confidence=self._ref_confidence(hier),
                )
            )
            for override in param_overrides:
                self.ir.unresolved_refs.append(
                    UnresolvedRef(
                        edge_kind=EdgeKind.PARAMETERIZES,
                        src_id=inst.id,
                        target_name=target,
                        line_span=self._span(hier),
                        attrs=dict(override),
                        confidence=self._ref_confidence(hier),
                    )
                )
            self._collect_connections(hier, inst, target)

    def _collect_param_overrides(self, node: TSNode) -> list[dict[str, object]]:
        pva = self._child(node, "parameter_value_assignment")
        assignments = (
            self._child(pva, "list_of_parameter_value_assignments") if pva is not None else None
        )
        if assignments is None:
            return []
        overrides: list[dict[str, object]] = []
        position = 0
        for child in assignments.children:
            if child.type == "named_parameter_assignment":
                expr = self._child(child, "param_expression")
                overrides.append(
                    {
                        "param_name": self._identifier(child),
                        "position": None,
                        "value_text": self._text(expr) if expr is not None else "",
                    }
                )
            elif child.type == "ordered_parameter_assignment":
                overrides.append(
                    {"param_name": None, "position": position, "value_text": self._text(child)}
                )
                position += 1
        return overrides

    def _collect_connections(self, hier: TSNode, inst: Node, target: str) -> None:
        connections = self._child(hier, "list_of_port_connections")
        if connections is None:
            return
        position = 0
        for child in connections.children:
            attrs: dict[str, object] | None = None
            if child.type == "named_port_connection":
                if self._child(child, ".*") is not None:
                    attrs = {
                        "port_name": None,
                        "position": None,
                        "wildcard": True,
                        "expr_text": ".*",
                    }
                else:
                    expr = self._child(child, "expression")
                    port_name = self._identifier(child)
                    if expr is not None:
                        expr_text = self._text(expr)
                    elif self._child(child, "(") is not None:
                        expr_text = ""  # `.name()` — explicitly open
                    else:
                        expr_text = port_name  # `.name` shorthand: like-named signal
                    attrs = {
                        "port_name": port_name,
                        "position": None,
                        "wildcard": False,
                        "expr_text": expr_text,
                    }
            elif child.type == "ordered_port_connection":
                attrs = {
                    "port_name": None,
                    "position": position,
                    "wildcard": False,
                    "expr_text": self._text(child),
                }
                position += 1
            if attrs is not None:
                self.ir.unresolved_refs.append(
                    UnresolvedRef(
                        edge_kind=EdgeKind.CONNECTS,
                        src_id=inst.id,
                        target_name=target,
                        line_span=self._span(child),
                        attrs=attrs,
                        confidence=self._ref_confidence(child),
                    )
                )

    # -- imports -------------------------------------------------------------------------

    def _on_import(self, node: TSNode) -> None:
        self._count_subtree_errors(node)
        for item in self._children(node, "package_import_item"):
            idents = self._children(item, *_IDENTIFIER_TYPES)
            if not idents:
                continue
            package = self._text(idents[0])
            symbol = self._text(idents[1]) if len(idents) > 1 else "*"
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.IMPORTS,
                    src_id=self.scope.node_id,
                    target_name=package,
                    line_span=self._span(item),
                    attrs={"symbol": symbol},
                    confidence=self._ref_confidence(item),
                )
            )

    # -- dataflow (M5) -------------------------------------------------------------

    def _dataflow_ref(
        self,
        kind: EdgeKind,
        src_id: str,
        name: str,
        ts_node: TSNode,
        confidence: float | None = None,
        **attrs: object,
    ) -> None:
        """A scoped ref: pass 2 resolves *name* in the enclosing unit."""
        self.ir.unresolved_refs.append(
            UnresolvedRef(
                edge_kind=kind,
                src_id=src_id,
                target_name=name,
                line_span=self._span(ts_node),
                attrs={k: v for k, v in attrs.items() if v is not None},
                confidence=(
                    min(confidence, self._ref_confidence(ts_node))
                    if confidence is not None
                    else self._ref_confidence(ts_node)
                ),
            )
        )

    def _hier_root(self, node: TSNode) -> str:
        """Root identifier of a (possibly dotted) hierarchical_identifier."""
        ident = self._child(node, *_IDENTIFIER_TYPES)
        return self._text(ident) if ident is not None else ""

    def _collect_signal_idents(
        self, node: TSNode, out: list[TSNode], skip: frozenset[str] = frozenset()
    ) -> None:
        """hierarchical_identifier nodes under *node* that name signals.

        Skips subroutine callee names and ``pkg::``/object prefixes of method
        calls; system-task names and macro usages never produce
        hierarchical_identifier nodes, so they are excluded structurally.
        *skip* prunes whole subtrees (e.g. a body walk skipping its event
        controls, which the clock/reset extraction handles).
        """
        if node.type in skip:
            return
        if node.type == "hierarchical_identifier":
            out.append(node)
            return
        if node.type == "tf_call":
            skip_callee = True
            for child in node.children:
                if skip_callee and child.type == "hierarchical_identifier":
                    skip_callee = False
                    continue
                self._collect_signal_idents(child, out, skip)
            return
        if node.type == "method_call":
            body = self._child(node, "method_call_body")
            if body is not None:
                self._collect_signal_idents(body, out, skip)
            return
        for child in node.children:
            self._collect_signal_idents(child, out, skip)

    def _lvalue_roots(self, lvalue: TSNode, out: list[TSNode]) -> None:
        """Driven root-identifier nodes of an lvalue (concats recurse;
        identifiers inside selects are reads, collected separately)."""
        nested = self._children(lvalue, "net_lvalue", "variable_lvalue")
        if nested:
            for inner in nested:
                self._lvalue_roots(inner, out)
            return
        hier = self._child(lvalue, "hierarchical_identifier")
        if hier is not None:
            out.append(hier)
            return
        ident = self._child(lvalue, *_IDENTIFIER_TYPES)
        if ident is not None:
            out.append(ident)

    def _find_all(self, node: TSNode, types: frozenset[str], out: list[TSNode]) -> None:
        if node.type in types:
            out.append(node)
        for child in node.children:
            self._find_all(child, types, out)

    def _local_decl_names(self, body: TSNode) -> set[str]:
        """Names declared inside a procedural body (for-loop variables,
        automatic locals) — excluded from the unit-scoped dataflow refs."""
        decls: list[TSNode] = []
        self._find_all(
            body,
            frozenset(
                {"variable_decl_assignment", "net_decl_assignment", "for_variable_declaration"}
            ),
            decls,
        )
        names: set[str] = set()
        for decl in decls:
            for ident in self._children(decl, *_IDENTIFIER_TYPES):
                names.add(self._text(ident))
        return names

    def _statement_dataflow(self, proc: Node, body: TSNode) -> set[str]:
        """Emit DRIVES/READS refs from the assignments under *body*; returns
        the names read."""
        assignments: list[TSNode] = []
        self._find_all(body, _ASSIGNMENT_TYPES, assignments)
        drive_nodes: list[TSNode] = []
        for assignment in assignments:
            lvalue = self._child(assignment, "variable_lvalue", "net_lvalue")
            if lvalue is not None:
                self._lvalue_roots(lvalue, drive_nodes)
        drive_ids = {n.id for n in drive_nodes}
        idents: list[TSNode] = []
        # Event/delay controls are clock-domain evidence, not data reads.
        self._collect_signal_idents(body, idents, skip=frozenset({"event_control"}))
        locals_ = self._local_decl_names(body)

        emitted: set[tuple[EdgeKind, str]] = set()

        def emit(kind: EdgeKind, name: str, site: TSNode, role: str) -> None:
            if not name or name in locals_ or (kind, name) in emitted:
                return
            emitted.add((kind, name))
            self._dataflow_ref(kind, proc.id, name, site, role=role)

        for node in drive_nodes:
            name = (
                self._hier_root(node)
                if node.type == "hierarchical_identifier"
                else (self._text(node))
            )
            emit(EdgeKind.DRIVES, name, node, "lhs")
        for node in idents:
            if node.id in drive_ids:
                continue
            emit(EdgeKind.READS, self._hier_root(node), node, "rhs")
        return {name for kind, name in emitted if kind is EdgeKind.READS}

    def _on_net_declaration(self, node: TSNode) -> None:
        if not self._in_dataflow_scope():
            return
        self._count_subtree_errors(node)
        net_type_node = self._child(node, "net_type")
        type_node = self._child(node, "data_type_or_implicit")
        decl_list = self._child(node, "list_of_net_decl_assignments")
        if decl_list is None:
            return
        for decl in self._children(decl_list, "net_decl_assignment"):
            name = self._identifier(decl)
            if not name:
                continue
            self._new_node(
                NodeKind.SIGNAL,
                name,
                decl,
                net_type=self._text(net_type_node) if net_type_node is not None else None,
                type_text=self._text(type_node) if type_node is not None else None,
                is_net=True,
            )
            self._decl_init_dataflow(decl, name)

    def _on_data_declaration(self, node: TSNode) -> None:
        decl_list = self._child(node, "list_of_variable_decl_assignments")
        if decl_list is None:
            # A nested type_declaration / package import / net-type alias:
            # let the normal dispatch see it.
            self._visit_children(node)
            return
        if not self._in_dataflow_scope():
            return  # class properties and subroutine locals are not SIGNALs
        self._count_subtree_errors(node)
        type_node = self._child(node, "data_type_or_implicit")
        for decl in self._children(decl_list, "variable_decl_assignment"):
            name = self._identifier(decl)
            if not name:
                continue
            self._new_node(
                NodeKind.SIGNAL,
                name,
                decl,
                type_text=self._text(type_node) if type_node is not None else None,
                is_net=False,
            )
            self._decl_init_dataflow(decl, name)

    def _decl_init_dataflow(self, decl: TSNode, name: str) -> None:
        """``wire x = expr;`` — the LRM-equivalent continuous assign."""
        expr = self._child(decl, "expression", "constant_expression")
        if expr is None:
            return
        origin = self._origin(decl)
        proc = self._new_node(
            NodeKind.PROCESS,
            f"assign@{origin.line}",
            decl,
            style="continuous_assign",
            decl_init=True,
        )
        self._dataflow_ref(EdgeKind.DRIVES, proc.id, name, decl, role="lhs")
        idents: list[TSNode] = []
        self._collect_signal_idents(expr, idents)
        seen: set[str] = set()
        for ident in idents:
            root = self._hier_root(ident)
            if root and root not in seen:
                seen.add(root)
                self._dataflow_ref(EdgeKind.READS, proc.id, root, ident, role="rhs")

    def _on_continuous_assign(self, node: TSNode) -> None:
        if not self._in_dataflow_scope():
            return
        self._count_subtree_errors(node)
        origin = self._origin(node)
        proc = self._new_node(
            NodeKind.PROCESS, f"assign@{origin.line}", node, style="continuous_assign"
        )
        self._statement_dataflow(proc, node)

    # -- always blocks, clocks, resets (M5) -----------------------------------------

    def _event_entries(self, event_control: TSNode | None) -> list[dict[str, object]] | None:
        """Explicit sensitivity entries, or None for ``@*``/no event control."""
        if event_control is None:
            return None
        clocking = self._child(event_control, "clocking_event")
        if clocking is None:
            return None  # @* / @(*)
        entries = self._event_entries_from_clocking(clocking)
        if not entries:
            # ``@cb`` — a named event or clocking block.
            ident = self._child(clocking, "hierarchical_identifier", *_IDENTIFIER_TYPES)
            if ident is not None:
                name = (
                    self._hier_root(ident)
                    if ident.type == "hierarchical_identifier"
                    else self._text(ident)
                )
                entries.append({"edge": None, "name": name})
        return entries

    def _clock_reset_refs(
        self, src: Node, entries: list[dict[str, object]], site: TSNode
    ) -> tuple[bool, set[str]]:
        """CLOCKED_BY/RESETS refs from edge-sensitivity terms.

        Returns (edge-clocked at 1.0, names already emitted as async RESETS) —
        the former enables the sync-reset name heuristic on the block's reads,
        the latter exempts the async resets from it.
        """
        edge_terms = [e for e in entries if e["edge"]]
        if not edge_terms:
            return False, set()
        if len(edge_terms) == 1:
            term = edge_terms[0]
            self._dataflow_ref(
                EdgeKind.CLOCKED_BY,
                src.id,
                str(term["name"]),
                site,
                evidence="sensitivity",
                edge=term["edge"],
            )
            return True, set()
        resets = [t for t in edge_terms if _RESET_NAME_RE.search(str(t["name"]))]
        others = [t for t in edge_terms if t not in resets]
        reset_names = {str(t["name"]) for t in resets}
        for term in resets:
            self._dataflow_ref(
                EdgeKind.RESETS,
                src.id,
                str(term["name"]),
                site,
                evidence="sensitivity",
                edge=term["edge"],
                is_async=True,
            )
        if len(others) == 1:
            self._dataflow_ref(
                EdgeKind.CLOCKED_BY,
                src.id,
                str(others[0]["name"]),
                site,
                evidence="sensitivity",
                edge=others[0]["edge"],
            )
            return True, reset_names
        for term in others:  # 0 or 2+ clock candidates: heuristic only
            self._dataflow_ref(
                EdgeKind.CLOCKED_BY,
                src.id,
                str(term["name"]),
                site,
                confidence=CONFIDENCE_HEURISTIC,
                evidence="ambiguous_sensitivity",
                edge=term["edge"],
            )
        return bool(resets), reset_names

    def _on_always(self, node: TSNode) -> None:
        if not self._in_dataflow_scope():
            self._visit_children(node)
            return
        self._count_subtree_errors(node)
        keyword = self._child(node, "always_keyword")
        style = self._text(keyword) if keyword is not None else "always"
        event_control = self._find_first(node, "event_control", max_depth=4)
        entries = self._event_entries(event_control)
        combinational = style in ("always_comb", "always_latch") or (
            event_control is not None and entries is None
        )
        origin = self._origin(node)
        proc = self._new_node(
            NodeKind.PROCESS,
            f"always@{origin.line}",
            node,
            style=style,
            sensitivity=entries,
            combinational=combinational or None,
        )
        clocked = False
        async_resets: set[str] = set()
        if entries:
            clocked, async_resets = self._clock_reset_refs(proc, entries, event_control or node)
        reads = self._statement_dataflow(proc, node)
        for entry in entries or ():
            if not entry["edge"] and str(entry["name"]) not in reads:
                self._dataflow_ref(
                    EdgeKind.READS, proc.id, str(entry["name"]), node, role="sensitivity"
                )
        if clocked:
            # Sync-reset heuristic: a reset-named signal read by a clocked block.
            for name in sorted(reads - async_resets):
                if _RESET_NAME_RE.search(name):
                    self._dataflow_ref(
                        EdgeKind.RESETS,
                        proc.id,
                        name,
                        node,
                        confidence=CONFIDENCE_HEURISTIC,
                        evidence="name",
                        is_async=False,
                    )

    # -- verification constructs (M5) -------------------------------------------

    def _clocking_event_refs(self, src: Node, subtree: TSNode) -> set[int]:
        """CLOCKED_BY refs for every clocking event under *subtree*.

        Returns the ts-node ids of identifiers inside those events so signal
        collection can skip them (the clock is not an asserted-on signal).
        """
        events: list[TSNode] = []
        self._find_all(subtree, frozenset({"clocking_event"}), events)
        consumed: set[int] = set()
        seen: set[str] = set()
        for event in events:
            idents: list[TSNode] = []
            self._collect_signal_idents(event, idents)
            consumed.update(n.id for n in idents)
            for entry in self._event_entries_from_clocking(event):
                name = str(entry["name"])
                if entry["edge"] and name not in seen:
                    seen.add(name)
                    self._dataflow_ref(
                        EdgeKind.CLOCKED_BY,
                        src.id,
                        name,
                        event,
                        evidence="sensitivity",
                        edge=entry["edge"],
                    )
        return consumed

    def _event_entries_from_clocking(self, clocking: TSNode) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []

        def walk(ee: TSNode) -> None:
            subs = self._children(ee, "event_expression")
            if subs:
                for sub in subs:
                    walk(sub)
                return
            edge_node = self._child(ee, "edge_identifier")
            expr = self._child(ee, "expression")
            if expr is None:
                return
            idents: list[TSNode] = []
            self._collect_signal_idents(expr, idents)
            if idents:
                entries.append(
                    {
                        "edge": self._text(edge_node) if edge_node is not None else None,
                        "name": self._hier_root(idents[0]),
                    }
                )

        top = self._child(clocking, "event_expression")
        if top is not None:
            walk(top)
        return entries

    def _spec_refs(self, src: Node, subtree: TSNode, kind: EdgeKind) -> None:
        """ASSERTS_ON/COVERS refs for the signal names under *subtree*,
        with its clocking events lifted out as CLOCKED_BY."""
        consumed = self._clocking_event_refs(src, subtree)
        idents: list[TSNode] = []
        self._collect_signal_idents(subtree, idents)
        seen: set[str] = set()
        for ident in idents:
            if ident.id in consumed:
                continue
            name = self._hier_root(ident)
            if name and name not in seen:
                seen.add(name)
                self._dataflow_ref(kind, src.id, name, ident)

    def _on_property_decl(self, node: TSNode) -> None:
        name = self._identifier(node)
        if not name:
            return
        self._count_subtree_errors(node)
        kind = NodeKind.PROPERTY if node.type == "property_declaration" else NodeKind.SEQUENCE
        decl = self._new_node(kind, name, node)
        self._spec_refs(decl, node, EdgeKind.ASSERTS_ON)

    def _on_assertion(self, node: TSNode) -> None:
        if not self._in_dataflow_scope():
            self._visit_children(node)
            return
        self._count_subtree_errors(node)
        statement = next((c for c in node.children if c.type in _ASSERT_STATEMENT_TYPES), None)
        label = self._identifier(node)
        origin = self._origin(node)
        assertion = self._new_node(
            NodeKind.ASSERTION,
            label or f"assert@{origin.line}",
            node,
            statement=_ASSERT_STATEMENT_TYPES[statement.type] if statement is not None else None,
        )
        self._spec_refs(
            assertion, statement if statement is not None else node, EdgeKind.ASSERTS_ON
        )

    def _on_covergroup(self, node: TSNode) -> None:
        origin = self._origin(node)
        self._count_subtree_errors(node)
        name = self._identifier(node) or f"covergroup@{origin.line}"
        group = self._new_node(NodeKind.COVERGROUP, name, node)
        event = self._child(node, "coverage_event")
        if event is not None:
            self._clocking_event_refs(group, event)
        self.scopes.append(_Scope(node_id=group.id, path=group.qualified_name, kind=group.kind))
        try:
            for spec in self._children(node, "coverage_spec_or_option"):
                point = self._child(spec, "cover_point")
                if point is None:
                    continue
                point_origin = self._origin(point)
                point_name = self._identifier(point) or f"cp@{point_origin.line}"
                cp = self._new_node(NodeKind.COVERPOINT, point_name, point)
                expr = self._child(point, "expression")
                if expr is not None:
                    self._spec_refs(cp, expr, EdgeKind.COVERS)
        finally:
            self.scopes.pop()

    def _on_constraint(self, node: TSNode) -> None:
        name = self._identifier(node)
        if name:
            self._new_node(NodeKind.CONSTRAINT, name, node)

    def _on_clocking(self, node: TSNode) -> None:
        origin = self._origin(node)
        self._count_subtree_errors(node)
        name = self._identifier(node) or f"clocking@{origin.line}"
        block = self._new_node(
            NodeKind.CLOCKING_BLOCK,
            name,
            node,
            is_default=(self._child(node, "default") is not None) or None,
        )
        event = self._child(node, "clocking_event")
        if event is not None:
            self._clocking_event_refs(block, event)

    _DISPATCH = {
        "module_declaration": _on_design_unit,
        "interface_declaration": _on_design_unit,
        "program_declaration": _on_design_unit,
        "package_declaration": _on_package,
        "class_declaration": _on_class,
        "ansi_port_declaration": _on_ansi_port,
        "port": _on_nonansi_port,
        "port_declaration": _on_port_declaration,
        "parameter_declaration": _on_parameter_declaration,
        "local_parameter_declaration": _on_parameter_declaration,
        "type_declaration": _on_type_declaration,
        "function_declaration": _on_function,
        "task_declaration": _on_task,
        "class_constructor_declaration": _on_constructor,
        "dpi_import_export": _on_dpi,
        "module_instantiation": _on_instantiation,
        "package_import_declaration": _on_import,
        # M5: dataflow + clocks
        "net_declaration": _on_net_declaration,
        "data_declaration": _on_data_declaration,
        "continuous_assign": _on_continuous_assign,
        "always_construct": _on_always,
        # M5: verification constructs
        "property_declaration": _on_property_decl,
        "sequence_declaration": _on_property_decl,
        "concurrent_assertion_item": _on_assertion,
        "covergroup_declaration": _on_covergroup,
        "constraint_declaration": _on_constraint,
        "clocking_declaration": _on_clocking,
    }


#: Validate the grammar's node-type surface once (the names the walker
#: dispatches on); a rename upstream would otherwise silently under-extract.
_grammar_validated = False


def _validate_sv_grammar() -> None:
    global _grammar_validated
    if _grammar_validated:
        return
    validate_grammar(
        SV_LANGUAGE,
        set(_Walker._DISPATCH) | set(_HEADER_TYPES) | set(_IDENTIFIER_TYPES),
        grammar="tree-sitter-systemverilog grammar",
    )
    _grammar_validated = True


class SystemVerilogParser:
    """Tree-sitter based SystemVerilog/Verilog pass-1 parser."""

    suffixes = SUFFIXES

    def __init__(self) -> None:
        _validate_sv_grammar()
        self._parser = TSParser(SV_LANGUAGE)

    def parse(self, path: Path, text: str, line_map: Sequence[LineOrigin] | None = None) -> FileIR:
        """Parse one file into its per-file IR.

        *path* should be relative to the build root; it becomes the node-id
        prefix and ``Node.file`` for everything in the file. When *text* is
        preprocessor output, pass its *line_map* so spans and file
        attribution track the original sources (including spliced headers).
        """
        relpath = path.as_posix()
        language = (
            Language.SYSTEMVERILOG if path.suffix in SYSTEMVERILOG_SUFFIXES else Language.VERILOG
        )
        ir = FileIR(path=relpath)
        file_node = Node(
            id=file_node_id(relpath),
            kind=NodeKind.FILE,
            name=path.name,
            qualified_name=relpath,
            file=relpath,
            language=language,
        )
        ir.nodes.append(file_node)
        # Work around tree-sitter grammar gaps (e.g. function-call size casts)
        # before parsing; line-count-preserving, so *line_map* stays valid.
        source = normalize_sv_source(text).encode()
        try:
            tree = self._parser.parse(source)
            walker = _Walker(ir, relpath, language, source, line_map)
            walker.scopes.append(_Scope(node_id=file_node.id, path=""))
            walker.visit(tree.root_node)
        except Exception as exc:  # defensive: a walker bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
        return ir
