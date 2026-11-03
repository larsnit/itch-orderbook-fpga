"""xgmii_tx: frames in as AXI4-Stream, XGMII out, checked with tb/common/xgmii.decode (the
reference xgmii_rx is tested against): every frame comes out with its FCS and a terminate,
starts in lane 0, at least 12 idle bytes between frames.

    python3.11 tb/xgmii_tx/test_xgmii_tx.py             (Questa)
    SIM=nvc python3.11 tb/xgmii_tx/test_xgmii_tx.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402
from cocotb.types import LogicArray  # noqa: E402

from tb.common import vectors  # noqa: E402
from tb.common.axis import frame_beats, is_high  # noqa: E402
from tb.common.runner import RTL, run  # noqa: E402
from tb.common.xgmii import IDLE, START, TERMINATE, Word, decode, with_fcs  # noqa: E402

CLOCK_PS = 6400
MIN_GAP = 12      # idle bytes between terminate and start
MAX_GAP = 19      # what this design gives at most (2 idle words + the rest of the terminate word)


class Wire:
    """Every XGMII word the DUT puts out, from the first cycle after reset."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.words: list[Word] = []

    async def run(self) -> None:
        while True:
            await RisingEdge(self.dut.clk)
            self.words.append((self.dut.tx_mii_d.value.to_unsigned(), self.dut.tx_mii_c.value.to_unsigned()))


async def start(dut) -> Wire:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    idle(dut)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    wire = Wire(dut)
    cocotb.start_soon(wire.run())
    return wire


def idle(dut) -> None:
    dut.s_tvalid.value = 0
    dut.s_tlast.value = 0
    dut.s_tdata.value = LogicArray("X" * 64)
    dut.s_tkeep.value = LogicArray("X" * 8)


async def send(dut, frame: bytes, stop_after: int | None = None) -> None:
    """One frame, each beat held until tready; with stop_after, tvalid drops after that many
    beats (an underrun) and the rest is not sent."""
    for index, beat in enumerate(frame_beats(frame)):
        if stop_after is not None and index == stop_after:
            await FallingEdge(dut.clk)
            idle(dut)
            await ClockCycles(dut.clk, 2)
            return
        await FallingEdge(dut.clk)
        dut.s_tdata.value = beat.data
        dut.s_tkeep.value = beat.keep
        dut.s_tlast.value = int(beat.last)
        dut.s_tvalid.value = 1
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.s_tready):
                break


async def drain(dut, cycles: int = 8) -> None:
    await FallingEdge(dut.clk)
    idle(dut)
    await ClockCycles(dut.clk, cycles)


def check_wire(words: list[Word], name: str, back_to_back: bool = True) -> None:
    """Starts only in lane 0; between a terminate and the next start at least MIN_GAP idles,
    and with frames offered back to back at most MAX_GAP."""
    lanes = [(data >> 8 * i & 0xFF, bool(control >> i & 1)) for data, control in words for i in range(8)]
    last_end = None
    for position, (byte, is_control) in enumerate(lanes):
        if is_control and byte == START:
            assert position % 8 == 0, f"{name}: start in lane {position % 8}"
            if last_end is not None:
                gap = position - last_end - 1
                assert MIN_GAP <= gap and (gap <= MAX_GAP or not back_to_back), f"{name}: {gap} idle bytes before the start at word {position // 8}"
                assert all(lanes[p] == (IDLE, True) for p in range(last_end + 1, position)), f"{name}: gap not all idle"
        if is_control and byte not in (IDLE, START):
            last_end = position


def check_frames(words: list[Word], frames: list[bytes], bad: set[int], name: str) -> None:
    decoded = [(b"".join(beat.data for beat in frame), frame[-1].user) for frame in decode(words)]
    assert len(decoded) == len(frames), f"{name}: {len(decoded)} frames on the wire, {len(frames)} sent"
    for index, ((got, got_bad), frame) in enumerate(zip(decoded, frames)):
        if index in bad:
            assert got_bad, f"{name}: frame {index} cut, but good on the wire"
        else:
            assert not got_bad, f"{name}: frame {index} ({len(frame)} bytes) bad on the wire"
            assert got == with_fcs(frame), f"{name}: frame {index} bytes differ"


