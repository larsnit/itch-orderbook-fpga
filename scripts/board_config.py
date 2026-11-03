"""Register writes that configure the core on the board, as devmem (Linux on the Zynq) or xsct
(JTAG) commands: feed address, tickers, bases, then a soft reset and the STATUS read to poll.

    python3.11 scripts/board_config.py                        # data/vectors/config.json, devmem
    python3.11 scripts/board_config.py --format xsct --base 0xA0010000
    python3.11 scripts/board_config.py --counters             # only the counter reads
    python3.11 scripts/board_config.py --record-output        # also switch the record output on

--base is the address the block design's Address Editor gives itch_top_0's s_axi.
"""

import argparse
import ipaddress
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from itch.config import Config  # noqa: E402
from itch.hw_model import COUNTERS  # noqa: E402

CONTROL, STATUS, VERSION = 0x000, 0x004, 0x008
GROUP_IP, PORT = 0x010, 0x014
TICKERS, BASES, COUNTER_REGS = 0x040, 0x080, 0x100
TX_CONTROL, RECORDS_DROPPED = 0x018, 0x0C0
NUM_BOOKS = 8
DEFAULT_BASE_CENTS = 100


def ticker_words(ticker: str) -> tuple[int, int]:
    raw = ticker.ljust(8).encode()
    return int.from_bytes(raw[:4], "big"), int.from_bytes(raw[4:], "big")


def config_writes(config: Config) -> list[tuple[int, int, str]]:
    """(offset, value, comment) in the order to write them."""
    writes = [
        (GROUP_IP, int(ipaddress.IPv4Address(config.feed.group)), f"group {config.feed.group}"),
        (PORT, config.feed.dst_port, f"port {config.feed.dst_port}"),
    ]
    for book in range(NUM_BOOKS):
        ticker = config.tickers[book] if book < len(config.tickers) else ""
        high, low = ticker_words(ticker)
        base = config.base_cents(book) if book < len(config.tickers) else DEFAULT_BASE_CENTS
        writes += [
            (TICKERS + 8 * book, high, f"book {book} ticker '{ticker}'"),
            (TICKERS + 8 * book + 4, low, ""),
            (BASES + 4 * book, base, f"book {book} base {base} cents"),
        ]
    return writes


def write_command(fmt: str, address: int, value: int) -> str:
    if fmt == "devmem":
        return f"devmem 0x{address:08X} 32 0x{value:08X}"
    return f"mwr 0x{address:08X} 0x{value:08X}"


def read_command(fmt: str, address: int) -> str:
    return f"devmem 0x{address:08X} 32" if fmt == "devmem" else f"mrd 0x{address:08X}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path("data/vectors/config.json"))
    parser.add_argument("--base", type=lambda text: int(text, 0), default=0xA001_0000)
    parser.add_argument("--format", choices=("devmem", "xsct"), default="devmem")
    parser.add_argument("--counters", action="store_true", help="print only the counter and STATUS reads")
    parser.add_argument("--record-output", action="store_true",
                        help="switch the record output on (default addresses: broadcast, UDP port 5000)")
    args = parser.parse_args()
    comment = "#"

    if not args.counters:
        print(f"{comment} VERSION")
        print(read_command(args.format, args.base + VERSION))
        for offset, value, note in config_writes(Config.load(args.config)):
            print(write_command(args.format, args.base + offset, value) + (f"   {comment} {note}" if note else ""))
        print(f"{comment} soft reset, then read STATUS until bit 0 (busy) is 0")
        print(write_command(args.format, args.base + CONTROL, 1))
        if args.record_output:
            print(f"{comment} record output on (frames to FF:FF:FF:FF:FF:FF, 255.255.255.255:5000)")
            print(write_command(args.format, args.base + TX_CONTROL, 1))
    print(f"{comment} STATUS: bit 0 busy, bit 1 stale_all, bits 15:8 book stale")
    print(read_command(args.format, args.base + STATUS))
    if args.counters:
        for index, name in enumerate(COUNTERS):
            print(read_command(args.format, args.base + COUNTER_REGS + 4 * index) + f"   {comment} {name}")
        print(read_command(args.format, args.base + RECORDS_DROPPED) + f"   {comment} records_dropped (record output)")


if __name__ == "__main__":
    main()