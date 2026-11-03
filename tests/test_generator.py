"""The generator's cases: counters, frame labels, decoys, coverage, determinism, and what the
hardware model does with them (status records, stale bits, flags)."""

import pytest

from itch.framing import (
    DEFAULT_FEED,
    MAX_PAYLOAD,
    MOLD_HEADER_SIZE,
    PAYLOAD_OFFSET,
    build_frame,
    frame_payload,
    read_packet,
)
from itch.generator import (
    CASES,
    COUNTERS,
    TICKERS,
    Frame,
    Market,
    all_cases,
    colliding_refs,
    fill,
    generator_config,
    raw_frame,
)
from itch.hashing import buckets
from itch.hw_model import HardwareModel
from itch.messages import decode, timestamp_of
from itch.model import Model
from itch.record import BAD_FCS, GAP, OUTSIDE_WINDOW, STALE, STATUS_BOOK_ID, UNUSABLE

BUS_BYTES = 8
CASE_NAMES = [make_case.__name__ for make_case in CASES]


def run(frames: list[Frame]) -> tuple[list, list, HardwareModel]:
    """Run frames through a new hardware model, with each frame's FCS flag."""
    model = HardwareModel(generator_config())
    records, events = [], []
    for frame in frames:
        more_records, more_events = model.process_frame(frame.data, frame.fcs_error)
        records += more_records
        events += more_events
    return records, events, model


@pytest.fixture(scope="module")
def cases():
    return {case.name: case for case in all_cases()}


@pytest.fixture(scope="module")
def outputs(cases):
    return {name: run(case.frames) for name, case in cases.items()}


@pytest.mark.parametrize("make_case", CASES, ids=CASE_NAMES)
def test_hardware_model_counters_match_the_case(make_case):
    case = make_case()
    _, _, model = run(case.frames)
    assert {name: model.counters[name] for name in COUNTERS} == case.expected_counters()


@pytest.mark.parametrize("make_case", [make for make in CASES if not make().hardware_only], ids=lambda make: make.__name__)
def test_ideal_model_agrees_on_shared_rules(make_case):
    """The ideal model has the same counters for the rules both models have."""
    case = make_case()
    model = Model(TICKERS)
    for frame in case.frames:
        model.process_frame(frame.data, frame.fcs_error)
    ideal = {
        "dropped_frames": model.dropped_frames, "malformed_packets": model.malformed_packets,
        "fcs_errors": model.fcs_errors, "gaps": model.gaps, "duplicates": model.duplicates,
        "unknown_refs": model.books.unknown_refs, "inconsistent": model.books.inconsistent,
    }
    assert ideal == {name: case.expected_counters()[name] for name in ideal}


@pytest.mark.parametrize("make_case", CASES, ids=CASE_NAMES)
def test_each_frame_is_accepted_exactly_when_labelled(make_case):
    for i, frame in enumerate(make_case().frames):
        payload = frame_payload(frame.data, DEFAULT_FEED)
        accepted = payload is not None and read_packet(payload) is not None
        assert accepted == frame.accept, f"frame {i}: {frame.note}"


@pytest.mark.parametrize("make_case", CASES, ids=CASE_NAMES)
def test_rejected_frames_change_nothing(make_case):
    case = make_case()
    records, events, _ = run(case.frames)
    records_without, events_without, _ = run([frame for frame in case.frames if frame.accept])
    assert (records, events) == (records_without, events_without)


@pytest.mark.parametrize("make_case", CASES, ids=CASE_NAMES)
def test_a_wrongly_accepted_decoy_shows_up(make_case):
    """Accepting a decoy's payload means its packet arrives twice: duplicates appear."""
    case = make_case()
    for i, frame in enumerate(case.frames):
        if not frame.decoy:
            continue
        follower = case.frames[i + 1]
        assert follower.accept
        payload = frame_payload(follower.data, DEFAULT_FEED)
        assert payload[: MOLD_HEADER_SIZE + 20] in frame.data, f"decoy {i} doesn't carry the next packet"

        mutant = case.frames[:i] + [follower] + case.frames[i + 1 :]
        _, _, model = run(mutant)
        assert model.counters["duplicates"] > case.expected_counters()["duplicates"], frame.note


