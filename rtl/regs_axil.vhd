-- Register block (implementation section 7): AXI4-Lite slave, 32-bit registers, 12-bit
-- address, in the core clock domain.
--
--   0x000          CONTROL      W   bit 0: soft reset, a one-cycle pulse to the core (reads 0)
--   0x004          STATUS       R   bit 0 busy, bit 1 stale_all, bits 15:8 per-book stale
--   0x008          VERSION      R   generic VERSION
--   0x010          GROUP_IP     RW  feed multicast group
--   0x014          PORT         RW  feed UDP port, bits 15:0
--   0x018          TX_CONTROL   RW  bit 0: record output on (section 12.8a)
--   0x020/0x024    TX_DST_MAC   RW  record frames' destination MAC: HI bits 15:0 = bytes 0-1,
--                                   LO = bytes 2-5 (first byte in the highest bits)
--   0x028/0x02C    TX_SRC_MAC   RW  their source MAC, the same way
--   0x030          TX_DST_IP    RW
--   0x034          TX_SRC_IP    RW
--   0x038          TX_PORTS     RW  bits 31:16 source port, 15:0 destination port
--   0x0C0          RECORDS_DROPPED R  records the record output had no room for
--   0x040 + 8i     TICKER_HI    RW  ticker i, characters 0-3 (character 0 in bits 31:24)
--   0x044 + 8i     TICKER_LO    RW  ticker i, characters 4-7
--   0x080 + 4i     BASE         RW  window start of book i, cents
--   0x100 + 4i     counter i    R   in register-map order (itch_pkg CNT_*)
--
-- A write to a read-only or unmapped address, and a read of an unmapped address, are answered
-- with SLVERR and change nothing. Byte strobes apply to the RW registers. Configuration
-- registers are reset only by rst, not by the soft reset, and should only be written before
-- the session: a write takes effect at once.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY regs_axil IS
    GENERIC (
        VERSION : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"20261020"
    );
    PORT (
        clk           : IN  STD_LOGIC;
        rst           : IN  STD_LOGIC;
        -- AXI4-Lite slave
        s_axi_awaddr  : IN  STD_LOGIC_VECTOR(11 DOWNTO 0);
        s_axi_awvalid : IN  STD_LOGIC;
        s_axi_awready : OUT STD_LOGIC;
        s_axi_wdata   : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        s_axi_wstrb   : IN  STD_LOGIC_VECTOR(3 DOWNTO 0);
        s_axi_wvalid  : IN  STD_LOGIC;
        s_axi_wready  : OUT STD_LOGIC;
        s_axi_bresp   : OUT STD_LOGIC_VECTOR(1 DOWNTO 0);
        s_axi_bvalid  : OUT STD_LOGIC;
        s_axi_bready  : IN  STD_LOGIC;
        s_axi_araddr  : IN  STD_LOGIC_VECTOR(11 DOWNTO 0);
        s_axi_arvalid : IN  STD_LOGIC;
        s_axi_arready : OUT STD_LOGIC;
        s_axi_rdata   : OUT STD_LOGIC_VECTOR(31 DOWNTO 0);
        s_axi_rresp   : OUT STD_LOGIC_VECTOR(1 DOWNTO 0);
        s_axi_rvalid  : OUT STD_LOGIC;
        s_axi_rready  : IN  STD_LOGIC;
        -- to the core
        soft_reset    : OUT STD_LOGIC;
        cfg_group_ip  : OUT STD_LOGIC_VECTOR(31 DOWNTO 0);
        cfg_port      : OUT STD_LOGIC_VECTOR(15 DOWNTO 0);
        cfg_ticker    : OUT TICKER_ARRAY_T;
        cfg_base      : OUT BASE_ARRAY_T;
        -- to the record output
        tx_enable     : OUT STD_LOGIC;
        tx_dst_mac    : OUT STD_LOGIC_VECTOR(47 DOWNTO 0);
        tx_src_mac    : OUT STD_LOGIC_VECTOR(47 DOWNTO 0);
        tx_dst_ip     : OUT STD_LOGIC_VECTOR(31 DOWNTO 0);
        tx_src_ip     : OUT STD_LOGIC_VECTOR(31 DOWNTO 0);
        tx_dst_port   : OUT STD_LOGIC_VECTOR(15 DOWNTO 0);
        tx_src_port   : OUT STD_LOGIC_VECTOR(15 DOWNTO 0);
        records_dropped : IN UNSIGNED(31 DOWNTO 0);
        -- from the core
        counts        : IN  COUNTER_ARRAY_T;
        book_stale    : IN  STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
        stale_all     : IN  STD_LOGIC;
        busy          : IN  STD_LOGIC
    );
