-- Simulation-only checks of decoder (implementation section 9.4), on its ports.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE work.itch_pkg.ALL;

ENTITY decoder_chk IS
    PORT (
        clk : IN STD_LOGIC;
        rst : IN STD_LOGIC;
        op  : IN OP_T
    );
END ENTITY;

ARCHITECTURE sim OF decoder_chk IS

    SIGNAL previous_kind  : OP_KIND_T;
    SIGNAL previous_valid : STD_LOGIC := '0';

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) AND rst = '0' THEN
            -- book-message spacing: operations of different messages are at least 2 cycles
            -- apart; only the two halves of a U come in consecutive cycles
            ASSERT NOT (op.valid = '1' AND previous_valid = '1'
                        AND NOT (previous_kind = OP_U_DELETE AND op.kind = OP_U_INSERT))
                REPORT "two operations in consecutive cycles that are not the halves of a U" SEVERITY FAILURE;

            ASSERT NOT (op.valid = '1' AND op.kind = OP_U_DELETE AND op.record_enable = '1')
                REPORT "U_DELETE must not produce a record" SEVERITY FAILURE;

            previous_valid <= op.valid;
            previous_kind  <= op.kind;
        END IF;
    END PROCESS;

END ARCHITECTURE;
