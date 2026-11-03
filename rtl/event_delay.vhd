-- Event delay (implementation section 8.7): EVENT_DELAY registers between the decoder and the
-- output stage, so that a status record leaves in the cycle in which the record of the message
-- that revealed it would leave (13 cycles after its last byte).

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY event_delay IS
    PORT (
        clk       : IN  STD_LOGIC;
        rst       : IN  STD_LOGIC;
        event_in  : IN  EVENT_T;
        event_out : OUT EVENT_T
    );
END ENTITY;

ARCHITECTURE rtl OF event_delay IS

    -- the package constant, by its full name: inside this entity, EVENT_DELAY is the entity
    CONSTANT LENGTH : NATURAL := work.itch_pkg.EVENT_DELAY;

    TYPE EVENTS_T IS ARRAY (1 TO LENGTH) OF EVENT_T;
    SIGNAL events : EVENTS_T;

BEGIN

    PROCESS(clk)
    BEGIN
        IF rising_edge(clk) THEN
            events(1) <= event_in;
            FOR k IN 2 TO LENGTH LOOP
                events(k) <= events(k - 1);
            END LOOP;
            IF rst = '1' THEN
                FOR k IN 1 TO LENGTH LOOP
                    events(k).valid <= '0';
                END LOOP;
            END IF;
        END IF;
    END PROCESS;

    event_out <= events(LENGTH);

END ARCHITECTURE;
