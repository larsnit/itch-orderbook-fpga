"""The hardware model and its parts: order table, ladder, decoder rules, stage taps, the
comparison with the ideal model, configuration, the struct mirror, the VHDL hash package."""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gen_hash_pkg import vhdl_package  # noqa: E402

from itch.book import NO_ASK_PRICE, TopOfBook  # noqa: E402
from itch.compare import compare  # noqa: E402
from itch.config import Config  # noqa: E402
from itch.generator import TICKERS, Market, colliding_refs, generator_config  # noqa: E402
from itch.hashing import HASHES  # noqa: E402
from itch.hw_model import HardwareModel  # noqa: E402
from itch.ladder import ABOVE, BELOW, INSIDE, UNREP, LadderSide, position_of  # noqa: E402
from itch.messages import AddOrder, OrderCancel, OrderDelete, OrderExecuted, OrderReplace, StockDirectory  # noqa: E402
from itch.model import Model  # noqa: E402
from itch.order_table import STASH_TABLE, Entry, OrderTable  # noqa: E402
from itch.record import OUTSIDE_WINDOW, STALE, Record  # noqa: E402
from tb.common.structs import (  # noqa: E402
    layouts,
    message_data,
    op_fields,
    ot_result_fields,
    pack,
    token_fields,
    top_fields,
    unpack,
    width,
)

DOLLAR = 10_000


# Order table


def entry(ref: int) -> Entry:
    return Entry(ref, 0, "B", INSIDE, 4096, 100)


def test_inserts_fill_the_emptier_bucket_then_the_stash_then_fail():
    table = OrderTable(bucket_bits=13, ways=8, stash=32)
    refs = colliding_refs(49, 13)
    locations = [table.insert(entry(ref)) for ref in refs]
    assert [location.table for location in locations[:16]] == [0, 1] * 8  # ties go to table 0
    assert [location.slot for location in locations[:4]] == [0, 0, 1, 1]
    assert all(location.table == STASH_TABLE for location in locations[16:48])
    assert locations[48] is None and table.live == 48

    table.remove(table.find(refs[3]))  # frees slot 1 of table 1
    assert table.insert(entry(refs[48])).table == 1


def test_find_and_refs_too_large():
    table = OrderTable(13, 8, 32)
    table.insert(entry(7))
    assert table.get(table.find(7)).order_ref == 7
    assert table.find(8) is None
    assert table.find(7 + 2**32) is None  # never stored, even if its low bits match


# Ladder


@pytest.mark.parametrize(
    "price, expected",
    [
        (9_0400, (INSIDE, 0)),  # base $9.04
        (90_9500, (INSIDE, 8191)),
        (9_0399, (BELOW, 0)),  # $9.0399: 903 cents, below
        (90_9600, (ABOVE, 0)),
        (50 * DOLLAR + 50, (UNREP, 0)),
        (50 * DOLLAR, (INSIDE, 4096)),
    ],
)
def test_position_of(price, expected):
    assert position_of(price, 904, 8192) == expected


def test_ladder_top_and_outside():
    bids = LadderSide(is_bid=True, base_cents=904)
    assert bids.top() == (0, 0) and not bids.outside()
    bids.off_ladder[BELOW] += 1
    assert bids.outside()  # only a bid below: the true best is below the window
    bids.add(4096, 300)
    bids.add(4090, 500)
    assert bids.top() == ((904 + 4096) * 100, 300) and not bids.outside()
    bids.off_ladder[ABOVE] += 1
    assert bids.outside()  # a bid above the window would be the best
    bids.off_ladder[ABOVE] -= 1
    bids.remove(4096, 300)
    assert bids.top() == ((904 + 4090) * 100, 500)

    asks = LadderSide(is_bid=False, base_cents=904)
    assert asks.top() == (NO_ASK_PRICE, 0)
    asks.off_ladder[ABOVE] += 1
    assert asks.outside()
    asks.add(5000, 10)
    assert not asks.outside()  # an ask above is never better than one inside


# Hardware model rules


