"""Perl codegen-lineage parser tests (M10 fourth wedge).

Covers pass-1 extraction (REFERENCES_FILE with read/write mode, the generator
flag from a Verilog heredoc, GENERATED_FROM for written HDL) and the pass-2
file resolution: GENERATED_FROM points the generated file → its generator, and
binds to the real FILE node when the generated HDL is in the build.
"""

from pathlib import Path

from hdl_kgraph.ids import file_node_id
from hdl_kgraph.parser.base import FileIR
from hdl_kgraph.parser.perl import PerlParser
from hdl_kgraph.pipeline import default_db_path, run_build
from hdl_kgraph.schema import EdgeKind, Language, NodeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore


def refs_of(ir: FileIR, kind: EdgeKind) -> dict[str, dict]:
    """{target relpath: attrs} for refs of *kind* in *ir*."""
    return {r.target_name: r.attrs for r in ir.unresolved_refs if r.edge_kind is kind}


def file_node(ir: FileIR):
    return next(n for n in ir.nodes if n.kind is NodeKind.FILE)


# --------------------------------------------------------------------------- #
# Pass 1: extraction
# --------------------------------------------------------------------------- #
def test_perl_marks_generator_and_emits_lineage(fixtures_dir) -> None:
    ir = PerlParser().parse(Path("gen_fifo.pl"), (fixtures_dir / "perl/gen_fifo.pl").read_text())
    assert ir.parse_error_count == 0
    node = file_node(ir)
    assert node.language is Language.PERL
    assert node.attrs.get("generator") is True  # Verilog heredoc detected
    # The written .v is both referenced (mode=write) and generated-from.
    written = refs_of(ir, EdgeKind.REFERENCES_FILE)
    assert written["gen_fifo.v"]["mode"] == "write"
    assert "gen_fifo.v" in refs_of(ir, EdgeKind.GENERATED_FROM)


def test_perl_read_open_is_reference_only() -> None:
    """A read `open` of an HDL file is a REFERENCES_FILE(read), never GENERATED_FROM."""
    ir = PerlParser().parse(Path("lint.pl"), "open(my $fh, '<', 'rtl/top.v') or die;\n")
    assert refs_of(ir, EdgeKind.REFERENCES_FILE)["rtl/top.v"]["mode"] == "read"
    assert not refs_of(ir, EdgeKind.GENERATED_FROM)
    assert file_node(ir).attrs.get("generator") is None  # no module/endmodule body


def test_perl_non_generator_write_has_no_lineage() -> None:
    """Writing HDL without a Verilog body is referenced but not a generator."""
    ir = PerlParser().parse(Path("copy.pl"), "open(O, '>', 'out.sv');\n")  # no module body
    assert refs_of(ir, EdgeKind.REFERENCES_FILE)["out.sv"]["mode"] == "write"
    assert not refs_of(ir, EdgeKind.GENERATED_FROM)


def test_perl_two_arg_open_and_mode_split() -> None:
    """The 2-arg `open(FH, '>x.v')` form splits the glued mode from the path."""
    ir = PerlParser().parse(Path("g.pl"), "module x endmodule\nopen(FH, '>>build/x.v');\n")
    assert refs_of(ir, EdgeKind.REFERENCES_FILE)["build/x.v"]["mode"] == "write"


def test_perl_read_write_mode_counts_as_write() -> None:
    """A read-write open (`+>`) writes the file, so it is a write with lineage."""
    ir = PerlParser().parse(Path("g.pl"), "module m endmodule\nopen(my $fh, '+>', 'rw.v');\n")
    assert refs_of(ir, EdgeKind.REFERENCES_FILE)["rw.v"]["mode"] == "write"
    assert "rw.v" in refs_of(ir, EdgeKind.GENERATED_FROM)


def test_perl_skips_interpolated_and_non_hdl_paths() -> None:
    text = "open(A, '>', \"$dir/x.v\");\nopen(B, '>', 'notes.txt');\nopen(C, '>', 'real.v');\n"
    ir = PerlParser().parse(Path("g.pl"), "module m endmodule\n" + text)
    refs = refs_of(ir, EdgeKind.REFERENCES_FILE)
    assert set(refs) == {"real.v"}  # $dir interpolation and .txt are skipped


def test_perl_parser_tolerates_garbage() -> None:
    ir = PerlParser().parse(Path("junk.pl"), "open(((\n'>' module {{{ endmodule\n")
    assert ir.parse_error_count == 0  # malformed input is tolerated, never fatal


# --------------------------------------------------------------------------- #
# Pass 2: GENERATED_FROM direction + resolution, end-to-end (the ROADMAP accept)
# --------------------------------------------------------------------------- #
def test_generated_from_resolves_to_real_emitted_verilog(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """When the emitted Verilog is in the build, GENERATED_FROM binds its real node."""
    (tmp_path / "gen_fifo.pl").write_text((fixtures_dir / "perl/gen_fifo.pl").read_text())
    # The emitted file, present in the tree this time, so it resolves (not a stub).
    (tmp_path / "gen_fifo.v").write_text("module gen_fifo; endmodule\n")
    run_build(tmp_path)
    graph, _f, _m = SqliteStore(default_db_path(tmp_path)).load()
    gen = [(u, v) for u, v, d in graph.edges(data=True) if d["kind"] is EdgeKind.GENERATED_FROM]
    # generated Verilog -> generator script (the edge points back to the .pl).
    assert (file_node_id("gen_fifo.v"), file_node_id("gen_fifo.pl")) in gen
    assert graph.nodes[file_node_id("gen_fifo.v")]["language"] is Language.VERILOG
    assert graph.nodes[file_node_id("gen_fifo.v")]["attrs"].get("unresolved") is not True


def test_generated_from_stubs_when_emitted_verilog_absent(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """A generated file not in the build is a non-shadowing unresolved stub."""
    (tmp_path / "gen_fifo.pl").write_text((fixtures_dir / "perl/gen_fifo.pl").read_text())
    run_build(tmp_path)
    graph, _f, _m = SqliteStore(default_db_path(tmp_path)).load()
    stub = "unresolved:file:gen_fifo.v"
    assert stub in graph
    assert (stub, file_node_id("gen_fifo.pl")) in [
        (u, v) for u, v, d in graph.edges(data=True) if d["kind"] is EdgeKind.GENERATED_FROM
    ]
