"""Order books for the tracked symbols: the table of resting orders and each book's price levels.

This is the ideal reference: unlimited orders and price levels, no hardware sizes.
"""

from dataclasses import dataclass

from itch.messages import AddOrder, Message, OrderCancel, OrderDelete, OrderExecuted, OrderReplace

BID = "B"
ASK = "S"

# How an empty side appears in the output record.
NO_BID_PRICE = 0
NO_ASK_PRICE = 0xFFFF_FFFF


@dataclass(frozen=True, slots=True)
class TopOfBook:
    bid_price: int = NO_BID_PRICE
    bid_shares: int = 0
    ask_price: int = NO_ASK_PRICE
    ask_shares: int = 0


@dataclass(slots=True)
class Order:
    book_id: int
    side: str
    price: int
    shares: int


class Book:
    """Total resting shares at each price, per side, for one symbol."""

    def __init__(self) -> None:
        self.levels: dict[str, dict[int, int]] = {BID: {}, ASK: {}}

    def add(self, side: str, price: int, shares: int) -> None:
        levels = self.levels[side]
        levels[price] = levels.get(price, 0) + shares

    def remove(self, side: str, price: int, shares: int) -> None:
        levels = self.levels[side]
        levels[price] -= shares
        if levels[price] == 0:
            del levels[price]

    def top(self) -> TopOfBook:
        bids, asks = self.levels[BID], self.levels[ASK]
        best_bid = max(bids, default=None)
        best_ask = min(asks, default=None)
        return TopOfBook(
            bid_price=NO_BID_PRICE if best_bid is None else best_bid,
            bid_shares=bids.get(best_bid, 0),
            ask_price=NO_ASK_PRICE if best_ask is None else best_ask,
            ask_shares=asks.get(best_ask, 0),
        )


class Books:
    """All tracked books plus the order table, updated one message at a time.

    Messages for unknown order refs are ignored and counted: they happen after a sequence
    gap, when the add that created the order was lost.

    Other contradictions can't come from lost messages (losing messages only leaves orders
    with too many shares, or unknown), only from corrupted ones, which the unchecked UDP
    checksum lets through. They are counted as inconsistent and handled the way the
    hardware can, without raising: an add or replace for a ref already in the table is
    ignored, and removing more shares than an order has left removes all of them.
    """

    def __init__(self, num_books: int) -> None:
        self.by_id = [Book() for _ in range(num_books)]
        self.orders: dict[int, Order] = {}
        self.unknown_refs = 0
        self.inconsistent = 0

    def apply(self, msg: Message, book_id: int) -> int | None:
        """Process one message for a tracked symbol; book_id is that symbol's book.

        Returns the id of the book that changed, or None if none did.
        """
        if isinstance(msg, AddOrder):  # also F
            return self._add(msg.order_ref, Order(book_id, msg.side, msg.price, msg.shares))
        if isinstance(msg, OrderExecuted):  # also C; its execution price doesn't affect the book
            return self._remove(msg.order_ref, msg.executed_shares)
        if isinstance(msg, OrderCancel):
            return self._remove(msg.order_ref, msg.cancelled_shares)
        if isinstance(msg, OrderDelete):
            return self._remove(msg.order_ref)
        if isinstance(msg, OrderReplace):
            return self._replace(msg.original_order_ref, msg.new_order_ref, msg.price, msg.shares)
        return None

    def _add(self, ref: int, order: Order) -> int | None:
        if ref in self.orders:
            self.inconsistent += 1
            return None
        self.orders[ref] = order
        self.by_id[order.book_id].add(order.side, order.price, order.shares)
        return order.book_id

    def _remove(self, ref: int, shares: int | None = None) -> int | None:
        """Take shares off an order, all remaining ones if shares is None."""
        order = self.orders.get(ref)
        if order is None:
            self.unknown_refs += 1
            return None
        if shares is None:
            shares = order.shares
        if shares > order.shares:
            self.inconsistent += 1
            shares = order.shares

        self.by_id[order.book_id].remove(order.side, order.price, shares)
        order.shares -= shares
        if order.shares == 0:
            del self.orders[ref]
        return order.book_id

    def _replace(self, old_ref: int, new_ref: int, price: int, shares: int) -> int | None:
        """The new order keeps the old one's book and side."""
        old = self.orders.get(old_ref)
        if old is None:
            self.unknown_refs += 1
            return None
        self._remove(old_ref)
        self._add(new_ref, Order(old.book_id, old.side, price, shares))  # may be ignored; old is gone either way
        return old.book_id