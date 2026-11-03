-- ITCH core (architecture section 4): MAC stream in, book records and status records out.
--
--   MAC -> busy gate -> packet_parser -> framer -> decoder -> order_table -> book_engine -> output_stage
--                                                  price_unit /           event_delay ----/
--
-- Configuration comes in as ports; the register block (step 8) sits on top of this.
-- After reset, the order table and the book engine clear their memories (busy). A frame whose
-- first beat arrives while busy is dropped whole, before the parser, and counted once.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY itch_core IS
    GENERIC (
        TABLE_STYLE  : STRING := "ultra";   -- order table memories
        LADDER_STYLE : STRING := "block"    -- ladder memories
    );
    PORT (
        clk          : IN  STD_LOGIC;
        rst          : IN  STD_LOGIC;
        cfg_group_ip : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        cfg_port     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        cfg_ticker   : IN  TICKER_ARRAY_T;
        cfg_base     : IN  BASE_ARRAY_T;
        -- from the MAC: whole frames, first byte in s_tdata(7 DOWNTO 0), s_tuser = bad FCS at s_tlast
        s_tdata      : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep      : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid     : IN  STD_LOGIC;
        s_tlast      : IN  STD_LOGIC;
        s_tuser      : IN  STD_LOGIC;
        -- book records, at most one per cycle; status records on their own channel
        m_rec_tdata  : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_rec_tvalid : OUT STD_LOGIC;
        m_evt_tdata  : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_evt_tvalid : OUT STD_LOGIC;
        -- for the register block
        counts       : OUT COUNTER_ARRAY_T;
        book_stale   : OUT STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
        stale_all    : OUT STD_LOGIC;
        busy         : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF itch_core IS

    SIGNAL cycle_count : UNSIGNED(15 DOWNTO 0);   -- free-running, for t_arrival

    -- busy gate
    SIGNAL clearing        : STD_LOGIC;           -- order table or book engine sweeping
    SIGNAL table_busy      : STD_LOGIC;
    SIGNAL book_busy       : STD_LOGIC;
    SIGNAL in_frame        : STD_LOGIC;           -- a frame's first beat has passed, its last not yet
    SIGNAL dropping        : STD_LOGIC;           -- the frame in progress started while busy
    SIGNAL first_beat      : STD_LOGIC;
    SIGNAL drop_beat       : STD_LOGIC;
    SIGNAL busy_drop_pulse : STD_LOGIC;
    SIGNAL parser_tvalid   : STD_LOGIC;

    -- stage outputs
    SIGNAL payload_tdata  : STD_LOGIC_VECTOR(63 DOWNTO 0);
    SIGNAL payload_tkeep  : STD_LOGIC_VECTOR(7 DOWNTO 0);
    SIGNAL payload_tvalid : STD_LOGIC;
    SIGNAL payload_tlast  : STD_LOGIC;
    SIGNAL payload_fcs    : STD_LOGIC;
    SIGNAL token          : TOKEN_T;
    SIGNAL op             : OP_T;
    SIGNAL event          : EVENT_T;
    SIGNAL delayed_event  : EVENT_T;
    SIGNAL price_info     : PRICE_INFO_T;
    SIGNAL ot_result      : OT_RESULT_T;
    SIGNAL top            : TOP_T;

    -- counter strobes
    SIGNAL pulses            : STD_LOGIC_VECTOR(NUM_COUNTERS - 1 DOWNTO 0);
    SIGNAL framer_drop_pulse : STD_LOGIC;

BEGIN

    -- busy gate: frames are dropped whole, decided at their first beat
    clearing        <= table_busy OR book_busy;
    first_beat      <= s_tvalid AND NOT in_frame;
    drop_beat       <= (first_beat AND clearing) OR (in_frame AND dropping);
    busy_drop_pulse <= first_beat AND clearing;
    parser_tvalid   <= s_tvalid AND NOT drop_beat;

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            cycle_count <= cycle_count + 1;
            IF s_tvalid = '1' THEN
                in_frame <= NOT s_tlast;
                IF first_beat = '1' THEN
                    dropping <= clearing;
                END IF;
            END IF;
            IF rst = '1' THEN
                cycle_count <= (OTHERS => '0');
                in_frame    <= '0';
                dropping    <= '0';
            END IF;
        END IF;
    END PROCESS;

    parser : ENTITY work.packet_parser
        PORT MAP (
            clk          => clk,
            rst          => rst,
            cfg_group_ip => cfg_group_ip,
            cfg_port     => cfg_port,
            s_tdata      => s_tdata,
            s_tkeep      => s_tkeep,
            s_tvalid     => parser_tvalid,
            s_tlast      => s_tlast,
            s_tuser      => s_tuser,
            m_tdata      => payload_tdata,
            m_tkeep      => payload_tkeep,
            m_tvalid     => payload_tvalid,
            m_tlast      => payload_tlast,
            m_fcs_error  => payload_fcs,
            drop_pulse   => pulses(CNT_DROPPED_FRAMES),
            fcs_pulse    => pulses(CNT_FCS_ERRORS)
        );

    framing : ENTITY work.framer
        PORT MAP (
            clk             => clk,
            rst             => rst,
            s_tdata         => payload_tdata,
            s_tkeep         => payload_tkeep,
            s_tvalid        => payload_tvalid,
            s_tlast         => payload_tlast,
            s_fcs_error     => payload_fcs,
            cycle_count     => cycle_count,
            token           => token,
            drop_pulse      => framer_drop_pulse,
            malformed_pulse => pulses(CNT_MALFORMED_PACKETS)
        );

    decoding : ENTITY work.decoder
        PORT MAP (
            clk             => clk,
            rst             => rst,
            cfg_ticker      => cfg_ticker,
            token           => token,
            op              => op,
            event_out       => event,
            duplicate_pulse => pulses(CNT_DUPLICATES),
            gap_pulse       => pulses(CNT_GAPS),
            unusable_pulse  => pulses(CNT_UNUSABLE_MESSAGES),
            messages_pulse  => pulses(CNT_MESSAGES),
            events_pulse    => pulses(CNT_EVENTS),
            all_books_stale => stale_all
        );

    prices : ENTITY work.price_unit
        PORT MAP (
            clk        => clk,
            cfg_base   => cfg_base,
            token      => token,
            op         => op,
            price_info => price_info
        );

    orders : ENTITY work.order_table
        GENERIC MAP (TABLE_STYLE => TABLE_STYLE)
        PORT MAP (
            clk                  => clk,
            rst                  => rst,
            op                   => op,
            price_info           => price_info,
            result               => ot_result,
            insert_failure_pulse => pulses(CNT_INSERT_FAILURES),
            ref_error_pulse      => pulses(CNT_REF_ERRORS),
            unknown_ref_pulse    => pulses(CNT_UNKNOWN_REFS),
            inconsistent_pulse   => pulses(CNT_INCONSISTENT),
            stash_insert_pulse   => pulses(CNT_STASH_INSERTS),
            off_ladder_pulse     => pulses(CNT_OFF_LADDER_ORDERS),
            unrep_pulse          => pulses(CNT_UNREP_PRICES),
            book_stale           => book_stale,
            busy                 => table_busy
        );

    books : ENTITY work.book_engine
        GENERIC MAP (LADDER_STYLE => LADDER_STYLE)
        PORT MAP (
            clk      => clk,
            rst      => rst,
            cfg_base => cfg_base,
            op       => ot_result,
            top      => top,
            busy     => book_busy
        );

    events : ENTITY work.event_delay
        PORT MAP (clk => clk, rst => rst, event_in => event, event_out => delayed_event);

    output : ENTITY work.output_stage
        PORT MAP (
            clk           => clk,
            rst           => rst,
            top           => top,
            event_in      => delayed_event,
            m_rec_tdata   => m_rec_tdata,
            m_rec_tvalid  => m_rec_tvalid,
            m_evt_tdata   => m_evt_tdata,
            m_evt_tvalid  => m_evt_tvalid,
            records_pulse => pulses(CNT_RECORDS)
        );

    counting : ENTITY work.counters
        PORT MAP (
            clk               => clk,
            rst               => rst,
            pulses            => pulses,
            framer_drop_pulse => framer_drop_pulse,
            busy_drop_pulse   => busy_drop_pulse,
            counts            => counts
        );

    busy <= clearing;

END ARCHITECTURE;
