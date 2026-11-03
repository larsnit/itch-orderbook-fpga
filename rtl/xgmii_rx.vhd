-- MAC receive path: cuts the frames out of the PCS's 64-bit XGMII stream and puts them out as
-- the AXI4-Stream packet_parser takes (architecture section 5.1).
--
-- A frame starts with the start character in lane 0 or lane 4, followed by 7 preamble/SFD
-- bytes, and ends at the first control character after it: the terminate character for a good
-- frame, anything else (error, start, idle) cuts it. A frame that started in lane 4 is
-- realigned so that its byte 0 is in lane 0. The FCS stays in the frame (the parser ignores
-- what follows the IP packet); the CRC over the whole frame including the FCS must give the
-- CRC-32 residue, else m_tuser = 1 on the last beat.
--
-- Latency: a beat leaves 2 cycles after the word holding its first byte. One cycle is the
-- look-ahead: a frame ending exactly at a word boundary is only known as ended when the next
-- word shows its terminate character in lane 0 (lane 4 for a realigned frame).

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

ENTITY xgmii_rx IS
    PORT (
        clk      : IN  STD_LOGIC;
        rst      : IN  STD_LOGIC;
        rx_mii_d : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);   -- byte i in bits 8i+7 DOWNTO 8i
        rx_mii_c : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);    -- bit i = 1: byte i is a control character
        m_tdata  : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        m_tkeep  : OUT STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid : OUT STD_LOGIC;
        m_tlast  : OUT STD_LOGIC;
        m_tuser  : OUT STD_LOGIC                        -- with m_tlast: 1 = bad CRC or cut frame
    );
END ENTITY;

ARCHITECTURE rtl OF xgmii_rx IS

    CONSTANT START_CHAR     : STD_LOGIC_VECTOR(7 DOWNTO 0)  := x"FB";
    CONSTANT TERMINATE_CHAR : STD_LOGIC_VECTOR(7 DOWNTO 0)  := x"FD";
    CONSTANT CRC_POLY       : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"EDB88320";   -- reflected
    CONSTANT CRC_RESIDUE    : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"DEBB20E3";   -- after frame + FCS

    SUBTYPE CRC_T IS STD_LOGIC_VECTOR(31 DOWNTO 0);

    SIGNAL previous_d : STD_LOGIC_VECTOR(63 DOWNTO 0) := (OTHERS => '0');
    SIGNAL previous_c : STD_LOGIC_VECTOR(7 DOWNTO 0)  := (OTHERS => '1');
    SIGNAL in_frame   : STD_LOGIC := '0';
    SIGNAL realigned  : STD_LOGIC := '0';   -- the frame started in lane 4
    SIGNAL crc        : CRC_T := (OTHERS => '1');

    -- CRC-32 (Ethernet, LSB first) over bytes 0 .. count - 1 of data
    FUNCTION crc_bytes(start : CRC_T; data : STD_LOGIC_VECTOR(63 DOWNTO 0); count : NATURAL) RETURN CRC_T IS
        VARIABLE value    : CRC_T := start;
        VARIABLE feedback : STD_LOGIC;
    BEGIN
        FOR b IN 0 TO count - 1 LOOP
            FOR i IN 0 TO 7 LOOP
                feedback := value(0) XOR data(8*b + i);
                value    := ('0' & value(31 DOWNTO 1)) XOR (CRC_POLY AND (31 DOWNTO 0 => feedback));
            END LOOP;
        END LOOP;
        RETURN value;
    END FUNCTION;

    -- number of data bytes before the first control character (8: none)
    FUNCTION data_lanes(control : STD_LOGIC_VECTOR(7 DOWNTO 0)) RETURN NATURAL IS
        VARIABLE count : NATURAL RANGE 0 TO 8 := 8;
    BEGIN
        FOR lane IN 7 DOWNTO 0 LOOP
            IF control(lane) = '1' THEN
                count := lane;
            END IF;
        END LOOP;
        RETURN count;
    END FUNCTION;

    FUNCTION keep_lanes(count : NATURAL) RETURN STD_LOGIC_VECTOR IS
        VARIABLE keep : STD_LOGIC_VECTOR(7 DOWNTO 0) := (OTHERS => '0');
    BEGIN
        FOR lane IN 0 TO 7 LOOP
            IF lane < count THEN
                keep(lane) := '1';
            END IF;
        END LOOP;
        RETURN keep;
    END FUNCTION;

