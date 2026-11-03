-- Simulation-only checks of order_table (implementation section 9.4), on its ports and, through
-- external names, its update stage.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY order_table_chk IS
    PORT (
        clk    : IN STD_LOGIC;
        rst    : IN STD_LOGIC;
        op     : IN OP_T;
        busy   : IN STD_LOGIC;
        result : IN OT_RESULT_T
    );
END ENTITY;

ARCHITECTURE sim OF order_table_chk IS

    ALIAS op_match   IS << SIGNAL .order_table_tb_top.dut.op_match   : OP_T >>;
    ALIAS hits0      IS << SIGNAL .order_table_tb_top.dut.hits0      : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0) >>;
    ALIAS hits1      IS << SIGNAL .order_table_tb_top.dut.hits1      : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0) >>;
    ALIAS stash_hits IS << SIGNAL .order_table_tb_top.dut.stash_hits : STD_LOGIC_VECTOR(STASH - 1 DOWNTO 0) >>;

    TYPE VALID_DELAY_T IS ARRAY (1 TO RAM_LATENCY + 2) OF STD_LOGIC;
    SIGNAL accepted   : VALID_DELAY_T := (OTHERS => '0');   -- operations taken in, k cycles ago
    SIGNAL reset_seen : BOOLEAN := FALSE;                   -- nothing is defined before the first reset
    SIGNAL previous_valid : STD_LOGIC := '0';   -- the operation of the cycle before
    SIGNAL previous_kind  : OP_KIND_T;

    FUNCTION ones(bits : STD_LOGIC_VECTOR) RETURN NATURAL IS
        VARIABLE count : NATURAL := 0;
    BEGIN
        FOR i IN bits'RANGE LOOP
            IF bits(i) = '1' THEN
                count := count + 1;
            END IF;
        END LOOP;
        RETURN count;
    END FUNCTION;

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            IF rst = '1' THEN
                reset_seen <= TRUE;
            END IF;
            IF rst = '0' AND reset_seen THEN
                ASSERT NOT (op_match.valid = '1' AND ones(hits0) + ones(hits1) + ones(stash_hits) > 1)
                    REPORT "order ref stored more than once" SEVERITY FAILURE;

                ASSERT result.valid = accepted(RAM_LATENCY + 2)
                    REPORT "a result without its operation 4 cycles before, or the other way round" SEVERITY FAILURE;

                -- book-message spacing: the match stage relies on it
                ASSERT NOT (op.valid = '1' AND previous_valid = '1'
                            AND NOT (previous_kind = OP_U_DELETE AND op.kind = OP_U_INSERT))
                    REPORT "two operations in consecutive cycles that are not the halves of a U" SEVERITY FAILURE;

                ASSERT NOT (result.valid = '1' AND result.change = CHG_NONE AND result.shares /= 0)
                    REPORT "no change, but shares" SEVERITY FAILURE;
            END IF;

            accepted(1) <= op.valid AND NOT busy AND NOT rst;
            FOR k IN 2 TO RAM_LATENCY + 2 LOOP
                accepted(k) <= accepted(k - 1) AND NOT rst;
            END LOOP;
            previous_valid <= op.valid AND NOT busy AND NOT rst;
            previous_kind  <= op.kind;
        END IF;
    END PROCESS;

END ARCHITECTURE;
