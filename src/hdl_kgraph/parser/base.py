"""Parser backend interface.

Design (see ROADMAP.md, "Two-pass build architecture"):

* **Pass 1 (parse):** a :class:`ParserBackend` parses one file independently
  into a per-file IR — declared nodes plus unresolved references (instance
  targets, package imports, include paths). Pass 1 is parallelizable (the
  pipeline currently runs it serially; see issue #26) and is the only stage
  re-run for changed files during incremental updates.
* **Pass 2 (link):** the graph builder resolves references across files with
  confidence scoring (see :mod:`hdl_kgraph.schema`).

Backends are intentionally swappable: the tree-sitter grammar choice (the #1
project risk) is isolated here, and M7 adds elaboration-accurate backends
(pyslang, GHDL/pyVHDLModel) behind the same interface with capability flags.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from tree_sitter import Node as TSNode

from hdl_kgraph.schema import CONFIDENCE_RESOLVED, Edge, EdgeKind, Node

#: A walker's per-language declaration-scope type (see :class:`_WalkerBase`).
ScopeT = TypeVar("ScopeT")

#: Per-file cap on recorded parse-error *details*; ``parse_error_count``
#: stays exact beyond it (a garbage/minified file must not bloat the store).
MAX_PARSE_ERRORS = 20

#: Whether a net name looks like a reset. Shared by the SystemVerilog and VHDL
#: backends so the two cannot drift (they held identical private copies).
#:
#: The reset token may be followed by polarity and qualifier segments —
#: `rst_n_i`, `rst_ni`, `wr_rst_n_i`, `m_rst_sync_n` are all resets. An earlier
#: pattern anchored `$` immediately after the optional polarity, so any further
#: suffix defeated it; since `_i`/`_o` port suffixes are a common convention,
#: that silently misclassified a design's resets as *clocks*. With no term in
#: `@(posedge clk_i or negedge rst_n_i)` recognised as a reset, both terms fell
#: through to the ambiguous-sensitivity branch and were emitted as
#: low-confidence CLOCKED_BY; the process then had two clock domains,
#: :mod:`hdl_kgraph.graph.clocks` skipped it as ambiguous, and CDC detection
#: went blind on exactly the modules where clock crossings live.
#:
#: The trailing segments are a **whitelist**, not `.*`, which is what keeps
#: `rst_count`, `reset_value`, and `clear_count` out: those are data signals
#: named after a reset, not resets. `restart`, `wrist_data`, `crystal_en`, and
#: `color_reg` are excluded by the leading `(?:^|_)` token boundary.
RESET_NAME_RE = re.compile(
    r"(?:^|_)(?:a?rst|a?reset|clr|clear)(?:_?[nb]i?)?(?:_(?:sync|async|n|b|i|o|in|out))*$",
    re.IGNORECASE,
)


class GrammarMismatchError(RuntimeError):
    """A loaded tree-sitter grammar is missing node types/fields the parser
    dispatches on.

    The parsers walk the tree with hardcoded ``node.type``/field-name string
    literals (the Query API churns across releases — see ROADMAP Risk #5). A
    syntax error is loud (it shows up in ``parse_error_count``), but an upstream
    *rename* of a node type that still parses cleanly would silently turn a
    handler into a no-op — missing nodes, no error. :func:`validate_grammar`
    checks the expected names at parser construction and raises this so the
    drift is a loud, actionable failure instead. See issue #71.
    """


def validate_grammar(
    language: Any,
    node_types: Iterable[str],
    *,
    field_names: Iterable[str] = (),
    grammar: str = "tree-sitter grammar",
) -> None:
    """Raise :class:`GrammarMismatchError` if the loaded *language* is missing any
    of the named node types or fields the parser dispatches on.

    Uses the tree-sitter ``Language`` introspection API
    (``id_for_node_kind``/``field_id_for_name``), which returns ``None`` for an
    unknown name — so a grammar that renamed a construct fails loudly here rather
    than under-extracting in silence.
    """
    missing_types = sorted(
        t for t in dict.fromkeys(node_types) if language.id_for_node_kind(t, True) is None
    )
    missing_fields = sorted(
        f for f in dict.fromkeys(field_names) if language.field_id_for_name(f) is None
    )
    if not missing_types and not missing_fields:
        return
    parts = []
    if missing_types:
        parts.append(f"node types {missing_types}")
    if missing_fields:
        parts.append(f"fields {missing_fields}")
    raise GrammarMismatchError(
        f"the loaded {grammar} is missing " + " and ".join(parts) + ". "
        "hdl-kgraph dispatches on these names, so a grammar rename would silently "
        "under-extract; pin a compatible grammar version or update the parser."
    )


class UnsupportedBackendError(NotImplementedError):
    """Raised by a registered-but-not-yet-implemented parser backend.

    Subclasses :class:`NotImplementedError` (so existing call sites keep
    working) but is a distinct, greppable type a future suffix router can
    catch to skip-with-warning instead of aborting the build. The stub
    backends (SDC/UPF/Tcl/Perl/SLN) are intentionally kept out of the
    discovery/pipeline routing path until implemented; this is the clean
    error they raise if one is ever dispatched to directly. See issue #77.
    """


def within_root(path: Path, root: Path) -> bool:
    """True if *path* resolves inside *root* (or equals it).

    Used to confine filelist source/``-y``/``-v`` tokens, ``+incdir+`` dirs, and
    ``\\`include`` resolution to the build root, so a crafted ``.f`` token —
    ``..`` segments or a ``$VAR`` that expands to an absolute/out-of-tree path —
    cannot pull in (and disclose the HDL structure of) files outside the tree
    being analyzed. See issue #68.
    """
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def error_snippet(text: str, limit: int = 50) -> str:
    """First line of *text*, trimmed to *limit* chars, for error messages."""
    first, _, rest = text.strip().partition("\n")
    first = first.strip()
    if len(first) > limit or rest:
        return first[:limit].rstrip() + "..."
    return first


@dataclass
class UnresolvedRef:
    """A cross-file reference recorded in pass 1 and resolved in pass 2.

    ``attrs`` carries the per-kind payload the linker needs:

    ================= ============================== ====================================
    ``edge_kind``     ``src_id`` / ``target_name``   ``attrs``
    ================= ============================== ====================================
    ``INSTANTIATES``  INSTANCE node / target module  --
    ``CONNECTS``      INSTANCE node / target module  ``port_name: str | None``,
      (per binding)                                  ``position: int | None``,
                                                     ``wildcard: bool``, ``expr_text: str``
    ``PARAMETERIZES`` INSTANCE node / target module  ``param_name: str | None``,
      (per override)                                 ``position: int | None``,
                                                     ``value_text: str``
    ``IMPORTS``       importing scope / package      ``symbol``: ``"*"`` or explicit name
    ``EXTENDS``       CLASS node / base class        ``package: str | None``,
                                                     ``param_args_text: str | None``
    ``DRIVES``/       PROCESS (or other site) /      ``role``: ``lhs``/``rhs``/
    ``READS``         signal name                    ``sensitivity``
    ``CLOCKED_BY``/   PROCESS, ASSERTION, COVER-     ``evidence``, ``edge``,
    ``RESETS``        GROUP, ... / clock or reset    ``is_async`` (RESETS)
    ``ASSERTS_ON``/   ASSERTION/PROPERTY/SEQUENCE/   --
    ``COVERS``        COVERPOINT / name in scope
    ================= ============================== ====================================

    Positional ``CONNECTS``/``PARAMETERIZES`` resolve against the target's
    PORT/PARAMETER children via their declaration-order ``attrs["index"]``.
    The M5 dataflow kinds (the last three rows) are **scoped**: ``target_name``
    resolves against the referring node's enclosing design unit's children,
    never globally — see :mod:`hdl_kgraph.graph.builder`. ``confidence`` on
    these carries the *evidence* score (1.0 sensitivity proof, 0.4 name
    heuristic), which the linker min-combines with resolution confidence.
    """

    edge_kind: EdgeKind
    src_id: str  # id of the referring node (present in FileIR.nodes)
    target_name: str  # bare name to resolve (module/package/class name)
    line_span: tuple[int, int] = (0, 0)
    attrs: dict[str, Any] = field(default_factory=dict)
    # Confidence of the reference *site* itself; below 1.0 for references in
    # non-selected both-branches preprocessor regions. The linker emits
    # min(resolution confidence, this).
    confidence: float = CONFIDENCE_RESOLVED


@dataclass
class FileIR:
    """Pass-1 result for a single source file."""

    path: str
    nodes: list[Node] = field(default_factory=list)
    # Edges whose endpoints are both local to this file (e.g. DECLARES).
    local_edges: list[Edge] = field(default_factory=list)
    # References to be resolved in pass 2 (instance targets, imports, extends).
    unresolved_refs: list[UnresolvedRef] = field(default_factory=list)
    parse_error_count: int = 0
    # Human-readable ``file:line: message`` details for the first
    # MAX_PARSE_ERRORS errors (so `build -v`/`status --errors` can point at
    # the offending source, not just count it).
    parse_errors: list[str] = field(default_factory=list)

    def record_parse_error(self, message: str) -> None:
        """Count one parse error, keeping the first MAX_PARSE_ERRORS details."""
        self.parse_error_count += 1
        if len(self.parse_errors) < MAX_PARSE_ERRORS:
            self.parse_errors.append(message)


class ParserBackend(Protocol):
    """A pass-1 parser for one or more languages."""

    #: File suffixes this backend handles (e.g. ``{".v", ".sv", ".svh"}``).
    suffixes: frozenset[str]

    def parse(self, path: Path, text: str) -> FileIR:
        """Parse one file into its per-file IR. Must tolerate syntax errors."""
        ...


class _WalkerBase(Generic[ScopeT]):
    """Shared tree-walking machinery for the tree-sitter parsers (issue #72).

    Holds the language-agnostic traversal that the SystemVerilog and VHDL
    walkers used to re-implement independently (and drift apart on): node-text
    extraction, typed child lookup, the dispatch-driven :meth:`visit`, and
    parse-error counting. A subclass provides ``source`` and ``scopes``, its
    ``_DISPATCH`` table (``node.type`` → handler), language-specific node
    handlers and ``_new_node``, and :meth:`_record_parse_error` (file/line
    attribution differs — VHDL uses the raw row, SV maps through the
    preprocessor line map). Parameterized by the subclass's scope type so
    ``self.scope`` stays precisely typed.

    The ERROR-node policy is an **explicit, documented** decision point rather
    than accidental drift: SystemVerilog *skips* an ERROR subtree (keep partial
    results from the siblings), while VHDL *descends* into it because its grammar
    wraps whole regions — often the entire ``design_file`` — in a single ERROR
    node with intact design units inside, so skipping would discard salvageable
    declarations. Subclasses set :attr:`ERROR_POLICY` accordingly.
    """

    #: ``"skip"`` the ERROR subtree (return) or ``"descend"`` into it.
    ERROR_POLICY: str = "skip"
    #: ``node.type`` → ``handler(self, node)``. Subclasses define this.
    _DISPATCH: dict[str, Any] = {}

    source: bytes
    scopes: list[ScopeT]

    @property
    def scope(self) -> ScopeT:
        return self.scopes[-1]

    def _text(self, node: TSNode) -> str:
        return self.source[node.start_byte : node.end_byte].decode(errors="replace")

    @staticmethod
    def _child(node: TSNode, *types: str) -> TSNode | None:
        for child in node.children:
            if child.type in types:
                return child
        return None

    @staticmethod
    def _children(node: TSNode, *types: str) -> list[TSNode]:
        return [c for c in node.children if c.type in types]

    def visit(self, node: TSNode) -> None:
        if node.is_missing:
            self._record_parse_error(node)
        if node.type == "ERROR":
            self._record_parse_error(node)
            if self.ERROR_POLICY == "skip":
                return  # partial results: keep going with siblings
        handler = self._DISPATCH.get(node.type)
        if handler is not None:
            handler(self, node)
        else:
            for child in node.children:
                self.visit(child)

    def _visit_children(self, node: TSNode) -> None:
        for child in node.children:
            self.visit(child)

    def _count_subtree_errors(self, node: TSNode) -> None:
        """Keep ``parse_error_count`` honest for subtrees a handler consumes
        without re-dispatching (mirrors :meth:`visit`'s counting)."""
        if node.is_missing:
            self._record_parse_error(node)
        if node.type == "ERROR":
            self._record_parse_error(node)
            return
        for child in node.children:
            self._count_subtree_errors(child)

    def _record_parse_error(self, node: TSNode) -> None:
        # Subclass responsibility: the file/line attribution is language-specific.
        raise NotImplementedError
