"""itch_top: the register map over AXI4-Lite, and the core configured and read only over the bus.

    python3.11 tb/top/test_itch_top.py             (Questa)
    SIM=nvc python3.11 tb/top/test_itch_top.py

itch_top has no record ports, so it is the toplevel itself (no wrapper). Frames go in as
XGMII (tb/common/xgmii.py), through xgmii_rx, starting in lane 0 or 4. The AXI4-Lite master
(tb/common/axil.py) inserts random gaps between the channels and checks that a response stays
stable until it is taken.
Needs data/vectors (python3.11 scripts/make_vectors.py -o data/vectors).
"""

import ipaddress
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402

from itch.config import Config  # noqa: E402
from itch.framing import DEFAULT_FEED  # noqa: E402
from itch.hw_model import COUNTERS, HardwareModel  # noqa: E402
from itch.record import Record  # noqa: E402
from tb.common import vectors  # noqa: E402
from tb.common.axil import OKAY, SLVERR, AxiLiteMaster  # noqa: E402
from tb.common.axis import is_high  # noqa: E402
from tb.common.runner import RTL, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402
from tb.common.taps import record_from_bus  # noqa: E402
from tb.common.xgmii import XgmiiDriver, decode  # noqa: E402
from itch import record_frames  # noqa: E402

CLOCK_PS = 6400
VERSION = 0x2026_1020   # itch_top's default generic
NUM_BOOKS = 8

CONTROL, STATUS, VERSION_REG, GROUP_IP, PORT = 0x000, 0x004, 0x008, 0x010, 0x014
TX_CONTROL, TX_DST_MAC, TX_SRC_MAC, TX_DST_IP, TX_SRC_IP, TX_PORTS = 0x018, 0x020, 0x028, 0x030, 0x034, 0x038
RECORDS_DROPPED = 0x0C0
TICKERS, BASES, COUNTER_REGS = 0x040, 0x080, 0x100
UNMAPPED = [0x00C, 0x01C, 0x03C, 0x0C4, 0x0FC, 0x140, 0x1FC, 0x800, 0xFFC]
READ_ONLY = [STATUS, VERSION_REG, RECORDS_DROPPED] + [COUNTER_REGS + 4 * i for i in range(len(COUNTERS))]
# record output registers: reset value and the bits that hold
TX_REGISTERS = {
    TX_CONTROL: (0, 0x1), TX_DST_MAC: (0xFFFF, 0xFFFF), TX_DST_MAC + 4: (0xFFFF_FFFF, 0xFFFF_FFFF),
    TX_SRC_MAC: (0x0200, 0xFFFF), TX_SRC_MAC + 4: (0x0000_0002, 0xFFFF_FFFF),
    TX_DST_IP: (0xFFFF_FFFF, 0xFFFF_FFFF), TX_SRC_IP: (0x0A00_0002, 0xFFFF_FFFF), TX_PORTS: (0x1388_1388, 0xFFFF_FFFF),
}
TX_CLOCK_PS = 6413       # the transmit clock runs a little apart from the core clock
DRAIN_CYCLES = 30


def ticker_words(ticker: str) -> tuple[int, int]:
    raw = ticker.ljust(8).encode()
    return int.from_bytes(raw[:4], "big"), int.from_bytes(raw[4:], "big")


class Records:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.records: list[Record] = []
        self.events: list[Record] = []
        self.frame_ends = 0

    async def run(self) -> None:
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.m_rec_tvalid):
                self.records.append(record_from_bus(dut.m_rec_tdata.value.to_unsigned()))
            if is_high(dut.m_evt_tvalid):
                self.events.append(record_from_bus(dut.m_evt_tdata.value.to_unsigned()))
            if is_high(dut.rx_frame_end):
                self.frame_ends += 1


class TxWire:
    """The record output's XGMII words, on the transmit clock."""

    def __init__(self, dut, port: int) -> None:
        self.dut = dut
        self.port = port
        self.words: list[tuple[int, int]] = []

    async def run(self) -> None:
        while True:
            await RisingEdge(self.dut.tx_clk)
            self.words.append((self.dut.tx_mii_d.value.to_unsigned(), self.dut.tx_mii_c.value.to_unsigned()))

    def records(self) -> tuple[list[Record], list[Record], list[record_frames.RecordFrame]]:
        """Book records and status records from all record frames so far, and the frames."""
        frames = []
        for beats in decode(self.words):
            raw = b"".join(beat.data for beat in beats)
            assert not beats[-1].user, "bad record frame on the wire"
            frame = record_frames.parse(raw[:-4], self.port)
            assert frame is not None, "not a record frame"
            frames.append(frame)
        everything = [record for frame in frames for record in frame.records]
        return ([r for r in everything if r.book_id != 0xFF], [r for r in everything if r.book_id == 0xFF], frames)


