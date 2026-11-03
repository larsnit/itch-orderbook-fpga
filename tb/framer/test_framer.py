"""framer against the hardware model's tokens: every token with its fields and the cycle it
appears in, plus the drop and malformed pulses.

    python3.11 tb/framer/test_framer.py             (Questa)
    SIM=nvc python3.11 tb/framer/test_framer.py

The payload is driven as packet_parser passes it on: payload byte p in lane (p + 2) mod 8,
s_fcs_error at the frame's last beat or a few cycles later (padding after the IP packet),
and at least 5 idle cycles between frames (the next frame's header beats).
Needs data/vectors (python3.11 scripts/make_vectors.py -o data/vectors).
"""

import random
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402
from cocotb.types import LogicArray  # noqa: E402

from itch.config import Config  # noqa: E402
from itch.framing import DEFAULT_FEED, MOLD_HEADER, MOLD_HEADER_SIZE, build_frame, frame_payload, read_packet  # noqa: E402
from itch.generator import generator_config  # noqa: E402
from itch.hw_model import HardwareModel  # noqa: E402
from tb.common import structs, vectors  # noqa: E402
from tb.common.axis import is_high  # noqa: E402
from tb.common.runner import CHECKERS, RTL, WRAPPERS, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402

CLOCK_PS = 6400
TOKEN_LAYOUT = structs.layouts()["TOKEN_T"]
HEADER_BEATS_OF_NEXT_FRAME = 5   # the parser passes nothing on while the next frame's headers arrive
FCS_TOKEN_DELAY = 3
CYCLE_MASK = 0xFFFF


async def count_cycles(dut) -> None:
    """cycle_count = number of the current cycle (16 bits)."""
    cycle = 0
    dut.cycle_count.value = 0
    while True:
        await RisingEdge(dut.clk)
        cycle += 1
        dut.cycle_count.value = cycle & CYCLE_MASK


class PayloadDriver:
    def __init__(self, dut, idle_probability: float, seed: int) -> None:
        self.dut = dut
        self.idle_probability = idle_probability
        self.rng = random.Random(seed)
        self._idle()

    def _idle(self) -> None:
        self.dut.s_tdata.value = LogicArray("X" * 64)
        self.dut.s_tkeep.value = LogicArray("X" * 8)
        self.dut.s_tvalid.value = 0
        self.dut.s_tlast.value = 0
        self.dut.s_fcs_error.value = 0

    def _cycle(self) -> int:
        return self.dut.cycle_count.value.to_unsigned()

    async def send(self, payload: bytes, fcs_error: bool, padding: int) -> tuple[list[int], int | None]:
        """Drive one payload; returns the cycle of each beat and of the s_fcs_error pulse."""
        dut = self.dut
        lanes = bytes(2) + payload   # payload byte 0 in lane 2
        beat_cycles = []
        fcs_cycle = None
        for start in range(0, len(lanes), 8):
            chunk = lanes[start : start + 8]
            last = start + 8 >= len(lanes)
            while self.rng.random() < self.idle_probability:
                await FallingEdge(dut.clk)
                self._idle()
            await FallingEdge(dut.clk)
            dut.s_tdata.value = int.from_bytes(chunk.ljust(8, b"\x00"), "little")
            dut.s_tkeep.value = ((1 << len(chunk)) - 1) & (0xFC if start == 0 else 0xFF)
            dut.s_tvalid.value = 1
            dut.s_tlast.value = int(last)
            dut.s_fcs_error.value = int(last and fcs_error and padding == 0)
            beat_cycles.append(self._cycle())
            if last and fcs_error and padding == 0:
                fcs_cycle = self._cycle()
        for idle in range(padding + HEADER_BEATS_OF_NEXT_FRAME):
            await FallingEdge(dut.clk)
            self._idle()
            if fcs_error and idle == padding - 1:
                dut.s_fcs_error.value = 1
                fcs_cycle = self._cycle()
        return beat_cycles, fcs_cycle


def message_end_offsets(payload: bytes) -> list[int]:
    """Payload offset of the last byte of each complete message, in order."""
    parsed = read_packet(payload)
    if parsed is None:
        return []   # cut inside the header: no messages
    packet, _ = parsed
    ends, position = [], MOLD_HEADER_SIZE
    for message in packet.messages:
        ends.append(position + 2 + len(message) - 1)
        position += 2 + len(message)
    return ends


def beat_of(offset: int) -> int:
    return (offset + 2) // 8


def expected_tokens(new_tokens, payload: bytes, beat_cycles: list[int], fcs_cycle: int | None) -> list[dict]:
    """The model's tokens for one frame, with the cycle each must appear in."""
    ends = iter(message_end_offsets(payload))
    expected = []
    for token in new_tokens:
        fields = structs.token_fields(token)
        if token.kind == "MSG":
            last_byte_cycle = beat_cycles[beat_of(next(ends))]
            fields["t_arrival"] = last_byte_cycle & CYCLE_MASK
            fields["cycle"] = (last_byte_cycle + 1) & CYCLE_MASK
        elif token.kind == "HEARTBEAT":
            fields["cycle"] = (beat_cycles[beat_of(MOLD_HEADER_SIZE - 1)] + 1) & CYCLE_MASK
        else:
            fields["cycle"] = (fcs_cycle + FCS_TOKEN_DELAY) & CYCLE_MASK
        expected.append(fields)
    return expected


