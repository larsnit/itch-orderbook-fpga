# ITCH 5.0 Feed Handler with Hardware Order Book

A VHDL-2008 design for the Nasdaq TotalView-ITCH 5.0 feed. It takes the raw 10G Ethernet
stream, maintains the order books of 8 stocks, and sends a record back out every time a
book's best bid or ask changes, 89.6 ns after the message leaves the MAC. It runs on a
Xilinx ZCU102 and was tested there with a full trading day.

## Background

An exchange keeps an **order book** for every stock. It holds all open buy orders (bids) and
sell orders (asks), grouped by price. The best bid and the best ask form the top of the book,
which is where most trading decisions start.

**ITCH 5.0** is Nasdaq's direct market data feed. Each message describes one change to a
book. An order is added (A, F), executed (E, C), partly cancelled (X), deleted (D) or
replaced by a new order (U). The receiver starts with empty books before the open and updates
them with every message. The messages travel in numbered MoldUDP64 packets over UDP
multicast. Our sample day has about 270 million messages, and they come in bursts, so a
receiver has to keep up with its link at line rate. Nasdaq offers 10 and 40 Gb/s connections.

This design handles one 10 Gb/s link end to end, from the XGMII words of the Ethernet frame
to the updated book.

## Design

```
XGMII 64 bit, 156.25 MHz                                       cycles
  xgmii_rx        MAC receive, preamble, CRC                       2
  packet_parser   Ethernet / IPv4 / UDP / MoldUDP64 checks         0
  framer          splits the payload into ITCH messages            1
  decoder         sequence check, stock filter, message -> ops     1
  order_table     order ref -> book, side, price, shares           4
  book_engine     price levels, best bid and ask                   7
  output_stage    32-byte record                                   1
                                         total after the MAC      14 = 89.6 ns
  record_tx       clock crossing, batching into UDP frames, MAC transmit
```

- No backpressure and no stalls. Each message type has a fixed latency of 14 cycles from the
  MAC output to its record, or 15 for a U. Simulation checks this for every record.
- Orders are found by reference number in a two-choice hash table with 2 x 8,192 buckets of
  8 orders and a 32-entry stash.
- Each book side has 8,192 one-cent price levels. A two-level bitmap finds the best level in
  one cycle.
- Sequence gaps, bad frames, unknown orders and overflows mark the affected book as stale in
  every record it sends, and counters record the cause.
- Own MAC (`xgmii_rx`, `xgmii_tx`) on top of the vendor PCS.

| ZCU102 (ZU9EG) | |
| --- | --- |
| Clock | 156.25 MHz, WNS +0.283 ns |
| LUTs | 33,482 (12.2 %) |
| BRAM tiles | 543 (59.5 %) |

More in [`docs/architecture.md`](docs/architecture.md) (what and why) and
[`docs/implementation.md`](docs/implementation.md) (how, including the board setup).

## Verification and results

