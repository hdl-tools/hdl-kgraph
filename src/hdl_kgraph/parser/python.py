"""Python / cocotb testbench parser backend (M8 — cocotb boundary).

The cocotb wedge of milestone M8 links a Python cocotb testbench to the HDL
design it exercises. cocotb tests drive and sample the DUT through attribute
access on a ``dut`` handle (``dut.sig.value = 1``, ``x = dut.sig.value``), and
the toplevel module is chosen by the *runner* (Makefile ``TOPLEVEL`` /
``runner.test(hdl_toplevel=…)``), **not** named in the Python file. So the DUT
is resolved heuristically: the configured top module(s) (``[build].top`` in
``hdl-kgraph.toml``) when available, else a filename heuristic
(``test_fifo.py`` → ``fifo``).

This backend emits, per ``@cocotb.test``-decorated function:

* a ``FUNCTION`` node (``language=python``, ``attrs["is_cocotb_test"]``);
* a ``TEST_COVERS`` ref to each DUT module (confidence 0.4 — name heuristic);
* ``READS``/``DRIVES`` refs for each ``dut.<signal>`` access (confidence 0.6),
  resolved in pass 2 against the DUT module's ports/signals.

Scope (the honest contract): ``dut.<signal>`` is resolved one level deep —
hierarchical access (``dut.sub.sig``) resolves only ``sub`` and is best-effort;
the DUT is a name heuristic, never elaboration. A `.py` file only becomes a
source when it mentions ``cocotb`` (discovery content-sniffs for it), so the
parser never turns ordinary Python scripts into graph nodes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language as TSLanguage
from tree_sitter import Node as TSNode
from tree_sitter import Parser as TSParser

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_HEURISTIC,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

PYTHON_LANGUAGE = TSLanguage(tree_sitter_python.language())

SUFFIXES = frozenset({".py"})

#: A `.py` is only treated as a cocotb source if it mentions this token; keeps
#: ordinary Python (incl. hdl-kgraph's own sources) out of the graph.
COCOTB_MARKER = "cocotb"

#: cocotb write API: ``dut.sig.value = x`` (assignment) and
#: ``dut.sig.setimmediatevalue(x)`` (method). Everything else is a read.
_WRITE_METHODS = frozenset({"setimmediatevalue"})
#: Decorator names that mark a cocotb test (``@cocotb.test`` / ``from cocotb import test``).
_COCOTB_TEST_DECORATORS = frozenset({"cocotb.test", "test"})

_TEST_PREFIX_RE = re.compile(r"^(?:test|tb)[_-]", re.IGNORECASE)
_TEST_SUFFIX_RE = re.compile(r"[_-](?:test|tb)$", re.IGNORECASE)


def _dut_from_filename(stem: str) -> str:
    """Filename heuristic for the DUT module: ``test_fifo`` / ``fifo_tb`` → ``fifo``."""
    name = _TEST_PREFIX_RE.sub("", stem)
    name = _TEST_SUFFIX_RE.sub("", name)
    return name or stem


class _Walker:
    """Targeted cocotb extraction over a tree-sitter-python tree."""

    def __init__(self, ir: FileIR, relpath: str, source: bytes, dut_modules: list[str]) -> None:
        self.ir = ir
        self.relpath = relpath
        self.source = source
        self.dut_modules = dut_modules
        self.file_id = file_node_id(relpath)
        self._used_ids: set[str] = set()

    def _text(self, node: TSNode) -> str:
        return self.source[node.start_byte : node.end_byte].decode(errors="replace")

    def _span(self, node: TSNode) -> tuple[int, int]:
        return (node.start_point[0] + 1, node.end_point[0] + 1)

    # -- traversal -----------------------------------------------------------

    def walk(self, root: TSNode) -> None:
        for node in self._descendants(root):
            if node.type == "ERROR" or node.is_missing:
                self._record_error(node)
            elif node.type == "decorated_definition":
                self._on_decorated(node)

    @staticmethod
    def _descendants(node: TSNode) -> Iterator[TSNode]:
        stack = [node]
        while stack:
            cur = stack.pop()
            yield cur
            stack.extend(reversed(cur.children))

    def _record_error(self, node: TSNode) -> None:
        self.ir.record_parse_error(f"{self.relpath}:{node.start_point[0] + 1}: syntax error")

    # -- cocotb tests --------------------------------------------------------

    def _on_decorated(self, node: TSNode) -> None:
        func = next((c for c in node.children if c.type == "function_definition"), None)
        if func is None or not self._has_cocotb_decorator(node):
            return
        name_node = func.child_by_field_name("name")
        name = self._text(name_node) if name_node is not None else ""
        if not name:
            return
        test = self._new_function(name, func)
        for dut in self.dut_modules:
            self.ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.TEST_COVERS,
                    src_id=test.id,
                    target_name=dut,
                    line_span=self._span(func),
                    attrs={"cocotb": True, "evidence": "cocotb_test"},
                    confidence=CONFIDENCE_HEURISTIC,
                )
            )
        dut_param = self._dut_param(func)
        body = func.child_by_field_name("body")
        if dut_param and body is not None:
            self._emit_signal_refs(test.id, body, dut_param)

    def _has_cocotb_decorator(self, decorated: TSNode) -> bool:
        for dec in decorated.children:
            if dec.type != "decorator":
                continue
            expr = next((c for c in dec.children if c.is_named), None)
            if expr is None:
                continue
            if expr.type == "call":
                expr = expr.child_by_field_name("function") or expr
            if self._text(expr) in _COCOTB_TEST_DECORATORS:
                return True
        return False

    def _dut_param(self, func: TSNode) -> str:
        params = func.child_by_field_name("parameters")
        if params is None:
            return ""
        for child in params.named_children:
            if child.type == "identifier":
                return self._text(child)
            if child.type in ("typed_parameter", "default_parameter", "typed_default_parameter"):
                ident = next((c for c in child.children if c.type == "identifier"), None)
                if ident is not None:
                    return self._text(ident)
        return ""

    def _new_function(self, name: str, func: TSNode) -> Node:
        node_id = decl_node_id(self.relpath, NodeKind.FUNCTION, name)
        if node_id in self._used_ids:
            node_id = f"{node_id}@{func.start_point[0] + 1}"
        self._used_ids.add(node_id)
        node = Node(
            id=node_id,
            kind=NodeKind.FUNCTION,
            name=name,
            qualified_name=name,
            file=self.relpath,
            line_span=self._span(func),
            language=Language.PYTHON,
            attrs={"is_cocotb_test": True},
        )
        self.ir.nodes.append(node)
        self.ir.local_edges.append(Edge(src=self.file_id, dst=node.id, kind=EdgeKind.DECLARES))
        return node

    # -- dut.<signal> dataflow ----------------------------------------------

    def _emit_signal_refs(self, test_id: str, body: TSNode, dut_param: str) -> None:
        # Dedupe by (signal, kind): a signal may be both read and driven, but
        # one edge per direction per test is enough.
        seen: set[tuple[str, EdgeKind]] = set()
        for node in self._descendants(body):
            if node.type != "attribute":
                continue
            obj = node.child_by_field_name("object")
            attr = node.child_by_field_name("attribute")
            if obj is None or attr is None:
                continue
            if obj.type != "identifier" or self._text(obj) != dut_param:
                continue
            signal = self._text(attr)
            kind = self._classify(node)
            if (signal, kind) in seen:
                continue
            seen.add((signal, kind))
            for dut in self.dut_modules:
                self.ir.unresolved_refs.append(
                    UnresolvedRef(
                        edge_kind=kind,
                        src_id=test_id,
                        target_name=signal,
                        line_span=self._span(node),
                        attrs={"cocotb": True, "dut_module": dut, "evidence": "dut_access"},
                        confidence=CONFIDENCE_AMBIGUOUS,
                    )
                )

    def _classify(self, base_attr: TSNode) -> EdgeKind:
        """READS unless the ``dut.sig`` access is an assignment target or a
        cocotb write-method call (``.value = x`` / ``.setimmediatevalue(x)``)."""
        node = base_attr
        while (
            node.parent is not None
            and node.parent.type == "attribute"
            and node.parent.child_by_field_name("object") == node
        ):
            node = node.parent
        parent = node.parent
        if parent is None:
            return EdgeKind.READS
        if parent.type in ("assignment", "augmented_assignment"):
            if parent.child_by_field_name("left") == node:
                return EdgeKind.DRIVES
        elif parent.type == "call" and parent.child_by_field_name("function") == node:
            method = node.child_by_field_name("attribute")
            if method is not None and self._text(method) in _WRITE_METHODS:
                return EdgeKind.DRIVES
        return EdgeKind.READS


class PythonParser:
    """tree-sitter-python pass-1 parser for cocotb testbenches (M8)."""

    suffixes = SUFFIXES

    def __init__(self) -> None:
        self._parser = TSParser(PYTHON_LANGUAGE)

    def parse(self, path: Path, text: str, tops: Sequence[str] = ()) -> FileIR:
        """Parse one cocotb testbench into its per-file IR.

        *tops* are the configured top module names; when empty the DUT module is
        guessed from the filename. Tolerates syntax errors.
        """
        relpath = path.as_posix()
        ir = FileIR(path=relpath)
        ir.nodes.append(
            Node(
                id=file_node_id(relpath),
                kind=NodeKind.FILE,
                name=path.name,
                qualified_name=relpath,
                file=relpath,
                language=Language.PYTHON,
            )
        )
        dut_modules = list(dict.fromkeys(tops)) if tops else [_dut_from_filename(path.stem)]
        source = text.encode()
        try:
            tree = self._parser.parse(source)
            _Walker(ir, relpath, source, dut_modules).walk(tree.root_node)
        except Exception as exc:  # defensive: a walker bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
        return ir
