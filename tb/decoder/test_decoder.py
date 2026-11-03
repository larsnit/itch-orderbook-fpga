"""decoder and price_unit against the hardware model: every operation (fields and the cycle
it appears in), every event, the price position of every insert in the order table's update
stage, and the counter pulses.

    python3.11 tb/decoder/test_decoder.py             (Questa)
    SIM=nvc python3.11 tb/decoder/test_decoder.py

Each vector case runs from reset with a fresh model (all books go stale at most once, so one
long run would see at most one event). Tokens come from the model's token tap, spaced as the
framer delivers them: a message token at least (length + 2) / 8 cycles after the previous one.
Needs data/vectors (python3.11 scripts/make_vectors.py -o data/vectors).
"""

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge  # noqa: E402

from itch.config import Config  # noqa: E402
from itch.generator import generator_config  # noqa: E402
from itch.hw_model import HardwareModel, Op, Token  # noqa: E402
from itch.ladder import position_of  # noqa: E402
from itch.messages import AddOrder, OrderReplace, StockDirectory  # noqa: E402
from itch.record import BAD_FCS, Record  # noqa: E402
from tb.common import structs, vectors  # noqa: E402
from tb.common.axis import is_high  # noqa: E402
from tb.common.runner import CHECKERS, RTL, WRAPPERS, run  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402

CLOCK_PS = 6400
LAYOUTS = structs.layouts()
DECODER_COUNTERS = ("duplicates", "gaps", "unusable_messages", "messages", "events")
PULSES = {"duplicates": "duplicate_pulse", "gaps": "gap_pulse", "unusable_messages": "unusable_pulse",
          "messages": "messages_pulse", "events": "events_pulse"}
PRICE_UPDATE_STAGE = 3   # cycles from an operation to its order-table update stage


@dataclass
class Step:
    """One token and what the model produced for it."""
    token: Token
    ops: list[Op] = field(default_factory=list)
    events: list[Record] = field(default_factory=list)


