"""The parts of tb/common that run without a simulator."""

import pytest

from itch.book import TopOfBook
from itch.record import Record
from tb.common import structs
from tb.common.axis import beat_bytes, frame_beats
from tb.common.scoreboard import compare
from tb.common.taps import record_from_bus


def test_frame_beats_of_a_67_byte_frame():
    frame = bytes(range(67))
    beats = frame_beats(frame, fcs_error=True)
    assert len(beats) == 9
    assert beats[0].data == 0x0706050403020100  # byte 0 in lane 0
    assert [beat.keep for beat in beats] == [0xFF] * 8 + [0x07]
    assert [beat.last for beat in beats] == [False] * 8 + [True]
    assert [beat.user for beat in beats] == [False] * 8 + [True]
    assert b"".join(beat_bytes(beat.data, beat.keep) for beat in beats) == frame


def test_beat_bytes_takes_only_kept_lanes():
    # the packet parser's first payload beat: lanes 0-1 are the UDP checksum
    assert beat_bytes(0x0706050403020100, 0b1111_1100) == bytes([2, 3, 4, 5, 6, 7])


def test_record_bus_round_trip():
    record = Record(3, 0x02, 0x29B4E1FA3C02, 1001, TopOfBook(2_894_400, 100, 2_894_500, 200))
    bus = structs.record_bus(record)
    assert bus & 0xFF == 3  # byte 0 (book id) in bits 7:0
    assert record_from_bus(bus) == record


def test_compare_passes_on_equal_streams_and_ignores_unmodelled_fields():
    compare([{"seq": 1}, {"seq": 2}], [{"seq": 1, "t_arrival": 7}, {"seq": 2, "t_arrival": 9}], "ops")


def test_compare_reports_first_mismatch_with_the_differing_field():
    with pytest.raises(AssertionError, match=r"first mismatch at item 1\n  seq: expected 2, got 5"):
        compare([{"seq": 1}, {"seq": 2}, {"seq": 3}], [{"seq": 1}, {"seq": 5}, {"seq": 3}], "ops")


def test_compare_reports_missing_items():
    with pytest.raises(AssertionError, match="expected 3 items, got 2"):
        compare([1, 2, 3], [1, 2], "records")