BEGIN

    PROCESS(clk)
        VARIABLE word_d     : STD_LOGIC_VECTOR(63 DOWNTO 0);   -- the frame's next 8 bytes, lane 0 first
        VARIABLE word_c     : STD_LOGIC_VECTOR(7 DOWNTO 0);
        VARIABLE ahead_c    : STD_LOGIC;                       -- lane 0 of the word after it
        VARIABLE ahead_d    : STD_LOGIC_VECTOR(7 DOWNTO 0);
        VARIABLE count      : NATURAL RANGE 0 TO 8;
        VARIABLE end_char   : STD_LOGIC_VECTOR(7 DOWNTO 0);
        VARIABLE crc_good   : STD_LOGIC_VECTOR(1 TO 8);        -- the residue after k bytes of word_d
    BEGIN
        IF rising_edge(clk) THEN
            IF realigned = '0' THEN
                word_d  := previous_d;
                word_c  := previous_c;
                ahead_c := rx_mii_c(0);
                ahead_d := rx_mii_d(7 DOWNTO 0);
            ELSE
                word_d  := rx_mii_d(31 DOWNTO 0) & previous_d(63 DOWNTO 32);
                word_c  := rx_mii_c(3 DOWNTO 0) & previous_c(7 DOWNTO 4);
                ahead_c := rx_mii_c(4);
                ahead_d := rx_mii_d(39 DOWNTO 32);
            END IF;
            count := data_lanes(word_c);

            FOR k IN 1 TO 8 LOOP
                crc_good(k) := '1' WHEN crc_bytes(crc, word_d, k) = CRC_RESIDUE ELSE '0';
            END LOOP;
            end_char := ahead_d;                                -- the character that ends the frame
            FOR lane IN 7 DOWNTO 0 LOOP
                IF lane >= count THEN
                    end_char := word_d(8*lane + 7 DOWNTO 8*lane);
                END IF;
            END LOOP;

            m_tdata  <= word_d;
            m_tkeep  <= keep_lanes(count);
            m_tvalid <= '0';
            m_tlast  <= '0';
            m_tuser  <= '0';
            IF in_frame = '1' AND count > 0 THEN
                m_tvalid <= '1';
                IF count < 8 OR ahead_c = '1' THEN              -- ends inside this word, or right after it
                    m_tlast <= '1';
                    IF crc_good(count) = '0' OR end_char /= TERMINATE_CHAR THEN
                        m_tuser <= '1';
                    END IF;
                ELSE
                    crc <= crc_bytes(crc, word_d, 8);
                END IF;
            END IF;
            IF count < 8 THEN
                in_frame <= '0';
            END IF;

            IF previous_c(0) = '1' AND previous_d(7 DOWNTO 0) = START_CHAR THEN
                in_frame  <= '1';
                realigned <= '0';
                crc       <= (OTHERS => '1');
            ELSIF previous_c(4) = '1' AND previous_d(39 DOWNTO 32) = START_CHAR THEN
                in_frame  <= '1';
                realigned <= '1';
                crc       <= (OTHERS => '1');
            END IF;

            previous_d <= rx_mii_d;
            previous_c <= rx_mii_c;

            IF rst = '1' THEN
                in_frame   <= '0';
                previous_c <= (OTHERS => '1');
                previous_d <= (OTHERS => '0');
                m_tvalid   <= '0';
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