@cocotb.test()
async def every_length_back_to_back(dut):
    """60 to 140 bytes (every end lane, FCS in one word or split over two), the next frame
    offered at once: the gap is the minimum the design allows."""
    wire = await start(dut)
    rng = random.Random(1)
    frames = [bytes(rng.getrandbits(8) for _ in range(length)) for length in range(60, 141)]
    for frame in frames:
        await send(dut, frame)
    await drain(dut)
    check_wire(wire.words, "back to back")
    check_frames(wire.words, frames, set(), "back to back")


@cocotb.test()
async def gaps_and_long_frames(dut):
    """Random idle between frames, up to 1514 bytes."""
    wire = await start(dut)
    rng = random.Random(2)
    frames = [bytes(rng.getrandbits(8) for _ in range(rng.choice([60, 61, 64, 100, 1000, 1514]))) for _ in range(40)]
    for frame in frames:
        await send(dut, frame)
        if rng.random() < 0.5:
            await FallingEdge(dut.clk)
            idle(dut)
            await ClockCycles(dut.clk, rng.randint(1, 6))
    await drain(dut)
    check_wire(wire.words, "gaps", back_to_back=False)
    check_frames(wire.words, frames, set(), "gaps")


@cocotb.test()
async def feed_vectors(dut):
    """The ITCH vector frames (padded to 60 bytes, as a NIC would)."""
    wire = await start(dut)
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    frames = [frame.data.ljust(60, b"\x00") for name in names[:6] for frame in vectors.load(name).frames]
    for frame in frames:
        await send(dut, frame)
    await drain(dut)
    check_wire(wire.words, "vectors")
    check_frames(wire.words, frames, set(), "vectors")


@cocotb.test()
async def underrun_cuts_the_frame(dut):
    """tvalid drops inside a frame: that frame ends with an error character (bad on the wire),
    the frames before and after are good."""
    wire = await start(dut)
    rng = random.Random(3)
    frames = [bytes(rng.getrandbits(8) for _ in range(80)) for _ in range(3)]
    await send(dut, frames[0])
    await send(dut, frames[1], stop_after=4)
    await send(dut, frames[2])
    await drain(dut)
    decoded = decode(wire.words)
    assert len(decoded) == 3 and decoded[1][-1].user and not decoded[0][-1].user and not decoded[2][-1].user
    assert b"".join(beat.data for beat in decoded[1]) == frames[1][:32], "cut frame: wrong bytes before the cut"
    check_frames(wire.words, [frames[0], frames[1], frames[2]], {1}, "underrun")


@cocotb.test()
async def ready_only_while_sending(dut):
    """tready is 0 in the cycle of the start word and during the tail and gap, 1 for every
    data beat: a frame of n beats is taken in exactly n cycles once it has started."""
    await start(dut)
    rng = random.Random(4)
    for length in (60, 64, 68, 72):
        frame = bytes(rng.getrandbits(8) for _ in range(length))
        beats = frame_beats(frame)
        await FallingEdge(dut.clk)
        dut.s_tdata.value, dut.s_tkeep.value = beats[0].data, beats[0].keep
        dut.s_tlast.value, dut.s_tvalid.value = int(beats[0].last), 1
        await RisingEdge(dut.clk)
        assert not is_high(dut.s_tready), "ready in the start word's cycle"
        for index, beat in enumerate(beats):
            await FallingEdge(dut.clk)
            dut.s_tdata.value, dut.s_tkeep.value = beat.data, beat.keep
            dut.s_tlast.value = int(beat.last)
            await RisingEdge(dut.clk)
            assert is_high(dut.s_tready), f"{length} bytes: not ready for beat {index}"
        await FallingEdge(dut.clk)
        idle(dut)
        await RisingEdge(dut.clk)
        assert not is_high(dut.s_tready), "ready right after the last beat"
        await ClockCycles(dut.clk, 4)


def test_xgmii_tx():
    run("xgmii_tx", [RTL / "xgmii_tx.vhd"], __file__)


if __name__ == "__main__":
    test_xgmii_tx()
