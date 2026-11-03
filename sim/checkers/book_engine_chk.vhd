-- Simulation-only checks of book_engine (implementation section 9.4), on its ports and, through
-- external names, its ladder writes and search result. The checker keeps its own copy of every
-- ladder level, from the writes, and its own delay line of the operations.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY book_engine_chk IS
    PORT (
        clk  : IN STD_LOGIC;
        rst  : IN STD_LOGIC;
        op   : IN OT_RESULT_T;
        busy : IN STD_LOGIC;
        top  : IN TOP_T
    );
END ENTITY;

ARCHITECTURE sim OF book_engine_chk IS

    CONSTANT SIDES        : NATURAL := 2 * NUM_BOOKS;
    CONSTANT LEVELS       : NATURAL := 2**LADDER_BITS;
    CONSTANT LEVEL_UPDATE : NATURAL := RAM_LATENCY;
    CONSTANT SEARCH       : NATURAL := RAM_LATENCY + 1;
    CONSTANT LATENCY      : NATURAL := 2 * RAM_LATENCY + 3;

    ALIAS search_empty IS << SIGNAL .book_engine_tb_top.dut.search_empty : STD_LOGIC >>;
    ALIAS search_index IS << SIGNAL .book_engine_tb_top.dut.search_index : UNSIGNED(LADDER_BITS - 1 DOWNTO 0) >>;
    ALIAS write_enable IS << SIGNAL .book_engine_tb_top.dut.write_enable : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0) >>;
    ALIAS write_index  IS << SIGNAL .book_engine_tb_top.dut.write_index : UNSIGNED(LADDER_BITS - 1 DOWNTO 0) >>;
    ALIAS write_shares IS << SIGNAL .book_engine_tb_top.dut.write_shares : UNSIGNED(31 DOWNTO 0) >>;

    TYPE OPS_T IS ARRAY (1 TO LATENCY) OF OT_RESULT_T;   -- ops(k): the operation taken k cycles ago
    SIGNAL ops        : OPS_T;
    SIGNAL reset_seen : BOOLEAN := FALSE;

BEGIN

    PROCESS(clk)
        TYPE LEVELS_T IS ARRAY (0 TO LEVELS - 1) OF UNSIGNED(31 DOWNTO 0);
        TYPE LADDERS_T IS ARRAY (0 TO SIDES - 1) OF LEVELS_T;
        VARIABLE ladders : LADDERS_T := (OTHERS => (OTHERS => (OTHERS => '0')));
        VARIABLE side    : NATURAL;
        VARIABLE best    : INTEGER;
        VARIABLE update_op : OT_RESULT_T;
        VARIABLE search_op : OT_RESULT_T;
    BEGIN
        IF rising_edge(clk) THEN
            IF rst = '1' THEN
                reset_seen <= TRUE;
            END IF;
            update_op := ops(LEVEL_UPDATE);
            search_op := ops(SEARCH);

            IF rst = '0' AND reset_seen THEN
                ASSERT top.valid = ops(LATENCY).valid
                    REPORT "a top without its operation 7 cycles before, or the other way round" SEVERITY FAILURE;

                IF update_op.valid = '1' AND update_op.change = CHG_REMOVE AND update_op.position = POS_INSIDE THEN
                    side := to_integer(update_op.book & update_op.side);
                    ASSERT ladders(side)(to_integer(update_op.index)) >= update_op.shares
                        REPORT "level below zero" SEVERITY FAILURE;
                END IF;

                -- the search finds the highest (bid) or lowest (ask) non-empty level
                IF search_op.valid = '1' THEN
                    side := to_integer(search_op.book & search_op.side);
                    best := -1;
                    FOR level IN 0 TO LEVELS - 1 LOOP
                        IF ladders(side)(level) /= 0 AND (best = -1 OR search_op.side = SIDE_BID) THEN
                            best := level;
                        END IF;
                    END LOOP;
                    ASSERT (best = -1) = (search_empty = '1')
                        REPORT "search: empty side wrong" SEVERITY FAILURE;
                    ASSERT best = -1 OR to_integer(search_index) = best
                        REPORT "search: best level " & INTEGER'IMAGE(to_integer(search_index)) & ", expected "
                               & INTEGER'IMAGE(best) SEVERITY FAILURE;
                END IF;
            END IF;

            -- a write is seen from the next cycle on, by the bitmaps and by this copy
            FOR s IN 0 TO SIDES - 1 LOOP
                IF write_enable(s) = '1' THEN
                    ladders(s)(to_integer(write_index)) := write_shares;
                END IF;
            END LOOP;

            ops(1)       <= op;
            ops(1).valid <= op.valid AND NOT busy;
            FOR k IN 2 TO LATENCY LOOP
                ops(k) <= ops(k - 1);
            END LOOP;
            IF rst = '1' THEN
                FOR k IN 1 TO LATENCY LOOP
                    ops(k).valid <= '0';
                END LOOP;
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