async def start(dut, seed: int = 0) -> tuple[AxiLiteMaster, XgmiiDriver, Records]:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    cocotb.start_soon(Clock(dut.tx_clk, TX_CLOCK_PS, "ps").start())
    dut.tx_rst.value = 1
    bus = AxiLiteMaster(dut, seed=seed)
    driver = XgmiiDriver(dut, seed)
    records = Records(dut)
    cocotb.start_soon(records.run())
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    dut.tx_rst.value = 0
    return bus, driver, records


async def read_ok(bus: AxiLiteMaster, address: int) -> int:
    data, response = await bus.read(address)
    assert response == OKAY, f"read 0x{address:03X}: response {response}"
    return data


async def write_ok(bus: AxiLiteMaster, address: int, data: int, strobe: int = 0xF) -> None:
    response = await bus.write(address, data, strobe)
    assert response == OKAY, f"write 0x{address:03X}: response {response}"


async def wait_not_busy(bus: AxiLiteMaster) -> None:
    for _ in range(20000):
        if not await read_ok(bus, STATUS) & 1:
            return
    raise AssertionError("STATUS.busy never went low")


async def configure(bus: AxiLiteMaster, config: Config) -> None:
    await write_ok(bus, GROUP_IP, int(ipaddress.IPv4Address(config.feed.group)))
    await write_ok(bus, PORT, config.feed.dst_port)
    for book in range(NUM_BOOKS):
        ticker = config.tickers[book] if book < len(config.tickers) else ""
        high, low = ticker_words(ticker)
        await write_ok(bus, TICKERS + 8 * book, high)
        await write_ok(bus, TICKERS + 8 * book + 4, low)
        await write_ok(bus, BASES + 4 * book, config.base_cents(book) if book < len(config.tickers) else 100)


async def counters(bus: AxiLiteMaster) -> dict[str, int]:
    return {name: await read_ok(bus, COUNTER_REGS + 4 * i) for i, name in enumerate(COUNTERS)}


async def run_vector(dut, bus: AxiLiteMaster, driver: XgmiiDriver, records: Records, vector: vectors.Vector, name: str) -> None:
    """Soft reset over the bus, the frames, then records, counters and STATUS against the model."""
    await write_ok(bus, CONTROL, 1)
    await wait_not_busy(bus)
    assert await counters(bus) == dict.fromkeys(COUNTERS, 0), f"{name}: counters not cleared by the soft reset"
    records_before, events_before = len(records.records), len(records.events)
    frame_ends_before = records.frame_ends

    model = HardwareModel(vector.config)
    for frame in vector.frames:
        model.process_frame(frame.data, frame.fcs_error)
        await driver.send(frame.data, frame.fcs_error)
    await driver.idle(DRAIN_CYCLES)

    assert records.frame_ends - frame_ends_before == len(vector.frames), f"{name}: rx_frame_end not once per frame"
    compare(vector.records, records.records[records_before:], f"{name}: records")
    compare(vector.events, records.events[events_before:], f"{name}: events")
    assert await counters(bus) == model.counters, f"{name}: counters over the bus differ from the model"
    status = await read_ok(bus, STATUS)
    stale = sum(bit << book for book, bit in enumerate(model.book_stale))
    assert status == (stale << 8 | int(model.stale_all) << 1), f"{name}: STATUS 0x{status:08X}, expected stale {stale:08b}, all {model.stale_all}"


