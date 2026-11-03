"""64-bit XGMII as the 10GBASE-R PCS delivers it: an encoder for test streams and the reference
decoder that xgmii_rx must match.

A word is (data, control): byte i in data bits 8i+7..8i, control bit i = 1 for a control
character. A frame is: start character in lane 0 or 4 (lanes 0-3 of a lane-4 start are idle),
6 x 0x55, 0xD5, the frame bytes, the FCS (CRC-32, little-endian), then the terminate character
and idles up to the end of the word. Terminate and start are never in the same word.
"""

import random
import zlib
from dataclasses import dataclass

from cocotb.triggers import FallingEdge

IDLE = 0x07
START = 0xFB
TERMINATE = 0xFD
ERROR = 0xFE
SEQUENCE = 0x9C
PREAMBLE = bytes([0x55] * 6 + [0xD5])
RESIDUE = 0x2144DF1C   # zlib.crc32 over a frame and its FCS
LATENCY = 2            # a beat leaves 2 cycles after the word holding its first byte

Word = tuple[int, int]


def with_fcs(frame: bytes) -> bytes:
    return frame + zlib.crc32(frame).to_bytes(4, "little")


@dataclass(frozen=True, slots=True)
class Beat:
    data: bytes   # the valid bytes, lane 0 first
    last: bool
    user: bool
    cycle: int    # the word index the beat must leave at: its first byte's word + LATENCY


class Encoder:
    """Builds a word stream lane by lane."""

    def __init__(self) -> None:
        self.lanes: list[tuple[int, bool]] = []   # (byte, is_control)

    def control(self, char: int) -> None:
        self.lanes.append((char, True))

    def data(self, raw: bytes) -> None:
        self.lanes += [(b, False) for b in raw]

    def idle_to_word_end(self) -> None:
        while len(self.lanes) % 8:
            self.control(IDLE)

    def idle_words(self, count: int) -> None:
        self.idle_to_word_end()
        for _ in range(8 * count):
            self.control(IDLE)

    def sequence_word(self) -> None:
        """Two local-fault ordered sets (control 9C, data 00 00 01), as between frames."""
        self.idle_to_word_end()
        for _ in range(2):
            self.control(SEQUENCE)
            self.data(bytes([0, 0, 1]))

    def start(self, lane: int) -> None:
        """Start character in lane 0 or 4 of a new word (not the word of a terminate)."""
        self.idle_to_word_end()
        if lane == 4:
            for _ in range(4):
                self.control(IDLE)
        self.control(START)
        self.data(PREAMBLE[:6])
        self.data(PREAMBLE[6:])

    def frame(self, raw: bytes, lane: int = 0, end: int = TERMINATE) -> None:
        """raw already holds the FCS (or a wrong one); end is the control character after it."""
        self.start(lane)
        self.data(raw)
        self.control(end)
        self.idle_to_word_end()

    def words(self, trailing_idle_words: int = 3) -> list[Word]:
        self.idle_words(trailing_idle_words)
        words = []
        for at in range(0, len(self.lanes), 8):
            chunk = self.lanes[at : at + 8]
            data = sum(byte << 8 * i for i, (byte, _) in enumerate(chunk))
            control = sum(1 << i for i, (_, is_control) in enumerate(chunk) if is_control)
            words.append((data, control))
        return words


def decode(words: list[Word]) -> list[list[Beat]]:
    """The reference: every frame xgmii_rx must put out, as beats. A frame runs from 7 bytes
    after a start character in lane 0 or 4 to the first control character; it is bad unless
    that character is a terminate and the CRC over its bytes gives the residue. A frame with no
    bytes gives nothing."""
    lanes = [(data >> 8 * i & 0xFF, bool(control >> i & 1), w, i) for w, (data, control) in enumerate(words) for i in range(8)]
    frames = []
    position = 0
    while position < len(lanes):
        byte, is_control, _, lane = lanes[position]
        if not (is_control and byte == START and lane in (0, 4)):
            position += 1
            continue
        position += 1 + len(PREAMBLE)
        content = []
        while position < len(lanes) and not lanes[position][1]:
            content.append(lanes[position])
            position += 1
        if position >= len(lanes):
            break    # stream ended inside the frame: not checked
        end = lanes[position][0]
        raw = bytes(byte for byte, *_ in content)
        bad = end != TERMINATE or zlib.crc32(raw) != RESIDUE
        beats = []
        for at in range(0, len(content), 8):
            chunk = content[at : at + 8]
            last = at + 8 >= len(content)
            beats.append(Beat(bytes(b for b, *_ in chunk), last, bad and last, chunk[0][2] + LATENCY))
        if beats:
            frames.append(beats)
        # a start character here is a new frame: the loop finds it
    return frames


class XgmiiDriver:
    """Drives frames into rx_mii_d/rx_mii_c: start lane 0 or 4 at random, FCS appended
    (one bit flipped for fcs_error), the next frame starts in the word after the terminate.
    Idle words between frames with idle()."""

    IDLE_WORD = (int.from_bytes(bytes([IDLE] * 8), "little"), 0xFF)

    def __init__(self, dut, seed: int = 0) -> None:
        self.clk = dut.clk
        self.data = dut.rx_mii_d
        self.control = dut.rx_mii_c
        self.rng = random.Random(seed)
        self.data.value, self.control.value = self.IDLE_WORD

    async def send(self, frame: bytes, fcs_error: bool = False) -> None:
        raw = bytearray(with_fcs(frame))
        if fcs_error:
            raw[-1] ^= 0x01
        encoder = Encoder()
        encoder.frame(bytes(raw), self.rng.choice((0, 4)))
        for data, control in encoder.words(trailing_idle_words=0):
            await FallingEdge(self.clk)
            self.data.value, self.control.value = data, control

    async def idle(self, cycles: int) -> None:
        for _ in range(cycles):
            await FallingEdge(self.clk)
            self.data.value, self.control.value = self.IDLE_WORD


def random_frame(rng: random.Random, length: int) -> bytes:
    return with_fcs(bytes(rng.getrandbits(8) for _ in range(length)))
