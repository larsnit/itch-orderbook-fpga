"""Write the generator's test cases as pcaps with the hardware model's expected output.

    python3.11 scripts/make_vectors.py -o data/vectors
    python3.11 scripts/make_vectors.py -o data/vectors --case gap heartbeat_gap
    python3.11 scripts/make_vectors.py -o data/vectors --case min_frame min_frames min_add min_book

For each case:
    <case>.pcap                 the frames, 1 µs apart, from 09:30 on the reference day
    <case>_expected.bin         the book records, 32 bytes each
    <case>_expected_events.bin  the status records
    <case>.json                 description, expected counters, and per frame: accept, fcs_error, length, note
and once, config.json: the hardware configuration all cases use.

pcap files have no FCS, so a frame the MAC must flag with a bad FCS is only marked in the
JSON (fcs_error); the testbench drives it on tuser at the end of that frame.

The script checks that the model's counters match the ones the case is built for, and
stops with an error if not.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itch.generator import CASES, COUNTERS, MINIMAL_CASES, OPEN_NS, Case, generator_config  # noqa: E402
from itch.hw_model import HardwareModel  # noqa: E402
from itch.pcap import write_pcap  # noqa: E402
from make_pcap import midnight_ns  # noqa: E402

REFERENCE_DAY = "12302019"
FRAME_SPACING_NS = 1_000


def write_case(case: Case, out_dir: Path) -> tuple[int, int, dict[str, int]]:
    start = midnight_ns(REFERENCE_DAY) + OPEN_NS
    with open(out_dir / f"{case.name}.pcap", "wb") as f:
        write_pcap(f, ((start + i * FRAME_SPACING_NS, frame.data) for i, frame in enumerate(case.frames)))

    model = HardwareModel(generator_config())
    records, events = [], []
    for frame in case.frames:
        more_records, more_events = model.process_frame(frame.data, frame.fcs_error)
        records += more_records
        events += more_events
    (out_dir / f"{case.name}_expected.bin").write_bytes(b"".join(record.to_bytes() for record in records))
    (out_dir / f"{case.name}_expected_events.bin").write_bytes(b"".join(event.to_bytes() for event in events))

    counters = {name: model.counters[name] for name in COUNTERS}
    info = {
        "name": case.name,
        "description": case.description,
        "records": len(records),
        "events": len(events),
        "counters": case.expected_counters(),
        "frames": [
            {"index": i, "accept": frame.accept, "fcs_error": frame.fcs_error, "length": len(frame.data), "note": frame.note}
            for i, frame in enumerate(case.frames)
        ],
    }
    (out_dir / f"{case.name}.json").write_text(json.dumps(info, indent=1) + "\n")
    return len(records), len(events), counters


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", type=Path, required=True, help="directory for the vectors")
    parser.add_argument("--case", nargs="+", help="only these cases (default: all)")
    args = parser.parse_args()

    makers = {make.__name__: make for make in CASES + MINIMAL_CASES}
    names = args.case or list(makers)
    if unknown := set(names) - set(makers):
        parser.error(f"unknown case(s): {', '.join(sorted(unknown))}; known: {', '.join(makers)}")
    args.output.mkdir(parents=True, exist_ok=True)
    generator_config().save(args.output / "config.json")

    failed = []
    print(f"{'case':<18} {'frames':>6} {'records':>7} {'events':>6}  counters")
    for name in names:
        case = makers[name]()
        num_records, num_events, counters = write_case(case, args.output)
        nonzero = ", ".join(f"{k}={v}" for k, v in counters.items() if v) or "-"
        print(f"{name:<18} {len(case.frames):>6} {num_records:>7} {num_events:>6}  {nonzero}")
        if counters != case.expected_counters():
            failed.append(f"{name}: model gave {counters}, case expects {case.expected_counters()}")

    if failed:
        sys.exit("counter mismatch:\n  " + "\n  ".join(failed))
    print(f"wrote {len(names)} cases to {args.output}")


if __name__ == "__main__":
    main()
