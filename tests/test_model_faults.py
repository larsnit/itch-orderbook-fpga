"""Model behaviour for faulty input: short and broken MoldUDP64 packets, heartbeats, and
messages that contradict the order table."""

import struct

import pytest

from itch.book import Books, TopOfBook
from itch.framing import DEFAULT_FEED, END_OF_SESSION, MOLD_HEADER, MoldPacket, build_frame, frame_payload, read_packet
from itch.generator import TICKERS, Market, raw_frame
from itch.messages import AddOrder, OrderCancel, OrderDelete, OrderExecuted, OrderReplace
from itch.model import Model
from itch.record import Record

SESSION = b"TEST".ljust(10)


def header(seq: int, count: int) -> bytes:
    return struct.pack(MOLD_HEADER, SESSION, seq, count)


def blocks(*messages: bytes) -> bytes:
    return b"".join(len(raw).to_bytes(2, "big") + raw for raw in messages)


M1, M2, M3 = b"\x01" * 12, b"\x02" * 19, b"\x03" * 36


# read_packet


def test_read_packet_well_formed():
    packet, ok = read_packet(header(5, 3) + blocks(M1, M2, M3))
    assert ok and packet.seq == 5 and packet.messages == (M1, M2, M3) and packet.session == "TEST"


@pytest.mark.parametrize("raw", [b"", b"\x00" * 19])
def test_read_packet_shorter_than_header(raw):
    assert read_packet(raw) is None
    with pytest.raises(ValueError):
        MoldPacket.from_bytes(raw)


@pytest.mark.parametrize(
    "raw, kept",
    [
        (header(1, 3) + blocks(M1, M2, M3)[:-1], (M1, M2)),  # last block cut
        (header(1, 4) + blocks(M1, M2, M3), (M1, M2, M3)),  # count too high
        (header(1, 2) + blocks(M1, M2, M3), (M1, M2)),  # bytes left over
        (header(1, 3) + blocks(M1) + b"\x00\x00" + blocks(M3), (M1,)),  # zero-length block
        (header(1, 3) + blocks(M1, M2) + b"\x00", (M1, M2)),  # half a length prefix
        (header(1, 0) + b"\x00", ()),  # heartbeat with a stray byte
    ],
)
def test_read_packet_keeps_complete_blocks_before_the_break(raw, kept):
    packet, ok = read_packet(raw)
    assert not ok and packet.messages == kept
    with pytest.raises(ValueError):
        MoldPacket.from_bytes(raw)


@pytest.mark.parametrize("count", [0, END_OF_SESSION])
def test_read_packet_without_messages(count):
    packet, ok = read_packet(header(42, count))
    assert ok and packet.seq == 42 and packet.messages == ()


# Model: frames


def frame(payload: bytes) -> bytes:
    return build_frame(DEFAULT_FEED, payload)


def test_short_mold_payload_is_dropped_not_raised():
    model = Model(TICKERS)
    assert model.process_frame(frame(b"abcd")) == []
    assert model.dropped_frames == 1 and model.next_seq == 1


def opened_model() -> tuple[Model, Market]:
    """A model that has seen the opening; next_seq continues from there."""
    market = Market()
    model = Model(TICKERS)
    model.process_frame(frame(MoldPacket("TEST", 1, tuple(market.opening())).to_bytes()))
    return model, market


def test_malformed_packet_processes_the_blocks_before_the_break():
    model, market = opened_model()
    seq = model.next_seq
    adds = [market.add("AAPL", "B", 1_000_000, 100), market.add("AAPL", "B", 1_000_100, 200)]
    payload = header(seq, 2) + blocks(*adds)[:-4]
    records = model.process_frame(frame(payload))
    assert model.malformed_packets == 1
    assert model.next_seq == seq + 1
    assert [record.top for record in records] == [TopOfBook(1_000_000, 100)]

    # The next packet starts at the lost message: no gap, no duplicate.
    model.process_frame(frame(MoldPacket("TEST", seq + 1, (adds[1],)).to_bytes()))
    assert model.gaps == 0 and model.duplicates == 0


def test_heartbeat_in_sequence_or_old_changes_nothing():
    model, _ = opened_model()
    seq = model.next_seq
    for hb_seq in (seq, seq - 3, seq):
        assert model.process_frame(frame(header(hb_seq, 0))) == []
    assert model.gaps == 0 and model.duplicates == 0 and model.next_seq == seq


def test_heartbeat_ahead_reveals_a_gap():
    model, market = opened_model()
    model.process_frame(frame(MoldPacket("TEST", model.next_seq, (market.add("MSFT", "S", 2_000_000, 10),)).to_bytes()))
    last_time = market.time
    seq = model.next_seq

    records = model.process_frame(frame(header(seq + 5, 0)))
    assert model.gaps == 1 and model.next_seq == seq + 5
    assert len(records) == len(TICKERS)
    assert all(record.stale and record.seq == seq + 5 and record.timestamp == last_time for record in records)
    assert records[1].top == TopOfBook(ask_price=2_000_000, ask_shares=10)

    # A second heartbeat with the same number, then the message it announced: nothing new.
    assert model.process_frame(frame(header(seq + 5, END_OF_SESSION))) == []
    model.process_frame(frame(MoldPacket("TEST", seq + 5, (market.system("Q"),)).to_bytes()))
    assert model.gaps == 1