END ENTITY;

ARCHITECTURE rtl OF regs_axil IS

    CONSTANT OKAY   : STD_LOGIC_VECTOR(1 DOWNTO 0) := "00";
    CONSTANT SLVERR : STD_LOGIC_VECTOR(1 DOWNTO 0) := "10";

    CONSTANT ADDR_CONTROL  : NATURAL := 16#000#;
    CONSTANT ADDR_STATUS   : NATURAL := 16#004#;
    CONSTANT ADDR_VERSION  : NATURAL := 16#008#;
    CONSTANT ADDR_GROUP_IP : NATURAL := 16#010#;
    CONSTANT ADDR_PORT     : NATURAL := 16#014#;
    CONSTANT ADDR_TX_CONTROL  : NATURAL := 16#018#;
    CONSTANT ADDR_TX_DST_MAC  : NATURAL := 16#020#;   -- HI, + 4: LO
    CONSTANT ADDR_TX_SRC_MAC  : NATURAL := 16#028#;
    CONSTANT ADDR_TX_DST_IP   : NATURAL := 16#030#;
    CONSTANT ADDR_TX_SRC_IP   : NATURAL := 16#034#;
    CONSTANT ADDR_TX_PORTS    : NATURAL := 16#038#;
    CONSTANT ADDR_RECORDS_DROPPED : NATURAL := 16#0C0#;
    CONSTANT ADDR_TICKERS  : NATURAL := 16#040#;   -- + 8 * book (+ 4 for characters 4-7)
    CONSTANT ADDR_BASES    : NATURAL := 16#080#;   -- + 4 * book
    CONSTANT ADDR_COUNTERS : NATURAL := 16#100#;   -- + 4 * counter

    CONSTANT DEFAULT_GROUP_IP : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"E9360C6F";   -- 233.54.12.111
    CONSTANT DEFAULT_PORT     : STD_LOGIC_VECTOR(15 DOWNTO 0) := x"676D";       -- 26477
    CONSTANT DEFAULT_TICKER   : STD_LOGIC_VECTOR(63 DOWNTO 0) := x"2020202020202020";   -- matches no R
    CONSTANT DEFAULT_BASE     : UNSIGNED(31 DOWNTO 0) := to_unsigned(100, 32);
    CONSTANT DEFAULT_TX_DST_MAC : STD_LOGIC_VECTOR(47 DOWNTO 0) := x"FFFFFFFFFFFF";   -- broadcast
    CONSTANT DEFAULT_TX_SRC_MAC : STD_LOGIC_VECTOR(47 DOWNTO 0) := x"020000000002";   -- locally administered
    CONSTANT DEFAULT_TX_DST_IP  : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"FFFFFFFF";       -- 255.255.255.255
    CONSTANT DEFAULT_TX_SRC_IP  : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"0A000002";       -- 10.0.0.2
    CONSTANT DEFAULT_TX_PORTS   : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"13881388";       -- 5000, 5000

    -- what a register address is
    TYPE REGISTER_T IS (REG_CONTROL, REG_STATUS, REG_VERSION, REG_GROUP_IP, REG_PORT,
                        REG_TX_CONTROL, REG_TX_DST_MAC_HI, REG_TX_DST_MAC_LO, REG_TX_SRC_MAC_HI,
                        REG_TX_SRC_MAC_LO, REG_TX_DST_IP, REG_TX_SRC_IP, REG_TX_PORTS,
                        REG_RECORDS_DROPPED, REG_TICKER_HI, REG_TICKER_LO, REG_BASE, REG_COUNTER, REG_NONE);

    FUNCTION register_at(address : STD_LOGIC_VECTOR(11 DOWNTO 0)) RETURN REGISTER_T IS
        CONSTANT WORD_ADDRESS : NATURAL := to_integer(UNSIGNED(address(11 DOWNTO 2))) * 4;
        CONSTANT IN_TICKERS   : BOOLEAN := WORD_ADDRESS >= ADDR_TICKERS AND WORD_ADDRESS < ADDR_TICKERS + 8 * NUM_BOOKS;
    BEGIN
        IF WORD_ADDRESS = ADDR_CONTROL THEN
            RETURN REG_CONTROL;
        ELSIF WORD_ADDRESS = ADDR_STATUS THEN
            RETURN REG_STATUS;
        ELSIF WORD_ADDRESS = ADDR_VERSION THEN
            RETURN REG_VERSION;
        ELSIF WORD_ADDRESS = ADDR_GROUP_IP THEN
            RETURN REG_GROUP_IP;
        ELSIF WORD_ADDRESS = ADDR_PORT THEN
            RETURN REG_PORT;
        ELSIF WORD_ADDRESS = ADDR_TX_CONTROL THEN
            RETURN REG_TX_CONTROL;
        ELSIF WORD_ADDRESS = ADDR_TX_DST_MAC THEN
            RETURN REG_TX_DST_MAC_HI;
        ELSIF WORD_ADDRESS = ADDR_TX_DST_MAC + 4 THEN
            RETURN REG_TX_DST_MAC_LO;
        ELSIF WORD_ADDRESS = ADDR_TX_SRC_MAC THEN
            RETURN REG_TX_SRC_MAC_HI;
        ELSIF WORD_ADDRESS = ADDR_TX_SRC_MAC + 4 THEN
            RETURN REG_TX_SRC_MAC_LO;
        ELSIF WORD_ADDRESS = ADDR_TX_DST_IP THEN
            RETURN REG_TX_DST_IP;
        ELSIF WORD_ADDRESS = ADDR_TX_SRC_IP THEN
            RETURN REG_TX_SRC_IP;
        ELSIF WORD_ADDRESS = ADDR_TX_PORTS THEN
            RETURN REG_TX_PORTS;
        ELSIF WORD_ADDRESS = ADDR_RECORDS_DROPPED THEN
            RETURN REG_RECORDS_DROPPED;
        ELSIF IN_TICKERS AND (WORD_ADDRESS - ADDR_TICKERS) MOD 8 = 0 THEN
            RETURN REG_TICKER_HI;
        ELSIF IN_TICKERS THEN
            RETURN REG_TICKER_LO;
        ELSIF WORD_ADDRESS >= ADDR_BASES AND WORD_ADDRESS < ADDR_BASES + 4 * NUM_BOOKS THEN
            RETURN REG_BASE;
        ELSIF WORD_ADDRESS >= ADDR_COUNTERS AND WORD_ADDRESS < ADDR_COUNTERS + 4 * NUM_COUNTERS THEN
            RETURN REG_COUNTER;
        ELSE
            RETURN REG_NONE;
        END IF;
    END FUNCTION;

    -- the book (tickers, bases) or counter number of an address
    FUNCTION number_at(address : STD_LOGIC_VECTOR(11 DOWNTO 0); first : NATURAL; stride : NATURAL) RETURN NATURAL IS
        CONSTANT WORD_ADDRESS : NATURAL := to_integer(UNSIGNED(address(11 DOWNTO 2))) * 4;
    BEGIN
        RETURN (WORD_ADDRESS - first) / stride;
    END FUNCTION;

    -- the old register value with the strobed bytes replaced
    FUNCTION strobed(old : STD_LOGIC_VECTOR(31 DOWNTO 0); data : STD_LOGIC_VECTOR(31 DOWNTO 0);
                     strobe : STD_LOGIC_VECTOR(3 DOWNTO 0)) RETURN STD_LOGIC_VECTOR IS
        VARIABLE result : STD_LOGIC_VECTOR(31 DOWNTO 0) := old;
    BEGIN
        FOR byte IN 0 TO 3 LOOP
            IF strobe(byte) = '1' THEN
                result(8*byte + 7 DOWNTO 8*byte) := data(8*byte + 7 DOWNTO 8*byte);
            END IF;
        END LOOP;
        RETURN result;
    END FUNCTION;

    -- write channel: address and data may come in either order; held until both are there
    SIGNAL write_address  : STD_LOGIC_VECTOR(11 DOWNTO 0);
    SIGNAL write_data     : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL write_strobe   : STD_LOGIC_VECTOR(3 DOWNTO 0);
    SIGNAL have_address   : STD_LOGIC;
    SIGNAL have_data      : STD_LOGIC;
    SIGNAL response_valid : STD_LOGIC;
    SIGNAL read_valid     : STD_LOGIC;

    SIGNAL group_ip : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL udp_port : STD_LOGIC_VECTOR(15 DOWNTO 0);
    SIGNAL tickers  : TICKER_ARRAY_T;
    SIGNAL bases    : BASE_ARRAY_T;
    SIGNAL tx_on    : STD_LOGIC;
    SIGNAL dst_mac  : STD_LOGIC_VECTOR(47 DOWNTO 0);
    SIGNAL src_mac  : STD_LOGIC_VECTOR(47 DOWNTO 0);
    SIGNAL dst_ip   : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL src_ip   : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL ports    : STD_LOGIC_VECTOR(31 DOWNTO 0);

