"""SLN (Cadence Perspec System Level Notation) parser backend (M10).

Implementation notes:

* SLN is Cadence's portable-stimulus language for Perspec System Verifier,
  written in the ``e``/Specman dialect (``<' ... '>`` wrappers, ``extend
  <unit>``, ``action <name>`` declarations, ``>sub_action`` "do" invocations,
  ``in sequence``/``in schedule`` scheduling, ``.path.field == value``
  constraints). It is proprietary with no public grammar, so this is a
  best-effort line/regex scan, not an ``e`` parser (see ROADMAP.md "Risks").
* ``action <name>`` -> ACTION node. Each ``>invoked`` is recorded on the
  enclosing action's ``attrs["invokes"]`` and emits two best-effort pass-2
  refs: INVOKES (to a same-file action) and TEST_COVERS (to a design module/
  instance the name matches) — both skip when unmatched, never stub. The
  ``extend`` unit(s) and ``.field == value`` constraints are kept in attrs.
  (Real SLN's root action *is* the scenario, so SCENARIO nodes are unused here.)
* ``.sln`` collides with Visual Studio solution files: discovery content-sniffs
  for the ``Microsoft Visual Studio Solution File`` header and skips those, so
  this parser only ever sees Perspec SLN.
"""

from __future__ import annotations

import re
from pathlib import Path

from hdl_kgraph.ids import decl_node_id, file_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.schema import Edge, EdgeKind, Language, Node, NodeKind

SUFFIXES = frozenset({".sln"})

#: First-line marker of a Visual Studio solution (shared with discovery's sniff).
VS_SOLUTION_MARKER = "Microsoft Visual Studio Solution File"

#: Cap on constraint expressions recorded per action (a model can have many).
_MAX_CONSTRAINTS = 32

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"(?://|--)[^\n]*")
#: Meaningful tokens; multi-char operators precede ``>`` so ``>=`` is not an
#: invoke, and ``;`` is kept so ``>`` after a terminator reads as a "do".
_TOKEN_RE = re.compile(r"<=|>=|==|!=|[{};>]|[A-Za-z_]\w*")
#: A ``.dotted.path == value`` constraint (no nested braces/terminator).
_CONSTRAINT_RE = re.compile(r"\.[\w.\[\]]+\s*==\s*[^;{}]+")


def _strip_comments(text: str) -> str:
    """Blank ``//``/``--`` line and ``/* */`` block comments, preserving newlines.

    Blanking in place (rather than deleting) keeps byte offsets and line numbers
    aligned with the original, so a commented-out ``//>foo`` is not scanned as an
    invocation while node line spans stay accurate.
    """
    text = _BLOCK_COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
    text = _LINE_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), text)
    # The ``e`` code-segment markers would otherwise tokenize ``>`` as an invoke.
    return text.replace("<'", "  ").replace("'>", "  ")


