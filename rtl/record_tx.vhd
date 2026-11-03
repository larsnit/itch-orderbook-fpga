-- Record output over Ethernet (implementation section 12.8a): the core's records and status
-- records go back to the PC as UDP frames on the transmit side of the same link. A debug and
-- verification tap behind the core, not part of the latency path.
--
--   core clock:     m_evt, m_rec -> merge (status record first) -> record_fifo write side
--   transmit clock: record_fifo read side -> record_packer -> xgmii_tx -> tx_mii_d/c
--
-- tx_enable (core domain) gates the writes; records are only dropped (and counted) when the
-- FIFO is full. The two resets are crossed into each other's domain, so a reset on either side
-- resets the FIFO on both. records_dropped is cleared only by a reset, not by the soft reset.
-- The address inputs must not change while tx_enable is 1 (they cross without handshake).

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

ENTITY record_tx IS
    GENERIC (
        ADDR_BITS   : POSITIVE := 9;    -- FIFO of 512 records
        MAX_RECORDS : POSITIVE := 32
    );
    PORT (
        clk             : IN  STD_LOGIC;
        rst             : IN  STD_LOGIC;
        rec_data        : IN  STD_LOGIC_VECTOR(255 DOWNTO 0);
        rec_valid       : IN  STD_LOGIC;
        evt_data        : IN  STD_LOGIC_VECTOR(255 DOWNTO 0);
        evt_valid       : IN  STD_LOGIC;
        tx_enable       : IN  STD_LOGIC;
        dst_mac         : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        src_mac         : IN  STD_LOGIC_VECTOR(47 DOWNTO 0);
        dst_ip          : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        src_ip          : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        dst_port        : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        src_port        : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        records_dropped : OUT UNSIGNED(31 DOWNTO 0);
        tx_clk          : IN  STD_LOGIC;
        tx_rst          : IN  STD_LOGIC;
        tx_mii_d        : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        tx_mii_c        : OUT STD_LOGIC_VECTOR(7 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF record_tx IS

    FUNCTION to_gray(value : UNSIGNED(31 DOWNTO 0)) RETURN STD_LOGIC_VECTOR IS
    BEGIN
        RETURN STD_LOGIC_VECTOR(value XOR shift_right(value, 1));
    END FUNCTION;

    FUNCTION from_gray(gray : STD_LOGIC_VECTOR(31 DOWNTO 0)) RETURN UNSIGNED IS
        VARIABLE value : UNSIGNED(31 DOWNTO 0);
    BEGIN
        value(31) := gray(31);
        FOR i IN 30 DOWNTO 0 LOOP
            value(i) := value(i + 1) XOR gray(i);
        END LOOP;
        RETURN value;
    END FUNCTION;

    -- resets, each side also reset by the other's
    SIGNAL core_reset_sync1 : STD_LOGIC := '1';
    SIGNAL core_reset_sync2 : STD_LOGIC := '1';
    SIGNAL tx_reset_sync1   : STD_LOGIC := '1';
    SIGNAL tx_reset_sync2   : STD_LOGIC := '1';
    SIGNAL write_reset      : STD_LOGIC;
    SIGNAL read_reset       : STD_LOGIC;

    -- core side
    SIGNAL held         : STD_LOGIC_VECTOR(255 DOWNTO 0);   -- a record that came with a status record
    SIGNAL held_valid   : STD_LOGIC := '0';
    SIGNAL write_valid  : STD_LOGIC;
    SIGNAL write_data   : STD_LOGIC_VECTOR(255 DOWNTO 0);
    SIGNAL fifo_dropped : STD_LOGIC;
    SIGNAL dropped      : UNSIGNED(31 DOWNTO 0) := (OTHERS => '0');
    SIGNAL dropped_gray : STD_LOGIC_VECTOR(31 DOWNTO 0) := (OTHERS => '0');

    -- transmit side
    SIGNAL enable_sync1       : STD_LOGIC := '0';
    SIGNAL enable_sync2       : STD_LOGIC := '0';
    SIGNAL dropped_gray_sync1 : STD_LOGIC_VECTOR(31 DOWNTO 0) := (OTHERS => '0');
    SIGNAL dropped_gray_sync2 : STD_LOGIC_VECTOR(31 DOWNTO 0) := (OTHERS => '0');
    SIGNAL config_tx_dst_mac  : STD_LOGIC_VECTOR(47 DOWNTO 0);
    SIGNAL config_tx_src_mac  : STD_LOGIC_VECTOR(47 DOWNTO 0);
    SIGNAL config_tx_dst_ip   : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL config_tx_src_ip   : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL config_tx_dst_port : STD_LOGIC_VECTOR(15 DOWNTO 0);
    SIGNAL config_tx_src_port : STD_LOGIC_VECTOR(15 DOWNTO 0);
    SIGNAL dropped_tx         : UNSIGNED(31 DOWNTO 0);
    SIGNAL level              : UNSIGNED(ADDR_BITS DOWNTO 0);
    SIGNAL fifo_read          : STD_LOGIC;
    SIGNAL fifo_data          : STD_LOGIC_VECTOR(255 DOWNTO 0);
    SIGNAL frame_data         : STD_LOGIC_VECTOR(63 DOWNTO 0);
    SIGNAL frame_keep         : STD_LOGIC_VECTOR(7 DOWNTO 0);
    SIGNAL frame_valid        : STD_LOGIC;
    SIGNAL frame_last         : STD_LOGIC;
    SIGNAL frame_ready        : STD_LOGIC;

    ATTRIBUTE async_reg : STRING;
    ATTRIBUTE async_reg OF core_reset_sync1, core_reset_sync2, tx_reset_sync1, tx_reset_sync2,
                           enable_sync1, enable_sync2, dropped_gray_sync1, dropped_gray_sync2 : SIGNAL IS "TRUE";

BEGIN

    -- core side ---------------------------------------------------------------------------

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            tx_reset_sync1 <= tx_rst;
            tx_reset_sync2 <= tx_reset_sync1;
        END IF;
    END PROCESS;
    write_reset <= rst OR tx_reset_sync2;

    -- merge: one write per cycle; a status record goes before a record of the same cycle,
    -- which is held for one cycle (records of different messages are at least 2 cycles apart)
    merge : PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            write_valid <= '0';
            write_data  <= rec_data;
            IF held_valid = '1' THEN
                write_valid <= '1';
                write_data  <= held;
                held_valid  <= '0';
            ELSIF evt_valid = '1' THEN
                write_valid <= '1';
                write_data  <= evt_data;
                held        <= rec_data;
                held_valid  <= rec_valid;
            ELSIF rec_valid = '1' THEN
                write_valid <= '1';
                write_data  <= rec_data;
            END IF;
            IF tx_enable = '0' THEN
                write_valid <= '0';
                held_valid  <= '0';
            END IF;

            IF fifo_dropped = '1' AND dropped /= x"FFFFFFFF" THEN
                dropped      <= dropped + 1;
                dropped_gray <= to_gray(dropped + 1);
            END IF;

            IF write_reset = '1' THEN
                write_valid  <= '0';
                held_valid   <= '0';
                dropped      <= (OTHERS => '0');
                dropped_gray <= (OTHERS => '0');
            END IF;
        END IF;
    END PROCESS;

    records_dropped <= dropped;

    fifo : ENTITY work.record_fifo
        GENERIC MAP (WIDTH => 256, ADDR_BITS => ADDR_BITS)
        PORT MAP (
            w_clk     => clk,
            w_rst     => write_reset,
            w_valid   => write_valid,
            w_data    => write_data,
            w_dropped => fifo_dropped,
            r_clk     => tx_clk,
            r_rst     => read_reset,
            r_level   => level,
            r_read    => fifo_read,
            r_data    => fifo_data
        );

    -- transmit side -----------------------------------------------------------------------

    PROCESS(tx_clk)
    BEGIN
        IF rising_edge(tx_clk) THEN
            core_reset_sync1   <= rst;
            core_reset_sync2   <= core_reset_sync1;
            enable_sync1       <= tx_enable;
            enable_sync2       <= enable_sync1;
            dropped_gray_sync1 <= dropped_gray;
            dropped_gray_sync2 <= dropped_gray_sync1;
            -- quasi-static: only changed while the output is off
            config_tx_dst_mac  <= dst_mac;
            config_tx_src_mac  <= src_mac;
            config_tx_dst_ip   <= dst_ip;
            config_tx_src_ip   <= src_ip;
            config_tx_dst_port <= dst_port;
            config_tx_src_port <= src_port;
        END IF;
    END PROCESS;
    read_reset <= tx_rst OR core_reset_sync2;
    dropped_tx <= from_gray(dropped_gray_sync2);

    packer : ENTITY work.record_packer
        GENERIC MAP (ADDR_BITS => ADDR_BITS, MAX_RECORDS => MAX_RECORDS)
        PORT MAP (
            clk       => tx_clk,
            rst       => read_reset,
            enable    => enable_sync2,
            dst_mac   => config_tx_dst_mac,
            src_mac   => config_tx_src_mac,
            dst_ip    => config_tx_dst_ip,
            src_ip    => config_tx_src_ip,
            dst_port  => config_tx_dst_port,
            src_port  => config_tx_src_port,
            dropped   => dropped_tx,
            level     => level,
            read      => fifo_read,
            record_in => fifo_data,
            m_tdata   => frame_data,
            m_tkeep   => frame_keep,
            m_tvalid  => frame_valid,
            m_tlast   => frame_last,
            m_tready  => frame_ready
        );

    mac : ENTITY work.xgmii_tx
        PORT MAP (
            clk      => tx_clk,
            rst      => read_reset,
            s_tdata  => frame_data,
            s_tkeep  => frame_keep,
            s_tvalid => frame_valid,
            s_tlast  => frame_last,
            s_tready => frame_ready,
            tx_mii_d => tx_mii_d,
            tx_mii_c => tx_mii_c
        );

    -- synthesis translate_off
    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            ASSERT NOT (write_reset = '0' AND tx_enable = '1' AND held_valid = '1' AND (evt_valid = '1' OR rec_valid = '1'))
                REPORT "record_tx: record lost in the merge (records less than 2 cycles apart)" SEVERITY FAILURE;
        END IF;
    END PROCESS;
    -- synthesis translate_on

END ARCHITECTURE;
