"""Build and run one cocotb testbench from a pytest function.

    SIM=questa python3.11 -m pytest tb/          (default)
    SIM=nvc    python3.11 -m pytest tb/

Each testbench directory has one pytest function that calls run(); the cocotb tests live in
the same file. The simulation runs in tb/<module>/sim_build/<build name>.
"""

import os
from pathlib import Path

from cocotb_tools.runner import VHDL, get_results, get_runner

REPO = Path(__file__).resolve().parents[2]
RTL = REPO / "rtl"
WRAPPERS = REPO / "sim" / "wrappers"
CHECKERS = REPO / "sim" / "checkers"

VHDL_2008 = {
    "questa": [VHDL("-2008")],
    "nvc": ["--std=2008"],
}


def run(
    toplevel: str,
    sources: list[Path],
    test_file: str,
    parameters: dict[str, object] | None = None,
    build_name: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Compile sources (in order), elaborate toplevel with the generics in parameters, and
    run the cocotb tests in test_file. Fails if any cocotb test fails."""
    simulator = os.environ.get("SIM", "questa")
    test_dir = Path(test_file).resolve().parent
    build_dir = test_dir / "sim_build" / (build_name or toplevel)
    python_path = os.pathsep.join(filter(None, [str(REPO), str(test_dir), os.environ.get("PYTHONPATH")]))

    runner = get_runner(simulator)
    runner.build(
        sources=sources,
        hdl_toplevel=toplevel,
        build_args=VHDL_2008[simulator],
        build_dir=build_dir,
        always=True,
    )
    results = runner.test(
        hdl_toplevel=toplevel,
        test_module=Path(test_file).stem,
        build_dir=build_dir,
        test_dir=build_dir,
        parameters=parameters or {},
        extra_env={"PYTHONPATH": python_path, **(env or {})},
    )
    tests, failed = get_results(results)
    assert tests > 0, f"no cocotb tests ran for {toplevel}"
    assert failed == 0, f"{failed} of {tests} cocotb tests failed for {toplevel}"
