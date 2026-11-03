"""Compare records captured by the ILA on the board with the hardware model, for a vector replayed
with tcpreplay.

    python3.11 scripts/compare_ila.py records.csv clean --loops 20
    python3.11 scripts/compare_ila.py records.csv example.pcap --loops 20   (any pcap, config.json)

The CSV comes from the Hardware Manager's Tcl console:

    write_hw_ila_data -force -csv_file records.csv [upload_hw_ila_data hw_ila_1]

ILA probes (found by name, or by --columns): m_rec_tdata (256), m_rec_tvalid, m_evt_tdata (256),
m_evt_tvalid, and rx_frame_end so the buffer fills with the replay loops.
Only samples with a tvalid are records; the order within one sample is status record first.

The expected records come from the model, not from <vector>_expected.bin, because on the wire:
- the NIC computes the FCS, so frames the vector marks as bad FCS arrive good;
- the NIC pads frames shorter than 60 bytes with zeros;
- with --loops N the pcap is played N times (later loops are duplicates for the core).
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itch.config import Config  # noqa: E402
from itch.hw_model import HardwareModel  # noqa: E402
from itch.pcap import read_pcap  # noqa: E402
from itch.record import Record  # noqa: E402
from tb.common import vectors  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402
from tb.common.taps import record_from_bus  # noqa: E402

MIN_FRAME = 60   # bytes without FCS
ROLES = {
    "rec_data": ("rec_tdata", "probe0"),
    "rec_valid": ("rec_tvalid", "probe1"),
    "evt_data": ("evt_tdata", "probe2"),
    "evt_valid": ("evt_tvalid", "probe3"),
}


def on_the_wire(frame: bytes) -> bytes:
    return frame.ljust(MIN_FRAME, b"\x00")


def expected(vector: vectors.Vector, loops: int) -> tuple[list[Record], list[Record]]:
    return expected_frames([frame.data for frame in vector.frames], vector.config, loops)


def load_case(case: str, config_path: Path) -> tuple[list[bytes], Config]:
    """A vector name, or a pcap file with the configuration from config_path."""
    if case.endswith(".pcap"):
        with open(case, "rb") as f:
            return [data for _, data in read_pcap(f)], Config.load(config_path)
    vector = vectors.load(case)
    return [frame.data for frame in vector.frames], vector.config


def expected_frames(frames: list[bytes], config: Config, loops: int) -> tuple[list[Record], list[Record]]:
    model = HardwareModel(config)
    records, events = [], []
    for _ in range(loops):
        for frame in frames:
            more_records, more_events = model.process_frame(on_the_wire(frame), False)
            records += more_records
            events += more_events
    return records, events


def parse_value(text: str, radix: str) -> int:
    text = text.strip()
    if radix == "HEX":
        return int(text, 16)
    if radix == "BINARY":
        return int(text, 2)
    return int(text)


def find_column(header: list[str], candidates: tuple[str, ...], role: str) -> int:
    for candidate in candidates:
        for index, name in enumerate(header):
            if candidate in name:
                return index
    raise SystemExit(f"no column for {role} (looked for {', '.join(candidates)}) in: {', '.join(header)}")


def read_capture(path: Path, overrides: dict[str, str]) -> tuple[list[Record], list[Record], int]:
    """Records and status records in capture order, and the number of samples."""
    with open(path, newline="") as f:
        rows = [row for row in csv.reader(f) if row]
    header = rows[0]
    radices = ["UNSIGNED"] * len(header)
    data = rows[1:]
    if data and data[0][0].startswith("Radix"):
        radices = [cell.replace("Radix - ", "").strip().upper() for cell in data[0]]
        data = data[1:]
    columns = {
        role: find_column(header, (overrides[role],) if role in overrides else candidates, role)
        for role, candidates in ROLES.items()
    }
    records, events = [], []
    for row in data:
        def value(role: str) -> int:
            return parse_value(row[columns[role]], radices[columns[role]])
        if value("evt_valid"):
            events.append(record_from_bus(value("evt_data")))
        if value("rec_valid"):
            records.append(record_from_bus(value("rec_data")))
    return records, events, len(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv", type=Path)
    parser.add_argument("vector", help="vector name (e.g. clean) or a .pcap file")
    parser.add_argument("--config", type=Path, default=vectors.vector_dir() / "config.json",
                        help="configuration for a .pcap (default: the vectors' config.json)")
    parser.add_argument("--loops", type=int, default=1, help="how often tcpreplay played the pcap")
    parser.add_argument("--columns", nargs="*", default=[], metavar="ROLE=TEXT",
                        help=f"column name parts, roles: {', '.join(ROLES)}")
    args = parser.parse_args()
    overrides = dict(item.split("=", 1) for item in args.columns)

    frames, config = load_case(args.vector, args.config)
    want_records, want_events = expected_frames(frames, config, args.loops)
    got_records, got_events, samples = read_capture(args.csv, overrides)
    print(f"{samples} samples: {len(got_records)} records, {len(got_events)} status records; "
          f"expected {len(want_records)} and {len(want_events)}")
    if len(got_records) < len(want_records) or len(got_events) < len(want_events):
        print("capture shorter than expected: comparing the captured part only (more loops, or a deeper ILA)")
        want_records, want_events = want_records[: len(got_records)], want_events[: len(got_events)]
    compare(want_records, got_records, f"{args.vector}: records")
    compare(want_events, got_events, f"{args.vector}: status records")
    print("match")


if __name__ == "__main__":
    main()
