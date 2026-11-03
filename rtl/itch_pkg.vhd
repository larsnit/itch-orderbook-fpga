-- Shared constants, types and conversions of the ITCH feed handler (implementation §3).
--
-- Bit order, the same everywhere and in tb/common/structs.py:
--   records             first field in the most significant bits
--   arrays, byte buses  element 0 in the least significant bits (message byte j, record byte j
--                       and bucket slot i all sit at the bottom end of their vector)

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

PACKAGE itch_pkg IS

    -- Sizes

    CONSTANT NUM_BOOKS   : NATURAL := 8;
    CONSTANT LADDER_BITS : NATURAL := 13;   -- W = 8,192 levels (cents)
    CONSTANT BUCKET_BITS : NATURAL := 13;   -- 8,192 buckets per table
    CONSTANT WAYS        : NATURAL := 8;    -- slots per bucket
    CONSTANT STASH       : NATURAL := 32;   -- stash entries
    CONSTANT MSG_BYTES   : NATURAL := 40;   -- bytes of a message the framer keeps
    CONSTANT RAM_LATENCY : NATURAL := 2;    -- address cycle → data cycle (§5)
    CONSTANT EVENT_DELAY : NATURAL := 11;   -- registers between decoder and output stage (§8.7)

    CONSTANT NO_BID_PRICE : UNSIGNED(31 DOWNTO 0) := x"00000000";
    CONSTANT NO_ASK_PRICE : UNSIGNED(31 DOWNTO 0) := x"FFFFFFFF";

    FUNCTION clog2(n : POSITIVE) RETURN NATURAL;

    -- Codes

    SUBTYPE TOKEN_KIND_T IS STD_LOGIC_VECTOR(1 DOWNTO 0);
    CONSTANT TOK_MSG       : TOKEN_KIND_T := "00";
    CONSTANT TOK_HEARTBEAT : TOKEN_KIND_T := "01";
    CONSTANT TOK_FCS       : TOKEN_KIND_T := "10";

    SUBTYPE OP_KIND_T IS STD_LOGIC_VECTOR(2 DOWNTO 0);
    CONSTANT OP_INSERT   : OP_KIND_T := "000";
    CONSTANT OP_REMOVE   : OP_KIND_T := "001";
    CONSTANT OP_DELETE   : OP_KIND_T := "010";
    CONSTANT OP_U_DELETE : OP_KIND_T := "011";
    CONSTANT OP_U_INSERT : OP_KIND_T := "100";

    SUBTYPE POSITION_T IS STD_LOGIC_VECTOR(1 DOWNTO 0);
    CONSTANT POS_INSIDE : POSITION_T := "00";
    CONSTANT POS_BELOW  : POSITION_T := "01";
    CONSTANT POS_ABOVE  : POSITION_T := "10";
    CONSTANT POS_UNREP  : POSITION_T := "11";

    SUBTYPE CHANGE_T IS STD_LOGIC_VECTOR(1 DOWNTO 0);
    CONSTANT CHG_NONE   : CHANGE_T := "00";
    CONSTANT CHG_ADD    : CHANGE_T := "01";
    CONSTANT CHG_REMOVE : CHANGE_T := "10";

    CONSTANT SIDE_BID : STD_LOGIC := '0';   -- ITCH "B"
    CONSTANT SIDE_ASK : STD_LOGIC := '1';   -- ITCH "S"

    -- ITCH message types (message byte 0) and the side byte of an add
    SUBTYPE MSG_TYPE_T IS STD_LOGIC_VECTOR(7 DOWNTO 0);
    CONSTANT TYPE_ADD            : MSG_TYPE_T := x"41";   -- A
    CONSTANT TYPE_ADD_MPID       : MSG_TYPE_T := x"46";   -- F
    CONSTANT TYPE_EXECUTED       : MSG_TYPE_T := x"45";   -- E
    CONSTANT TYPE_EXECUTED_PRICE : MSG_TYPE_T := x"43";   -- C
    CONSTANT TYPE_CANCEL         : MSG_TYPE_T := x"58";   -- X
    CONSTANT TYPE_DELETE         : MSG_TYPE_T := x"44";   -- D
    CONSTANT TYPE_REPLACE        : MSG_TYPE_T := x"55";   -- U
    CONSTANT TYPE_DIRECTORY      : MSG_TYPE_T := x"52";   -- R
    CONSTANT SIDE_SELL           : MSG_TYPE_T := x"53";   -- S

    -- Flag bits of a record
    CONSTANT FLAG_STALE          : NATURAL := 0;
    CONSTANT FLAG_OUTSIDE_WINDOW : NATURAL := 1;
    CONSTANT FLAG_GAP            : NATURAL := 2;   -- status records only
    CONSTANT FLAG_BAD_FCS        : NATURAL := 3;   -- status records only
    CONSTANT FLAG_UNUSABLE       : NATURAL := 4;   -- status records only

    CONSTANT STATUS_BOOK_ID : UNSIGNED(7 DOWNTO 0) := x"FF";

    -- Message bytes: byte j in bits 8j+7 DOWNTO 8j

    SUBTYPE MSG_DATA_T IS STD_LOGIC_VECTOR(MSG_BYTES*8 - 1 DOWNTO 0);

    -- count bytes starting at offset, most significant byte first (ITCH fields are big-endian)
    FUNCTION be(data : MSG_DATA_T; offset : NATURAL; count : POSITIVE) RETURN UNSIGNED;

    -- Interfaces between stages

    -- framer → decoder
    TYPE TOKEN_T IS RECORD
        valid     : STD_LOGIC;
        kind      : TOKEN_KIND_T;
        seq       : UNSIGNED(63 DOWNTO 0);     -- MSG: its number; HEARTBEAT: the next message's
        len       : UNSIGNED(15 DOWNTO 0);
        data      : MSG_DATA_T;
        t_arrival : UNSIGNED(15 DOWNTO 0);     -- cycle counter at the message's last byte
    END RECORD;
    CONSTANT TOKEN_WIDTH : NATURAL := 1 + 2 + 64 + 16 + MSG_BYTES*8 + 16;

    -- decoder → order table
    TYPE OP_T IS RECORD
        valid         : STD_LOGIC;
        kind          : OP_KIND_T;
        book          : UNSIGNED(2 DOWNTO 0);  -- from the symbol filter
        side          : STD_LOGIC;             -- INSERT only; U_INSERT takes it from U_DELETE
        order_ref     : UNSIGNED(31 DOWNTO 0);
        ref_error     : STD_LOGIC;             -- the 64-bit ref has bits above 31 set
        bucket0       : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);  -- h0(ref)
        bucket1       : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);  -- h1(ref)
        shares        : UNSIGNED(31 DOWNTO 0); -- INSERT/U_INSERT: order shares; REMOVE: shares removed
        seq           : UNSIGNED(63 DOWNTO 0);
        timestamp     : UNSIGNED(47 DOWNTO 0);
        t_arrival     : UNSIGNED(15 DOWNTO 0);
        stale_all     : STD_LOGIC;             -- snapshot at the decoder
        record_enable : STD_LOGIC;             -- '0' for U_DELETE
    END RECORD;
    CONSTANT OP_WIDTH : NATURAL := 1 + 3 + 3 + 1 + 32 + 1 + 2*BUCKET_BITS + 32 + 64 + 48 + 16 + 1 + 1;

    -- price unit → order table, arrives in the update stage together with its operation
    TYPE PRICE_INFO_T IS RECORD
        position : POSITION_T;
        index    : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
    END RECORD;
    CONSTANT PRICE_INFO_WIDTH : NATURAL := 2 + LADDER_BITS;

    -- one order-table slot
    TYPE ENTRY_T IS RECORD
        valid     : STD_LOGIC;
        order_ref : UNSIGNED(31 DOWNTO 0);
        book      : UNSIGNED(2 DOWNTO 0);
        side      : STD_LOGIC;
        position  : POSITION_T;
        index     : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        shares    : UNSIGNED(31 DOWNTO 0);
    END RECORD;
    CONSTANT ENTRY_WIDTH : NATURAL := 1 + 32 + 3 + 1 + 2 + LADDER_BITS + 32;   -- 84
    CONSTANT ROW_WIDTH   : NATURAL := WAYS * ENTRY_WIDTH;                        -- 672

    -- one memory row; slot i in row bits (i+1)*ENTRY_WIDTH-1 DOWNTO i*ENTRY_WIDTH
    TYPE BUCKET_T IS ARRAY (0 TO WAYS - 1) OF ENTRY_T;

    -- order table → book engine
    TYPE OT_RESULT_T IS RECORD
        valid         : STD_LOGIC;
        change        : CHANGE_T;              -- NONE for unknown refs, failures, duplicates
        book          : UNSIGNED(2 DOWNTO 0);
        side          : STD_LOGIC;
        position      : POSITION_T;
        index         : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        shares        : UNSIGNED(31 DOWNTO 0); -- shares added or removed
        order_gone    : STD_LOGIC;             -- the whole order left the table
        book_stale    : STD_LOGIC;             -- this book's stale bit after this operation
        stale_all     : STD_LOGIC;
        record_enable : STD_LOGIC;
        seq           : UNSIGNED(63 DOWNTO 0);
        timestamp     : UNSIGNED(47 DOWNTO 0);
        t_arrival     : UNSIGNED(15 DOWNTO 0);
    END RECORD;
    CONSTANT OT_RESULT_WIDTH : NATURAL := 1 + 2 + 3 + 1 + 2 + LADDER_BITS + 32 + 1 + 1 + 1 + 1 + 64 + 48 + 16;

    -- book engine → output stage
    TYPE TOP_T IS RECORD
        valid         : STD_LOGIC;
        book          : UNSIGNED(2 DOWNTO 0);
        flags         : STD_LOGIC_VECTOR(7 DOWNTO 0);   -- bit 0 stale, bit 1 top outside window
        bid_price     : UNSIGNED(31 DOWNTO 0);
        bid_shares    : UNSIGNED(31 DOWNTO 0);
        ask_price     : UNSIGNED(31 DOWNTO 0);
        ask_shares    : UNSIGNED(31 DOWNTO 0);
        record_enable : STD_LOGIC;
        seq           : UNSIGNED(63 DOWNTO 0);
        timestamp     : UNSIGNED(47 DOWNTO 0);
        t_arrival     : UNSIGNED(15 DOWNTO 0);
    END RECORD;
    CONSTANT TOP_WIDTH : NATURAL := 1 + 3 + 8 + 4*32 + 1 + 64 + 48 + 16;

    -- decoder → event delay → output stage
    TYPE EVENT_T IS RECORD
        valid     : STD_LOGIC;
        flags     : STD_LOGIC_VECTOR(7 DOWNTO 0);   -- bit 0 stale, bit 2 gap, bit 3 FCS, bit 4 unusable
        seq       : UNSIGNED(63 DOWNTO 0);
        timestamp : UNSIGNED(47 DOWNTO 0);
    END RECORD;
    CONSTANT EVENT_WIDTH : NATURAL := 1 + 8 + 64 + 48;

    -- Configuration

    -- first letter in bits 63 DOWNTO 56, space-padded: compares directly with be(data, 11, 8) of an R
    TYPE TICKER_ARRAY_T IS ARRAY (0 TO NUM_BOOKS - 1) OF STD_LOGIC_VECTOR(63 DOWNTO 0);
    TYPE BASE_ARRAY_T   IS ARRAY (0 TO NUM_BOOKS - 1) OF UNSIGNED(31 DOWNTO 0);   -- window start, cents

    -- Counters, in register-map order: register offset 0x100 + 4 * index

    CONSTANT CNT_DROPPED_FRAMES    : NATURAL := 0;
    CONSTANT CNT_MALFORMED_PACKETS : NATURAL := 1;
    CONSTANT CNT_FCS_ERRORS        : NATURAL := 2;
    CONSTANT CNT_DUPLICATES        : NATURAL := 3;
    CONSTANT CNT_GAPS              : NATURAL := 4;
    CONSTANT CNT_UNUSABLE_MESSAGES : NATURAL := 5;
    CONSTANT CNT_INSERT_FAILURES   : NATURAL := 6;
    CONSTANT CNT_REF_ERRORS        : NATURAL := 7;
    CONSTANT CNT_UNKNOWN_REFS      : NATURAL := 8;
    CONSTANT CNT_INCONSISTENT      : NATURAL := 9;
    CONSTANT CNT_OFF_LADDER_ORDERS : NATURAL := 10;
    CONSTANT CNT_UNREP_PRICES      : NATURAL := 11;
    CONSTANT CNT_STASH_INSERTS     : NATURAL := 12;
    CONSTANT CNT_MESSAGES          : NATURAL := 13;
    CONSTANT CNT_RECORDS           : NATURAL := 14;
    CONSTANT CNT_EVENTS            : NATURAL := 15;
    CONSTANT NUM_COUNTERS          : NATURAL := 16;

    CONSTANT COUNTER_MAX : UNSIGNED(31 DOWNTO 0) := x"FFFFFFFF";

    TYPE COUNTER_ARRAY_T IS ARRAY (0 TO NUM_COUNTERS - 1) OF UNSIGNED(31 DOWNTO 0);

    -- Conversions to and from flat vectors (memories, testbench wrappers)

    FUNCTION to_slv(token : TOKEN_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(op : OP_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(price_info : PRICE_INFO_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(entry : ENTRY_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(bucket : BUCKET_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(result : OT_RESULT_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(top : TOP_T) RETURN STD_LOGIC_VECTOR;
    FUNCTION to_slv(event : EVENT_T) RETURN STD_LOGIC_VECTOR;

    FUNCTION to_token(vector : STD_LOGIC_VECTOR) RETURN TOKEN_T;
    FUNCTION to_op(vector : STD_LOGIC_VECTOR) RETURN OP_T;
    FUNCTION to_price_info(vector : STD_LOGIC_VECTOR) RETURN PRICE_INFO_T;
    FUNCTION to_entry(vector : STD_LOGIC_VECTOR) RETURN ENTRY_T;
    FUNCTION to_bucket(vector : STD_LOGIC_VECTOR) RETURN BUCKET_T;
    FUNCTION to_ot_result(vector : STD_LOGIC_VECTOR) RETURN OT_RESULT_T;
    FUNCTION to_top(vector : STD_LOGIC_VECTOR) RETURN TOP_T;
    FUNCTION to_event(vector : STD_LOGIC_VECTOR) RETURN EVENT_T;

    -- The 32-byte output record (§3.5): record byte j in bits 8j+7 DOWNTO 8j

    CONSTANT RECORD_WIDTH : NATURAL := 256;

    FUNCTION pack_record(
        book_id    : UNSIGNED(7 DOWNTO 0);
        flags      : STD_LOGIC_VECTOR(7 DOWNTO 0);
        timestamp  : UNSIGNED(47 DOWNTO 0);
        seq        : UNSIGNED(63 DOWNTO 0);
        bid_price  : UNSIGNED(31 DOWNTO 0);
        bid_shares : UNSIGNED(31 DOWNTO 0);
        ask_price  : UNSIGNED(31 DOWNTO 0);
        ask_shares : UNSIGNED(31 DOWNTO 0)
    ) RETURN STD_LOGIC_VECTOR;

    FUNCTION pack_record(top : TOP_T) RETURN STD_LOGIC_VECTOR;       -- book record
    FUNCTION pack_record(event : EVENT_T) RETURN STD_LOGIC_VECTOR;   -- status record

END PACKAGE;


PACKAGE BODY itch_pkg IS

    FUNCTION clog2(n : POSITIVE) RETURN NATURAL IS
        VARIABLE bits : NATURAL := 0;
    BEGIN
        WHILE 2**bits < n LOOP
            bits := bits + 1;
        END LOOP;
        RETURN bits;
    END FUNCTION;

    FUNCTION be(data : MSG_DATA_T; offset : NATURAL; count : POSITIVE) RETURN UNSIGNED IS
        VARIABLE result : UNSIGNED(8*count - 1 DOWNTO 0);
    BEGIN
        FOR i IN 0 TO count - 1 LOOP
            result(8*(count - i) - 1 DOWNTO 8*(count - i - 1)) := UNSIGNED(data(8*(offset + i) + 7 DOWNTO 8*(offset + i)));
        END LOOP;
        RETURN result;
    END FUNCTION;

    -- Unpacking: take the next field from the top of the vector; high is the bit where it starts

    PROCEDURE take(bits : IN STD_LOGIC_VECTOR; high : INOUT INTEGER; field : OUT STD_LOGIC) IS
    BEGIN
        field := bits(high);
        high  := high - 1;
    END PROCEDURE;

    PROCEDURE take(bits : IN STD_LOGIC_VECTOR; high : INOUT INTEGER; field : OUT STD_LOGIC_VECTOR) IS
    BEGIN
        field := bits(high DOWNTO high - field'LENGTH + 1);
        high  := high - field'LENGTH;
    END PROCEDURE;

    PROCEDURE take(bits : IN STD_LOGIC_VECTOR; high : INOUT INTEGER; field : OUT UNSIGNED) IS
    BEGIN
        field := UNSIGNED(bits(high DOWNTO high - field'LENGTH + 1));
        high  := high - field'LENGTH;
    END PROCEDURE;

    -- TOKEN_T

    FUNCTION to_slv(token : TOKEN_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN token.valid & token.kind & STD_LOGIC_VECTOR(token.seq) & STD_LOGIC_VECTOR(token.len)
             & token.data & STD_LOGIC_VECTOR(token.t_arrival);
    END FUNCTION;

    FUNCTION to_token(vector : STD_LOGIC_VECTOR) RETURN TOKEN_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high  : INTEGER := bits'HIGH;
        VARIABLE token : TOKEN_T;
    BEGIN
        take(bits, high, token.valid);
        take(bits, high, token.kind);
        take(bits, high, token.seq);
        take(bits, high, token.len);
        take(bits, high, token.data);
        take(bits, high, token.t_arrival);
        RETURN token;
    END FUNCTION;

    -- OP_T

    FUNCTION to_slv(op : OP_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN op.valid & op.kind & STD_LOGIC_VECTOR(op.book) & op.side & STD_LOGIC_VECTOR(op.order_ref)
             & op.ref_error & STD_LOGIC_VECTOR(op.bucket0) & STD_LOGIC_VECTOR(op.bucket1)
             & STD_LOGIC_VECTOR(op.shares) & STD_LOGIC_VECTOR(op.seq) & STD_LOGIC_VECTOR(op.timestamp)
             & STD_LOGIC_VECTOR(op.t_arrival) & op.stale_all & op.record_enable;
    END FUNCTION;

    FUNCTION to_op(vector : STD_LOGIC_VECTOR) RETURN OP_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high : INTEGER := bits'HIGH;
        VARIABLE op   : OP_T;
    BEGIN
        take(bits, high, op.valid);
        take(bits, high, op.kind);
        take(bits, high, op.book);
        take(bits, high, op.side);
        take(bits, high, op.order_ref);
        take(bits, high, op.ref_error);
        take(bits, high, op.bucket0);
        take(bits, high, op.bucket1);
        take(bits, high, op.shares);
        take(bits, high, op.seq);
        take(bits, high, op.timestamp);
        take(bits, high, op.t_arrival);
        take(bits, high, op.stale_all);
        take(bits, high, op.record_enable);
        RETURN op;
    END FUNCTION;

    -- PRICE_INFO_T

    FUNCTION to_slv(price_info : PRICE_INFO_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN price_info.position & STD_LOGIC_VECTOR(price_info.index);
    END FUNCTION;

    FUNCTION to_price_info(vector : STD_LOGIC_VECTOR) RETURN PRICE_INFO_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high       : INTEGER := bits'HIGH;
        VARIABLE price_info : PRICE_INFO_T;
    BEGIN
        take(bits, high, price_info.position);
        take(bits, high, price_info.index);
        RETURN price_info;
    END FUNCTION;

    -- ENTRY_T and BUCKET_T

    FUNCTION to_slv(entry : ENTRY_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN entry.valid & STD_LOGIC_VECTOR(entry.order_ref) & STD_LOGIC_VECTOR(entry.book) & entry.side
             & entry.position & STD_LOGIC_VECTOR(entry.index) & STD_LOGIC_VECTOR(entry.shares);
    END FUNCTION;

    FUNCTION to_entry(vector : STD_LOGIC_VECTOR) RETURN ENTRY_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high  : INTEGER := bits'HIGH;
        VARIABLE entry : ENTRY_T;
    BEGIN
        take(bits, high, entry.valid);
        take(bits, high, entry.order_ref);
        take(bits, high, entry.book);
        take(bits, high, entry.side);
        take(bits, high, entry.position);
        take(bits, high, entry.index);
        take(bits, high, entry.shares);
        RETURN entry;
    END FUNCTION;

    FUNCTION to_slv(bucket : BUCKET_T) RETURN STD_LOGIC_VECTOR IS
        VARIABLE row : STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
    BEGIN
        FOR slot IN 0 TO WAYS - 1 LOOP
            row((slot + 1)*ENTRY_WIDTH - 1 DOWNTO slot*ENTRY_WIDTH) := to_slv(bucket(slot));
        END LOOP;
        RETURN row;
    END FUNCTION;

    FUNCTION to_bucket(vector : STD_LOGIC_VECTOR) RETURN BUCKET_T IS
        ALIAS row : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE bucket : BUCKET_T;
    BEGIN
        FOR slot IN 0 TO WAYS - 1 LOOP
            bucket(slot) := to_entry(row((slot + 1)*ENTRY_WIDTH - 1 DOWNTO slot*ENTRY_WIDTH));
        END LOOP;
        RETURN bucket;
    END FUNCTION;

    -- OT_RESULT_T

    FUNCTION to_slv(result : OT_RESULT_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN result.valid & result.change & STD_LOGIC_VECTOR(result.book) & result.side & result.position
             & STD_LOGIC_VECTOR(result.index) & STD_LOGIC_VECTOR(result.shares) & result.order_gone
             & result.book_stale & result.stale_all & result.record_enable & STD_LOGIC_VECTOR(result.seq)
             & STD_LOGIC_VECTOR(result.timestamp) & STD_LOGIC_VECTOR(result.t_arrival);
    END FUNCTION;

    FUNCTION to_ot_result(vector : STD_LOGIC_VECTOR) RETURN OT_RESULT_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high   : INTEGER := bits'HIGH;
        VARIABLE result : OT_RESULT_T;
    BEGIN
        take(bits, high, result.valid);
        take(bits, high, result.change);
        take(bits, high, result.book);
        take(bits, high, result.side);
        take(bits, high, result.position);
        take(bits, high, result.index);
        take(bits, high, result.shares);
        take(bits, high, result.order_gone);
        take(bits, high, result.book_stale);
        take(bits, high, result.stale_all);
        take(bits, high, result.record_enable);
        take(bits, high, result.seq);
        take(bits, high, result.timestamp);
        take(bits, high, result.t_arrival);
        RETURN result;
    END FUNCTION;

    -- TOP_T

    FUNCTION to_slv(top : TOP_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN top.valid & STD_LOGIC_VECTOR(top.book) & top.flags & STD_LOGIC_VECTOR(top.bid_price)
             & STD_LOGIC_VECTOR(top.bid_shares) & STD_LOGIC_VECTOR(top.ask_price) & STD_LOGIC_VECTOR(top.ask_shares)
             & top.record_enable & STD_LOGIC_VECTOR(top.seq) & STD_LOGIC_VECTOR(top.timestamp)
             & STD_LOGIC_VECTOR(top.t_arrival);
    END FUNCTION;

    FUNCTION to_top(vector : STD_LOGIC_VECTOR) RETURN TOP_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high : INTEGER := bits'HIGH;
        VARIABLE top  : TOP_T;
    BEGIN
        take(bits, high, top.valid);
        take(bits, high, top.book);
        take(bits, high, top.flags);
        take(bits, high, top.bid_price);
        take(bits, high, top.bid_shares);
        take(bits, high, top.ask_price);
        take(bits, high, top.ask_shares);
        take(bits, high, top.record_enable);
        take(bits, high, top.seq);
        take(bits, high, top.timestamp);
        take(bits, high, top.t_arrival);
        RETURN top;
    END FUNCTION;

    -- EVENT_T

    FUNCTION to_slv(event : EVENT_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN event.valid & event.flags & STD_LOGIC_VECTOR(event.seq) & STD_LOGIC_VECTOR(event.timestamp);
    END FUNCTION;

    FUNCTION to_event(vector : STD_LOGIC_VECTOR) RETURN EVENT_T IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        VARIABLE high  : INTEGER := bits'HIGH;
        VARIABLE event : EVENT_T;
    BEGIN
        take(bits, high, event.valid);
        take(bits, high, event.flags);
        take(bits, high, event.seq);
        take(bits, high, event.timestamp);
        RETURN event;
    END FUNCTION;

    -- Output record

    FUNCTION byte_reversed(vector : STD_LOGIC_VECTOR) RETURN STD_LOGIC_VECTOR IS
        ALIAS bits : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0) IS vector;
        CONSTANT BYTES : NATURAL := vector'LENGTH / 8;
        VARIABLE result : STD_LOGIC_VECTOR(vector'LENGTH - 1 DOWNTO 0);
    BEGIN
        FOR byte IN 0 TO BYTES - 1 LOOP
            result(8*byte + 7 DOWNTO 8*byte) := bits(8*(BYTES - 1 - byte) + 7 DOWNTO 8*(BYTES - 1 - byte));
        END LOOP;
        RETURN result;
    END FUNCTION;

    FUNCTION pack_record(
        book_id    : UNSIGNED(7 DOWNTO 0);
        flags      : STD_LOGIC_VECTOR(7 DOWNTO 0);
        timestamp  : UNSIGNED(47 DOWNTO 0);
        seq        : UNSIGNED(63 DOWNTO 0);
        bid_price  : UNSIGNED(31 DOWNTO 0);
        bid_shares : UNSIGNED(31 DOWNTO 0);
        ask_price  : UNSIGNED(31 DOWNTO 0);
        ask_shares : UNSIGNED(31 DOWNTO 0)
    ) RETURN STD_LOGIC_VECTOR IS
        -- the record as sent on the wire: byte 0 first, so in the top bits here
        CONSTANT WIRE_ORDER : STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0) :=
            STD_LOGIC_VECTOR(book_id) & flags & STD_LOGIC_VECTOR(timestamp) & STD_LOGIC_VECTOR(seq)
            & STD_LOGIC_VECTOR(bid_price) & STD_LOGIC_VECTOR(bid_shares)
            & STD_LOGIC_VECTOR(ask_price) & STD_LOGIC_VECTOR(ask_shares);
    BEGIN
        RETURN byte_reversed(WIRE_ORDER);
    END FUNCTION;

    FUNCTION pack_record(top : TOP_T) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN pack_record(resize(top.book, 8), top.flags, top.timestamp, top.seq,
                           top.bid_price, top.bid_shares, top.ask_price, top.ask_shares);
    END FUNCTION;

    FUNCTION pack_record(event : EVENT_T) RETURN STD_LOGIC_VECTOR IS
        CONSTANT ZERO : UNSIGNED(31 DOWNTO 0) := (OTHERS => '0');
    BEGIN
        RETURN pack_record(STATUS_BOOK_ID, event.flags, event.timestamp, event.seq, ZERO, ZERO, ZERO, ZERO);
    END FUNCTION;

END PACKAGE BODY;
