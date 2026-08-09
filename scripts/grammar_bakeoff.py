#!/usr/bin/env python3
"""Grammar bake-off: compare tree-sitter grammars on the fixture corpus.

Parses every ``.v``/``.sv``/``.svh`` (SV/Verilog candidates) and
``.vhd``/``.vhdl`` (VHDL candidates) file under a directory with each
installed candidate grammar and reports, per file:

* ERROR-node count and MISSING-node count
* percentage of source bytes covered by ERROR subtrees
* which expected top-level constructs (module/interface/package/program/class
  declarations) were found

Candidates are imported lazily, so the script works with whichever subset of
``tree-sitter-systemverilog`` / ``tree-sitter-verilog`` / ``tree-sitter-vhdl``
is installed. Each candidate only parses files whose suffix it serves.

Usage::

    python scripts/grammar_bakeoff.py [DIR]            # default: tests/fixtures
    python scripts/grammar_bakeoff.py --dump-tree FILE # print the node.type tree

The ``--dump-tree`` mode is how the exact node-type names used by the parser
dispatch tables (src/hdl_kgraph/parser/systemverilog.py and
src/hdl_kgraph/parser/vhdl.py) were confirmed.

Results and the grammar decision are recorded in docs/internals/grammar-bakeoff.md.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser

SV_SUFFIXES = {".v", ".vh", ".sv", ".svh"}
VHDL_SUFFIXES = {".vhd", ".vhdl"}
SUFFIXES = SV_SUFFIXES | VHDL_SUFFIXES

# Node types that indicate a construct was recognized, per grammar family.
# The SV grammars follow the IEEE 1800 BNF naming; the VHDL names are from
# the jpt13653903 grammar's node-types.json. (VHDL reuses the
# ``interface_declaration`` type name for ports, hence per-candidate sets.)
SV_CONSTRUCTS = frozenset(
    {
        "module_declaration",
        "interface_declaration",
        "package_declaration",
        "program_declaration",
        "class_declaration",
    }
)
VHDL_CONSTRUCTS = frozenset(
    {
        "entity_declaration",
        "architecture_definition",
        "package_declaration",
        "package_definition",  # package body
        "configuration_declaration",
    }
)


@dataclass
class Candidate:
    language: Language
    suffixes: set[str]
    constructs: frozenset[str]


def load_candidates() -> dict[str, Candidate]:
    candidates: dict[str, Candidate] = {}
    try:
        import tree_sitter_systemverilog

        candidates["tree-sitter-systemverilog"] = Candidate(
            Language(tree_sitter_systemverilog.language()), SV_SUFFIXES, SV_CONSTRUCTS
        )
    except ImportError:
        pass
    try:
        import tree_sitter_verilog

        candidates["tree-sitter-verilog"] = Candidate(
            Language(tree_sitter_verilog.language()), SV_SUFFIXES, SV_CONSTRUCTS
        )
    except ImportError:
        pass
    try:
        import tree_sitter_vhdl

        candidates["tree-sitter-vhdl"] = Candidate(
            Language(tree_sitter_vhdl.language()), VHDL_SUFFIXES, VHDL_CONSTRUCTS
        )
    except ImportError:
        pass
    return candidates


@dataclass
class FileResult:
    path: Path
    error_nodes: int = 0
    missing_nodes: int = 0
    error_bytes: int = 0
    total_bytes: int = 0
    constructs: set[str] = field(default_factory=set)

    @property
    def error_pct(self) -> float:
        return 100.0 * self.error_bytes / self.total_bytes if self.total_bytes else 0.0


def analyze(root: Node, total_bytes: int, path: Path, expected: frozenset[str]) -> FileResult:
    result = FileResult(path=path, total_bytes=total_bytes)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.is_missing:
            result.missing_nodes += 1
        if node.type == "ERROR":
            result.error_nodes += 1
            result.error_bytes += node.end_byte - node.start_byte
            # Do not descend: bytes are already counted for the whole subtree.
            continue
        if node.type in expected:
            result.constructs.add(node.type)
        stack.extend(node.children)
    return result


def dump_tree(node: Node, source: bytes, depth: int = 0) -> None:
    text = source[node.start_byte : node.end_byte].decode(errors="replace")
    snippet = text.splitlines()[0][:40] if text else ""
    print(f"{'  ' * depth}{node.type} [{node.start_point[0] + 1}] {snippet!r}")
    for child in node.children:
        dump_tree(child, source, depth + 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dir", nargs="?", default="tests/fixtures", type=Path)
    ap.add_argument("--dump-tree", type=Path, metavar="FILE")
    ap.add_argument("--grammar", help="restrict to one candidate (substring match)")
    args = ap.parse_args()

    candidates = load_candidates()
    if args.grammar:
        candidates = {k: v for k, v in candidates.items() if args.grammar in k}
    if not candidates:
        print("no candidate grammars installed", file=sys.stderr)
        return 1

    if args.dump_tree:
        source = args.dump_tree.read_bytes()
        for name, candidate in candidates.items():
            if args.dump_tree.suffix not in candidate.suffixes:
                continue
            print(f"=== {name} ===")
            tree = Parser(candidate.language).parse(source)
            dump_tree(tree.root_node, source)
        return 0

    all_files = sorted(p for p in args.dir.rglob("*") if p.suffix in SUFFIXES)
    if not all_files:
        print(f"no HDL files under {args.dir}", file=sys.stderr)
        return 1

    for name, candidate in candidates.items():
        files = [p for p in all_files if p.suffix in candidate.suffixes]
        if not files:
            continue
        parser = Parser(candidate.language)
        print(f"\n=== {name} ===")
        print(f"{'file':40} {'ERR':>4} {'MISS':>5} {'err%':>6}  constructs")
        totals = FileResult(path=Path("TOTAL"))
        for path in files:
            source = path.read_bytes()
            tree = parser.parse(source)
            r = analyze(tree.root_node, len(source), path, candidate.constructs)
            totals.error_nodes += r.error_nodes
            totals.missing_nodes += r.missing_nodes
            totals.error_bytes += r.error_bytes
            totals.total_bytes += r.total_bytes
            constructs = ",".join(sorted(c.removesuffix("_declaration") for c in r.constructs))
            print(
                f"{str(path.name):40} {r.error_nodes:>4} {r.missing_nodes:>5}"
                f" {r.error_pct:>5.1f}%  {constructs}"
            )
        print(
            f"{'TOTAL':40} {totals.error_nodes:>4} {totals.missing_nodes:>5}"
            f" {totals.error_pct:>5.1f}%"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
