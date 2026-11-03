"""Print the records in a record-output capture one per line, readable, next to the model's,
each below the input message that caused it.

    python3.11 scripts/dump_capture.py data/results/records_min_book.pcap min_book
    python3.11 scripts/dump_capture.py data/results/records_clean.pcap clean --limit 20
    python3.11 scripts/dump_capture.py data/results/records_gap.pcap gap --frames

Record frames (implementation 12.8a): Ethernet 14, IPv4 20, UDP 8, batch header 14
(0x4952, frame counter 32, record count 16, records dropped 32, 0x0000), then 32-byte records.
The expected records come from the hardware model over the vector's frames as the NIC sends
them (good FCS, padded to 60 bytes), replayed --loops times.

Per record two lines:
  in   the ITCH message with the record's sequence number, decoded from the vector's frames
       (ticker from the vector's R messages, prices in dollars with 4 decimals as in ITCH)
  out  frame counter, seq, book, flags (S stale, W top outside window, G gap, F bad FCS,
       U unusable message), ITCH timestamp, best bid and ask as price x shares, and the check
       against the model's record at the same position (book records and status records each
       in their own order)
"""

import argparse
import signal
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itch.config import Config  # noqa: E402
from itch.framing import frame_payload, read_packet  # noqa: E402
from itch.hw_model import EXPECTED_LENGTH, HardwareModel  # noqa: E402
from itch.messages import (  # noqa: E402
    AddOrder,
    AddOrderMPID,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    StockDirectory,
    SystemEvent,
    decode,
)
from itch.pcap import read_pcap  # noqa: E402
from itch.record import BAD_FCS, GAP, OUTSIDE_WINDOW, STALE, STATUS_BOOK_ID, UNUSABLE, Record  # noqa: E402

BATCH_MAGIC = 0x4952
HEADER_BYTES = 14 + 20 + 8
BATCH_HEADER = ">H I H I H"
BATCH_HEADER_BYTES = struct.calcsize(BATCH_HEADER)
RECORD_BYTES = 32
MIN_FRAME = 60
NO_ASK_PRICE = 0xFFFF_FFFF
FLAG_LETTERS = ((STALE, "S"), (OUTSIDE_WINDOW, "W"), (GAP, "G"), (BAD_FCS, "F"), (UNUSABLE, "U"))


def record_frames(capture: Path, port: int) -> list[tuple[int, int, list[Record]]]:
    """(frame counter, records dropped so far, records) per record frame in the capture."""
    frames = []
    with open(capture, "rb") as f:
        for _, data in read_pcap(f):
            if len(data) < HEADER_BYTES + BATCH_HEADER_BYTES or data[12:14] != b"\x08\x00" or data[23] != 17:
                continue
            if int.from_bytes(data[36:38], "big") != port:
                continue
            magic, counter, count, dropped, _ = struct.unpack_from(BATCH_HEADER, data, HEADER_BYTES)
            if magic != BATCH_MAGIC:
                continue
            start = HEADER_BYTES + BATCH_HEADER_BYTES
            records = [Record.from_bytes(data[start + i * RECORD_BYTES:start + (i + 1) * RECORD_BYTES])
                       for i in range(count)]
            frames.append((counter, dropped, records))
    return frames


def wire_frames_of(vector_dir: Path, case: str) -> list[bytes]:
    with open(vector_dir / f"{case}.pcap", "rb") as f:
        return [data.ljust(MIN_FRAME, b"\x00") for _, data in read_pcap(f)]


def expected_records(wire_frames: list[bytes], config: Config, loops: int) -> tuple[list[Record], list[Record]]:
    model = HardwareModel(config)
    records, events = [], []
    for _ in range(loops):
        for frame in wire_frames:
            more_records, more_events = model.process_frame(frame, False)
            records += more_records
            events += more_events
    return records, events


