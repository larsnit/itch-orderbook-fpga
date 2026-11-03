"""Expected output of every stage, from the hardware model (implementation §9.3).

run_model() feeds frames through itch.hw_model with taps on and returns each stage's stream
as field dicts in the layout of structs.py, ready for scoreboard.compare against the DUT's
unpacked output.
"""

from dataclasses import dataclass
from typing import Iterable

from itch.config import Config
from itch.hw_model import HardwareModel
from itch.record import Record
from tb.common import structs


@dataclass
class Expected:
    tokens: list[dict[str, int]]
    ops: list[dict[str, int]]
    ot_results: list[dict[str, int]]
    tops: list[dict[str, int]]
    records: list[Record]
    events: list[Record]
    counters: dict[str, int]


def run_model(frames: Iterable[tuple[bytes, bool]], config: Config) -> Expected:
    """frames: (frame bytes, bad FCS) in arrival order."""
    model = HardwareModel(config, taps=True)
    records, events = [], []
    for data, fcs_error in frames:
        more_records, more_events = model.process_frame(data, fcs_error)
        records += more_records
        events += more_events
    taps = model.taps
    return Expected(
        tokens=[structs.token_fields(token) for token in taps.tokens],
        ops=[structs.op_fields(op) for op in taps.ops],
        ot_results=[structs.ot_result_fields(result) for result in taps.ot_results],
        tops=[structs.top_fields(top) for top in taps.tops],
        records=records,
        events=events,
        counters=dict(model.counters),
    )


def record_from_bus(value: int) -> Record:
    """The inverse of structs.record_bus: a 256-bit output bus value as a Record."""
    return Record.from_bytes(value.to_bytes(32, "little"))
