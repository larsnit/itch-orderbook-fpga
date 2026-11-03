"""Test vectors written by scripts/make_vectors.py (data/vectors, or $ITCH_VECTORS).

Per case: <case>.pcap (frames), <case>.json (per frame: accept, fcs_error, note; expected
counters), <case>_expected.bin and <case>_expected_events.bin (32-byte records), and once
config.json. pcap files carry no FCS: the bad FCS of a frame comes from the JSON.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from itch.config import Config
from itch.pcap import read_pcap
from itch.record import SIZE, Record

REPO = Path(__file__).resolve().parents[2]   # not from runner: no cocotb needed to read vectors


@dataclass(frozen=True, slots=True)
class VectorFrame:
    data: bytes
    accept: bool
    fcs_error: bool
    note: str


@dataclass
class Vector:
    name: str
    frames: list[VectorFrame]
    counters: dict[str, int]
    records: list[Record]
    events: list[Record]
    config: Config


def vector_dir() -> Path:
    return Path(os.environ.get("ITCH_VECTORS", REPO / "data" / "vectors"))


def case_names(directory: Path | None = None) -> list[str]:
    directory = directory or vector_dir()
    return sorted(path.stem for path in directory.glob("*.json") if path.stem != "config")


def load(name: str, directory: Path | None = None) -> Vector:
    directory = directory or vector_dir()
    info = json.loads((directory / f"{name}.json").read_text())
    with open(directory / f"{name}.pcap", "rb") as f:
        frames = [data for _, data in read_pcap(f)]
    if len(frames) != len(info["frames"]):
        raise ValueError(f"{name}: {len(frames)} frames in the pcap, {len(info['frames'])} in the JSON")
    return Vector(
        name=name,
        frames=[
            VectorFrame(data, meta["accept"], meta["fcs_error"], meta["note"])
            for data, meta in zip(frames, info["frames"])
        ],
        counters=info["counters"],
        records=read_records(directory / f"{name}_expected.bin"),
        events=read_records(directory / f"{name}_expected_events.bin"),
        config=Config.load(directory / "config.json"),
    )


def read_records(path: Path) -> list[Record]:
    raw = path.read_bytes()
    return [Record.from_bytes(raw[i : i + SIZE]) for i in range(0, len(raw), SIZE)]
