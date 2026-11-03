"""The hardware order table: two hash tables of buckets with a fixed number of slots, and a
small stash. Same placement rules as the RTL (architecture §8), so the model fails an insert
exactly when the hardware does.

A ref lives in bucket h0 of table 0, bucket h1 of table 1, or the stash. An insert goes into
the bucket with fewer used slots (table 0 on a tie), at its lowest free slot; if both are
full, into the lowest free stash entry; if the stash is full too, it fails.
"""

from dataclasses import dataclass

from itch.hashing import buckets

REF_LIMIT = 1 << 32  # the table stores 32-bit refs


@dataclass(slots=True)
class Entry:
    order_ref: int
    book_id: int
    side: str  # "B" or "S"
    position: str  # "inside", "below", "above" or "unrep"
    index: int  # ladder index; meaningful when inside
    shares: int


@dataclass(frozen=True, slots=True)
class Location:
    table: int  # 0, 1, or STASH_TABLE
    row: int  # bucket, or stash entry
    slot: int


STASH_TABLE = 2


class OrderTable:
    def __init__(self, bucket_bits: int, ways: int, stash: int) -> None:
        self.bucket_bits = bucket_bits
        self.ways = ways
        buckets_per_table = 1 << bucket_bits
        self.tables = [[[None] * ways for _ in range(buckets_per_table)] for _ in range(2)]
        self.stash: list[Entry | None] = [None] * stash
        self.live = 0

    def find(self, order_ref: int) -> Location | None:
        """Where the order is, or None. Refs that don't fit in 32 bits are never stored."""
        if order_ref >= REF_LIMIT:
            return None
        bucket0, bucket1 = buckets(order_ref, self.bucket_bits)
        for table, row in ((0, bucket0), (1, bucket1)):
            for slot, entry in enumerate(self.tables[table][row]):
                if entry is not None and entry.order_ref == order_ref:
                    return Location(table, row, slot)
        for slot, entry in enumerate(self.stash):
            if entry is not None and entry.order_ref == order_ref:
                return Location(STASH_TABLE, 0, slot)
        return None

    def get(self, location: Location) -> Entry:
        if location.table == STASH_TABLE:
            return self.stash[location.slot]
        return self.tables[location.table][location.row][location.slot]

    def insert(self, entry: Entry) -> Location | None:
        """Store a new order (its ref must not be present); None if there is no room."""
        bucket0, bucket1 = buckets(entry.order_ref, self.bucket_bits)
        rows = [self.tables[0][bucket0], self.tables[1][bucket1]]
        used = [sum(slot is not None for slot in row) for row in rows]
        table = 0 if used[0] <= used[1] else 1
        if used[table] < self.ways:
            slot = rows[table].index(None)
            rows[table][slot] = entry
            self.live += 1
            return Location(table, (bucket0, bucket1)[table], slot)
        if None in self.stash:
            slot = self.stash.index(None)
            self.stash[slot] = entry
            self.live += 1
            return Location(STASH_TABLE, 0, slot)
        return None

    def remove(self, location: Location) -> None:
        if location.table == STASH_TABLE:
            self.stash[location.slot] = None
        else:
            self.tables[location.table][location.row][location.slot] = None
        self.live -= 1
