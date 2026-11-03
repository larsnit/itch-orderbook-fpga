"""Hardware model: exactly what the RTL outputs, rule for rule (architecture §13 and §16).

Unlike the ideal model (model.py), it has the hardware's limits and flags: a fixed price
window per book, an order table that can fill up, per-book stale bits, status records, and
messages whose length doesn't match their type. It is built from the same stages as the RTL,
and can record each stage's output ("taps") so every RTL module can be tested on its own:

    process_frame   packet parser + framer     → tokens
    process         decoder                    → operations (a U becomes two)
    _order_table    order table                → order-table results
    _book_engine    book engine                → tops (one per operation)
    _output         output stage               → book records; status records come from
                                                 the decoder, on their own channel

Every method returns (records, events): book records and status records.
"""

from dataclasses import dataclass, field

from itch.book import TopOfBook
from itch.config import Config
from itch.framing import END_OF_SESSION, frame_payload, read_packet
from itch.hashing import buckets
from itch.ladder import INSIDE, UNREP, LadderSide, position_of
from itch.messages import (
    AddOrder,
    AddOrderMPID,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    StockDirectory,
    decode,
    timestamp_of,
)
from itch.order_table import REF_LIMIT, STASH_TABLE, Entry, OrderTable
from itch.record import BAD_FCS, GAP, OUTSIDE_WINDOW, STALE, STATUS_BOOK_ID, STATUS_TOP, UNUSABLE, Record

# Types whose fields the hardware reads at fixed offsets: their length must match.
EXPECTED_LENGTH = {
    cls.TYPE: cls.size()
    for cls in (AddOrder, AddOrderMPID, OrderExecuted, OrderExecutedWithPrice, OrderCancel, OrderDelete, OrderReplace, StockDirectory)
}
BOOK_TYPES = {AddOrder.TYPE, AddOrderMPID.TYPE, OrderExecuted.TYPE, OrderExecutedWithPrice.TYPE, OrderCancel.TYPE, OrderDelete.TYPE, OrderReplace.TYPE}
MSG_BYTES = 40  # message bytes the framer keeps

COUNTERS = (
    "dropped_frames",
    "malformed_packets",
    "fcs_errors",
    "duplicates",
    "gaps",
    "unusable_messages",
    "insert_failures",
    "ref_errors",
    "unknown_refs",
    "inconsistent",
    "off_ladder_orders",
    "unrep_prices",
    "stash_inserts",
    "messages",
    "records",
    "events",
)


# Stage outputs, with the fields of the VHDL records (implementation §3.4)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str  # "MSG", "HEARTBEAT", "FCS"
    seq: int = 0
    length: int = 0
    data: bytes = b""  # message bytes 0-39


@dataclass(frozen=True, slots=True)
class Op:
    kind: str  # "INSERT", "REMOVE", "DELETE", "U_DELETE", "U_INSERT"
    book_id: int
    side: str  # INSERT only ("B" or "S"); "" otherwise
    order_ref: int  # the full ref from the message
    bucket0: int  # its bucket in table 0 and table 1 (hash of all 64 bits)
    bucket1: int
    shares: int
    price: int
    seq: int
    timestamp: int
    stale_all: bool
    record_enable: bool

    @property
    def ref_error(self) -> bool:
        return self.order_ref >= REF_LIMIT


@dataclass(frozen=True, slots=True)
class OtResult:
    change: str  # "NONE", "ADD", "REMOVE"
    book_id: int
    side: str
    position: str
    index: int
    shares: int
    order_gone: bool
    book_stale: bool
    stale_all: bool
    record_enable: bool
    seq: int
    timestamp: int


@dataclass(frozen=True, slots=True)
class Top:
    book_id: int
    flags: int
    top: TopOfBook
    record_enable: bool
    seq: int
    timestamp: int


@dataclass
class Taps:
    tokens: list[Token] = field(default_factory=list)
    ops: list[Op] = field(default_factory=list)
    ot_results: list[OtResult] = field(default_factory=list)
    tops: list[Top] = field(default_factory=list)


