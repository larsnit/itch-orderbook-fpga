"""Check a pcap before replaying it into the board: is it our feed, is it complete, how long
will the replay take, what should the board count. Reads the file once, streaming.

    python3.11 scripts/check_pcap.py data/day20m.pcap
    python3.11 scripts/check_pcap.py data/day20m.pcap --model      # also run the hardware model

Without --model: frames, feed addresses, MoldUDP64 sessions and sequence numbers (gaps,
duplicates), messages per type, messages of the configured tickers, time span, replay times.
With --model: the records, status records and counters the board must show afterwards (takes a
while for a whole day; nothing is kept in memory but counts).
"""

import argparse
import ipaddress
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itch.config import Config  # noqa: E402
from itch.framing import DEFAULT_FEED, frame_payload, read_packet  # noqa: E402
from itch.pcap import read_pcap  # noqa: E402

LINE_RATE = 10e9
WIRE_OVERHEAD = 24   # preamble + SFD (8), FCS (4), minimum gap (12)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pcap", type=Path)
    parser.add_argument("--config", type=Path, default=Path("data/vectors/config.json"))
    parser.add_argument("--model", action="store_true", help="also run the hardware model")
    args = parser.parse_args()

    config = Config.load(args.config)
    model = None
    if args.model:
        from itch.hw_model import HardwareModel
        model = HardwareModel(config)

    frames = feed_frames = other_frames = wire_bytes = 0
    first_ns = last_ns = None
    max_length = 0
    destinations: Counter = Counter()
    sessions: Counter = Counter()
    types: Counter = Counter()
    ticker_messages: Counter = Counter()
    locates: dict[int, str] = {}
    expected_seq: dict[str, int] = {}
    gaps = duplicates = heartbeats = broken = 0
    records = events = 0
    started = time.time()

    with open(args.pcap, "rb") as f:
        for time_ns, frame in read_pcap(f):
            frames += 1
            first_ns = time_ns if first_ns is None else first_ns
            last_ns = time_ns
            max_length = max(max_length, len(frame))
            wire_bytes += max(len(frame), 60) + WIRE_OVERHEAD
            if len(frame) >= 38 and frame[12:14] == b"\x08\x00":
                destinations[(str(ipaddress.IPv4Address(frame[30:34])), int.from_bytes(frame[36:38], "big"))] += 1
            if model is not None:
                more_records, more_events = model.process_frame(frame.ljust(60, b"\x00"), False)
                records += len(more_records)
                events += len(more_events)
            payload = frame_payload(frame, config.feed)
            if payload is None:
                other_frames += 1
                continue
            feed_frames += 1
            parsed = read_packet(payload)
            if parsed is None:
                broken += 1
                continue
            packet, well_formed = parsed
            broken += not well_formed
            sessions[packet.session] += 1
            if not packet.messages:
                heartbeats += 1
            expected = expected_seq.get(packet.session, packet.seq)
            if packet.seq > expected:
                gaps += 1
            elif packet.seq < expected and packet.messages:
                duplicates += 1
            expected_seq[packet.session] = max(expected, packet.seq + len(packet.messages))
            for message in packet.messages:
                kind = message[:1].decode("ascii", "replace")
                types[kind] += 1
                locate = int.from_bytes(message[1:3], "big") if len(message) >= 3 else 0
                if kind == "R" and len(message) >= 19:
                    stock = message[11:19].decode("ascii", "replace").rstrip()
                    if stock in config.tickers:
                        locates[locate] = stock
                if locate in locates:
                    ticker_messages[locates[locate]] += 1
            if frames % 5_000_000 == 0:
                print(f"  ... {frames:,} frames, {time.time() - started:.0f} s", file=sys.stderr)

    span = (last_ns - first_ns) / 1e9 if frames > 1 else 0.0
    print(f"file              {args.pcap} ({args.pcap.stat().st_size / 1e9:.2f} GB)")
    print(f"frames            {frames:,} (feed {feed_frames:,}, other {other_frames:,}), longest {max_length} bytes")
    print(f"destinations      " + ", ".join(f"{ip}:{port} x{count:,}" for (ip, port), count in destinations.most_common(4)))
    print(f"feed configured   {config.feed.group}:{config.feed.dst_port}")
    print(f"sessions          " + ", ".join(f"'{name}' x{count:,}" for name, count in sessions.most_common(3)))
    print(f"sequence          up to {max(expected_seq.values(), default=1) - 1:,}; gaps {gaps}, duplicate packets {duplicates}, "
          f"heartbeats {heartbeats}, broken packets {broken}")
    print(f"messages          {sum(types.values()):,}: " + ", ".join(f"{k} {v:,}" for k, v in types.most_common()))
    print(f"configured books  " + ", ".join(f"{t} {ticker_messages[t]:,}" for t in config.tickers)
          + ("" if locates else "   (no R message for them: books stay empty)"))
    print(f"time span         {span:,.1f} s in the capture's timestamps (tcpreplay without --topspeed takes this long)")
    print(f"at 10 Gb/s        {wire_bytes * 8 / LINE_RATE:,.1f} s on the wire ({wire_bytes / 1e9:.2f} GB incl. preamble, FCS, gap); "
          "tcpreplay --topspeed reaches this only if the PC keeps up")
    if model is not None:
        print(f"model             {records:,} records, {events:,} status records")
        for name, value in model.counters.items():
            print(f"  {name:20s} {value:,}")
    if gaps or duplicates or broken or other_frames:
        print("note: gaps/duplicates/broken packets/other frames are counted by the board too (compare with its counters)")


if __name__ == "__main__":
    main()
