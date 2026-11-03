"""Golden model: what the FPGA should output for a given input.

Input is either Ethernet frames (as the FPGA receives them) or (sequence number, raw message)
pairs (as a Nasdaq ITCH file provides them, once numbered). A frame is checked and unpacked
into its MoldUDP64 messages; each message then goes through the same steps:
  1. check the sequence number: duplicates are dropped, a gap marks every book stale,
  2. learn the tracked tickers' stock locate codes from the R messages,
  3. drop messages for any other stock without decoding them,
  4. update the order books and emit a record for the book whose top of book changed.

A packet without messages (heartbeat or end of session) only takes part in step 1: its
sequence number is that of the next message, so it can reveal a gap.

A frame with a bad FCS is only known to be bad when it ends, after its messages have been
applied. If the parser accepted it, a corrupted byte may now be in a book, so every book
goes stale, as after a gap.
"""

from typing import BinaryIO, Iterable, Iterator

from itch.book import Books, TopOfBook
from itch.framing import DEFAULT_FEED, END_OF_SESSION, Feed, frame_payload, read_packet
from itch.messages import StockDirectory, decode, peek, read_stream, timestamp_of
from itch.pcap import read_pcap
from itch.record import STALE, Record


class Model:
    def __init__(self, tickers: list[str], feed: Feed = DEFAULT_FEED) -> None:
        self.tickers = tickers
        self.feed = feed
        self.books = Books(len(tickers))
        self.locate_to_book_id: dict[int, int] = {}
        self.last_top = [TopOfBook()] * len(tickers)
        self.next_seq = 1
        self.last_timestamp = 0  # of the last message in sequence; stamps a heartbeat's stale records
        self.stale = False
        self.gaps = 0
        self.duplicates = 0
        self.dropped_frames = 0
        self.malformed_packets = 0
        self.fcs_errors = 0

    def process_frame(self, frame: bytes, fcs_error: bool = False) -> list[Record]:
        """Handle one Ethernet frame; frames not for our feed, or too short, are dropped.

        A MoldUDP64 packet whose blocks don't match its length, or a frame that ends before
        its IP length, is counted as malformed; its complete blocks before the break are
        still processed (see framing.read_packet). fcs_error is the MAC's flag at the end
        of the frame.
        """
        if fcs_error:
            self.fcs_errors += 1
        payload = frame_payload(frame, self.feed)
        parsed = read_packet(payload) if payload is not None else None
        if parsed is None:
            self.dropped_frames += 1
            return []
        packet, well_formed = parsed
        if not well_formed:
            self.malformed_packets += 1

        if int.from_bytes(payload[18:20], "big") in (0, END_OF_SESSION):  # message count
            records = self.heartbeat(packet.seq)
        else:
            records = []
            for offset, raw in enumerate(packet.messages):
                records += self.process(packet.seq + offset, raw)

        if fcs_error:  # stamped with the last message in sequence
            records += self._go_stale(self.next_seq - 1, self.last_timestamp)
        return records

    def heartbeat(self, seq: int) -> list[Record]:
        """A packet without messages; seq is that of the next message to come."""
        if seq <= self.next_seq:
            return []
        records = self._gap(seq, self.last_timestamp)
        self.next_seq = seq
        return records

    def process(self, seq: int, raw: bytes) -> list[Record]:
        """Handle one message; returns the records it causes, usually none."""
        if seq < self.next_seq:
            self.duplicates += 1
            return []

        records = self._gap(seq, timestamp_of(raw)) if seq > self.next_seq else []
        self.next_seq = seq + 1
        self.last_timestamp = timestamp_of(raw)

        msg_type, locate = peek(raw)
        if msg_type == StockDirectory.TYPE:
            self._register(decode(raw))
            return records

        book_id = self.locate_to_book_id.get(locate)
        msg = decode(raw) if book_id is not None else None
        if msg is None:
            return records

        changed_id = self.books.apply(msg, book_id)
        if changed_id is not None:
            top = self.books.by_id[changed_id].top()
            if top != self.last_top[changed_id]:
                records.append(self._record(changed_id, seq, self.last_timestamp, top))
        return records

    def missing_tickers(self) -> list[str]:
        """Tickers no R message has announced (yet)."""
        found = set(self.locate_to_book_id.values())
        return [t for i, t in enumerate(self.tickers) if i not in found]

    def _gap(self, seq: int, timestamp: int) -> list[Record]:
        self.gaps += 1
        return self._go_stale(seq, timestamp)

    def _go_stale(self, seq: int, timestamp: int) -> list[Record]:
        """Make every book stale; the first time, emit one record per book."""
        if self.stale:
            return []
        self.stale = True
        return [self._record(book_id, seq, timestamp) for book_id in range(len(self.tickers))]

    def _register(self, msg: StockDirectory) -> None:
        if msg.stock in self.tickers:
            self.locate_to_book_id[msg.stock_locate] = self.tickers.index(msg.stock)

    def _record(self, book_id: int, seq: int, timestamp: int, top: TopOfBook | None = None) -> Record:
        """Record of a book's current state; also what the next change is compared against."""
        if top is None:
            top = self.books.by_id[book_id].top()
        self.last_top[book_id] = top
        return Record(book_id, STALE if self.stale else 0, timestamp, seq, top)


def number(messages: Iterable[bytes], first_seq: int = 1) -> Iterator[tuple[int, bytes]]:
    """Give raw messages consecutive sequence numbers, as an unbroken MoldUDP64 session would."""
    return enumerate(messages, start=first_seq)


def run(messages: Iterable[tuple[int, bytes]], model: Model) -> Iterator[Record]:
    for seq, raw in messages:
        yield from model.process(seq, raw)


def run_frames(frames: Iterable[bytes], model: Model) -> Iterator[Record]:
    for frame in frames:
        yield from model.process_frame(frame)


def run_file(f: BinaryIO, model: Model) -> Iterator[Record]:
    """Run a Nasdaq ITCH file (length-prefixed messages, no sequence numbers)."""
    return run(number(read_stream(f)), model)


def run_pcap(f: BinaryIO, model: Model) -> Iterator[Record]:
    return run_frames((frame for _, frame in read_pcap(f)), model)
