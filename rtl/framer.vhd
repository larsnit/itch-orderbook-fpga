-- Framer (architecture section 6): turns the UDP payload of accepted frames (MoldUDP64
-- packets) into tokens, one per message, plus HEARTBEAT and FCS tokens. At most one token
-- per cycle; a message's token is valid the cycle after the beat holding its last byte.
--
-- The payload arrives as the parser passes it on: payload byte p in lane (p + 2) mod 8.
-- Payload beats 0-2 hold the 20-byte MoldUDP64 header; beat 2 also holds the first length
-- prefix in lanes 6-7. From there each beat is walked lane by lane: the rest of the message
-- in progress, a length prefix (possibly split across two beats), the start of the next
-- message. Blocks are at least 14 bytes, so at most one message ends in a beat.
--
-- Two 40-byte assembly buffers are used alternately: one finishes a message while the
-- other starts the next. Message byte j sits in lane (j + start lane) mod 8, so each
-- buffer byte has one 8:1 multiplexer whose select is fixed for the whole message.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY framer IS
    PORT (
        clk             : IN  STD_LOGIC;
        rst             : IN  STD_LOGIC;
        s_tdata         : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);   -- from packet_parser
        s_tkeep         : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid        : IN  STD_LOGIC;
        s_tlast         : IN  STD_LOGIC;
        s_fcs_error     : IN  STD_LOGIC;                       -- the frame had a bad FCS (may come after s_tlast)
        cycle_count     : IN  UNSIGNED(15 DOWNTO 0);           -- free-running, for t_arrival
        token           : OUT TOKEN_T;
        drop_pulse      : OUT STD_LOGIC;                       -- frame cut inside the MoldUDP64 header
        malformed_pulse : OUT STD_LOGIC                        -- one per broken MoldUDP64 packet
    );
END ENTITY;

ARCHITECTURE rtl OF framer IS

    CONSTANT MIN_MESSAGE    : NATURAL := 12;                    -- smallest ITCH message
    CONSTANT END_OF_SESSION : UNSIGNED(15 DOWNTO 0) := x"FFFF";  -- message count: no messages
    CONSTANT HEADER_BEATS   : NATURAL := 3;                     -- payload beats 0-2

    SUBTYPE LANE_T IS NATURAL RANGE 0 TO 8;                     -- 8: past the end of the beat

    TYPE MESSAGE_BUFFER_T IS ARRAY (0 TO MSG_BYTES - 1) OF STD_LOGIC_VECTOR(7 DOWNTO 0);
    TYPE BUFFER_PAIR_T    IS ARRAY (0 TO 1) OF MESSAGE_BUFFER_T;

    FUNCTION byte_of(data : STD_LOGIC_VECTOR(63 DOWNTO 0); lane : NATURAL) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN data(8*lane + 7 DOWNTO 8*lane);
    END FUNCTION;

    -- count bytes from first_lane on, most significant first (network fields are big-endian)
    FUNCTION field_of(data : STD_LOGIC_VECTOR(63 DOWNTO 0); first_lane : NATURAL; count : POSITIVE) RETURN UNSIGNED IS
        VARIABLE result : UNSIGNED(8*count - 1 DOWNTO 0);
    BEGIN
        FOR i IN 0 TO count - 1 LOOP
            result(8*(count - i) - 1 DOWNTO 8*(count - i - 1)) := UNSIGNED(byte_of(data, first_lane + i));
        END LOOP;
        RETURN result;
    END FUNCTION;

    -- number of valid lanes: lanes 0 to (result - 1); only the last beat of a frame is partial
    FUNCTION valid_lanes(keep : STD_LOGIC_VECTOR(7 DOWNTO 0)) RETURN LANE_T IS
    BEGIN
        FOR lane IN 7 DOWNTO 0 LOOP
            IF keep(lane) = '1' THEN
                RETURN lane + 1;
            END IF;
        END LOOP;
        RETURN 0;
    END FUNCTION;

    FUNCTION to_msg_data(message : MESSAGE_BUFFER_T) RETURN MSG_DATA_T IS
        VARIABLE data : MSG_DATA_T;
    BEGIN
        FOR j IN 0 TO MSG_BYTES - 1 LOOP
            data(8*j + 7 DOWNTO 8*j) := message(j);
        END LOOP;
        RETURN data;
    END FUNCTION;

    -- position in the frame
    SIGNAL payload_beat  : NATURAL RANGE 0 TO HEADER_BEATS;    -- 0-2: header beats, 3: blocks
    SIGNAL seq_high      : UNSIGNED(31 DOWNTO 0);              -- sequence number bytes 0-3
    SIGNAL ignore        : STD_LOGIC;                          -- the rest of the frame is ignored
    SIGNAL frame_dropped : STD_LOGIC;                          -- this frame was cut inside the header

    -- walking the blocks
    SIGNAL next_seq      : UNSIGNED(63 DOWNTO 0);              -- sequence number of the next message
    SIGNAL blocks_left   : UNSIGNED(15 DOWNTO 0);
    SIGNAL prefix_split  : STD_LOGIC;                          -- a length prefix started in lane 7
    SIGNAL prefix_high   : STD_LOGIC_VECTOR(7 DOWNTO 0);       -- its first byte

    -- the message being assembled
    SIGNAL in_message     : STD_LOGIC;
    SIGNAL need           : UNSIGNED(15 DOWNTO 0);             -- its bytes still missing
    SIGNAL message_length : UNSIGNED(15 DOWNTO 0);
    SIGNAL start_lane     : LANE_T;                            -- lane of its byte 0 in its first beat (8: the next beat)
    SIGNAL message_beat   : NATURAL RANGE 0 TO 7;              -- beats since its first beat
    SIGNAL active         : NATURAL RANGE 0 TO 1;              -- its buffer
    SIGNAL buffers        : BUFFER_PAIR_T;

    -- the token register
    SIGNAL token_valid      : STD_LOGIC;
    SIGNAL token_kind       : TOKEN_KIND_T;
    SIGNAL token_seq        : UNSIGNED(63 DOWNTO 0);
    SIGNAL token_len        : UNSIGNED(15 DOWNTO 0);
    SIGNAL token_t_arrival  : UNSIGNED(15 DOWNTO 0);
    SIGNAL token_buffer     : NATURAL RANGE 0 TO 1;
    SIGNAL token_is_message : STD_LOGIC;
    SIGNAL token_data       : MSG_DATA_T;

    SIGNAL fcs_delay : STD_LOGIC_VECTOR(1 DOWNTO 0);           -- with the token register: 3 cycles

