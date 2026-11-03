"""tb/common/axis.py: frames driven with tvalid gaps and bad FCS come back out of a register
byte for byte.

    python3.11 tb/axis/test_axis.py             (Questa)
    SIM=nvc python3.11 tb/axis/test_axis.py
"""

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, when run directly

import cocotb  # noqa: E402
from cocotb.clock import Clock  # noqa: E402
from cocotb.triggers import ClockCycles  # noqa: E402

from tb.common.axis import AxisDriver, AxisMonitor, StreamMonitor  # noqa: E402
from tb.common.runner import WRAPPERS, run  # noqa: E402

CLOCK_PS = 6400


@cocotb.test()
async def frames_survive_driver_and_monitor(dut):
    cocotb.start_soon(Clock(dut.clk, CLOCK_PS, "ps").start())
    rng = random.Random(1)
    driver = AxisDriver(dut, idle_probability=0.2, seed=2)
    monitor = AxisMonitor(dut)
    beats = StreamMonitor(dut.clk, dut.m_tdata, dut.m_tvalid)
    cocotb.start_soon(monitor.run())
    cocotb.start_soon(beats.run())

    sent = []
    for length in [60, 61, 62, 63, 64, 65, 66, 67, 1514] + [rng.randrange(60, 300) for _ in range(30)]:
        frame = rng.randbytes(length)
        fcs_error = rng.random() < 0.3
        sent.append((frame, fcs_error))
        await driver.send(frame, fcs_error)
        await driver.idle(rng.choice([0, 0, 1, 2]))  # 0: the next frame follows back to back
    await driver.idle(1)
    await ClockCycles(dut.clk, 5)

    assert monitor.frames == sent
    total_beats = sum((len(frame) + 7) // 8 for frame, _ in sent)
    assert len(beats.items) == total_beats


def test_axis():
    run("axis_loopback_tb_top", [WRAPPERS / "axis_loopback_tb_top.vhd"], __file__)


if __name__ == "__main__":
    test_axis()
