"""Knowledge graph schema for HDL designs.

This module is the anchor of hdl-kgraph: every parser, linker, analysis, and
storage layer speaks in terms of the :class:`Node` and :class:`Edge` types
defined here. Milestones extend this schema; they do not rework it.

Confidence convention
---------------------
Every edge carries a ``confidence`` score describing how it was derived:

* ``1.0`` — syntactically resolved within the compilation unit.
* ``0.8`` — cross-file name match with a unique candidate.
* ``0.6`` — ambiguous name match (multiple candidates; an edge is emitted to
  each).
* ``0.4`` — heuristic (e.g. ``CLOCKED_BY`` inferred from ``clk``/``clock``
  naming patterns).

References that cannot be resolved at all become stub nodes with
``attrs["unresolved"] = True`` so the graph stays connected and queries never
dead-end silently.

Edge provenance (M7 semantic enrichment)
----------------------------------------
Edges derived or confirmed by a native-frontend elaboration backend (pyslang,
GHDL) carry ``attrs["source"] = "elaborated"`` and ``attrs["backend"]`` naming
the backend; their confidence is upgraded to ``1.0``. The tree-sitter baseline
leaves ``attrs["source"]`` absent, which reads as ``"heuristic"``. When
elaboration contradicts a heuristic edge, the heuristic edge is annotated with
``attrs["contradicted_by"]`` rather than being deleted (see
:mod:`hdl_kgraph.enrich`).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

CONFIDENCE_RESOLVED = 1.0
CONFIDENCE_UNIQUE_MATCH = 0.8
CONFIDENCE_AMBIGUOUS = 0.6
CONFIDENCE_HEURISTIC = 0.4


class Language(enum.Enum):
    """Source language a node was extracted from."""

    VERILOG = "verilog"
    SYSTEMVERILOG = "systemverilog"
    VHDL = "vhdl"
    # Future milestones (see ROADMAP.md M8/M9/M10).
    C = "c"
    CPP = "cpp"
    PYTHON = "python"
    FIRRTL = "firrtl"
    PERL = "perl"
    TCL = "tcl"  # also SDC/XDC/UPF files (Tcl subsets); see FILE attrs for the flavor
    SLN = "sln"  # Cadence Perspec System Level Notation (portable stimulus)
    UNKNOWN = "unknown"


class NodeKind(enum.Enum):
    """Kinds of entities extracted from HDL sources."""

    # Structure
    FILE = "file"
    FILELIST = "filelist"
    LIBRARY = "library"

    # Verilog / SystemVerilog design units
    MODULE = "module"  # covers macromodule via attrs["is_macromodule"]
    PROGRAM = "program"
    INTERFACE = "interface"
    MODPORT = "modport"
    PACKAGE = "package"
    CHECKER = "checker"
    PRIMITIVE = "primitive"  # UDP

    # VHDL design units (names normalized lowercase; original casing in attrs)
    ENTITY = "entity"
    ARCHITECTURE = "architecture"
    VHDL_PACKAGE = "vhdl_package"
    PACKAGE_BODY = "package_body"
    CONFIGURATION = "configuration"
    CONTEXT = "context"

    # Behavioral
    FUNCTION = "function"
    TASK = "task"
    PROCESS = "process"  # VHDL process / SV always block (attrs: always_ff/comb/latch)
    GENERATE_BLOCK = "generate_block"

    # OOP / verification
    CLASS = "class"
    CONSTRAINT = "constraint"
    COVERGROUP = "covergroup"
    COVERPOINT = "coverpoint"
    PROPERTY = "property"
    SEQUENCE = "sequence"
    ASSERTION = "assertion"
    CLOCKING_BLOCK = "clocking_block"

    # Data
    PORT = "port"
    PARAMETER = "parameter"  # parameter/localparam/VHDL generic via attrs
    SIGNAL = "signal"  # net/variable/VHDL signal via attrs
    TYPEDEF = "typedef"
    STRUCT = "struct"
    ENUM = "enum"
    ENUM_MEMBER = "enum_member"

    # Elaboration
    INSTANCE = "instance"

    # Preprocessor
    MACRO = "macro"  # `define
    INCLUDE_FILE = "include_file"

    # Constraints / scenarios (M10)
    CLOCK = "clock"  # SDC create_clock / create_generated_clock (may be virtual)
    TIMING_CONSTRAINT = "timing_constraint"  # false/multicycle path, delays, clock groups
    POWER_DOMAIN = "power_domain"  # UPF create_power_domain (strategies in attrs)
    SCENARIO = "scenario"  # SLN/PSS scenario
    ACTION = "action"  # SLN/PSS action (resources in attrs)


class EdgeKind(enum.Enum):
    """Kinds of relationships between nodes."""

    DECLARES = "declares"  # scope -> declaration
    INSTANTIATES = "instantiates"  # instance -> target module/entity
    CONNECTS = "connects"  # instance -> port binding (named/positional/wildcard)
    PARAMETERIZES = "parameterizes"  # instance -> parameter override
    IMPORTS = "imports"  # scope -> SV package (wildcard vs explicit in attrs)
    INCLUDES = "includes"  # file -> file (`include / Tcl source)
    DEFINES_MACRO = "defines_macro"
    USES_MACRO = "uses_macro"
    EXTENDS = "extends"  # SV class inheritance
    IMPLEMENTS = "implements"  # VHDL architecture -> entity
    BINDS = "binds"  # SV bind directive / VHDL configuration -> target
    USES_PACKAGE = "uses_package"  # VHDL library/use clause
    DRIVES = "drives"  # process/assign/instance port -> signal
    READS = "reads"
    CLOCKED_BY = "clocked_by"
    RESETS = "resets"
    ASSERTS_ON = "asserts_on"
    COVERS = "covers"
    TEST_COVERS = "test_covers"  # testbench/UVM test/SLN scenario -> DUT module
    FOREIGN_BINDS = "foreign_binds"  # SV DPI-C <-> C function (M8)
    GENERATED_FROM = "generated_from"  # generated HDL -> generator (M9 Chisel/etc., M10 Perl)
    CONSTRAINS = "constrains"  # timing constraint/clock/power domain -> design object (M10)
    REFERENCES_FILE = "references_file"  # script -> design file (M10; attrs: read/write/compile)
    INVOKES = "invokes"  # SLN/PSS action -> sub-action it does (same-file, M10)


@dataclass
class Node:
    """A single entity in the knowledge graph."""

    id: str
    kind: NodeKind
    name: str
    qualified_name: str = ""
    file: str = ""
    line_span: tuple[int, int] = (0, 0)
    language: Language = Language.UNKNOWN
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    """A directed relationship between two nodes."""

    src: str
    dst: str
    kind: EdgeKind
    confidence: float = CONFIDENCE_RESOLVED
    attrs: dict[str, Any] = field(default_factory=dict)
