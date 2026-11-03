# Core clock: 156.25 MHz (the 10G MAC's user clock), 6.4 ns.
create_clock -period 6.400 -name clk [get_ports clk]
# Transmit clock of the record output: 156.25 MHz from the Ethernet subsystem (tx_mii_clk_0),
# not related to clk on the board. Only for the out-of-context run: in the block design the
# Ethernet subsystem creates both clocks.
create_clock -period 6.400 -name tx_clk [get_ports tx_clk]
