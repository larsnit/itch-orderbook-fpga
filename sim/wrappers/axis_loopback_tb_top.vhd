-- Test wrapper for tb/common/axis.py: the stream goes through one register, so the driver
-- and the monitor can be checked against each other.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;

ENTITY axis_loopback_tb_top IS
    PORT (
        clk      : IN  STD_LOGIC;
        s_tdata  : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep  : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid : IN  STD_LOGIC;
        s_tlast  : IN  STD_LOGIC;
        s_tuser  : IN  STD_LOGIC;
        m_tdata  : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        m_tkeep  : OUT STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid : OUT STD_LOGIC;
        m_tlast  : OUT STD_LOGIC;
        m_tuser  : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF axis_loopback_tb_top IS
BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            m_tdata  <= s_tdata;
            m_tkeep  <= s_tkeep;
            m_tvalid <= s_tvalid;
            m_tlast  <= s_tlast;
            m_tuser  <= s_tuser;
        END IF;
    END PROCESS;

END ARCHITECTURE;
