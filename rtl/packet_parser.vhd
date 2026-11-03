-- Packet parser (architecture section 5): checks the Ethernet, IPv4 and UDP headers of each
-- frame from the MAC and passes the UDP payload (the MoldUDP64 packet) of accepted frames on.
--
-- Every check is decided by beat 4 (frame bytes 32-39), before the first payload byte
-- (byte 42, lane 2 of beat 5). From beat 5 on the data passes through as wires; only the
-- control signals come from registers, so the parser adds no latency. Bytes after the IP
-- length (Ethernet padding, an FCS the MAC left in) are not passed on.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

ENTITY packet_parser IS
    PORT (
        clk          : IN  STD_LOGIC;
        rst          : IN  STD_LOGIC;
        cfg_group_ip : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);  -- 233.54.12.111
        cfg_port     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);  -- 26477
        s_tdata      : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);  -- whole frames, first byte in s_tdata(7 DOWNTO 0)
        s_tkeep      : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid     : IN  STD_LOGIC;
        s_tlast      : IN  STD_LOGIC;
        s_tuser      : IN  STD_LOGIC;                      -- '1' = bad FCS, valid with s_tlast
        m_tdata      : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);  -- UDP payload of accepted frames, starting in lane 2
        m_tkeep      : OUT STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid     : OUT STD_LOGIC;
        m_tlast      : OUT STD_LOGIC;
        m_fcs_error  : OUT STD_LOGIC;                      -- an accepted frame had a bad FCS (may come after m_tlast)
        drop_pulse   : OUT STD_LOGIC;                      -- one per dropped frame, at its last beat
        fcs_pulse    : OUT STD_LOGIC                       -- one per bad FCS, accepted or not
    );
END ENTITY;

ARCHITECTURE rtl OF packet_parser IS

    CONSTANT ETHERTYPE_IPV4     : UNSIGNED(15 DOWNTO 0) := x"0800";
    CONSTANT IPV4_NO_OPTIONS    : UNSIGNED(7 DOWNTO 0)  := x"45";   -- version 4, 20-byte header
    CONSTANT PROTOCOL_UDP       : UNSIGNED(7 DOWNTO 0)  := x"11";
    CONSTANT FRAGMENT_BITS      : UNSIGNED(15 DOWNTO 0) := x"3FFF"; -- more-fragments flag and offset
    CONSTANT MIN_UDP_LENGTH     : NATURAL := 8 + 20;                -- UDP header + MoldUDP64 header
    CONSTANT FIRST_PAYLOAD_BEAT : NATURAL := 5;
    CONSTANT FIRST_PAYLOAD_LANE : NATURAL := 2;

    SUBTYPE BEAT_T IS UNSIGNED(13 DOWNTO 0);   -- enough for the largest IP packet (8,194 beats)
    CONSTANT LAST_BEAT_NUMBER : BEAT_T := (OTHERS => '1');

    -- byte in a lane of a beat
    FUNCTION byte_of(data : STD_LOGIC_VECTOR(63 DOWNTO 0); lane : NATURAL) RETURN UNSIGNED IS
    BEGIN
        RETURN UNSIGNED(data(8*lane + 7 DOWNTO 8*lane));
    END FUNCTION;

    -- 16-bit network field (big-endian) starting in a lane
    FUNCTION word_of(data : STD_LOGIC_VECTOR(63 DOWNTO 0); lane : NATURAL) RETURN UNSIGNED IS
    BEGIN
        RETURN byte_of(data, lane) & byte_of(data, lane + 1);
    END FUNCTION;

    -- the four 16-bit words of a beat added up, for the IPv4 header checksum
    FUNCTION sum_of_words(data : STD_LOGIC_VECTOR(63 DOWNTO 0)) RETURN UNSIGNED IS
    BEGIN
        RETURN resize(word_of(data, 0), 18) + word_of(data, 2) + word_of(data, 4) + word_of(data, 6);
    END FUNCTION;

    -- lanes 0 to last set
    FUNCTION lanes_up_to(last : UNSIGNED(2 DOWNTO 0)) RETURN STD_LOGIC_VECTOR IS
        VARIABLE keep : STD_LOGIC_VECTOR(7 DOWNTO 0) := (OTHERS => '0');
    BEGIN
        FOR lane IN 0 TO 7 LOOP
            IF lane <= to_integer(last) THEN
                keep(lane) := '1';
            END IF;
        END LOOP;
        RETURN keep;
    END FUNCTION;

    SIGNAL beat         : BEAT_T;                 -- number of the current beat within its frame
    SIGNAL bad          : STD_LOGIC;              -- a header check of beats 1-3 failed
    SIGNAL accept       : STD_LOGIC;              -- all header checks passed; valid from beat 5
    SIGNAL ip_length    : UNSIGNED(15 DOWNTO 0);
    SIGNAL checksum_sum : UNSIGNED(19 DOWNTO 0);  -- header words added up so far (at most 10 words)
    SIGNAL last_beat    : BEAT_T;                 -- beat holding the last payload byte, byte 13 + ip_length
    SIGNAL last_lane    : UNSIGNED(2 DOWNTO 0);

    SIGNAL check_failed   : STD_LOGIC;            -- a check of the current beat fails
    SIGNAL frame_accepted : STD_LOGIC;            -- the current beat belongs to an accepted frame
    SIGNAL payload_beat   : STD_LOGIC;            -- the current beat holds payload bytes
    SIGNAL payload_ends   : STD_LOGIC;            -- the current beat holds the last payload byte

