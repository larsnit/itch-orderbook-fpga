-- Distributed memory with asynchronous read (implementation §5): read data follows the
-- address in the same cycle; a write takes effect at the end of its cycle.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY lutram IS
    GENERIC (
        WIDTH : POSITIVE := 64;
        DEPTH : POSITIVE := 128
    );
    PORT (
        clk           : IN  STD_LOGIC;
        read_address  : IN  UNSIGNED(clog2(DEPTH) - 1 DOWNTO 0);
        read_data     : OUT STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);
        write_enable  : IN  STD_LOGIC;
        write_address : IN  UNSIGNED(clog2(DEPTH) - 1 DOWNTO 0);
        write_data    : IN  STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF lutram IS

    TYPE MEM_T IS ARRAY (0 TO DEPTH - 1) OF STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);
    SIGNAL mem : MEM_T;

    ATTRIBUTE ram_style : STRING;
    ATTRIBUTE ram_style OF mem : SIGNAL IS "distributed";

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            IF write_enable = '1' THEN
                mem(to_integer(write_address)) <= write_data;
            END IF;
        END IF;
    END PROCESS;

    read_data <= mem(to_integer(read_address));

END ARCHITECTURE;
