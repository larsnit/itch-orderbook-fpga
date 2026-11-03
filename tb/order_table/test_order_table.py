"""order_table against the hardware model's order table: every result (fields and the cycle it
appears in, 3 cycles after its operation), the anomaly pulses, and the book_stale port.

    python3.11 tb/order_table/test_order_table.py             (Questa)
    SIM=nvc python3.11 tb/order_table/test_order_table.py

Operations are driven straight into the order table, and the price position of each insert in
its update stage, as the price unit delivers it; in all other cycles price_info carries random
values. Each test starts from reset, which also checks the clearing sweep. The reference is a
fresh HardwareModel's _order_table, fed the same operations.
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
from itch.generator import colliding_refs, generator_config  # noqa: E402
from itch.hashing import buckets  # noqa: E402
from itch.hw_model import HardwareModel, Op  # noqa: E402
from itch.ladder import position_of  # noqa: E402
from tb.common import structs, vectors  # noqa: E402
from tb.common.axis import is_high  # noqa: E402
from tb.common.runner import CHECKERS, RTL, WRAPPERS, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402

CLOCK_PS = 6400
LAYOUTS = structs.layouts()
UPDATE_STAGE = 3     # cycles from an operation to its update stage (RAM_LATENCY + 1)
MIN_GAP = 2          # operations of different messages are at least 2 cycles apart
RESULT_LATENCY = 4   # cycles from an operation to its result
SWEEP_CYCLES = 1 << 13
PULSES = {
    "insert_failures": "insert_failure_pulse", "ref_errors": "ref_error_pulse", "unknown_refs": "unknown_ref_pulse",
    "inconsistent": "inconsistent_pulse", "stash_inserts": "stash_insert_pulse",
    "off_ladder_orders": "off_ladder_pulse", "unrep_prices": "unrep_pulse",
}
CONFIG = generator_config()


@dataclass
class Item:
    op: Op
    gap: int = MIN_GAP   # cycles from this operation to the next (1 only from U_DELETE to U_INSERT)


class Operations:
    """Builds operations as the decoder produces them."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.seq = 0

    def op(self, kind: str, book: int, ref: int, shares: int = 0, price: int = 0, side: str = "") -> Op:
        self.seq += 1
        bucket0, bucket1 = buckets(ref, self.config.bucket_bits)
        return Op(kind, book, side, ref, bucket0, bucket1, shares, price, self.seq, 1000 + self.seq,
                  False, kind != "U_DELETE")

    def price(self, book: int, cents_from_base: int = 10, sub_penny: int = 0) -> int:
        return (self.config.base_cents(book) + cents_from_base) * 100 + sub_penny

    def insert(self, book: int, ref: int, shares: int = 100, price: int | None = None, side: str = "B") -> Op:
        return self.op("INSERT", book, ref, shares, self.price(book) if price is None else price, side)

    def remove(self, book: int, ref: int, shares: int) -> Op:
        return self.op("REMOVE", book, ref, shares)

    def delete(self, book: int, ref: int) -> Op:
        return self.op("DELETE", book, ref)

    def replace(self, book: int, old_ref: int, new_ref: int, shares: int = 50, price: int | None = None) -> list[Item]:
        """U_DELETE, then U_INSERT in the next cycle."""
        return [Item(self.op("U_DELETE", book, old_ref), 1),
                Item(self.op("U_INSERT", book, new_ref, shares, self.price(book) if price is None else price))]


class Monitor:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.cycle = 0   # the cycle whose outputs the next sample sees
        self.results: list[dict] = []
        self.pulses = dict.fromkeys(PULSES, 0)

    async def run(self) -> None:
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.result_valid):
                self.results.append(structs.unpack(LAYOUTS["OT_RESULT_T"], dut.result_vector.value.to_unsigned()) | {"cycle": self.cycle})
            for counter, port in PULSES.items():
                self.pulses[counter] += is_high(getattr(dut, port))
            self.cycle += 1


async def start(dut) -> Monitor:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.op_vector.value = 0
    dut.price_info_vector.value = 0
    dut.rst.value = 0
    monitor = Monitor(dut)
    cocotb.start_soon(monitor.run())
    await ClockCycles(dut.clk, 2)
    return monitor


async def reset(dut) -> None:
    """Reset, and wait for the sweep: busy exactly SWEEP_CYCLES cycles; an operation driven
    meanwhile is ignored (the checker asserts no result comes)."""
    await FallingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    busy_cycles = 0
    while is_high(dut.busy):
        busy_cycles += 1
        if busy_cycles == 100:   # ignored, not processed after the sweep
            dut.op_vector.value = structs.pack(LAYOUTS["OP_T"], structs.op_fields(Operations(CONFIG).insert(0, 77)))
        await FallingEdge(dut.clk)
        dut.op_vector.value = 0
    assert busy_cycles == SWEEP_CYCLES, f"busy for {busy_cycles} cycles, expected {SWEEP_CYCLES}"