def messages_by_seq(wire_frames: list[bytes], config: Config) -> tuple[dict[int, tuple[int, bytes]], dict[int, str], dict[int, int]]:
    """(frame index, raw message) per sequence number at its first arrival, stock per locate from
    the R messages, and the frame index of the first heartbeat packet per sequence number."""
    messages: dict[int, tuple[int, bytes]] = {}
    stocks: dict[int, str] = {}
    heartbeats: dict[int, int] = {}
    for frame_index, frame in enumerate(wire_frames):
        payload = frame_payload(frame, config.feed)
        parsed = read_packet(payload) if payload is not None else None
        if parsed is None:
            continue
        packet, _ = parsed
        if not packet.messages:
            heartbeats.setdefault(packet.seq, frame_index)
        for offset, raw in enumerate(packet.messages):
            messages.setdefault(packet.seq + offset, (frame_index, raw))
            if raw[:1] == StockDirectory.TYPE and len(raw) == StockDirectory.size():
                directory = StockDirectory.from_bytes(raw)
                stocks.setdefault(directory.stock_locate, directory.stock)
    return messages, stocks, heartbeats


def price_text(price: int) -> str:
    return f"{price / 10_000:.4f}"


def describe_message(raw: bytes, stocks: dict[int, str]) -> str:
    type_letter = raw[:1].decode("ascii", "replace")
    expected_length = EXPECTED_LENGTH.get(raw[:1])
    if expected_length is not None and len(raw) != expected_length:
        return f"{type_letter}  unusable: {len(raw)} bytes, a {type_letter} has {expected_length}"
    msg = decode(raw)
    if msg is None:
        return f"{type_letter}  {len(raw)} bytes (type not used by the book)"
    stock = stocks.get(msg.stock_locate, f"locate {msg.stock_locate}")
    side = {"B": "buy", "S": "sell"}
    if isinstance(msg, SystemEvent):
        return f"S  system event '{msg.event_code}'"
    if isinstance(msg, StockDirectory):
        return f"R  {msg.stock:<6} directory: locate {msg.stock_locate}"
    if isinstance(msg, AddOrderMPID):
        return (f"F  {stock:<6} add ref {msg.order_ref}: {side.get(msg.side, msg.side)} {msg.shares} @ "
                f"{price_text(msg.price)} ({msg.attribution})")
    if isinstance(msg, AddOrder):
        return f"A  {stock:<6} add ref {msg.order_ref}: {side.get(msg.side, msg.side)} {msg.shares} @ {price_text(msg.price)}"
    if isinstance(msg, OrderExecutedWithPrice):
        return (f"C  {stock:<6} execute ref {msg.order_ref}: {msg.executed_shares} shares "
                f"(at {price_text(msg.execution_price)})")
    if isinstance(msg, OrderExecuted):
        return f"E  {stock:<6} execute ref {msg.order_ref}: {msg.executed_shares} shares"
    if isinstance(msg, OrderCancel):
        return f"X  {stock:<6} cancel ref {msg.order_ref}: {msg.cancelled_shares} shares"
    if isinstance(msg, OrderDelete):
        return f"D  {stock:<6} delete ref {msg.order_ref}"
    if isinstance(msg, OrderReplace):
        return (f"U  {stock:<6} replace ref {msg.original_order_ref} -> ref {msg.new_order_ref}: "
                f"{msg.shares} @ {price_text(msg.price)}")
    return f"{type_letter}  {msg}"


def input_text(record: Record, messages: dict[int, tuple[int, bytes]], stocks: dict[int, str],
               heartbeats: dict[int, int]) -> str:
    message = messages.get(record.seq)
    heartbeat_index = heartbeats.get(record.seq)
    revealed_by_heartbeat = record.book_id == STATUS_BOOK_ID and heartbeat_index is not None and (
        message is None or heartbeat_index < message[0])
    if revealed_by_heartbeat:
        return "heartbeat (packet without messages) announcing this sequence number"
    if message is not None:
        return describe_message(message[1], stocks)
    return "no message with this sequence number in the vector (lost before the board)"


def flags_text(flags: int) -> str:
    return "".join(letter for bit, letter in FLAG_LETTERS if flags & bit) or "-"


