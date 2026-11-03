"""Configuration of the hardware model: what the core's registers and generics hold.

    {
     "tickers": ["AAPL", "MSFT", ...],
     "reference_cents": {"AAPL": 28944, ...},
     "ladder_bits": 13, "bucket_bits": 13, "ways": 8, "stash": 32
    }

Each book's window covers 2**ladder_bits cents from its base. The base is the reference
minus half the window, but never below $1.00 (sub-penny prices are only allowed below $1,
so they always fall below the window).
"""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from itch.framing import DEFAULT_FEED, Feed

MIN_BASE_CENTS = 100


@dataclass
class Config:
    tickers: list[str]
    reference_cents: dict[str, int]
    ladder_bits: int = 13
    bucket_bits: int = 13
    ways: int = 8
    stash: int = 32
    feed: Feed = field(default=DEFAULT_FEED)

    @property
    def window(self) -> int:
        """Ladder levels per book side, one per cent."""
        return 1 << self.ladder_bits

    def base_cents(self, book_id: int) -> int:
        """First cent of the book's window."""
        reference = self.reference_cents[self.tickers[book_id]]
        return max(MIN_BASE_CENTS, reference - self.window // 2)

    def save(self, path: Path) -> None:
        values = asdict(self)
        del values["feed"]
        path.write_text(json.dumps(values, indent=1) + "\n")

    @classmethod
    def load(cls, path: Path) -> "Config":
        return cls(**json.loads(path.read_text()))
