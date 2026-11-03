"""Run a model on a Nasdaq ITCH file or a pcap and write the expected records.

    python3.11 scripts/run_model.py data/12302019.NASDAQ_ITCH50 -o data/expected.bin --csv data/expected.csv
    python3.11 scripts/run_model.py data/day.pcap -o data/expected_pcap.bin
    python3.11 scripts/run_model.py data/day.pcap -o data/expected_hw.bin --config data/config.json

Without --config: the ideal model (unlimited books). With --config: the hardware model, which
also writes its status records (default: <output>_events.bin).

Input ending in .pcap is read as Ethernet frames; anything else as a Nasdaq ITCH file.
--limit counts messages for an ITCH file and frames for a pcap.
"""

import argparse
import csv
import sys
import time
from collections import Counter
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itch.book import NO_ASK_PRICE, NO_BID_PRICE  # noqa: E402
from itch.config import Config  # noqa: E402
from itch.hw_model import HardwareModel  # noqa: E402
from itch.messages import read_stream  # noqa: E402
from itch.model import Model, number  # noqa: E402
from itch.pcap import read_pcap  # noqa: E402

DEFAULT_TICKERS = ["AAPL", "MSFT", "TSLA", "QQQ", "NFLX", "COST", "SIRI", "ZM"]


def format_time(ns: int) -> str:
    seconds, nanos = divmod(ns, 1_000_000_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{nanos:09}"


def format_price(price: int, empty: int) -> str:
    return "" if price == empty else f"{price / 10_000:.4f}"


def ideal_outputs(model: Model, source, is_pcap: bool, limit: int | None):
    """(records, events) per input item, from the ideal model (it has no events)."""
    if is_pcap:
        for _, frame in islice(read_pcap(source), limit):
            yield model.process_frame(frame), []
    else:
        for seq, raw in number(islice(read_stream(source), limit)):
            yield model.process(seq, raw), []


def hardware_outputs(model: HardwareModel, source, is_pcap: bool, limit: int | None):
    if is_pcap:
        for _, frame in islice(read_pcap(source), limit):
            yield model.process_frame(frame)
    else:
        for seq, raw in number(islice(read_stream(source), limit)):
            yield model.process(seq, raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="Nasdaq ITCH file or .pcap")
    parser.add_argument("-o", "--output", type=Path, required=True, help="binary records, 32 bytes each")
    parser.add_argument("--csv", type=Path, help="the same records, human-readable")
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS, help="ideal model only")
    parser.add_argument("--config", type=Path, help="run the hardware model with this configuration")
    parser.add_argument("--events", type=Path, help="status records (hardware model; default <output>_events.bin)")
    parser.add_argument("--limit", type=int, help="stop after this many messages (frames for a pcap)")
    args = parser.parse_args()

    is_pcap = args.input.suffix == ".pcap"
    if args.config:
        config = Config.load(args.config)
        tickers = config.tickers
        model = HardwareModel(config)
        events_path = args.events or args.output.with_name(args.output.stem + "_events.bin")
    else:
        tickers = args.tickers
        model = Model(tickers)
        events_path = None

    per_book = Counter()
    num_events = 0
    start = time.monotonic()
    with open(args.input, "rb", buffering=1 << 24) as source, open(args.output, "wb") as out:
        events_file = open(events_path, "wb") if events_path else None
        csv_file = open(args.csv, "w", newline="") if args.csv else None
        writer = csv.writer(csv_file) if csv_file else None
        if writer:
            writer.writerow(["seq", "time", "ticker", "flags", "bid_px", "bid_sz", "ask_px", "ask_sz"])

        if args.config:
            outputs = hardware_outputs(model, source, is_pcap, args.limit)
        else:
            outputs = ideal_outputs(model, source, is_pcap, args.limit)
        for records, events in outputs:
            for event in events:
                events_file.write(event.to_bytes())
                num_events += 1
            for record in records:
                out.write(record.to_bytes())
                per_book[record.book_id] += 1
                if writer:
                    top = record.top
                    writer.writerow([
                        record.seq,
                        format_time(record.timestamp),
                        tickers[record.book_id],
                        record.flags,
                        format_price(top.bid_price, NO_BID_PRICE),
                        top.bid_shares,
                        format_price(top.ask_price, NO_ASK_PRICE),
                        top.ask_shares,
                    ])
        for extra in (csv_file, events_file):
            if extra:
                extra.close()

    elapsed = time.monotonic() - start
    print(f"messages:     {model.next_seq - 1:,} (last sequence number) in {elapsed:.0f} s")
    print(f"records:      {sum(per_book.values()):,}")
    for book_id, ticker in enumerate(tickers):
        print(f"  {ticker:<6} {per_book[book_id]:>12,}")
    if args.config:
        print(f"status records: {num_events:,} → {events_path}")
        for name, value in model.counters.items():
            print(f"  {name:<20} {value:,}")
    else:
        print(f"open orders:  {len(model.books.orders):,}")
        print(f"unknown refs: {model.books.unknown_refs:,}")
        print(f"inconsistent: {model.books.inconsistent:,}")
        print(f"gaps:         {model.gaps:,}")
        print(f"duplicates:   {model.duplicates:,}")
        if is_pcap:
            print(f"dropped frames:    {model.dropped_frames:,}")
            print(f"malformed packets: {model.malformed_packets:,}")
    if missing := model.missing_tickers():
        print(f"not in file:  {' '.join(missing)}")


if __name__ == "__main__":
    main()
