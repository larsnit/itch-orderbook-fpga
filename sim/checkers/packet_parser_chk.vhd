-- Simulation-only checks of packet_parser (implementation section 9.4), on its ports.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;

ENTITY packet_parser_chk IS
    PORT (
        clk         : IN STD_LOGIC;
        rst         : IN STD_LOGIC;
        s_tkeep     : IN STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid    : IN STD_LOGIC;
        s_tlast     : IN STD_LOGIC;
        m_tkeep     : IN STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid    : IN STD_LOGIC;
        m_fcs_error : IN STD_LOGIC;
        drop_pulse  : IN STD_LOGIC
    );
END ENTITY;

ARCHITECTURE sim OF packet_parser_chk IS

    SIGNAL payload_seen : BOOLEAN := FALSE;   -- the current frame already passed payload on

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) AND rst = '0' THEN
            ASSERT NOT (s_tvalid = '1' AND s_tlast = '0' AND s_tkeep /= x"FF")
                REPORT "frames start in lane 0: only the last beat of a frame may be partial" SEVERITY FAILURE;

            ASSERT NOT (m_tvalid = '1' AND s_tvalid = '0')
                REPORT "payload passed on without an input beat" SEVERITY FAILURE;

            ASSERT NOT (m_tvalid = '1' AND m_tkeep = x"00")
                REPORT "payload beat without a valid byte" SEVERITY FAILURE;

            ASSERT NOT (drop_pulse = '1' AND (payload_seen OR m_tvalid = '1' OR m_fcs_error = '1'))
                REPORT "drops are decided before the payload: a frame that passed payload on was dropped" SEVERITY FAILURE;

            IF s_tvalid = '1' AND s_tlast = '1' THEN
                payload_seen <= FALSE;
            ELSIF m_tvalid = '1' THEN
                payload_seen <= TRUE;
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
