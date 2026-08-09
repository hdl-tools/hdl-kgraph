# Grammar bake-off: SystemVerilog/Verilog tree-sitter grammars

**Decision: `tree-sitter-systemverilog` (gmlarumbe) for both `.v` and `.sv`.**

Evaluated per ROADMAP.md M1 / Risk #1 with `scripts/grammar_bakeoff.py`
against the fixture corpus in `tests/fixtures/` (18 SV/Verilog files covering
modules, non-ANSI ports, interfaces, packages, classes, programs,
instantiations, and one intentionally broken file).

## Candidates

| | `gmlarumbe/tree-sitter-systemverilog` | `tree-sitter/tree-sitter-verilog` |
|---|---|---|
| PyPI package | `tree-sitter-systemverilog` 0.3.1 | `tree-sitter-verilog` 1.0.3 |
| Wheels | abi3 cp310: manylinux/musllinux x86_64+aarch64, macOS x86_64+arm64, Windows amd64+arm64 | abi3 cp39, same platforms |
| Coverage | full IEEE 1800-2023, validated against sv-tests/UVM/cva6 | stale; classes, constraints, many SV-2017 constructs error |
| Maintenance | active (powers Emacs `verilog-ts-mode`, nvim-treesitter, helix) | grammar effectively unmaintained |

## Results (2026-06-10, fixture corpus)

```text
                              ERROR nodes   err-bytes %
tree-sitter-systemverilog          1            1.8 %     (only broken.sv, intentional)
tree-sitter-verilog                2            2.4 %     (broken.sv + uses_interface.sv)
```

- `tree-sitter-systemverilog`: zero ERROR nodes on every clean fixture; all
  expected constructs (module/interface/package/program/class declarations)
  recognized, including the `bus_if.slave bus` interface port.
- `tree-sitter-verilog`: ERROR on the interface-port header in
  `uses_interface.sv` — exactly the IEEE-1800 weakness flagged in ROADMAP
  Risk #1. Disqualifying for the M1 CLASS/EXTENDS and interface requirements
  even before considering real-world SV.

Both grammars use IEEE-BNF-style node names; the parser dispatch table in
`src/hdl_kgraph/parser/systemverilog.py` was written against node-type dumps
from the chosen grammar (`scripts/grammar_bakeoff.py --dump-tree FILE`).

## Reproducing

```sh
pip install tree-sitter-verilog   # the loser is not a project dependency
python scripts/grammar_bakeoff.py tests/fixtures
python scripts/grammar_bakeoff.py --dump-tree tests/fixtures/top.v
```

API-churn note (Risk #5): the parser walks trees manually via `node.type`
dispatch (stable across py-tree-sitter releases) rather than the
Query/QueryCursor API that changed between 0.23 and 0.25.

# VHDL grammar (M3)

**Decision: `tree-sitter-vhdl` (jpt13653903) for `.vhd` and `.vhdl`.**

Evaluated per ROADMAP.md M3 / Risk #2. There was no real contest:
`alemuller/tree-sitter-vhdl` is unmaintained and has no PyPI package, while
the PyPI `tree-sitter-vhdl` package *is* the maintained jpt13653903 grammar
(1.5.0 at evaluation time) with the same abi3 cp310 wheel matrix as
`tree-sitter-systemverilog` (manylinux/musllinux x86_64+aarch64, macOS
x86_64+arm64, Windows amd64+arm64).

## Results (2026-06-11, fixture corpus)

Zero ERROR/MISSING nodes on every clean VHDL fixture; entities,
architectures, packages, package bodies, configurations, component and
direct-entity instantiation all produce well-shaped subtrees (confirmed with
`--dump-tree`, which now routes VHDL files to this grammar).

## Caveats

- The grammar describes itself as built for **syntax highlighting with a
  simplified grammar**: some invalid VHDL parses to a valid tree, and rare
  valid constructs may not. Acceptable for structural extraction under the
  project's confidence-scoring contract; elaboration accuracy is M7
  (pyVHDLModel/GHDL backend) territory.
- Identifiers that collide with names the grammar knows from the standard
  libraries lex as `library_type`/`library_namespace`/`library_constant_*`
  instead of `identifier` (e.g. a generic named `WIDTH`). The parser
  therefore extracts names by *text*, never by relying on the `identifier`
  node type alone.
- Case-insensitivity and library/work scoping are handled in our layer
  (`src/hdl_kgraph/parser/vhdl.py` normalizes names to lowercase, original
  casing in `attrs["original_name"]`), not by the grammar.
