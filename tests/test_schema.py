"""The schema is the project's contract — guard its shape."""

from hdl_kgraph.schema import (
    CONFIDENCE_AMBIGUOUS,
    CONFIDENCE_HEURISTIC,
    CONFIDENCE_RESOLVED,
    CONFIDENCE_UNIQUE_MATCH,
    Edge,
    EdgeKind,
    Language,
    Node,
    NodeKind,
)

EXPECTED_NODE_KINDS = {
    # Structure
    "FILE",
    "FILELIST",
    "LIBRARY",
    # Verilog/SV design units
    "MODULE",
    "PROGRAM",
    "INTERFACE",
    "MODPORT",
    "PACKAGE",
    "CHECKER",
    "PRIMITIVE",
    # VHDL design units
    "ENTITY",
    "ARCHITECTURE",
    "VHDL_PACKAGE",
    "PACKAGE_BODY",
    "CONFIGURATION",
    "CONTEXT",
    # Behavioral
    "FUNCTION",
    "TASK",
    "PROCESS",
    "GENERATE_BLOCK",
    # OOP / verification
    "CLASS",
    "CONSTRAINT",
    "COVERGROUP",
    "COVERPOINT",
    "PROPERTY",
    "SEQUENCE",
    "ASSERTION",
    "CLOCKING_BLOCK",
    # Data
    "PORT",
    "PARAMETER",
    "SIGNAL",
    "TYPEDEF",
    "STRUCT",
    "ENUM",
    "ENUM_MEMBER",
    # Elaboration
    "INSTANCE",
    # Preprocessor
    "MACRO",
    "INCLUDE_FILE",
    # Constraints / scenarios
    "CLOCK",
    "TIMING_CONSTRAINT",
    "POWER_DOMAIN",
    "SCENARIO",
    "ACTION",
}

EXPECTED_EDGE_KINDS = {
    "DECLARES",
    "INSTANTIATES",
    "CONNECTS",
    "PARAMETERIZES",
    "IMPORTS",
    "INCLUDES",
    "DEFINES_MACRO",
    "USES_MACRO",
    "EXTENDS",
    "IMPLEMENTS",
    "BINDS",
    "USES_PACKAGE",
    "DRIVES",
    "READS",
    "CLOCKED_BY",
    "RESETS",
    "ASSERTS_ON",
    "COVERS",
    "TEST_COVERS",
    "FOREIGN_BINDS",
    "GENERATED_FROM",
    "CONSTRAINS",
    "REFERENCES_FILE",
    "INVOKES",
}

EXPECTED_LANGUAGES = {
    "VERILOG",
    "SYSTEMVERILOG",
    "VHDL",
    "C",
    "CPP",
    "PYTHON",
    "FIRRTL",
    "PERL",
    "TCL",
    "SLN",
    "UNKNOWN",
}


def test_node_kinds_complete() -> None:
    """NodeKind matches the schema documented in ROADMAP.md exactly."""
    assert {k.name for k in NodeKind} == EXPECTED_NODE_KINDS


def test_edge_kinds_complete() -> None:
    """EdgeKind matches the schema documented in ROADMAP.md exactly."""
    assert {k.name for k in EdgeKind} == EXPECTED_EDGE_KINDS


def test_languages_complete() -> None:
    """Language matches the targets documented in ROADMAP.md exactly."""
    assert {lang.name for lang in Language} == EXPECTED_LANGUAGES


def test_kind_values_unique() -> None:
    """Enum string values never collide (they are persisted to SQLite)."""
    assert len({k.value for k in NodeKind}) == len(NodeKind)
    assert len({k.value for k in EdgeKind}) == len(EdgeKind)
    assert len({lang.value for lang in Language}) == len(Language)


def test_confidence_ordering() -> None:
    """Confidence tiers are strictly ordered from resolved down to heuristic."""
    assert (
        CONFIDENCE_RESOLVED
        > CONFIDENCE_UNIQUE_MATCH
        > CONFIDENCE_AMBIGUOUS
        > CONFIDENCE_HEURISTIC
        > 0.0
    )


def test_node_construction() -> None:
    """A fully-populated Node round-trips its fields and attrs."""
    node = Node(
        id="counter.sv::counter",
        kind=NodeKind.MODULE,
        name="counter",
        qualified_name="counter",
        file="counter.sv",
        line_span=(1, 20),
        language=Language.SYSTEMVERILOG,
        attrs={"is_macromodule": False},
    )
    assert node.kind is NodeKind.MODULE
    assert node.attrs["is_macromodule"] is False


def test_edge_defaults() -> None:
    """Edges default to full confidence with empty attrs."""
    edge = Edge(src="a", dst="b", kind=EdgeKind.INSTANTIATES)
    assert edge.confidence == CONFIDENCE_RESOLVED
    assert edge.attrs == {}


def test_unresolved_stub_convention() -> None:
    """Unresolved references are representable as stub nodes per the schema docs."""
    stub = Node(id="?missing_mod", kind=NodeKind.MODULE, name="missing_mod")
    stub.attrs["unresolved"] = True
    assert stub.attrs.get("unresolved") is True
