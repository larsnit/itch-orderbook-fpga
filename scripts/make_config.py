"""Write the hardware model's configuration for a trading day.

    python3.11 scripts/make_config.py data/12302019.NASDAQ_ITCH50 -o data/config.json

Each book's reference price is its opening cross (the same pass as stats.py), standing in
for the previous close that software would configure before the open. --ref TICKER=PRICE
(in dollars) sets one by hand.
"""

import argparse
import sys
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itch.config import Config  # noqa: E402
from itch.messages import read_stream  # noqa: E402
from run_model import DEFAULT_TICKERS  # noqa: E402
from stats import opening_crosses  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("itch_file", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--tickers", nargs="+", default=DEFAULT_TICKERS)
    parser.add_argument("--ref", nargs="+", default=[], metavar="TICKER=PRICE", help="reference price in dollars")
    parser.add_argument("--limit", type=int, help="stop the opening-cross search after this many messages")
    parser.add_argument("--ladder-bits", type=int, default=13)
    parser.add_argument("--bucket-bits", type=int, default=13)
    parser.add_argument("--ways", type=int, default=8)
    parser.add_argument("--stash", type=int, default=32)
    args = parser.parse_args()

    reference_cents = {}
    for item in args.ref:
        ticker, price = item.split("=")
        reference_cents[ticker] = round(float(price) * 100)
    missing = [ticker for ticker in args.tickers if ticker not in reference_cents]
    if missing:
        with open(args.itch_file, "rb", buffering=1 << 24) as source:
            found = opening_crosses(islice(read_stream(source), args.limit), missing)
        if still_missing := [ticker for ticker in missing if ticker not in found]:
            sys.exit(f"no opening cross for {' '.join(still_missing)}; give it with --ref TICKER=PRICE")
        for ticker, price in found.items():
            reference_cents[ticker] = price // 100

    config = Config(
        args.tickers,
        {ticker: reference_cents[ticker] for ticker in args.tickers},
        args.ladder_bits,
        args.bucket_bits,
        args.ways,
        args.stash,
    )
    config.save(args.output)
    for book_id, ticker in enumerate(config.tickers):
        base = config.base_cents(book_id)
        print(f"  {ticker:<6} reference ${reference_cents[ticker] / 100:>9.2f}   window ${base / 100:.2f} - ${(base + config.window - 1) / 100:.2f}")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
