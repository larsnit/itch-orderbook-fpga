"""The module's output: 32-byte records.

A book record each time a book's top of book or flags change; a status record (book id
0xFF, words 2 and 3 zero) when all books become stale.

    word 0   book id (8 bit) | flags (8 bit) | timestamp (48 bit)
    word 1   sequence number of the message that caused it
    word 2   bid price (32 bit) | bid shares (32 bit)
    word 3   ask price (32 bit) | ask shares (32 bit)

Flags: bit 0 stale, bit 1 top outside window, bit 2 gap, bit 3 bad FCS, bit 4 unusable
message (bits 2-4 only in status records).
"""

import struct
from dataclasses import dataclass

from itch.book import TopOfBook

# As in ITCH, the 48-bit timestamp is packed as its high 2 and low 4 bytes.
LAYOUT = ">B B H I Q I I I I"
SIZE = struct.calcsize(LAYOUT)

STALE = 0x01
OUTSIDE_WINDOW = 0x02
GAP = 0x04
BAD_FCS = 0x08
UNUSABLE = 0x10

STATUS_BOOK_ID = 0xFF
STATUS_TOP = TopOfBook(0, 0, 0, 0)  # words 2 and 3 of a status record


@dataclass(frozen=True, slots=True)
class Record:
    book_id: int
    flags: int
    timestamp: int
    seq: int
    top: TopOfBook

    @property
    def stale(self) -> bool:
        return bool(self.flags & STALE)

    def to_bytes(self) -> bytes:
        ts, top = self.timestamp, self.top
        return struct.pack(
            LAYOUT,
            self.book_id,
            self.flags,
            ts >> 32,
            ts & 0xFFFF_FFFF,
            self.seq,
            top.bid_price,
            top.bid_shares,
            top.ask_price,
            top.ask_shares,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Record":
        book_id, flags, ts_high, ts_low, seq, *top = struct.unpack(LAYOUT, raw)
        return cls(book_id, flags, ts_high << 32 | ts_low, seq, TopOfBook(*top))