def test_cases_are_reproducible():
    first = [[frame.data for frame in case.frames] for case in all_cases()]
    second = [[frame.data for frame in case.frames] for case in all_cases()]
    assert first == second


def test_case_names_are_unique():
    names = [make_case().name for make_case in CASES]
    assert len(names) == len(set(names))
    assert names == [make_case.__name__ for make_case in CASES]


def test_raw_frame_defaults_equal_build_frame():
    market = Market()
    for ip_id, n in [(0, 1), (1, 5), (0xFFFF, 30), (0x12345, 2)]:
        payload = b"".join(market.batch(n))
        assert raw_frame(DEFAULT_FEED, payload, ip_id=ip_id) == build_frame(DEFAULT_FEED, payload, ip_id=ip_id)
    assert raw_frame(DEFAULT_FEED, b"") == build_frame(DEFAULT_FEED, b"")


def test_market_messages_are_valid_and_consistent():
    """A long random run on one book feed: every message decodes, no ref goes wrong."""
    market = Market(seed=99)
    messages = market.opening() + market.batch(20_000)
    assert all(decode(raw) is not None for raw in messages)
    model = Model(TICKERS)
    for seq, raw in enumerate(messages, start=1):
        model.process(seq, raw)
    assert model.books.unknown_refs == 0
    assert model.books.inconsistent == 0
    tracked = {ref for ref, order in market.live.items() if order.ticker in TICKERS}
    assert tracked == set(model.books.orders)


def test_fill_hits_every_size_from_210_bytes():
    market = Market()
    for block_bytes in range(210, MAX_PAYLOAD - MOLD_HEADER_SIZE + 1):
        messages = fill(market, block_bytes)
        assert sum(2 + len(raw) for raw in messages) == block_bytes


def test_frame_sizes_cover_every_bus_residue(cases):
    lengths = {len(frame.data) for frame in cases["frame_sizes"].frames if frame.accept}
    assert PAYLOAD_OFFSET + MOLD_HEADER_SIZE in lengths  # heartbeat: 62 bytes
    assert 14 + 20 + 8 + MAX_PAYLOAD in lengths  # 1514 bytes
    assert {length % BUS_BYTES for length in lengths} == set(range(BUS_BYTES))


def test_alignment_puts_block_boundaries_in_every_lane(cases):
    prefix_lanes, message_lanes, split_prefixes = set(), set(), 0
    for frame in cases["alignment"].frames:
        payload = frame_payload(frame.data, DEFAULT_FEED)
        packet, _ = read_packet(payload)
        pos = PAYLOAD_OFFSET + MOLD_HEADER_SIZE
        for raw in packet.messages:
            prefix_lanes.add(pos % BUS_BYTES)
            message_lanes.add((pos + 2) % BUS_BYTES)
            split_prefixes += pos % BUS_BYTES == BUS_BYTES - 1
            pos += 2 + len(raw)
    assert prefix_lanes == set(range(BUS_BYTES))
    assert message_lanes == set(range(BUS_BYTES))
    assert split_prefixes > 0  # a length prefix across two bus words


def test_alignment_uses_every_message_type(cases):
    types = set()
    for frame in cases["alignment"].frames:
        packet, _ = read_packet(frame_payload(frame.data, DEFAULT_FEED))
        types |= {raw[:1] for raw in packet.messages}
    assert types == {b"S", b"R", b"A", b"F", b"E", b"C", b"X", b"D", b"U"}


def test_frames_cut_short_keep_their_complete_blocks(cases):
    frames = [frame for frame in cases["malformed_mold"].frames if "frame ends" in frame.note]
    assert [frame.accept for frame in frames] == [True, True, False]
    kept = []
    for frame in frames[:2]:
        payload = frame_payload(frame.data, DEFAULT_FEED)
        assert len(frame.data) < 14 + int.from_bytes(frame.data[16:18], "big")  # shorter than its IP length
        packet, well_formed = read_packet(payload)
        assert not well_formed
        kept.append(len(packet.messages))
    assert kept == [4, 3]


