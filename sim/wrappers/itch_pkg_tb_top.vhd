-- Test wrapper for itch_pkg: every conversion between flat ports, for test_itch_pkg.py.
--   <record>_<field> → <record>_packed      to_slv of a record built from its fields
--   <record>_vector  → <record>_round_trip  to_slv(to_<record>(vector))

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY itch_pkg_tb_top IS
    PORT (
        token_valid             : IN  STD_LOGIC;
        token_kind              : IN  STD_LOGIC_VECTOR(1 DOWNTO 0);
        token_seq               : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        token_len               : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        token_data              : IN  STD_LOGIC_VECTOR(MSG_BYTES*8 - 1 DOWNTO 0);
        token_t_arrival         : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        token_packed            : OUT STD_LOGIC_VECTOR(TOKEN_WIDTH - 1 DOWNTO 0);
        token_vector            : IN  STD_LOGIC_VECTOR(TOKEN_WIDTH - 1 DOWNTO 0);
        token_round_trip        : OUT STD_LOGIC_VECTOR(TOKEN_WIDTH - 1 DOWNTO 0);

        op_valid                : IN  STD_LOGIC;
        op_kind                 : IN  STD_LOGIC_VECTOR(2 DOWNTO 0);
        op_book                 : IN  STD_LOGIC_VECTOR(2 DOWNTO 0);
        op_side                 : IN  STD_LOGIC;
        op_order_ref            : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        op_ref_error            : IN  STD_LOGIC;
        op_bucket0              : IN  STD_LOGIC_VECTOR(BUCKET_BITS - 1 DOWNTO 0);
        op_bucket1              : IN  STD_LOGIC_VECTOR(BUCKET_BITS - 1 DOWNTO 0);
        op_shares               : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        op_seq                  : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        op_timestamp            : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        op_t_arrival            : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        op_stale_all            : IN  STD_LOGIC;
        op_record_enable        : IN  STD_LOGIC;
        op_packed               : OUT STD_LOGIC_VECTOR(OP_WIDTH - 1 DOWNTO 0);
        op_vector               : IN  STD_LOGIC_VECTOR(OP_WIDTH - 1 DOWNTO 0);
        op_round_trip           : OUT STD_LOGIC_VECTOR(OP_WIDTH - 1 DOWNTO 0);

        price_info_position     : IN  STD_LOGIC_VECTOR(1 DOWNTO 0);
        price_info_index        : IN  STD_LOGIC_VECTOR(LADDER_BITS - 1 DOWNTO 0);
        price_info_packed       : OUT STD_LOGIC_VECTOR(PRICE_INFO_WIDTH - 1 DOWNTO 0);
        price_info_vector       : IN  STD_LOGIC_VECTOR(PRICE_INFO_WIDTH - 1 DOWNTO 0);
        price_info_round_trip   : OUT STD_LOGIC_VECTOR(PRICE_INFO_WIDTH - 1 DOWNTO 0);

        entry_valid             : IN  STD_LOGIC;
        entry_order_ref         : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        entry_book              : IN  STD_LOGIC_VECTOR(2 DOWNTO 0);
        entry_side              : IN  STD_LOGIC;
        entry_position          : IN  STD_LOGIC_VECTOR(1 DOWNTO 0);
        entry_index             : IN  STD_LOGIC_VECTOR(LADDER_BITS - 1 DOWNTO 0);
        entry_shares            : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        entry_packed            : OUT STD_LOGIC_VECTOR(ENTRY_WIDTH - 1 DOWNTO 0);
        entry_vector            : IN  STD_LOGIC_VECTOR(ENTRY_WIDTH - 1 DOWNTO 0);
        entry_round_trip        : OUT STD_LOGIC_VECTOR(ENTRY_WIDTH - 1 DOWNTO 0);

        bucket_vector           : IN  STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
        bucket_round_trip       : OUT STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
        bucket_first_slot       : OUT STD_LOGIC_VECTOR(ENTRY_WIDTH - 1 DOWNTO 0);
        bucket_last_slot        : OUT STD_LOGIC_VECTOR(ENTRY_WIDTH - 1 DOWNTO 0);

        ot_result_valid         : IN  STD_LOGIC;
        ot_result_change        : IN  STD_LOGIC_VECTOR(1 DOWNTO 0);
        ot_result_book          : IN  STD_LOGIC_VECTOR(2 DOWNTO 0);
        ot_result_side          : IN  STD_LOGIC;
        ot_result_position      : IN  STD_LOGIC_VECTOR(1 DOWNTO 0);
        ot_result_index         : IN  STD_LOGIC_VECTOR(LADDER_BITS - 1 DOWNTO 0);
        ot_result_shares        : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        ot_result_order_gone    : IN  STD_LOGIC;
        ot_result_book_stale    : IN  STD_LOGIC;
        ot_result_stale_all     : IN  STD_LOGIC;
        ot_result_record_enable : IN  STD_LOGIC;
        ot_result_seq           : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        ot_result_timestamp     : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        ot_result_t_arrival     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        ot_result_packed        : OUT STD_LOGIC_VECTOR(OT_RESULT_WIDTH - 1 DOWNTO 0);
        ot_result_vector        : IN  STD_LOGIC_VECTOR(OT_RESULT_WIDTH - 1 DOWNTO 0);
        ot_result_round_trip    : OUT STD_LOGIC_VECTOR(OT_RESULT_WIDTH - 1 DOWNTO 0);

        top_valid               : IN  STD_LOGIC;
        top_book                : IN  STD_LOGIC_VECTOR(2 DOWNTO 0);
        top_flags               : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        top_bid_price           : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        top_bid_shares          : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        top_ask_price           : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        top_ask_shares          : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        top_record_enable       : IN  STD_LOGIC;
        top_seq                 : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        top_timestamp           : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        top_t_arrival           : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        top_packed              : OUT STD_LOGIC_VECTOR(TOP_WIDTH - 1 DOWNTO 0);
        top_vector              : IN  STD_LOGIC_VECTOR(TOP_WIDTH - 1 DOWNTO 0);
        top_round_trip          : OUT STD_LOGIC_VECTOR(TOP_WIDTH - 1 DOWNTO 0);

        event_valid             : IN  STD_LOGIC;
        event_flags             : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        event_seq               : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        event_timestamp         : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        event_packed            : OUT STD_LOGIC_VECTOR(EVENT_WIDTH - 1 DOWNTO 0);
        event_vector            : IN  STD_LOGIC_VECTOR(EVENT_WIDTH - 1 DOWNTO 0);
        event_round_trip        : OUT STD_LOGIC_VECTOR(EVENT_WIDTH - 1 DOWNTO 0);

        -- pack_record from fields, from a TOP_T (top_vector) and from an EVENT_T (event_vector)
        record_book_id          : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        record_flags            : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        record_timestamp        : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        record_seq              : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        record_bid_price        : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        record_bid_shares       : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        record_ask_price        : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        record_ask_shares       : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        record_bus              : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        top_record_bus          : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        event_record_bus        : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);

        -- be() on the fields of an A message
        message_data            : IN  STD_LOGIC_VECTOR(MSG_BYTES*8 - 1 DOWNTO 0);
        message_locate          : OUT STD_LOGIC_VECTOR(15 DOWNTO 0);
        message_timestamp       : OUT STD_LOGIC_VECTOR(47 DOWNTO 0);
        message_order_ref       : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        message_price           : OUT STD_LOGIC_VECTOR(31 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF itch_pkg_tb_top IS

    SIGNAL bucket : BUCKET_T;

BEGIN

    token_packed <= to_slv(TOKEN_T'(
        valid     => token_valid,
        kind      => token_kind,
        seq       => UNSIGNED(token_seq),
        len       => UNSIGNED(token_len),
        data      => token_data,
        t_arrival => UNSIGNED(token_t_arrival)));
    token_round_trip <= to_slv(to_token(token_vector));

    op_packed <= to_slv(OP_T'(
        valid         => op_valid,
        kind          => op_kind,
        book          => UNSIGNED(op_book),
        side          => op_side,
        order_ref     => UNSIGNED(op_order_ref),
        ref_error     => op_ref_error,
        bucket0       => UNSIGNED(op_bucket0),
        bucket1       => UNSIGNED(op_bucket1),
        shares        => UNSIGNED(op_shares),
        seq           => UNSIGNED(op_seq),
        timestamp     => UNSIGNED(op_timestamp),
        t_arrival     => UNSIGNED(op_t_arrival),
        stale_all     => op_stale_all,
        record_enable => op_record_enable));
    op_round_trip <= to_slv(to_op(op_vector));

    price_info_packed <= to_slv(PRICE_INFO_T'(
        position => price_info_position,
        index    => UNSIGNED(price_info_index)));
    price_info_round_trip <= to_slv(to_price_info(price_info_vector));

    entry_packed <= to_slv(ENTRY_T'(
        valid     => entry_valid,
        order_ref => UNSIGNED(entry_order_ref),
        book      => UNSIGNED(entry_book),
        side      => entry_side,
        position  => entry_position,
        index     => UNSIGNED(entry_index),
        shares    => UNSIGNED(entry_shares)));
    entry_round_trip <= to_slv(to_entry(entry_vector));

    bucket            <= to_bucket(bucket_vector);
    bucket_round_trip <= to_slv(bucket);
    bucket_first_slot <= to_slv(bucket(0));
    bucket_last_slot  <= to_slv(bucket(WAYS - 1));

    ot_result_packed <= to_slv(OT_RESULT_T'(
        valid         => ot_result_valid,
        change        => ot_result_change,
        book          => UNSIGNED(ot_result_book),
        side          => ot_result_side,
        position      => ot_result_position,
        index         => UNSIGNED(ot_result_index),
        shares        => UNSIGNED(ot_result_shares),
        order_gone    => ot_result_order_gone,
        book_stale    => ot_result_book_stale,
        stale_all     => ot_result_stale_all,
        record_enable => ot_result_record_enable,
        seq           => UNSIGNED(ot_result_seq),
        timestamp     => UNSIGNED(ot_result_timestamp),
        t_arrival     => UNSIGNED(ot_result_t_arrival)));
    ot_result_round_trip <= to_slv(to_ot_result(ot_result_vector));

    top_packed <= to_slv(TOP_T'(
        valid         => top_valid,
        book          => UNSIGNED(top_book),
        flags         => top_flags,
        bid_price     => UNSIGNED(top_bid_price),
        bid_shares    => UNSIGNED(top_bid_shares),
        ask_price     => UNSIGNED(top_ask_price),
        ask_shares    => UNSIGNED(top_ask_shares),
        record_enable => top_record_enable,
        seq           => UNSIGNED(top_seq),
        timestamp     => UNSIGNED(top_timestamp),
        t_arrival     => UNSIGNED(top_t_arrival)));
    top_round_trip <= to_slv(to_top(top_vector));

    event_packed <= to_slv(EVENT_T'(
        valid     => event_valid,
        flags     => event_flags,
        seq       => UNSIGNED(event_seq),
        timestamp => UNSIGNED(event_timestamp)));
    event_round_trip <= to_slv(to_event(event_vector));

    record_bus <= pack_record(
        UNSIGNED(record_book_id), record_flags, UNSIGNED(record_timestamp), UNSIGNED(record_seq),
        UNSIGNED(record_bid_price), UNSIGNED(record_bid_shares), UNSIGNED(record_ask_price), UNSIGNED(record_ask_shares));
    top_record_bus   <= pack_record(to_top(top_vector));
    event_record_bus <= pack_record(to_event(event_vector));

    message_locate    <= STD_LOGIC_VECTOR(be(message_data, 1, 2));
    message_timestamp <= STD_LOGIC_VECTOR(be(message_data, 5, 6));
    message_order_ref <= STD_LOGIC_VECTOR(be(message_data, 11, 8));
    message_price     <= STD_LOGIC_VECTOR(be(message_data, 32, 4));

END ARCHITECTURE;