async def drive(dut, monitor: Monitor, model: HardwareModel, items: list[Item], name: str, rng: random.Random) -> None:
    """Drive items from the current cycle on (call right after a falling edge), and compare
    results, pulses and book_stale with what the model's order table gives for them."""
    config = model.config
    start_cycle = monitor.cycle
    ops_at, prices_at, expected = {}, {}, []
    counters_before = dict(model.counters)
    cycle = start_cycle
    for item in items:
        op = item.op
        ops_at[cycle] = structs.pack(LAYOUTS["OP_T"], structs.op_fields(op) | {"t_arrival": cycle & 0xFFFF})
        if op.kind in ("INSERT", "U_INSERT"):
            position, index = position_of(op.price, config.base_cents(op.book_id), config.window)
            prices_at[cycle + UPDATE_STAGE] = structs.pack(LAYOUTS["PRICE_INFO_T"], {"position": structs.POSITIONS[position], "index": index})
        result = model._order_table(op)
        expected.append(structs.ot_result_fields(result) | {"t_arrival": cycle & 0xFFFF, "cycle": cycle + RESULT_LATENCY})
        cycle += item.gap

    results_before = len(monitor.results)
    pulses_before = dict(monitor.pulses)
    junk_width = structs.width(LAYOUTS["PRICE_INFO_T"])
    for cycle in range(start_cycle, cycle + RESULT_LATENCY + 2):
        assert monitor.cycle == cycle
        dut.op_vector.value = ops_at.get(cycle, 0)
        dut.price_info_vector.value = prices_at.get(cycle, rng.getrandbits(junk_width))
        await FallingEdge(dut.clk)
    dut.op_vector.value = 0

    compare(expected, monitor.results[results_before:], f"{name}: results")
    for counter in PULSES:
        got = monitor.pulses[counter] - pulses_before[counter]
        want = model.counters[counter] - counters_before[counter]
        assert got == want, f"{name}: {got} {counter} pulses, expected {want}"
    stale = sum(bit << book for book, bit in enumerate(model.book_stale))
    assert dut.book_stale.value.to_unsigned() == stale, f"{name}: book_stale {dut.book_stale.value}, expected {stale:08b}"


async def check(dut, items: list[Item], name: str, seed: int = 0) -> None:
    monitor = await start(dut)
    await reset(dut)
    await drive(dut, monitor, HardwareModel(CONFIG), items, name, random.Random(seed))


# Operation streams


def spaced(ops: list[Op], gap: int) -> list[Item]:
    return [Item(op, gap) for op in ops]


def forwarding_items(gap: int) -> list[Item]:
    """The same order, and orders sharing its buckets, touched every gap cycles: at a gap of 2 and 3
    the match stage reads rows whose last writes are still in the history, from 4 on the memory
    has them."""
    ops = Operations(CONFIG)
    first, second, third = colliding_refs(3, CONFIG.bucket_bits, base=0x1234_5678)
    items = spaced([
        ops.insert(0, first, 100),
        ops.remove(0, first, 30),           # 100 -> 70, found in the row just written
        ops.insert(0, second, 200),         # same buckets: must see the first's slot taken
        ops.remove(0, first, 20),           # 70 -> 50
        ops.insert(0, third, 300),
        ops.remove(0, second, 200),         # gone
        ops.insert(0, second, 5),           # back into the freed slot
        ops.delete(0, first),
        ops.remove(0, third, 1),
        ops.delete(0, third),
        ops.delete(0, second),
        ops.delete(0, second),              # unknown now
    ], gap)
    return items


def replace_items() -> list[Item]:
    """U in all its cases, back to back."""
    ops = Operations(CONFIG)
    a, b, c, d = colliding_refs(4, CONFIG.bucket_bits, base=0x0BAD_0000)
    items = spaced([ops.insert(1, a, 100, side="S"), ops.insert(1, 0x5000_0001, 10)], MIN_GAP)
    items += ops.replace(1, a, b, 60)                 # old and new ref in the same buckets; side S taken over
    items += spaced([ops.delete(1, a)], MIN_GAP)      # the old ref is gone from that row: unknown
    items += ops.replace(1, b, 0x5000_0002, 70)       # to other buckets
    items += ops.replace(1, 0x7777_7777, c, 80)       # old ref unknown: the U_INSERT does nothing
    items += ops.replace(1, 0x5000_0002, 0x5000_0001, 90)   # new ref exists: inconsistent
    items += ops.replace(2, 0x5000_0001, d, 40)       # the order is book 1's, the U says book 2
    items += ops.replace(1, d, 1 << 32 | d, 40)       # d was never inserted
    items += spaced([ops.insert(3, 0x6000_0000, 10)], MIN_GAP)
    items += ops.replace(3, 0x6000_0000, 1 << 33, 20)   # new ref too big: ref error
    items += spaced([ops.delete(3, 1 << 33)], MIN_GAP)      # a ref error never matches: unknown
    return items


