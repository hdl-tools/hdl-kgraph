"""Parser backends route unambiguously by suffix; every M10 backend is implemented."""

from itertools import combinations
from pathlib import Path

from hdl_kgraph.parser.c import CParser, CppParser
from hdl_kgraph.parser.perl import PerlParser
from hdl_kgraph.parser.python import PythonParser
from hdl_kgraph.parser.sln import SlnParser
from hdl_kgraph.parser.systemverilog import SystemVerilogParser
from hdl_kgraph.parser.tcl import SdcParser, TclScriptParser, UpfParser
from hdl_kgraph.parser.vhdl import VhdlParser

ALL_BACKENDS = [
    SystemVerilogParser,
    VhdlParser,
    CParser,
    CppParser,
    PythonParser,
    SdcParser,
    UpfParser,
    TclScriptParser,
    PerlParser,
    SlnParser,
]


def test_suffix_sets_disjoint() -> None:
    """No two backends claim the same file suffix (routing must be unambiguous)."""
    for a, b in combinations(ALL_BACKENDS, 2):
        assert not (a.suffixes & b.suffixes), f"{a.__name__} and {b.__name__} overlap"


def test_c_family_suffixes_route_through_discovery() -> None:
    """The M8 C/C++/Python backends are implemented, so their suffixes are discoverable."""
    from hdl_kgraph import discovery

    assert CParser.suffixes <= discovery.SUFFIXES
    assert CppParser.suffixes <= discovery.SUFFIXES
    assert PythonParser.suffixes <= discovery.SUFFIXES


def test_m10_suffixes_route_through_discovery() -> None:
    """Every M10 backend (SDC/XDC, UPF, Tcl flow, Perl, SLN) is implemented and discoverable."""
    from hdl_kgraph import discovery

    for backend in (SdcParser, UpfParser, TclScriptParser, PerlParser, SlnParser):
        assert backend.suffixes <= discovery.SUFFIXES, backend.__name__


def test_unsupported_suffix_is_skipped(tmp_path: Path) -> None:
    """A file whose suffix no backend claims is skipped as ``unsupported``, never parsed."""
    from hdl_kgraph.discovery import check_file

    other = tmp_path / "notes.md"
    other.write_text("not a source file\n")
    assert check_file(other, tmp_path).skipped_reason == "unsupported"
