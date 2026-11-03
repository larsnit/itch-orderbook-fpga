"""Sizing statistics for the hardware order table and price ladders, from a full ITCH day.

    python3.11 scripts/stats.py data/12302019.NASDAQ_ITCH50 --json data/stats.json

Pass 1 reads the day up to the opening cross of every tracked ticker and takes the cross
price as that book's reference price, standing in for the previous close that software
would configure before the open. Pass 2 replays the whole day through ideal books and
measures, per book:

  - live orders and price levels (peak, and when),
  - price range, and prices that are not whole cents (and whether any fall inside a window),
  - where the best bid and ask moved during regular hours,
  - how far new orders are from the best price on their side,
  - for each ladder window W (ticks, centred on the reference price): how many orders fall
    outside it, and how many messages leave the book with its top outside the window,
  - the planned order table (2-choice hashing, small stash) for 4 and 8 slots per bucket
    and several sizes, with the hash functions the hardware would use,
  - the largest order ref, order and level sizes (bit widths).

--ref TICKER=PRICE sets a reference price by hand (in dollars), e.g. when --limit stops
before the opening cross.
"""

import argparse
import heapq
import json
import struct
import sys
import time
from dataclasses import asdict, dataclass, field
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itch.hashing import HASH_SEEDS, H3Hash  # noqa: E402
from itch.messages import (  # noqa: E402
    AddOrder,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderReplace,
    StockDirectory,
    decode,
    peek,
    read_stream,
    timestamp_of,
)
from run_model import DEFAULT_TICKERS, format_time  # noqa: E402

TICK = 100  # one cent in ITCH price units (4 implied decimals)
OPEN_NS = (9 * 3600 + 30 * 60) * 1_000_000_000
CLOSE_NS = 16 * 3600 * 1_000_000_000

WINDOWS = [1024, 2048, 4096, 8192]  # ladder sizes in ticks
# Order table sizes to simulate: (slots per bucket, buckets per table); always two tables.
TABLE_SIZES = [(4, 16384), (4, 32768), (4, 65536), (8, 4096), (8, 8192), (8, 16384), (8, 32768)]
STASH = 16  # entries for inserts that find both buckets full
# One entry: order ref 40, shares 32, ladder index 13, window position 2, book id 3, side 1, valid 1.
ENTRY_BITS = 92

# Cross Trade message (not modelled in messages.py): type, locate, tracking, timestamp,
# shares, stock, cross price, match number, cross type.
CROSS_TRADE = ">1s H H H I Q 8s I Q 1s"
DISTANCE_BUCKETS = ["better", "0", "1-9", "10-99", "100-999", "1000-9999", ">=10000", "no best"]


# Order table model


@dataclass
class TableResult:
    ways: int
    buckets: int
    capacity: int
    peak_live: int = 0
    max_bucket_fill: int = 0
    stash_inserts: int = 0  # inserts that found both buckets full
    stash_peak: int = 0
    failed_inserts: int = 0  # both buckets and the stash full


class OrderTableSim:
    """The planned order table for one size: two tables of `buckets` buckets of `ways` slots.

    An insert goes to the emptier of the key's two buckets (table 0 on a tie); if both are
    full, to the stash; if the stash is full too, it fails.
    """

    def __init__(self, buckets: int, ways: int) -> None:
        self.ways = ways
        self.mask = buckets - 1
        self.fill = [[0] * buckets, [0] * buckets]
        self.where: dict[int, tuple[int, int] | None] = {}  # ref -> (table, bucket); None = stash
        self.live = 0
        self.stash = 0
        self.result = TableResult(ways, buckets, 2 * buckets * ways)

    def insert(self, ref: int, hash0: int, hash1: int) -> None:
        bucket0, bucket1 = hash0 & self.mask, hash1 & self.mask
        fill0, fill1 = self.fill[0][bucket0], self.fill[1][bucket1]
        result = self.result
        if min(fill0, fill1) < self.ways:
            table, bucket = (0, bucket0) if fill0 <= fill1 else (1, bucket1)
            self.fill[table][bucket] += 1
            result.max_bucket_fill = max(result.max_bucket_fill, self.fill[table][bucket])
            self.where[ref] = (table, bucket)
        elif self.stash < STASH:
            self.stash += 1
            result.stash_inserts += 1
            result.stash_peak = max(result.stash_peak, self.stash)
            self.where[ref] = None
        else:
            result.failed_inserts += 1
            return
        self.live += 1
        result.peak_live = max(result.peak_live, self.live)

    def delete(self, ref: int) -> None:
        if ref not in self.where:  # its insert failed
            return
        location = self.where.pop(ref)
        if location is None:
            self.stash -= 1
        else:
            table, bucket = location
            self.fill[table][bucket] -= 1
        self.live -= 1


