-- Book engine (architecture section 9): the price ladders of all books, and the top of book
-- after every operation. One shared pipeline works on 16 ladder_side instances (8 books x bid,
-- ask); the operation's (book, side) selects the instance.
--
--   level read    off-ladder position: count the order now (insert +1, whole order gone -1);
--                 snapshot the book's counters; inside: the level's index to ladder copy 1
--   level fetch   RAM_LATENCY - 1 cycles of memory output registers
--   level update  old level (pending writes forwarded), new = old +/- shares, written to both
--                 copies; the level's bitmap bit = new /= 0
--   search        best level of the side: each ladder_side searches every cycle, the stage
--                 takes its side's result
--   best read     the best index to ladder copy 2; a later operation that writes that level
--                 now or wrote it in the cycle before gives its old value instead
--   best fetch    RAM_LATENCY - 1 cycles of memory output registers
--   result        best shares, the other side's best registers, flags; top registered
--
-- The top is valid 2 * RAM_LATENCY + 3 = 7 cycles after the operation. After reset a sweep
-- writes zero into every ladder level and bitmap word; busy is high meanwhile and operations
-- are ignored.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY book_engine IS
    GENERIC (
        LADDER_STYLE : STRING := "block"   -- "block" (BRAM) or "ultra" (URAM)
    );
    PORT (
        clk      : IN  STD_LOGIC;
        rst      : IN  STD_LOGIC;
        cfg_base : IN  BASE_ARRAY_T;
        op       : IN  OT_RESULT_T;
        top      : OUT TOP_T;
        busy     : OUT STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF book_engine IS

    CONSTANT SIDES           : NATURAL := 2 * NUM_BOOKS;
    CONSTANT LAST_LEVEL      : UNSIGNED(LADDER_BITS - 1 DOWNTO 0) := (OTHERS => '1');

    -- stages, as cycles after level read
    CONSTANT AT_LEVEL_UPDATE : NATURAL := RAM_LATENCY;
    CONSTANT AT_SEARCH       : NATURAL := AT_LEVEL_UPDATE + 1;
    CONSTANT AT_BEST_READ    : NATURAL := AT_SEARCH + 1;
    CONSTANT AT_RESULT       : NATURAL := AT_BEST_READ + RAM_LATENCY;

    -- an operation and what it has collected on its way through the pipeline
    TYPE STAGE_T IS RECORD
        op             : OT_RESULT_T;
        occupied       : STD_LOGIC_VECTOR(5 DOWNTO 0);   -- off-ladder counters > 0: bid below 0, above 1, unrep 2; ask 3, 4, 5
        best_empty     : STD_LOGIC;                      -- from the search
        best_index     : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        forward        : STD_LOGIC;                      -- the best shares come from forward_shares
        forward_shares : UNSIGNED(31 DOWNTO 0);
    END RECORD;
    TYPE STAGES_T IS ARRAY (1 TO AT_RESULT) OF STAGE_T;

    -- a ladder write: (side, index) and the level's shares before and after
    TYPE LEVEL_WRITE_T IS RECORD
        valid : STD_LOGIC;
        side  : NATURAL RANGE 0 TO SIDES - 1;
        index : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        shares_before : UNSIGNED(31 DOWNTO 0);
        shares_after  : UNSIGNED(31 DOWNTO 0);
    END RECORD;
    TYPE HISTORY_T IS ARRAY (1 TO RAM_LATENCY) OF LEVEL_WRITE_T;   -- 1: the write of the cycle before

    TYPE SHARES_ARRAY_T  IS ARRAY (0 TO SIDES - 1) OF UNSIGNED(31 DOWNTO 0);
    TYPE INDEX_ARRAY_T   IS ARRAY (0 TO SIDES - 1) OF UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
    TYPE WORD_ARRAY_T    IS ARRAY (0 TO SIDES - 1) OF STD_LOGIC_VECTOR(63 DOWNTO 0);
    TYPE COUNTS_ARRAY_T  IS ARRAY (0 TO SIDES - 1) OF STD_LOGIC_VECTOR(2 DOWNTO 0);

    -- side number: book * 2 + side (bid 0, ask 1)
    FUNCTION side_number(book : UNSIGNED(2 DOWNTO 0); side : STD_LOGIC) RETURN NATURAL IS
    BEGIN
        RETURN to_integer(book & side);
    END FUNCTION;

    -- the record price of a ladder index: (base + index) cents in ITCH units
    FUNCTION price_of(base : UNSIGNED(31 DOWNTO 0); index : UNSIGNED(LADDER_BITS - 1 DOWNTO 0)) RETURN UNSIGNED IS
    BEGIN
        RETURN resize((base + index) * to_unsigned(100, 7), 32);
    END FUNCTION;

    SIGNAL stages      : STAGES_T;
    SIGNAL history     : HISTORY_T;
    SIGNAL level_now   : LEVEL_WRITE_T;   -- the level update's write in this cycle
    SIGNAL sweeping    : STD_LOGIC;
    SIGNAL sweep_level : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);

    -- ladder_side ports, one element per side; shared inputs are single signals
    SIGNAL level_shares        : SHARES_ARRAY_T;
    SIGNAL best_level_shares   : SHARES_ARRAY_T;
    SIGNAL update_word         : WORD_ARRAY_T;
    SIGNAL side_empty          : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0);
    SIGNAL side_best           : INDEX_ARRAY_T;
    SIGNAL occupied            : COUNTS_ARRAY_T;
    SIGNAL best_empty          : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0);
    SIGNAL best_index          : INDEX_ARRAY_T;
    SIGNAL best_shares         : SHARES_ARRAY_T;
    SIGNAL write_enable        : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0);
    SIGNAL bitmap_write_enable : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0);
    SIGNAL count_enable        : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0);
    SIGNAL best_write_enable   : STD_LOGIC_VECTOR(SIDES - 1 DOWNTO 0);

    SIGNAL write_index         : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
    SIGNAL write_shares        : UNSIGNED(31 DOWNTO 0);
    SIGNAL update_word_index   : UNSIGNED(LADDER_BITS - 7 DOWNTO 0);
    SIGNAL bitmap_write_index  : UNSIGNED(LADDER_BITS - 7 DOWNTO 0);
    SIGNAL bitmap_write_word   : STD_LOGIC_VECTOR(63 DOWNTO 0);
    SIGNAL best_read_index     : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);

    -- per stage
    SIGNAL accepted        : STD_LOGIC;                      -- level read: the input operation is taken
    SIGNAL count_up        : STD_LOGIC;
    SIGNAL search_side     : NATURAL RANGE 0 TO SIDES - 1;
    SIGNAL read_occupied   : STD_LOGIC_VECTOR(5 DOWNTO 0);   -- level read: the snapshot
    SIGNAL search_empty    : STD_LOGIC;
    SIGNAL search_index    : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
    SIGNAL best_forward    : STD_LOGIC;
    SIGNAL best_forwarded  : UNSIGNED(31 DOWNTO 0);
    SIGNAL next_top        : TOP_T;
    SIGNAL own_best_empty  : STD_LOGIC;
    SIGNAL own_best_index  : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
    SIGNAL own_best_shares : UNSIGNED(31 DOWNTO 0);

