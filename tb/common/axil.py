"""AXI4-Lite master for the register block, with random gaps between the channels.

Timing convention as everywhere: inputs are driven after the falling edge, outputs are sampled
right after the rising edge. A handshake happens at the rising edge at which valid and ready
are both high. The master also checks the slave's side of the protocol: once bvalid or rvalid
is high it stays high, with the same response and data, until the handshake.
"""

import random

from cocotb.triggers import FallingEdge, RisingEdge

from tb.common.axis import is_high

OKAY = 0
SLVERR = 2


class AxiLiteMaster:
    def __init__(self, dut, prefix: str = "s_axi_", seed: int = 0, max_delay: int = 3) -> None:
        self.clk = dut.clk
        self.signals = {name: getattr(dut, prefix + name) for name in (
            "awaddr", "awvalid", "awready", "wdata", "wstrb", "wvalid", "wready",
            "bresp", "bvalid", "bready", "araddr", "arvalid", "arready", "rdata", "rresp", "rvalid", "rready")}
        self.rng = random.Random(seed)
        self.max_delay = max_delay
        for name in ("awvalid", "wvalid", "bready", "arvalid", "rready"):
            self.signals[name].value = 0
        self.signals["awaddr"].value = 0
        self.signals["araddr"].value = 0
        self.signals["wdata"].value = 0
        self.signals["wstrb"].value = 0

    def _delay(self) -> int:
        return self.rng.choice([0, 0, self.rng.randrange(self.max_delay + 1)])

    async def write(self, address: int, data: int, strobe: int = 0xF) -> int:
        """One write; returns BRESP. Address and data come in random order, with random gaps."""
        s = self.signals
        address_at, data_at, ready_at = self._delay(), self._delay(), self._delay()
        address_done = data_done = False
        cycle = 0
        while not (address_done and data_done):
            await FallingEdge(self.clk)
            s["awvalid"].value = int(not address_done and cycle >= address_at)
            s["awaddr"].value = address
            s["wvalid"].value = int(not data_done and cycle >= data_at)
            s["wdata"].value = data
            s["wstrb"].value = strobe
            await RisingEdge(self.clk)
            address_done |= is_high(s["awvalid"]) and is_high(s["awready"])
            data_done |= is_high(s["wvalid"]) and is_high(s["wready"])
            cycle += 1
        await FallingEdge(self.clk)
        s["awvalid"].value = 0
        s["wvalid"].value = 0
        return await self._response(s["bvalid"], s["bready"], lambda: s["bresp"].value.to_unsigned(), ready_at)

    async def read(self, address: int) -> tuple[int, int]:
        """One read; returns (RDATA, RRESP)."""
        s = self.signals
        address_at, ready_at = self._delay(), self._delay()
        cycle = 0
        while True:
            await FallingEdge(self.clk)
            s["arvalid"].value = int(cycle >= address_at)
            s["araddr"].value = address
            await RisingEdge(self.clk)
            cycle += 1
            if is_high(s["arvalid"]) and is_high(s["arready"]):
                break
        await FallingEdge(self.clk)
        s["arvalid"].value = 0
        return await self._response(s["rvalid"], s["rready"],
                                    lambda: (s["rdata"].value.to_unsigned(), s["rresp"].value.to_unsigned()), ready_at)

    async def _response(self, valid, ready, value, ready_at: int):
        """Wait for valid, raise ready after ready_at cycles; the response must not change
        while it waits."""
        seen = None
        cycle = 0
        while True:
            ready.value = int(cycle >= ready_at)
            await RisingEdge(self.clk)
            if is_high(valid):
                now = value()
                assert seen is None or now == seen, f"response changed while waiting: {seen} -> {now}"
                seen = now
                if is_high(ready):
                    await FallingEdge(self.clk)
                    ready.value = 0
                    return now
            else:
                assert seen is None, "valid dropped before the handshake"
            await FallingEdge(self.clk)
            cycle += 1
