"""Run compare_capture.py on every record capture in a results directory and print one table.

    python3.11 scripts/compare_all_captures.py                         # data/results, --loops 1
    python3.11 scripts/compare_all_captures.py data/results --loops 1
    python3.11 scripts/compare_all_captures.py --case clean min_add    # only these

A capture is <results>/records_<case>.pcap (tcpdump -w). Each case's full compare_capture.py
output goes to <results>/compare_<case>.txt. A capture without any record frame passes only
if the vector expects no records and no status records (<vectors>/<case>.json).
Exit code 0 only if every case passes.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPARE_CAPTURE = ROOT / "scripts" / "compare_capture.py"
CAPTURE_PREFIX = "records_"
NO_FRAMES_TEXT = "no record frames"
TIMEOUT_SECONDS = 600


def default_vector_dir() -> Path:
    return Path(os.environ.get("ITCH_VECTORS", ROOT / "data" / "vectors"))


def expects_no_output(vector_dir: Path, case: str) -> bool | None:
    """True if the vector expects no records and no status records; None if its .json is missing."""
    info_path = vector_dir / f"{case}.json"
    if not info_path.exists():
        return None
    info = json.loads(info_path.read_text())
    return info.get("records", 0) == 0 and info.get("events", 0) == 0


def first_line_with(text: str, needle: str) -> str:
    return next((line.strip() for line in text.splitlines() if needle in line), "")


def last_lines(text: str, count: int) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-count:]


def compare_case(capture: Path, case: str, loops: int, vector_dir: Path, log_path: Path) -> tuple[str, str]:
    """(verdict, one-line detail) for one capture."""
    command = [sys.executable, str(COMPARE_CAPTURE), str(capture), case, "--loops", str(loops),
               "--config", str(vector_dir / "config.json")]
    environment = dict(os.environ, ITCH_VECTORS=str(vector_dir))
    try:
        run = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True,
                             timeout=TIMEOUT_SECONDS)
        output = run.stdout + run.stderr
        exit_code = run.returncode
    except subprocess.TimeoutExpired:
        output, exit_code = f"timeout after {TIMEOUT_SECONDS} s", -1
    log_path.write_text(f"$ {' '.join(command)}\n{output}")

    summary = first_line_with(output, "record frames (counters")
    if exit_code == 0 and last_lines(output, 1) == ["match"]:
        return "PASS", summary
    if NO_FRAMES_TEXT in output:
        no_output_expected = expects_no_output(vector_dir, case)
        if no_output_expected:
            return "PASS", "no record frames, none expected"
        if no_output_expected is None:
            return "FAIL", f"no record frames, and no {case}.json to check against"
        return "FAIL", "no record frames, but the vector expects records"
    if "Traceback (most recent call last)" in output:
        return "ERROR", f"compare_capture.py crashed: {last_lines(output, 1)[0]}"
    detail = " | ".join(last_lines(output, 3)) or f"exit code {exit_code}, no output"
    return "FAIL", detail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("results", type=Path, nargs="?", default=ROOT / "data" / "results",
                        help="directory with records_<case>.pcap")
    parser.add_argument("--loops", type=int, default=1, help="how often tcpreplay played each vector")
    parser.add_argument("--vectors", type=Path, default=default_vector_dir(), help="vector directory")
    parser.add_argument("--case", nargs="+", help="only these cases (default: every capture found)")
    args = parser.parse_args()

    results = args.results.resolve()
    vector_dir = args.vectors.resolve()
    captures = {path.stem[len(CAPTURE_PREFIX):]: path for path in sorted(results.glob(f"{CAPTURE_PREFIX}*.pcap"))}
    cases = args.case or list(captures)
    if not cases:
        raise SystemExit(f"no {CAPTURE_PREFIX}*.pcap in {results}")

    print(f"captures: {results}   vectors: {vector_dir}   loops: {args.loops}")
    print(f"{'case':<20} {'result':<7} detail")
    failed = []
    for case in cases:
        capture = captures.get(case)
        if capture is None:
            verdict, detail = "MISSING", f"no {CAPTURE_PREFIX}{case}.pcap"
        else:
            verdict, detail = compare_case(capture, case, args.loops, vector_dir, results / f"compare_{case}.txt")
        print(f"{case:<20} {verdict:<7} {detail}")
        if verdict != "PASS":
            failed.append(case)

    without_capture = sorted(path.stem for path in vector_dir.glob("*.pcap") if path.stem not in captures)
    print(f"\n{len(cases) - len(failed)} of {len(cases)} passed; full output per case in {results}/compare_<case>.txt")
    if without_capture and not args.case:
        print(f"vectors without a capture: {', '.join(without_capture)}")
    if failed:
        print(f"not passed: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
