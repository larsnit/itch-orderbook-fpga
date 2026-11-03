"""itch_pkg against its Python mirrors: every record against tb/common/structs.py, the
output record against itch/record.py, be() against int.from_bytes.

    python3.11 tb/itch_pkg/test_itch_pkg.py             (Questa)
    SIM=nvc python3.11 tb/itch_pkg/test_itch_pkg.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.triggers import Timer  # noqa: E402

from itch.book import TopOfBook  # noqa: E402
from itch.record import STATUS_BOOK_ID, Record  # noqa: E402
from tb.common import structs  # noqa: E402
from tb.common.runner import RTL, WRAPPERS, run  # noqa: E402

PORT_PREFIX = {
    "TOKEN_T": "token",
    "OP_T": "op",
    "PRICE_INFO_T": "price_info",
    "ENTRY_T": "entry",
    "OT_RESULT_T": "ot_result",
    "TOP_T": "top",
    "EVENT_T": "event",
}
RANDOM_TRIALS = 100
ENTRY_BITS = 84
WAYS = 8


def value_of(signal) -> int:
    return signal.value.to_unsigned()


async def settle() -> None:
    await Timer(1, "ns")


async def drive_fields(dut, prefix: str, values: dict[str, int]) -> int:
    for field, value in values.items():
        getattr(dut, f"{prefix}_{field}").value = value
    await settle()
    return value_of(getattr(dut, f"{prefix}_packed"))


@cocotb.test()
async def each_field_lands_where_structs_puts_it(dut):
    """One field all ones, the others zero: shows exactly which field is misplaced."""
    for type_name, prefix in PORT_PREFIX.items():
        layout = structs.layouts()[type_name]
        assert len(getattr(dut, f"{prefix}_packed")) == structs.width(layout), f"{type_name}: width"
        for field, bits in layout:
            values = {name: 0 for name, _ in layout}
            values[field] = (1 << bits) - 1
            packed = await drive_fields(dut, prefix, values)
            assert packed == structs.pack(layout, values), f"{type_name}.{field}: got {packed:#x}"


@cocotb.test()
async def random_records_pack_like_structs(dut):
    rng = random.Random(1)
    for type_name, prefix in PORT_PREFIX.items():
        layout = structs.layouts()[type_name]
        for _ in range(RANDOM_TRIALS):
            values = {name: rng.getrandbits(bits) for name, bits in layout}
            packed = await drive_fields(dut, prefix, values)
            assert packed == structs.pack(layout, values), f"{type_name}: {values}"


@cocotb.test()
async def unpacking_inverts_packing(dut):
    rng = random.Random(2)
    for type_name, prefix in PORT_PREFIX.items():
        width = structs.width(structs.layouts()[type_name])
        for _ in range(RANDOM_TRIALS):
            vector = rng.getrandbits(width)
            getattr(dut, f"{prefix}_vector").value = vector
            await settle()
            assert value_of(getattr(dut, f"{prefix}_round_trip")) == vector, f"{type_name}: {vector:#x}"


@cocotb.test()
async def bucket_slot_zero_is_at_the_bottom(dut):
    rng = random.Random(3)
    assert len(dut.bucket_vector) == WAYS * ENTRY_BITS
    for _ in range(RANDOM_TRIALS):
        row = rng.getrandbits(WAYS * ENTRY_BITS)
        dut.bucket_vector.value = row
        await settle()
        assert value_of(dut.bucket_round_trip) == row
        assert value_of(dut.bucket_first_slot) == row & ((1 << ENTRY_BITS) - 1)
        assert value_of(dut.bucket_last_slot) == row >> (WAYS - 1) * ENTRY_BITS


@cocotb.test()
async def output_records_match_record_py(dut):
    rng = random.Random(4)
    top_layout = structs.layouts()["TOP_T"]
    event_layout = structs.layouts()["EVENT_T"]
    for _ in range(RANDOM_TRIALS):
        record = Record(
            rng.getrandbits(8), rng.getrandbits(8), rng.getrandbits(48), rng.getrandbits(64),
            TopOfBook(rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(32), rng.getrandbits(32)),
        )
        dut.record_book_id.value = record.book_id
        dut.record_flags.value = record.flags
        dut.record_timestamp.value = record.timestamp
        dut.record_seq.value = record.seq
        dut.record_bid_price.value = record.top.bid_price
        dut.record_bid_shares.value = record.top.bid_shares
        dut.record_ask_price.value = record.top.ask_price
        dut.record_ask_shares.value = record.top.ask_shares

        top = {name: rng.getrandbits(bits) for name, bits in top_layout}
        dut.top_vector.value = structs.pack(top_layout, top)
        event = {name: rng.getrandbits(bits) for name, bits in event_layout}
        dut.event_vector.value = structs.pack(event_layout, event)
        await settle()

        assert value_of(dut.record_bus) == structs.record_bus(record)
        book_record = Record(
            top["book"], top["flags"], top["timestamp"], top["seq"],
            TopOfBook(top["bid_price"], top["bid_shares"], top["ask_price"], top["ask_shares"]),
        )
        assert value_of(dut.top_record_bus) == structs.record_bus(book_record)
        status_record = Record(STATUS_BOOK_ID, event["flags"], event["timestamp"], event["seq"], TopOfBook(0, 0, 0, 0))
        assert value_of(dut.event_record_bus) == structs.record_bus(status_record)


@cocotb.test()
async def be_reads_big_endian_fields(dut):
    # An A message: locate 1-2, timestamp 5-10, order ref 11-18, price 32-35
    raw = bytes.fromhex("41" "0dfb" "0000" "29b4e1fa3c02" "0000000012656e48" "42" "00000064" "4141504c" "20202020" "002c2f20")
    raw = raw.ljust(structs.MSG_BYTES, b"\x00")
    dut.message_data.value = structs.message_data(raw)
    await settle()
    assert value_of(dut.message_locate) == 0x0DFB
    assert value_of(dut.message_timestamp) == 0x29B4E1FA3C02
    assert value_of(dut.message_order_ref) == 0x12656E48
    assert value_of(dut.message_price) == 2_895_648  # $289.5648

    rng = random.Random(5)
    for _ in range(RANDOM_TRIALS):
        raw = rng.randbytes(structs.MSG_BYTES)
        dut.message_data.value = structs.message_data(raw)
        await settle()
        assert value_of(dut.message_locate) == int.from_bytes(raw[1:3], "big")
        assert value_of(dut.message_timestamp) == int.from_bytes(raw[5:11], "big")
        assert value_of(dut.message_order_ref) == int.from_bytes(raw[11:19], "big")
        assert value_of(dut.message_price) == int.from_bytes(raw[32:36], "big")


def test_itch_pkg():
    run("itch_pkg_tb_top", [RTL / "itch_pkg.vhd", WRAPPERS / "itch_pkg_tb_top.vhd"], __file__)


if __name__ == "__main__":
    test_itch_pkg()
