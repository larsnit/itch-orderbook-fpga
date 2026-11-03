-- Packs records from the record FIFO into UDP frames for xgmii_tx (implementation section 12.8a).
-- Transmit clock domain.
--
-- A frame carries the records waiting when it starts, at most MAX_RECORDS (n):
--
--   bytes  0-13   Ethernet: destination MAC, source MAC, 0x0800
--   bytes 14-33   IPv4: 0x45, 0, total length 42 + 32n, id = frame counter, 0x4000 (DF),
--                 TTL 64, UDP, header checksum, source IP, destination IP
--   bytes 34-41   UDP: source port, destination port, length 22 + 32n, checksum 0
--   bytes 42-55   batch header: 0x4952 ("IR"), frame counter (32 bits), n (16),
--                 records dropped so far (32), 0x0000
--   bytes 56-     n records of 32 bytes, record byte j = bus bits 8j+7 DOWNTO 8j
--
-- 56 header bytes are 7 beats, so every record is 4 whole beats. Multi-byte fields are
-- big-endian. The configuration inputs must be stable while enable is 1.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

ENTITY record_packer IS
    GENERIC (
        ADDR_BITS   : POSITIVE := 9;
        MAX_RECORDS : POSITIVE := 32
    );
    PORT (
        clk       : IN  STD_LOGIC;
        rst       : IN  STD_LOGIC;
        enable    : IN  STD_LOGIC;
        dst_mac   : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);   -- first byte on the wire in bits 47:40
        src_mac   : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        dst_ip    : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        src_ip    : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        dst_port  : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        src_port  : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        dropped   : IN  UNSIGNED(31 DOWNTO 0);
        -- record FIFO, read side
        level     : IN  UNSIGNED(ADDR_BITS DOWNTO 0);
        read      : OUT STD_LOGIC;
        record_in : IN  STD_LOGIC_VECTOR(255 DOWNTO 0);   -- one cycle after read
        -- to xgmii_tx
        m_tdata   : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        m_tkeep   : OUT STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid  : OUT STD_LOGIC;
        m_tlast   : OUT STD_LOGIC;
        m_tready  : IN  STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF record_packer IS

    CONSTANT HEADER_BEATS : POSITIVE := 7;
    CONSTANT MAGIC        : STD_LOGIC_VECTOR(15 DOWNTO 0) := x"4952";

    TYPE STATE_T IS (WAITING, PREPARING, HEADER, RECORDS);

    SIGNAL state         : STATE_T := WAITING;
    SIGNAL count         : NATURAL RANGE 0 TO MAX_RECORDS;   -- records in this frame
    SIGNAL left          : NATURAL RANGE 0 TO MAX_RECORDS;   -- records not yet started
    SIGNAL beat          : NATURAL RANGE 0 TO HEADER_BEATS - 1;
    SIGNAL frame_counter : UNSIGNED(31 DOWNTO 0) := (OTHERS => '0');
    SIGNAL header_bytes  : STD_LOGIC_VECTOR(8 * 8 * HEADER_BEATS - 1 DOWNTO 0);   -- byte i in bits 8i+7..8i
    SIGNAL current       : STD_LOGIC_VECTOR(255 DOWNTO 0);   -- the record being sent
    SIGNAL waiting_next  : STD_LOGIC_VECTOR(255 DOWNTO 0);   -- the next one, from the FIFO
    SIGNAL fetched       : STD_LOGIC;                        -- read was 1 last cycle
    SIGNAL fetch         : STD_LOGIC;

    -- a big-endian field as frame bytes: byte 0 of the field in bits 7:0
    FUNCTION wire(field : STD_LOGIC_VECTOR) RETURN STD_LOGIC_VECTOR IS
        CONSTANT BYTES : NATURAL := field'LENGTH / 8;
        VARIABLE value : STD_LOGIC_VECTOR(field'LENGTH - 1 DOWNTO 0) := field;
        VARIABLE bytes_out : STD_LOGIC_VECTOR(field'LENGTH - 1 DOWNTO 0);
    BEGIN
        FOR i IN 0 TO BYTES - 1 LOOP
            bytes_out(8*i + 7 DOWNTO 8*i) := value(field'LENGTH - 8*i - 1 DOWNTO field'LENGTH - 8*i - 8);
        END LOOP;
        RETURN bytes_out;
    END FUNCTION;

    FUNCTION ip_checksum(total_length, id : UNSIGNED(15 DOWNTO 0);
                         src, dst : STD_LOGIC_VECTOR(31 DOWNTO 0)) RETURN STD_LOGIC_VECTOR IS
        VARIABLE sum : UNSIGNED(19 DOWNTO 0);
    BEGIN
        sum := to_unsigned(16#4500# + 16#4000# + 16#4011#, 20) + total_length + id
             + UNSIGNED(src(31 DOWNTO 16)) + UNSIGNED(src(15 DOWNTO 0))
             + UNSIGNED(dst(31 DOWNTO 16)) + UNSIGNED(dst(15 DOWNTO 0));
        sum := resize(sum(15 DOWNTO 0), 20) + sum(19 DOWNTO 16);
        sum := resize(sum(15 DOWNTO 0), 20) + sum(19 DOWNTO 16);
        RETURN NOT STD_LOGIC_VECTOR(sum(15 DOWNTO 0));
    END FUNCTION;

BEGIN

    read <= fetch;

    PROCESS(clk)
        VARIABLE n            : NATURAL RANGE 0 TO MAX_RECORDS;
        VARIABLE ip_length    : UNSIGNED(15 DOWNTO 0);
        VARIABLE udp_length   : UNSIGNED(15 DOWNTO 0);
        VARIABLE id           : UNSIGNED(15 DOWNTO 0);
        VARIABLE accepted     : BOOLEAN;
        VARIABLE record_beat  : NATURAL RANGE 0 TO 3;
    BEGIN
        IF rising_edge(clk) THEN
            fetch   <= '0';
            fetched <= fetch;
            IF fetched = '1' THEN
                waiting_next <= record_in;
            END IF;
            accepted := m_tvalid = '1' AND m_tready = '1';

            CASE state IS
                WHEN WAITING =>
                    m_tvalid <= '0';
                    IF enable = '1' AND level > 0 THEN
                        n     := MAX_RECORDS WHEN level >= MAX_RECORDS ELSE to_integer(level);
                        count <= n;
                        left  <= n;
                        fetch <= '1';                              -- the first record
                        state <= PREPARING;
                    END IF;

                WHEN PREPARING =>
                    ip_length  := to_unsigned(42 + 32 * count, 16);
                    udp_length := to_unsigned(22 + 32 * count, 16);
                    id         := frame_counter(15 DOWNTO 0);
                    header_bytes <= wire(MAGIC & STD_LOGIC_VECTOR(frame_counter) & STD_LOGIC_VECTOR(to_unsigned(count, 16))
                                         & STD_LOGIC_VECTOR(dropped) & x"0000")
                                  & wire(src_port & dst_port & STD_LOGIC_VECTOR(udp_length) & x"0000")
                                  & wire(x"4500" & STD_LOGIC_VECTOR(ip_length) & STD_LOGIC_VECTOR(id) & x"4000" & x"4011"
                                         & ip_checksum(ip_length, id, src_ip, dst_ip) & src_ip & dst_ip)
                                  & wire(dst_mac & src_mac & x"0800");
                    beat  <= 0;
                    state <= HEADER;

                WHEN HEADER =>
                    m_tvalid <= '1';
                    m_tkeep  <= x"FF";
                    m_tlast  <= '0';
                    m_tdata  <= header_bytes(64*beat + 63 DOWNTO 64*beat);
                    IF accepted THEN
                        IF beat = HEADER_BEATS - 1 THEN
                            -- the header's last beat is taken: the first record follows
                            current <= waiting_next;
                            left    <= left - 1;
                            IF left > 1 THEN
                                fetch <= '1';
                            END IF;
                            record_beat := 0;
                            m_tdata  <= waiting_next(63 DOWNTO 0);
                            m_tlast  <= '0';
                            beat     <= 0;
                            state    <= RECORDS;
                        ELSE
                            m_tdata <= header_bytes(64*(beat + 1) + 63 DOWNTO 64*(beat + 1));
                            beat    <= beat + 1;
                        END IF;
                    END IF;

                WHEN RECORDS =>
                    IF accepted THEN
                        record_beat := beat MOD 4;
                        IF record_beat < 3 THEN
                            m_tdata <= current(64*(record_beat + 1) + 63 DOWNTO 64*(record_beat + 1));
                            m_tlast <= '1' WHEN record_beat = 2 AND left = 0 ELSE '0';
                            beat    <= beat + 1;
                        ELSIF left > 0 THEN                       -- next record
                            current <= waiting_next;
                            m_tdata <= waiting_next(63 DOWNTO 0);
                            m_tlast <= '0';
                            left    <= left - 1;
                            IF left > 1 THEN
                                fetch <= '1';
                            END IF;
                            beat <= 0;
                        ELSE                                      -- the frame's last beat is taken
                            m_tvalid      <= '0';
                            m_tlast       <= '0';
                            frame_counter <= frame_counter + 1;
                            state         <= WAITING;
                        END IF;
                    END IF;
            END CASE;

            IF rst = '1' THEN
                state         <= WAITING;
                m_tvalid      <= '0';
                m_tlast       <= '0';
                fetch         <= '0';
                fetched       <= '0';
                frame_counter <= (OTHERS => '0');
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
