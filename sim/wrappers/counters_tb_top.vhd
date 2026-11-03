-- Test wrapper for counters: the counter array as one flat vector, counter i in bits
-- 32i+31 DOWNTO 32i.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY counters_tb_top IS
    PORT (
        clk               : IN  STD_LOGIC;
        rst               : IN  STD_LOGIC;
        pulses            : IN  STD_LOGIC_VECTOR(NUM_COUNTERS - 1 DOWNTO 0);
        framer_drop_pulse : IN  STD_LOGIC;
        busy_drop_pulse   : IN  STD_LOGIC;
        counts            : OUT STD_LOGIC_VECTOR(32*NUM_COUNTERS - 1 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF counters_tb_top IS

    SIGNAL count_array : COUNTER_ARRAY_T;

BEGIN

    dut : ENTITY work.counters
        PORT MAP (
            clk               => clk,
            rst               => rst,
            pulses            => pulses,
            framer_drop_pulse => framer_drop_pulse,
            busy_drop_pulse   => busy_drop_pulse,
            counts            => count_array
        );

    flatten : FOR counter IN 0 TO NUM_COUNTERS - 1 GENERATE
        counts(32*counter + 31 DOWNTO 32*counter) <= STD_LOGIC_VECTOR(count_array(counter));
    END GENERATE;

END ARCHITECTURE;
