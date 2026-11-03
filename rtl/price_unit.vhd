-- Price unit (architecture section 7.4): price -> cents -> position in the book's window and
-- ladder index, in parallel with the order table.
--
--   cycle of the token     read the price: A, F bytes 32-35; U bytes 31-34
--   next cycle             price * 0x51EB851F (DSP, registered); the book from the decoder's
--                          operation
--   third cycle            cents = product >> 37 (= price / 100, exact for every 32-bit price),
--                          and whether price is a whole number of cents
--   fourth cycle           position and index in the window [base, base + W) of that book
--
-- The result is valid 4 cycles after the token: in the order table's update stage of the
-- message's operation. It holds until the next price, so a U_INSERT (one cycle later) sees it too.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;

ENTITY price_unit IS
    PORT (
        clk        : IN  STD_LOGIC;
        cfg_base   : IN  BASE_ARRAY_T;
        token      : IN  TOKEN_T;         -- the decoder's input: the price bytes
        op         : IN  OP_T;            -- the decoder's output: the book
        price_info : OUT PRICE_INFO_T
    );
END ENTITY;

ARCHITECTURE rtl OF price_unit IS

    CONSTANT ONE_HUNDREDTH : UNSIGNED(31 DOWNTO 0) := x"51EB851F";   -- 2^37 / 100, rounded up
    CONSTANT WINDOW        : NATURAL := 2**LADDER_BITS;

    SIGNAL price_read        : UNSIGNED(31 DOWNTO 0);
    SIGNAL priced_read       : STD_LOGIC;
    SIGNAL product           : UNSIGNED(63 DOWNTO 0);
    SIGNAL price_multiplied  : UNSIGNED(31 DOWNTO 0);
    SIGNAL priced_multiplied : STD_LOGIC;
    SIGNAL book_multiplied   : UNSIGNED(2 DOWNTO 0);
    SIGNAL cents             : UNSIGNED(26 DOWNTO 0);   -- 2^32 / 100 < 2^26
    SIGNAL whole_cents       : STD_LOGIC;               -- the price has no sub-penny part
    SIGNAL priced_divided    : STD_LOGIC;
    SIGNAL book_divided      : UNSIGNED(2 DOWNTO 0);

BEGIN

    PROCESS(clk)
        VARIABLE msg_type : MSG_TYPE_T;
        VARIABLE quotient : UNSIGNED(26 DOWNTO 0);
        VARIABLE base     : UNSIGNED(32 DOWNTO 0);
    BEGIN
        IF rising_edge(clk) THEN
            -- cycle of the token: the price field
            msg_type    := token.data(7 DOWNTO 0);
            priced_read <= '0';
            IF token.valid = '1' AND token.kind = TOK_MSG THEN
                IF msg_type = TYPE_ADD OR msg_type = TYPE_ADD_MPID THEN
                    price_read  <= be(token.data, 32, 4);
                    priced_read <= '1';
                ELSIF msg_type = TYPE_REPLACE THEN
                    price_read  <= be(token.data, 31, 4);
                    priced_read <= '1';
                END IF;
            END IF;

            -- next cycle: the multiplication; the decoder's operation now carries the book
            product           <= price_read * ONE_HUNDREDTH;
            price_multiplied  <= price_read;
            priced_multiplied <= priced_read;
            book_multiplied   <= op.book;

            -- third cycle: cents, and whether nothing is left over
            quotient       := product(63 DOWNTO 37);
            cents          <= quotient;
            whole_cents    <= '1' WHEN resize(quotient * 100, 32) = price_multiplied ELSE '0';
            priced_divided <= priced_multiplied;
            book_divided   <= book_multiplied;

            -- fourth cycle: position in the book's window
            IF priced_divided = '1' THEN
                base := resize(cfg_base(to_integer(book_divided)), 33);
                price_info.index <= (OTHERS => '0');
                IF cents < base THEN
                    price_info.position <= POS_BELOW;
                ELSIF cents >= base + WINDOW THEN
                    price_info.position <= POS_ABOVE;
                ELSIF whole_cents = '0' THEN
                    price_info.position <= POS_UNREP;     -- a sub-penny price inside the window
                ELSE
                    price_info.position <= POS_INSIDE;
                    price_info.index    <= resize(cents - base, LADDER_BITS);
                END IF;
            END IF;
        END IF;
    END PROCESS;

END ARCHITECTURE;
