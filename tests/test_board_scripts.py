"""scripts/compare_ila.py on a CSV laid out like Vivado's ILA export, and scripts/board_config.py."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import board_config  # noqa: E402
import compare_ila  # noqa: E402
from itch.config import Config  # noqa: E402
from tb.common import vectors  # noqa: E402

pytestmark = pytest.mark.skipif(not vectors.case_names(), reason="no vectors in data/vectors")

HEADER = [
    "Sample in Buffer", "Sample in Window", "TRIGGER",
    "design_1_i/itch_top_0_m_rec_tdata[255:0]", "design_1_i/itch_top_0_m_rec_tvalid",
    "design_1_i/itch_top_0_m_evt_tdata[255:0]", "design_1_i/itch_top_0_m_evt_tvalid",
    "design_1_i/itch_top_0_rx_frame_end",
]


def bus(record) -> str:
    return int.from_bytes(record.to_bytes(), "little").to_bytes(32, "big").hex().upper()


def write_csv(path: Path, records, events, frame_ends: int = 5) -> None:
    rows = [",".join(HEADER), "Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,BINARY,HEX,BINARY,BINARY"]
    zero = "0" * 64
    samples = [(None, events[0])] if events else []
    samples += [(record, None) for record in records] + [(None, None)] * frame_ends
    for index, (record, event) in enumerate(samples):
        rows.append(",".join([
            str(index), str(index), "1" if index == 0 else "0",
            bus(record) if record else zero, "1" if record else "0",
            bus(event) if event else zero, "1" if event else "0",
            "0" if record or event else "1",
        ]))
    path.write_text("\n".join(rows) + "\n")


def run_compare(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "scripts" / "compare_ila.py"), *args],
                          capture_output=True, text=True, cwd=REPO)


def test_a_matching_capture_passes(tmp_path):
    records, events = compare_ila.expected(vectors.load("gap"), 1)
    write_csv(tmp_path / "capture.csv", records, events)
    result = run_compare(str(tmp_path / "capture.csv"), "gap", "--loops", "3")
    assert result.returncode == 0 and "match" in result.stdout, result.stdout + result.stderr


def test_a_wrong_record_fails(tmp_path):
    records, events = compare_ila.expected(vectors.load("clean"), 1)
    records[10], records[11] = records[11], records[10]
    write_csv(tmp_path / "capture.csv", records, events)
    result = run_compare(str(tmp_path / "capture.csv"), "clean")
    assert result.returncode != 0 and "records" in result.stderr


def test_a_missing_record_fails(tmp_path):
    records, events = compare_ila.expected(vectors.load("clean"), 1)
    write_csv(tmp_path / "capture.csv", records[:5] + records[6:], events)
    assert run_compare(str(tmp_path / "capture.csv"), "clean").returncode != 0


def test_bad_fcs_frames_arrive_good_and_short_frames_padded():
    vector = vectors.load("bad_fcs")
    records, _ = compare_ila.expected(vector, 1)
    assert records != vector.records   # the vector's bad-FCS frames carry records on the wire
    assert all(len(compare_ila.on_the_wire(frame.data)) >= 60 for frame in vector.frames)


def test_config_writes_cover_every_book():
    config = Config.load(vectors.vector_dir() / "config.json")
    offsets = [offset for offset, _, _ in board_config.config_writes(config)]
    assert offsets[:2] == [board_config.GROUP_IP, board_config.PORT]
    assert len(offsets) == 2 + 3 * board_config.NUM_BOOKS and len(set(offsets)) == len(offsets)
    assert board_config.write_command("devmem", 0xA0010010, 0xE9360C6F) == "devmem 0xA0010010 32 0xE9360C6F"


def write_capture(path: Path, records, frames_per: int = 5, skip_frame: int | None = None) -> None:
    from itch import record_frames
    from itch.pcap import write_pcap
    chunks = [records[i : i + frames_per] for i in range(0, len(records), frames_per)]
    frames = [record_frames.build(record_frames.Addresses(), counter + 7, 0, [r.to_bytes() for r in chunk])
              for counter, chunk in enumerate(chunks) if counter != skip_frame]
    with open(path, "wb") as f:
        write_pcap(f, [(1000 * i, frame) for i, frame in enumerate(frames)])


def run_capture(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "scripts" / "compare_capture.py"), *args],
                          capture_output=True, text=True, cwd=REPO)


def test_capture_matching_passes(tmp_path):
    records, events = compare_ila.expected(vectors.load("gap"), 1)
    write_capture(tmp_path / "records.pcap", events + records)
    result = run_capture(str(tmp_path / "records.pcap"), "gap", "--loops", "2")
    assert result.returncode == 0 and "match" in result.stdout, result.stdout + result.stderr


def test_capture_lost_frame_is_reported_and_fails(tmp_path):
    records, _ = compare_ila.expected(vectors.load("clean"), 1)
    write_capture(tmp_path / "records.pcap", records, skip_frame=3)
    result = run_capture(str(tmp_path / "records.pcap"), "clean")
    assert result.returncode != 0 and "frames lost on the PC (first: [10])" in result.stdout, result.stdout