BEGIN

    header_checks : PROCESS(ALL)
        VARIABLE udp_length : UNSIGNED(15 DOWNTO 0);
        VARIABLE total      : UNSIGNED(19 DOWNTO 0);
        VARIABLE folded     : UNSIGNED(16 DOWNTO 0);
    BEGIN
        check_failed <= '0';
        IF s_tvalid = '1' THEN   -- checks look at valid beats only
            CASE to_integer(beat) IS
                WHEN 1 =>   -- bytes 8-15: EtherType 12-13, version/IHL 14
                    IF word_of(s_tdata, 4) /= ETHERTYPE_IPV4 OR byte_of(s_tdata, 6) /= IPV4_NO_OPTIONS THEN
                        check_failed <= '1';
                    END IF;

                WHEN 2 =>   -- bytes 16-23: flags and fragment offset 20-21, protocol 23
                    IF (word_of(s_tdata, 4) AND FRAGMENT_BITS) /= 0 OR byte_of(s_tdata, 7) /= PROTOCOL_UDP THEN
                        check_failed <= '1';
                    END IF;

                WHEN 3 =>   -- bytes 24-31: destination IP bytes 0-1 at 30-31
                    IF word_of(s_tdata, 6) /= UNSIGNED(cfg_group_ip(31 DOWNTO 16)) THEN
                        check_failed <= '1';
                    END IF;

                WHEN 4 =>   -- bytes 32-39: destination IP bytes 2-3, destination port 36-37, UDP length 38-39
                    udp_length := word_of(s_tdata, 6);

                    -- one's complement sum of the header words 14-33; a valid header folds to FFFF
                    total  := checksum_sum + word_of(s_tdata, 0);
                    folded := resize(total(15 DOWNTO 0), 17) + total(19 DOWNTO 16);
                    folded := resize(folded(15 DOWNTO 0), 17) + folded(16 DOWNTO 16);

                    IF word_of(s_tdata, 0) /= UNSIGNED(cfg_group_ip(15 DOWNTO 0))
                        OR word_of(s_tdata, 4) /= UNSIGNED(cfg_port)
                        OR resize(udp_length, 17) + 20 /= ip_length
                        OR udp_length < MIN_UDP_LENGTH
                        OR folded(15 DOWNTO 0) /= x"FFFF"
                    THEN
                        check_failed <= '1';
                    END IF;

                WHEN OTHERS =>
                    NULL;
            END CASE;
        END IF;
    END PROCESS;

    PROCESS(clk)
        VARIABLE last_byte : UNSIGNED(16 DOWNTO 0);
    BEGIN
        IF rising_edge(clk) THEN
            IF s_tvalid = '1' THEN
                IF s_tlast = '1' THEN
                    beat <= (OTHERS => '0');
                ELSIF beat /= LAST_BEAT_NUMBER THEN
                    beat <= beat + 1;
                END IF;

                CASE to_integer(beat) IS
                    WHEN 0 =>
                        bad    <= '0';
                        accept <= '0';
                    WHEN 1 =>
                        bad          <= check_failed;
                        checksum_sum <= resize(word_of(s_tdata, 6), 20);
                    WHEN 2 =>
                        bad          <= bad OR check_failed;
                        checksum_sum <= checksum_sum + sum_of_words(s_tdata);
                        ip_length    <= word_of(s_tdata, 0);
                        last_byte    := resize(word_of(s_tdata, 0), 17) + 13;
                        last_beat    <= last_byte(16 DOWNTO 3);
                        last_lane    <= last_byte(2 DOWNTO 0);
                    WHEN 3 =>
                        bad          <= bad OR check_failed;
                        checksum_sum <= checksum_sum + sum_of_words(s_tdata);
                    WHEN 4 =>
                        accept <= NOT (bad OR check_failed);
                    WHEN OTHERS =>
                        NULL;
                END CASE;
            END IF;

            IF rst = '1' THEN
                beat   <= (OTHERS => '0');
                accept <= '0';
            END IF;
        END IF;
    END PROCESS;

    -- A frame that ends before its first payload byte (lane 2 of beat 5 empty) is dropped.
    frame_accepted <= accept WHEN beat >= FIRST_PAYLOAD_BEAT
                                  AND NOT (beat = FIRST_PAYLOAD_BEAT AND s_tkeep(FIRST_PAYLOAD_LANE) = '0')
                      ELSE '0';
    payload_beat   <= frame_accepted WHEN beat <= last_beat ELSE '0';
    payload_ends   <= '1' WHEN beat = last_beat ELSE '0';

    output_keep : PROCESS(ALL)
        VARIABLE keep : STD_LOGIC_VECTOR(7 DOWNTO 0);
    BEGIN
        keep := s_tkeep;
        IF beat = FIRST_PAYLOAD_BEAT THEN
            keep := keep AND "11111100";   -- lanes 0-1 hold the UDP checksum
        END IF;
        IF beat = last_beat THEN
            keep := keep AND lanes_up_to(last_lane);
        END IF;
        m_tkeep <= keep;
    END PROCESS;

    m_tdata  <= s_tdata;
    m_tvalid <= s_tvalid AND payload_beat;
    m_tlast  <= s_tvalid AND payload_beat AND (s_tlast OR payload_ends);   -- s_tlast: a frame cut short

    m_fcs_error <= s_tvalid AND s_tlast AND s_tuser AND frame_accepted;
    drop_pulse  <= s_tvalid AND s_tlast AND NOT frame_accepted;
    fcs_pulse   <= s_tvalid AND s_tlast AND s_tuser;

END ARCHITECTURE;
