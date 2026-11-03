-- Test wrapper for itch_core: arrays as flat vectors. Ticker i in cfg_ticker bits
-- 64i+63 DOWNTO 64i, base i in cfg_base bits 32i+31 DOWNTO 32i, counter i in counts bits
-- 32i+31 DOWNTO 32i.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY itch_core_tb_top IS
    PORT (
        clk          : IN  STD_LOGIC;
        rst          : IN  STD_LOGIC;
        cfg_group_ip : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        cfg_port     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        cfg_ticker   : IN  STD_LOGIC_VECTOR(64*NUM_BOOKS - 1 DOWNTO 0);
        cfg_base     : IN  STD_LOGIC_VECTOR(32*NUM_BOOKS - 1 DOWNTO 0);
        s_tdata      : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep      : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid     : IN  STD_LOGIC;
        s_tlast      : IN  STD_LOGIC;
        s_tuser      : IN  STD_LOGIC;
        m_rec_tdata  : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_rec_tvalid : OUT STD_LOGIC;
        m_evt_tdata  : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_evt_tvalid : OUT STD_LOGIC;
        counts       : OUT STD_LOGIC_VECTOR(32*NUM_COUNTERS - 1 DOWNTO 0);
        book_stale   : OUT STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
        stale_all    : OUT STD_LOGIC;
        busy         : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF itch_core_tb_top IS

    SIGNAL tickers      : TICKER_ARRAY_T;
    SIGNAL bases        : BASE_ARRAY_T;
    SIGNAL counter_list : COUNTER_ARRAY_T;

BEGIN

    -- one process over the whole vectors: Questa 2019.2 lost writes to some generate slices
    PROCESS(ALL)
    BEGIN
        FOR i IN 0 TO NUM_BOOKS - 1 LOOP
            tickers(i) <= cfg_ticker(64*i + 63 DOWNTO 64*i);
            bases(i)   <= UNSIGNED(cfg_base(32*i + 31 DOWNTO 32*i));
        END LOOP;
    END PROCESS;

    PROCESS(ALL)
    BEGIN
        FOR i IN 0 TO NUM_COUNTERS - 1 LOOP
            counts(32*i + 31 DOWNTO 32*i) <= STD_LOGIC_VECTOR(counter_list(i));
        END LOOP;
    END PROCESS;

    dut : ENTITY work.itch_core
        PORT MAP (
            clk          => clk,
            rst          => rst,
            cfg_group_ip => cfg_group_ip,
            cfg_port     => cfg_port,
            cfg_ticker   => tickers,
            cfg_base     => bases,
            s_tdata      => s_tdata,
            s_tkeep      => s_tkeep,
            s_tvalid     => s_tvalid,
            s_tlast      => s_tlast,
            s_tuser      => s_tuser,
            m_rec_tdata  => m_rec_tdata,
            m_rec_tvalid => m_rec_tvalid,
            m_evt_tdata  => m_evt_tdata,
            m_evt_tvalid => m_evt_tvalid,
            counts       => counter_list,
            book_stale   => book_stale,
            stale_all    => stale_all,
            busy         => busy
        );

    checker : ENTITY work.itch_core_chk
        PORT MAP (clk => clk, rst => rst, m_evt_tvalid => m_evt_tvalid);

END ARCHITECTURE;
