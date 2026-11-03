"""Python mirror of the VHDL records in rtl/itch_pkg.vhd (implementation §3.4).

A record crosses into the testbench as one flat STD_LOGIC_VECTOR (the wrapper's to_slv),
first field in the most significant bits. Each layout here lists the same fields with the
same widths in the same order; pack() and unpack() convert between that vector (a Python
int) and a dict of field values. The *_fields() functions turn the hardware model's stage
outputs (itch.hw_model taps) into such dicts, so a testbench compares field by field.
"""

from itch.hw_model import Op, OtResult, Token, Top
from itch.record import Record

MSG_BYTES = 40

TOKEN_KINDS = {"MSG": 0, "HEARTBEAT": 1, "FCS": 2}
OP_KINDS = {"INSERT": 0, "REMOVE": 1, "DELETE": 2, "U_DELETE": 3, "U_INSERT": 4}
POSITIONS = {"inside": 0, "below": 1, "above": 2, "unrep": 3}
CHANGES = {"NONE": 0, "ADD": 1, "REMOVE": 2}
SIDES = {"B": 0, "S": 1, "": 0}


def layouts(ladder_bits: int = 13, bucket_bits: int = 13) -> dict[str, list[tuple[str, int]]]:
    """(field, width) per record, in VHDL order."""
    return {
        "TOKEN_T": [
            ("valid", 1), ("kind", 2), ("seq", 64), ("len", 16), ("data", MSG_BYTES * 8), ("t_arrival", 16),
        ],
        "OP_T": [
            ("valid", 1), ("kind", 3), ("book", 3), ("side", 1), ("order_ref", 32), ("ref_error", 1),
            ("bucket0", bucket_bits), ("bucket1", bucket_bits), ("shares", 32), ("seq", 64),
            ("timestamp", 48), ("t_arrival", 16), ("stale_all", 1), ("record_enable", 1),
        ],
        "PRICE_INFO_T": [("position", 2), ("index", ladder_bits)],
        "ENTRY_T": [
            ("valid", 1), ("order_ref", 32), ("book", 3), ("side", 1), ("position", 2),
            ("index", ladder_bits), ("shares", 32),
        ],
        "OT_RESULT_T": [
            ("valid", 1), ("change", 2), ("book", 3), ("side", 1), ("position", 2), ("index", ladder_bits),
            ("shares", 32), ("order_gone", 1), ("book_stale", 1), ("stale_all", 1), ("record_enable", 1),
            ("seq", 64), ("timestamp", 48), ("t_arrival", 16),
        ],
        "TOP_T": [
            ("valid", 1), ("book", 3), ("flags", 8), ("bid_price", 32), ("bid_shares", 32),
            ("ask_price", 32), ("ask_shares", 32), ("record_enable", 1), ("seq", 64),
            ("timestamp", 48), ("t_arrival", 16),
        ],
        "EVENT_T": [("valid", 1), ("flags", 8), ("seq", 64), ("timestamp", 48)],
    }


def width(layout: list[tuple[str, int]]) -> int:
    return sum(bits for _, bits in layout)


def pack(layout: list[tuple[str, int]], values: dict[str, int]) -> int:
    """Field values → the flat vector, first field most significant. Missing fields are 0."""
    vector = 0
    for name, bits in layout:
        value = values.get(name, 0)
        if not 0 <= value < 1 << bits:
            raise ValueError(f"{name} = {value} doesn't fit in {bits} bits")
        vector = vector << bits | value
    return vector


def unpack(layout: list[tuple[str, int]], vector: int) -> dict[str, int]:
    values = {}
    for name, bits in reversed(layout):
        values[name] = vector & ((1 << bits) - 1)
        vector >>= bits
    return values


def message_data(raw: bytes) -> int:
    """Message bytes as the token's data field: byte j in bits 8j+7 DOWNTO 8j."""
    return int.from_bytes(raw[:MSG_BYTES].ljust(MSG_BYTES, b"\x00"), "little")


# The hardware model's stage outputs as field dicts (t_arrival is cycle-based: not modelled)


def token_fields(token: Token) -> dict[str, int]:
    return {
        "valid": 1, "kind": TOKEN_KINDS[token.kind], "seq": token.seq,
        "len": token.length, "data": message_data(token.data),
    }


def op_fields(op: Op) -> dict[str, int]:
    return {
        "valid": 1, "kind": OP_KINDS[op.kind], "book": op.book_id, "side": SIDES[op.side],
        "order_ref": op.order_ref & 0xFFFF_FFFF, "ref_error": int(op.ref_error),
        "bucket0": op.bucket0, "bucket1": op.bucket1, "shares": op.shares, "seq": op.seq,
        "timestamp": op.timestamp, "stale_all": int(op.stale_all), "record_enable": int(op.record_enable),
    }


def ot_result_fields(result: OtResult) -> dict[str, int]:
    return {
        "valid": 1, "change": CHANGES[result.change], "book": result.book_id, "side": SIDES[result.side],
        "position": POSITIONS[result.position], "index": result.index, "shares": result.shares,
        "order_gone": int(result.order_gone), "book_stale": int(result.book_stale),
        "stale_all": int(result.stale_all), "record_enable": int(result.record_enable),
        "seq": result.seq, "timestamp": result.timestamp,
    }


def top_fields(top: Top) -> dict[str, int]:
    return {
        "valid": 1, "book": top.book_id, "flags": top.flags,
        "bid_price": top.top.bid_price, "bid_shares": top.top.bid_shares,
        "ask_price": top.top.ask_price, "ask_shares": top.top.ask_shares,
        "record_enable": int(top.record_enable), "seq": top.seq, "timestamp": top.timestamp,
    }


def record_bus(record: Record) -> int:
    """A record as the 256-bit output bus: byte j of the record in bits 8j+7 DOWNTO 8j."""
    return int.from_bytes(record.to_bytes(), "little")