BEGIN

    ladders : FOR s IN 0 TO SIDES - 1 GENERATE
        ladder : ENTITY work.ladder_side
            GENERIC MAP (BID_SIDE => s MOD 2 = 0, STYLE => LADDER_STYLE)
            PORT MAP (
                clk                 => clk,
                rst                 => rst,
                level_read_index    => op.index,
                level_shares        => level_shares(s),
                best_read_index     => best_read_index,
                best_level_shares   => best_level_shares(s),
                write_enable        => write_enable(s),
                write_index         => write_index,
                write_shares        => write_shares,
                update_word_index   => update_word_index,
                update_word         => update_word(s),
                search_empty        => side_empty(s),
                search_index        => side_best(s),
                bitmap_write_enable => bitmap_write_enable(s),
                bitmap_write_index  => bitmap_write_index,
                bitmap_write_word   => bitmap_write_word,
                count_enable        => count_enable(s),
                count_position      => op.position,
                count_up            => count_up,
                occupied            => occupied(s),
                best_write_enable   => best_write_enable(s),
                best_write_empty    => own_best_empty,
                best_write_index    => own_best_index,
                best_write_shares   => own_best_shares,
                best_empty          => best_empty(s),
                best_index          => best_index(s),
                best_shares         => best_shares(s)
            );
    END GENERATE;

    -- level read: count off-ladder orders, snapshot the book's counters
    level_read : PROCESS(ALL)
        VARIABLE counted : BOOLEAN;
    BEGIN
        accepted <= op.valid AND NOT sweeping;
        count_up <= '1' WHEN op.change = CHG_ADD ELSE '0';
        counted  := op.valid = '1' AND sweeping = '0' AND op.position /= POS_INSIDE
                    AND (op.change = CHG_ADD OR (op.change = CHG_REMOVE AND op.order_gone = '1'));
        count_enable <= (OTHERS => '0');
        IF counted THEN
            count_enable(side_number(op.book, op.side)) <= '1';
        END IF;
        read_occupied <= occupied(side_number(op.book, SIDE_ASK)) & occupied(side_number(op.book, SIDE_BID));
    END PROCESS;

    -- level update: old level (newest pending write wins), new level, bitmap bit
    level_update : PROCESS(ALL)
        VARIABLE stage : STAGE_T;
        VARIABLE side  : NATURAL RANGE 0 TO SIDES - 1;
        VARIABLE shares_before : UNSIGNED(31 DOWNTO 0);
        VARIABLE shares_after  : UNSIGNED(31 DOWNTO 0);
        VARIABLE writes : STD_LOGIC;
        VARIABLE word   : STD_LOGIC_VECTOR(63 DOWNTO 0);
    BEGIN
        stage := stages(AT_LEVEL_UPDATE);
        side  := side_number(stage.op.book, stage.op.side);

        shares_before := level_shares(side);
        FOR k IN RAM_LATENCY DOWNTO 1 LOOP
            IF history(k).valid = '1' AND history(k).side = side AND history(k).index = stage.op.index THEN
                shares_before := history(k).shares_after;
            END IF;
        END LOOP;
        shares_after  := shares_before + stage.op.shares WHEN stage.op.change = CHG_ADD ELSE shares_before - stage.op.shares;
        writes := '1' WHEN stage.op.valid = '1' AND stage.op.change /= CHG_NONE AND stage.op.position = POS_INSIDE ELSE '0';

        level_now <= (
            valid         => writes,
            side          => side,
            index         => stage.op.index,
            shares_before => shares_before,
            shares_after  => shares_after
        );

        update_word_index <= stage.op.index(LADDER_BITS - 1 DOWNTO 6);
        word := update_word(side);
        word(to_integer(stage.op.index(5 DOWNTO 0))) := '0' WHEN shares_after = 0 ELSE '1';

        write_enable        <= (OTHERS => sweeping);
        bitmap_write_enable <= (OTHERS => sweeping);
        IF sweeping = '1' THEN
            write_index        <= sweep_level;
            write_shares       <= (OTHERS => '0');
            bitmap_write_index <= sweep_level(LADDER_BITS - 7 DOWNTO 0);
            bitmap_write_word  <= (OTHERS => '0');
        ELSE
            write_index        <= stage.op.index;
            write_shares       <= shares_after;
            bitmap_write_index <= stage.op.index(LADDER_BITS - 1 DOWNTO 6);
            bitmap_write_word  <= word;
            IF writes = '1' THEN
                write_enable(side)        <= '1';
                bitmap_write_enable(side) <= '1';
            END IF;
        END IF;
    END PROCESS;

    -- search: the best level of the operation's side
    search_side  <= side_number(stages(AT_SEARCH).op.book, stages(AT_SEARCH).op.side);
    search_empty <= side_empty(search_side);
    search_index <= side_best(search_side);

    -- best read: a later operation that writes the best level now (2 behind) or wrote it in
    -- the cycle before (1 behind) has changed the memory; its old value is this operation's
    best_read : PROCESS(ALL)
        VARIABLE stage : STAGE_T;
        VARIABLE side  : NATURAL RANGE 0 TO SIDES - 1;
    BEGIN
        stage := stages(AT_BEST_READ);
        side  := side_number(stage.op.book, stage.op.side);
        best_read_index <= stage.best_index;
        best_forward    <= '0';
        best_forwarded  <= level_now.shares_before;
        IF level_now.valid = '1' AND level_now.side = side AND level_now.index = stage.best_index THEN
            best_forward   <= '1';
            best_forwarded <= level_now.shares_before;
        END IF;
        IF history(1).valid = '1' AND history(1).side = side AND history(1).index = stage.best_index THEN
            best_forward   <= '1';
            best_forwarded <= history(1).shares_before;   -- the earlier of the two
        END IF;
    END PROCESS;

    -- result: the top of the operation's book, flags; the own side's best registers
    result_stage : PROCESS(ALL)
        VARIABLE stage       : STAGE_T;
        VARIABLE own         : NATURAL RANGE 0 TO SIDES - 1;
        VARIABLE other       : NATURAL RANGE 0 TO SIDES - 1;
        VARIABLE shares      : UNSIGNED(31 DOWNTO 0);
        VARIABLE bid_empty   : STD_LOGIC;
        VARIABLE ask_empty   : STD_LOGIC;
        VARIABLE bid_index   : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        VARIABLE ask_index   : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        VARIABLE bid_shares  : UNSIGNED(31 DOWNTO 0);
        VARIABLE ask_shares  : UNSIGNED(31 DOWNTO 0);
        VARIABLE base        : UNSIGNED(31 DOWNTO 0);
        VARIABLE bid_outside : STD_LOGIC;
        VARIABLE ask_outside : STD_LOGIC;
    BEGIN
        stage := stages(AT_RESULT);
        own   := side_number(stage.op.book, stage.op.side);
        other := side_number(stage.op.book, NOT stage.op.side);

        shares := stage.forward_shares WHEN stage.forward = '1' ELSE best_level_shares(own);
        IF stage.best_empty = '1' THEN
            shares := (OTHERS => '0');
        END IF;

        -- this side from the search, the other side from its best registers
        IF stage.op.side = SIDE_BID THEN
            bid_empty  := stage.best_empty;
            bid_index  := stage.best_index;
            bid_shares := shares;
            ask_empty  := best_empty(other);
            ask_index  := best_index(other);
            ask_shares := best_shares(other);
        ELSE
            ask_empty  := stage.best_empty;
            ask_index  := stage.best_index;
            ask_shares := shares;
            bid_empty  := best_empty(other);
            bid_index  := best_index(other);
            bid_shares := best_shares(other);
        END IF;

        -- the true top may lie outside the window: a better or unrepresentable order off the
        -- ladder, or nothing on the ladder and a worse order off it
        bid_outside := stage.occupied(2) OR stage.occupied(1) OR (bid_empty AND stage.occupied(0));
        ask_outside := stage.occupied(5) OR stage.occupied(3) OR (ask_empty AND stage.occupied(4));

        base := cfg_base(to_integer(stage.op.book));
        next_top.valid                      <= stage.op.valid;
        next_top.book                       <= stage.op.book;
        next_top.flags                      <= (OTHERS => '0');
        next_top.flags(FLAG_STALE)          <= stage.op.stale_all OR stage.op.book_stale;
        next_top.flags(FLAG_OUTSIDE_WINDOW) <= bid_outside OR ask_outside;
        next_top.bid_price                  <= NO_BID_PRICE WHEN bid_empty = '1' ELSE price_of(base, bid_index);
        next_top.bid_shares                 <= (OTHERS => '0') WHEN bid_empty = '1' ELSE bid_shares;
        next_top.ask_price                  <= NO_ASK_PRICE WHEN ask_empty = '1' ELSE price_of(base, ask_index);
        next_top.ask_shares                 <= (OTHERS => '0') WHEN ask_empty = '1' ELSE ask_shares;
        next_top.record_enable              <= stage.op.record_enable;
        next_top.seq                        <= stage.op.seq;
        next_top.timestamp                  <= stage.op.timestamp;
        next_top.t_arrival                  <= stage.op.t_arrival;

        own_best_empty  <= stage.best_empty;
        own_best_index  <= stage.best_index;
        own_best_shares <= shares;
        best_write_enable <= (OTHERS => '0');
        IF stage.op.valid = '1' THEN
            best_write_enable(own) <= '1';
        END IF;
    END PROCESS;

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            -- level read -> level fetch: the operation and its counter snapshot
            stages(1).op             <= op;
            stages(1).op.valid       <= accepted;
            stages(1).occupied       <= read_occupied;
            stages(1).best_empty     <= '1';
            stages(1).best_index     <= (OTHERS => '0');
            stages(1).forward        <= '0';
            stages(1).forward_shares <= (OTHERS => '0');
            FOR k IN 2 TO AT_RESULT LOOP
                stages(k) <= stages(k - 1);
            END LOOP;
            stages(AT_SEARCH + 1).best_empty <= search_empty;
            stages(AT_SEARCH + 1).best_index <= search_index;
            stages(AT_BEST_READ + 1).forward        <= best_forward AND NOT stages(AT_BEST_READ).best_empty;
            stages(AT_BEST_READ + 1).forward_shares <= best_forwarded;

            history(1) <= level_now;
            FOR k IN 2 TO RAM_LATENCY LOOP
                history(k) <= history(k - 1);
            END LOOP;

            top <= next_top;

            IF sweeping = '1' THEN
                sweep_level <= sweep_level + 1;
                IF sweep_level = LAST_LEVEL THEN
                    sweeping <= '0';
                END IF;
            END IF;

            IF rst = '1' THEN
                FOR k IN 1 TO AT_RESULT LOOP
                    stages(k).op.valid <= '0';
                END LOOP;
                FOR k IN 1 TO RAM_LATENCY LOOP
                    history(k).valid <= '0';
                END LOOP;
                top.valid   <= '0';
                sweeping    <= '1';
                sweep_level <= (OTHERS => '0');
            END IF;
        END IF;
    END PROCESS;

    busy <= sweeping;

END ARCHITECTURE;
