"""counters: one count per pulse, dropped frames summed from three sources, saturation at
2^32 - 1, cleared by reset.

    python3.11 tb/counters/test_counters.py             (Questa)
    SIM=nvc python3.11 tb/counters/test_counters.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.handle import Deposit  # noqa: E402
from cocotb.triggers import FallingEdge, RisingEdge  # noqa: E402

from itch.hw_model import COUNTERS  # noqa: E402
from tb.common.runner import RTL, WRAPPERS, run  # noqa: E402

CLOCK_PS = 6400
MAX = 0xFFFF_FFFF
DROPPED_FRAMES = COUNTERS.index("dropped_frames")


def counts_of(dut) -> list[int]:
    flat = dut.counts.value.to_unsigned()
    return [flat >> 32 * i & MAX for i in range(len(COUNTERS))]


async def start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.pulses.value = 0
    dut.framer_drop_pulse.value = 0
    dut.busy_drop_pulse.value = 0
    dut.rst.value = 1
    await FallingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst.value = 0


@cocotb.test()
async def counts_follow_random_pulses(dut):
    """Every pulse and drop source at random, including all three drop sources at once."""
    assert len(dut.pulses) == len(COUNTERS)
    rng = random.Random(1)
    await start(dut)
    expected = [0] * len(COUNTERS)
    for _ in range(2000):
        await FallingEdge(dut.clk)
        pulses = rng.getrandbits(len(COUNTERS))
        framer_drop, busy_drop = rng.getrandbits(1), rng.getrandbits(1)
        dut.pulses.value = pulses
        dut.framer_drop_pulse.value = framer_drop
        dut.busy_drop_pulse.value = busy_drop
        for i in range(len(COUNTERS)):
            expected[i] += pulses >> i & 1
        expected[DROPPED_FRAMES] += framer_drop + busy_drop
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        dut.pulses.value = 0
        dut.framer_drop_pulse.value = 0
        dut.busy_drop_pulse.value = 0
        assert counts_of(dut) == expected


@cocotb.test()
async def counters_stop_at_the_maximum(dut):
    await start(dut)
    for counter in range(len(COUNTERS)):
        dut.dut.count[counter].value = Deposit(MAX - 2)
    for _ in range(3):
        await FallingEdge(dut.clk)
        dut.pulses.value = (1 << len(COUNTERS)) - 1
        dut.framer_drop_pulse.value = 1
        dut.busy_drop_pulse.value = 1
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.pulses.value = 0
    counts = counts_of(dut)
    assert counts[DROPPED_FRAMES] == MAX  # MAX - 2 + 3: stops instead of wrapping
    assert all(count == MAX for count in counts), [hex(count) for count in counts]


@cocotb.test()
async def reset_clears_and_wins_over_pulses(dut):
    await start(dut)
    for _ in range(5):
        await FallingEdge(dut.clk)
        dut.pulses.value = (1 << len(COUNTERS)) - 1
    await FallingEdge(dut.clk)
    dut.rst.value = 1
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    dut.pulses.value = 0
    assert counts_of(dut) == [0] * len(COUNTERS)


def test_counters():
    run("counters_tb_top", [RTL / "itch_pkg.vhd", RTL / "counters.vhd", WRAPPERS / "counters_tb_top.vhd"], __file__)


if __name__ == "__main__":
    test_counters()