def spacing(token: Token) -> int:
    """Fewest cycles from this token to the next, as the framer delivers them."""
    return max(1, (token.length + 2) // 8) if token.kind == "MSG" else 1


def attribute(tokens: list[Token], ops: list[Op], events: list[Record]) -> list[Step]:
    """Which token of a frame caused which operation and event."""
    steps = [Step(token) for token in tokens]
    for op in ops:
        next(s for s in steps if s.token.kind == "MSG" and s.token.seq == op.seq).ops.append(op)
    for event in events:
        if event.flags & BAD_FCS:
            cause = next(s for s in steps if s.token.kind == "FCS")
        else:   # a gap or an unusable message: the token with the event's sequence number
            cause = next(s for s in steps if s.token.kind in ("MSG", "HEARTBEAT") and s.token.seq == event.seq)
        cause.events.append(event)
    return steps


def vector_steps(vector: vectors.Vector) -> tuple[list[Step], dict[str, int]]:
    model = HardwareModel(vector.config, taps=True)
    steps = []
    for frame in vector.frames:
        tokens_before, ops_before = len(model.taps.tokens), len(model.taps.ops)
        _, events = model.process_frame(frame.data, frame.fcs_error)
        steps += attribute(model.taps.tokens[tokens_before:], model.taps.ops[ops_before:], events)
    return steps, model.counters


def directory(locate: int, stock: str, timestamp: int) -> bytes:
    return StockDirectory(locate, 0, timestamp, stock, "Q", "N", 100, "N", "C", "Z", "P", "N", "N", "1", "N", 0, "N").to_bytes()


def add(locate: int, ref: int, timestamp: int, price: int = 2_895_600) -> bytes:
    return AddOrder(locate, 0, timestamp, ref, "B", 100, "AAPL", price).to_bytes()


def locate_learning_steps(config: Config) -> tuple[list[Step], dict[str, int]]:
    """R messages re-assigning locates: a new R replaces a book's locate; two books holding the
    same locate give the lowest book; an R for an untracked ticker or of the wrong length
    teaches nothing (the latter is unusable)."""
    raws = [
        directory(7, "AAPL", 1000),          # book 0 = locate 7
        add(7, 1, 1001),                     # -> book 0
        directory(9, "AAPL", 1002),          # book 0 = locate 9 now
        add(7, 2, 1003),                     # locate 7: no book
        add(9, 3, 1004),                     # -> book 0
        directory(9, "MSFT", 1005),          # book 1 = locate 9 too
        add(9, 4, 1006),                     # -> book 0, the lowest
        directory(5, "IBM", 1007),           # not tracked
        directory(5, "TSLA", 1008)[:-1],     # wrong length: unusable, all books stale
        add(5, 5, 1009),                     # locate 5 unknown: no book
        add(9, 6, 1010, price=17),           # -> book 0, stale; a price below the window
    ]
    model = HardwareModel(config, taps=True)
    steps = []
    for seq, raw in enumerate(raws, start=1):
        ops_before = len(model.taps.ops)
        _, events = model.process(seq, raw)
        steps += attribute([Token("MSG", seq, len(raw), raw[: structs.MSG_BYTES])], model.taps.ops[ops_before:], events)
    return steps, model.counters


def window_edge_prices(base: int, window: int) -> list[int]:
    """Prices at and around both edges of a window that starts at base cents."""
    low, high = base * 100, (base + window) * 100
    return [low - 100, low - 1, low, low + 1, low + 99, low + 100, high - 100, high - 99, high - 1, high, high + 100]


def price_steps(config: Config, rng: random.Random) -> tuple[list[Step], dict[str, int]]:
    """Every book learns its locate, then A and U messages with prices at the window edges,
    sub-penny prices, 0, the largest 32-bit price and random prices; A carries the price in
    bytes 32-35, U in bytes 31-34."""
    raws = [directory(100 + book, ticker, 1) for book, ticker in enumerate(config.tickers)]
    prices = [(book, price) for book in range(len(config.tickers))
              for price in window_edge_prices(config.base_cents(book), config.window) + [0, 0xFFFF_FFFF]]
    for _ in range(400):
        book = rng.randrange(len(config.tickers))
        inside = config.base_cents(book) * 100 + rng.randrange(config.window * 100)   # often sub-penny
        prices.append((book, rng.choice([inside, inside - inside % 100, rng.getrandbits(32), rng.randrange(10**8)])))
    for ref, (book, price) in enumerate(prices, start=1):
        if ref % 2:
            raws.append(AddOrder(100 + book, 0, ref, ref, rng.choice("BS"), 100, config.tickers[book], price).to_bytes())
        else:
            raws.append(OrderReplace(100 + book, 0, ref, ref - 1, ref + 10**6, 100, price).to_bytes())
    model = HardwareModel(config, taps=True)
    steps = []
    for seq, raw in enumerate(raws, start=1):
        ops_before = len(model.taps.ops)
        _, events = model.process(seq, raw)
        steps += attribute([Token("MSG", seq, len(raw), raw[: structs.MSG_BYTES])], model.taps.ops[ops_before:], events)
    return steps, model.counters


class OutputMonitor:
    def __init__(self, dut) -> None:
        self.dut = dut
        self.cycle = 0   # number of the cycle whose outputs the next sample sees
        self.ops: list[dict] = []
        self.events: list[dict] = []
        self.prices: dict[int, tuple[int, int]] = {}
        self.pulses = dict.fromkeys(PULSES, 0)

    async def run(self) -> None:
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if is_high(dut.op_valid):
                self.ops.append(structs.unpack(LAYOUTS["OP_T"], dut.op_vector.value.to_unsigned()) | {"cycle": self.cycle})
            if is_high(dut.event_valid):
                self.events.append(structs.unpack(LAYOUTS["EVENT_T"], dut.event_vector.value.to_unsigned()) | {"cycle": self.cycle})
            if dut.price_info_vector.value.is_resolvable:
                price = structs.unpack(LAYOUTS["PRICE_INFO_T"], dut.price_info_vector.value.to_unsigned())
                self.prices[self.cycle] = (price["position"], price["index"])
            for counter, port in PULSES.items():
                self.pulses[counter] += is_high(getattr(dut, port))
            self.cycle += 1


def event_fields(event: Record) -> dict[str, int]:
    return {"valid": 1, "flags": event.flags, "seq": event.seq, "timestamp": event.timestamp}


async def start(dut, config: Config) -> OutputMonitor:
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    dut.cfg_ticker.value = sum(int.from_bytes(t.ljust(8).encode(), "big") << 64 * i for i, t in enumerate(config.tickers))
    dut.cfg_base.value = sum(config.base_cents(i) << 32 * i for i in range(len(config.tickers)))
    dut.token_vector.value = 0
    monitor = OutputMonitor(dut)
    cocotb.start_soon(monitor.run())
    return monitor


async def check_steps(dut, monitor: OutputMonitor, config: Config, steps: list[Step], counters: dict, name: str, rng) -> None:
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    ops_before, events_before = len(monitor.ops), len(monitor.events)
    pulses_before = dict(monitor.pulses)

    expected_ops, expected_events, expected_prices = [], [], []
    for step in steps:
        await FallingEdge(dut.clk)
        cycle = monitor.cycle
        fields = structs.token_fields(step.token) | {"t_arrival": cycle & 0xFFFF}
        dut.token_vector.value = structs.pack(LAYOUTS["TOKEN_T"], fields)
        for half, op in enumerate(step.ops):   # a U: U_DELETE, then U_INSERT one cycle later
            op_cycle = cycle + 1 + half
            expected_ops.append(structs.op_fields(op) | {"t_arrival": cycle & 0xFFFF, "cycle": op_cycle})
            if op.kind in ("INSERT", "U_INSERT"):
                position, index = position_of(op.price, config.base_cents(op.book_id), config.window)
                expected_prices.append((op_cycle + PRICE_UPDATE_STAGE, structs.POSITIONS[position], index, op))
        expected_events += [event_fields(event) | {"cycle": cycle + 1} for event in step.events]
        for _ in range(spacing(step.token) + (rng.choice([0, 0, 1, 3]) if rng else 0)):
            await FallingEdge(dut.clk)
            dut.token_vector.value = 0
    await ClockCycles(dut.clk, 6)

    compare(expected_ops, monitor.ops[ops_before:], f"{name}: operations")
    compare(expected_events, monitor.events[events_before:], f"{name}: events")
    for cycle, position, index, op in expected_prices:
        assert monitor.prices.get(cycle) == (position, index), (
            f"{name}: price {op.price} of {op.kind} seq {op.seq} (book {op.book_id}): "
            f"expected position {position} index {index} in cycle {cycle}, got {monitor.prices.get(cycle)}")
    for counter in DECODER_COUNTERS:
        got = monitor.pulses[counter] - pulses_before[counter]
        assert got == counters[counter], f"{name}: {got} {counter} pulses, expected {counters[counter]}"


async def check_vectors(dut, rng) -> None:
    names = vectors.case_names()
    assert names, "no vectors: run python3.11 scripts/make_vectors.py -o data/vectors"
    loaded = [vectors.load(name) for name in names]
    monitor = await start(dut, loaded[0].config)
    for vector in loaded:
        steps, counters = vector_steps(vector)
        await check_steps(dut, monitor, vector.config, steps, counters, vector.name, rng)


@cocotb.test()
async def vectors_at_the_framers_fastest_spacing(dut):
    await check_vectors(dut, rng=None)


@cocotb.test()
async def vectors_with_random_gaps(dut):
    await check_vectors(dut, rng=random.Random(1))


@cocotb.test()
async def locate_learning(dut):
    config = generator_config()
    monitor = await start(dut, config)
    steps, counters = locate_learning_steps(config)
    await check_steps(dut, monitor, config, steps, counters, "locate learning", rng=None)


@cocotb.test()
async def prices_across_the_window(dut):
    config = generator_config()
    monitor = await start(dut, config)
    steps, counters = price_steps(config, random.Random(2))
    await check_steps(dut, monitor, config, steps, counters, "prices", rng=None)


def test_decoder():
    run(
        "decoder_tb_top",
        [
            RTL / "itch_pkg.vhd", RTL / "itch_hash_pkg.vhd", RTL / "h3_hash.vhd", RTL / "decoder.vhd",
            RTL / "price_unit.vhd", CHECKERS / "decoder_chk.vhd", WRAPPERS / "decoder_tb_top.vhd",
        ],
        __file__,
    )


if __name__ == "__main__":
    test_decoder()