# Books


class Side:
    """Total shares per price for one side of one book, with the best price kept in a heap."""

    def __init__(self, is_bid: bool) -> None:
        self.is_bid = is_bid
        self.levels: dict[int, int] = {}
        self.heap: list[int] = []  # bids stored negated, so the heap top is always the best

    def add(self, price: int, shares: int) -> int:
        """Add shares at price; returns the level's new total."""
        if price not in self.levels:
            self.levels[price] = 0
            heapq.heappush(self.heap, -price if self.is_bid else price)
        self.levels[price] += shares
        return self.levels[price]

    def remove(self, price: int, shares: int) -> None:
        self.levels[price] -= shares
        if self.levels[price] == 0:
            del self.levels[price]  # its heap entry is dropped lazily in best()

    def best(self) -> int | None:
        while self.heap:
            price = -self.heap[0] if self.is_bid else self.heap[0]
            if price in self.levels:
                return price
            heapq.heappop(self.heap)
        return None


@dataclass(slots=True)
class LiveOrder:
    book_id: int
    side: str
    price: int
    shares: int


@dataclass
class WindowResult:
    """One ladder window size W, for one book."""

    low: int  # lowest price inside the window
    high: int  # lowest price above it
    outside_adds: int = 0
    sub_penny_inside: int = 0  # prices the ladder can't hold that lie inside the window
    flagged_messages: int = 0  # messages after which the top lies outside the window
    flagged_regular: int = 0  # the same, during regular hours
    first_flagged: str = ""


@dataclass
class BookResult:
    ticker: str
    reference: int
    messages: dict[str, int] = field(default_factory=dict)  # per message type
    regular_messages: int = 0
    live_orders: int = 0
    peak_live: int = 0
    peak_live_at: str = ""
    peak_bid_levels: int = 0
    peak_ask_levels: int = 0
    min_price: int = 0
    max_price: int = 0
    sub_penny_prices: int = 0
    sub_penny_highest: int = 0
    max_order_shares: int = 0
    max_level_shares: int = 0
    regular_bid_range: list[int] = field(default_factory=list)  # [lowest, highest] best bid
    regular_ask_range: list[int] = field(default_factory=list)
    distance: dict[str, int] = field(default_factory=dict)  # ticks from the best price at insert
    windows: dict[int, WindowResult] = field(default_factory=dict)


def distance_bucket(ticks: int | None) -> str:
    if ticks is None:
        return "no best"
    if ticks < 0:
        return "better"
    if ticks == 0:
        return "0"
    for limit, label in [(10, "1-9"), (100, "10-99"), (1000, "100-999"), (10_000, "1000-9999")]:
        if ticks < limit:
            return label
    return ">=10000"


def window_position(price: int, window: WindowResult) -> int:
    """0 below the window, 1 inside, 2 above."""
    if price < window.low:
        return 0
    if price < window.high:
        return 1
    return 2