class SlnParser:
    """Perspec SLN (``e``-dialect) action/invocation pass-1 scanner (M10)."""

    suffixes = SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        """Scan one SLN file for actions and their invocations. Tolerates garbage."""
        relpath = path.as_posix()
        ir = FileIR(path=relpath)
        file_id = file_node_id(relpath)
        file_node = Node(
            id=file_id,
            kind=NodeKind.FILE,
            name=path.name,
            qualified_name=relpath,
            file=relpath,
            language=Language.SLN,
        )
        ir.nodes.append(file_node)
        try:
            units = self._scan(ir, relpath, file_id, text)
        except Exception as exc:  # defensive: a parser bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
            return ir
        if units:
            file_node.attrs["units"] = sorted(units)
        return ir

    def _scan(self, ir: FileIR, relpath: str, file_id: str, text: str) -> set[str]:
        """Walk tokens, emitting ACTION nodes + invocation refs; return extend units."""
        stripped = _strip_comments(text)
        units: set[str] = set()
        used_ids: set[str] = set()
        depth = 0
        pending_action: tuple[str, int] | None = None  # (name, line) awaiting its '{'
        # Stack of (action node, brace depth at which its body opened).
        stack: list[tuple[Node, int]] = []
        spans: list[tuple[Node, int, int]] = []  # (node, open_pos, close_pos) for constraints
        open_pos: dict[int, int] = {}  # id(node) -> body open offset
        expect: str | None = None  # 'action' | 'extend' | 'invoke'
        prev: str | None = None  # last significant token, for '>' disambiguation

        for match in _TOKEN_RE.finditer(stripped):
            tok = match.group(0)
            if tok.isidentifier():
                if expect == "action":
                    pending_action = (tok, stripped.count("\n", 0, match.start()) + 1)
                    expect = None
                elif expect == "extend":
                    units.add(tok)
                    expect = None
                elif expect == "invoke":
                    self._invocation(ir, stack, tok, match.start(), stripped)
                    expect = None
                elif tok == "action":
                    expect = "action"
                elif tok == "extend":
                    expect = "extend"
                # any other identifier (compound, in, sequence, ...) is ignored
                prev = tok
                continue
            expect = None  # an operator/brace ends any pending name lookahead
            if tok == ">":
                # '>' is the "do"/invoke operator only at statement position
                # (body start, or after ';'/'}'); after an operand it is the
                # relational '>' of a constraint (`.field > n`), not an invoke.
                if prev is None or prev in ("{", "}", ";"):
                    expect = "invoke"
            elif tok == "{":
                depth += 1
                if pending_action is not None:
                    name, line = pending_action
                    node = self._new_action(ir, relpath, file_id, used_ids, name, line)
                    stack.append((node, depth))
                    open_pos[id(node)] = match.start()
                    pending_action = None
            elif tok == "}":
                if stack and stack[-1][1] == depth:
                    node, _ = stack.pop()
                    spans.append((node, open_pos.pop(id(node), match.start()), match.start()))
                depth = max(0, depth - 1)
            prev = tok
        self._attach_constraints(stripped, spans)
        return units

    def _new_action(
        self, ir: FileIR, relpath: str, file_id: str, used_ids: set[str], name: str, line: int
    ) -> Node:
        """Build an ACTION node (id deduped by line) and the file's DECLARES edge."""
        node_id = decl_node_id(relpath, NodeKind.ACTION, name)
        if node_id in used_ids:
            node_id = f"{node_id}@{line}"
        used_ids.add(node_id)
        node = Node(
            id=node_id,
            kind=NodeKind.ACTION,
            name=name,
            qualified_name=name,
            file=relpath,
            line_span=(line, line),
            language=Language.SLN,
            attrs={"invokes": []},
        )
        ir.nodes.append(node)
        ir.local_edges.append(Edge(src=file_id, dst=node.id, kind=EdgeKind.DECLARES))
        return node

    def _invocation(
        self, ir: FileIR, stack: list[tuple[Node, int]], invoked: str, pos: int, stripped: str
    ) -> None:
        """Record a ``>invoked`` on the enclosing action and emit INVOKES + TEST_COVERS refs."""
        if not stack:
            return  # an invocation outside any action body — nothing to attribute it to
        action = stack[-1][0]
        invokes = action.attrs["invokes"]
        assert isinstance(invokes, list)
        invokes.append(invoked)
        line = stripped.count("\n", 0, pos) + 1
        for edge_kind in (EdgeKind.INVOKES, EdgeKind.TEST_COVERS):
            ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=edge_kind,
                    src_id=action.id,
                    target_name=invoked,
                    line_span=(line, line),
                    attrs={"sln": True},
                )
            )

    def _attach_constraints(self, stripped: str, spans: list[tuple[Node, int, int]]) -> None:
        """Attribute each ``.path == value`` constraint to the innermost action containing it."""
        for match in _CONSTRAINT_RE.finditer(stripped):
            pos = match.start()
            inner: Node | None = None
            inner_open = -1
            for node, open_pos, close_pos in spans:
                if open_pos < pos < close_pos and open_pos > inner_open:
                    inner, inner_open = node, open_pos
            if inner is None:
                continue
            constraints = inner.attrs.setdefault("constraints", [])
            assert isinstance(constraints, list)
            if len(constraints) < _MAX_CONSTRAINTS:
                constraints.append(match.group(0).strip())
