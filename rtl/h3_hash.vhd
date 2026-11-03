-- The two H3 bucket hashes of an order ref (architecture section 7.4), combinational.
-- Bucket bit i of table t is the XOR of the ref bits selected by H3_MASK_t(i), over the full
-- 64-bit ref, so refs with high bits set (ref errors) still hash consistently. The masks come
-- from rtl/itch_hash_pkg.vhd, generated from the same Python code the model uses.

LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;
USE work.itch_pkg.ALL;
USE work.itch_hash_pkg.ALL;

ENTITY h3_hash IS
    PORT (
        order_ref : IN  UNSIGNED(63 DOWNTO 0);
        bucket0   : OUT UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);
        bucket1   : OUT UNSIGNED(BUCKET_BITS - 1 DOWNTO 0)
    );
END ENTITY;

ARCHITECTURE rtl OF h3_hash IS
BEGIN

    bits : FOR i IN 0 TO BUCKET_BITS - 1 GENERATE
        bucket0(i) <= XOR (STD_LOGIC_VECTOR(order_ref) AND H3_MASK_0(i));
        bucket1(i) <= XOR (STD_LOGIC_VECTOR(order_ref) AND H3_MASK_1(i));
    END GENERATE;

END ARCHITECTURE;
