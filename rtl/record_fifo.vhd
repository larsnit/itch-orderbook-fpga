-- Asynchronous FIFO between the core clock (write side) and the transmit clock (read side), for
-- the record output (implementation section 12.8a).
--
-- The pointers cross as Gray code through two registers (ASYNC_REG); each side compares its own
-- binary pointer with the other side's synchronized one, so it sees the other side a few cycles
-- late: full and level are conservative, never wrong.
--
-- Write: w_valid with w_data; a write while full is dropped (w_dropped = 1 that cycle).
-- Read: r_level entries are there; r_read takes one, its data is in r_data the next cycle.
-- Both resets must be applied together (itch_top's record_tx crosses each into the other domain).

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

ENTITY record_fifo IS
    GENERIC (
        WIDTH     : POSITIVE := 256;
        ADDR_BITS : POSITIVE := 9     -- 512 entries
    );
    PORT (
        w_clk     : IN  STD_LOGIC;
        w_rst     : IN  STD_LOGIC;
        w_valid   : IN  STD_LOGIC;
        w_data    : IN  STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);
        w_dropped : OUT STD_LOGIC;
        r_clk     : IN  STD_LOGIC;
        r_rst     : IN  STD_LOGIC;
        r_level   : OUT UNSIGNED(ADDR_BITS DOWNTO 0);
        r_read    : IN  STD_LOGIC;
        r_data    : OUT STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF record_fifo IS

    CONSTANT DEPTH : POSITIVE := 2 ** ADDR_BITS;

    SUBTYPE POINTER_T IS UNSIGNED(ADDR_BITS DOWNTO 0);   -- one bit more than the address: full vs empty
    TYPE MEM_T IS ARRAY (0 TO DEPTH - 1) OF STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);

    FUNCTION to_gray(value : POINTER_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN STD_LOGIC_VECTOR(value XOR shift_right(value, 1));
    END FUNCTION;

    FUNCTION from_gray(gray : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0)) RETURN POINTER_T IS
        VARIABLE value : POINTER_T;
    BEGIN
        value(ADDR_BITS) := gray(ADDR_BITS);
        FOR i IN ADDR_BITS - 1 DOWNTO 0 LOOP
            value(i) := value(i + 1) XOR gray(i);
        END LOOP;
        RETURN value;
    END FUNCTION;

    SIGNAL mem : MEM_T;
    ATTRIBUTE ram_style : STRING;
    ATTRIBUTE ram_style OF mem : SIGNAL IS "block";

    SIGNAL write_pointer   : POINTER_T := (OTHERS => '0');
    SIGNAL write_gray      : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0) := (OTHERS => '0');
    SIGNAL read_pointer    : POINTER_T := (OTHERS => '0');
    SIGNAL read_gray       : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0) := (OTHERS => '0');
    -- the other side's pointer, two registers deep
    SIGNAL read_gray_sync1  : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0) := (OTHERS => '0');
    SIGNAL read_gray_sync2  : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0) := (OTHERS => '0');
    SIGNAL write_gray_sync1 : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0) := (OTHERS => '0');
    SIGNAL write_gray_sync2 : STD_LOGIC_VECTOR(ADDR_BITS DOWNTO 0) := (OTHERS => '0');

    ATTRIBUTE async_reg : STRING;
    ATTRIBUTE async_reg OF read_gray_sync1, read_gray_sync2, write_gray_sync1, write_gray_sync2 : SIGNAL IS "TRUE";

BEGIN

    write_side : PROCESS(w_clk)
        VARIABLE used : POINTER_T;
    BEGIN
        IF rising_edge(w_clk) THEN
            read_gray_sync1 <= read_gray;
            read_gray_sync2 <= read_gray_sync1;
            used      := write_pointer - from_gray(read_gray_sync2);
            w_dropped <= '0';
            IF w_valid = '1' THEN
                IF used = DEPTH THEN
                    w_dropped <= '1';
                ELSE
                    mem(to_integer(write_pointer(ADDR_BITS - 1 DOWNTO 0))) <= w_data;
                    write_pointer <= write_pointer + 1;
                    write_gray    <= to_gray(write_pointer + 1);
                END IF;
            END IF;
            IF w_rst = '1' THEN
                write_pointer   <= (OTHERS => '0');
                write_gray      <= (OTHERS => '0');
                read_gray_sync1 <= (OTHERS => '0');
                read_gray_sync2 <= (OTHERS => '0');
                w_dropped       <= '0';
            END IF;
        END IF;
    END PROCESS;

    read_side : PROCESS(r_clk)
    BEGIN
        IF rising_edge(r_clk) THEN
            write_gray_sync1 <= write_gray;
            write_gray_sync2 <= write_gray_sync1;
            r_data <= mem(to_integer(read_pointer(ADDR_BITS - 1 DOWNTO 0)));
            IF r_read = '1' THEN
                read_pointer <= read_pointer + 1;
                read_gray    <= to_gray(read_pointer + 1);
            END IF;
            IF r_rst = '1' THEN
                read_pointer     <= (OTHERS => '0');
                read_gray        <= (OTHERS => '0');
                write_gray_sync1 <= (OTHERS => '0');
                write_gray_sync2 <= (OTHERS => '0');
            END IF;
        END IF;
    END PROCESS;

    r_level <= from_gray(write_gray_sync2) - read_pointer;

    -- synthesis translate_off
    PROCESS(r_clk)
    BEGIN
        IF rising_edge(r_clk) THEN
            ASSERT NOT (r_rst = '0' AND r_read = '1' AND from_gray(write_gray_sync2) = read_pointer)
                REPORT "record_fifo: read while empty" SEVERITY FAILURE;
        END IF;
    END PROCESS;
    -- synthesis translate_on

END ARCHITECTURE;
