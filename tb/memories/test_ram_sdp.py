"""ram_sdp: read data comes exactly LATENCY cycles after the address; a write is visible to
a read one cycle later. Runs with LATENCY = 2 (the package's RAM_LATENCY) and 3, so the
register chain is proven to follow the generic.

    python3.11 tb/memories/test_ram_sdp.py             (Questa)
    SIM=nvc python3.11 tb/memories/test_ram_sdp.py
"""

import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import FallingEdge, RisingEdge  # noqa: E402

from tb.common.runner import RTL, run  # noqa: E402

CLOCK_PS = 6400  # 156.25 MHz
WIDTH = 72
DEPTH = 512
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
async def ram_read_data_comes_latency_cycles_after_the_address(dut):
    latency = int(os.environ["RAM_LATENCY_UNDER_TEST"])
    rng = random.Random(latency)
    await start(dut)
    contents = await fill(dut, DEPTH, WIDTH, rng)

    presented = []  # read address of each cycle
    for cycle in range(CYCLES):
        await FallingEdge(dut.clk)
        address = rng.randrange(DEPTH)
        while presented and address == presented[-1]:
            address = rng.randrange(DEPTH)
        dut.read_address.value = address
        presented.append(address)
        await RisingEdge(dut.clk)
        if cycle >= latency:
            expected = contents[presented[cycle - latency]]
            assert value_of(dut.read_data) == expected, f"cycle {cycle}: data of address {presented[cycle - latency]} expected"


@cocotb.test()
async def ram_write_is_visible_to_a_read_one_cycle_later(dut):
    """A write in cycle c, a read of the same address in cycle c + 1: the read sees the new
    data. (A read in cycle c itself is undefined; the write history covers it.)"""
    latency = int(os.environ["RAM_LATENCY_UNDER_TEST"])
    rng = random.Random(10 + latency)
    await start(dut)
    contents = await fill(dut, DEPTH, WIDTH, rng)

    for _ in range(50):
        address = rng.randrange(DEPTH)
        new_data = rng.getrandbits(WIDTH)
        await FallingEdge(dut.clk)
        dut.write_enable.value = 1
        dut.write_address.value = address
        dut.write_data.value = new_data
        dut.read_address.value = (address + 1) % DEPTH
        await FallingEdge(dut.clk)
        dut.write_enable.value = 0
        dut.read_address.value = address
        contents[address] = new_data
        for _ in range(latency + 1):  # sampled at the end of the cycle latency cycles after the read
            await RisingEdge(dut.clk)
        assert value_of(dut.read_data) == new_data


def test_ram_sdp():
    for latency in (2, 3):
        run(
            "ram_sdp",
            [RTL / "itch_pkg.vhd", RTL / "ram_sdp.vhd"],
            __file__,
            parameters={"WIDTH": WIDTH, "DEPTH": DEPTH, "LATENCY": latency},
            build_name=f"ram_sdp_latency_{latency}",
            env={"RAM_LATENCY_UNDER_TEST": str(latency)},
        )


if __name__ == "__main__":
    test_ram_sdp()