def test_heartbeat_before_any_message_uses_timestamp_0():
    model = Model(TICKERS)
    records = model.process_frame(frame(header(10, 0)))
    assert model.gaps == 1 and {record.timestamp for record in records} == {0}


# Books: contradictions are counted, never raised


def aapl_bid() -> AddOrder:
    """Order 7: buy 100 AAPL at $150.00."""
    return AddOrder(1, 0, 100, 7, "B", 100, "AAPL", 1_500_000)


def test_execute_more_than_left_removes_the_order():
    books = Books(1)
    books.apply(aapl_bid(), 0)
    assert books.apply(OrderExecuted(1, 0, 101, 7, 150, 1), 0) == 0
    assert books.inconsistent == 1
    assert 7 not in books.orders and books.by_id[0].top() == TopOfBook()


def test_cancel_more_than_left_removes_the_order():
    books = Books(1)
    books.apply(aapl_bid(), 0)
    books.apply(OrderCancel(1, 0, 101, 7, 101), 0)
    assert books.inconsistent == 1 and not books.orders


def test_add_with_a_known_ref_is_ignored():
    books = Books(1)
    books.apply(aapl_bid(), 0)
    assert books.apply(AddOrder(1, 0, 102, 7, "S", 50, "AAPL", 1_600_000), 0) is None
    assert books.inconsistent == 1
    assert books.by_id[0].top() == TopOfBook(1_500_000, 100)


def test_replace_onto_a_known_ref_removes_the_old_order_only():
    books = Books(1)
    books.apply(aapl_bid(), 0)
    books.apply(AddOrder(1, 0, 102, 8, "B", 30, "AAPL", 1_400_000), 0)
    assert books.apply(OrderReplace(1, 0, 103, 7, 8, 500, 1_450_000), 0) == 0
    assert books.inconsistent == 1
    assert books.by_id[0].top() == TopOfBook(1_400_000, 30)


def test_unknown_refs_are_counted_separately():
    books = Books(1)
    books.apply(OrderDelete(1, 0, 100, 99), 0)
    books.apply(OrderReplace(1, 0, 100, 99, 100, 5, 1), 0)
    assert books.unknown_refs == 2 and books.inconsistent == 0


# Frames cut short


def test_frame_payload_of_a_frame_cut_short_is_what_arrived():
    payload = header(1, 3) + blocks(M1, M2, M3)
    full = frame(payload)
    assert frame_payload(full[:-5], DEFAULT_FEED) == payload[:-5]
    assert frame_payload(full + b"\xaa" * 4, DEFAULT_FEED) == payload  # padding after the IP length is cut off


def test_frame_cut_short_keeps_the_messages_that_arrived():
    model, market = opened_model()
    seq = model.next_seq
    adds = [market.add("AAPL", "B", 1_000_000, 100), market.add("AAPL", "B", 1_000_100, 200)]
    full = frame(MoldPacket("TEST", seq, tuple(adds)).to_bytes())
    records = model.process_frame(full[:-1])
    assert model.malformed_packets == 1 and model.dropped_frames == 0
    assert model.next_seq == seq + 1
    assert [record.top for record in records] == [TopOfBook(1_000_000, 100)]


def test_frame_cut_inside_the_mold_header_is_dropped():
    model, _ = opened_model()
    full = frame(header(model.next_seq, 0))
    assert model.process_frame(full[:-1]) == []
    assert model.dropped_frames == 1 and model.malformed_packets == 0


# Bad FCS


def test_bad_fcs_on_a_dropped_frame_is_only_counted():
    model, _ = opened_model()
    foreign = raw_frame(DEFAULT_FEED, header(model.next_seq, 0), dst_port=26478)
    assert model.process_frame(foreign, fcs_error=True) == []
    assert model.fcs_errors == 1 and model.dropped_frames == 1 and not model.stale


def test_bad_fcs_on_a_feed_frame_applies_its_messages_then_makes_every_book_stale():
    model, market = opened_model()
    seq = model.next_seq
    add = market.add("MSFT", "S", 2_000_000, 10)
    records = model.process_frame(frame(MoldPacket("TEST", seq, (add,)).to_bytes()), fcs_error=True)

    assert model.fcs_errors == 1 and model.gaps == 0 and model.stale
    assert records[0] == Record(1, 0, market.time, seq, TopOfBook(ask_price=2_000_000, ask_shares=10))
    burst = records[1:]
    assert [record.book_id for record in burst] == list(range(len(TICKERS)))
    assert all(record.stale and record.seq == seq and record.timestamp == market.time for record in burst)
    assert burst[1].top == records[0].top

    # Already stale: a second bad frame is counted, nothing more.
    assert model.process_frame(frame(header(model.next_seq, 0)), fcs_error=True) == []
    assert model.fcs_errors == 2
