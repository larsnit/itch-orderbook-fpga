"""One book side in hardware: a ladder of shares per cent over a fixed window, plus counters
of the orders that are not on it (architecture §9).

Prices are ITCH integers (4 implied decimals); the ladder works in cents. A price is:
    below          under the window
    above          at or over the window's end
    unrep          inside, but not a whole cent (sub-penny at $1 or more: can't be on the ladder)
    inside         on the ladder, at index cents - base
"""

import heapq

from itch.book import NO_ASK_PRICE, NO_BID_PRICE

INSIDE = "inside"
BELOW = "below"
ABOVE = "above"
UNREP = "unrep"

SHARES_MASK = 0xFFFF_FFFF  # a level holds 32 bits, as in the RAM


def position_of(price: int, base_cents: int, window: int) -> tuple[str, int]:
    """(position, ladder index) of a price; the index is 0 unless the position is inside."""
    cents, remainder = divmod(price, 100)
    if cents < base_cents:
        return BELOW, 0
    if cents >= base_cents + window:
        return ABOVE, 0
    if remainder != 0:
        return UNREP, 0
    return INSIDE, cents - base_cents


class LadderSide:
    def __init__(self, is_bid: bool, base_cents: int) -> None:
        self.is_bid = is_bid
        self.base_cents = base_cents
        self.levels: dict[int, int] = {}  # index -> shares, only non-empty levels
        self.heap: list[int] = []  # bids stored negated, so the heap top is always the best
        self.off_ladder = {BELOW: 0, ABOVE: 0, UNREP: 0}

    def add(self, index: int, shares: int) -> None:
        new = (self.levels.get(index, 0) + shares) & SHARES_MASK
        self._set(index, new)

    def remove(self, index: int, shares: int) -> None:
        new = (self.levels.get(index, 0) - shares) & SHARES_MASK
        self._set(index, new)

    def _set(self, index: int, shares: int) -> None:
        if shares == 0:
            self.levels.pop(index, None)
        else:
            if index not in self.levels:
                heapq.heappush(self.heap, -index if self.is_bid else index)
            self.levels[index] = shares

    def best(self) -> int | None:
        """Index of the best non-empty level, or None if the ladder side is empty."""
        while self.heap:
            index = -self.heap[0] if self.is_bid else self.heap[0]
            if index in self.levels:
                return index
            heapq.heappop(self.heap)
        return None

    def top(self) -> tuple[int, int]:
        """(price, shares) of the best level as the record shows it, or the empty values."""
        index = self.best()
        if index is None:
            return (NO_BID_PRICE if self.is_bid else NO_ASK_PRICE), 0
        return (self.base_cents + index) * 100, self.levels[index]

    def outside(self) -> bool:
        """True if the true best price of this side may lie outside the window."""
        if self.off_ladder[UNREP] > 0:
            return True
        better, worse = (ABOVE, BELOW) if self.is_bid else (BELOW, ABOVE)
        return self.off_ladder[better] > 0 or (self.best() is None and self.off_ladder[worse] > 0)