BEGIN

    PROCESS(clk)
        VARIABLE end_lane         : LANE_T;
        VARIABLE lane             : LANE_T;
        VARIABLE walk             : BOOLEAN;
        VARIABLE dropped          : BOOLEAN;
        VARIABLE malformed        : BOOLEAN;
        VARIABLE have_length      : BOOLEAN;
        VARIABLE length           : UNSIGNED(15 DOWNTO 0);
        VARIABLE count            : UNSIGNED(15 DOWNTO 0);
        VARIABLE new_message      : BOOLEAN;
        VARIABLE new_start        : LANE_T;
        VARIABLE still_in_message : BOOLEAN;
        VARIABLE still_split      : BOOLEAN;
        VARIABLE missing          : UNSIGNED(15 DOWNTO 0);
        VARIABLE blocks           : UNSIGNED(15 DOWNTO 0);
        VARIABLE seq              : UNSIGNED(63 DOWNTO 0);
        VARIABLE fcs_of_frame     : STD_LOGIC;
    BEGIN
        IF rising_edge(clk) THEN
            token_valid     <= '0';
            drop_pulse      <= '0';
            malformed_pulse <= '0';
            dropped         := FALSE;

            IF s_tvalid = '1' THEN
                end_lane         := valid_lanes(s_tkeep);
                lane             := 0;
                walk             := FALSE;
                malformed        := FALSE;
                have_length      := FALSE;
                new_message      := FALSE;
                new_start        := 0;
                still_in_message := in_message = '1';
                still_split      := prefix_split = '1';
                missing          := need;
                blocks           := blocks_left;
                seq              := next_seq;

                CASE payload_beat IS
                    WHEN 0 =>   -- payload bytes 0-5: session (not checked)
                        ignore        <= '0';
                        frame_dropped <= '0';
                        in_message    <= '0';
                        prefix_split  <= '0';
                        dropped       := s_tlast = '1';

                    WHEN 1 =>   -- payload bytes 6-13: sequence number bytes 0-3 in lanes 4-7
                        seq_high <= field_of(s_tdata, 4, 4);
                        dropped  := s_tlast = '1';

                    WHEN 2 =>   -- payload bytes 14-21: sequence number bytes 4-7, count, first length prefix
                        IF end_lane < 6 THEN
                            dropped := TRUE;
                        ELSE
                            seq   := seq_high & field_of(s_tdata, 0, 4);
                            count := field_of(s_tdata, 4, 2);
                            IF count = 0 OR count = END_OF_SESSION THEN
                                token_valid      <= '1';
                                token_kind       <= TOK_HEARTBEAT;
                                token_seq        <= seq;
                                token_len        <= (OTHERS => '0');
                                token_t_arrival  <= cycle_count;
                                token_is_message <= '0';
                                blocks           := (OTHERS => '0');
                            ELSE
                                blocks := count;
                            END IF;
                            still_in_message := FALSE;
                            still_split      := FALSE;
                            lane             := 6;
                            walk             := TRUE;
                        END IF;

                    WHEN OTHERS =>
                        walk := ignore = '0';
                END CASE;

                IF walk THEN
                    -- the end of the message in progress (it continues from lane 0)
                    IF still_in_message THEN
                        IF missing <= 8 THEN
                            IF missing <= end_lane THEN
                                token_valid      <= '1';
                                token_kind       <= TOK_MSG;
                                token_seq        <= seq;
                                token_len        <= message_length;
                                token_t_arrival  <= cycle_count;
                                token_buffer     <= active;
                                token_is_message <= '1';
                                seq              := seq + 1;
                                still_in_message := FALSE;
                                lane             := to_integer(missing);
                            ELSE
                                malformed := TRUE;   -- the frame ends inside the message
                            END IF;
                        ELSIF end_lane = 8 THEN
                            missing := missing - 8;
                            lane    := 8;
                        ELSE
                            malformed := TRUE;
                        END IF;

                    -- the low byte of a length prefix split across beats
                    ELSIF still_split THEN
                        length      := UNSIGNED(prefix_high) & UNSIGNED(byte_of(s_tdata, 0));
                        have_length := TRUE;
                        still_split := FALSE;
                        lane        := 1;
                    END IF;

                    -- the next length prefix
                    IF NOT malformed AND NOT still_in_message AND NOT have_length AND lane < end_lane THEN
                        IF blocks = 0 THEN
                            malformed := TRUE;   -- bytes left after the last block
                        ELSIF lane = 7 THEN
                            prefix_high <= byte_of(s_tdata, 7);
                            still_split := TRUE;
                            lane        := 8;
                        ELSIF lane + 2 <= end_lane THEN
                            length      := field_of(s_tdata, lane, 2);
                            have_length := TRUE;
                            lane        := lane + 2;
                        END IF;   -- else the frame ends inside the prefix: found below
                    END IF;

                    -- a new message
                    IF have_length THEN
                        blocks := blocks - 1;
                        IF length < MIN_MESSAGE THEN
                            malformed := TRUE;
                        ELSE
                            new_message      := TRUE;
                            new_start        := lane;
                            still_in_message := TRUE;
                            missing          := length - (8 - lane);   -- at least 4: it can't end in this beat
                            message_length   <= length;
                        END IF;
                    END IF;

                    -- the frame ends
                    IF s_tlast = '1' AND NOT malformed AND (still_in_message OR still_split OR blocks /= 0) THEN
                        malformed := TRUE;
                    END IF;

                    in_message   <= '1' WHEN still_in_message ELSE '0';
                    prefix_split <= '1' WHEN still_split ELSE '0';
                    need         <= missing;
                    blocks_left  <= blocks;
                    next_seq     <= seq;
                    IF malformed THEN
                        ignore          <= '1';
                        malformed_pulse <= '1';
                    END IF;

                    -- bytes of the message in progress, into its buffer
                    IF in_message = '1' THEN
                        FOR j IN 0 TO MSG_BYTES - 1 LOOP
                            IF (j + start_lane) / 8 = message_beat AND j < message_length THEN
                                buffers(active)(j) <= byte_of(s_tdata, (j + start_lane) MOD 8);
                            END IF;
                        END LOOP;
                        IF message_beat < 7 THEN
                            message_beat <= message_beat + 1;
                        END IF;
                    END IF;

                    -- bytes of a new message, into the other buffer; the rest of it cleared
                    IF new_message THEN
                        FOR j IN 0 TO MSG_BYTES - 1 LOOP
                            IF j + new_start < 8 AND j < length THEN
                                buffers(1 - active)(j) <= byte_of(s_tdata, j + new_start);
                            ELSE
                                buffers(1 - active)(j) <= x"00";
                            END IF;
                        END LOOP;
                        active       <= 1 - active;
                        start_lane   <= new_start;
                        message_beat <= 1;
                    END IF;
                END IF;

                IF dropped THEN
                    ignore        <= '1';
                    frame_dropped <= '1';
                    drop_pulse    <= '1';
                END IF;

                IF s_tlast = '1' THEN
                    payload_beat <= 0;
                ELSIF payload_beat < HEADER_BEATS THEN
                    payload_beat <= payload_beat + 1;
                END IF;
            END IF;

            -- FCS token: 3 cycles after the bad FCS, unless the framer dropped the frame
            fcs_of_frame := s_fcs_error AND NOT frame_dropped;
            IF dropped THEN
                fcs_of_frame := '0';
            END IF;
            fcs_delay <= fcs_delay(0) & fcs_of_frame;
            IF fcs_delay(1) = '1' THEN
                token_valid      <= '1';
                token_kind       <= TOK_FCS;
                token_seq        <= (OTHERS => '0');
                token_len        <= (OTHERS => '0');
                token_t_arrival  <= cycle_count;
                token_is_message <= '0';
            END IF;

            IF rst = '1' THEN
                payload_beat    <= 0;
                ignore          <= '0';
                frame_dropped   <= '0';
                in_message      <= '0';
                prefix_split    <= '0';
                token_valid     <= '0';
                fcs_delay       <= (OTHERS => '0');
                drop_pulse      <= '0';
                malformed_pulse <= '0';
            END IF;
        END IF;
    END PROCESS;

    token_data <= to_msg_data(buffers(token_buffer)) WHEN token_is_message = '1' ELSE (OTHERS => '0');

    token <= (
        valid     => token_valid,
        kind      => token_kind,
        seq       => token_seq,
        len       => token_len,
        data      => token_data,
        t_arrival => token_t_arrival
    );

END ARCHITECTURE;
