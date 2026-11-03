"""packet_parser against framing.frame_payload: for every frame, the payload passed on (or the
drop), the bad-FCS outputs, in order.

    python3.11 tb/packet_parser/test_packet_parser.py             (Questa)
    SIM=nvc python3.11 tb/packet_parser/test_packet_parser.py

Needs data/vectors (python3.11 scripts/make_vectors.py -o data/vectors). With ITCH_DAY_PCAP
set, the first ITCH_DAY_FRAMES frames (default 1,000,000) of that pcap run too.
"""

import ipaddress
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402

from itch.framing import DEFAULT_FEED, MoldPacket, build_frame, frame_payload  # noqa: E402
from itch.pcap import read_pcap  # noqa: E402
from tb.common import vectors  # noqa: E402
from tb.common.axis import AxisDriver, beat_bytes, is_high  # noqa: E402
from tb.common.runner import CHECKERS, RTL, WRAPPERS, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402

CLOCK_PS = 6400
DAY_FRAMES = int(os.environ.get("ITCH_DAY_FRAMES", 1_000_000))


def expected_outputs(frame: bytes, fcs_error: bool) -> list[tuple]:
    """What the parser must output for one frame, in order."""
    payload = frame_payload(frame, DEFAULT_FEED)
    outputs = [("drop",)] if payload is None else [("payload", payload)]
    if fcs_error:
        if payload is not None:
            outputs.append(("accepted_fcs_error",))
        outputs.append(("fcs_error",))
    return outputs


def describe(output: tuple) -> str:
    if output[0] == "payload":
        return f"payload {len(output[1])} bytes {output[1][:16].hex()}..."
    return output[0]


class OutputMonitor:
    """Everything the parser outputs, in order: ("payload", bytes) at m_tlast,
    ("accepted_fcs_error",), ("drop",), ("fcs_error",)."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.outputs: list[tuple] = []

    async def run(self) -> None:
        dut = self.dut
        payload = bytearray()
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.m_tvalid):
                payload += beat_bytes(dut.m_tdata.value.to_unsigned(), dut.m_tkeep.value.to_unsigned())
                if is_high(dut.m_tlast):
                    self.outputs.append(("payload", bytes(payload)))
                    payload = bytearray()
            if is_high(dut.m_fcs_error):
                self.outputs.append(("accepted_fcs_error",))
            if is_high(dut.drop_pulse):
                self.outputs.append(("drop",))
            if is_high(dut.fcs_pulse):
                self.outputs.append(("fcs_error",))


async def start(dut, idle_probability: float, seed: int) -> tuple[AxisDriver, OutputMonitor]:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.cfg_group_ip.value = int(ipaddress.IPv4Address(DEFAULT_FEED.group))
    dut.cfg_port.value = DEFAULT_FEED.dst_port
    driver = AxisDriver(dut, idle_probability=idle_probability, seed=seed)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    monitor = OutputMonitor(dut)
    cocotb.start_soon(monitor.run())
    return driver, monitor


async def check_frames(dut, frames: list[tuple[bytes, bool]], name: str, idle_probability: float, seed: int) -> None:
    driver, monitor = await start(dut, idle_probability, seed)
    rng = random.Random(seed)
    expected = []
    for data, fcs_error in frames:
        expected += expected_outputs(data, fcs_error)
        await driver.send(data, fcs_error)
        if idle_probability:
            await driver.idle(rng.choice([0, 0, 1, 3]))
    await driver.idle(1)
    await ClockCycles(dut.clk, 3)
    compare(expected, monitor.outputs, name, describe)


def all_vector_frames() -> list[tuple[bytes, bool]]:
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    return [(frame.data, frame.fcs_error) for name in names for frame in vectors.load(name).frames]


@cocotb.test()
async def vectors_back_to_back(dut):
    """Every vector frame, each frame right after the previous one, tvalid never low."""
    await check_frames(dut, all_vector_frames(), "vectors back to back", idle_probability=0.0, seed=1)


@cocotb.test()
async def vectors_with_tvalid_gaps(dut):
    """Every vector frame, tvalid low before 20% of the beats and between frames."""
    await check_frames(dut, all_vector_frames(), "vectors with tvalid gaps", idle_probability=0.2, seed=2)


@cocotb.test()
async def frame_cut_at_every_length(dut):
    """A feed frame cut after every byte count: dropped up to byte 42 (no payload byte), and
    the bytes that arrived after that. Then with 4, 12 and 20 bytes after the IP packet (an FCS
    the MAC didn't remove, padding): in the last payload beat, one and two beats beyond it.
    Every second one has a bad FCS."""
    packet = MoldPacket("SESSION001", 1000, (bytes(range(19)), bytes(range(36)), bytes(range(23))))
    frame = build_frame(DEFAULT_FEED, packet.to_bytes())
    frames = [(frame[:length], length % 2 == 0) for length in range(1, len(frame) + 1)]
    frames += [(frame + bytes([0xEE] * extra), extra != 12) for extra in (4, 12, 20)]
    await check_frames(dut, frames, "frame cut at every length", idle_probability=0.1, seed=3)


@cocotb.test()
async def udp_payload_shorter_than_the_moldudp64_header(dut):
    """Valid UDP packets with 0 to 21 payload bytes: dropped below 20 (no room for the
    MoldUDP64 header), passed on from 20."""
    frames = [(build_frame(DEFAULT_FEED, bytes(range(length))), False) for length in range(22)]
    await check_frames(dut, frames, "short UDP payloads", idle_probability=0.0, seed=5)


@cocotb.test(skip="ITCH_DAY_PCAP" not in os.environ)
async def day_sample(dut):
    """The first ITCH_DAY_FRAMES frames of the reference day's pcap."""
    frames = []
    with open(os.environ["ITCH_DAY_PCAP"], "rb") as f:
        for _, data in read_pcap(f):
            frames.append((data, False))
            if len(frames) == DAY_FRAMES:
                break
    await check_frames(dut, frames, "day sample", idle_probability=0.0, seed=4)


def test_packet_parser():
    run(
        "packet_parser_tb_top",
        [RTL / "packet_parser.vhd", CHECKERS / "packet_parser_chk.vhd", WRAPPERS / "packet_parser_tb_top.vhd"],
        __file__,
    )


if __name__ == "__main__":
    test_packet_parser()