def fill_items() -> list[Item]:
    """Refs that all share both buckets: 16 go into the two rows (alternating, table 0 first),
    32 into the stash, the 49th fails. Then removals from rows and stash, re-inserts, and
    anomalies on stash entries."""
    ops = Operations(CONFIG)
    refs = colliding_refs(50, CONFIG.bucket_bits, base=0x2468_ACE0)
    items = spaced([ops.insert(4, ref, 10 + n, side="BS"[n % 2]) for n, ref in enumerate(refs[:49])], MIN_GAP)
    items += spaced([
        ops.insert(4, refs[3], MIN_GAP),          # already in a row: inconsistent
        ops.insert(4, refs[40], MIN_GAP),         # already in the stash: inconsistent
        ops.delete(4, 1 << 40 | refs[30]),  # low 32 bits match a stash entry, but a ref error never matches
        ops.remove(4, refs[40], 1000),      # more than it has: clamped, inconsistent
        ops.remove(4, refs[20], 5),         # a stash entry, partly
        ops.delete(4, refs[2]),             # a row slot freed ...
        ops.insert(4, refs[49], 7),         # ... and taken again
        ops.insert(4, refs[48], 8),         # full again: the failed ref still fails
        ops.delete(4, refs[25]),            # a stash entry freed ...
        ops.insert(4, refs[48], 9),         # ... and taken
    ], MIN_GAP)
    items += ops.replace(4, refs[30], refs[30], 11)   # same ref, in the stash: U_DELETE frees it, U_INSERT takes it again
    items += ops.replace(4, refs[0], refs[0], 12)     # same ref, in a row
    items += spaced([ops.delete(4, ref) for ref in refs], MIN_GAP)   # everything out, some unknown
    items += spaced([ops.insert(4, ref, 3) for ref in refs[:20]], MIN_GAP)   # rows only again
    return items


def anomaly_and_price_items() -> list[Item]:
    ops = Operations(CONFIG)
    items = spaced([
        ops.insert(5, 11, 100, price=ops.price(5, 0)),                  # the window's first level
        ops.insert(5, 12, 100, price=ops.price(5, CONFIG.window - 1)),  # its last level
        ops.insert(5, 13, 100, price=ops.price(5, -1)),                 # below
        ops.insert(5, 14, 100, price=ops.price(5, CONFIG.window)),      # above
        ops.insert(5, 15, 100, price=ops.price(5, 10, sub_penny=50)),   # unrepresentable
        ops.insert(6, 1 << 40, 100),                                    # ref error
        ops.remove(6, 99, 1),                                           # unknown
        ops.delete(7, 1 << 40 | 11),                                    # ref error: never matches 11
        ops.remove(6, 13, 40),                                          # book 5's order, book 6's message
        ops.remove(5, 13, 60),                                          # gone exactly
        ops.remove(5, 14, 101),                                         # clamped
        ops.insert(5, 0, 1, side="S"),                                  # ref 0 is a valid ref
        ops.insert(5, 0xFFFF_FFFF, 2, side="S"),
        ops.delete(5, 0),
        ops.remove(5, 0xFFFF_FFFF, 0),                                  # 0 shares: nothing changes
        ops.delete(5, 0xFFFF_FFFF),
    ], 2)
    return items


