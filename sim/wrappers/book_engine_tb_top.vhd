-- Test wrapper for book_engine: records as flat vectors (to_slv / to_<record>); base i in
-- cfg_base bits 32i+31 DOWNTO 32i.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY book_engine_tb_top IS
    PORT (
        clk        : IN  STD_LOGIC;
        rst        : IN  STD_LOGIC;
        cfg_base   : IN  STD_LOGIC_VECTOR(32*NUM_BOOKS - 1 DOWNTO 0);
        op_vector  : IN  STD_LOGIC_VECTOR(OT_RESULT_WIDTH - 1 DOWNTO 0);
        top_vector : OUT STD_LOGIC_VECTOR(TOP_WIDTH - 1 DOWNTO 0);
        top_valid  : OUT STD_LOGIC;
        busy       : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF book_engine_tb_top IS

    SIGNAL bases    : BASE_ARRAY_T;
    SIGNAL op       : OT_RESULT_T;
    SIGNAL top      : TOP_T;
    SIGNAL busy_out : STD_LOGIC;

BEGIN

    -- one process over the whole vector: Questa 2019.2 lost writes to some generate slices
    PROCESS(ALL)
    BEGIN
        FOR i IN 0 TO NUM_BOOKS - 1 LOOP
            bases(i) <= UNSIGNED(cfg_base(32*i + 31 DOWNTO 32*i));
        END LOOP;
    END PROCESS;

    op <= to_ot_result(op_vector);

    dut : ENTITY work.book_engine
        PORT MAP (clk => clk, rst => rst, cfg_base => bases, op => op, top => top, busy => busy_out);

    checker : ENTITY work.book_engine_chk
        PORT MAP (clk => clk, rst => rst, op => op, busy => busy_out, top => top);

    top_vector <= to_slv(top);
    top_valid  <= top.valid;
    busy       <= busy_out;

END ARCHITECTURE;
