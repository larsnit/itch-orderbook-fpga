-- MAC transmit path: puts frames from an AXI4-Stream out as 64-bit XGMII for the PCS, the
-- mirror of xgmii_rx.
--
-- Per frame: a start word (start character in lane 0, 6 x 0x55, 0xD5), the frame bytes, the
-- FCS (CRC-32, appended here), the terminate character, then idle: at least 12 idle bytes
-- before the next start word. The frame comes without FCS and with at least 60 bytes; its
-- bytes are contiguous from lane 0, only the last beat may be partial.
--
-- s_tready is 0 during the start word, the tail word and the gap. Once a frame has started,
-- s_tvalid must stay 1 until s_tlast: XGMII can't pause inside a frame. If it drops, the frame
-- is cut with an error character (the receiver sees a bad frame).

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

ENTITY xgmii_tx IS
    PORT (
        clk      : IN  STD_LOGIC;
        rst      : IN  STD_LOGIC;
        s_tdata  : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep  : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid : IN  STD_LOGIC;
        s_tlast  : IN  STD_LOGIC;
        s_tready : OUT STD_LOGIC;
        tx_mii_d : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);   -- byte i in bits 8i+7 DOWNTO 8i
        tx_mii_c : OUT STD_LOGIC_VECTOR(7 DOWNTO 0)     -- bit i = 1: byte i is a control character
    );
END ENTITY;

ARCHITECTURE rtl OF xgmii_tx IS

    CONSTANT IDLE_CHAR      : STD_LOGIC_VECTOR(7 DOWNTO 0)  := x"07";
    CONSTANT START_CHAR     : STD_LOGIC_VECTOR(7 DOWNTO 0)  := x"FB";
    CONSTANT TERMINATE_CHAR : STD_LOGIC_VECTOR(7 DOWNTO 0)  := x"FD";
    CONSTANT ERROR_CHAR     : STD_LOGIC_VECTOR(7 DOWNTO 0)  := x"FE";
    CONSTANT CRC_POLY       : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"EDB88320";   -- reflected

    CONSTANT IDLE_D  : STD_LOGIC_VECTOR(63 DOWNTO 0) := x"0707070707070707";
    CONSTANT START_D : STD_LOGIC_VECTOR(63 DOWNTO 0) := x"D5555555555555FB";
    CONSTANT ERROR_D : STD_LOGIC_VECTOR(63 DOWNTO 0) := x"07070707070707FE";

    SUBTYPE CRC_T IS STD_LOGIC_VECTOR(31 DOWNTO 0);
    TYPE CRC_ARRAY_T IS ARRAY (1 TO 8) OF CRC_T;
    TYPE STATE_T IS (WAITING, SENDING, TAIL, GAP);

    SIGNAL state     : STATE_T := WAITING;
    SIGNAL crc       : CRC_T := (OTHERS => '1');
    SIGNAL tail_d    : STD_LOGIC_VECTOR(63 DOWNTO 0);
    SIGNAL tail_c    : STD_LOGIC_VECTOR(7 DOWNTO 0);
    SIGNAL tail_long : STD_LOGIC;   -- the terminate is in lane 4 or above: 2 idle words follow
    SIGNAL gap_words : NATURAL RANGE 0 TO 2;

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

    FUNCTION ones(bits : STD_LOGIC_VECTOR(7 DOWNTO 0)) RETURN NATURAL IS
        VARIABLE count : NATURAL RANGE 0 TO 8 := 0;
    BEGIN
        FOR lane IN 0 TO 7 LOOP
            IF bits(lane) = '1' THEN
                count := count + 1;
            END IF;
        END LOOP;
        RETURN count;
    END FUNCTION;