def config() -> Config:
    return Config(["AAPL", "MSFT"], {"AAPL": 5000, "MSFT": 8700})  # $50.00 and $87.00


def directory(locate: int, ticker: str) -> bytes:
    return StockDirectory(locate, 0, 1, ticker, "Q", "N", 100, "N", "C", "Z", "P", "N", "N", "1", "N", 0, " ").to_bytes()


def feed(model: HardwareModel, messages, first_seq: int = 1):
    """Process messages in sequence; all records and events."""
    records, events = [], []
    for seq, raw in enumerate(messages, start=first_seq):
        more_records, more_events = model.process(seq, raw)
        records += more_records
        events += more_events
    return records, events


def opened(taps: bool = False) -> HardwareModel:
    model = HardwareModel(config(), taps=taps)
    feed(model, [directory(10, "AAPL"), directory(11, "MSFT")])
    return model


def add(ref, side="B", shares=100, price=50 * DOLLAR, locate=10, stock="AAPL") -> bytes:
    return AddOrder(locate, 0, ref, ref, side, shares, stock, price).to_bytes()


def test_record_shows_the_ladder_top():
    model = opened()
    records, _ = feed(model, [add(1, "B", 100, 49 * DOLLAR), add(2, "S", 50, 51 * DOLLAR)], first_seq=3)
    assert records[-1] == Record(0, 0, 2, 4, TopOfBook(49 * DOLLAR, 100, 51 * DOLLAR, 50))


def test_flag_change_alone_gives_a_record():
    model = opened()
    feed(model, [add(1, "B", 100, 49 * DOLLAR)], first_seq=3)
    records, _ = feed(model, [add(2, "B", 100, 95 * DOLLAR)], first_seq=4)  # a bid above the window
    assert records[0].flags == OUTSIDE_WINDOW and records[0].top.bid_price == 49 * DOLLAR
    records, _ = feed(model, [OrderDelete(10, 0, 9, 2).to_bytes()], first_seq=5)
    assert records[0].flags == 0 and records[0].top.bid_price == 49 * DOLLAR


def test_unknown_ref_marks_only_its_book():
    model = opened()
    records, events = feed(model, [OrderDelete(10, 0, 5, 77).to_bytes()], first_seq=3)
    assert events == [] and model.book_stale == [True, False]
    assert records == [Record(0, STALE, 5, 3, TopOfBook())]
    assert model.counters["unknown_refs"] == 1


def test_over_cancel_clamps_and_marks_the_book():
    model = opened()
    feed(model, [add(1, "B", 100, 49 * DOLLAR)], first_seq=3)
    records, _ = feed(model, [OrderCancel(10, 0, 9, 1, 150).to_bytes()], first_seq=4)
    assert records[0].top == TopOfBook() and records[0].flags == STALE
    assert model.counters["inconsistent"] == 1 and model.table.live == 0


def test_ref_above_32_bits_is_a_ref_error():
    model = opened()
    feed(model, [add(2**32 + 1)], first_seq=3)
    assert model.counters["ref_errors"] == 1 and model.book_stale[0] and model.table.live == 0


def test_u_moves_the_order_and_gives_one_record():
    model = opened(taps=True)
    feed(model, [add(1, "S", 100, 51 * DOLLAR)], first_seq=3)
    records, _ = feed(model, [OrderReplace(10, 0, 9, 1, 2, 300, 52 * DOLLAR).to_bytes()], first_seq=4)
    assert [op.kind for op in model.taps.ops[-2:]] == ["U_DELETE", "U_INSERT"]
    assert len(records) == 1 and records[0].top.ask_price == 52 * DOLLAR and records[0].top.ask_shares == 300
    assert model.table.get(model.table.find(2)).side == "S"


def test_u_into_another_book_is_inconsistent():
    model = opened()
    feed(model, [add(1, "S", 100, 51 * DOLLAR)], first_seq=3)
    feed(model, [OrderReplace(11, 0, 9, 1, 2, 300, 88 * DOLLAR).to_bytes()], first_seq=4)  # MSFT's locate
    assert model.counters["inconsistent"] == 1 and model.book_stale == [True, True]
    assert model.table.live == 0


