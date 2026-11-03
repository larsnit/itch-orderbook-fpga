"""lutram: read data follows the address in the same cycle; a write takes effect only after
its cycle (what the bitmap search timing relies on).

    python3.11 tb/memories/test_lutram.py             (Questa)
    SIM=nvc python3.11 tb/memories/test_lutram.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, RisingEdge  # noqa: E402

from tb.common.runner import RTL, run  # noqa: E402

CLOCK_PS = 6400  # 156.25 MHz
LUTRAM_WIDTH = 64
LUTRAM_DEPTH = 128
CYCLES = 400


def value_of(signal) -> int:
    return signal.value.to_unsigned()


async def start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.write_enable.value = 0
    dut.read_address.value = 0
    dut.write_address.value = 0
    dut.write_data.value = 0


async def fill(dut, depth: int, width: int, rng: random.Random) -> list[int]:
    contents = [rng.getrandbits(width) for _ in range(depth)]
    for address, data in enumerate(contents):
        await FallingEdge(dut.clk)
        dut.write_enable.value = 1
        dut.write_address.value = address
        dut.write_data.value = data
    await FallingEdge(dut.clk)
    dut.write_enable.value = 0
    return contents


@cocotb.test()
async def lutram_reads_in_the_same_cycle(dut):
    rng = random.Random(20)
    await start(dut)
    contents = await fill(dut, LUTRAM_DEPTH, LUTRAM_WIDTH, rng)

    for _ in range(CYCLES):
        await FallingEdge(dut.clk)
        address = rng.randrange(LUTRAM_DEPTH)
        new_data = rng.getrandbits(LUTRAM_WIDTH)
        dut.read_address.value = address
        dut.write_enable.value = 1
        dut.write_address.value = address
        dut.write_data.value = new_data
        await RisingEdge(dut.clk)
        # the write to the same address takes effect only after this cycle
        assert value_of(dut.read_data) == contents[address]
        contents[address] = new_data
        await FallingEdge(dut.clk)
        dut.write_enable.value = 0
        await RisingEdge(dut.clk)
        assert value_of(dut.read_data) == new_data


def test_lutram():
    run(
        "lutram",
        [RTL / "itch_pkg.vhd", RTL / "lutram.vhd"],
        __file__,
        parameters={"WIDTH": LUTRAM_WIDTH, "DEPTH": LUTRAM_DEPTH},
    )


if __name__ == "__main__":
    test_lutram()
