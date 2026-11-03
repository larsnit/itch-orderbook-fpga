-- Simple dual-port memory: one read port, one write port (implementation §5).
-- Read data appears LATENCY cycles after the address: the memory read register plus
-- LATENCY - 1 output registers (Vivado places the first one inside the BRAM or URAM).
-- A read and a write of the same address in the same cycle return undefined data;
-- forwarding covers that case.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY ram_sdp IS
    GENERIC (
        WIDTH   : POSITIVE := 72;
        DEPTH   : POSITIVE := 512;
        LATENCY : POSITIVE := RAM_LATENCY;
        STYLE   : STRING   := "block"      -- "block" (BRAM) or "ultra" (URAM)
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

ARCHITECTURE rtl OF ram_sdp IS

    TYPE MEM_T IS ARRAY (0 TO DEPTH - 1) OF STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);
    SIGNAL mem : MEM_T;

    ATTRIBUTE ram_style : STRING;
    ATTRIBUTE ram_style OF mem : SIGNAL IS STYLE;

    TYPE READ_PIPE_T IS ARRAY (1 TO LATENCY) OF STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);
    SIGNAL read_pipe : READ_PIPE_T;

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            IF write_enable = '1' THEN
                mem(to_integer(write_address)) <= write_data;
            END IF;
            read_pipe(1) <= mem(to_integer(read_address));
            -- synthesis translate_off
            IF write_enable = '1' AND write_address = read_address THEN
                read_pipe(1) <= (OTHERS => 'X');   -- undefined in hardware: forwarding must cover it
            END IF;
            -- synthesis translate_on
            FOR stage IN 2 TO LATENCY LOOP
                read_pipe(stage) <= read_pipe(stage - 1);
            END LOOP;
        END IF;
    END PROCESS;

    read_data <= read_pipe(LATENCY);

END ARCHITECTURE;
