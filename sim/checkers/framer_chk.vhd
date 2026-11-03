-- Simulation-only checks of framer (implementation section 9.4), on its ports.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY framer_chk IS
    PORT (
        clk   : IN STD_LOGIC;
        rst   : IN STD_LOGIC;
        token : IN TOKEN_T
    );
END ENTITY;

ARCHITECTURE sim OF framer_chk IS

    SIGNAL message_token_before : BOOLEAN := FALSE;   -- the previous cycle had a message token

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) AND rst = '0' THEN
            IF token.valid = '1' AND token.kind = TOK_MSG THEN
                ASSERT token.len >= 12
                    REPORT "message token shorter than the smallest ITCH message" SEVERITY FAILURE;
            END IF;

            IF token.valid = '1' AND token.kind /= TOK_MSG THEN
                ASSERT token.len = 0 AND token.data = (token.data'RANGE => '0')
                    REPORT "heartbeat or FCS token with message bytes" SEVERITY FAILURE;
            END IF;

            ASSERT NOT (token.valid = '1' AND token.kind = TOK_FCS AND message_token_before)
                REPORT "FCS token less than 2 cycles after a message token" SEVERITY FAILURE;

            message_token_before <= token.valid = '1' AND token.kind = TOK_MSG;
        END IF;
    END PROCESS;

END ARCHITECTURE;
