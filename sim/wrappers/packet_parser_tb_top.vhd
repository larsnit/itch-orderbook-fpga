-- Test wrapper for packet_parser: the parser and its checker.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;

ENTITY packet_parser_tb_top IS
    PORT (
        clk          : IN  STD_LOGIC;
        rst          : IN  STD_LOGIC;
        cfg_group_ip : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        cfg_port     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        s_tdata      : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep      : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid     : IN  STD_LOGIC;
        s_tlast      : IN  STD_LOGIC;
        s_tuser      : IN  STD_LOGIC;
        m_tdata      : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        m_tkeep      : OUT STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid     : OUT STD_LOGIC;
        m_tlast      : OUT STD_LOGIC;
        m_fcs_error  : OUT STD_LOGIC;
        drop_pulse   : OUT STD_LOGIC;
        fcs_pulse    : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF packet_parser_tb_top IS
BEGIN

    dut : ENTITY work.packet_parser
        PORT MAP (
            clk          => clk,
            rst          => rst,
            cfg_group_ip => cfg_group_ip,
            cfg_port     => cfg_port,
            s_tdata      => s_tdata,
            s_tkeep      => s_tkeep,
            s_tvalid     => s_tvalid,
            s_tlast      => s_tlast,
            s_tuser      => s_tuser,
            m_tdata      => m_tdata,
            m_tkeep      => m_tkeep,
            m_tvalid     => m_tvalid,
            m_tlast      => m_tlast,
            m_fcs_error  => m_fcs_error,
            drop_pulse   => drop_pulse,
            fcs_pulse    => fcs_pulse
        );

    checker : ENTITY work.packet_parser_chk
        PORT MAP (
            clk         => clk,
            rst         => rst,
            s_tkeep     => s_tkeep,
            s_tvalid    => s_tvalid,
            s_tlast     => s_tlast,
            m_tkeep     => m_tkeep,
            m_tvalid    => m_tvalid,
            m_fcs_error => m_fcs_error,
            drop_pulse  => drop_pulse
        );

END ARCHITECTURE;
