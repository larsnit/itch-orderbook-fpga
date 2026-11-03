"""Compare the records the board sent back over Ethernet (record output, implementation section
12.8a) with the hardware model, for a vector or pcap replayed with tcpreplay.

    sudo tcpdump -i <nic> -B 262144 -w records.pcap udp port 5000      (start before the replay)
    python3.11 scripts/compare_capture.py records.pcap clean --loops 20
    python3.11 scripts/compare_capture.py records.pcap example.pcap --loops 20

The capture must be a classic pcap (tcpdump -w writes one). Frames that aren't record frames
are skipped. Checks: frame counters without gaps (a gap = frames the PC lost), the board's
dropped-records count, then records and status records against the model, as compare_ila.py.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_ila import expected_frames, load_case  # noqa: E402
from itch import record_frames  # noqa: E402
from itch.pcap import read_pcap  # noqa: E402
from tb.common import vectors  # noqa: E402
from tb.common.scoreboard import compare  # noqa: E402

STATUS_BOOK_ID = 0xFF


def read_capture(path: Path, port: int) -> list[record_frames.RecordFrame]:
    with open(path, "rb") as f:
        frames = [record_frames.parse(data, port) for _, data in read_pcap(f)]
    return [frame for frame in frames if frame is not None]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", type=Path, help="tcpdump capture of the record frames")
    parser.add_argument("case", help="vector name (e.g. clean) or the .pcap that was replayed")
    parser.add_argument("--loops", type=int, default=1, help="how often tcpreplay played it")
    parser.add_argument("--port", type=int, default=record_frames.DEFAULT_PORT, help="record frames' UDP port")
    parser.add_argument("--config", type=Path, default=vectors.vector_dir() / "config.json")
    args = parser.parse_args()

    frames = read_capture(args.capture, args.port)
    if not frames:
        raise SystemExit(f"no record frames to port {args.port} in {args.capture}")
    counters = [frame.frame_counter for frame in frames]
    missing = sorted(set(range(counters[0], counters[-1] + 1)) - set(counters))
    got = [record for frame in frames for record in frame.records]
    got_records = [record for record in got if record.book_id != STATUS_BOOK_ID]
    got_events = [record for record in got if record.book_id == STATUS_BOOK_ID]

    wire_frames, config = load_case(args.case, args.config)
    want_records, want_events = expected_frames(wire_frames, config, args.loops)

    print(f"{len(frames)} record frames (counters {counters[0]}..{counters[-1]}), "
          f"{len(got_records)} records, {len(got_events)} status records; "
          f"expected {len(want_records)} and {len(want_events)}")
    dropped = frames[-1].dropped - frames[0].dropped
    print(f"board: {dropped} records dropped during the capture ({frames[-1].dropped} since its reset)")
    if missing:
        print(f"{len(missing)} frames lost on the PC (first: {missing[:10]}): "
              "bigger tcpdump buffer (-B), or replay slower")
    if dropped or missing:
        print("records are missing: comparing only what arrived is not meaningful, the first mismatch shows where")
    compare(want_records, got_records, f"{args.case}: records")
    compare(want_events, got_events, f"{args.case}: status records")
    print("match")


if __name__ == "__main__":
    main()
