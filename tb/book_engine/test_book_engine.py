"""book_engine against the hardware model's book engine: every top (fields and the cycle it
appears in, 7 cycles after its order-table result).

    python3.11 tb/book_engine/test_book_engine.py             (Questa)
    SIM=nvc python3.11 tb/book_engine/test_book_engine.py

The inputs are order-table results. They come from the hardware model's order table, fed with
operations, so they are consistent (a removal only takes shares that are there); the same model
then gives the expected top for each. Each test starts from reset, which also checks the
clearing sweep.
Needs data/vectors (python3.11 scripts/make_vectors.py -o data/vectors) for the vector test.
"""

import random
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402

from itch.config import Config  # noqa: E402
from itch.generator import generator_config  # noqa: E402
from itch.hashing import buckets  # noqa: E402
from itch.hw_model import HardwareModel, Op  # noqa: E402
from tb.common import structs, vectors  # noqa: E402
from tb.common.axis import is_high  # noqa: E402
from tb.common.runner import CHECKERS, RTL, WRAPPERS, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402

CLOCK_PS = 6400
LAYOUTS = structs.layouts()
LATENCY = 7          # cycles from an order-table result to its top
SWEEP_CYCLES = 1 << 13
CONFIG = generator_config()
WINDOW = CONFIG.window


@dataclass
class Item:
    op: Op
    gap: int = 1   # cycles from this operation to the next


