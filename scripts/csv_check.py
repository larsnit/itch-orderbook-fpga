"""Sanity checks on the golden model's CSV output.

    python scripts/check_csv.py data/expected.csv

Per ticker: price range, median spread, and quotes where bid >= ask. Outside regular
hours a few crossed or locked quotes are normal; during 09:30-16:00 they point to a bug.
Also checks what the model guarantees: sequence numbers rise, time never goes backwards,
and a ticker never gets two identical rows in a row.
"""

import csv
import sys
from collections import defaultdict
from statistics import median

REGULAR_HOURS = ("09:30:00", "16:00:00")
EXAMPLES = 5


def main(path: str) -> None:
    rows = defaultdict(int)
    stale = defaultdict(int)
    prices = defaultdict(list)
    spreads = defaultdict(list)
    crossed_regular = defaultdict(int)
    crossed_other = defaultdict(int)
    last_quote = {}
    problems = []
    last_seq, last_time = 0, ""

    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            ticker, seq, time = row["ticker"], int(row["seq"]), row["time"]
            quote = (row["stale"], row["bid_px"], row["bid_sz"], row["ask_px"], row["ask_sz"])

            if seq <= last_seq:
                problems.append(f"seq {seq} after {last_seq}")
            if time < last_time:
                problems.append(f"seq {seq}: time {time} before {last_time}")
            if last_quote.get(ticker) == quote:
                problems.append(f"seq {seq}: {ticker} repeats its previous row")
            last_seq, last_time, last_quote[ticker] = seq, time, quote

            rows[ticker] += 1
            stale[ticker] += row["stale"] == "1"
            if not (row["bid_px"] and row["ask_px"]):
                continue

            bid, ask = float(row["bid_px"]), float(row["ask_px"])
            prices[ticker] += [bid, ask]
            spreads[ticker].append(ask - bid)
            if bid >= ask:
                in_regular_hours = REGULAR_HOURS[0] <= time < REGULAR_HOURS[1]
                if in_regular_hours:
                    crossed_regular[ticker] += 1
                    if crossed_regular[ticker] <= EXAMPLES:
                        problems.append(f"seq {seq} {time}: {ticker} crossed, bid {bid} >= ask {ask}")
                else:
                    crossed_other[ticker] += 1

    print(f"{'ticker':<7}{'rows':>10}{'stale':>8}{'low':>10}{'high':>10}{'spread':>9}{'crossed 9:30-16':>17}{'outside':>9}")
    for ticker in rows:
        low = f"{min(prices[ticker]):.2f}" if prices[ticker] else "-"
        high = f"{max(prices[ticker]):.2f}" if prices[ticker] else "-"
        spread = f"{median(spreads[ticker]):.4f}" if spreads[ticker] else "-"
        print(
            f"{ticker:<7}{rows[ticker]:>10,}{stale[ticker]:>8,}{low:>10}{high:>10}{spread:>9}"
            f"{crossed_regular[ticker]:>17,}{crossed_other[ticker]:>9,}"
        )

    print()
    if problems:
        print(f"{len(problems)} problem(s), first ones:")
        for problem in problems[:20]:
            print("  " + problem)
    else:
        print("no problems found")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/check_csv.py <expected.csv>")
    main(sys.argv[1])