@cocotb.test()
async def register_map(dut):
    bus, _, _ = await start(dut, seed=1)
    rng = random.Random(2)

    # reset values: the default feed, tickers that match nothing, bases at the minimum
    assert await read_ok(bus, VERSION_REG) == VERSION
    assert await read_ok(bus, GROUP_IP) == int(ipaddress.IPv4Address(DEFAULT_FEED.group))
    assert await read_ok(bus, PORT) == DEFAULT_FEED.dst_port
    for book in range(NUM_BOOKS):
        assert await read_ok(bus, TICKERS + 8 * book) == 0x2020_2020
        assert await read_ok(bus, TICKERS + 8 * book + 4) == 0x2020_2020
        assert await read_ok(bus, BASES + 4 * book) == 100
    assert await read_ok(bus, STATUS) & 1, "busy after reset"
    assert await read_ok(bus, CONTROL) == 0
    for address, (reset_value, _) in TX_REGISTERS.items():
        assert await read_ok(bus, address) == reset_value, f"0x{address:03X} reset value"
    assert await read_ok(bus, RECORDS_DROPPED) == 0
    await wait_not_busy(bus)
    assert await counters(bus) == dict.fromkeys(COUNTERS, 0)

    # every RW register holds what is written; PORT keeps 16 bits
    values = {GROUP_IP: rng.getrandbits(32), PORT: rng.getrandbits(32)}
    for book in range(NUM_BOOKS):
        values[TICKERS + 8 * book] = rng.getrandbits(32)
        values[TICKERS + 8 * book + 4] = rng.getrandbits(32)
        values[BASES + 4 * book] = rng.getrandbits(32)
    for address in TX_REGISTERS:
        values[address] = rng.getrandbits(32)
    for address, value in values.items():
        await write_ok(bus, address, value)
    for address, value in values.items():
        want = value & 0xFFFF if address == PORT else value & TX_REGISTERS.get(address, (0, 0xFFFF_FFFF))[1]
        assert await read_ok(bus, address) == want, f"0x{address:03X} reads back wrong"

    # byte strobes: only the strobed bytes change, in every kind of RW register
    for address in (GROUP_IP, TICKERS + 8 * 5, TICKERS + 8 * 5 + 4, BASES + 4 * 3, TX_DST_MAC + 4, TX_PORTS):
        for strobe in range(16):
            before = await read_ok(bus, address)
            data = rng.getrandbits(32)
            await write_ok(bus, address, data, strobe)
            mask = sum(0xFF << 8 * byte for byte in range(4) if strobe >> byte & 1)
            want = (before & ~mask | data & mask) & 0xFFFF_FFFF
            assert await read_ok(bus, address) == want, f"0x{address:03X} strobe {strobe:04b}"

    # read-only and unmapped addresses: SLVERR, nothing changes
    snapshot = {address: await read_ok(bus, address) for address in values}
    for address in READ_ONLY + UNMAPPED:
        assert await bus.write(address, rng.getrandbits(32)) == SLVERR, f"write 0x{address:03X} not refused"
    for address in UNMAPPED:
        assert (await bus.read(address))[1] == SLVERR, f"read 0x{address:03X} not refused"
    assert await read_ok(bus, VERSION_REG) == VERSION
    for address, value in snapshot.items():
        assert await read_ok(bus, address) == value, f"0x{address:03X} changed by a refused write"
    assert await counters(bus) == dict.fromkeys(COUNTERS, 0)


async def overlapping_writes(dut, first: tuple[int, int], second: tuple[int, int], hold: int) -> list[int]:
    """Write 1 is answered, but its response is not taken for hold cycles; meanwhile write 2's
    address and data are offered all the time. The slave must not take write 2 before write 1's
    response is gone, or must at least not lose it. Returns both responses."""
    responses = []
    await FallingEdge(dut.clk)
    dut.s_axi_awaddr.value, dut.s_axi_wdata.value = first
    dut.s_axi_awvalid.value = dut.s_axi_wvalid.value = 1
    dut.s_axi_wstrb.value = 0xF
    dut.s_axi_bready.value = 0
    address_done = data_done = False
    while not (address_done and data_done):
        await RisingEdge(dut.clk)
        address_done |= is_high(dut.s_axi_awready)
        data_done |= is_high(dut.s_axi_wready)
        await FallingEdge(dut.clk)
        dut.s_axi_awvalid.value = int(not address_done)
        dut.s_axi_wvalid.value = int(not data_done)
    # write 2 offered at once, response 1 held back
    dut.s_axi_awaddr.value, dut.s_axi_wdata.value = second
    dut.s_axi_awvalid.value = dut.s_axi_wvalid.value = 1
    address_done = data_done = False
    cycle = 0
    while len(responses) < 2:
        dut.s_axi_bready.value = int(cycle >= hold)
        await RisingEdge(dut.clk)
        if is_high(dut.s_axi_bvalid) and is_high(dut.s_axi_bready):
            responses.append(dut.s_axi_bresp.value.to_unsigned())
        address_done |= is_high(dut.s_axi_awvalid) and is_high(dut.s_axi_awready)
        data_done |= is_high(dut.s_axi_wvalid) and is_high(dut.s_axi_wready)
        await FallingEdge(dut.clk)
        dut.s_axi_awvalid.value = int(not address_done)
        dut.s_axi_wvalid.value = int(not data_done)
        cycle += 1
        assert cycle < 100, "write 2 never answered"
    dut.s_axi_bready.value = 0
    return responses