def random_items(rng: random.Random, count: int) -> list[Item]:
    """A random mix like the decoder produces, with anomalies, two groups of colliding refs (so
    the stash fills and empties), random spacing and U halves in consecutive cycles."""
    ops = Operations(CONFIG)
    model = HardwareModel(CONFIG)   # only to know which refs are live
    groups = [colliding_refs(60, CONFIG.bucket_bits, base=0x1357_9BDF), colliding_refs(60, CONFIG.bucket_bits, base=0x0ACE_1357)]
    books = len(CONFIG.tickers)

    def new_ref() -> int:
        if rng.random() < 0.5:
            return rng.choice(rng.choice(groups))
        return rng.getrandbits(32) if rng.random() < 0.98 else rng.getrandbits(40) | 1 << 35

    def price(book: int) -> int:
        roll = rng.random()
        if roll < 0.85:
            return ops.price(book, rng.randrange(CONFIG.window))
        if roll < 0.95:
            return ops.price(book, rng.choice([-5, CONFIG.window + 5]))
        return ops.price(book, rng.randrange(CONFIG.window), sub_penny=rng.randrange(1, 100))

    items: list[Item] = []
    live: list[tuple[int, int]] = []   # (ref, book)
    while len(items) < count:
        roll = rng.random()
        target = rng.choice(live) if live and rng.random() < 0.9 else (rng.getrandbits(32), rng.randrange(books))
        book = target[1] if rng.random() < 0.95 else rng.randrange(books)
        if roll < 0.4 or not live:
            book = rng.randrange(books)
            new = [Item(ops.insert(book, new_ref(), rng.randrange(1, 1000), price(book), rng.choice("BS")))]
        elif roll < 0.6:
            new = [Item(ops.remove(book, target[0], rng.choice([rng.randrange(1, 200), rng.randrange(1, 2000)])))]
        elif roll < 0.75:
            new = [Item(ops.delete(book, target[0]))]
        elif roll < 0.95:
            new = ops.replace(book, target[0], new_ref(), rng.randrange(1, 1000), price(book))
        else:
            new = [Item(ops.insert(book, target[0], 5, price(book)))]   # often a duplicate
        new[-1].gap = rng.choice([2, 2, 2, 3, 4, 6])
        for item in new:
            model._order_table(item.op)
        items += new
        live = [(entry.order_ref, entry.book_id) for table in model.table.tables for row in table for entry in row if entry]
        live += [(entry.order_ref, entry.book_id) for entry in model.table.stash if entry]
    return items


def vector_items(vector: vectors.Vector, rng: random.Random | None) -> list[Item]:
    """The model's operations for a vector; U halves 1 cycle apart, others 1 cycle (fastest)
    or randomly spaced."""
    model = HardwareModel(vector.config, taps=True)
    for frame in vector.frames:
        model.process_frame(frame.data, frame.fcs_error)
    return [Item(op, 1 if op.kind == "U_DELETE" else MIN_GAP if rng is None else rng.choice([2, 2, 3, 4, 7])) for op in model.taps.ops]


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
            items = vector_items(vector, spacing)
            await drive(dut, monitor, HardwareModel(vector.config), items, f"{name} ({'spaced' if spacing else 'back to back'})", rng)


@cocotb.test()
async def forwarding_at_distances_2_to_5(dut):
    monitor = await start(dut)
    for gap in (2, 3, 4, 5):
        await reset(dut)
        await drive(dut, monitor, HardwareModel(CONFIG), forwarding_items(gap), f"gap {gap}", random.Random(gap))


@cocotb.test()
async def replace_cases(dut):
    await check(dut, replace_items(), "replace")


@cocotb.test()
async def buckets_and_stash_up_to_a_failed_insert(dut):
    await check(dut, fill_items(), "fill")


@cocotb.test()
async def anomalies_and_price_positions(dut):
    await check(dut, anomaly_and_price_items(), "anomalies")


@cocotb.test()
async def random_streams(dut):
    monitor = await start(dut)
    for seed in range(3):
        await reset(dut)
        rng = random.Random(100 + seed)
        await drive(dut, monitor, HardwareModel(CONFIG), random_items(rng, 4000), f"random {seed}", rng)


@cocotb.test()
async def reset_clears_the_table(dut):
    """Orders inserted, then reset: the sweep empties the memories (which keep their contents
    through rst), so the same refs are unknown afterwards."""
    ops = Operations(CONFIG)
    refs = [rng_ref for rng_ref in random.Random(5).sample(range(1 << 32), 40)] + colliding_refs(40, CONFIG.bucket_bits)
    monitor = await start(dut)
    await reset(dut)
    await drive(dut, monitor, HardwareModel(CONFIG), spaced([ops.insert(n % 8, ref) for n, ref in enumerate(refs)], MIN_GAP), "before reset", random.Random(6))
    await reset(dut)
    await drive(dut, monitor, HardwareModel(CONFIG), spaced([ops.delete(n % 8, ref) for n, ref in enumerate(refs)], MIN_GAP), "after reset", random.Random(7))


def test_order_table():
    run(
        "order_table_tb_top",
        [
            RTL / "itch_pkg.vhd", RTL / "ram_sdp.vhd", RTL / "ot_bank.vhd", RTL / "ot_stash.vhd",
            RTL / "order_table.vhd", CHECKERS / "order_table_chk.vhd", WRAPPERS / "order_table_tb_top.vhd",
        ],
        __file__,
    )


if __name__ == "__main__":
    test_order_table()