class DayStats:
    """Replays a day through ideal books and collects the sizing statistics."""

    def __init__(self, tickers: list[str], references: dict[str, int]) -> None:
        self.tickers = tickers
        self.locate_to_book_id: dict[int, int] = {}
        self.sides = [{"B": Side(True), "S": Side(False)} for _ in tickers]
        self.orders: dict[int, LiveOrder] = {}
        self.hash0 = H3Hash(HASH_SEEDS[0])
        self.hash1 = H3Hash(HASH_SEEDS[1])
        self.tables = [OrderTableSim(buckets, ways) for ways, buckets in TABLE_SIZES]
        # Live orders below / inside / above each window, per window, book and side.
        self.window_counts = {w: [{"B": [0, 0, 0], "S": [0, 0, 0]} for _ in tickers] for w in WINDOWS}

        self.books = []
        for ticker in tickers:
            reference = references[ticker] - references[ticker] % TICK
            book = BookResult(ticker, reference)
            for w in WINDOWS:
                book.windows[w] = WindowResult(reference - w // 2 * TICK, reference + w // 2 * TICK)
            self.books.append(book)

        self.peak_live = 0
        self.peak_live_at = ""
        self.max_order_ref = 0
        self.unknown_refs = 0
        self.inconsistent = 0
        self.time = 0

    def process(self, raw: bytes) -> None:
        msg_type, locate = peek(raw)
        if msg_type == StockDirectory.TYPE:
            msg = decode(raw)
            if msg.stock in self.tickers:
                self.locate_to_book_id[msg.stock_locate] = self.tickers.index(msg.stock)
            return
        book_id = self.locate_to_book_id.get(locate)
        if book_id is None:
            return
        msg = decode(raw)
        if msg is None:
            return

        self.time = msg.timestamp
        regular = OPEN_NS <= self.time < CLOSE_NS
        book = self.books[book_id]
        name = msg_type.decode()
        book.messages[name] = book.messages.get(name, 0) + 1
        if regular:
            book.regular_messages += 1

        if isinstance(msg, AddOrder):  # also F
            self._add(msg.order_ref, LiveOrder(book_id, msg.side, msg.price, msg.shares))
        elif isinstance(msg, OrderExecuted):  # also C
            self._remove(msg.order_ref, msg.executed_shares)
        elif isinstance(msg, OrderCancel):
            self._remove(msg.order_ref, msg.cancelled_shares)
        elif isinstance(msg, OrderDelete):
            self._remove(msg.order_ref, None)
        elif isinstance(msg, OrderReplace):
            old = self.orders.get(msg.original_order_ref)
            if old is None:
                self.unknown_refs += 1
            else:
                self._remove(msg.original_order_ref, None)
                self._add(msg.new_order_ref, LiveOrder(old.book_id, old.side, msg.price, msg.shares))
        else:
            return
        self._after_message(book_id, regular)

    def _add(self, ref: int, order: LiveOrder) -> None:
        if ref in self.orders:
            self.inconsistent += 1
            return
        book = self.books[order.book_id]
        side = self.sides[order.book_id][order.side]

        best = side.best()
        if best is None:
            bucket = distance_bucket(None)
        elif order.side == "B":
            bucket = distance_bucket((best - order.price) // TICK)
        else:
            bucket = distance_bucket((order.price - best) // TICK)
        book.distance[bucket] = book.distance.get(bucket, 0) + 1

        self.orders[ref] = order
        level = side.add(order.price, order.shares)
        book.max_level_shares = max(book.max_level_shares, level)
        book.max_order_shares = max(book.max_order_shares, order.shares)
        book.min_price = min(book.min_price, order.price) if book.min_price else order.price
        book.max_price = max(book.max_price, order.price)
        sub_penny = order.price % TICK != 0
        if sub_penny:
            book.sub_penny_prices += 1
            book.sub_penny_highest = max(book.sub_penny_highest, order.price)
        if order.side == "B":
            book.peak_bid_levels = max(book.peak_bid_levels, len(side.levels))
        else:
            book.peak_ask_levels = max(book.peak_ask_levels, len(side.levels))
        self.max_order_ref = max(self.max_order_ref, ref)

        for w in WINDOWS:
            position = window_position(order.price, book.windows[w])
            self.window_counts[w][order.book_id][order.side][position] += 1
            if position != 1:
                book.windows[w].outside_adds += 1
            elif sub_penny:
                book.windows[w].sub_penny_inside += 1

        hash0, hash1 = self.hash0(ref), self.hash1(ref)
        for table in self.tables:
            table.insert(ref, hash0, hash1)

        book.live_orders += 1
        if book.live_orders > book.peak_live:
            book.peak_live = book.live_orders
            book.peak_live_at = format_time(self.time)
        if len(self.orders) > self.peak_live:
            self.peak_live = len(self.orders)
            self.peak_live_at = format_time(self.time)

    def _remove(self, ref: int, shares: int | None) -> None:
        """Take shares off an order, all remaining ones if shares is None."""
        order = self.orders.get(ref)
        if order is None:
            self.unknown_refs += 1
            return
        if shares is None:
            shares = order.shares
        if shares > order.shares:
            self.inconsistent += 1
            shares = order.shares
        self.sides[order.book_id][order.side].remove(order.price, shares)
        order.shares -= shares
        if order.shares > 0:
            return

        del self.orders[ref]
        book = self.books[order.book_id]
        book.live_orders -= 1
        for w in WINDOWS:
            position = window_position(order.price, book.windows[w])
            self.window_counts[w][order.book_id][order.side][position] -= 1
        for table in self.tables:
            table.delete(ref)

    def _after_message(self, book_id: int, regular: bool) -> None:
        book = self.books[book_id]
        bids, asks = self.sides[book_id]["B"], self.sides[book_id]["S"]

        if regular:
            for side, prices in [(bids, book.regular_bid_range), (asks, book.regular_ask_range)]:
                best = side.best()
                if best is None:
                    continue
                if not prices:
                    prices.extend([best, best])
                prices[0] = min(prices[0], best)
                prices[1] = max(prices[1], best)

        for w in WINDOWS:
            bid_counts = self.window_counts[w][book_id]["B"]
            ask_counts = self.window_counts[w][book_id]["S"]
            # The true best bid is outside the window if a bid sits above it, or if none is
            # inside and some sit below it; mirrored for asks.
            bid_outside = bid_counts[2] > 0 or (bid_counts[1] == 0 and bid_counts[0] > 0)
            ask_outside = ask_counts[0] > 0 or (ask_counts[1] == 0 and ask_counts[2] > 0)
            if bid_outside or ask_outside:
                window = book.windows[w]
                window.flagged_messages += 1
                if regular:
                    window.flagged_regular += 1
                if not window.first_flagged:
                    window.first_flagged = format_time(self.time)


def opening_crosses(messages, tickers: list[str]) -> dict[str, int]:
    """Opening cross price per ticker; stops when all are found or after 09:35."""
    wanted = {ticker.encode("ascii").ljust(8): ticker for ticker in tickers}
    found: dict[str, int] = {}
    for raw in messages:
        if raw[:1] == b"Q":
            *_, stock, price, _, cross_type = struct.unpack(CROSS_TRADE, raw)
            if cross_type == b"O" and stock in wanted:
                found[wanted[stock]] = price
                if len(found) == len(tickers):
                    break
        elif timestamp_of(raw) > OPEN_NS + 5 * 60 * 1_000_000_000:
            break
    return found


# Report


def dollars(price: int) -> str:
    return f"{price / 10_000:.2f}"


def print_report(stats: DayStats, elapsed: float) -> None:
    books = stats.books
    print(f"\nanalysed in {elapsed:.0f} s, up to {format_time(stats.time)}")
    print(f"unknown refs: {stats.unknown_refs:,}   inconsistent: {stats.inconsistent:,}")

    print("\nLIVE ORDERS AND LEVELS")
    print(f"  all tracked books: peak {stats.peak_live:,} at {stats.peak_live_at}")
    print(f"  {'ticker':<6} {'ref $':>9} {'peak live':>10} {'at':>19} {'bid lvls':>9} {'ask lvls':>9} {'messages':>12}")
    for book in books:
        print(f"  {book.ticker:<6} {dollars(book.reference):>9} {book.peak_live:>10,} {book.peak_live_at:>19} "
              f"{book.peak_bid_levels:>9,} {book.peak_ask_levels:>9,} {sum(book.messages.values()):>12,}")

    print("\nPRICES (all orders, all day) AND BEST PRICES (regular hours)")
    print(f"  {'ticker':<6} {'min $':>10} {'max $':>10} {'range ticks':>12} {'sub-penny':>10} {'highest $':>10}   "
          f"{'best bid $ low-high':>21} {'best ask $ low-high':>21} {'best range ticks':>17}")
    for book in books:
        bid, ask = book.regular_bid_range, book.regular_ask_range
        if bid and ask:
            best_range = (max(bid[1], ask[1]) - min(bid[0], ask[0])) // TICK
            bid_text = f"{dollars(bid[0])}-{dollars(bid[1])}"
            ask_text = f"{dollars(ask[0])}-{dollars(ask[1])}"
        else:
            best_range, bid_text, ask_text = 0, "-", "-"
        print(f"  {book.ticker:<6} {dollars(book.min_price):>10} {dollars(book.max_price):>10} "
              f"{(book.max_price - book.min_price) // TICK:>12,} {book.sub_penny_prices:>10,} "
              f"{book.sub_penny_highest / 10_000:>10.4f}   "
              f"{bid_text:>21} {ask_text:>21} {best_range:>17,}")

    print("\nDISTANCE OF NEW ORDERS FROM THE BEST PRICE ON THEIR SIDE (ticks; % of adds)")
    print(f"  {'ticker':<6} " + " ".join(f"{label:>9}" for label in DISTANCE_BUCKETS))
    for book in books:
        total = sum(book.distance.values()) or 1
        print(f"  {book.ticker:<6} " + " ".join(f"{100 * book.distance.get(label, 0) / total:>8.2f}%" for label in DISTANCE_BUCKETS))

    print("\nLADDER WINDOW (centred on the reference price)")
    print("  outside = orders added outside the window; flagged = messages after which the top is outside;")
    print("  sub-penny inside = prices the ladder can't hold, inside the window")
    for w in WINDOWS:
        print(f"  W = {w} ticks (${w * TICK / 10_000:,.2f})")
        for book in books:
            window = book.windows[w]
            total = sum(book.messages.values()) or 1
            regular = book.regular_messages or 1
            print(f"    {book.ticker:<6} ${dollars(window.low)}-${dollars(window.high)}  "
                  f"outside {window.outside_adds:>10,}  flagged {100 * window.flagged_messages / total:6.2f}% all day, "
                  f"{100 * window.flagged_regular / regular:6.2f}% regular hours, "
                  f"sub-penny inside {window.sub_penny_inside:,}"
                  + (f", first at {window.first_flagged}" if window.first_flagged else ""))

    print(f"\nORDER TABLE (2 tables x buckets x slots, stash {STASH}, H3 hash; memory at {ENTRY_BITS} bits per entry)")
    print(f"  {'slots':>5} {'buckets':>8} {'capacity':>9} {'Mbit':>6} {'peak live':>10} {'peak load':>10} "
          f"{'max fill':>9} {'stash ins':>10} {'stash peak':>11} {'failed':>10}")
    for table in stats.tables:
        result = table.result
        print(f"  {result.ways:>5} {result.buckets:>8,} {result.capacity:>9,} "
              f"{result.capacity * ENTRY_BITS / 1e6:>6.1f} {result.peak_live:>10,} "
              f"{100 * result.peak_live / result.capacity:>9.1f}% {result.max_bucket_fill:>9} "
              f"{result.stash_inserts:>10,} {result.stash_peak:>11} {result.failed_inserts:>10,}")

    print("\nBIT WIDTHS")
    print(f"  largest order ref:    {stats.max_order_ref:,} ({stats.max_order_ref.bit_length()} bits)")
    max_order = max(book.max_order_shares for book in books)
    max_level = max(book.max_level_shares for book in books)
    max_price = max(book.max_price for book in books)
    print(f"  largest order:        {max_order:,} shares ({max_order.bit_length()} bits)")
    print(f"  largest level:        {max_level:,} shares ({max_level.bit_length()} bits)")
    print(f"  highest price:        ${dollars(max_price)} ({max_price.bit_length()} bits)")

    print("\nMESSAGES PER TYPE")
    types = sorted({t for book in books for t in book.messages})
    print(f"  {'ticker':<6} " + " ".join(f"{t:>11}" for t in types))
    for book in books:
        print(f"  {book.ticker:<6} " + " ".join(f"{book.messages.get(t, 0):>11,}" for t in types))


def to_json(stats: DayStats) -> dict:
    return {
        "time": format_time(stats.time),
        "peak_live": stats.peak_live,
        "peak_live_at": stats.peak_live_at,
        "max_order_ref": stats.max_order_ref,
        "unknown_refs": stats.unknown_refs,
        "inconsistent": stats.inconsistent,
        "books": [asdict(book) for book in stats.books],
        "order_table": [asdict(table.result) for table in stats.tables],
        "hash_masks": [stats.hash0.masks, stats.hash1.masks],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("itch_file", type=Path)
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--limit", type=int, help="stop after this many messages")
    parser.add_argument("--ref", nargs="+", default=[], metavar="TICKER=PRICE", help="reference price in dollars")
    parser.add_argument("--json", type=Path, help="also write the results as JSON")
    args = parser.parse_args()

    references = {}
    for item in args.ref:
        ticker, price = item.split("=")
        references[ticker] = round(float(price) * 10_000)
    missing = [ticker for ticker in args.tickers if ticker not in references]
    if missing:
        print("pass 1: opening crosses ...")
        with open(args.itch_file, "rb", buffering=1 << 24) as source:
            found = opening_crosses(islice(read_stream(source), args.limit), missing)
        references.update(found)
        if still_missing := [ticker for ticker in missing if ticker not in found]:
            sys.exit(f"no opening cross for {' '.join(still_missing)}; give it with --ref TICKER=PRICE")
    for ticker in args.tickers:
        print(f"  {ticker:<6} reference ${dollars(references[ticker])}")

    print("pass 2: the whole day ...")
    stats = DayStats(args.tickers, references)
    start = time.monotonic()
    with open(args.itch_file, "rb", buffering=1 << 24) as source:
        for count, raw in enumerate(islice(read_stream(source), args.limit), start=1):
            stats.process(raw)
            if count % 50_000_000 == 0:
                print(f"  {count:,} messages, at {format_time(stats.time)}")
    print_report(stats, time.monotonic() - start)

    if args.json:
        args.json.write_text(json.dumps(to_json(stats), indent=1) + "\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
