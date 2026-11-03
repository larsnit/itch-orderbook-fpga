-- Order table (architecture section 8): every live order of the tracked books, so that E, C,
-- X, D and U, which carry only the order ref, find their order's book, side and price level.
--
--   read     bucket0 and bucket1 of the operation go to the two banks
--   fetch    RAM_LATENCY - 1 cycles of memory output registers; the banks compare the bucket
--            with their pending writes
--   match    the rows (pending writes forwarded) and the stash are searched for the ref;
--            rows, hits and the stash entry are registered
--   update   the insert or remove is decided, the changed row or stash entry written back
--   result   registered: valid 4 cycles after the operation
--
-- Operations of different messages are at least 2 cycles apart; only U_INSERT follows its
-- U_DELETE in the next cycle. So the only write a match stage cannot see is U_DELETE's, for
-- U_INSERT: its update stage takes the row U_DELETE wrote (previous_write) and drops hits on
-- slots that are no longer valid.
--
-- After reset a sweep writes empty rows into every bucket of both banks, one per cycle; busy is
-- high meanwhile and operations are ignored.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY order_table IS
    GENERIC (
        TABLE_STYLE : STRING := "ultra"   -- "ultra" (URAM) or "block" (BRAM)
    );
    PORT (
        clk                  : IN  STD_LOGIC;
        rst                  : IN  STD_LOGIC;
        op                   : IN  OP_T;
        price_info           : IN  PRICE_INFO_T;   -- of the operation in the update stage
        result               : OUT OT_RESULT_T;
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

ARCHITECTURE rtl OF order_table IS

    CONSTANT NO_ENTRY : ENTRY_T := (
        valid     => '0',
        order_ref => (OTHERS => '0'),
        book      => (OTHERS => '0'),
        side      => '0',
        position  => POS_INSIDE,
        index     => (OTHERS => '0'),
        shares    => (OTHERS => '0')
    );
    CONSTANT EMPTY_ROW   : BUCKET_T := (OTHERS => NO_ENTRY);
    CONSTANT LAST_BUCKET : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0) := (OTHERS => '1');

    TYPE PULSES_T IS RECORD
        insert_failure : STD_LOGIC;
        ref_error      : STD_LOGIC;
        unknown_ref    : STD_LOGIC;
        inconsistent   : STD_LOGIC;
        stash_insert   : STD_LOGIC;
        off_ladder     : STD_LOGIC;
        unrep          : STD_LOGIC;
    END RECORD;
    CONSTANT NO_PULSES : PULSES_T := (OTHERS => '0');

    TYPE OP_DELAY_T IS ARRAY (1 TO RAM_LATENCY) OF OP_T;
    TYPE SHARES_ARRAY_T IS ARRAY (0 TO WAYS - 1) OF UNSIGNED(31 DOWNTO 0);

    FUNCTION lowest_set(bits : STD_LOGIC_VECTOR) RETURN NATURAL IS
    BEGIN
        FOR i IN bits'LOW TO bits'HIGH LOOP
            IF bits(i) = '1' THEN
                RETURN i;
            END IF;
        END LOOP;
        RETURN 0;
    END FUNCTION;

    FUNCTION free_slots(row : BUCKET_T) RETURN STD_LOGIC_VECTOR IS
        VARIABLE free : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    BEGIN
        FOR slot IN 0 TO WAYS - 1 LOOP
            free(slot) := NOT row(slot).valid;
        END LOOP;
        RETURN free;
    END FUNCTION;

    FUNCTION used_slots(row : BUCKET_T) RETURN NATURAL IS
        VARIABLE count : NATURAL RANGE 0 TO WAYS := 0;
    BEGIN
        FOR slot IN 0 TO WAYS - 1 LOOP
            IF row(slot).valid = '1' THEN
                count := count + 1;
            END IF;
        END LOOP;
        RETURN count;
    END FUNCTION;

    -- the row as the update stage must use it: U_DELETE's write of the cycle before, if it went
    -- to this bucket, else the row registered in the match stage
    FUNCTION current_row(registered : BUCKET_T; written : STD_LOGIC; written_bucket : UNSIGNED;
                         written_row : BUCKET_T; bucket : UNSIGNED) RETURN BUCKET_T IS
    BEGIN
        IF written = '1' AND written_bucket = bucket THEN
            RETURN written_row;
        END IF;
        RETURN registered;
    END FUNCTION;

    FUNCTION valid_slots(row : BUCKET_T) RETURN STD_LOGIC_VECTOR IS
        VARIABLE valid : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    BEGIN
        FOR slot IN 0 TO WAYS - 1 LOOP
            valid(slot) := row(slot).valid;
        END LOOP;
        RETURN valid;
    END FUNCTION;

    -- pipeline: op_delay(k) is the operation k cycles after its read stage
    SIGNAL op_delay        : OP_DELAY_T;
    SIGNAL op_before_match : OP_T;
    SIGNAL op_match        : OP_T;
    SIGNAL op_update       : OP_T;

    -- match stage: the two rows and the stash
    SIGNAL row0             : BUCKET_T;
    SIGNAL row1             : BUCKET_T;
    SIGNAL hits0            : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL hits1            : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL stash_hits       : STD_LOGIC_VECTOR(STASH - 1 DOWNTO 0);
    SIGNAL stash_hit_index  : NATURAL RANGE 0 TO STASH - 1;
    SIGNAL stash_hit_entry  : ENTRY_T;

    -- match stage: a REMOVE's arithmetic for every slot and the stash entry, before knowing which
    -- one holds the order: shares left, all shares used up, more removed than there are
    SIGNAL left0            : SHARES_ARRAY_T;
    SIGNAL left1            : SHARES_ARRAY_T;
    SIGNAL used_up0         : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL used_up1         : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL too_many0        : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL too_many1        : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL stash_left       : UNSIGNED(31 DOWNTO 0);
    SIGNAL stash_used_up    : STD_LOGIC;
    SIGNAL stash_too_many   : STD_LOGIC;

    -- registered in the match stage, used in the update stage
    SIGNAL matched_row0           : BUCKET_T;
    SIGNAL matched_row1           : BUCKET_T;
    SIGNAL matched_hits0          : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL matched_hits1          : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL matched_stash_hits     : STD_LOGIC_VECTOR(STASH - 1 DOWNTO 0);
    SIGNAL matched_stash_index    : NATURAL RANGE 0 TO STASH - 1;
    SIGNAL matched_stash_entry    : ENTRY_T;
    SIGNAL matched_left0          : SHARES_ARRAY_T;
    SIGNAL matched_left1          : SHARES_ARRAY_T;
    SIGNAL matched_used_up0       : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL matched_used_up1       : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL matched_too_many0      : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL matched_too_many1      : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
    SIGNAL matched_stash_left     : UNSIGNED(31 DOWNTO 0);
    SIGNAL matched_stash_used_up  : STD_LOGIC;
    SIGNAL matched_stash_too_many : STD_LOGIC;

    -- update stage: the current stash, and the writes of the cycle before
    SIGNAL stash_valids      : STD_LOGIC_VECTOR(STASH - 1 DOWNTO 0);
    SIGNAL stash_free_found  : STD_LOGIC;
    SIGNAL stash_free_index  : NATURAL RANGE 0 TO STASH - 1;
    SIGNAL written0          : STD_LOGIC;
    SIGNAL written1          : STD_LOGIC;
    SIGNAL written_bucket0   : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL written_bucket1   : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL written_row0      : BUCKET_T;
    SIGNAL written_row1      : BUCKET_T;

    -- update stage: decisions, written or registered at the end of the cycle
    SIGNAL update_write0      : STD_LOGIC;
    SIGNAL update_write1      : STD_LOGIC;
    SIGNAL update_row0        : BUCKET_T;
    SIGNAL update_row1        : BUCKET_T;
    SIGNAL stash_write        : STD_LOGIC;
    SIGNAL stash_write_index  : NATURAL RANGE 0 TO STASH - 1;
    SIGNAL stash_write_entry  : ENTRY_T;
    SIGNAL next_result        : OT_RESULT_T;
    SIGNAL next_pulses        : PULSES_T;
    SIGNAL next_stale         : STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
    SIGNAL next_u_found       : STD_LOGIC;
    SIGNAL next_u_book        : UNSIGNED(2 DOWNTO 0);
    SIGNAL next_u_side        : STD_LOGIC;

    -- registers
    SIGNAL stale        : STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
    SIGNAL u_found      : STD_LOGIC;               -- the last U_DELETE found its order ...
    SIGNAL u_book       : UNSIGNED(2 DOWNTO 0);    -- ... of this book
    SIGNAL u_side       : STD_LOGIC;               -- ... and side, for the U_INSERT
    SIGNAL sweeping     : STD_LOGIC;
    SIGNAL sweep_bucket : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);

    -- bank write ports: the sweep, or the update stage
    SIGNAL write_enable0 : STD_LOGIC;
    SIGNAL write_enable1 : STD_LOGIC;
    SIGNAL write_bucket0 : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL write_bucket1 : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL write_row0    : BUCKET_T;
    SIGNAL write_row1    : BUCKET_T;

