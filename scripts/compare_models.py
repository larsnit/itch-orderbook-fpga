"""Run the ideal and the hardware model side by side on an ITCH file and compare their tops.

    python3.11 scripts/compare_models.py data/12302019.NASDAQ_ITCH50 --config data/config.json

Passes when no compared top differs. Tops the hardware flags (outside the window, or a stale
book) are counted instead of compared; on the reference day none should be flagged during
regular hours.
"""

import argparse
import sys
import time
from itertools import islice
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from itch.compare import compare  # noqa: E402
from itch.config import Config  # noqa: E402
from itch.hw_model import HardwareModel  # noqa: E402
from itch.messages import read_stream  # noqa: E402
from itch.model import Model, number  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("itch_file", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="stop after this many messages")
    args = parser.parse_args()

    config = Config.load(args.config)
    ideal = Model(config.tickers)
    hardware = HardwareModel(config)
    start = time.monotonic()
    with open(args.itch_file, "rb", buffering=1 << 24) as source:
        result = compare(number(islice(read_stream(source), args.limit)), ideal, hardware)

    print(f"compared in {time.monotonic() - start:.0f} s")
    print(f"book messages compared:      {result.compared:,}")
    print(f"flagged (outside window):    {result.flagged:,}, of which {result.flagged_regular:,} in regular hours")
    print(f"not compared (stale book):   {result.stale:,}")
    print(f"mismatches:                  {result.mismatches:,}")
    for name, value in hardware.counters.items():
        if value:
            print(f"  {name:<20} {value:,}")
    if result.mismatches:
        sys.exit(f"first mismatch: {result.first_mismatch}")
    print("PASS")


if __name__ == "__main__":
    main()