@cocotb.test()
async def writes_while_a_response_waits(dut):
    """A master may offer the next write before it takes the previous response."""
    bus, _, _ = await start(dut, seed=4)
    for hold in (0, 1, 3, 6):
        first, second = (BASES, 1000 + hold), (BASES + 4, 2000 + hold)
        assert await overlapping_writes(dut, first, second, hold) == [OKAY, OKAY]
        assert await read_ok(bus, BASES) == first[1], f"hold {hold}: write 1 lost"
        assert await read_ok(bus, BASES + 4) == second[1], f"hold {hold}: write 2 lost"


@cocotb.test()
async def core_over_the_bus(dut):
    """Configure over the bus, soft reset, run vectors; records out, counters and STATUS read
    over the bus. The configuration survives each soft reset."""
    bus, driver, records = await start(dut, seed=3)
    await wait_not_busy(bus)
    names = [name for name in ("clean", "gap", "bad_fcs", "book_anomalies", "heartbeats") if name in vectors.case_names()]
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    loaded = [vectors.load(name) for name in names]
    await configure(bus, loaded[0].config)
    for vector in loaded:
        await run_vector(dut, bus, driver, records, vector, vector.name)
    high, low = ticker_words(loaded[0].config.tickers[0])
    assert await read_ok(bus, TICKERS) == high and await read_ok(bus, TICKERS + 4) == low, "configuration lost"


@cocotb.test()
async def records_over_ethernet(dut):
    """Record output on: every vector's records and status records come back in UDP frames on
    the transmit side, in order, with consecutive frame counters and nothing dropped. The
    addresses come from the registers."""
    bus, driver, records = await start(dut, seed=5)
    wire = TxWire(dut, port=0x138A)
    cocotb.start_soon(wire.run())
    await wait_not_busy(bus)
    names = [name for name in ("clean", "gap", "bad_fcs", "back_to_back") if name in vectors.case_names()]
    loaded = [vectors.load(name) for name in names]
    await configure(bus, loaded[0].config)
    await write_ok(bus, TX_DST_IP, 0xC0A8_0109)      # 192.168.1.9
    await write_ok(bus, TX_PORTS, 0x1389_138A)       # from 5001 to 5002
    await write_ok(bus, TX_CONTROL, 1)
    want_records, want_events = [], []
    for vector in loaded:
        await run_vector(dut, bus, driver, records, vector, vector.name)
        want_records += vector.records
        want_events += vector.events
    await ClockCycles(dut.tx_clk, 300)
    got_records, got_events, frames = wire.records()
    compare(want_records, got_records, "records over Ethernet")
    compare(want_events, got_events, "status records over Ethernet")
    assert [frame.frame_counter for frame in frames] == list(range(len(frames)))
    assert all(frame.dropped == 0 for frame in frames) and await read_ok(bus, RECORDS_DROPPED) == 0
    raw = b"".join(beat.data for beat in decode(wire.words)[0])
    assert raw[30:34] == bytes([192, 168, 1, 9]) and raw[34:38] == bytes.fromhex("1389138A"), "addresses from the registers"


def test_itch_top():
    run(
        "itch_top",
        [
            RTL / "itch_pkg.vhd", RTL / "itch_hash_pkg.vhd", RTL / "ram_sdp.vhd", RTL / "lutram.vhd",
            RTL / "counters.vhd", RTL / "packet_parser.vhd", RTL / "framer.vhd", RTL / "h3_hash.vhd",
            RTL / "decoder.vhd", RTL / "price_unit.vhd", RTL / "ot_bank.vhd", RTL / "ot_stash.vhd",
            RTL / "order_table.vhd", RTL / "bitmap_search.vhd", RTL / "ladder_side.vhd", RTL / "book_engine.vhd",
            RTL / "event_delay.vhd", RTL / "output_stage.vhd", RTL / "itch_core.vhd", RTL / "regs_axil.vhd",
            RTL / "xgmii_rx.vhd", RTL / "record_fifo.vhd", RTL / "record_packer.vhd", RTL / "xgmii_tx.vhd",
            RTL / "record_tx.vhd", RTL / "itch_top.vhd",
        ],
        __file__,
    )


if __name__ == "__main__":
    test_itch_top()
