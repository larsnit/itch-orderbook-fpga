-- Test wrapper for order_table: records as flat vectors (to_slv / to_<record>).

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY order_table_tb_top IS
    PORT (
        clk                  : IN  STD_LOGIC;
        rst                  : IN  STD_LOGIC;
        op_vector            : IN  STD_LOGIC_VECTOR(OP_WIDTH - 1 DOWNTO 0);
        price_info_vector    : IN  STD_LOGIC_VECTOR(PRICE_INFO_WIDTH - 1 DOWNTO 0);
        result_vector        : OUT STD_LOGIC_VECTOR(OT_RESULT_WIDTH - 1 DOWNTO 0);
        result_valid         : OUT STD_LOGIC;
        insert_failure_pulse : OUT STD_LOGIC;
        ref_error_pulse      : OUT STD_LOGIC;
        unknown_ref_pulse    : OUT STD_LOGIC;
        inconsistent_pulse   : OUT STD_LOGIC;
        stash_insert_pulse   : OUT STD_LOGIC;
        off_ladder_pulse     : OUT STD_LOGIC;
        unrep_pulse          : OUT STD_LOGIC;
        book_stale           : OUT STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
        busy                 : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF order_table_tb_top IS

    SIGNAL op         : OP_T;
    SIGNAL price_info : PRICE_INFO_T;
    SIGNAL result     : OT_RESULT_T;
    SIGNAL busy_out   : STD_LOGIC;

BEGIN

    op         <= to_op(op_vector);
    price_info <= to_price_info(price_info_vector);

    dut : ENTITY work.order_table
        PORT MAP (
            clk                  => clk,
            rst                  => rst,
            op                   => op,
            price_info           => price_info,
            result               => result,
            insert_failure_pulse => insert_failure_pulse,
            ref_error_pulse      => ref_error_pulse,
            unknown_ref_pulse    => unknown_ref_pulse,
            inconsistent_pulse   => inconsistent_pulse,
            stash_insert_pulse   => stash_insert_pulse,
            off_ladder_pulse     => off_ladder_pulse,
            unrep_pulse          => unrep_pulse,
            book_stale           => book_stale,
            busy                 => busy_out
        );

    checker : ENTITY work.order_table_chk
        PORT MAP (clk => clk, rst => rst, op => op, busy => busy_out, result => result);

    result_vector <= to_slv(result);
    result_valid  <= result.valid;
    busy          <= busy_out;

END ARCHITECTURE;