class Operations:
    """Builds operations as the decoder produces them; prices as ladder indices."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.seq = 0

    def op(self, kind: str, book: int, ref: int, shares: int = 0, price: int = 0, side: str = "") -> Op:
        self.seq += 1
        bucket0, bucket1 = buckets(ref, self.config.bucket_bits)
        return Op(kind, book, side, ref, bucket0, bucket1, shares, price, self.seq, 1000 + self.seq,
                  False, kind != "U_DELETE")

    def price(self, book: int, index: int, sub_penny: int = 0) -> int:
        return (self.config.base_cents(book) + index) * 100 + sub_penny

    def insert(self, book: int, ref: int, index: int, shares: int = 100, side: str = "B", sub_penny: int = 0) -> Op:
        return self.op("INSERT", book, ref, shares, self.price(book, index, sub_penny), side)

    def remove(self, book: int, ref: int, shares: int) -> Op:
        return self.op("REMOVE", book, ref, shares)

    def delete(self, book: int, ref: int) -> Op:
        return self.op("DELETE", book, ref)

    def replace(self, book: int, old_ref: int, new_ref: int, index: int, shares: int = 50) -> list[Item]:
        return [Item(self.op("U_DELETE", book, old_ref), 1),
                Item(self.op("U_INSERT", book, new_ref, shares, self.price(book, index)))]


class Monitor:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.cycle = 0   # the cycle whose outputs the next sample sees
        self.tops: list[dict] = []

    async def run(self) -> None:
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.top_valid):
                self.tops.append(structs.unpack(LAYOUTS["TOP_T"], dut.top_vector.value.to_unsigned()) | {"cycle": self.cycle})
            self.cycle += 1


async def start(dut) -> Monitor:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.cfg_base.value = sum(CONFIG.base_cents(i) << 32 * i for i in range(len(CONFIG.tickers)))
    dut.op_vector.value = 0
    dut.rst.value = 0
    monitor = Monitor(dut)
    cocotb.start_soon(monitor.run())
    await ClockCycles(dut.clk, 2)
    return monitor


async def reset(dut) -> None:
    """Reset, and wait for the sweep: busy exactly SWEEP_CYCLES cycles; an operation driven
    meanwhile is ignored (the checker asserts no top comes)."""
    await FallingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    busy_cycles = 0
    ignored = {"valid": 1, "change": structs.CHANGES["ADD"], "position": 0, "index": 5, "shares": 7}
    while is_high(dut.busy):
        busy_cycles += 1
        if busy_cycles == 100:
            dut.op_vector.value = structs.pack(LAYOUTS["OT_RESULT_T"], ignored)
        await FallingEdge(dut.clk)
        dut.op_vector.value = 0
    assert busy_cycles == SWEEP_CYCLES, f"busy for {busy_cycles} cycles, expected {SWEEP_CYCLES}"


def results_and_tops(model: HardwareModel, items: list[Item]) -> list[tuple[dict, dict, int]]:
    """(order-table result, expected top, gap) per operation, from the model."""
    out = []
    for item in items:
        result = model._order_table(item.op)
        top = model._book_engine(result)
        out.append((structs.ot_result_fields(result), structs.top_fields(top), item.gap))
    return out


async def drive(dut, monitor: Monitor, steps: list[tuple[dict, dict, int]], name: str) -> None:
    """Drive the results from the current cycle on (call right after a falling edge) and
    compare every top, with its cycle."""
    start_cycle = monitor.cycle
    inputs_at, expected = {}, []
    cycle = start_cycle
    for result, top, gap in steps:
        inputs_at[cycle] = structs.pack(LAYOUTS["OT_RESULT_T"], result | {"t_arrival": cycle & 0xFFFF})
        expected.append(top | {"t_arrival": cycle & 0xFFFF, "cycle": cycle + LATENCY})
        cycle += gap
    tops_before = len(monitor.tops)
    for cycle in range(start_cycle, cycle + LATENCY + 2):
        assert monitor.cycle == cycle
        dut.op_vector.value = inputs_at.get(cycle, 0)
        await FallingEdge(dut.clk)
    dut.op_vector.value = 0
    compare(expected, monitor.tops[tops_before:], f"{name}: tops")


async def check(dut, items: list[Item], name: str) -> None:
    monitor = await start(dut)
    await reset(dut)
    await drive(dut, monitor, results_and_tops(HardwareModel(CONFIG), items), name)


def spaced(ops: list[Op], gap: int) -> list[Item]:
    return [Item(op, gap) for op in ops]


# Operation streams


def level_read_after_write_items(gap: int) -> list[Item]:
    """Several operations on one level, gap cycles apart: 1 and 2 read the level while earlier
    writes are pending (write history), 3 reads it from the memory."""
    ops = Operations(CONFIG)
    items = spaced([
        ops.insert(0, 1, 4096, 100),
        ops.insert(0, 2, 4096, 50),       # 150
        ops.remove(0, 1, 30),             # 120
        ops.insert(0, 3, 4097, 10),       # a better level
        ops.delete(0, 2),                 # 4096: 70
        ops.delete(0, 3),                 # 4097 empty again
        ops.delete(0, 1),                 # 4096 empty: bid side empty
        ops.insert(0, 4, 4096, 5, "S"),   # the ask side at the same index
        ops.insert(0, 5, 4096, 6),
    ], gap)
    items += ops.replace(0, 5, 6, 4096, 9)    # U at the same price: U_DELETE and U_INSERT 1 cycle apart
    items += ops.replace(0, 6, 7, 4095, 9)
    return items


def reverse_forwarding_items(gap: int) -> list[Item]:
    """The best level changes right after an operation's search found it: the operation's top
    must show the level as it was after itself (best-shares reverse forwarding)."""
    ops = Operations(CONFIG)
    items = spaced([ops.insert(1, 1, 200, 100), ops.insert(1, 2, 190, 50), ops.insert(1, 3, 180, 25)], 3)
    items += spaced([
        ops.delete(1, 1),           # best becomes 190 (50) ...
        ops.remove(1, 2, 20),       # ... which the next operation changes to 30
        ops.remove(1, 2, 5),        # and the one after to 25
        ops.insert(1, 4, 190, 7),
        ops.delete(1, 2),           # best stays 190 (7)
        ops.delete(1, 4),           # best becomes 180 ...
        ops.insert(1, 5, 180, 1),   # ... changed at once
        ops.insert(1, 6, 185, 3, "S"),   # the other side, in between
        ops.remove(1, 3, 25),
    ], gap)
    return items


def flag_items() -> list[Item]:
    """Off-ladder orders on both sides, right before and after operations that produce tops."""
    ops = Operations(CONFIG)
    return spaced([
        ops.insert(2, 1, 4000, 100),                  # bid inside
        ops.insert(2, 2, -10, 5),                     # bid below: only matters without inside bids
        ops.insert(2, 3, 4100, 100, "S"),             # ask inside
        ops.insert(2, 4, WINDOW + 3, 5, "S"),         # ask above: same
        ops.delete(2, 1),                             # no inside bid: outside (bid below)
        ops.insert(2, 5, 4001, 10),                   # inside bid again
        ops.insert(2, 6, WINDOW + 1, 10),             # bid above the window: outside
        ops.delete(2, 6),
        ops.insert(2, 7, 50, 10, "S", sub_penny=37),  # unrepresentable ask: outside
        ops.remove(2, 7, 4),                          # partly: still there
        ops.delete(2, 7),
        ops.insert(2, 10, 4002, 3, sub_penny=1),      # unrepresentable bid: outside
        ops.delete(2, 10),
        ops.insert(2, 8, -1, 10, "S"),                # ask below the window: outside
        ops.delete(2, 3),
        ops.delete(2, 8),
        ops.delete(2, 4),                             # no ask at all
        ops.insert(2, 9, 3, 1),
    ], 1)


def search_items() -> list[Item]:
    """Bids and asks spread over several bitmap words, and both window edges."""
    ops = Operations(CONFIG)
    bids = [4096, 4090, 3000, 0, 8191, 64, 63, 127, 128]
    items = spaced([ops.insert(3, 100 + n, index, 10 + n) for n, index in enumerate(bids)], 1)
    items += spaced([ops.insert(3, 200 + n, index, 20 + n, "S") for n, index in enumerate(bids)], 1)
    for n in sorted(range(len(bids)), key=lambda n: -bids[n]):   # empty the bids from the best down
        items.append(Item(ops.delete(3, 100 + n)))
    for n in sorted(range(len(bids)), key=lambda n: bids[n]):    # the asks from the best up
        items.append(Item(ops.delete(3, 200 + n), 2))
    return items


def random_items(rng: random.Random, count: int) -> list[Item]:
    """Random operations on all books, prices on a few neighbouring levels so that levels and
    the best change all the time, some off the ladder; random spacing, U halves 1 cycle apart."""
    ops = Operations(CONFIG)
    books = len(CONFIG.tickers)
    live: list[int] = []
    next_ref = 1
    items: list[Item] = []

    def index() -> tuple[int, int]:
        roll = rng.random()
        if roll < 0.9:
            return 4090 + rng.randrange(12), 0
        if roll < 0.95:
            return rng.choice([-3, WINDOW + 3, 0, WINDOW - 1]), 0
        return 4090 + rng.randrange(12), rng.randrange(1, 100)

    while len(items) < count:
        roll = rng.random()
        if roll < 0.45 or not live:
            book = rng.randrange(books)
            level, sub_penny = index()
            new = [Item(ops.insert(book, next_ref, level, rng.randrange(1, 500), rng.choice("BS"), sub_penny))]
            live.append(next_ref)
            next_ref += 1
        else:
            ref = rng.choice(live)
            book = rng.randrange(books)   # the order table uses the stored order's book anyway
            if roll < 0.65:
                new = [Item(ops.remove(book, ref, rng.randrange(1, 300)))]
            elif roll < 0.85:
                new = [Item(ops.delete(book, ref))]
                live.remove(ref)
            else:
                level, _ = index()
                new = ops.replace(book, ref, next_ref, level, rng.randrange(1, 500))
                live.remove(ref)
                live.append(next_ref)
                next_ref += 1
        new[-1].gap = rng.choice([1, 1, 1, 2, 3])
        items += new
    return items


def vector_steps(vector: vectors.Vector, rng: random.Random | None) -> list[tuple[dict, dict, int]]:
    model = HardwareModel(vector.config, taps=True)
    for frame in vector.frames:
        model.process_frame(frame.data, frame.fcs_error)
    taps = model.taps
    return [
        (structs.ot_result_fields(result), structs.top_fields(top),
         1 if op.kind == "U_DELETE" or rng is None else rng.choice([1, 2, 2, 3, 6]))
        for op, result, top in zip(taps.ops, taps.ot_results, taps.tops)
    ]


# Tests


@cocotb.test()
async def vectors_back_to_back_and_spaced(dut):
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    monitor = await start(dut)
    rng = random.Random(1)
    for name in names:
        vector = vectors.load(name)
        for spacing in (None, rng):
            await reset(dut)
            dut.cfg_base.value = sum(vector.config.base_cents(i) << 32 * i for i in range(len(vector.config.tickers)))
            await drive(dut, monitor, vector_steps(vector, spacing), f"{name} ({'spaced' if spacing else 'back to back'})")


@cocotb.test()
async def level_read_after_write(dut):
    monitor = await start(dut)
    for gap in (1, 2, 3):
        await reset(dut)
        await drive(dut, monitor, results_and_tops(HardwareModel(CONFIG), level_read_after_write_items(gap)), f"gap {gap}")


@cocotb.test()
async def best_shares_reverse_forwarding(dut):
    monitor = await start(dut)
    for gap in (1, 2, 3):
        await reset(dut)
        await drive(dut, monitor, results_and_tops(HardwareModel(CONFIG), reverse_forwarding_items(gap)), f"gap {gap}")


@cocotb.test()
async def flags_and_off_ladder_counters(dut):
    await check(dut, flag_items(), "flags")


@cocotb.test()
async def search_across_words_and_window_edges(dut):
    await check(dut, search_items(), "search")


@cocotb.test()
async def random_streams(dut):
    monitor = await start(dut)
    for seed in range(3):
        await reset(dut)
        items = random_items(random.Random(200 + seed), 3000)
        await drive(dut, monitor, results_and_tops(HardwareModel(CONFIG), items), f"random {seed}")


@cocotb.test()
async def reset_clears_the_ladders(dut):
    """Levels set, then reset: the sweep zeroes the memories and bitmaps, so the old levels
    don't show up in later tops."""
    ops = Operations(CONFIG)
    monitor = await start(dut)
    await reset(dut)
    before = spaced([ops.insert(4, n, 5000 + n, 10, "BS"[n % 2]) for n in range(40)], 1)
    await drive(dut, monitor, results_and_tops(HardwareModel(CONFIG), before), "before reset")
    await reset(dut)
    after = spaced([ops.insert(4, 100 + n, 4000 - n, 10, "BS"[n % 2]) for n in range(10)], 1)
    await drive(dut, monitor, results_and_tops(HardwareModel(CONFIG), after), "after reset")


def test_book_engine():
    run(
        "book_engine_tb_top",
        [
            RTL / "itch_pkg.vhd", RTL / "ram_sdp.vhd", RTL / "lutram.vhd", RTL / "bitmap_search.vhd",
            RTL / "ladder_side.vhd", RTL / "book_engine.vhd", CHECKERS / "book_engine_chk.vhd",
            WRAPPERS / "book_engine_tb_top.vhd",
        ],
        __file__,
    )


if __name__ == "__main__":
    test_book_engine()
