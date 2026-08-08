"""M8 cocotb fixture: a testbench driving/sampling the counter DUT.

The DUT is resolved from the filename (``test_counter.py`` -> ``counter``)
unless a top module is configured.
"""

import cocotb
from cocotb.triggers import RisingEdge, Timer


@cocotb.test()
async def test_count_up(dut):
    # Drives: assignment to .value and setimmediatevalue.
    dut.rst_n.value = 0
    dut.enable.value = 1
    dut.data.setimmediatevalue(0)
    await Timer(10, units="ns")
    dut.rst_n.value = 1
    await RisingEdge(dut.clk)
    # Reads: sampling .value and a bare handle in a trigger.
    got = dut.count.value
    assert got == 0
    if dut.overflow.value:
        raise AssertionError("unexpected overflow")


@cocotb.test(skip=True)
async def test_skipped(dut):
    x = dut.count.value
