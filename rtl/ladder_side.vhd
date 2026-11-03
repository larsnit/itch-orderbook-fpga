-- One book side (architecture section 9.1): the storage the book engine's shared pipeline works
-- on. No pipeline logic here.
--
--   ladder copy 1   shares per level; port A reads the level an operation changes
--   ladder copy 2   the same data; port A reads the best level
--   L0 bitmap       bit = level not empty, 64 levels per word, in LUTRAM; two copies, one read
--                   by the level update, one by the search
--   L1 summary      bit = L0 word not zero, registers
--   search          the best level, from L1 and L0, every cycle (combinational)
--   off-ladder      orders below, above and unrepresentable: one counter each
--   best            index and shares of the best level, or empty, after the last operation

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY ladder_side IS
    GENERIC (
        BID_SIDE : BOOLEAN;           -- the best level is the highest (bids) or the lowest (asks)
        STYLE    : STRING := "block"  -- ladder memories: "block" (BRAM) or "ultra" (URAM)
    );
    PORT (
        clk                 : IN  STD_LOGIC;
        rst                 : IN  STD_LOGIC;
        -- ladder
        level_read_index    : IN  UNSIGNED(LADDER_BITS - 1 DOWNTO 0);   -- copy 1
        level_shares        : OUT UNSIGNED(31 DOWNTO 0);                -- RAM_LATENCY cycles later
        best_read_index     : IN  UNSIGNED(LADDER_BITS - 1 DOWNTO 0);   -- copy 2
        best_level_shares   : OUT UNSIGNED(31 DOWNTO 0);                -- RAM_LATENCY cycles later
        write_enable        : IN  STD_LOGIC;                            -- both copies
        write_index         : IN  UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        write_shares        : IN  UNSIGNED(31 DOWNTO 0);
        -- bitmaps
        update_word_index   : IN  UNSIGNED(LADDER_BITS - 7 DOWNTO 0);
        update_word         : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);        -- same cycle
        search_empty        : OUT STD_LOGIC;                            -- no level set, now
        search_index        : OUT UNSIGNED(LADDER_BITS - 1 DOWNTO 0);   -- the best level, now
        bitmap_write_enable : IN  STD_LOGIC;
        bitmap_write_index  : IN  UNSIGNED(LADDER_BITS - 7 DOWNTO 0);
        bitmap_write_word   : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        -- off-ladder counters
        count_enable        : IN  STD_LOGIC;
        count_position      : IN  POSITION_T;                           -- BELOW, ABOVE or UNREP
        count_up            : IN  STD_LOGIC;                            -- '1': +1, '0': -1
        occupied            : OUT STD_LOGIC_VECTOR(2 DOWNTO 0);         -- counters > 0 with this cycle's change: below, above, unrep
        -- best level
        best_write_enable   : IN  STD_LOGIC;
        best_write_empty    : IN  STD_LOGIC;
        best_write_index    : IN  UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        best_write_shares   : IN  UNSIGNED(31 DOWNTO 0);
        best_empty          : OUT STD_LOGIC;
        best_index          : OUT UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        best_shares         : OUT UNSIGNED(31 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF ladder_side IS

    CONSTANT WORDS      : NATURAL := 2**(LADDER_BITS - 6);
    CONSTANT COUNT_BITS : NATURAL := 18;   -- more than the order table's 131,104 entries

    TYPE COUNTS_T IS ARRAY (0 TO 2) OF UNSIGNED(COUNT_BITS - 1 DOWNTO 0);   -- below, above, unrep
    TYPE POSITIONS_T IS ARRAY (0 TO 2) OF POSITION_T;
    CONSTANT POSITIONS : POSITIONS_T := (POS_BELOW, POS_ABOVE, POS_UNREP);

    SIGNAL level_data        : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL best_data         : STD_LOGIC_VECTOR(31 DOWNTO 0);
    SIGNAL counts            : COUNTS_T;
    SIGNAL next_counts       : COUNTS_T;
    SIGNAL summary_bits      : STD_LOGIC_VECTOR(WORDS - 1 DOWNTO 0);
    SIGNAL search_word_index : UNSIGNED(LADDER_BITS - 7 DOWNTO 0);
    SIGNAL search_word       : STD_LOGIC_VECTOR(63 DOWNTO 0);

BEGIN

    copy1 : ENTITY work.ram_sdp
        GENERIC MAP (WIDTH => 32, DEPTH => 2**LADDER_BITS, STYLE => STYLE)
        PORT MAP (
            clk           => clk,
            read_address  => level_read_index,
            read_data     => level_data,
            write_enable  => write_enable,
            write_address => write_index,
            write_data    => STD_LOGIC_VECTOR(write_shares)
        );

    copy2 : ENTITY work.ram_sdp
        GENERIC MAP (WIDTH => 32, DEPTH => 2**LADDER_BITS, STYLE => STYLE)
        PORT MAP (
            clk           => clk,
            read_address  => best_read_index,
            read_data     => best_data,
            write_enable  => write_enable,
            write_address => write_index,
            write_data    => STD_LOGIC_VECTOR(write_shares)
        );

    level_shares      <= UNSIGNED(level_data);
    best_level_shares <= UNSIGNED(best_data);

    l0_for_update : ENTITY work.lutram
        GENERIC MAP (WIDTH => 64, DEPTH => WORDS)
        PORT MAP (
            clk           => clk,
            read_address  => update_word_index,
            read_data     => update_word,
            write_enable  => bitmap_write_enable,
            write_address => bitmap_write_index,
            write_data    => bitmap_write_word
        );

    l0_for_search : ENTITY work.lutram
        GENERIC MAP (WIDTH => 64, DEPTH => WORDS)
        PORT MAP (
            clk           => clk,
            read_address  => search_word_index,
            read_data     => search_word,
            write_enable  => bitmap_write_enable,
            write_address => bitmap_write_index,
            write_data    => bitmap_write_word
        );

    search : ENTITY work.bitmap_search
        GENERIC MAP (HIGHEST => BID_SIDE)
        PORT MAP (
            summary    => summary_bits,
            word_index => search_word_index,
            word       => search_word,
            empty      => search_empty,
            best_index => search_index
        );

    -- the counters as they are after this cycle's change
    PROCESS(ALL)
        VARIABLE count : UNSIGNED(COUNT_BITS - 1 DOWNTO 0);
    BEGIN
        FOR p IN 0 TO 2 LOOP
            count := counts(p);
            IF count_enable = '1' AND count_position = POSITIONS(p) THEN
                count := counts(p) + 1 WHEN count_up = '1' ELSE counts(p) - 1;
            END IF;
            next_counts(p) <= count;
            occupied(p)    <= '0' WHEN count = 0 ELSE '1';
        END LOOP;
    END PROCESS;

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            counts <= next_counts;
            IF bitmap_write_enable = '1' THEN
                summary_bits(to_integer(bitmap_write_index)) <= OR bitmap_write_word;
            END IF;
            IF best_write_enable = '1' THEN
                best_empty  <= best_write_empty;
                best_index  <= best_write_index;
                best_shares <= best_write_shares;
            END IF;
            IF rst = '1' THEN
                counts       <= (OTHERS => (OTHERS => '0'));
                summary_bits <= (OTHERS => '0');
                best_empty   <= '1';
                best_index   <= (OTHERS => '0');
                best_shares  <= (OTHERS => '0');
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
