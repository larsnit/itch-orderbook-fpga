-- Test wrapper for framer: the framer and its checker, the token as a flat vector.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY framer_tb_top IS
    PORT (
        clk             : IN  STD_LOGIC;
        rst             : IN  STD_LOGIC;
        s_tdata         : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep         : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid        : IN  STD_LOGIC;
        s_tlast         : IN  STD_LOGIC;
        s_fcs_error     : IN  STD_LOGIC;
        cycle_count     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        token_vector    : OUT STD_LOGIC_VECTOR(TOKEN_WIDTH - 1 DOWNTO 0);
        token_valid     : OUT STD_LOGIC;
        drop_pulse      : OUT STD_LOGIC;
        malformed_pulse : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF framer_tb_top IS

    SIGNAL token : TOKEN_T;

BEGIN

    dut : ENTITY work.framer
        PORT MAP (
            clk             => clk,
            rst             => rst,
            s_tdata         => s_tdata,
            s_tkeep         => s_tkeep,
            s_tvalid        => s_tvalid,
            s_tlast         => s_tlast,
            s_fcs_error     => s_fcs_error,
            cycle_count     => UNSIGNED(cycle_count),
            token           => token,
            drop_pulse      => drop_pulse,
            malformed_pulse => malformed_pulse
        );

    checker : ENTITY work.framer_chk
        PORT MAP (
            clk   => clk,
            rst   => rst,
            token => token
        );

    token_vector <= to_slv(token);
    token_valid  <= token.valid;

END ARCHITECTURE;