class HardwareModel:
    def __init__(self, config: Config, taps: bool = False) -> None:
        self.config = config
        self.table = OrderTable(config.bucket_bits, config.ways, config.stash)
        self.sides = []
        for book_id in range(len(config.tickers)):
            base = config.base_cents(book_id)
            self.sides.append({"B": LadderSide(True, base), "S": LadderSide(False, base)})
        self.book_locates: list[int | None] = [None] * len(config.tickers)  # one register per book, from R messages
        self.next_seq = 1
        self.last_timestamp = 0  # of the last usable message in sequence
        self.stale_all = False
        self.book_stale = [False] * len(config.tickers)
        empty = (TopOfBook(), 0)
        self.last_emitted = [empty] * len(config.tickers)  # (top, flags 0-1) of each book's last record
        self.u_old: Entry | None = None  # the order U_DELETE removed, for U_INSERT
        self.counters = dict.fromkeys(COUNTERS, 0)
        self.taps = Taps() if taps else None

    # Packet parser and framer

    def process_frame(self, frame: bytes, fcs_error: bool = False) -> tuple[list[Record], list[Record]]:
        if fcs_error:
            self.counters["fcs_errors"] += 1
        payload = frame_payload(frame, self.config.feed)
        parsed = read_packet(payload) if payload is not None else None
        if parsed is None:
            self.counters["dropped_frames"] += 1
            return [], []
        packet, well_formed = parsed
        if not well_formed:
            self.counters["malformed_packets"] += 1

        records, events = [], []
        count = int.from_bytes(payload[18:20], "big")
        if count in (0, END_OF_SESSION):
            self._tap_token(Token("HEARTBEAT", packet.seq))
            events += self.heartbeat(packet.seq)
        for offset, raw in enumerate(packet.messages):
            self._tap_token(Token("MSG", packet.seq + offset, len(raw), raw[:MSG_BYTES]))
            more_records, more_events = self.process(packet.seq + offset, raw)
            records += more_records
            events += more_events

        if fcs_error:  # the frame was accepted: its messages are applied, one may be corrupt
            self._tap_token(Token("FCS"))
            events += self._go_stale(BAD_FCS, self.next_seq - 1, self.last_timestamp)
        return records, events

    # Decoder

    def heartbeat(self, seq: int) -> list[Record]:
        """A packet without messages; seq is that of the next message."""
        if seq <= self.next_seq:
            return []
        self.counters["gaps"] += 1
        events = self._go_stale(GAP, seq, self.last_timestamp)
        self.next_seq = seq
        return events

    def process(self, seq: int, raw: bytes) -> tuple[list[Record], list[Record]]:
        """One message, in arrival order."""
        if seq < self.next_seq:
            self.counters["duplicates"] += 1
            return [], []
        gap = seq > self.next_seq
        self.next_seq = seq + 1
        self.counters["messages"] += 1

        msg_type = raw[:1]
        usable = msg_type not in EXPECTED_LENGTH or len(raw) == EXPECTED_LENGTH[msg_type]
        timestamp = timestamp_of(raw) if usable else self.last_timestamp
        events = []
        if gap:
            self.counters["gaps"] += 1
            events += self._go_stale(GAP, seq, timestamp)
        if not usable:
            self.counters["unusable_messages"] += 1
            events += self._go_stale(UNUSABLE, seq, timestamp)
            return [], events
        self.last_timestamp = timestamp

        if msg_type == StockDirectory.TYPE:
            msg = decode(raw)
            if msg.stock in self.config.tickers:
                self.book_locates[self.config.tickers.index(msg.stock)] = msg.stock_locate
            return [], events
        if msg_type not in BOOK_TYPES:
            return [], events
        book_id = self.book_of(int.from_bytes(raw[1:3], "big"))
        if book_id is None:
            return [], events

        records = []
        for op in self._operations(decode(raw), book_id, seq, timestamp):
            if self.taps:
                self.taps.ops.append(op)
            result = self._order_table(op)
            top = self._book_engine(result)
            record = self._output(top)
            if record:
                records.append(record)
        return records, events

    def _operations(self, msg, book_id: int, seq: int, timestamp: int) -> list[Op]:
        def op(kind, side, order_ref, shares=0, price=0, record_enable=True):
            bucket0, bucket1 = buckets(order_ref, self.config.bucket_bits)
            return Op(
                kind, book_id, side, order_ref, bucket0, bucket1, shares, price,
                seq, timestamp, self.stale_all, record_enable,
            )

        if isinstance(msg, AddOrder):  # also F
            return [op("INSERT", msg.side, msg.order_ref, msg.shares, msg.price)]
        if isinstance(msg, OrderExecuted):  # also C
            return [op("REMOVE", "", msg.order_ref, msg.executed_shares)]
        if isinstance(msg, OrderCancel):
            return [op("REMOVE", "", msg.order_ref, msg.cancelled_shares)]
        if isinstance(msg, OrderDelete):
            return [op("DELETE", "", msg.order_ref)]
        return [  # OrderReplace
            op("U_DELETE", "", msg.original_order_ref, record_enable=False),
            op("U_INSERT", "", msg.new_order_ref, msg.shares, msg.price),
        ]

    def _go_stale(self, reason: int, seq: int, timestamp: int) -> list[Record]:
        """All books stale; the first time, one status record."""
        if self.stale_all:
            return []
        self.stale_all = True
        self.counters["events"] += 1
        return [Record(STATUS_BOOK_ID, STALE | reason, timestamp, seq, STATUS_TOP)]

    # Order table

    def _order_table(self, op: Op) -> OtResult:
        def result(change="NONE", book_id=op.book_id, side="B", position=INSIDE, index=0, shares=0, gone=False):
            return OtResult(
                change, book_id, side, position, index, shares, gone,
                self.book_stale[book_id], op.stale_all, op.record_enable, op.seq, op.timestamp,
            )

        if op.kind == "INSERT":
            outcome = self._insert(op, op.book_id, op.side)
        elif op.kind == "U_INSERT":
            old = self.u_old
            if old is None:  # U_DELETE found nothing; already counted
                outcome = None
            elif old.book_id != op.book_id:  # corrupt data: both books may be wrong now
                self._anomaly("inconsistent", old.book_id)
                self.book_stale[op.book_id] = True
                outcome = None
            else:
                outcome = self._insert(op, op.book_id, old.side)
        else:
            outcome = self._remove(op)

        built = result(*outcome) if outcome else result()
        if self.taps:
            self.taps.ot_results.append(built)
        return built

    def _insert(self, op: Op, book_id: int, side: str) -> tuple | None:
        if op.ref_error:
            self._anomaly("ref_errors", book_id)
            return None
        if self.table.find(op.order_ref) is not None:
            self._anomaly("inconsistent", book_id)
            return None
        position, index = position_of(op.price, self.config.base_cents(book_id), self.config.window)
        location = self.table.insert(Entry(op.order_ref, book_id, side, position, index, op.shares))
        if location is None:
            self._anomaly("insert_failures", book_id)
            return None
        if location.table == STASH_TABLE:
            self.counters["stash_inserts"] += 1
        if position == UNREP:
            self.counters["unrep_prices"] += 1
        elif position != INSIDE:
            self.counters["off_ladder_orders"] += 1
        return ("ADD", book_id, side, position, index, op.shares, False)

    def _remove(self, op: Op) -> tuple | None:
        """REMOVE (some shares), DELETE and U_DELETE (all of them)."""
        if op.kind == "U_DELETE":
            self.u_old = None
        location = self.table.find(op.order_ref)
        if location is None:
            self._anomaly("unknown_refs", op.book_id)
            return None
        entry = self.table.get(location)
        shares = entry.shares if op.kind != "REMOVE" else op.shares
        if shares > entry.shares:
            self._anomaly("inconsistent", entry.book_id)
            shares = entry.shares
        entry.shares -= shares
        gone = entry.shares == 0
        if gone:
            self.table.remove(location)
        if op.kind == "U_DELETE":
            self.u_old = entry
        return ("REMOVE", entry.book_id, entry.side, entry.position, entry.index, shares, gone)

    def _anomaly(self, counter: str, book_id: int) -> None:
        """An order of this book is missing or wrong: count it, and mark the book stale."""
        self.counters[counter] += 1
        self.book_stale[book_id] = True

    # Book engine

    def _book_engine(self, result: OtResult) -> Top:
        side = self.sides[result.book_id][result.side]
        if result.change == "ADD":
            if result.position == INSIDE:
                side.add(result.index, result.shares)
            else:
                side.off_ladder[result.position] += 1
        elif result.change == "REMOVE":
            if result.position == INSIDE:
                side.remove(result.index, result.shares)
            elif result.order_gone:
                side.off_ladder[result.position] -= 1

        bids, asks = self.sides[result.book_id]["B"], self.sides[result.book_id]["S"]
        flags = 0
        if result.stale_all or result.book_stale:
            flags |= STALE
        if bids.outside() or asks.outside():
            flags |= OUTSIDE_WINDOW
        top = Top(
            result.book_id, flags, TopOfBook(*bids.top(), *asks.top()),
            result.record_enable, result.seq, result.timestamp,
        )
        if self.taps:
            self.taps.tops.append(top)
        return top

    # Output stage

    def _output(self, top: Top) -> Record | None:
        if not top.record_enable:
            return None
        state = (top.top, top.flags & (STALE | OUTSIDE_WINDOW))
        if state == self.last_emitted[top.book_id]:
            return None
        self.last_emitted[top.book_id] = state
        self.counters["records"] += 1
        return Record(top.book_id, top.flags, top.timestamp, top.seq, top.top)

    def _tap_token(self, token: Token) -> None:
        if self.taps:
            self.taps.tokens.append(token)

    def book_of(self, locate: int) -> int | None:
        """The book whose register holds this locate; the lowest book id if several do."""
        for book_id, book_locate in enumerate(self.book_locates):
            if book_locate == locate:
                return book_id
        return None

    def missing_tickers(self) -> list[str]:
        return [t for t, locate in zip(self.config.tickers, self.book_locates) if locate is None]
