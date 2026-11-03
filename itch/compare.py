"""Compare the hardware model with the ideal model, message by message.

After every message for a tracked book, the hardware model's top of that book must equal the
ideal model's, unless the hardware says it can't be trusted: flag bit 1 (the true top may be
outside the window) or a stale book. Those messages are counted, not compared.
"""

from dataclasses import dataclass
from typing import Iterable

from itch.book import TopOfBook
from itch.hw_model import BOOK_TYPES, HardwareModel
from itch.messages import timestamp_of
from itch.model import Model

OPEN_NS = (9 * 3600 + 30 * 60) * 1_000_000_000
CLOSE_NS = 16 * 3600 * 1_000_000_000


@dataclass
class Comparison:
    compared: int = 0  # book messages whose tops were compared
    flagged: int = 0  # not compared: top outside the window
    flagged_regular: int = 0  # the same, during regular hours
    stale: int = 0  # not compared: book stale in the hardware
    mismatches: int = 0
    first_mismatch: str = ""


def compare(messages: Iterable[tuple[int, bytes]], ideal: Model, hardware: HardwareModel) -> Comparison:
    result = Comparison()
    for seq, raw in messages:
        ideal.process(seq, raw)
        hardware.process(seq, raw)
        if raw[:1] not in BOOK_TYPES:
            continue
        book_id = hardware.book_of(int.from_bytes(raw[1:3], "big"))
        if book_id is None:
            continue

        bids, asks = hardware.sides[book_id]["B"], hardware.sides[book_id]["S"]
        if hardware.stale_all or hardware.book_stale[book_id]:
            result.stale += 1
            continue
        if bids.outside() or asks.outside():
            result.flagged += 1
            if OPEN_NS <= timestamp_of(raw) < CLOSE_NS:
                result.flagged_regular += 1
            continue

        result.compared += 1
        hardware_top = TopOfBook(*bids.top(), *asks.top())
        if hardware_top != ideal.last_top[book_id]:
            result.mismatches += 1
            if not result.first_mismatch:
                result.first_mismatch = (
                    f"seq {seq}, book {hardware.config.tickers[book_id]}: "
                    f"hardware {hardware_top}, ideal {ideal.last_top[book_id]}"
                )
    return result