def describe(token: dict) -> str:
    kind = {0: "MSG", 1: "HEARTBEAT", 2: "FCS"}.get(token.get("kind"), "?")
    data = token.get("data", 0).to_bytes(structs.MSG_BYTES, "little")[: min(token.get("len", 0), 12)]
    return f"{kind} seq={token.get('seq')} len={token.get('len')} cycle={token.get('cycle')} data={data.hex()}..."


class OutputMonitor:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.tokens: list[dict] = []
        self.drops = 0
        self.malformed = 0

    async def run(self) -> None:
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            cycle = dut.cycle_count.value.to_unsigned()
            if is_high(dut.token_valid):
                fields = structs.unpack(TOKEN_LAYOUT, dut.token_vector.value.to_unsigned())
                fields["cycle"] = cycle
                self.tokens.append(fields)
            self.drops += is_high(dut.drop_pulse)
            self.malformed += is_high(dut.malformed_pulse)


async def check_frames(dut, frames: list[tuple[bytes, bool]], config: Config, name: str, idle_probability: float, seed: int) -> None:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    cocotb.start_soon(count_cycles(dut))
    driver = PayloadDriver(dut, idle_probability, seed)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    monitor = OutputMonitor(dut)
    cocotb.start_soon(monitor.run())

    rng = random.Random(seed)
    model = HardwareModel(config, taps=True)
    expected, drops, malformed = [], 0, 0
    for frame, fcs_error in frames:
        payload = frame_payload(frame, DEFAULT_FEED)
        if payload is None:
            continue   # the parser drops it: the framer never sees it
        tokens_before = len(model.taps.tokens)
        model.process_frame(frame, fcs_error)
        parsed = read_packet(payload)
        drops += parsed is None
        malformed += parsed is not None and not parsed[1]

        padding = rng.choice([0, 0, 1, 3]) if idle_probability else 0
        beat_cycles, fcs_cycle = await driver.send(payload, fcs_error, padding)
        expected += expected_tokens(model.taps.tokens[tokens_before:], payload, beat_cycles, fcs_cycle)

    await ClockCycles(dut.clk, 10)
    compare(expected, monitor.tokens, f"{name}: tokens", describe)
    assert monitor.drops == drops, f"{name}: {monitor.drops} drop pulses, expected {drops}"
    assert monitor.malformed == malformed, f"{name}: {monitor.malformed} malformed pulses, expected {malformed}"


def vector_frames() -> tuple[list[tuple[bytes, bool]], Config]:
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    loaded = [vectors.load(name) for name in names]
    frames = [(frame.data, frame.fcs_error) for vector in loaded for frame in vector.frames]
    return frames, loaded[0].config


def random_packets(rng: random.Random, count: int) -> list[tuple[bytes, bool]]:
    """MoldUDP64 packets of every shape the framer must handle: messages of 12-60 bytes
    (type Z, which the model ignores), heartbeats, end of session, too few blocks, bytes
    left over, a block shorter than 12 bytes, and frames cut at any byte."""
    frames, seq = [], 1
    for _ in range(count):
        messages = [b"Z" + rng.randbytes(rng.randint(11, 59)) for _ in range(rng.randint(1, 6))]
        blocks = [len(m).to_bytes(2, "big") + m for m in messages]
        announced = len(messages)
        shape = rng.random()
        if shape < 0.10:
            blocks, announced = [], rng.choice([0, 0xFFFF])
        elif shape < 0.15:
            blocks, announced = [rng.randbytes(rng.randint(1, 9))], rng.choice([0, 0xFFFF])
        elif shape < 0.25:
            announced += rng.randint(1, 3)
        elif shape < 0.35:
            blocks.append(rng.randbytes(rng.randint(1, 9)))
        elif shape < 0.45:
            short = rng.randint(0, 11)
            blocks.insert(rng.randint(0, len(blocks)), short.to_bytes(2, "big") + rng.randbytes(short))
        header = struct.pack(MOLD_HEADER, b"RANDOM    ", seq, announced)
        frame = build_frame(DEFAULT_FEED, header + b"".join(blocks))
        if rng.random() < 0.15:
            frame = frame[: rng.randint(43, len(frame) - 1)]
        frames.append((frame, rng.random() < 0.2))
        seq += len(messages)
    return frames


@cocotb.test()
async def vectors_back_to_back(dut):
    frames, config = vector_frames()
    await check_frames(dut, frames, config, "vectors back to back", idle_probability=0.0, seed=1)


@cocotb.test()
async def vectors_with_tvalid_gaps_and_padding(dut):
    frames, config = vector_frames()
    await check_frames(dut, frames, config, "vectors with gaps", idle_probability=0.2, seed=2)


@cocotb.test()
async def random_packets_of_every_shape(dut):
    frames = random_packets(random.Random(3), 400)
    await check_frames(dut, frames, generator_config(), "random packets", idle_probability=0.1, seed=3)


def test_framer():
    run(
        "framer_tb_top",
        [RTL / "itch_pkg.vhd", RTL / "framer.vhd", CHECKERS / "framer_chk.vhd", WRAPPERS / "framer_tb_top.vhd"],
        __file__,
    )


if __name__ == "__main__":
    test_framer()
