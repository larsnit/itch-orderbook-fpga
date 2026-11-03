"""The order table's two hash functions, shared by the model, stats.py and the VHDL package.

H3 hash: output bit i is the parity (XOR) of the key bits selected by mask i. In hardware
each output bit is one XOR tree over about 32 of the 64 key bits: LUTs only, one cycle. A
table with 2**n buckets uses the low n bits. scripts/gen_hash_pkg.py writes the same masks
into rtl/itch_hash_pkg.vhd, so model and hardware always agree.
"""

import random

HASH_BITS = 16
HASH_SEEDS = (0x1D_C0FFEE, 0x2B_ADBEEF)  # fixed: changing them changes the hardware


class H3Hash:
    def __init__(self, seed: int, bits: int = HASH_BITS) -> None:
        rng = random.Random(seed)
        self.masks = [rng.getrandbits(64) for _ in range(bits)]

    def __call__(self, key: int) -> int:
        value = 0
        for bit, mask in enumerate(self.masks):
            value |= ((key & mask).bit_count() & 1) << bit
        return value


HASHES = (H3Hash(HASH_SEEDS[0]), H3Hash(HASH_SEEDS[1]))


def buckets(order_ref: int, bucket_bits: int) -> tuple[int, int]:
    """The ref's bucket in table 0 and in table 1, from the full 64-bit ref."""
    mask = (1 << bucket_bits) - 1
    return HASHES[0](order_ref) & mask, HASHES[1](order_ref) & mask
