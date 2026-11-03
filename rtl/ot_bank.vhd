-- One order-table bank (architecture section 8): a table of 2^BUCKET_BITS rows of WAYS slots,
-- and the write history that forwards rows written after the read (section 8.3).
--
--   read stage     read_bucket goes to the memory
--   before match   the bucket of the same operation one cycle before its match stage is compared
--                  with the write of this cycle and the history; only the hit bits are registered
--   match stage    RAM_LATENCY cycles after the read: row = the memory row, replaced by the
--                  newest of the last RAM_LATENCY writes to the same bucket
--
-- A write in cycle c is in the memory for reads presented in cycle c + 1 or later. An operation
-- in its match stage in cycle m read in cycle m - RAM_LATENCY, so it misses the writes of cycles
-- m - RAM_LATENCY to m - 1: exactly what the history holds then. The write of cycle m itself
-- (the operation one cycle ahead) is the update stage's business: previous_write is that
-- write, one cycle later.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY ot_bank IS
    GENERIC (
        STYLE : STRING := "ultra"   -- "ultra" (URAM) or "block" (BRAM)
    );
    PORT (
        clk                   : IN  STD_LOGIC;
        rst                   : IN  STD_LOGIC;
        read_bucket           : IN  UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);   -- read stage
        next_bucket           : IN  UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);   -- the stage before match
        row                   : OUT BUCKET_T;                             -- match stage, writes forwarded
        write_enable          : IN  STD_LOGIC;
        write_bucket          : IN  UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
        write_row             : IN  BUCKET_T;
        previous_write_valid  : OUT STD_LOGIC;                            -- the write of the cycle before
        previous_write_bucket : OUT UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
        previous_write_row    : OUT BUCKET_T
    );
END ENTITY;

ARCHITECTURE rtl OF ot_bank IS

    TYPE WRITE_T IS RECORD
        valid  : STD_LOGIC;
        bucket : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
        row    : STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
    END RECORD;
    TYPE HISTORY_T IS ARRAY (1 TO RAM_LATENCY) OF WRITE_T;   -- 1: the write of the cycle before

    SIGNAL write_data : STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
    SIGNAL memory_row : STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
    SIGNAL history    : HISTORY_T;
    SIGNAL same       : STD_LOGIC_VECTOR(1 TO RAM_LATENCY);   -- match stage: history(k) is this bucket

BEGIN

    write_data <= to_slv(write_row);

    memory : ENTITY work.ram_sdp
        GENERIC MAP (
            WIDTH => ROW_WIDTH,
            DEPTH => 2**BUCKET_BITS,
            STYLE => STYLE
        )
        PORT MAP (
            clk           => clk,
            read_address  => read_bucket,
            read_data     => memory_row,
            write_enable  => write_enable,
            write_address => write_bucket,
            write_data    => write_data
        );

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            history(1) <= (valid => write_enable, bucket => write_bucket, row => write_data);
            FOR k IN 2 TO RAM_LATENCY LOOP
                history(k) <= history(k - 1);
            END LOOP;

            -- what will be history(k) in the next cycle, compared with the bucket now
            same(1) <= '1' WHEN write_bucket = next_bucket ELSE '0';
            FOR k IN 2 TO RAM_LATENCY LOOP
                same(k) <= '1' WHEN history(k - 1).bucket = next_bucket ELSE '0';
            END LOOP;

            IF rst = '1' THEN
                FOR k IN 1 TO RAM_LATENCY LOOP
                    history(k).valid <= '0';
                END LOOP;
            END IF;
        END IF;
    END PROCESS;

    -- oldest first, so the newest matching write is the one that stays
    PROCESS(ALL)
        VARIABLE data : STD_LOGIC_VECTOR(ROW_WIDTH - 1 DOWNTO 0);
    BEGIN
        data := memory_row;
        FOR k IN RAM_LATENCY DOWNTO 1 LOOP
            IF history(k).valid = '1' AND same(k) = '1' THEN
                data := history(k).row;
            END IF;
        END LOOP;
        row <= to_bucket(data);
    END PROCESS;

    previous_write_valid  <= history(1).valid;
    previous_write_bucket <= history(1).bucket;
    previous_write_row    <= to_bucket(history(1).row);

END ARCHITECTURE;