def time_text(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{nanoseconds:09}"


def side_text(price: int, shares: int, empty_price: int) -> str:
    if price == empty_price and shares == 0:
        return "-"
    return f"{price / 10_000:.2f} x {shares}"


def book_text(record: Record, tickers: list[str]) -> str:
    if record.book_id == STATUS_BOOK_ID:
        return "STATUS"
    return tickers[record.book_id] if record.book_id < len(tickers) else f"book {record.book_id}"


def record_text(record: Record, tickers: list[str]) -> str:
    top = record.top
    if record.book_id == STATUS_BOOK_ID:
        sides = ""
    else:
        sides = f"{side_text(top.bid_price, top.bid_shares, 0):>18}  {side_text(top.ask_price, top.ask_shares, NO_ASK_PRICE):>18}"
    return f"{record.seq:>7}  {book_text(record, tickers):<6}  {flags_text(record.flags):<5}  {time_text(record.timestamp)}  {sides}"


def check_text(got: Record, want: Record | None) -> str:
    if want is None:
        return "EXTRA (model has no record here)"
    if got == want:
        return "ok"
    fields = [name for name in ("book_id", "flags", "timestamp", "seq", "top") if getattr(got, name) != getattr(want, name)]
    return f"DIFF {', '.join(fields)}"


def main() -> None:
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", type=Path, help="tcpdump capture of the record frames")
    parser.add_argument("case", help="vector name, e.g. clean")
    parser.add_argument("--loops", type=int, default=1, help="how often tcpreplay played it")
    parser.add_argument("--vectors", type=Path, default=Path("data/vectors"))
    parser.add_argument("--port", type=int, default=5000, help="record frames' UDP destination port")
    parser.add_argument("--limit", type=int, help="print at most this many records")
    parser.add_argument("--frames", action="store_true", help="also print a line per record frame")
    args = parser.parse_args()

    frames = record_frames(args.capture, args.port)
    config = Config.load(args.vectors / "config.json")
    wire_frames = wire_frames_of(args.vectors, args.case)
    want_records, want_events = expected_records(wire_frames, config, args.loops)
    messages, stocks, heartbeats = messages_by_seq(wire_frames, config)
    tickers = config.tickers

    print(f"{'in':<4}{'':>6}  {'seq':>7}  message")
    print(f"{'out':<4}{'frame':>6}  {'seq':>7}  {'book':<6}  {'flags':<5}  {'timestamp':<18}  "
          f"{'bid':>18}  {'ask':>18}  check")
    book_index = event_index = printed = mismatches = 0
    for counter, dropped, records in frames:
        if args.frames and (args.limit is None or printed < args.limit):
            print(f"{'':<4}{counter:>6}  frame with {len(records)} record(s), {dropped} dropped so far")
        for record in records:
            if record.book_id == STATUS_BOOK_ID:
                want = want_events[event_index] if event_index < len(want_events) else None
                event_index += 1
            else:
                want = want_records[book_index] if book_index < len(want_records) else None
                book_index += 1
            check = check_text(record, want)
            mismatches += check != "ok"
            if args.limit is None or printed < args.limit:
                print()
                print(f"{'in':<4}{'':>6}  {record.seq:>7}  {input_text(record, messages, stocks, heartbeats)}")
                print(f"{'out':<4}{counter:>6}  {record_text(record, tickers)}  {check}")
                if check.startswith("DIFF"):
                    print(f"{'':<4}{'':>6}  {record_text(want, tickers)}  <- model")
            printed += 1

    missing_records = max(0, len(want_records) - book_index)
    missing_events = max(0, len(want_events) - event_index)
    counters = [counter for counter, _, _ in frames]
    lost = sorted(set(range(counters[0], counters[-1] + 1)) - set(counters)) if counters else []
    print()
    print(f"{len(frames)} record frames, {book_index} records, {event_index} status records captured; "
          f"model: {len(want_records)} records, {len(want_events)} status records")
    if args.limit is not None and printed > args.limit:
        print(f"printed the first {args.limit} of {printed}")
    print(f"frame counters: {counters[0]}..{counters[-1]}, {len(lost)} lost on the PC" if counters else "no record frames")
    print(f"not matching the model: {mismatches}; model records not captured: {missing_records + missing_events}")


if __name__ == "__main__":
    main()