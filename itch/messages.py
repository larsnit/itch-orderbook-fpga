"""Nasdaq TotalView-ITCH 5.0 messages: decode from bytes, encode to bytes.

Only the message types the order book needs are modelled; decode() returns None
for all others. Prices are raw integers with 4 implied decimals (1500000 = $150.00).
Text fields are str; multi-character fields have their trailing space padding removed.

Wire layouts use Python struct codes, always big-endian:
  H = 2-byte int, I = 4-byte int, Q = 8-byte int, Ns = N bytes of ASCII text.
"""

import struct
from dataclasses import astuple, dataclass
from typing import BinaryIO, Iterable, Iterator

# Common to every message: type, stock locate, tracking number, timestamp.
# struct has no 6-byte integer, so the timestamp is read as its high 2 and low 4 bytes.
HEADER = ">1s H H H I"


@dataclass(frozen=True, slots=True)
class Message:
    TYPE = b""  # set by each message type, e.g. b"A"
    BODY = ""  # layout of the fields after the header

    stock_locate: int
    tracking_number: int
    timestamp: int  # ns since midnight

    @classmethod
    def layout(cls) -> str:
        return f"{HEADER} {cls.BODY}"

    @classmethod
    def size(cls) -> int:
        return struct.calcsize(cls.layout())

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Message":
        if len(raw) != cls.size():
            raise ValueError(f"{cls.__name__} must be {cls.size()} bytes, got {len(raw)}")
        _, locate, tracking, ts_high, ts_low, *body = struct.unpack(cls.layout(), raw)
        values = [_text_from_bytes(v) if isinstance(v, bytes) else v for v in body]
        return cls(locate, tracking, ts_high << 32 | ts_low, *values)

    def to_bytes(self) -> bytes:
        locate, tracking, ts, *values = astuple(self)
        body = [
            _text_to_bytes(v, token) if isinstance(v, str) else v
            for v, token in zip(values, self.BODY.split())
        ]
        return struct.pack(self.layout(), self.TYPE, locate, tracking, ts >> 32, ts & 0xFFFF_FFFF, *body)


def _text_from_bytes(raw: bytes) -> str:
    text = raw.decode("ascii")
    # A single space is a real value ("not available"), not padding.
    return text if text == " " else text.rstrip(" ")


def _text_to_bytes(text: str, token: str) -> bytes:
    width = int(token[:-1])  # "8s" -> 8
    raw = text.encode("ascii")
    if len(raw) > width:
        raise ValueError(f"{text!r} does not fit in {width} bytes")
    return raw.ljust(width)


@dataclass(frozen=True, slots=True)
class SystemEvent(Message):
    TYPE = b"S"
    BODY = "1s"

    event_code: str


@dataclass(frozen=True, slots=True)
class StockDirectory(Message):
    TYPE = b"R"
    BODY = "8s 1s 1s I 1s 1s 2s 1s 1s 1s 1s 1s I 1s"

    stock: str
    market_category: str
    financial_status: str
    round_lot_size: int
    round_lots_only: str
    issue_classification: str
    issue_subtype: str
    authenticity: str
    short_sale_threshold: str
    ipo_flag: str
    luld_tier: str
    etp_flag: str
    etp_leverage_factor: int
    inverse_indicator: str


@dataclass(frozen=True, slots=True)
class AddOrder(Message):
    TYPE = b"A"
    BODY = "Q 1s I 8s I"

    order_ref: int
    side: str  # "B" or "S"
    shares: int
    stock: str
    price: int


@dataclass(frozen=True, slots=True)
class AddOrderMPID(AddOrder):
    TYPE = b"F"
    BODY = AddOrder.BODY + " 4s"

    attribution: str


@dataclass(frozen=True, slots=True)
class OrderExecuted(Message):
    TYPE = b"E"
    BODY = "Q I Q"

    order_ref: int
    executed_shares: int
    match_number: int


@dataclass(frozen=True, slots=True)
class OrderExecutedWithPrice(OrderExecuted):
    TYPE = b"C"
    BODY = OrderExecuted.BODY + " 1s I"

    printable: str
    execution_price: int


@dataclass(frozen=True, slots=True)
class OrderCancel(Message):
    TYPE = b"X"
    BODY = "Q I"

    order_ref: int
    cancelled_shares: int


@dataclass(frozen=True, slots=True)
class OrderDelete(Message):
    TYPE = b"D"
    BODY = "Q"

    order_ref: int


@dataclass(frozen=True, slots=True)
class OrderReplace(Message):
    TYPE = b"U"
    BODY = "Q Q I I"

    original_order_ref: int
    new_order_ref: int
    shares: int
    price: int


MESSAGE_TYPES = {
    cls.TYPE: cls
    for cls in (
        SystemEvent,
        StockDirectory,
        AddOrder,
        AddOrderMPID,
        OrderExecuted,
        OrderExecutedWithPrice,
        OrderCancel,
        OrderDelete,
        OrderReplace,
    )
}


def decode(raw: bytes) -> Message | None:
    """Decode any message; None for types the order book doesn't use."""
    cls = MESSAGE_TYPES.get(raw[:1])
    return cls.from_bytes(raw) if cls else None


def peek(raw: bytes) -> tuple[bytes, int]:
    """Type and stock locate without decoding the rest; cheap enough to filter a full day."""
    return raw[:1], int.from_bytes(raw[1:3], "big")


def timestamp_of(raw: bytes) -> int:
    """Timestamp (ns since midnight) without decoding the rest."""
    return int.from_bytes(raw[5:11], "big")


def read_stream(f: BinaryIO) -> Iterator[bytes]:
    """Raw messages from a stream of 2-byte length prefixes and messages (the Nasdaq file format)."""
    while header := f.read(2):
        if len(header) != 2:
            raise EOFError("stream ends inside a length prefix")
        length = int.from_bytes(header, "big")
        raw = f.read(length)
        if len(raw) != length:
            raise EOFError("stream ends inside a message")
        yield raw


def write_stream(f: BinaryIO, messages: Iterable[bytes]) -> None:
    for raw in messages:
        f.write(len(raw).to_bytes(2, "big"))
        f.write(raw)