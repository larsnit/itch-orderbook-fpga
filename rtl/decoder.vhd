-- Decoder (architecture section 7): one token in, at most one operation and one event out,
-- one cycle later.
--
--   sequence check   duplicate (dropped), gap (all books stale), expected sequence number
--   length check     A, F, E, C, X, D, U, R of the wrong length are unusable (all books stale)
--   symbol filter    R messages teach each book's locate code; other locates are dropped
--   operations       A, F: INSERT; E, C, X: REMOVE; D: DELETE; U: U_DELETE now and
--                    U_INSERT in the next cycle (held in a register)
--   events           one status event when all books first go stale (gap, bad FCS, unusable)

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY decoder IS
    PORT (
        clk             : IN  STD_LOGIC;
        rst             : IN  STD_LOGIC;
        cfg_ticker      : IN  TICKER_ARRAY_T;
        token           : IN  TOKEN_T;
        op              : OUT OP_T;
        event_out       : OUT EVENT_T;
        duplicate_pulse : OUT STD_LOGIC;
        gap_pulse       : OUT STD_LOGIC;
        unusable_pulse  : OUT STD_LOGIC;
        messages_pulse  : OUT STD_LOGIC;   -- a message in sequence (not a duplicate)
        events_pulse    : OUT STD_LOGIC;   -- an event was emitted
        all_books_stale : OUT STD_LOGIC    -- stale_all, for the STATUS register
    );
END ENTITY;

ARCHITECTURE rtl OF decoder IS

    -- the length a message of this type must have; 0 for types the decoder doesn't read
    FUNCTION expected_length(msg_type : MSG_TYPE_T) RETURN NATURAL IS
    BEGIN
        CASE msg_type IS
            WHEN TYPE_ADD            => RETURN 36;
            WHEN TYPE_ADD_MPID       => RETURN 40;
            WHEN TYPE_EXECUTED       => RETURN 31;
            WHEN TYPE_EXECUTED_PRICE => RETURN 36;
            WHEN TYPE_CANCEL         => RETURN 23;
            WHEN TYPE_DELETE         => RETURN 19;
            WHEN TYPE_REPLACE        => RETURN 35;
            WHEN TYPE_DIRECTORY      => RETURN 39;
            WHEN OTHERS              => RETURN 0;
        END CASE;
    END FUNCTION;

    TYPE LOCATE_ARRAY_T IS ARRAY (0 TO NUM_BOOKS - 1) OF UNSIGNED(15 DOWNTO 0);

    SIGNAL expected       : UNSIGNED(63 DOWNTO 0);              -- sequence number of the next message
    SIGNAL stale_all      : STD_LOGIC;
    SIGNAL last_timestamp : UNSIGNED(47 DOWNTO 0);              -- of the last usable message in sequence
    SIGNAL book_locate    : LOCATE_ARRAY_T;                     -- locate code of each book, from R messages
    SIGNAL locate_known   : STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
    SIGNAL u_insert       : OP_T;                               -- second half of a U, for the next cycle

    SIGNAL order_ref       : UNSIGNED(63 DOWNTO 0);             -- message bytes 11-18
    SIGNAL new_order_ref   : UNSIGNED(63 DOWNTO 0);             -- message bytes 19-26 (U)
    SIGNAL ref_bucket0     : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL ref_bucket1     : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL new_ref_bucket0 : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
    SIGNAL new_ref_bucket1 : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);

