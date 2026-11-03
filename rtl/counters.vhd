-- The statistics counters (implementation §7): 32 bits, saturating, cleared by reset.
-- Counter i counts pulses(i), index CNT_* from itch_pkg. Dropped frames have three sources
-- whose pulses can coincide (they belong to different frames), so that counter adds them up.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY counters IS
    PORT (
        clk               : IN  STD_LOGIC;
        rst               : IN  STD_LOGIC;
        pulses            : IN  STD_LOGIC_VECTOR(NUM_COUNTERS - 1 DOWNTO 0);  -- CNT_DROPPED_FRAMES: the parser's drops
        framer_drop_pulse : IN  STD_LOGIC;   -- frame cut inside the MoldUDP64 header
        busy_drop_pulse   : IN  STD_LOGIC;   -- frame arrived during the clearing sweep
        counts            : OUT COUNTER_ARRAY_T
    );
END ENTITY;

ARCHITECTURE rtl OF counters IS

    SIGNAL count : COUNTER_ARRAY_T;

BEGIN

    PROCESS(clk)
        VARIABLE increment : NATURAL RANGE 0 TO 3;
    BEGIN
        IF rising_edge(clk) THEN
            FOR counter IN 0 TO NUM_COUNTERS - 1 LOOP
                increment := 0;
                IF pulses(counter) = '1' THEN
                    increment := 1;
                END IF;
                IF counter = CNT_DROPPED_FRAMES THEN
                    IF framer_drop_pulse = '1' THEN
                        increment := increment + 1;
                    END IF;
                    IF busy_drop_pulse = '1' THEN
                        increment := increment + 1;
                    END IF;
                END IF;

                IF count(counter) > COUNTER_MAX - increment THEN
                    count(counter) <= COUNTER_MAX;
                ELSE
                    count(counter) <= count(counter) + increment;
                END IF;
            END LOOP;

            IF rst = '1' THEN
                count <= (OTHERS => (OTHERS => '0'));
            END IF;
        END IF;
    END PROCESS;

    counts <= count;

END ARCHITECTURE;