BEGIN

    op_match <= op_delay(RAM_LATENCY);

    before_match_read : IF RAM_LATENCY = 1 GENERATE
        op_before_match <= op;
    ELSE GENERATE
        op_before_match <= op_delay(RAM_LATENCY - 1);
    END GENERATE;

    write_enable0 <= sweeping OR update_write0;
    write_enable1 <= sweeping OR update_write1;
    write_bucket0 <= sweep_bucket WHEN sweeping = '1' ELSE op_update.bucket0;
    write_bucket1 <= sweep_bucket WHEN sweeping = '1' ELSE op_update.bucket1;
    write_row0    <= EMPTY_ROW WHEN sweeping = '1' ELSE update_row0;
    write_row1    <= EMPTY_ROW WHEN sweeping = '1' ELSE update_row1;

    bank0 : ENTITY work.ot_bank
        GENERIC MAP (STYLE => TABLE_STYLE)
        PORT MAP (
            clk                   => clk,
            rst                   => rst,
            read_bucket           => op.bucket0,
            next_bucket           => op_before_match.bucket0,
            row                   => row0,
            write_enable          => write_enable0,
            write_bucket          => write_bucket0,
            write_row             => write_row0,
            previous_write_valid  => written0,
            previous_write_bucket => written_bucket0,
            previous_write_row    => written_row0
        );

    bank1 : ENTITY work.ot_bank
        GENERIC MAP (STYLE => TABLE_STYLE)
        PORT MAP (
            clk                   => clk,
            rst                   => rst,
            read_bucket           => op.bucket1,
            next_bucket           => op_before_match.bucket1,
            row                   => row1,
            write_enable          => write_enable1,
            write_bucket          => write_bucket1,
            write_row             => write_row1,
            previous_write_valid  => written1,
            previous_write_bucket => written_bucket1,
            previous_write_row    => written_row1
        );

    stash_entries : ENTITY work.ot_stash
        PORT MAP (
            clk          => clk,
            rst          => rst,
            lookup_ref   => op_match.order_ref,
            valids       => stash_valids,
            hits         => stash_hits,
            hit_index    => stash_hit_index,
            hit_entry    => stash_hit_entry,
            free_found   => stash_free_found,
            free_index   => stash_free_index,
            write_enable => stash_write,
            write_index  => stash_write_index,
            write_entry  => stash_write_entry
        );

    slots : FOR slot IN 0 TO WAYS - 1 GENERATE
        hits0(slot) <= '1' WHEN row0(slot).valid = '1' AND row0(slot).order_ref = op_match.order_ref ELSE '0';
        hits1(slot) <= '1' WHEN row1(slot).valid = '1' AND row1(slot).order_ref = op_match.order_ref ELSE '0';

        left0(slot)     <= row0(slot).shares - op_match.shares;
        left1(slot)     <= row1(slot).shares - op_match.shares;
        used_up0(slot)  <= '1' WHEN op_match.shares >= row0(slot).shares ELSE '0';
        used_up1(slot)  <= '1' WHEN op_match.shares >= row1(slot).shares ELSE '0';
        too_many0(slot) <= '1' WHEN op_match.shares > row0(slot).shares ELSE '0';
        too_many1(slot) <= '1' WHEN op_match.shares > row1(slot).shares ELSE '0';
    END GENERATE;

    stash_left     <= stash_hit_entry.shares - op_match.shares;
    stash_used_up  <= '1' WHEN op_match.shares >= stash_hit_entry.shares ELSE '0';
    stash_too_many <= '1' WHEN op_match.shares > stash_hit_entry.shares ELSE '0';

    update : PROCESS(ALL)
        VARIABLE res        : OT_RESULT_T;
        VARIABLE pulses     : PULSES_T;
        VARIABLE new_stale  : STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
        VARIABLE now_row0   : BUCKET_T;
        VARIABLE now_row1   : BUCKET_T;
        VARIABLE now_hits0  : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
        VARIABLE now_hits1  : STD_LOGIC_VECTOR(WAYS - 1 DOWNTO 0);
        VARIABLE new_row0   : BUCKET_T;
        VARIABLE new_row1   : BUCKET_T;
        VARIABLE found      : BOOLEAN;
        VARIABLE entry      : ENTRY_T;
        VARIABLE side       : STD_LOGIC;
        VARIABLE inserted   : BOOLEAN;
        VARIABLE removed    : UNSIGNED(31 DOWNTO 0);
        VARIABLE remaining  : UNSIGNED(31 DOWNTO 0);
        VARIABLE left       : UNSIGNED(31 DOWNTO 0);
        VARIABLE used_up    : STD_LOGIC;
        VARIABLE too_many   : STD_LOGIC;
        VARIABLE used0      : NATURAL RANGE 0 TO WAYS;
        VARIABLE used1      : NATURAL RANGE 0 TO WAYS;

        -- an order of this book is missing or wrong
        PROCEDURE mark_stale(book : UNSIGNED(2 DOWNTO 0)) IS
        BEGIN
            new_stale(to_integer(book)) := '1';
        END PROCEDURE;
    BEGIN
        res := (
            valid         => op_update.valid,
            change        => CHG_NONE,
            book          => op_update.book,
            side          => SIDE_BID,
            position      => POS_INSIDE,
            index         => (OTHERS => '0'),
            shares        => (OTHERS => '0'),
            order_gone    => '0',
            book_stale    => '0',
            stale_all     => op_update.stale_all,
            record_enable => op_update.record_enable,
            seq           => op_update.seq,
            timestamp     => op_update.timestamp,
            t_arrival     => op_update.t_arrival
        );
        pulses            := NO_PULSES;
        new_stale         := stale;
        -- the rows now: only U_INSERT can differ from what its match stage saw
        now_row0  := current_row(matched_row0, written0, written_bucket0, written_row0, op_update.bucket0);
        now_row1  := current_row(matched_row1, written1, written_bucket1, written_row1, op_update.bucket1);
        now_hits0 := matched_hits0 AND valid_slots(now_row0);
        now_hits1 := matched_hits1 AND valid_slots(now_row1);
        new_row0  := now_row0;
        new_row1  := now_row1;
        update_write0     <= '0';
        update_write1     <= '0';
        stash_write       <= '0';
        stash_write_index <= matched_stash_index;
        stash_write_entry <= NO_ENTRY;
        next_u_found      <= u_found;
        next_u_book       <= u_book;
        next_u_side       <= u_side;

        -- the order with this ref: a ref is stored at most once; a ref error never matches
        found := op_update.ref_error = '0'
                 AND (OR now_hits0 = '1' OR OR now_hits1 = '1' OR OR (matched_stash_hits AND stash_valids) = '1');
        IF OR matched_hits0 = '1' THEN
            entry    := matched_row0(lowest_set(matched_hits0));
            left     := matched_left0(lowest_set(matched_hits0));
            used_up  := matched_used_up0(lowest_set(matched_hits0));
            too_many := matched_too_many0(lowest_set(matched_hits0));
        ELSIF OR matched_hits1 = '1' THEN
            entry    := matched_row1(lowest_set(matched_hits1));
            left     := matched_left1(lowest_set(matched_hits1));
            used_up  := matched_used_up1(lowest_set(matched_hits1));
            too_many := matched_too_many1(lowest_set(matched_hits1));
        ELSE
            entry    := matched_stash_entry;
            left     := matched_stash_left;
            used_up  := matched_stash_used_up;
            too_many := matched_stash_too_many;
        END IF;

        IF op_update.valid = '1' THEN
            CASE op_update.kind IS
                WHEN OP_INSERT | OP_U_INSERT =>
                    side := op_update.side WHEN op_update.kind = OP_INSERT ELSE u_side;
                    inserted := FALSE;
                    IF op_update.kind = OP_U_INSERT AND u_found = '0' THEN
                        NULL;   -- the U_DELETE found no order; counted there
                    ELSIF op_update.kind = OP_U_INSERT AND u_book /= op_update.book THEN
                        pulses.inconsistent := '1';
                        mark_stale(u_book);
                        mark_stale(op_update.book);
                    ELSIF op_update.ref_error = '1' THEN
                        pulses.ref_error := '1';
                        mark_stale(op_update.book);
                    ELSIF found THEN
                        pulses.inconsistent := '1';
                        mark_stale(op_update.book);
                    ELSE
                        entry := (
                            valid     => '1',
                            order_ref => op_update.order_ref,
                            book      => op_update.book,
                            side      => side,
                            position  => price_info.position,
                            index     => price_info.index,
                            shares    => op_update.shares
                        );
                        used0 := used_slots(now_row0);
                        used1 := used_slots(now_row1);
                        IF used0 <= used1 AND used0 < WAYS THEN
                            new_row0(lowest_set(free_slots(now_row0))) := entry;
                            update_write0 <= '1';
                            inserted := TRUE;
                        ELSIF used1 < used0 THEN
                            new_row1(lowest_set(free_slots(now_row1))) := entry;
                            update_write1 <= '1';
                            inserted := TRUE;
                        ELSIF stash_free_found = '1' THEN
                            stash_write       <= '1';
                            stash_write_index <= stash_free_index;
                            stash_write_entry <= entry;
                            pulses.stash_insert := '1';
                            inserted := TRUE;
                        ELSE
                            pulses.insert_failure := '1';
                            mark_stale(op_update.book);
                        END IF;
                    END IF;
                    IF inserted THEN
                        res.change   := CHG_ADD;
                        res.side     := side;
                        res.position := price_info.position;
                        res.index    := price_info.index;
                        res.shares   := op_update.shares;
                        IF price_info.position = POS_UNREP THEN
                            pulses.unrep := '1';
                        ELSIF price_info.position /= POS_INSIDE THEN
                            pulses.off_ladder := '1';
                        END IF;
                    END IF;

                WHEN OTHERS =>   -- REMOVE, DELETE, U_DELETE
                    IF op_update.kind = OP_U_DELETE THEN
                        next_u_found <= '0';
                    END IF;
                    IF NOT found THEN
                        pulses.unknown_ref := '1';
                        mark_stale(op_update.book);
                    ELSE
                        -- the arithmetic was done in the match stage
                        IF op_update.kind /= OP_REMOVE THEN   -- DELETE, U_DELETE: all of them
                            removed   := entry.shares;
                            remaining := (OTHERS => '0');
                            used_up   := '1';
                        ELSIF too_many = '1' THEN              -- clamped
                            pulses.inconsistent := '1';
                            mark_stale(entry.book);
                            removed   := entry.shares;
                            remaining := (OTHERS => '0');
                        ELSE
                            removed   := op_update.shares;
                            remaining := left;
                        END IF;

                        res.change     := CHG_REMOVE;
                        res.book       := entry.book;
                        res.side       := entry.side;
                        res.position   := entry.position;
                        res.index      := entry.index;
                        res.shares     := removed;
                        res.order_gone := used_up;

                        IF op_update.kind = OP_U_DELETE THEN
                            next_u_found <= '1';
                            next_u_book  <= entry.book;
                            next_u_side  <= entry.side;
                        END IF;

                        entry.shares := remaining;
                        IF used_up = '1' THEN
                            entry := NO_ENTRY;
                        END IF;
                        IF OR matched_hits0 = '1' THEN
                            new_row0(lowest_set(matched_hits0)) := entry;
                            update_write0 <= '1';
                        ELSIF OR matched_hits1 = '1' THEN
                            new_row1(lowest_set(matched_hits1)) := entry;
                            update_write1 <= '1';
                        ELSE
                            stash_write       <= '1';
                            stash_write_entry <= entry;
                        END IF;
                    END IF;
            END CASE;
        END IF;

        res.book_stale := new_stale(to_integer(res.book));
        next_result    <= res;
        next_pulses    <= pulses;
        next_stale     <= new_stale;
        update_row0    <= new_row0;
        update_row1    <= new_row1;
    END PROCESS;

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            op_delay(1)       <= op;
            op_delay(1).valid <= op.valid AND NOT sweeping;
            FOR k IN 2 TO RAM_LATENCY LOOP
                op_delay(k) <= op_delay(k - 1);
            END LOOP;

            -- match -> update
            op_update              <= op_match;
            matched_row0           <= row0;
            matched_row1           <= row1;
            matched_hits0          <= hits0;
            matched_hits1          <= hits1;
            matched_stash_hits     <= stash_hits;
            matched_stash_index    <= stash_hit_index;
            matched_stash_entry    <= stash_hit_entry;
            matched_left0          <= left0;
            matched_left1          <= left1;
            matched_used_up0       <= used_up0;
            matched_used_up1       <= used_up1;
            matched_too_many0      <= too_many0;
            matched_too_many1      <= too_many1;
            matched_stash_left     <= stash_left;
            matched_stash_used_up  <= stash_used_up;
            matched_stash_too_many <= stash_too_many;

            result               <= next_result;
            insert_failure_pulse <= next_pulses.insert_failure;
            ref_error_pulse      <= next_pulses.ref_error;
            unknown_ref_pulse    <= next_pulses.unknown_ref;
            inconsistent_pulse   <= next_pulses.inconsistent;
            stash_insert_pulse   <= next_pulses.stash_insert;
            off_ladder_pulse     <= next_pulses.off_ladder;
            unrep_pulse          <= next_pulses.unrep;
            stale                <= next_stale;
            u_found              <= next_u_found;
            u_book               <= next_u_book;
            u_side               <= next_u_side;

            IF sweeping = '1' THEN
                sweep_bucket <= sweep_bucket + 1;
                IF sweep_bucket = LAST_BUCKET THEN
                    sweeping <= '0';
                END IF;
            END IF;

            IF rst = '1' THEN
                FOR k IN 1 TO RAM_LATENCY LOOP
                    op_delay(k).valid <= '0';
                END LOOP;
                op_update.valid      <= '0';
                result.valid         <= '0';
                insert_failure_pulse <= '0';
                ref_error_pulse      <= '0';
                unknown_ref_pulse    <= '0';
                inconsistent_pulse   <= '0';
                stash_insert_pulse   <= '0';
                off_ladder_pulse     <= '0';
                unrep_pulse          <= '0';
                stale                <= (OTHERS => '0');
                u_found              <= '0';
                sweeping             <= '1';
                sweep_bucket         <= (OTHERS => '0');
            END IF;
        END IF;
    END PROCESS;

    book_stale <= stale;
    busy       <= sweeping;

END ARCHITECTURE;
