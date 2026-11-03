-- Best-level search over one side's bitmaps (architecture section 9.2, appendix D),
-- combinational: L1 summary -> the L0 word to read -> the best bit in that word.
-- Bids take the highest set bit at both levels, asks the lowest. Every book side has its own
-- search, so the direction is fixed per instance.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY bitmap_search IS
    GENERIC (
        HIGHEST : BOOLEAN   -- TRUE: bid side, FALSE: ask side
    );
    PORT (
        summary    : IN  STD_LOGIC_VECTOR(2**(LADDER_BITS - 6) - 1 DOWNTO 0);   -- L1
        word_index : OUT UNSIGNED(LADDER_BITS - 7 DOWNTO 0);    -- the L0 word to read ...
        word       : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);         -- ... and its content, same cycle
        empty      : OUT STD_LOGIC;                             -- no level is set
        best_index : OUT UNSIGNED(LADDER_BITS - 1 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF bitmap_search IS

    -- the highest or the lowest set bit; 0 if none is set
    FUNCTION best_bit(bits : STD_LOGIC_VECTOR) RETURN NATURAL IS
        VARIABLE result : NATURAL := 0;
    BEGIN
        IF HIGHEST THEN
            FOR i IN 0 TO bits'HIGH LOOP        -- upwards: the highest set bit is assigned last
                IF bits(i) = '1' THEN
                    result := i;
                END IF;
            END LOOP;
        ELSE
            FOR i IN bits'HIGH DOWNTO 0 LOOP    -- downwards: the lowest set bit is assigned last
                IF bits(i) = '1' THEN
                    result := i;
                END IF;
            END LOOP;
        END IF;
        RETURN result;
    END FUNCTION;

    SIGNAL word_number : NATURAL RANGE 0 TO 2**(LADDER_BITS - 6) - 1;

BEGIN

    word_number <= best_bit(summary);
    word_index  <= to_unsigned(word_number, LADDER_BITS - 6);
    empty       <= NOT (OR summary);
    best_index  <= to_unsigned(word_number * 64 + best_bit(word), LADDER_BITS);

END ARCHITECTURE;