# Status records and stale bits


@pytest.mark.parametrize("name", ["gap", "two_gaps", "reordered", "heartbeat_gap", "late_join"])
def test_first_gap_gives_one_status_record(outputs, name):
    records, events, _ = outputs[name]
    assert len(events) == 1
    event = events[0]
    assert event.book_id == STATUS_BOOK_ID and event.flags == STALE | GAP
    assert not any(record.stale for record in records if record.seq < event.seq)
    assert all(record.stale for record in records if record.seq >= event.seq)


def test_heartbeat_gap_status_record_uses_the_heartbeat(cases, outputs):
    case = cases["heartbeat_gap"]
    heartbeat = next(frame for frame in case.frames if frame.note == "heartbeat after the lost packet")
    packet, _ = read_packet(frame_payload(heartbeat.data, DEFAULT_FEED))
    event = outputs["heartbeat_gap"][1][0]
    assert event.seq == packet.seq

    # The timestamp is that of the last message before the heartbeat.
    before = case.frames[: case.frames.index(heartbeat)]
    last_packet, _ = read_packet(frame_payload(before[-1].data, DEFAULT_FEED))
    assert event.timestamp == timestamp_of(last_packet.messages[-1])


def test_bad_fcs_status_record_follows_the_frames_messages(cases, outputs):
    case = cases["bad_fcs"]
    records, events, _ = outputs["bad_fcs"]
    assert len(events) == 1 and events[0].flags == STALE | BAD_FCS  # the second bad frame adds none

    bad = next(i for i, frame in enumerate(case.frames) if frame.fcs_error and frame.accept)
    packet, _ = read_packet(frame_payload(case.frames[bad].data, DEFAULT_FEED))
    last_seq = packet.seq + len(packet.messages) - 1
    assert events[0].seq == last_seq
    assert not any(record.stale for record in records if record.seq <= last_seq)
    assert all(record.stale for record in records if record.seq > last_seq)


def test_unusable_messages_give_one_status_record(outputs):
    records, events, _ = outputs["unusable_messages"]
    assert len(events) == 1 and events[0].flags == STALE | UNUSABLE
    assert all(record.stale for record in records if record.seq > events[0].seq)


def test_book_anomalies_mark_only_their_books(outputs):
    records, events, model = outputs["book_anomalies"]
    assert events == []
    assert model.book_stale == [True] * 6 + [False, False]  # AAPL … COST; SIRI and ZM untouched
    stale_books = {record.book_id for record in records if record.stale}
    assert stale_books <= set(range(6))


def test_table_overflow_marks_only_siri(cases, outputs):
    records, events, model = outputs["table_overflow"]
    siri = TICKERS.index("SIRI")
    assert model.book_stale == [book_id == siri for book_id in range(len(TICKERS))]
    assert any(record.book_id == siri and record.stale for record in records)


def test_window_edges_flag_on_and_off(outputs):
    records, _, _ = outputs["window_edges"]
    aapl = [record for record in records if record.book_id == 0]
    flags = [bool(record.flags & OUTSIDE_WINDOW) for record in aapl]
    switches_on = sum(1 for before, after in zip(flags, flags[1:]) if after and not before)
    assert switches_on == 3  # a bid above, a sub-penny price inside, only bids below
    assert not flags[-1]  # cleared at the end


def test_colliding_refs_share_both_buckets():
    refs = colliding_refs(49, 13)
    assert len(set(refs)) == 49 and all(ref < 2**32 for ref in refs)
    assert len({buckets(ref, 13) for ref in refs}) == 1


@pytest.mark.parametrize("make_case", [make for make in CASES if not make().hardware_only], ids=lambda make: make.__name__)
def test_without_gaps_both_models_give_the_same_records(make_case):
    case = make_case()
    records, events, _ = run(case.frames)
    if events:
        pytest.skip("the models report staleness differently")
    model = Model(TICKERS)
    ideal = []
    for frame in case.frames:
        ideal += model.process_frame(frame.data, frame.fcs_error)
    assert records == ideal
