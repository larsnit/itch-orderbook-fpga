-- Test wrapper for decoder and price_unit, connected as in itch_core: the price unit reads
-- the decoder's input token and output operation. Records and arrays as flat vectors:
-- ticker i in cfg_ticker bits 64i+63 DOWNTO 64i, base i in cfg_base bits 32i+31 DOWNTO 32i.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY decoder_tb_top IS
    PORT (
        clk               : IN  STD_LOGIC;
        rst               : IN  STD_LOGIC;
        cfg_ticker        : IN  STD_LOGIC_VECTOR(64*NUM_BOOKS - 1 DOWNTO 0);
        cfg_base          : IN  STD_LOGIC_VECTOR(32*NUM_BOOKS - 1 DOWNTO 0);
        token_vector      : IN  STD_LOGIC_VECTOR(TOKEN_WIDTH - 1 DOWNTO 0);
        op_vector         : OUT STD_LOGIC_VECTOR(OP_WIDTH - 1 DOWNTO 0);
        op_valid          : OUT STD_LOGIC;
        event_vector      : OUT STD_LOGIC_VECTOR(EVENT_WIDTH - 1 DOWNTO 0);
        event_valid       : OUT STD_LOGIC;
        price_info_vector : OUT STD_LOGIC_VECTOR(PRICE_INFO_WIDTH - 1 DOWNTO 0);
        duplicate_pulse   : OUT STD_LOGIC;
        gap_pulse         : OUT STD_LOGIC;
        unusable_pulse    : OUT STD_LOGIC;
        messages_pulse    : OUT STD_LOGIC;
        events_pulse      : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF decoder_tb_top IS

    SIGNAL tickers    : TICKER_ARRAY_T;
    SIGNAL bases      : BASE_ARRAY_T;
    SIGNAL token      : TOKEN_T;
    SIGNAL op         : OP_T;
    SIGNAL event      : EVENT_T;
    SIGNAL price_info : PRICE_INFO_T;

BEGIN

    -- one process over the whole vectors: Questa 2019.2 lost writes to some generate slices
    PROCESS(ALL)
    BEGIN
        FOR i IN 0 TO NUM_BOOKS - 1 LOOP
            tickers(i) <= cfg_ticker(64*i + 63 DOWNTO 64*i);
            bases(i)   <= UNSIGNED(cfg_base(32*i + 31 DOWNTO 32*i));
        END LOOP;
    END PROCESS;

    token <= to_token(token_vector);

    dut : ENTITY work.decoder
        PORT MAP (
            clk             => clk,
            rst             => rst,
            cfg_ticker      => tickers,
            token           => token,
            op              => op,
            event_out       => event,
            duplicate_pulse => duplicate_pulse,
            gap_pulse       => gap_pulse,
            unusable_pulse  => unusable_pulse,
            messages_pulse  => messages_pulse,
            events_pulse    => events_pulse
        );

    prices : ENTITY work.price_unit
        PORT MAP (
            clk        => clk,
            cfg_base   => bases,
            token      => token,
            op         => op,
            price_info => price_info
        );

    checker : ENTITY work.decoder_chk
        PORT MAP (clk => clk, rst => rst, op => op);

    op_vector         <= to_slv(op);
    op_valid          <= op.valid;
    event_vector      <= to_slv(event);
    event_valid       <= event.valid;
    price_info_vector <= to_slv(price_info);

END ARCHITECTURE;
