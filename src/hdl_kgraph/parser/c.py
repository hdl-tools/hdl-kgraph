"""C/C++ parser backend (M8 — DPI-C boundary).

The DPI-C wedge of milestone M8 links a SystemVerilog ``import "DPI-C"``
prototype to the C/C++ function that implements it. This backend supplies the
C-side half: a thin pass-1 scan that emits a ``FUNCTION`` node for every
top-level function *definition* (and prototype *declaration*, so a header-only
target still resolves), which the pass-2 linker then matches to the SV import
by name via a ``FOREIGN_BINDS`` edge.

Scope notes (the honest contract — DPI uses C linkage, so a bare-name match is
the right tier):

* Only function definitions/prototypes are extracted; full C type/width
  modeling and the C preprocessor (``#include``/``#define``) are out of scope.
* C++ name mangling is **not** modeled: a DPI export has C linkage
  (``extern "C"``), so the unmangled identifier is what matters. Functions
  inside ``extern "C"`` blocks, ``namespace`` blocks, and at file scope are all
  recorded as flat, file-scoped ``FUNCTION`` nodes; a ``ns::name`` definition is
  recorded under its bare ``name``.
* The grammar is ``tree-sitter-c`` for ``.c``/``.h`` and ``tree-sitter-cpp``
  for ``.cpp``/``.cc``/``.cxx``/``.hpp``/``.hh``/``.hxx``. ``.h`` is treated as
  C — DPI headers are C — which the C grammar parses fine.
* Files with tree-sitter ERROR nodes still yield partial results; the error
  count is reported in ``FileIR.parse_error_count`` (ERROR subtrees are skipped,
  as in the SystemVerilog walker).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import tree_sitter_c
import tree_sitter_cpp
from tree_sitter import Language as TSLanguage
from tree_sitter import Node as TSNode
from tree_sitter import Parser as TSParser

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import (
    FileIR,
    _WalkerBase,
    error_snippet,
    validate_grammar,
)
from hdl_kgraph.schema import (
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

C_LANGUAGE = TSLanguage(tree_sitter_c.language())
CPP_LANGUAGE = TSLanguage(tree_sitter_cpp.language())

C_SUFFIXES = frozenset({".c", ".h"})
CPP_SUFFIXES = frozenset({".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"})
#: Every suffix this module's backends handle (consumed by ``discovery``).
SUFFIXES = C_SUFFIXES | CPP_SUFFIXES

#: Declarator wrappers between a definition/declaration and its function
#: declarator (``int *foo()`` nests the name under a ``pointer_declarator``).
_DECLARATOR_WRAPPERS = frozenset(
    {"pointer_declarator", "reference_declarator", "parenthesized_declarator"}
)


def _line_span(node: TSNode) -> tuple[int, int]:
    return (node.start_point[0] + 1, node.end_point[0] + 1)


@dataclass
class _Scope:
    """One declaration-scope level (functions are recorded flat at file scope)."""

    node_id: str
    path: str  # dotted prefix; "" at file scope


class _CWalker(_WalkerBase[_Scope]):
    #: Skip an ERROR subtree and keep partial results from its siblings, as the
    #: SystemVerilog walker does (the C grammars do not wrap whole regions).
    ERROR_POLICY = "skip"

    def __init__(self, ir: FileIR, relpath: str, source: bytes, language: Language) -> None:
        self.ir = ir
        self.relpath = relpath
        self.source = source
        self.language = language
        self.scopes: list[_Scope] = []
        self._used_ids: set[str] = set()

    # -- helpers -------------------------------------------------------------

    def _new_node(self, kind: NodeKind, name: str, ts_node: TSNode, **attrs: object) -> Node:
        """Create a file-scoped node and emit its DECLARES edge."""
        node_id = decl_node_id(self.relpath, kind, name)
        if node_id in self._used_ids:
            node_id = f"{node_id}@{ts_node.start_point[0] + 1}"
        self._used_ids.add(node_id)
        node = Node(
            id=node_id,
            kind=kind,
            name=name,
            qualified_name=name,
            file=self.relpath,
            line_span=_line_span(ts_node),
            language=self.language,
            attrs={k: v for k, v in attrs.items() if v is not None},
        )
        self.ir.nodes.append(node)
        self.ir.local_edges.append(
            Edge(src=self.scope.node_id, dst=node.id, kind=EdgeKind.DECLARES)
        )
        return node

    def _function_declarator(self, node: TSNode) -> TSNode | None:
        """The ``function_declarator`` under a definition/declaration, unwrapping
        the pointer/reference/parenthesized declarators that may nest it."""
        decl = node.child_by_field_name("declarator")
        while decl is not None and decl.type in _DECLARATOR_WRAPPERS:
            decl = decl.child_by_field_name("declarator")
        if decl is not None and decl.type == "function_declarator":
            return decl
        return None

    def _function_name(self, func_decl: TSNode) -> str:
        """The bare function name from a ``function_declarator`` (``ns::f`` → ``f``)."""
        name_node = func_decl.child_by_field_name("declarator")
        if name_node is None:
            return ""
        if name_node.type not in ("identifier", "field_identifier", "qualified_identifier"):
            return ""  # operator/destructor/function-pointer names: not DPI targets
        return self._text(name_node).rsplit("::", 1)[-1].strip()

    # -- handlers ------------------------------------------------------------

    def _on_function_definition(self, node: TSNode) -> None:
        func_decl = self._function_declarator(node)
        if func_decl is None:
            return
        name = self._function_name(func_decl)
        if name:
            self._new_node(NodeKind.FUNCTION, name, node, is_definition=True)

    def _on_declaration(self, node: TSNode) -> None:
        # A `declaration` is a function *prototype* only when it carries a
        # function_declarator; otherwise it is a variable/typedef (not a target).
        func_decl = self._function_declarator(node)
        if func_decl is None:
            return
        name = self._function_name(func_decl)
        if name:
            self._new_node(NodeKind.FUNCTION, name, node, is_prototype=True)

    def _record_parse_error(self, node: TSNode) -> None:
        line = node.start_point[0] + 1
        if node.is_missing:
            message = f"missing `{node.type}`"
        else:
            message = f"syntax error near `{error_snippet(self._text(node))}`"
        self.ir.record_parse_error(f"{self.relpath}:{line}: {message}")

    _DISPATCH = {
        "function_definition": _on_function_definition,
        "declaration": _on_declaration,
    }


#: The node types the walker dispatches on (plus the name leaves it reads), used
#: to guard against an upstream grammar rename silently under-extracting.
_VALIDATED_TYPES = set(_CWalker._DISPATCH) | {"function_declarator", "identifier"}

_c_grammar_validated = False
_cpp_grammar_validated = False


def _validate_c_grammar() -> None:
    global _c_grammar_validated
    if _c_grammar_validated:
        return
    validate_grammar(
        C_LANGUAGE, _VALIDATED_TYPES, field_names={"declarator"}, grammar="tree-sitter-c grammar"
    )
    _c_grammar_validated = True


def _validate_cpp_grammar() -> None:
    global _cpp_grammar_validated
    if _cpp_grammar_validated:
        return
    validate_grammar(
        CPP_LANGUAGE,
        _VALIDATED_TYPES | {"field_identifier"},
        field_names={"declarator"},
        grammar="tree-sitter-cpp grammar",
    )
    _cpp_grammar_validated = True


class _CFamilyParser:
    """Shared parse driver for the C and C++ backends."""

    suffixes: frozenset[str]
    _language: Language
    _ts_language: TSLanguage
    _parser: TSParser

    def parse(self, path: Path, text: str) -> FileIR:
        """Parse one C/C++ file into its per-file IR. Tolerates syntax errors."""
        relpath = path.as_posix()
        ir = FileIR(path=relpath)
        ir.nodes.append(
            Node(
                id=file_node_id(relpath),
                kind=NodeKind.FILE,
                name=path.name,
                qualified_name=relpath,
                file=relpath,
                language=self._language,
            )
        )
        source = text.encode()
        try:
            tree = self._parser.parse(source)
            walker = _CWalker(ir, relpath, source, self._language)
            walker.scopes.append(_Scope(node_id=file_node_id(relpath), path=""))
            walker.visit(tree.root_node)
        except Exception as exc:  # defensive: a walker bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
        return ir


class CParser(_CFamilyParser):
    """tree-sitter-c pass-1 parser for ``.c``/``.h`` (M8 DPI-C)."""

    suffixes = C_SUFFIXES
    _language = Language.C
    _ts_language = C_LANGUAGE

    def __init__(self) -> None:
        _validate_c_grammar()
        self._parser = TSParser(C_LANGUAGE)


class CppParser(_CFamilyParser):
    """tree-sitter-cpp pass-1 parser for ``.cpp``/``.cc``/``.cxx``/``.hpp``/… (M8 DPI-C)."""

    suffixes = CPP_SUFFIXES
    _language = Language.CPP
    _ts_language = CPP_LANGUAGE

    def __init__(self) -> None:
        _validate_cpp_grammar()
        self._parser = TSParser(CPP_LANGUAGE)
