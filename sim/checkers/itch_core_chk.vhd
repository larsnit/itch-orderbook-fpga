-- Simulation-only checks of itch_core (implementation section 9.4), through external names.
--
-- Latency: the book engine's top is valid 13 cycles after the beat holding the message's last
-- byte (t_arrival), so its record leaves in cycle 14. The second half of a U is one cycle later:
-- 14, right after the U_DELETE's top (record_enable 0) with the same t_arrival.
--
-- Status records: one leaves exactly EVENT_DELAY + 1 cycles after the decoder emitted it, and
-- the decoder emits an event for a message or heartbeat 2 cycles after its last byte, so the
-- status record leaves in cycle 14, with the record of the message that revealed it.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY itch_core_chk IS
    PORT (
        clk          : IN STD_LOGIC;
        rst          : IN STD_LOGIC;
        m_evt_tvalid : IN STD_LOGIC
    );
END ENTITY;

ARCHITECTURE sim OF itch_core_chk IS

    CONSTANT TOP_LATENCY     : NATURAL := 13;   -- record: + 1 in the output stage
    CONSTANT DECODER_LATENCY : NATURAL := 2;    -- last byte -> decoder output

    ALIAS top         IS << SIGNAL .itch_core_tb_top.dut.top : TOP_T >>;
    ALIAS cycle_count IS << SIGNAL .itch_core_tb_top.dut.cycle_count : UNSIGNED(15 DOWNTO 0) >>;
    ALIAS token       IS << SIGNAL .itch_core_tb_top.dut.token : TOKEN_T >>;
    ALIAS event       IS << SIGNAL .itch_core_tb_top.dut.event : EVENT_T >>;

    TYPE EMITTED_T IS ARRAY (1 TO EVENT_DELAY + 1) OF STD_LOGIC;   -- decoder events, k cycles ago

    SIGNAL previous       : TOP_T;
    SIGNAL previous_token : TOKEN_T;
    SIGNAL emitted        : EMITTED_T := (OTHERS => '0');
    SIGNAL reset_seen     : BOOLEAN := FALSE;

BEGIN

    PROCESS(clk)
        VARIABLE age : UNSIGNED(15 DOWNTO 0);
    BEGIN
        IF rising_edge(clk) THEN
            IF rst = '1' THEN
                reset_seen <= TRUE;
            END IF;
            IF rst = '0' AND reset_seen AND top.valid = '1' THEN
                age := cycle_count - top.t_arrival;
                ASSERT age = TOP_LATENCY
                       OR (age = TOP_LATENCY + 1 AND previous.valid = '1' AND previous.record_enable = '0'
                           AND previous.t_arrival = top.t_arrival)
                    REPORT "top " & INTEGER'IMAGE(to_integer(age)) & " cycles after the message's last byte, expected "
                           & INTEGER'IMAGE(TOP_LATENCY) & " (U: " & INTEGER'IMAGE(TOP_LATENCY + 1) & ")"
                    SEVERITY FAILURE;
            END IF;
            IF rst = '0' AND reset_seen THEN
                ASSERT m_evt_tvalid = emitted(EVENT_DELAY + 1)
                    REPORT "status record not " & INTEGER'IMAGE(EVENT_DELAY + 1) & " cycles after the decoder's event"
                    SEVERITY FAILURE;
                IF event.valid = '1' AND previous_token.valid = '1' AND previous_token.kind /= TOK_FCS THEN
                    age := cycle_count - previous_token.t_arrival;
                    ASSERT age = DECODER_LATENCY
                        REPORT "event " & INTEGER'IMAGE(to_integer(age)) & " cycles after the last byte, expected "
                               & INTEGER'IMAGE(DECODER_LATENCY) SEVERITY FAILURE;
                END IF;
            END IF;
            emitted(1) <= event.valid AND NOT rst;
            FOR k IN 2 TO EVENT_DELAY + 1 LOOP
                emitted(k) <= emitted(k - 1) AND NOT rst;
            END LOOP;
            previous       <= top;
            previous_token <= token;
        END IF;
    END PROCESS;

END ARCHITECTURE;