BEGIN

    -- a new address or data word is taken only while no write is waiting for its response
    s_axi_awready <= NOT have_address AND NOT response_valid;
    s_axi_wready  <= NOT have_data AND NOT response_valid;
    s_axi_bvalid  <= response_valid;
    s_axi_arready <= NOT read_valid;
    s_axi_rvalid  <= read_valid;

    PROCESS(clk)
        VARIABLE address     : STD_LOGIC_VECTOR(11 DOWNTO 0);
        VARIABLE data        : STD_LOGIC_VECTOR(31 DOWNTO 0);
        VARIABLE strobe      : STD_LOGIC_VECTOR(3 DOWNTO 0);
        VARIABLE got_address : BOOLEAN;
        VARIABLE got_data    : BOOLEAN;
        VARIABLE book        : NATURAL RANGE 0 TO NUM_BOOKS - 1;
        VARIABLE counter     : NATURAL RANGE 0 TO NUM_COUNTERS - 1;
        VARIABLE word        : STD_LOGIC_VECTOR(31 DOWNTO 0);
        VARIABLE status      : STD_LOGIC_VECTOR(31 DOWNTO 0);
    BEGIN
        IF rising_edge(clk) THEN
            soft_reset <= '0';

            -- write: collect address and data, then apply and answer
            address     := write_address;
            data        := write_data;
            strobe      := write_strobe;
            got_address := have_address = '1';
            got_data    := have_data = '1';
            IF s_axi_awvalid = '1' AND have_address = '0' AND response_valid = '0' THEN
                address     := s_axi_awaddr;
                got_address := TRUE;
            END IF;
            IF s_axi_wvalid = '1' AND have_data = '0' AND response_valid = '0' THEN
                data     := s_axi_wdata;
                strobe   := s_axi_wstrb;
                got_data := TRUE;
            END IF;
            write_address <= address;
            write_data    <= data;
            write_strobe  <= strobe;
            have_address  <= '1' WHEN got_address ELSE '0';
            have_data     <= '1' WHEN got_data ELSE '0';

            IF got_address AND got_data THEN
                have_address   <= '0';
                have_data      <= '0';
                response_valid <= '1';
                s_axi_bresp    <= OKAY;
                CASE register_at(address) IS
                    WHEN REG_CONTROL =>
                        soft_reset <= data(0) AND strobe(0);
                    WHEN REG_GROUP_IP =>
                        group_ip <= strobed(group_ip, data, strobe);
                    WHEN REG_PORT =>
                        word     := strobed(x"0000" & udp_port, data, strobe);
                        udp_port <= word(15 DOWNTO 0);
                    WHEN REG_TX_CONTROL =>
                        word  := strobed(x"0000000" & "000" & tx_on, data, strobe);
                        tx_on <= word(0);
                    WHEN REG_TX_DST_MAC_HI =>
                        word    := strobed(x"0000" & dst_mac(47 DOWNTO 32), data, strobe);
                        dst_mac(47 DOWNTO 32) <= word(15 DOWNTO 0);
                    WHEN REG_TX_DST_MAC_LO =>
                        dst_mac(31 DOWNTO 0) <= strobed(dst_mac(31 DOWNTO 0), data, strobe);
                    WHEN REG_TX_SRC_MAC_HI =>
                        word    := strobed(x"0000" & src_mac(47 DOWNTO 32), data, strobe);
                        src_mac(47 DOWNTO 32) <= word(15 DOWNTO 0);
                    WHEN REG_TX_SRC_MAC_LO =>
                        src_mac(31 DOWNTO 0) <= strobed(src_mac(31 DOWNTO 0), data, strobe);
                    WHEN REG_TX_DST_IP =>
                        dst_ip <= strobed(dst_ip, data, strobe);
                    WHEN REG_TX_SRC_IP =>
                        src_ip <= strobed(src_ip, data, strobe);
                    WHEN REG_TX_PORTS =>
                        ports <= strobed(ports, data, strobe);
                    WHEN REG_TICKER_HI =>
                        book := number_at(address, ADDR_TICKERS, 8);
                        tickers(book)(63 DOWNTO 32) <= strobed(tickers(book)(63 DOWNTO 32), data, strobe);
                    WHEN REG_TICKER_LO =>
                        book := number_at(address, ADDR_TICKERS, 8);
                        tickers(book)(31 DOWNTO 0) <= strobed(tickers(book)(31 DOWNTO 0), data, strobe);
                    WHEN REG_BASE =>
                        book := number_at(address, ADDR_BASES, 4);
                        bases(book) <= UNSIGNED(strobed(STD_LOGIC_VECTOR(bases(book)), data, strobe));
                    WHEN OTHERS =>   -- read-only or unmapped
                        s_axi_bresp <= SLVERR;
                END CASE;
            END IF;
            IF response_valid = '1' AND s_axi_bready = '1' THEN
                response_valid <= '0';
            END IF;

            -- read: one cycle after the address
            IF s_axi_arvalid = '1' AND read_valid = '0' THEN
                read_valid  <= '1';
                s_axi_rresp <= OKAY;
                s_axi_rdata <= (OTHERS => '0');
                CASE register_at(s_axi_araddr) IS
                    WHEN REG_CONTROL =>
                        NULL;   -- reads 0
                    WHEN REG_STATUS =>
                        status              := (OTHERS => '0');
                        status(0)           := busy;
                        status(1)           := stale_all;
                        status(15 DOWNTO 8) := book_stale;
                        s_axi_rdata         <= status;
                    WHEN REG_VERSION =>
                        s_axi_rdata <= VERSION;
                    WHEN REG_GROUP_IP =>
                        s_axi_rdata <= group_ip;
                    WHEN REG_PORT =>
                        s_axi_rdata <= x"0000" & udp_port;
                    WHEN REG_TX_CONTROL =>
                        s_axi_rdata <= x"0000000" & "000" & tx_on;
                    WHEN REG_TX_DST_MAC_HI =>
                        s_axi_rdata <= x"0000" & dst_mac(47 DOWNTO 32);
                    WHEN REG_TX_DST_MAC_LO =>
                        s_axi_rdata <= dst_mac(31 DOWNTO 0);
                    WHEN REG_TX_SRC_MAC_HI =>
                        s_axi_rdata <= x"0000" & src_mac(47 DOWNTO 32);
                    WHEN REG_TX_SRC_MAC_LO =>
                        s_axi_rdata <= src_mac(31 DOWNTO 0);
                    WHEN REG_TX_DST_IP =>
                        s_axi_rdata <= dst_ip;
                    WHEN REG_TX_SRC_IP =>
                        s_axi_rdata <= src_ip;
                    WHEN REG_TX_PORTS =>
                        s_axi_rdata <= ports;
                    WHEN REG_RECORDS_DROPPED =>
                        s_axi_rdata <= STD_LOGIC_VECTOR(records_dropped);
                    WHEN REG_TICKER_HI =>
                        s_axi_rdata <= tickers(number_at(s_axi_araddr, ADDR_TICKERS, 8))(63 DOWNTO 32);
                    WHEN REG_TICKER_LO =>
                        s_axi_rdata <= tickers(number_at(s_axi_araddr, ADDR_TICKERS, 8))(31 DOWNTO 0);
                    WHEN REG_BASE =>
                        s_axi_rdata <= STD_LOGIC_VECTOR(bases(number_at(s_axi_araddr, ADDR_BASES, 4)));
                    WHEN REG_COUNTER =>
                        counter     := number_at(s_axi_araddr, ADDR_COUNTERS, 4);
                        s_axi_rdata <= STD_LOGIC_VECTOR(counts(counter));
                    WHEN REG_NONE =>
                        s_axi_rresp <= SLVERR;
                END CASE;
            ELSIF read_valid = '1' AND s_axi_rready = '1' THEN
                read_valid <= '0';
            END IF;

            IF rst = '1' THEN
                have_address   <= '0';
                have_data      <= '0';
                response_valid <= '0';
                read_valid     <= '0';
                soft_reset     <= '0';
                group_ip       <= DEFAULT_GROUP_IP;
                udp_port       <= DEFAULT_PORT;
                tickers        <= (OTHERS => DEFAULT_TICKER);
                bases          <= (OTHERS => DEFAULT_BASE);
                tx_on          <= '0';
                dst_mac        <= DEFAULT_TX_DST_MAC;
                src_mac        <= DEFAULT_TX_SRC_MAC;
                dst_ip         <= DEFAULT_TX_DST_IP;
                src_ip         <= DEFAULT_TX_SRC_IP;
                ports          <= DEFAULT_TX_PORTS;
            END IF;
        END IF;
    END PROCESS;

    cfg_group_ip <= group_ip;
    cfg_port     <= udp_port;
    cfg_ticker   <= tickers;
    cfg_base     <= bases;
    tx_enable    <= tx_on;
    tx_dst_mac   <= dst_mac;
    tx_src_mac   <= src_mac;
    tx_dst_ip    <= dst_ip;
    tx_src_ip    <= src_ip;
    tx_src_port  <= ports(31 DOWNTO 16);
    tx_dst_port  <= ports(15 DOWNTO 0);

END ARCHITECTURE;