BEGIN

    order_ref     <= be(token.data, 11, 8);
    new_order_ref <= be(token.data, 19, 8);

    ref_hash : ENTITY work.h3_hash
        PORT MAP (order_ref => order_ref, bucket0 => ref_bucket0, bucket1 => ref_bucket1);

    new_ref_hash : ENTITY work.h3_hash
        PORT MAP (order_ref => new_order_ref, bucket0 => new_ref_bucket0, bucket1 => new_ref_bucket1);

    PROCESS(clk)
        VARIABLE stale       : BOOLEAN;
        VARIABLE event       : EVENT_T;
        VARIABLE next_op     : OP_T;
        VARIABLE msg_type    : MSG_TYPE_T;
        VARIABLE usable      : BOOLEAN;
        VARIABLE timestamp   : UNSIGNED(47 DOWNTO 0);
        VARIABLE locate      : UNSIGNED(15 DOWNTO 0);
        VARIABLE book        : NATURAL RANGE 0 TO NUM_BOOKS - 1;
        VARIABLE book_found  : BOOLEAN;
        VARIABLE side        : STD_LOGIC;

        -- all books stale; the first time, one event
        PROCEDURE go_stale(reason : NATURAL; seq : UNSIGNED(63 DOWNTO 0); at : UNSIGNED(47 DOWNTO 0)) IS
        BEGIN
            IF NOT stale THEN
                event.valid             := '1';
                event.flags             := (OTHERS => '0');
                event.flags(FLAG_STALE) := '1';
                event.flags(reason)     := '1';
                event.seq               := seq;
                event.timestamp         := at;
                events_pulse            <= '1';
                stale                   := TRUE;
            END IF;
        END PROCEDURE;

        -- an operation of the current message, with the first order ref's buckets
        IMPURE FUNCTION message_op(kind : OP_KIND_T; side : STD_LOGIC; shares : UNSIGNED(31 DOWNTO 0)) RETURN OP_T IS
            VARIABLE result : OP_T;
        BEGIN
            result.valid         := '1';
            result.kind          := kind;
            result.book          := to_unsigned(book, 3);
            result.side          := side;
            result.order_ref     := order_ref(31 DOWNTO 0);
            result.ref_error     := '1' WHEN order_ref(63 DOWNTO 32) /= 0 ELSE '0';
            result.bucket0       := ref_bucket0;
            result.bucket1       := ref_bucket1;
            result.shares        := shares;
            result.seq           := token.seq;
            result.timestamp     := timestamp;
            result.t_arrival     := token.t_arrival;
            result.stale_all     := '1' WHEN stale ELSE '0';
            result.record_enable := '1';
            RETURN result;
        END FUNCTION;

    BEGIN
        IF rising_edge(clk) THEN
            duplicate_pulse <= '0';
            gap_pulse       <= '0';
            unusable_pulse  <= '0';
            messages_pulse  <= '0';
            events_pulse    <= '0';

            stale         := stale_all = '1';
            event.valid   := '0';
            next_op       := u_insert;     -- the second half of a U from the cycle before, if any
            u_insert.valid <= '0';

            IF token.valid = '1' THEN
                CASE token.kind IS
                    WHEN TOK_MSG =>
                        IF token.seq < expected THEN
                            duplicate_pulse <= '1';
                        ELSE
                            messages_pulse <= '1';
                            expected       <= token.seq + 1;

                            msg_type  := token.data(7 DOWNTO 0);
                            usable    := expected_length(msg_type) = 0 OR token.len = expected_length(msg_type);
                            timestamp := be(token.data, 5, 6) WHEN usable ELSE last_timestamp;

                            IF token.seq > expected THEN
                                gap_pulse <= '1';
                                go_stale(FLAG_GAP, token.seq, timestamp);
                            END IF;

                            IF NOT usable THEN
                                unusable_pulse <= '1';
                                go_stale(FLAG_UNUSABLE, token.seq, timestamp);
                            ELSE
                                last_timestamp <= timestamp;

                                -- the book of this message's locate; the lowest book if several match
                                locate     := be(token.data, 1, 2);
                                book       := 0;
                                book_found := FALSE;
                                FOR i IN NUM_BOOKS - 1 DOWNTO 0 LOOP
                                    IF locate_known(i) = '1' AND book_locate(i) = locate THEN
                                        book       := i;
                                        book_found := TRUE;
                                    END IF;
                                END LOOP;

                                CASE msg_type IS
                                    WHEN TYPE_DIRECTORY =>
                                        FOR i IN 0 TO NUM_BOOKS - 1 LOOP
                                            IF be(token.data, 11, 8) = UNSIGNED(cfg_ticker(i)) THEN
                                                book_locate(i)  <= locate;
                                                locate_known(i) <= '1';
                                            END IF;
                                        END LOOP;

                                    WHEN TYPE_ADD | TYPE_ADD_MPID =>
                                        side := SIDE_ASK WHEN token.data(8*19 + 7 DOWNTO 8*19) = SIDE_SELL ELSE SIDE_BID;
                                        IF book_found THEN
                                            next_op := message_op(OP_INSERT, side, be(token.data, 20, 4));
                                        END IF;

                                    WHEN TYPE_EXECUTED | TYPE_EXECUTED_PRICE | TYPE_CANCEL =>
                                        IF book_found THEN
                                            next_op := message_op(OP_REMOVE, SIDE_BID, be(token.data, 19, 4));
                                        END IF;

                                    WHEN TYPE_DELETE =>
                                        IF book_found THEN
                                            next_op := message_op(OP_DELETE, SIDE_BID, (OTHERS => '0'));
                                        END IF;

                                    WHEN TYPE_REPLACE =>
                                        IF book_found THEN
                                            next_op               := message_op(OP_U_DELETE, SIDE_BID, (OTHERS => '0'));
                                            next_op.record_enable := '0';
                                            u_insert              <= message_op(OP_U_INSERT, SIDE_BID, be(token.data, 27, 4));
                                            u_insert.order_ref    <= new_order_ref(31 DOWNTO 0);
                                            u_insert.ref_error    <= '1' WHEN new_order_ref(63 DOWNTO 32) /= 0 ELSE '0';
                                            u_insert.bucket0      <= new_ref_bucket0;
                                            u_insert.bucket1      <= new_ref_bucket1;
                                        END IF;

                                    WHEN OTHERS =>
                                        NULL;
                                END CASE;
                            END IF;
                        END IF;

                    WHEN TOK_HEARTBEAT =>   -- seq is that of the next message
                        IF token.seq > expected THEN
                            gap_pulse <= '1';
                            go_stale(FLAG_GAP, token.seq, last_timestamp);
                            expected  <= token.seq;
                        END IF;

                    WHEN TOK_FCS =>         -- the frame's messages are applied; one may be corrupt
                        go_stale(FLAG_BAD_FCS, expected - 1, last_timestamp);

                    WHEN OTHERS =>
                        NULL;
                END CASE;
            END IF;

            op        <= next_op;
            event_out <= event;
            stale_all <= '1' WHEN stale ELSE '0';

            IF rst = '1' THEN
                expected        <= to_unsigned(1, 64);
                stale_all       <= '0';
                last_timestamp  <= (OTHERS => '0');
                locate_known    <= (OTHERS => '0');
                u_insert.valid  <= '0';
                op.valid        <= '0';
                event_out.valid <= '0';
                duplicate_pulse <= '0';
                gap_pulse       <= '0';
                unusable_pulse  <= '0';
                messages_pulse  <= '0';
                events_pulse    <= '0';
            END IF;
        END IF;
    END PROCESS;

    all_books_stale <= stale_all;

END ARCHITECTURE;
