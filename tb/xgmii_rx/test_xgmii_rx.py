"""xgmii_rx against tb/common/xgmii.decode: every beat (bytes, tlast, tuser) and the cycle it
leaves in (2 after the word holding its first byte). Clean frames are also checked against the
frames that were encoded, so the reference decoder is checked too.

    python3.11 tb/xgmii_rx/test_xgmii_rx.py             (Questa)
    SIM=nvc python3.11 tb/xgmii_rx/test_xgmii_rx.py

The vector test needs data/vectors (python3.11 scripts/make_vectors.py -o data/vectors).
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402

from tb.common import vectors  # noqa: E402
from tb.common.axis import beat_bytes, is_high  # noqa: E402
from tb.common.runner import RTL, run  # noqa: E402
from tb.common.xgmii import ERROR, IDLE, START, Beat, Encoder, Word, decode, random_frame, with_fcs  # noqa: E402

CLOCK_PS = 6400
IDLE_WORD = (int.from_bytes(bytes([IDLE] * 8), "little"), 0xFF)


async def start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.rx_mii_d.value, dut.rx_mii_c.value = IDLE_WORD
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0


async def send(dut, words: list[Word]) -> list[Beat]:
    """Drive the words, one per cycle, and collect the beats with the cycle (word index) they
    leave in. Ends with idle words, longer than the latency."""
    beats: list[Beat] = []

    async def monitor() -> None:
        cycle = 0
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.m_tvalid):
                keep = dut.m_tkeep.value.to_unsigned()
                last, user = is_high(dut.m_tlast), is_high(dut.m_tuser)
                assert keep and keep & (keep + 1) == 0, f"cycle {cycle}: tkeep {keep:08b} not contiguous from lane 0"
                assert last or keep == 0xFF, f"cycle {cycle}: tkeep {keep:08b} on a beat without tlast"
                assert last or not user, f"cycle {cycle}: tuser without tlast"
                beats.append(Beat(beat_bytes(dut.m_tdata.value.to_unsigned(), keep), last, user, cycle))
            cycle += 1

    for word in words + [IDLE_WORD] * 4:
        await FallingEdge(dut.clk)
        dut.rx_mii_d.value, dut.rx_mii_c.value = word
        if word is words[0]:
            task = cocotb.start_soon(monitor())
    await FallingEdge(dut.clk)
    task.cancel()
    return beats


def describe(beat: Beat) -> str:
    return f"cycle {beat.cycle}: {beat.data.hex()}{' last' if beat.last else ''}{' bad' if beat.user else ''}"


def check(got: list[Beat], words: list[Word], name: str, frames: list[bytes] | None = None) -> None:
    """got against the reference decoder; with frames given, the decoded frames must be exactly
    those (all good)."""
    expected = [beat for frame in decode(words) for beat in frame]
    for index, (want, have) in enumerate(zip(expected, got)):
        assert want == have, f"{name}: beat {index}: expected {describe(want)}, got {describe(have)}"
    assert len(got) == len(expected), f"{name}: {len(got)} beats, expected {len(expected)}"
    if frames is not None:
        decoded = [(b"".join(beat.data for beat in frame), frame[-1].user) for frame in decode(words)]
        assert decoded == [(frame, False) for frame in frames], f"{name}: reference decoder disagrees with the encoded frames"


@cocotb.test()
async def every_length_both_start_lanes(dut):
    """Frames of 1 to 40 bytes plus FCS (every end lane, also ends exactly at a word boundary)
    and a few long ones, starting in lane 0 and 4, back to back (start in the word after the
    terminate) and with idle words between."""
    await start(dut)
    rng = random.Random(1)
    for lane in (0, 4):
        for gap in (0, 2):
            encoder = Encoder()
            frames = [random_frame(rng, length) for length in list(range(1, 41)) + [60, 64, 100, 1500]]
            for frame in frames:
                encoder.frame(frame, lane)
                encoder.idle_words(gap)
            words = encoder.words()
            check(await send(dut, words), words, f"lane {lane}, gap {gap}", frames)


@cocotb.test()
async def lanes_alternate(dut):
    """Every pair of start lanes in a row, every end lane, back to back."""
    await start(dut)
    rng = random.Random(2)
    encoder = Encoder()
    frames = []
    for length in range(60, 76):
        for lane in (0, 4, 4, 0):
            frames.append(random_frame(rng, length))
            encoder.frame(frames[-1], lane)
    words = encoder.words()
    check(await send(dut, words), words, "alternating lanes", frames)


@cocotb.test()
async def feed_vectors(dut):
    """The ITCH vector frames as they come off the fibre: all good, bytes = frame + FCS."""
    await start(dut)
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    rng = random.Random(3)
    for name in names:
        encoder = Encoder()
        frames = []
        for frame in vectors.load(name).frames:
            frames.append(with_fcs(frame.data))
            encoder.frame(frames[-1], rng.choice((0, 4)))
        words = encoder.words()
        check(await send(dut, words), words, name, frames)


@cocotb.test()
async def bad_frames(dut):
    """Bad FCS (one bit), an error character in every lane, a frame cut by a new start, a
    frame with no bytes, ordered sets between frames; the good frames around them come out."""
    await start(dut)
    rng = random.Random(4)
    for lane in (0, 4):
        encoder = Encoder()
        for length in range(60, 68):
            frame = bytearray(random_frame(rng, length))
            frame[rng.randrange(len(frame))] ^= 1 << rng.randrange(8)
            encoder.frame(bytes(frame), lane)                          # bad FCS
            encoder.frame(random_frame(rng, length), lane)
        for length in range(60, 68):                                   # error character after every lane
            encoder.frame(random_frame(rng, length)[:length], lane, end=ERROR)
            encoder.sequence_word()
            encoder.frame(random_frame(rng, length), lane)
        for length in (1, 9, 63):                                      # cut by a new start in lane 0
            encoder.start(lane)
            encoder.data(bytes(rng.getrandbits(8) for _ in range(length)))
            while len(encoder.lanes) % 8:
                encoder.data(b"\x00")
            encoder.frame(random_frame(rng, 64), 0)
        encoder.frame(b"", lane)                                       # no bytes
        encoder.frame(random_frame(rng, 64), lane)
        encoder.frame(b"", lane, end=ERROR)
        encoder.frame(random_frame(rng, 64), lane)
        words = encoder.words()
        check(await send(dut, words), words, f"bad frames, lane {lane}")


@cocotb.test()
async def start_inside_a_frame(dut):
    """A start character in lane 0 right after the last data word: the old frame is bad and
    ends there, the new one is received."""
    await start(dut)
    rng = random.Random(5)
    for lane in (0, 4):
        encoder = Encoder()
        encoder.start(lane)
        encoder.data(random_frame(rng, 60))
        while len(encoder.lanes) % 8:
            encoder.data(b"\x00")
        encoder.frame(random_frame(rng, 64), 0)
        words = encoder.words()
        got = await send(dut, words)
        check(got, words, f"start inside a frame, lane {lane}")
        frames = decode(words)
        assert len(frames) == 2 and frames[0][-1].user and not frames[1][-1].user, f"lane {lane}: {frames}"


@cocotb.test()
async def random_streams(dut):
    """Random mixes of good, bad-FCS, error-cut and empty frames, lanes and gaps."""
    await start(dut)
    rng = random.Random(6)
    for round_ in range(4):
        encoder = Encoder()
        for _ in range(150):
            frame = random_frame(rng, rng.choice([rng.randint(1, 20), rng.randint(42, 200)]))
            kind = rng.random()
            if kind < 0.1:
                frame = bytes([frame[0] ^ 0x80]) + frame[1:]
            end = ERROR if 0.1 <= kind < 0.15 else rng.choice([0xFD] * 20 + [IDLE, START])
            if end == START:
                end = ERROR   # a start in the terminate's place is tested above
            encoder.frame(frame, rng.choice((0, 4)), end)
            if rng.random() < 0.3:
                encoder.idle_words(rng.randint(1, 3))
            if rng.random() < 0.1:
                encoder.sequence_word()
        words = encoder.words()
        check(await send(dut, words), words, f"random round {round_}")


@cocotb.test()
async def reset_inside_a_frame(dut):
    """A reset in the middle of a frame: the rest of that frame is not put out, the next one is."""
    await start(dut)
    rng = random.Random(7)
    encoder = Encoder()
    encoder.frame(random_frame(rng, 200), 0)
    encoder.frame(random_frame(rng, 64), 4)
    words = encoder.words()
    seen = []

    async def collect() -> None:
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.m_tvalid):
                seen.append(is_high(dut.m_tlast))

    task = cocotb.start_soon(collect())
    for index, word in enumerate(words):
        await FallingEdge(dut.clk)
        dut.rx_mii_d.value, dut.rx_mii_c.value = word
        dut.rst.value = 1 if index in (10, 11) else 0
    await ClockCycles(dut.clk, 6)
    task.cancel()
    second = len(decode(words)[1])
    assert seen.count(True) == 1 and seen[-second:] == [False] * (second - 1) + [True], f"beats after the reset: {seen}"


def test_xgmii_rx():
    run("xgmii_rx", [RTL / "xgmii_rx.vhd"], __file__)


if __name__ == "__main__":
    test_xgmii_rx()
