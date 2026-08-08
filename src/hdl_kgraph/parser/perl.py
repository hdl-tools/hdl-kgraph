"""Perl parser backend (M10).

Implementation notes:

* Scope is legacy EDA codegen scripts, not Perl semantics: detect which HDL
  files a script reads/writes/generates and record the lineage.
* ``open()`` calls whose path literal ends in an HDL suffix (``.v``/``.sv``/
  ``.vhd``/...) -> REFERENCES_FILE edges with ``attrs["mode"]`` =
  ``read``/``write``; a heredoc/body that looks like Verilog (``module``...
  ``endmodule``) marks the script as a generator.
* Each HDL file the generator *writes* links back via GENERATED_FROM (the same
  edge M9 uses for Chisel/Amaranth/SpinalHDL output): generated file ->
  generator script.
* A line/regex scan, not a Perl parser; ``tree-sitter-perl`` exists if that
  proves insufficient. Expectations are modest by design (see ROADMAP.md
  "Risks"): only literal quoted paths in parenthesized ``open(...)`` calls are
  recognized — an interpolated path (``"$dir/x.v"``) is left for a later pass.
* Paths are normalized into the build-root relpath keyspace relative to the
  script. An *absolute* path is kept verbatim here and canonicalized onto that
  keyspace by the linker when it falls inside the build root (#164); an
  out-of-tree absolute still resolves to an unresolved stub. Only a
  ``$var``-interpolated path remains unresolved (no literal to canonicalize).
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from pathlib import Path

from hdl_kgraph.ids import file_node_id
from hdl_kgraph.parser.base import FileIR, UnresolvedRef
from hdl_kgraph.schema import EdgeKind, Language, Node, NodeKind

SUFFIXES = frozenset({".pl", ".pm"})

#: HDL file suffixes a codegen script's ``open()`` paths are matched against.
_HDL_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"})

#: A parenthesized ``open(...)`` call's argument list.
_OPEN_RE = re.compile(r"\bopen\b\s*\(([^)]*)\)")
#: Single- or double-quoted string literals within those args.
_STR_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")
#: Leading file-mode characters of a 2-arg ``open`` (``>``/``>>``/``<``/``+<``).
_MODE_RE = re.compile(r"^\s*(\+?<|\+?>>?|>>?)")
#: A Verilog module body (heredoc or otherwise) marking the script a generator.
_VERILOG_RE = re.compile(r"\bmodule\b.*?\bendmodule\b", re.DOTALL)


def _is_hdl_path(path: str) -> bool:
    """True if *path* has an HDL suffix (``.v``/``.sv``/``.vhd``/...)."""
    return posixpath.splitext(path)[1].lower() in _HDL_SUFFIXES


def _iter_open_refs(text: str) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line, mode_token, path)`` for each ``open(...)`` with a literal path.

    Handles the 3-arg form ``open($fh, '>', 'x.v')`` (mode and path are separate
    quoted args, the filehandle is unquoted) and the 2-arg ``open($fh, '>x.v')``
    (mode glued to the path). A trailing ``or die "..."`` is excluded by matching
    only inside the call's parentheses.
    """
    for match in _OPEN_RE.finditer(text):
        line = text.count("\n", 0, match.start()) + 1
        strings = [single or double for single, double in _STR_RE.findall(match.group(1))]
        if not strings:
            continue
        if len(strings) >= 2:
            mode_token, path = strings[0], strings[1]
        else:
            token = strings[0]
            mode_match = _MODE_RE.match(token)
            mode_token, path = (
                (mode_match.group(1), token[mode_match.end() :]) if mode_match else ("<", token)
            )
        yield line, mode_token.strip(), path.strip()


class PerlParser:
    """Perl codegen-lineage pass-1 scanner (M10 fourth wedge)."""

    suffixes = SUFFIXES

    def parse(self, path: Path, text: str) -> FileIR:
        """Scan one Perl script for HDL file lineage. Tolerates malformed input."""
        relpath = path.as_posix()
        ir = FileIR(path=relpath)
        file_id = file_node_id(relpath)
        is_generator = bool(_VERILOG_RE.search(text))
        ir.nodes.append(
            Node(
                id=file_id,
                kind=NodeKind.FILE,
                name=path.name,
                qualified_name=relpath,
                file=relpath,
                language=Language.PERL,
                attrs={"generator": True} if is_generator else {},
            )
        )
        try:
            self._scan(ir, relpath, file_id, text, is_generator)
        except Exception as exc:  # defensive: a parser bug must not abort the build
            ir.record_parse_error(f"{relpath}: internal parser error ({exc})")
        return ir

    def _scan(self, ir: FileIR, relpath: str, file_id: str, text: str, is_generator: bool) -> None:
        """Emit a REFERENCES_FILE ref per HDL ``open()``, and GENERATED_FROM for writes."""
        script_dir = posixpath.dirname(relpath)
        for line, mode_token, path in _iter_open_refs(text):
            # An interpolated path is not a literal we can resolve; skip it.
            if "$" in path or not _is_hdl_path(path):
                continue
            rel = (
                path
                if posixpath.isabs(path)
                else posixpath.normpath(posixpath.join(script_dir, path))
            )
            # Any mode containing ``>`` writes: ``>``/``>>`` and the read-write
            # ``+>``/``+>>`` (truncate/append) variants. ``<``/``+<`` are reads.
            writes = ">" in mode_token
            ir.unresolved_refs.append(
                UnresolvedRef(
                    edge_kind=EdgeKind.REFERENCES_FILE,
                    src_id=file_id,
                    target_name=rel,
                    line_span=(line, line),
                    attrs={"file_ref": True, "mode": "write" if writes else "read", "line": line},
                )
            )
            # A generator's *written* HDL is generated from this script.
            if is_generator and writes:
                ir.unresolved_refs.append(
                    UnresolvedRef(
                        edge_kind=EdgeKind.GENERATED_FROM,
                        src_id=file_id,
                        target_name=rel,
                        line_span=(line, line),
                        attrs={"file_ref": True, "line": line},
                    )
                )
