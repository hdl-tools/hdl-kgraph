"""Reset-name recognition, and what misreading it costs downstream.

A reset that is not recognised as one does not merely lose its RESETS edge. In
``@(posedge clk or negedge rst)`` the parser classifies the edge terms: with
the reset unrecognised, *both* terms become clock candidates, so both are
emitted as low-confidence CLOCKED_BY. The process then has two clock domains,
:mod:`hdl_kgraph.graph.clocks` skips it as ambiguous, and it contributes to no
domain at all — so CDC detection goes blind on exactly the modules where clock
crossings live.

That is why these tests assert on the *consequences* (edge kinds, confidence,
domain membership) and not only on the regex.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from hdl_kgraph.cli.main import main
from hdl_kgraph.parser.base import RESET_NAME_RE
from hdl_kgraph.schema import EdgeKind
from hdl_kgraph.storage.sqlite_store import SqliteStore

#: Spellings that must be recognised. The suffixed and infixed forms are the
#: regression: `_i`/`_o` port suffixes are a common convention, and an
#: `$`-anchored pattern rejects every one of them.
RESET_NAMES = [
    "rst",
    "rst_n",
    "rstn",
    "reset_n",
    "resetb",
    "arst_n",
    "areset_n",
    "rst_n_i",
    "rst_ni",
    "wr_rst_n_i",
    "rd_rst_n_i",
    "m_rst_sync_n",
    "s_rst_sync_n",
    "clear_i",
    "auto_clear",
]

#: Names that must NOT be read as resets — clocks, and words that merely
#: contain the letters.
NON_RESET_NAMES = [
    "clk",
    "clk_i",
    "pclk",
    "aclk",
    "hclk",
    "clock",
    "clk0",
    "ref_clk_i",
    "wr_clk_i",
    "rd_clk_i",
    "m_clk_i",
    "s_clk_i",
    "clkdiv",
    "restart",
    "wrist_data",
    "crystal_en",
    "color_reg",
]


@pytest.mark.parametrize("name", RESET_NAMES)
def test_reset_names_are_recognised(name: str) -> None:
    assert RESET_NAME_RE.search(name), f"{name} should read as a reset"


@pytest.mark.parametrize("name", NON_RESET_NAMES)
def test_non_reset_names_are_not(name: str) -> None:
    assert not RESET_NAME_RE.search(name), f"{name} should not read as a reset"


def test_the_two_parser_backends_share_one_pattern() -> None:
    """They held identical private copies and could drift apart."""
    from hdl_kgraph.parser import systemverilog, vhdl

    assert systemverilog.RESET_NAME_RE is RESET_NAME_RE
    assert vhdl.RESET_NAME_RE is RESET_NAME_RE


@pytest.fixture(scope="module")
def suffixed_graph(tmp_path_factory: pytest.TempPathFactory, fixtures_dir: Path):
    root = tmp_path_factory.mktemp("reset_suffixed")
    shutil.copy(fixtures_dir / "reset_suffixed.sv", root / "reset_suffixed.sv")
    result = CliRunner().invoke(main, ["build", str(root)])
    assert result.exit_code == 0, result.output
    graph, _files, _meta = SqliteStore(root / ".hdl-kgraph" / "graph.db").load()
    return graph


def _targets(graph, kind: EdgeKind) -> set[str]:
    return {
        graph.nodes[dst]["name"]
        for _src, dst, data in graph.edges(data=True)
        if data["kind"] is kind
    }


def test_suffixed_resets_become_resets_not_clocks(suffixed_graph) -> None:
    resets = _targets(suffixed_graph, EdgeKind.RESETS)
    clocks = _targets(suffixed_graph, EdgeKind.CLOCKED_BY)

    assert {"wr_rst_n_i", "m_rst_sync_n"} <= resets
    # The regression: these used to appear here, as clock domains.
    assert "wr_rst_n_i" not in clocks
    assert "m_rst_sync_n" not in clocks
    assert clocks == {"wr_clk_i", "rd_clk_i"}


def test_the_clock_keeps_full_confidence(suffixed_graph) -> None:
    """With the reset recognised, the remaining term is the unique clock and is
    resolved evidence — not the 0.4 `ambiguous_sensitivity` fallback."""
    for _src, _dst, data in suffixed_graph.edges(data=True):
        if data["kind"] is EdgeKind.CLOCKED_BY:
            assert data["confidence"] == 1.0
            assert data["attrs"].get("evidence") == "sensitivity"


def test_each_process_has_exactly_one_clock_domain(suffixed_graph) -> None:
    """Two CLOCKED_BY edges on a process make it ambiguous, and the domain
    analysis drops it — the mechanism that emptied `cdc_suspects`."""
    per_process: dict[str, int] = {}
    for src, _dst, data in suffixed_graph.edges(data=True):
        if data["kind"] is EdgeKind.CLOCKED_BY:
            per_process[src] = per_process.get(src, 0) + 1
    assert per_process, "the fixture has two clocked processes"
    assert set(per_process.values()) == {1}


def test_both_clock_domains_are_reported(suffixed_graph) -> None:
    """The payoff: two distinct domains, so a crossing between them is visible
    at all."""
    from hdl_kgraph.graph.clocks import clock_domains

    domains = clock_domains(suffixed_graph)
    names = {n for d in domains for n in d.clock_names}
    assert {"wr_clk_i", "rd_clk_i"} <= names
    assert not any("rst" in n for n in names), "a reset must never head a domain"
