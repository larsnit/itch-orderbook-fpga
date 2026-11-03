-- The order table's stash (architecture section 8.1): STASH entries in registers for orders
-- whose two buckets are full, all compared with the ref in parallel. A write at the end of a
-- cycle is seen from the next cycle on, so no forwarding is needed.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY ot_stash IS
    PORT (
        clk          : IN  STD_LOGIC;
        rst          : IN  STD_LOGIC;
        lookup_ref   : IN  UNSIGNED(31 DOWNTO 0);
        valids       : OUT STD_LOGIC_VECTOR(STASH - 1 DOWNTO 0);   -- entry i holds an order
        hits         : OUT STD_LOGIC_VECTOR(STASH - 1 DOWNTO 0);   -- entry i holds lookup_ref
        hit_index    : OUT NATURAL RANGE 0 TO STASH - 1;           -- the lowest hit
        hit_entry    : OUT ENTRY_T;
        free_found   : OUT STD_LOGIC;
        free_index   : OUT NATURAL RANGE 0 TO STASH - 1;           -- the lowest free entry
        write_enable : IN  STD_LOGIC;
        write_index  : IN  NATURAL RANGE 0 TO STASH - 1;
        write_entry  : IN  ENTRY_T
    );
END ENTITY;

ARCHITECTURE rtl OF ot_stash IS

    TYPE ENTRIES_T IS ARRAY (0 TO STASH - 1) OF ENTRY_T;
    SIGNAL entries : ENTRIES_T;

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            IF write_enable = '1' THEN
                entries(write_index) <= write_entry;
            END IF;
            IF rst = '1' THEN
                FOR i IN 0 TO STASH - 1 LOOP
                    entries(i).valid <= '0';
                END LOOP;
            END IF;
        END IF;
    END PROCESS;

    -- highest first, so the lowest match is the one that stays
    PROCESS(ALL)
        VARIABLE hit  : NATURAL RANGE 0 TO STASH - 1;
        VARIABLE free : NATURAL RANGE 0 TO STASH - 1;
    BEGIN
        hit        := 0;
        free       := 0;
        free_found <= '0';
        FOR i IN STASH - 1 DOWNTO 0 LOOP
            valids(i) <= entries(i).valid;
            hits(i)   <= '0';
            IF entries(i).valid = '1' AND entries(i).order_ref = lookup_ref THEN
                hits(i) <= '1';
                hit     := i;
            END IF;
            IF entries(i).valid = '0' THEN
                free       := i;
                free_found <= '1';
            END IF;
        END LOOP;
        hit_index  <= hit;
        hit_entry  <= entries(hit);
        free_index <= free;
    END PROCESS;

END ARCHITECTURE;
