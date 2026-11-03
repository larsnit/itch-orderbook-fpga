"""Turn a Nasdaq ITCH file into a pcap of MoldUDP64 frames, as the FPGA will receive them.

    python scripts/make_pcap.py data/12302019.NASDAQ_ITCH50 -o data/day.pcap --limit 20000000

Messages are numbered from 1 and packed into packets up to the MTU. Each frame is stamped
with the trading day's date plus the ITCH timestamp of its last message, so tcpreplay
reproduces the day's timing and Wireshark shows real times.
"""

import argparse
import re
import sys
from datetime import datetime
from itertools import islice
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itch.framing import DEFAULT_FEED, MAX_PAYLOAD, build_frame, packetize  # noqa: E402
from itch.messages import read_stream, timestamp_of  # noqa: E402
from itch.pcap import write_pcap  # noqa: E402

SESSION = "ITCHFPGA01"


def midnight_ns(date: str) -> int:
    """Midnight New York time on an MMDDYYYY date, in ns since 1970."""
    day = datetime.strptime(date, "%m%d%Y").replace(tzinfo=ZoneInfo("America/New_York"))
    return int(day.timestamp()) * 1_000_000_000


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("itch_file", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="stop after this many messages")
    parser.add_argument("--date", help="trading day as MMDDYYYY (default: from the file name)")
    parser.add_argument("--max-payload", type=int, default=MAX_PAYLOAD, help="largest UDP payload in bytes")
    parser.add_argument("--max-delay-ns", type=int, help="close a packet when messages are further apart")
    args = parser.parse_args()

    date = args.date
    if date is None:
        match = re.match(r"\d{8}", args.itch_file.name)
        if match is None:
            parser.error("can't read the date from the file name, use --date MMDDYYYY")
        date = match.group()
    midnight = midnight_ns(date)

    with open(args.itch_file, "rb", buffering=1 << 24) as source, open(args.output, "wb", buffering=1 << 24) as out:
        messages = islice(read_stream(source), args.limit)
        packets = packetize(messages, SESSION, max_payload=args.max_payload, max_delay_ns=args.max_delay_ns)
        frames = (
            (midnight + timestamp_of(packet.messages[-1]), build_frame(DEFAULT_FEED, packet.to_bytes(), ip_id=i))
            for i, packet in enumerate(packets)
        )
        write_pcap(out, frames)

    print(f"wrote {args.output} ({args.output.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