BEGIN

    s_tready <= '1' WHEN state = SENDING ELSE '0';

    PROCESS(clk)
        VARIABLE count : NATURAL RANGE 0 TO 8;
        VARIABLE fcs   : CRC_T;
        VARIABLE fcs_after : CRC_ARRAY_T;   -- the FCS if the last beat has k bytes
        VARIABLE lanes : STD_LOGIC_VECTOR(127 DOWNTO 0);   -- the last data word and the one after it
        VARIABLE flags : STD_LOGIC_VECTOR(15 DOWNTO 0);
    BEGIN
        IF rising_edge(clk) THEN
            tx_mii_d <= IDLE_D;
            tx_mii_c <= x"FF";

            CASE state IS
                WHEN WAITING =>
                    IF s_tvalid = '1' THEN
                        tx_mii_d <= START_D;
                        tx_mii_c <= x"01";
                        crc      <= (OTHERS => '1');
                        state    <= SENDING;
                    END IF;

                WHEN SENDING =>
                    IF s_tvalid = '0' THEN                     -- underrun: cut the frame
                        tx_mii_d  <= ERROR_D;
                        tx_mii_c  <= x"FF";
                        gap_words <= 1;
                        state     <= GAP;
                    ELSIF s_tlast = '0' THEN
                        tx_mii_d <= s_tdata;
                        tx_mii_c <= x"00";
                        crc      <= crc_bytes(crc, s_tdata, 8);
                    ELSE
                        -- data, FCS, terminate, idle over this word and the next
                        -- one CRC per possible byte count (constant loop bounds for synthesis), then select
                        count := ones(s_tkeep);
                        FOR k IN 1 TO 8 LOOP
                            fcs_after(k) := NOT crc_bytes(crc, s_tdata, k);
                        END LOOP;
                        fcs := fcs_after(count);
                        FOR lane IN 0 TO 15 LOOP
                            IF lane < count THEN
                                lanes(8*lane + 7 DOWNTO 8*lane) := s_tdata(8*lane + 7 DOWNTO 8*lane);
                                flags(lane) := '0';
                            ELSIF lane < count + 4 THEN
                                lanes(8*lane + 7 DOWNTO 8*lane) := fcs(8*(lane - count) + 7 DOWNTO 8*(lane - count));
                                flags(lane) := '0';
                            ELSIF lane = count + 4 THEN
                                lanes(8*lane + 7 DOWNTO 8*lane) := TERMINATE_CHAR;
                                flags(lane) := '1';
                            ELSE
                                lanes(8*lane + 7 DOWNTO 8*lane) := IDLE_CHAR;
                                flags(lane) := '1';
                            END IF;
                        END LOOP;
                        tx_mii_d  <= lanes(63 DOWNTO 0);
                        tx_mii_c  <= flags(7 DOWNTO 0);
                        tail_d    <= lanes(127 DOWNTO 64);
                        tail_c    <= flags(15 DOWNTO 8);
                        tail_long <= '1' WHEN count + 4 >= 12 ELSE '0';
                        IF count + 4 <= 7 THEN                 -- terminate in this word
                            gap_words <= 1 WHEN count + 4 <= 3 ELSE 2;
                            state     <= GAP;
                        ELSE
                            state <= TAIL;
                        END IF;
                    END IF;

                WHEN TAIL =>
                    tx_mii_d  <= tail_d;
                    tx_mii_c  <= tail_c;
                    gap_words <= 2 WHEN tail_long = '1' ELSE 1;
                    state     <= GAP;

                WHEN GAP =>
                    IF gap_words <= 1 THEN
                        state <= WAITING;
                    END IF;
                    gap_words <= gap_words - 1 WHEN gap_words > 0 ELSE 0;
            END CASE;

            IF rst = '1' THEN
                state    <= WAITING;
                tx_mii_d <= IDLE_D;
                tx_mii_c <= x"FF";
            END IF;
        END IF;
    END PROCESS;

    -- synthesis translate_off
    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            ASSERT NOT (rst = '0' AND state = SENDING AND s_tvalid = '0')
                REPORT "xgmii_tx: tvalid dropped inside a frame, frame cut" SEVERITY WARNING;
        END IF;
    END PROCESS;
    -- synthesis translate_on

END ARCHITECTURE;