- **Model.** `itch/hw_model.py` produces exactly the records the hardware must produce, bit
  for bit. It matches a simpler reference model, written straight from the spec, on a full
  real day (Nasdaq's public sample of 30 Dec 2019). There is no official answer key for the
  books, but over the whole day every execute, cancel, delete and replace refers to an order
  the models know.
- **Simulation.** 24 generated test vectors cover cut frames, bad checksums, sequence gaps,
  duplicates, malformed packets, hash collisions and more. Each module has a cocotb testbench
  against the model, plus VHDL assertions for the timing the design relies on. The tests were
  checked with injected bugs that they had to catch.
- **Board, vectors.** Replayed with tcpreplay at full speed. The design sends its records back
  over Ethernet, where they are compared with the model record by record. All 24 match.
- **Board, full day.** 785,103 record frames came back and none were dropped. The records
  matched the model until the case below.

### The bug the full day found

The smallest message that changes a book is a D with 21 bytes including its length field.
On a 64-bit datapath two consecutive messages therefore end at least two words apart, so the
design assumed that operations of different messages are always at least 2 cycles apart. The
order table and the book engine rely on that to handle read-after-write and write-after-write
hazards on the same bucket or price level.

A U breaks this assumption. It replaces an order, so the decoder turns it into two operations,
a delete of the old order and an insert of the new one in the next cycle. If the U ends in
byte 0-2 of a word and a D follows, the D's operation comes only one cycle after the insert.

The full day contains exactly that. A U replaces order 317911 by 318355, and the next message
deletes 318355. The order table checked the delete against the bucket from before the insert
and reported the order as unknown.

The fix delays such an operation by one cycle in the decoder. It costs one cycle in this case
only and can't cascade, because a D that needs the delay ends in byte 5-7, which leaves at
least 3 words to the next book message.

This was a design error. The 2-cycle spacing was written down as a fact in the architecture
and asserted in simulation, but the argument behind it only counted message ends and
overlooked the second operation of a U. The tests could not catch it because they were built
on the same assumption. The decoder testbench spaced its input by it, and the test vectors
are synthetic, one per fault case, so none had a U directly followed by a D for the same
book. The plan called for running a slice of the real day through the simulated core, and
that was never done. It would have found the bug before the board did. A coverage point on
message pairs or a formal check of the spacing would have found it too.

The safety net did work. The board did not publish a wrong book silently. It counted an
unknown order and marked the book as stale in every record after it.

The fix is verified in simulation but not on hardware, since I no longer have access to the
board. I would love to replay the full day on it again.

## Limitations

- No gap recovery. After a lost packet all books stay stale until reset.
- Each book covers a fixed window of +/-$40.96 around a price set before the open.
- 8 books, no stocks under $1.
- Too slow for production trading. About 90 ns from the MAC plus the PCS is far from the tens
  of nanoseconds that competitive systems reach.

## Where the latency goes, and how to cut it

The design takes 14 cycles of 6.4 ns from the MAC to the record. Fast commercial systems run
their logic at 300-600 MHz, keep only the state their strategy needs, and act on the first
bytes of a packet. Following a message through the design shows where the time goes and what
could be cut at 10 Gb/s.

1. **The PCS.** The vendor PCS adds tens of nanoseconds on receive and again on transmit. A
   custom low-latency PCS would remove most of that.
2. **Waiting for the last byte.** The pipeline starts only when a message is complete. The
   order reference is in bytes 11-18, and in an A, E, C, X or U more fields follow, so the
   order table lookup could start up to 2 cycles earlier. A D gains nothing, because the
   reference is its last field. The FCS arrives last, so a message from a frame that later
   turns out corrupt must be undone or flagged.
3. **The clock.** Every pipeline stage costs at least one 6.4 ns cycle. At twice the MAC clock,
   312.5 MHz and synchronous to it, the same 14 cycles take about 45 ns. The deep stages, the
   bitmap search and the order table update, would have to be split first.
4. **Memory reads.** The order table reads its bucket and the book engine reads two price
   levels. Each BRAM read takes 2 cycles, so 6 of the 14 cycles are spent waiting on memory.
   With the top few price levels of each side kept in registers, the new top would be known
   right after the level update, after about 10 cycles instead of 14.
5. **Sending the record.** The first word of a record frame goes out 9 cycles after the
   record is ready. A frame with exactly one record has fixed lengths, so its headers are
   known in advance and could already be on the wire while the record is still in the
   pipeline. Batching several records per frame would no longer be possible.

The bigger step is a **40 Gb/s link**. Every byte arrives four times sooner, so a 21-byte D
takes 4.2 ns on the wire instead of 16.8 ns. The datapath would grow to 128 bits at 312.5 MHz
or 256 bits at 156.25 MHz. Book operations could then come every cycle or even two in one
cycle, so the order table and the book engine would need forwarding between back-to-back
operations. That is a redesign of most of the core, not a tuning step.

## Board setup (not in this repo)

On the ZCU102 `itch_top` sits in a Vivado block design built from AMD IP, which is not part of
this repo.

- Zynq MPSoC, which writes the configuration registers
- 10G/25G Ethernet Subsystem in PCS/PMA-only mode
- AXI SmartConnect
- three Processor System Resets
- ILA

Section 11 of [`docs/implementation.md`](docs/implementation.md) describes how to build it
and how to replay and capture.

## Repository

The simulations run without the board (Python 3.11, cocotb 2.0, Questa or nvc), for example
`SIM=nvc python3.11 tb/core/test_itch_core.py`.

```
rtl/          VHDL-2008 design
sim/          testbench wrappers, simulation-only checkers
tb/           cocotb testbenches
itch/         Python models, ITCH and MoldUDP64 handling, test generator
scripts/      vectors, pcaps, statistics, board configuration, capture comparison, synthesis
tests/        pytest
constraints/  timing constraints
docs/         architecture and implementation
```

## Reflection

This was my first larger FPGA project and my first one aimed at trading. When I got access to
a ZCU102, I wanted to build something for the markets right away. It is fascinating to work
with the same data and message formats that real trading systems run on, and the market gives
hard, immediate feedback that pushes for very good engineering.

The board was only available for a limited time, the project was large, and I had little
experience with 10 Gb/s designs. So I used AI (Claude) for much of the code, the testbenches
and the documentation. The speed at which it writes good RTL is impressive. But it reminded me
that the real work stays with me. I have to understand what each module is for, what its
inputs look like and which requirements it must meet, and then check that the code meets
them. With AI writing the code, that understanding needs more effort up front, not less.
The same goes for verification. A test only checks what I tell it to expect, and in this
project the tests carried the same wrong assumption as the design.

I also enjoyed building a golden model for a problem that has no reference output. In
hindsight I spent too much effort on edge cases and too little on running real data through
the simulation. A slice of the real day would have caught the bug before the board did.

## License

MIT. No Nasdaq data is included.
