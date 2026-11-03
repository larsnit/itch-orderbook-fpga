"""64-bit AXI4-Stream without tready, as the MAC delivers it: driver and monitors.

Timing convention for every testbench: inputs are driven after the falling edge, outputs are
sampled right after the rising edge (the values the edge registers).

Byte j of a beat is in tdata bits 8j+7 DOWNTO 8j and tkeep bit j says it is valid. A frame
starts in lane 0; only its last beat (tlast) may have fewer than 8 bytes. tuser = 1 on the
last beat means a bad FCS.
"""

import random
from dataclasses import dataclass

from cocotb.triggers import FallingEdge, RisingEdge
from cocotb.types import LogicArray

BEAT_BYTES = 8


def is_high(signal) -> bool:
    """A one-bit signal is '1' (X, U and Z count as low)."""
    return str(signal.value) == "1"


@dataclass(frozen=True, slots=True)
class Beat:
    data: int
    keep: int
    last: bool
    user: bool


def frame_beats(frame: bytes, fcs_error: bool = False) -> list[Beat]:
    """A frame as the MAC delivers it, beat by beat."""
    beats = []
    for start in range(0, len(frame), BEAT_BYTES):
        chunk = frame[start : start + BEAT_BYTES]
        last = start + BEAT_BYTES >= len(frame)
        beats.append(Beat(int.from_bytes(chunk, "little"), (1 << len(chunk)) - 1, last, fcs_error and last))
    return beats


def beat_bytes(data: int, keep: int) -> bytes:
    """The valid bytes of a beat, lane 0 first (any keep pattern)."""
    raw = data.to_bytes(BEAT_BYTES, "little")
    return bytes(raw[lane] for lane in range(BEAT_BYTES) if keep >> lane & 1)


class AxisDriver:
    """Drives frames into s_tdata/s_tkeep/s_tvalid/s_tlast/s_tuser. With idle_probability > 0,
    tvalid drops for a cycle before a beat with that probability, also inside a frame. While
    tvalid is 0, tdata and tkeep are X, so a DUT that uses them without tvalid shows it.

    send() leaves the frame's last beat on the bus, so the next send() follows back to back;
    call idle() for a gap, and always after the last frame."""

    def __init__(self, dut, prefix: str = "s_", idle_probability: float = 0.0, seed: int = 0) -> None:
        self.clk = dut.clk
        self.tdata = getattr(dut, prefix + "tdata")
        self.tkeep = getattr(dut, prefix + "tkeep")
        self.tvalid = getattr(dut, prefix + "tvalid")
        self.tlast = getattr(dut, prefix + "tlast")
        self.tuser = getattr(dut, prefix + "tuser")
        self.idle_probability = idle_probability
        self.rng = random.Random(seed)
        self._drive_idle()

    async def send(self, frame: bytes, fcs_error: bool = False) -> None:
        for beat in frame_beats(frame, fcs_error):
            while self.rng.random() < self.idle_probability:
                await FallingEdge(self.clk)
                self._drive_idle()
            await FallingEdge(self.clk)
            self.tdata.value = beat.data
            self.tkeep.value = beat.keep
            self.tvalid.value = 1
            self.tlast.value = int(beat.last)
            self.tuser.value = int(beat.user)

    async def idle(self, cycles: int) -> None:
        for _ in range(cycles):
            await FallingEdge(self.clk)
            self._drive_idle()

    def _drive_idle(self) -> None:
        self.tdata.value = LogicArray("X" * len(self.tdata))
        self.tkeep.value = LogicArray("X" * len(self.tkeep))
        self.tvalid.value = 0
        self.tlast.value = 0
        self.tuser.value = 0


class AxisMonitor:
    """Collects frames from m_tdata/m_tkeep/m_tvalid/m_tlast: frames[i] = (bytes, tuser at
    tlast, or None without tuser). Bytes are taken from the lanes tkeep marks, whatever the
    pattern (the packet parser's first payload beat starts in lane 2)."""

    def __init__(self, dut, prefix: str = "m_", with_user: bool = True) -> None:
        self.clk = dut.clk
        self.tdata = getattr(dut, prefix + "tdata")
        self.tkeep = getattr(dut, prefix + "tkeep")
        self.tvalid = getattr(dut, prefix + "tvalid")
        self.tlast = getattr(dut, prefix + "tlast")
        self.tuser = getattr(dut, prefix + "tuser") if with_user else None
        self.frames: list[tuple[bytes, bool | None]] = []

    async def run(self) -> None:
        current = bytearray()
        while True:
            await RisingEdge(self.clk)
            if not is_high(self.tvalid):
                continue
            current += beat_bytes(self.tdata.value.to_unsigned(), self.tkeep.value.to_unsigned())
            if is_high(self.tlast):
                user = is_high(self.tuser) if self.tuser is not None else None
                self.frames.append((bytes(current), user))
                current = bytearray()


class StreamMonitor:
    """Collects (cycle, tdata) of every cycle with tvalid = 1, for streams without tkeep and
    tlast: tokens, operations, records. The cycle counts rising edges since run() started."""

    def __init__(self, clk, tdata, tvalid) -> None:
        self.clk = clk
        self.tdata = tdata
        self.tvalid = tvalid
        self.items: list[tuple[int, int]] = []

    async def run(self) -> None:
        cycle = 0
        while True:
            await RisingEdge(self.clk)
            cycle += 1
            if is_high(self.tvalid):
                self.items.append((cycle, self.tdata.value.to_unsigned()))

    @property
    def values(self) -> list[int]:
        return [value for _, value in self.items]