def test_unusable_message_counts_as_lost():
    model = opened()
    records, events = feed(model, [add(1)[:-1] + b"\x00\x00"], first_seq=3)  # 37 bytes
    assert records == [] and len(events) == 1 and model.stale_all
    assert model.counters["unusable_messages"] == 1 and model.table.live == 0


def test_taps_line_up():
    model = opened(taps=True)
    feed(model, [add(1), OrderExecuted(10, 0, 9, 1, 40, 1).to_bytes(), OrderReplace(10, 0, 9, 1, 2, 10, 50 * DOLLAR).to_bytes()], first_seq=3)
    taps = model.taps
    assert len(taps.ops) == len(taps.ot_results) == len(taps.tops) == 4
    assert [result.change for result in taps.ot_results] == ["ADD", "REMOVE", "REMOVE", "ADD"]
    assert [top.record_enable for top in taps.tops] == [True, True, False, True]


# Agreement with the ideal model


def test_hardware_and_ideal_model_agree_on_a_long_generated_day():
    market = Market(seed=7)
    messages = market.opening() + market.batch(20_000)
    result = compare(enumerate(messages, start=1), Model(TICKERS), HardwareModel(generator_config()))
    assert result.mismatches == 0, result.first_mismatch
    assert result.compared > 10_000 and result.flagged == 0 and result.stale == 0


# Configuration, struct mirror, VHDL package


def test_config_round_trip_and_base(tmp_path):
    config = Config(["AAPL", "SIRI"], {"AAPL": 28944, "SIRI": 714})
    config.save(tmp_path / "config.json")
    loaded = Config.load(tmp_path / "config.json")
    assert loaded == config
    assert loaded.base_cents(0) == 28944 - 4096
    assert loaded.base_cents(1) == 100  # never below $1.00


def test_struct_widths_and_round_trip():
    layout = layouts()
    assert width(layout["ENTRY_T"]) == 84
    values = {"valid": 1, "order_ref": 0xDEADBEEF, "book": 5, "side": 1, "position": 3, "index": 8191, "shares": 7}
    assert unpack(layout["ENTRY_T"], pack(layout["ENTRY_T"], values)) == values
    with pytest.raises(ValueError):
        pack(layout["ENTRY_T"], {"book": 8})


def test_tap_fields_pack():
    model = opened(taps=True)
    feed(model, [add(1), OrderReplace(10, 0, 9, 1, 2, 10, 50 * DOLLAR).to_bytes()], first_seq=3)
    layout = layouts()
    for op in model.taps.ops:
        pack(layout["OP_T"], op_fields(op))
    for result in model.taps.ot_results:
        pack(layout["OT_RESULT_T"], ot_result_fields(result))
    for top in model.taps.tops:
        pack(layout["TOP_T"], top_fields(top))


def test_token_data_byte_order():
    raw = bytes(range(1, 20))
    assert message_data(raw) & 0xFF == 1  # byte 0 in bits 7 DOWNTO 0
    assert (message_data(raw) >> 8) & 0xFF == 2


def test_frame_tokens_from_the_hardware_model():
    from itch.framing import DEFAULT_FEED, MoldPacket, build_frame

    model = HardwareModel(config(), taps=True)
    packet = MoldPacket("TEST", 1, (directory(10, "AAPL"), add(1)))
    model.process_frame(build_frame(DEFAULT_FEED, packet.to_bytes()), fcs_error=True)
    kinds = [token.kind for token in model.taps.tokens]
    assert kinds == ["MSG", "MSG", "FCS"]
    assert token_fields(model.taps.tokens[1])["len"] == 36


def test_vhdl_hash_package_holds_the_masks():
    text = vhdl_package()
    found = [int(value, 16) for value in re.findall(r'x"([0-9A-F]{16})"', text)]
    assert found == HASHES[0].masks + HASHES[1].masks
    assert "PACKAGE itch_hash_pkg IS" in text and "END PACKAGE;" in text
