-- Top of the feed handler: XGMII from the PCS in, records out. xgmii_rx cuts the frames out of
-- the XGMII stream for itch_core; regs_axil holds the configuration and reads the counters;
-- record_tx sends the records back as UDP frames on the transmit side (when switched on).
-- The core is reset by rst or by the register block's soft reset (CONTROL bit 0); the
-- configuration registers and xgmii_rx only by rst.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY itch_top IS
    GENERIC (
        VERSION      : STD_LOGIC_VECTOR(31 DOWNTO 0) := x"20261020";
        TABLE_STYLE  : STRING := "block";    -- ZCU102 (ZU9EG): no URAM; "ultra" on devices with URAM
        LADDER_STYLE : STRING := "block"
    );
    PORT (
        clk           : IN  STD_LOGIC;
        rst           : IN  STD_LOGIC;
        -- AXI4-Lite register block
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
        -- from the PCS (XGMII, 64 bits)
        rx_mii_d      : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        rx_mii_c      : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        -- records
        m_rec_tdata   : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_rec_tvalid  : OUT STD_LOGIC;
        m_evt_tdata   : OUT STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
        m_evt_tvalid  : OUT STD_LOGIC;
        -- debug (ILA): the last beat of every received frame
        rx_frame_end  : OUT STD_LOGIC;
        -- record output to the PCS (XGMII), on the transmit clock
        tx_clk        : IN  STD_LOGIC;
        tx_rst        : IN  STD_LOGIC;
        tx_mii_d      : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);
        tx_mii_c      : OUT STD_LOGIC_VECTOR(7 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF itch_top IS

    SIGNAL soft_reset   : STD_LOGIC;
    SIGNAL core_rst     : STD_LOGIC;
    SIGNAL cfg_group_ip : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL cfg_port     : STD_LOGIC_VECTOR(15 DOWNTO 0);
    SIGNAL cfg_ticker   : TICKER_ARRAY_T;
    SIGNAL cfg_base     : BASE_ARRAY_T;
    SIGNAL counts       : COUNTER_ARRAY_T;
    SIGNAL book_stale   : STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
    SIGNAL stale_all    : STD_LOGIC;
    SIGNAL busy         : STD_LOGIC;
    SIGNAL frame_data   : STD_LOGIC_VECTOR(63 DOWNTO 0);
    SIGNAL frame_keep   : STD_LOGIC_VECTOR(7 DOWNTO 0);
    SIGNAL frame_valid  : STD_LOGIC;
    SIGNAL frame_last   : STD_LOGIC;
    SIGNAL frame_bad    : STD_LOGIC;
    SIGNAL rec_data     : STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
    SIGNAL rec_valid    : STD_LOGIC;
    SIGNAL evt_data     : STD_LOGIC_VECTOR(RECORD_WIDTH - 1 DOWNTO 0);
    SIGNAL evt_valid    : STD_LOGIC;
    SIGNAL tx_enable    : STD_LOGIC;
    SIGNAL tx_dst_mac   : STD_LOGIC_VECTOR(47 DOWNTO 0);
    SIGNAL tx_src_mac   : STD_LOGIC_VECTOR(47 DOWNTO 0);
    SIGNAL tx_dst_ip    : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL tx_src_ip    : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL tx_dst_port  : STD_LOGIC_VECTOR(15 DOWNTO 0);
    SIGNAL tx_src_port  : STD_LOGIC_VECTOR(15 DOWNTO 0);
    SIGNAL dropped      : UNSIGNED(31 DOWNTO 0);

BEGIN

    core_rst     <= rst OR soft_reset;
    rx_frame_end <= frame_valid AND frame_last;

    mac : ENTITY work.xgmii_rx
        PORT MAP (
            clk      => clk,
            rst      => rst,
            rx_mii_d => rx_mii_d,
            rx_mii_c => rx_mii_c,
            m_tdata  => frame_data,
            m_tkeep  => frame_keep,
            m_tvalid => frame_valid,
            m_tlast  => frame_last,
            m_tuser  => frame_bad
        );

    registers : ENTITY work.regs_axil
        GENERIC MAP (VERSION => VERSION)
        PORT MAP (
            clk           => clk,
            rst           => rst,
            s_axi_awaddr  => s_axi_awaddr,
            s_axi_awvalid => s_axi_awvalid,
            s_axi_awready => s_axi_awready,
            s_axi_wdata   => s_axi_wdata,
            s_axi_wstrb   => s_axi_wstrb,
            s_axi_wvalid  => s_axi_wvalid,
            s_axi_wready  => s_axi_wready,
            s_axi_bresp   => s_axi_bresp,
            s_axi_bvalid  => s_axi_bvalid,
            s_axi_bready  => s_axi_bready,
            s_axi_araddr  => s_axi_araddr,
            s_axi_arvalid => s_axi_arvalid,
            s_axi_arready => s_axi_arready,
            s_axi_rdata   => s_axi_rdata,
            s_axi_rresp   => s_axi_rresp,
            s_axi_rvalid  => s_axi_rvalid,
            s_axi_rready  => s_axi_rready,
            soft_reset    => soft_reset,
            cfg_group_ip  => cfg_group_ip,
            cfg_port      => cfg_port,
            cfg_ticker    => cfg_ticker,
            cfg_base      => cfg_base,
            tx_enable     => tx_enable,
            tx_dst_mac    => tx_dst_mac,
            tx_src_mac    => tx_src_mac,
            tx_dst_ip     => tx_dst_ip,
            tx_src_ip     => tx_src_ip,
            tx_dst_port   => tx_dst_port,
            tx_src_port   => tx_src_port,
            records_dropped => dropped,
            counts        => counts,
            book_stale    => book_stale,
            stale_all     => stale_all,
            busy          => busy
        );

    core : ENTITY work.itch_core
        GENERIC MAP (TABLE_STYLE => TABLE_STYLE, LADDER_STYLE => LADDER_STYLE)
        PORT MAP (
            clk          => clk,
            rst          => core_rst,
            cfg_group_ip => cfg_group_ip,
            cfg_port     => cfg_port,
            cfg_ticker   => cfg_ticker,
            cfg_base     => cfg_base,
            s_tdata      => frame_data,
            s_tkeep      => frame_keep,
            s_tvalid     => frame_valid,
            s_tlast      => frame_last,
            s_tuser      => frame_bad,
            m_rec_tdata  => rec_data,
            m_rec_tvalid => rec_valid,
            m_evt_tdata  => evt_data,
            m_evt_tvalid => evt_valid,
            counts       => counts,
            book_stale   => book_stale,
            stale_all    => stale_all,
            busy         => busy
        );

    m_rec_tdata  <= rec_data;
    m_rec_tvalid <= rec_valid;
    m_evt_tdata  <= evt_data;
    m_evt_tvalid <= evt_valid;

    record_output : ENTITY work.record_tx
        PORT MAP (
            clk             => clk,
            rst             => rst,
            rec_data        => rec_data,
            rec_valid       => rec_valid,
            evt_data        => evt_data,
            evt_valid       => evt_valid,
            tx_enable       => tx_enable,
            dst_mac         => tx_dst_mac,
            src_mac         => tx_src_mac,
            dst_ip          => tx_dst_ip,
            src_ip          => tx_src_ip,
            dst_port        => tx_dst_port,
            src_port        => tx_src_port,
            records_dropped => dropped,
            tx_clk          => tx_clk,
            tx_rst          => tx_rst,
            tx_mii_d        => tx_mii_d,
            tx_mii_c        => tx_mii_c
        );

END ARCHITECTURE;
