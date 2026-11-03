"""itch_core end to end: MAC frames in, book records and status records out, against the
vectors' expected files and the hardware model's counters and stale bits.

    python3.11 tb/core/test_itch_core.py             (Questa)
    SIM=nvc python3.11 tb/core/test_itch_core.py

Every vector runs from reset, its frames back to back and with random gaps. Besides the
records themselves the test checks the order of the two channels: merged by cycle (a status
record before a book record of the same cycle), sequence numbers never go down. The checker
asserts each record's latency (13 cycles after the message's last byte, U 14).
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
from itch.hw_model import COUNTERS, HardwareModel  # noqa: E402
from itch.record import Record  # noqa: E402
from tb.common import vectors  # noqa: E402
from tb.common.axis import AxisDriver, is_high  # noqa: E402
from tb.common.runner import CHECKERS, RTL, WRAPPERS, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402
from tb.common.taps import record_from_bus  # noqa: E402

CLOCK_PS = 6400
SWEEP_CYCLES = 1 << 13
DRAIN_CYCLES = 30    # after the last frame: longer than the 14-cycle latency
MAX = 0xFFFF_FFFF


class Monitor:
    """Book and status records with the cycle they leave in."""

    def __init__(self, dut) -> None:
        self.dut = dut
        self.cycle = 0
        self.records: list[tuple[int, Record]] = []
        self.events: list[tuple[int, Record]] = []

    async def run(self) -> None:
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.m_rec_tvalid):
                self.records.append((self.cycle, record_from_bus(dut.m_rec_tdata.value.to_unsigned())))
            if is_high(dut.m_evt_tvalid):
                self.events.append((self.cycle, record_from_bus(dut.m_evt_tdata.value.to_unsigned())))
            self.cycle += 1


def configure(dut, config: Config) -> None:
    dut.cfg_group_ip.value = int(ipaddress.IPv4Address(config.feed.group))
    dut.cfg_port.value = config.feed.dst_port
    dut.cfg_ticker.value = sum(int.from_bytes(t.ljust(8).encode(), "big") << 64 * i for i, t in enumerate(config.tickers))
    dut.cfg_base.value = sum(config.base_cents(i) << 32 * i for i in range(len(config.tickers)))


async def start(dut, config: Config) -> tuple[Monitor, AxisDriver]:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    configure(dut, config)
    dut.rst.value = 0
    driver = AxisDriver(dut)
    monitor = Monitor(dut)
    cocotb.start_soon(monitor.run())
    await ClockCycles(dut.clk, 2)
    return monitor, driver


async def reset(dut, wait: bool = True) -> None:
    await FallingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    if wait:
        busy_cycles = 0
        while is_high(dut.busy):
            busy_cycles += 1
            await FallingEdge(dut.clk)
        assert busy_cycles == SWEEP_CYCLES, f"busy for {busy_cycles} cycles, expected {SWEEP_CYCLES}"


def counters_of(dut) -> dict[str, int]:
    flat = dut.counts.value.to_unsigned()
    return {name: flat >> 32 * i & MAX for i, name in enumerate(COUNTERS)}


def check_order(records: list[tuple[int, Record]], events: list[tuple[int, Record]], name: str) -> None:
    """Merged by cycle, events first within a cycle: sequence numbers never go down."""
    merged = sorted([(cycle, 0, r.seq) for cycle, r in events] + [(cycle, 1, r.seq) for cycle, r in records])
    for (cycle_a, _, seq_a), (cycle_b, _, seq_b) in zip(merged, merged[1:]):
        assert seq_b >= seq_a, f"{name}: sequence {seq_b} in cycle {cycle_b} after {seq_a} in cycle {cycle_a}"


async def run_frames(dut, monitor: Monitor, driver: AxisDriver, frames: list[tuple[bytes, bool]],
                     model: HardwareModel, name: str, rng: random.Random | None,
                     expected_records: list[Record] | None = None,
                     expected_events: list[Record] | None = None) -> None:
    """Send frames (bad FCS flag each), compare records, events, counters and stale bits."""
    records_before, events_before = len(monitor.records), len(monitor.events)
    counters_before = counters_of(dut)
    model_records, model_events = [], []
    counters_model_before = dict(model.counters)
    for frame, fcs_error in frames:
        more_records, more_events = model.process_frame(frame, fcs_error)
        model_records += more_records
        model_events += more_events
        await driver.send(frame, fcs_error)
        await driver.idle(rng.choice([0, 0, 1, 2, 5]) if rng else 0)
    await driver.idle(DRAIN_CYCLES)

    records = monitor.records[records_before:]
    events = monitor.events[events_before:]
    compare(expected_records if expected_records is not None else model_records,
            [record for _, record in records], f"{name}: records")
    compare(expected_events if expected_events is not None else model_events,
            [event for _, event in events], f"{name}: events")
    check_order(records, events, name)

    counters_after = counters_of(dut)
    for counter in COUNTERS:
        got = counters_after[counter] - counters_before[counter]
        want = model.counters[counter] - counters_model_before[counter]
        assert got == want, f"{name}: {counter} counted {got}, expected {want}"
    stale = sum(bit << book for book, bit in enumerate(model.book_stale))
    assert dut.book_stale.value.to_unsigned() == stale, f"{name}: book_stale {dut.book_stale.value}, expected {stale:08b}"
    assert is_high(dut.stale_all) == model.stale_all, f"{name}: stale_all {dut.stale_all.value}, expected {model.stale_all}"


@cocotb.test()
async def vectors_back_to_back_and_with_gaps(dut):
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    loaded = [vectors.load(name) for name in names]
    monitor, driver = await start(dut, loaded[0].config)
    rng = random.Random(1)
    for vector in loaded:
        for spacing in (None, rng):
            configure(dut, vector.config)
            await reset(dut)
            frames = [(frame.data, frame.fcs_error) for frame in vector.frames]
            label = f"{vector.name} ({'gaps' if spacing else 'back to back'})"
            await run_frames(dut, monitor, driver, frames, HardwareModel(vector.config), label, spacing,
                             vector.records, vector.events)
            for counter, value in vector.counters.items():   # the vector's own list (13 of the 16)
                assert counters_of(dut)[counter] == value, f"{label}: {counter} = {counters_of(dut)[counter]}, vector says {value}"


@cocotb.test()
async def frames_during_the_sweep_are_dropped_whole(dut):
    """Frames whose first beat comes while busy are dropped before the parser and counted once
    each, also the one still arriving when busy ends; the frames after it are processed."""
    vector = vectors.load("clean")
    monitor, driver = await start(dut, vector.config)
    await reset(dut)
    await reset(dut, wait=False)
    frames = [(frame.data, frame.fcs_error) for frame in vector.frames]

    # frames back to back during the sweep, the last one straddling its end
    sent_while_busy = 0
    cycles_left = SWEEP_CYCLES - 2
    index = 0
    while cycles_left > 0:
        frame, fcs_error = frames[index % len(frames)]
        beats = (len(frame) + 7) // 8
        await driver.send(frame, fcs_error)
        sent_while_busy += 1
        cycles_left -= beats
        index += 1
    await driver.idle(1)
    assert not is_high(dut.busy), "busy longer than expected"
    counters = counters_of(dut)
    assert counters["dropped_frames"] == sent_while_busy, f"{counters['dropped_frames']} dropped, {sent_while_busy} sent while busy"
    assert all(counters[name] == 0 for name in COUNTERS if name != "dropped_frames"), f"counted while busy: {counters}"
    assert not monitor.records and not monitor.events

    model = HardwareModel(vector.config)
    model.counters["dropped_frames"] = sent_while_busy
    await run_frames(dut, monitor, driver, frames, model, "after the sweep", None, vector.records, vector.events)


def test_itch_core():
    run(
        "itch_core_tb_top",
        [
            RTL / "itch_pkg.vhd", RTL / "itch_hash_pkg.vhd", RTL / "ram_sdp.vhd", RTL / "lutram.vhd",
            RTL / "counters.vhd", RTL / "packet_parser.vhd", RTL / "framer.vhd", RTL / "h3_hash.vhd",
            RTL / "decoder.vhd", RTL / "price_unit.vhd", RTL / "ot_bank.vhd", RTL / "ot_stash.vhd",
            RTL / "order_table.vhd", RTL / "bitmap_search.vhd", RTL / "ladder_side.vhd", RTL / "book_engine.vhd",
            RTL / "event_delay.vhd", RTL / "output_stage.vhd", RTL / "itch_core.vhd",
            CHECKERS / "itch_core_chk.vhd", WRAPPERS / "itch_core_tb_top.vhd",
        ],
        __file__,
    )


if __name__ == "__main__":
    test_itch_core()
