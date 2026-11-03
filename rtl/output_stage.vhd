-- Output stage (architecture section 9.6), 1 cycle: a book record when the top or flags of
-- a book changed, and every status record on its own channel.
--
-- Per book a register holds the last emitted (bid, ask, flags 1..0). An operation with
-- record_enable emits a record if its top differs from that register, and updates it.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY output_stage IS
    PORT (
        clk           : IN  STD_LOGIC;
        rst           : IN  STD_LOGIC;
        top           : IN  TOP_T;
        event_in      : IN  EVENT_T;                          -- already delayed
        m_rec_tdata   : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_rec_tvalid  : OUT STD_LOGIC;
        m_evt_tdata   : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_evt_tvalid  : OUT STD_LOGIC;
        records_pulse : OUT STD_LOGIC                         -- a book record left
    );
END ENTITY;

ARCHITECTURE rtl OF output_stage IS

    TYPE EMITTED_T IS RECORD
        bid_price  : UNSIGNED(31 DOWNTO 0);
        bid_shares : UNSIGNED(31 DOWNTO 0);
        ask_price  : UNSIGNED(31 DOWNTO 0);
        ask_shares : UNSIGNED(31 DOWNTO 0);
        flags      : STD_LOGIC_VECTOR(1 DOWNTO 0);   -- stale, outside window
    END RECORD;
    TYPE EMITTED_ARRAY_T IS ARRAY (0 TO NUM_BOOKS - 1) OF EMITTED_T;

    CONSTANT NOTHING_EMITTED : EMITTED_T := (
        bid_price  => NO_BID_PRICE,
        bid_shares => (OTHERS => '0'),
        ask_price  => NO_ASK_PRICE,
        ask_shares => (OTHERS => '0'),
        flags      => "00"
    );

    SIGNAL emitted : EMITTED_ARRAY_T;

BEGIN

    PROCESS(clk)
        VARIABLE now : EMITTED_T;
    BEGIN
        IF rising_edge(clk) THEN
            now := (
                bid_price  => top.bid_price,
                bid_shares => top.bid_shares,
                ask_price  => top.ask_price,
                ask_shares => top.ask_shares,
                flags      => top.flags(1 DOWNTO 0)
            );
            m_rec_tvalid  <= '0';
            records_pulse <= '0';
            m_rec_tdata   <= pack_record(top);
            IF top.valid = '1' AND top.record_enable = '1' AND now /= emitted(to_integer(top.book)) THEN
                emitted(to_integer(top.book)) <= now;
                m_rec_tvalid  <= '1';
                records_pulse <= '1';
            END IF;

            m_evt_tdata  <= pack_record(event_in);
            m_evt_tvalid <= event_in.valid;

            IF rst = '1' THEN
                emitted       <= (OTHERS => NOTHING_EMITTED);
                m_rec_tvalid  <= '0';
                records_pulse <= '0';
                m_evt_tvalid  <= '0';
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
