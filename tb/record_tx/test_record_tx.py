"""record_fifo and record_tx with two clocks of slightly different frequency, as the core clock
(rx_clk_out_0) and the transmit clock (tx_mii_clk_0) are on the board.

    python3.11 tb/record_tx/test_record_tx.py             (Questa)
    SIM=nvc python3.11 tb/record_tx/test_record_tx.py

record_fifo: order kept, a write while full dropped and flagged, level never more than what is
there (the RTL asserts no read while empty). record_tx: the wire decoded with
tb/common/xgmii.decode, every frame taken apart with itch/record_frames.parse: addresses,
lengths, IP checksum, frame counter, records in input order (a status record before a record
of the same cycle), dropped records counted in the header and on records_dropped.
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402

from itch import record_frames  # noqa: E402
from tb.common.axis import is_high  # noqa: E402
from tb.common.runner import RTL, run  # noqa: E402
from tb.common.xgmii import decode  # noqa: E402

CORE_PS = 6400
FIFO_BITS = 4     # small FIFO for the FIFO tests: 16 records
TX_FIFO_BITS = 6  # record_tx: 64 records, more than a frame holds
MAX_RECORDS = 32

# ---------------------------------------------------------------- record_fifo


async def fifo_start(dut, read_ps: int) -> None:
    cocotb.start_soon(Clock(dut.w_clk, CORE_PS, "ps").start())
    cocotb.start_soon(Clock(dut.r_clk, read_ps, "ps").start())
    dut.w_valid.value = 0
    dut.w_data.value = 0
    dut.r_read.value = 0
    dut.w_rst.value = 1
    dut.r_rst.value = 1
    await ClockCycles(dut.w_clk, 4)
    await FallingEdge(dut.w_clk)
    dut.w_rst.value = 0
    await FallingEdge(dut.r_clk)
    dut.r_rst.value = 0


class FifoSides:
    """Writes with a probability per cycle, reads with another; logs what was kept and read."""

    def __init__(self, dut, rng: random.Random) -> None:
        self.dut = dut
        self.rng = rng
        self.attempts: list[int] = []
        self.dropped: list[bool] = []
        self.read: list[int] = []

    async def write(self, count: int, probability: float) -> None:
        dut = self.dut
        sent = 0
        while sent < count:
            await FallingEdge(dut.w_clk)
            if self.rng.random() < probability:
                value = self.rng.getrandbits(256)
                dut.w_valid.value, dut.w_data.value = 1, value
                self.attempts.append(value)
                sent += 1
            else:
                dut.w_valid.value = 0
        await FallingEdge(dut.w_clk)
        dut.w_valid.value = 0

    async def watch_drops(self) -> None:
        dut = self.dut
        pending = 0
        while True:
            await RisingEdge(dut.w_clk)
            if pending:      # w_dropped answers the attempt registered one edge earlier
                self.dropped.append(is_high(dut.w_dropped))
            pending = is_high(dut.w_valid)

    async def read_all(self, probability: float, stop_after_idle: int = 200) -> None:
        dut = self.dut
        idle = 0
        reading = False      # r_read as driven for the coming edge
        taken = False        # r_read was 1 at the edge before
        while idle < stop_after_idle or taken or reading:
            await RisingEdge(dut.r_clk)
            if taken:        # r_data holds the entry taken one edge earlier
                self.read.append(dut.r_data.value.to_unsigned())
            taken = reading
            await FallingEdge(dut.r_clk)
            level = dut.r_level.value.to_unsigned()
            reading = level > int(taken) and self.rng.random() < probability   # level lags the read just taken
            dut.r_read.value = int(reading)
            idle = 0 if level else idle + 1
        dut.r_read.value = 0

    def kept(self) -> list[int]:
        return [value for value, dropped in zip(self.attempts, self.dropped) if not dropped]


async def fifo_random_traffic(dut, read_ps: int, seed: int) -> None:
    await fifo_start(dut, read_ps)
    sides = FifoSides(dut, random.Random(seed))
    watcher = cocotb.start_soon(sides.watch_drops())
    reader = cocotb.start_soon(sides.read_all(0.5))
    await sides.write(600, 0.4)
    await reader
    watcher.cancel()
    assert len(sides.dropped) == len(sides.attempts)
    assert sides.read == sides.kept(), f"read {read_ps} ps: data or order wrong"
    assert sides.read, "nothing read"


@cocotb.test()
async def fifo_random_traffic_slower_reader(dut):
    """Random writes and reads, read clock slower than the write clock."""
    await fifo_random_traffic(dut, 6430, 1)


@cocotb.test()
async def fifo_random_traffic_faster_reader(dut):
    """Random writes and reads, read clock faster than the write clock."""
    await fifo_random_traffic(dut, 6371, 2)


@cocotb.test()
async def fifo_full_drops_and_flags(dut):
    """No reads: exactly DEPTH writes are kept, the rest flagged dropped; then all read back."""
    await fifo_start(dut, 6430)
    sides = FifoSides(dut, random.Random(3))
    watcher = cocotb.start_soon(sides.watch_drops())
    await sides.write(2 ** FIFO_BITS + 7, 1.0)
    await ClockCycles(dut.w_clk, 3)
    assert sides.dropped.count(True) == 7 and sides.dropped[: 2 ** FIFO_BITS] == [False] * 2 ** FIFO_BITS
    await ClockCycles(dut.r_clk, 4)
    assert dut.r_level.value.to_unsigned() == 2 ** FIFO_BITS
    await sides.read_all(1.0, stop_after_idle=20)
    watcher.cancel()
    assert sides.read == sides.kept()


def test_record_fifo():
    run("record_fifo", [RTL / "record_fifo.vhd"], __file__, parameters={"ADDR_BITS": FIFO_BITS},
        build_name="record_fifo", env={"COCOTB_TEST_FILTER": "fifo_"})


# ---------------------------------------------------------------- record_tx

ADDRESSES = record_frames.Addresses(
    dst_mac=bytes.fromhex("0A0B0C0D0E0F"), src_mac=bytes.fromhex("020000000002"),
    dst_ip="192.168.7.9", src_ip="10.0.0.2", dst_port=5001, src_port=4999,
)


def configure(dut, addresses: record_frames.Addresses, enable: bool) -> None:
    dut.dst_mac.value = int.from_bytes(addresses.dst_mac, "big")
    dut.src_mac.value = int.from_bytes(addresses.src_mac, "big")
    dut.dst_ip.value = int.from_bytes(bytes(map(int, addresses.dst_ip.split("."))), "big")
    dut.src_ip.value = int.from_bytes(bytes(map(int, addresses.src_ip.split("."))), "big")
    dut.dst_port.value = addresses.dst_port
    dut.src_port.value = addresses.src_port
    dut.tx_enable.value = int(enable)


class Wire:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.words: list[tuple[int, int]] = []

    async def run(self) -> None:
        while True:
            await RisingEdge(self.dut.tx_clk)
            self.words.append((self.dut.tx_mii_d.value.to_unsigned(), self.dut.tx_mii_c.value.to_unsigned()))

    def frames(self) -> list[record_frames.RecordFrame]:
        result = []
        for beats in decode(self.words):
            raw = b"".join(beat.data for beat in beats)
            assert not beats[-1].user, "bad frame on the wire"
            frame = record_frames.parse(raw[:-4], ADDRESSES.dst_port)
            assert frame is not None, f"not a record frame: {raw[:60].hex()}"
            assert raw[:6] == ADDRESSES.dst_mac and raw[6:12] == ADDRESSES.src_mac, "MAC addresses"
            assert raw[26:30] == bytes([10, 0, 0, 2]) and raw[30:34] == bytes([192, 168, 7, 9]), "IP addresses"
            assert raw[34:36] == (4999).to_bytes(2, "big"), "source port"
            result.append(frame)
        return result


async def tx_start(dut, tx_ps: int, enable: bool = True) -> Wire:
    cocotb.start_soon(Clock(dut.clk, CORE_PS, "ps").start())
    cocotb.start_soon(Clock(dut.tx_clk, tx_ps, "ps").start())
    configure(dut, ADDRESSES, enable)
    dut.rec_valid.value = 0
    dut.evt_valid.value = 0
    dut.rec_data.value = 0
    dut.evt_data.value = 0
    dut.rst.value = 1
    dut.tx_rst.value = 1
    await ClockCycles(dut.clk, 6)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    dut.tx_rst.value = 0
    await ClockCycles(dut.clk, 8)
    wire = Wire(dut)
    cocotb.start_soon(wire.run())
    return wire


def record_bytes(rng: random.Random, status: bool) -> bytes:
    raw = bytearray(rng.randbytes(32))
    raw[0] = 0xFF if status else raw[0] % 8
    return bytes(raw)


async def drive(dut, rng: random.Random, count: int, min_gap: int, max_gap: int, status_share: float) -> list[bytes]:
    """count book records, at least min_gap cycles apart; sometimes a status record in the same
    cycle (or alone). Returns everything in the order it must come out."""
    order = []
    for _ in range(count):
        await FallingEdge(dut.clk)
        record = record_bytes(rng, False)
        dut.rec_valid.value, dut.rec_data.value = 1, int.from_bytes(record, "little")
        if rng.random() < status_share:
            status = record_bytes(rng, True)
            dut.evt_valid.value, dut.evt_data.value = 1, int.from_bytes(status, "little")
            order.append(status)
        order.append(record)
        await FallingEdge(dut.clk)
        dut.rec_valid.value = 0
        dut.evt_valid.value = 0
        for _ in range(rng.randint(min_gap, max_gap) - 1):
            await FallingEdge(dut.clk)
    return order


def as_bytes(records) -> list[bytes]:
    return [record.to_bytes() for record in records]


async def records_out_in_order(dut, tx_ps: int, seed: int) -> None:
    wire = await tx_start(dut, tx_ps)
    rng = random.Random(seed)
    order = await drive(dut, rng, 60, 20, 60, 0.2)       # sparse: one record per frame
    order += await drive(dut, rng, 200, 4, 9, 0.05)      # dense enough to batch, not to overflow
    await ClockCycles(dut.tx_clk, 600)
    frames = wire.frames()
    assert [frame.frame_counter for frame in frames] == list(range(len(frames)))
    assert all(frame.dropped == 0 for frame in frames)
    assert all(1 <= len(frame.records) <= MAX_RECORDS for frame in frames)
    got = [record for frame in frames for record in as_bytes(frame.records)]
    assert got == order, f"{tx_ps} ps: records differ (got {len(got)}, sent {len(order)})"
    assert max(len(frame.records) for frame in frames) > 1, "never batched"
    assert dut.records_dropped.value.to_unsigned() == 0


@cocotb.test()
async def records_out_slower_transmit_clock(dut):
    """Sparse and dense records with status records: every record once, in order; frame
    counters 0, 1, 2, ...; nothing dropped."""
    await records_out_in_order(dut, 6430, 1)


@cocotb.test()
async def records_out_faster_transmit_clock(dut):
    """The same with the transmit clock faster than the core clock."""
    await records_out_in_order(dut, 6371, 2)


@cocotb.test()
async def overflow_is_counted(dut):
    """Records every 2 cycles, faster than 10G can carry: what arrives is in order, and
    arrived + dropped = sent; the header's dropped count never goes down."""
    wire = await tx_start(dut, 6430)
    rng = random.Random(3)
    order = await drive(dut, rng, 400, 2, 2, 0.0)
    await ClockCycles(dut.tx_clk, 2000)
    frames = wire.frames()
    got = [record for frame in frames for record in as_bytes(frame.records)]
    dropped = dut.records_dropped.value.to_unsigned()
    assert dropped > 0, "no overflow: test too slow"
    assert len(got) + dropped == len(order), f"{len(got)} arrived + {dropped} dropped != {len(order)} sent"
    position = 0
    for record in got:               # an in-order subsequence
        position = order.index(record, position) + 1
    assert max(len(frame.records) for frame in frames) == MAX_RECORDS, "frames never full"
    counts = [frame.dropped for frame in frames]
    assert counts == sorted(counts) and counts[-1] <= dropped
    assert [frame.frame_counter for frame in frames] == list(range(len(frames)))


@cocotb.test()
async def disabled_sends_nothing(dut):
    """tx_enable = 0: no frames, no drops; enabled again, the new records go out."""
    wire = await tx_start(dut, 6430, enable=False)
    rng = random.Random(4)
    await drive(dut, rng, 50, 3, 6, 0.1)
    await ClockCycles(dut.tx_clk, 300)
    assert not decode(wire.words) and dut.records_dropped.value.to_unsigned() == 0
    dut.tx_enable.value = 1
    await ClockCycles(dut.clk, 4)
    order = await drive(dut, rng, 20, 3, 6, 0.1)
    await ClockCycles(dut.tx_clk, 400)
    got = [record for frame in wire.frames() for record in as_bytes(frame.records)]
    assert got == order


def test_record_tx():
    run("record_tx",
        [RTL / "record_fifo.vhd", RTL / "record_packer.vhd", RTL / "xgmii_tx.vhd", RTL / "record_tx.vhd"],
        __file__, parameters={"ADDR_BITS": TX_FIFO_BITS}, build_name="record_tx",
        env={"COCOTB_TEST_FILTER": "records_out|overflow|disabled"})


if __name__ == "__main__":
    test_record_fifo()
    test_record_tx()
