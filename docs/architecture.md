# Architecture: ITCH Feed Handler with Hardware Order Book

2026-10-15 · Lars Nitschke

This document is the plan for the RTL. It says what every stage does, cycle by cycle, which
memory it uses, where one message can disturb the next one and how that is handled, and how
each part is verified. The reference model (`itch/`) and this document must agree; where the
model and the hardware differ from the ideal books, section 16 describes the hardware model.

**How to read it.** Sections 1–3 are the big picture and the facts the design rests on;
read them first. Sections 4–9 describe the stages in pipeline order; read the one you are
building. Sections 10–17 cover the cross-cutting parts: hazards, memory, latency, errors,
configuration, verification, model changes, limitations. Section 18 is a glossary, and the
appendix walks through the key mechanisms with concrete numbers.

**How to build it:** `docs/implementation.md` turns this plan into modules, interfaces,
coding rules, a testbench structure and a build order. Read this document first.

---

## 1. What it does

The design receives the Nasdaq TotalView-ITCH 5.0 feed (MoldUDP64 inside Ethernet/IPv4/UDP)
from a 10G PCS as XGMII (our own MAC receive path `xgmii_rx` cuts out the frames), keeps price-level order books for 8 configured stocks, and outputs a 32-byte
record whenever a book's top of book (best bid and ask, price and shares) or its flags change.

| Requirement | Value | How the design meets it |
| --- | --- | --- |
| Throughput | 10 Gb/s, no backpressure, no message lost | One 64-bit beat per cycle at 156.25 MHz; every stage accepts one input per cycle and never stalls (§3) |
| Latency | 80–100 ns, fixed | 14 cycles (90 ns) for A/F/E/C/X/D, 15 cycles (96 ns) for U (§12) |
| Correctness | bit-exact against the reference model | Every rule, including faults and overflows, is defined here (§13) and in the model (§16) |
| Never silently wrong | stale and window flags | Every case where a book may be wrong sets a flag in its records (§13) |

The latency is measured from the cycle in which the beat holding a message's last byte is on
the MAC output (the output of `xgmii_rx`) to the cycle in which that message's record leaves
the core. `xgmii_rx` adds 2 cycles in front: 16 cycles (102 ns) from the XGMII word, U 17.

## 2. Design principles

1. **One clock, 156.25 MHz, the MAC's receive clock.** No clock crossings in the core.
2. **No stalls, no backpressure.** Data that arrives is processed in a fixed number of
   cycles. Nothing waits for anything.
3. **Fixed latency per message type.** No caches, no retries, no queues in the message path.
   One exception, one cycle: a D right after a U (§3).
4. **Decide early, stream the rest.** Drop decisions are made from headers before any
   payload is passed on. Faults that are only visible later (cut frames, bad FCS) are
   flagged, never "undone".
5. **Never silently wrong.** If a book can't be guaranteed correct, its records say so.
6. **Shallow stages.** Each stage has a few LUT levels, so the core can later move to a
   2× clock (§17) without being restructured.

## 3. Facts the design relies on

These are the guarantees that make "no stalls, no queues" possible. Each one has a short name
that the rest of the documents, the code comments and the test names use, and each is checked
by an assertion in simulation (§15).

**One beat per cycle.** 64 bits × 156.25 MHz = 10 Gb/s exactly. The MAC has no
`tready`; it delivers a beat whenever it has one.

**Frames start in lane 0.** In AXI4-Stream, `tlast` ends a packet and the next frame
starts in a new transfer. The Xilinx 10G/25G MAC (PG210) keeps all bytes valid except in the
final transfer.

**Drops are decided before the payload.** Every header check uses bytes 0–39
(beats 0–4). The MoldUDP64 payload starts at byte 42 (beat 5), the first message at byte
64 (beat 8).

**One message end per beat.** The framer rejects blocks shorter than 14
bytes (2-byte length + the smallest ITCH message, 12 bytes). A beat holds 8 bytes, so after
one message ends, the next can't end in the same beat.

**Book-message spacing: a book message completes at least 2 cycles after the previous
message.** The
smallest book message is D: 19 bytes, a 21-byte block. If the previous message ends in lane
`e` of beat `t`, the next 21 bytes end in beat `t + (e + 21) / 8 ≥ t + 2`. This needs the
decoder's length check (§6): a message whose length doesn't match its type is never
treated as a book message. A U (37-byte block) completes at least 4 cycles after the
previous message.

**One book operation per cycle, operations of different messages at least 2 cycles apart.**
Every book message is one operation, except U, which becomes two (delete old, insert new) in
consecutive cycles `t` and `t+1`. The spacing above is between message *ends*, so it does not
cover the U's second half: a D right after a U that ends in lane 0–2 completes 2 beats after
the U, and its operation would come at `t+2`, 1 cycle after U_INSERT. (The first version
assumed `t+2` was always far enough; the whole-day replay on the board found the case, seq
546545: the D missed the order U_INSERT had just written.) So the decoder holds such an
operation one cycle (§7, implementation §8.2). That can't cascade: the held D ends in lane
5–7, so the next book message ends at least 3 beats later, 2 cycles after the held D.
Cost: this D's record leaves 1 cycle later (15 instead of 14).

**One record per cycle.** Only one operation per message produces a record (for U, the
second). Because there is only one book operation per cycle, the records of two messages never
leave in the same cycle.

Consequences used everywhere below: the pipelines after the decoder see at most one operation
per cycle, and two operations from different messages are at least 2 cycles apart (enforced
by the decoder, asserted at its output); only the two halves of a U are 1 cycle apart.

## 4. Overview

`itch_top` = `xgmii_rx` (MAC receive, 2 cycles: XGMII from the PCS → frames, implementation
§8.8) + the core below + the register block. "MAC" in the diagram is the output of `xgmii_rx`.

```
             64-bit AXIS               64-bit AXIS             tokens               ops                  ops                   256-bit records
MAC ──────► Packet parser ──────────► Framer ─────────────► Decoder ────────────► Order table ───────► Book engine ───────► Output ─────────►
 (156.25    Eth/IPv4/UDP checks        MoldUDP64 header,     sequence check,       hash lookup,         price ladders,        compare with last
  MHz)      payload pass-through       message assembly,     length check,         insert / update /    best bid and ask,     record, emit
            0 cycles                   1 msg per cycle       symbol filter,        delete               window flags                               256-bit events
                                       1 cycle               hash, U split         3 cycles             7 cycles             1 cycle      ──────────►
                                                             1 cycle                                                                       (status records)
```

| Stage | Input | Output | Latency | Memory |
| --- | --- | --- | --- | --- |
| Packet parser (§5) | MAC frames | UDP payload stream + FCS pulse | 0 | registers |
| Framer (§6) | payload stream | message tokens (≤ 1 per cycle) | 1 | registers |
| Decoder (§7) | tokens | book operations, events | 1 | registers |
| Order table (§8) | operations | operations with order state | 4 | 2 hash tables (BRAM or URAM), stash |
| Book engine (§9) | operations | top of book + flags per op | 7 | 16 ladders × 2 copies (BRAM), bitmaps (LUTRAM) |
| Output (§9.6) | tops, flags, events | records, status records | 1 | registers |

Prices handed to the book engine are converted from ITCH units (1/10,000 $) to cents and to a
ladder index in parallel with the order table (§7.4), so that conversion adds no latency.

## 5. Packet parser

### 5.1 Ports

The RTL is VHDL-2008: keywords, standard and own types, and constants in upper case; signals,
ports and entities in lower case (implementation.md §2):

```vhdl
ENTITY packet_parser IS
    PORT (
        clk          : IN  STD_LOGIC;
        rst          : IN  STD_LOGIC;
        cfg_group_ip : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);  -- 233.54.12.111
        cfg_port     : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);  -- 26477
        s_tdata      : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);  -- from the MAC: whole frames, first byte in s_tdata(7 DOWNTO 0)
        s_tkeep      : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid     : IN  STD_LOGIC;
        s_tlast      : IN  STD_LOGIC;
        s_tuser      : IN  STD_LOGIC;   -- '1' = bad FCS, valid with s_tlast (a wrapper adapts the MAC's polarity)
        m_tdata      : OUT STD_LOGIC_VECTOR(63 DOWNTO 0);  -- UDP payload of accepted frames (the MoldUDP64 packet)
        m_tkeep      : OUT STD_LOGIC_VECTOR(7 DOWNTO 0);
        m_tvalid     : OUT STD_LOGIC;
        m_tlast      : OUT STD_LOGIC;
        m_fcs_error  : OUT STD_LOGIC;   -- pulse: an accepted frame had a bad FCS (may come after m_tlast)
        drop_pulse   : OUT STD_LOGIC;   -- one per dropped frame, for the counter
        fcs_pulse    : OUT STD_LOGIC    -- one per bad FCS (accepted or not), for the counter
    );
END ENTITY;
```

No `tready` anywhere (one beat per cycle). `tstrb`, `tid` and `tdest` are not used: MACs don't drive them and
there is one stream.

### 5.2 Where the fields are

Byte `b` of the frame is in beat `b / 8`, bits `8·(b % 8) + 7 : 8·(b % 8)`. Network fields are
big-endian: a 16-bit field at byte `b` is `{byte b, byte b+1}`.

| Beat | Bytes | Fields used |
| --- | --- | --- |
| 0 | 0–7 | (destination MAC, not checked) |
| 1 | 8–15 | EtherType (12–13), version/IHL (14) |
| 2 | 16–23 | IP total length (16–17), flags + fragment offset (20–21), protocol (23) |
| 3 | 24–31 | IP checksum (24–25), destination IP bytes 0–1 (30–31) |
| 4 | 32–39 | destination IP bytes 2–3 (32–33), destination port (36–37), UDP length (38–39) |
| 5 | 40–47 | UDP checksum (40–41, not checked), payload bytes 0–5 (42–47) |
| 6… | 48… | payload |

### 5.3 Checks and when they are decided

A beat counter (0, 1, 2, …, saturating) runs within each frame. Each check sets or keeps a
registered `bad` bit when its beat arrives:

| Beat | Check (fails → drop) |
| --- | --- |
| 1 | EtherType = 0x0800; version/IHL = 0x45 |
| 2 | protocol = 17; `flags_offset & 0x3FFF` = 0 (no fragment); register IP length |
| 1–4 | IPv4 checksum: add the 16-bit words 14–33 beat by beat (one's complement); at beat 4 the folded sum must be 0xFFFF |
| 3–4 | destination IP = `cfg_group_ip` |
| 4 | destination port = `cfg_port`; UDP length = IP length − 20; UDP length ≥ 8 + 20 (room for the MoldUDP64 header) |
| any | the frame ends at or before byte 42, before its first payload byte (`tlast` before beat 5, or at beat 5 with lane 2 empty) |

At the end of beat 4, `accept = !bad` is registered and stays valid for the rest of the frame
(drops are decided before the payload). A dropped frame produces one `drop_pulse`, at its `tlast`.

### 5.4 Payload forwarding (0 cycles)

From beat 5 on, the data path is wires: `m_tdata = s_tdata`. The control signals are
computed from registers:

- `m_tvalid = s_tvalid && accept && in_payload`, where `in_payload` is set from beat 5 until
  the last payload beat.
- `m_tkeep`: beat 5 is `8'b1111_1100 & s_tkeep` (lanes 0–1 hold the UDP checksum). The last
  payload byte is byte `13 + ip_length`; its beat `(13 + ip_length) / 8` is computed when the
  IP length arrives (beat 2) and registered. On that beat, `m_tkeep` keeps the lanes up to
  `(13 + ip_length) % 8`, and `m_tlast` = 1. Beats after it (Ethernet padding, or an FCS
  the MAC didn't remove) are not forwarded.
- A frame that ends before its IP length (`s_tlast` earlier): `m_tlast` = 1 on that beat, with
  `m_tkeep = s_tkeep`. The framer finds the cut (§6.4).
- `m_fcs_error = s_tvalid && s_tlast && s_tuser && accept`. It comes on the MAC's last beat,
  which can be after `m_tlast` if the frame has padding.

The payload starts in **lane 2 of beat 5** and is passed on unaligned (option B): realigning
would cost a cycle for 6 of 8 bytes and save the framer nothing, because messages start in any
lane anyway.

`tvalid` may drop in the middle of a frame; every counter and register only advances when
`s_tvalid` is 1.

### 5.5 Specification and tests

`framing.frame_payload` is the reference for accept/drop and for the payload bytes. Test
vectors: `foreign_traffic`, `bad_ipv4`, `accepted_variants`, `frame_sizes`, `back_to_back`,
`malformed_mold` (cut frames), `bad_fcs`.

## 6. Framer

The framer turns the payload stream into **tokens**, one per MoldUDP64 message, plus tokens for
heartbeats and bad FCS. It emits at most one token per cycle.

### 6.1 Token

| Field | Bits | Meaning |
| --- | --- | --- |
| `kind` | 2 | MSG, HEARTBEAT, FCS |
| `seq` | 64 | sequence number of the message (MSG) or of the next message (HEARTBEAT) |
| `len` | 16 | message length from its block |
| `data` | 40 × 8 | message bytes 0–39, byte 0 = message type |
| `t_arrival` | 16 | cycle counter when the message's last byte arrived (for the latency histogram) |

40 bytes covers every message the book needs (F, the largest, has 40; R has 39). Longer
messages (I, O, P, Q: 40–50 bytes) are still counted for sequencing; their bytes beyond 40 are
not kept, and the decoder ignores those types.

### 6.2 Header

The payload offset is constant (byte 42 of the frame), so the MoldUDP64 header sits at fixed
lanes:

| Field | Payload bytes | Frame bytes | Beat, lanes |
| --- | --- | --- | --- |
| session | 0–9 | 42–51 | not checked |
| sequence number | 10–17 | 52–59 | beat 6 lanes 4–7, beat 7 lanes 0–3 |
| message count | 18–19 | 60–61 | beat 7 lanes 4–5 |
| first block | 20… | 62… | beat 7 lanes 6–7 = first length prefix |

At beat 7 the framer registers `packet_seq` and `count`. A count of 0 or 0xFFFF means no
messages: the framer emits a HEARTBEAT token with `seq = packet_seq`. If the frame ends before
the header is complete (cut inside it), no token is emitted and the frame counts as dropped
(`drop_pulse` from the framer).

### 6.3 Message assembly

The framer keeps the byte position of the next length prefix and walks through the blocks.
Per beat, because there is only one message end per beat, at most this happens: the end of message *k*, the length prefix of *k+1*
(possibly split across two beats: then its first byte is held in a register), and the first
bytes of *k+1*.

So two **assembly buffers** of 40 bytes, used alternately, are enough: one finishes message
*k* while the other starts *k+1*. Writing into a buffer: all 8 lanes of a beat land at the
same offset relative to the message start, `offset = beat_position − message_start`. Buffer
byte `j` therefore takes lane `(j − offset) mod 8`, when that lane belongs to the message. That
is one 8:1 multiplexer per buffer byte (40 × 8 bits per buffer).

*Why not a ring buffer?* A ring buffer (say 64 bytes, 8 written per cycle) is the other
textbook answer. Reading a 40-byte message out of it at an arbitrary start needs a 64:1
multiplexer per output byte: about eight times the logic and a deeper path. The two buffers
do the same with 8:1 multiplexers.

When the beat contains a message's last byte, the buffer is complete at the end of that
cycle and the token is valid in the next cycle (1 cycle latency). `seq = packet_seq + index`
(index counted from 0 within the packet).

### 6.4 Broken packets

Every case is counted as one malformed packet, and the framer then ignores the rest of the
payload. Messages already emitted stay emitted. The lost messages become a gap at the next
packet.

| Case | Detected at |
| --- | --- |
| Block length < 12 (includes 0) | the length prefix |
| `m_tlast` in the middle of a length prefix or a message (frame cut, or blocks longer than the payload) | `m_tlast` |
| `m_tlast` at a block boundary with fewer blocks than `count` | `m_tlast` |
| Bytes left after `count` blocks | the next byte |

### 6.5 Bad FCS

When `m_fcs_error` arrives, the framer emits an FCS token 3 cycles later (a fixed delay), which
is at least two cycles after the frame's last message token, so it always comes after that frame's messages, and its status
record leaves after their records (§9.7). The slot is free: the next frame starts in a new
beat (frames start in lane 0), and its first message can't complete before that frame's beat 9 (headers fill
beats 0–7; the smallest message, 12 bytes, ends at byte 75). That is at least 10 cycles after
this frame's last beat.

### 6.6 Specification and tests

`framing.read_packet` is the reference (blocks shorter than 12 bytes are broken). Vectors:
`malformed_mold`, `frame_sizes`, `alignment` (message starts in every lane, a length prefix
split across beats), `heartbeats`, `end_of_session`, `bad_fcs`.

## 7. Decoder

One cycle. It does the sequence check, the length check, the symbol filter, field extraction,
and hashing, and splits U into two operations.

### 7.1 Sequence check

Register `expected` (64 bits, reset to 1).

| Token | Condition | Action |
| --- | --- | --- |
| MSG | `seq < expected` | duplicate: drop the token, count |
| MSG | `seq > expected` | gap: count; set `stale_all`, emit a gap event if it was 0 (§9.7); then process the message |
| MSG | always (if not a duplicate) | `expected = seq + 1` |
| HEARTBEAT | `seq > expected` | gap as above, with the event stamped with the last message's timestamp; `expected = seq` |
| HEARTBEAT | `seq ≤ expected` | nothing |
| FCS | — | set `stale_all`, emit an FCS event if it was 0 |

Every operation carries a snapshot of `stale_all` taken here, so a record's stale bit reflects
the state *at its message*, not at the time it leaves the pipeline.

### 7.2 Length check and message types

| Type | Length | Fields (byte offsets in the message) |
| --- | --- | --- |
| A | 36 | locate 1–2, timestamp 5–10, ref 11–18, side 19, shares 20–23, price 32–35 |
| F | 40 | as A |
| E | 31 | locate, timestamp, ref 11–18, shares 19–22 |
| C | 36 | as E (execution price ignored) |
| X | 23 | as E |
| D | 19 | locate, timestamp, ref 11–18 |
| U | 35 | locate, timestamp, old ref 11–18, new ref 19–26, shares 27–30, price 31–34 |
| R | 39 | locate 1–2, stock 11–18 |
| other | any | ignored (still counted for sequencing) |

A message of type A, F, E, C, X, D, U or R whose length doesn't match is **unusable**. We
can't trust any of its fields, so we don't know which book it belonged to. It is handled like
a lost message: count it, set `stale_all`, and emit an event if it was 0 (reason "unusable
message").

### 7.3 Symbol filter

Eight registers hold the locate code of each tracked ticker, learned from R messages: an R
whose stock (8 bytes) equals `cfg_ticker[i]` sets `locate[i]` and `locate_valid[i]`. A later R for
the same ticker replaces it. If two books hold the same locate, the lowest book id wins (the
hardware model keeps one locate per book in the same way). A book
message's locate is compared with all 8 in parallel; no match drops the message. An R
followed by a message for that locate in the next cycle works, because the registers update
at the end of this cycle.

### 7.4 Hash and price conversion

- **Hash:** for the order ref (and for U the new ref), compute `h0 = H3_0(ref)` and
  `h1 = H3_1(ref)`. Each output bit is the XOR of a fixed set of ref bits (masks from
  `stats.py`, seeds 0x1DC0FFEE and 0x2BADBEEF); about 32 inputs per bit, two LUT levels. The
  low 13 bits are the bucket indices.
- **Ref width:** refs ≥ 2³² can't be stored (the table keeps 32 bits). Such an add, or a
  lookup for such a ref, is a ref error (§13). The reference day's largest ref was 308,629,516
  (29 bits).
- **Price → cents → ladder index** (A, F, U): `cents = price / 100`, computed as
  `(price × 0x51EB851F) >> 37` (exact for all 32-bit values) in a pipelined DSP multiplier
  that runs in parallel with the order table (3 cycles available). `remainder = price −
  cents × 100`. Then, with the book's window `[base, base + W)` in cents (§14):

  | Condition | Position | Ladder index |
  | --- | --- | --- |
  | `cents < base` | below | — |
  | `cents ≥ base + W` | above | — |
  | inside, `remainder ≠ 0` (a sub-penny price ≥ $1) | unrepresentable | — |
  | inside, `remainder = 0` | inside | `cents − base` (13 bits) |

### 7.5 Operations and the U split

| Message | Operations |
| --- | --- |
| A, F | INSERT(ref, side, shares, position, index) |
| E, C, X | REMOVE(ref, shares) |
| D | DELETE(ref) |
| U | cycle t: U_DELETE(old ref); cycle t+1: U_INSERT(new ref, shares, position, index), with side and book from the old order |

U_INSERT is held in a register for one cycle and carries U's `stale_all` snapshot. A book
operation that would come in cycle t+2 (a D right after the U, §3) is held one more cycle and
leaves in t+3, so it is 2 cycles behind U_INSERT. Its window
position uses the U's own book (from its locate), so it can be computed here. If a
non-book token arrives in cycle t+1, it is processed normally; it produces no operation, so
there is no conflict (one book operation per cycle). Each operation also carries `book_id` (from the symbol filter),
`seq`, `timestamp`, `t_arrival`, and `record_enable` (0 for U_DELETE). For REMOVE, DELETE and
U_DELETE the order table replaces `book_id` with the book stored in the order's entry, as the
model does.

## 8. Order table

### 8.1 Structure

| Part | Size (base configuration) |
| --- | --- |
| Two tables T0, T1 | 8,192 buckets each (parameter `BUCKET_BITS = 13`) |
| Bucket | 8 slots (parameter `WAYS = 8`), one memory row |
| Slot (84 bits) | valid 1, ref 32, book 3, side 1, position 2, ladder index 13, shares 32 |
| Stash | 32 entries in registers, searched in parallel |
| Capacity | 2 × 8,192 × 8 = 131,072 orders, plus 32 in the stash |

A key `ref` lives in bucket `h0` of T0, or bucket `h1` of T1, or the stash. An insert goes
into the emptier of its two buckets (T0 on a tie), into the stash if both are full, and fails
if the stash is full too. On the reference day this configuration peaked at 100,496 live
orders (77% load), used at most 11 stash entries, and never failed (`stats.py`).

Each table is a dual-port memory: **port A only reads, port B only writes**, whole rows.
Because every operation needs exactly one read and at most one write per table, there are
never port conflicts. That is the reason U is split into two operations: a single U would need
two reads (old and new bucket) in the same table.

### 8.2 Pipeline (4 cycles)

In the code the operation in the read stage is the input `op`; `op_delay(k)` holds it k cycles
later, `op_match = op_delay(RAM_LATENCY)` is the operation in the match stage and `op_update`
(a register after it) the one in the update stage:

| Stage | Work |
| --- | --- |
| **read** | present `h0`, `h1` to port A of T0, T1 |
| **fetch** | memory output register; each bank compares the bucket with its pending writes (§8.3) and registers only the hit bits |
| **match** | forwarded rows (a mux), compare 16 slots + 32 stash entries with the ref; rows, hits and the stash entry are registered |
| **update** | modify, write rows on port B, update the stash |

The first synthesis put lookup and modify into one stage: 28 logic levels from the URAM output
to its write input, 0.9 ns too slow at 156.25 MHz. Splitting them costs one cycle.

**The U hand-over in the match stage.** Operations of different messages are at least 2 cycles
apart (§3); only U_INSERT follows its U_DELETE in the next cycle. So U_DELETE's write is the
only one a match stage can miss: it happens in the same cycle. The update stage takes the row
written in the cycle before (the bank's `previous_write_row`) when it is the same bucket, drops
hits on slots that are no longer valid, counts free slots on that row, and takes the stash's
current free entry. It does that for every operation, but only U_INSERT can find a difference.
Hits are never *added* in the update stage: an operation 1 cycle after an insert would not
find the inserted ref. That is why the decoder keeps every other pair 2 cycles apart.

In the update stage:

- **INSERT / U_INSERT:** if the ref is already present → inconsistent (§13), ignore. Else
  choose a slot: the bucket with fewer valid slots (T0 on a tie), the first free slot in it;
  else the stash; else insert failed (§13). U_INSERT takes the side from the U_DELETE one
  cycle ahead (a direct register). If U_DELETE found no order, U_INSERT does nothing (the
  unknown ref is counted once). If the old order's book differs from the U's book, U_INSERT
  is ignored and counted as inconsistent, and both books are marked stale (the old order is
  gone from one book, and the new one is missing from the other).
- **REMOVE:** found → `shares −= n`; if `n > shares`, clamp to `shares` and count
  inconsistent; if `shares` becomes 0, clear `valid`. Not found → unknown ref (§13).
- **DELETE / U_DELETE:** found → clear `valid`, the removed shares are the entry's shares. Not
  found → unknown ref.
- The output to the book engine: book, side, position, ladder index, and the share change (+
  for an insert, − for a remove), or "no change".

### 8.3 Forwarding

An operation reads its rows in its read stage and uses them in its match stage, 2 cycles later.
Writes made in those 2 cycles (the read cycle and the one after) are not in what it read.

A **write history** of the writes of the last 2 cycles per table (bucket index, row) is kept.
In the match stage, if a row's bucket index matches an entry, the newest matching entry replaces
the memory data. The bucket compare happens a cycle earlier, in the fetch stage, against the
write of that cycle and the history; only the hit bits are registered. The write made in the
match cycle itself can only be U_DELETE's for its U_INSERT (§8.2). The stash is registers,
updated in the update stage, so later operations see it without forwarding.

Cases this covers: A then E on the same order 2 cycles later; U_DELETE and U_INSERT whose
refs hash to the same bucket; any two refs that share a bucket.

Never rely on the memory's read-during-write behaviour: the same-cycle case (a write in
cycle *c* and a read of the same row in cycle *c*) is covered by the history too.

### 8.4 Memory mapping

A row is 8 × 84 = 672 bits.

| Device memory | Per table | Both tables |
| --- | --- | --- |
| BRAM36 (512 × 72) | 10 wide × 16 deep = 160 | **320 BRAM36** |
| URAM (4K × 72) | 10 wide × 2 deep = 20 | **40 URAM** |

The robust configuration (`BUCKET_BITS = 14`, 262K slots, 38% load) doubles this.

The generic `TABLE_STYLE` of `order_table` selects the memory: `"ultra"` (default) or `"block"`
for devices without URAM (ZCU102).

## 9. Book engine and output

### 9.1 Structure, per book and side

| Part | Size | Memory |
| --- | --- | --- |
| Ladder: shares per price level | W = 8,192 levels × 32 bits | 2 copies (§9.3), BRAM |
| Level bitmap L0: bit = level not empty | 128 words × 64 bits | LUTRAM (asynchronous read), two copies: one read by the level update, one by the search |
| Summary bitmap L1: bit = L0 word not zero | 128 bits | registers |
| Off-ladder counters: below, above, unrepresentable | 3 × 18 bits | registers |
| Best level index, best shares | 13 + 32 bits | registers |

There are 16 of these (8 books × bid/ask). The book's price for ladder index `i` is
`(base + i) × 100` in ITCH units.

### 9.2 Pipeline (7 cycles)

The seven stages, in order, with the names used in the code:

| Stage | Work |
| --- | --- |
| **level read** | Off-ladder operation (position ≠ inside): update the counter now (insert +1, removal of the whole order −1). Inside operation: present the ladder index to copy 1, port A |
| **level fetch** | memory output register |
| **level update** | forward (§9.4); `new = old ± shares`; write `new` to both copies (port B) at the index; set or clear the bitmap bit if the level became non-empty or empty (L0 word in LUTRAM, L1 bit) |
| **search** | bids take the highest set L1 bit, read that L0 word, take its highest set bit; asks take the lowest. Result: best index, or "side empty". Each book side runs its own search every cycle; the stage takes its side's result |
| **best read** | present the best index to copy 2, port A |
| **best fetch** | memory output register |
| **result** | best shares (with reverse forwarding, §9.4); compute flags (§9.5); hand top + flags to the output stage |

Off-ladder operations take the same 7 cycles (the ladder steps do nothing), so latency stays
fixed.

The search runs after every operation, even when the best can't have changed. That keeps the
latency fixed and avoids special cases. If the search needs more than one cycle at the target
clock, it becomes two stages (L1, then L0), and the latency grows by one cycle.

### 9.3 Why two ladder copies

Each operation needs two reads (the level it changes, and later the best level) and one
write. With a single dual-port copy, the reads of consecutive operations collide on the ports
for some spacings. With two copies, each port has one fixed role:

| Copy | Port A | Port B |
| --- | --- | --- |
| 1 | read the changed level (level read) | write (level update) |
| 2 | read the best level (best read) | write (level update), the same data |

That costs memory (§11), but it makes the schedule independent of message spacing.

### 9.4 Forwarding in the book engine

**Ladder level read-after-write (copy 1).** An operation reads its level in its level-read
stage and writes it two stages later, in level update, so the two previous operations may have
pending writes to the same (book, side, index).
Example: D removes 100 shares at $289.44, A adds 200 at $289.44 two cycles later; or the two
halves of a U at the same price. A write history of the last 2 writes (book, side, index,
value) is checked in level update; the newest match replaces the memory data.

**Best-shares read sees a later write: reverse forwarding (copy 2).** An operation reads the
best level in its best-read stage. A *later* operation that is 2 cycles behind is in its
level-update stage in that same cycle, and writes. If it
writes the same level, the memory may already show the later value, but this record must
show the state *after this operation and before the later one*. Rule: if a later operation
writes the same (book, side, index) in or before the cycle of this operation's best read, use
the `old` value (the value it read before adding or subtracting) of the earliest such
operation, not the memory data. With a one-cycle search those are the operations 1 and 2
cycles behind: the one 1 behind wrote in the cycle before the best read (the memory already has
it), the one 2 behind writes in the same cycle (the memory may return either value). With a
two-cycle search (best read one cycle later) it is the operations 1, 2 and 3 cycles behind.
Operations further behind write later and can't interfere.

Only the two halves of a U are 1 cycle apart, and U_DELETE produces no record; the RTL covers
the case anyway, so every top (also U_DELETE's, which updates the side's best registers) is
the state after its own operation.

**Bitmap search timing.** No hazard. An operation updates its bits in level update (the write
takes effect at the end of the cycle) and searches in the next stage, so it sees its own update
and every earlier one. A later operation updates in this operation's search cycle at the
earliest (the second half of a U), and that write only takes effect after the search.

**Flag snapshot.** Each operation takes a snapshot of its book's off-ladder counters (as "> 0"
bits) right after its own update in level read, and carries it. The flags are computed from
that snapshot, so later operations can't leak into them.

### 9.5 Flags

Per book, for each record:

| Bit | Name | Set when |
| --- | --- | --- |
| 0 | stale | `stale_all` snapshot (§7.1) OR the book's stale bit (§13), sampled in the order table's update stage |
| 1 | top outside window | bid side: a bid above the window, or an unrepresentable bid, or (no bid inside and a bid below). Ask side: an ask below the window, or an unrepresentable ask, or (no ask inside and an ask above) |
| 2 | gap | status records only |
| 3 | bad FCS | status records only |
| 4 | unusable message | status records only |

When bit 1 is set, the record still shows the ladder's best (or empty), and the flag says it
may not be the true top.

### 9.6 Output stage (1 cycle)

Per book, a register holds the last emitted top and flags. An operation with `record_enable`
emits a record if its top or flags differ from that register, and updates the register.

Record layout (32 bytes, byte 0 in bits 7:0 of the 256-bit bus, the same byte order as
`record.py`):

| Word | Bits | Book record | Status record |
| --- | --- | --- | --- |
| 0 | 63:56 | book id 0–7 | 0xFF |
| 0 | 55:48 | flags | flags (stale + reason) |
| 0 | 47:0 | ITCH timestamp of the message | timestamp of the message that revealed it |
| 1 | 63:0 | sequence number | sequence number |
| 2 | 63:32 / 31:0 | bid price / bid shares | 0 |
| 3 | 63:32 / 31:0 | ask price / ask shares | 0 |

An empty bid is price 0, shares 0; an empty ask is price 0xFFFFFFFF, shares 0.

### 9.7 Status records (events)

When `stale_all` goes from 0 to 1 (a gap, a bad FCS, or an unusable message), one status
record is emitted. Status records leave on their **own channel** (`m_evt`, same 256-bit
layout, book id 0xFF), so they never compete with book records for a slot.

Each event goes through a delay line, so that it leaves 13 cycles after the decoder cycle in
which it was detected: for a gap revealed by a message, that is the cycle in which an A's
record would leave (cycle 14 of that message). Records and events therefore come out in
sequence-number order. When a later stage merges both channels into one stream, an event and a
record that leave in the same cycle go out **event first**:

- a gap revealed by an A: the event, then the A's record (already stale);
- a gap revealed by a U: the event leaves in cycle 14, the U's record in cycle 15;
- a bad FCS: the FCS token comes 2 cycles after the frame's last message token (§6.5), so the
  event leaves after the records of that frame.

A heartbeat gap event carries the heartbeat's sequence number and the timestamp of the last
usable message processed (0 if none). An FCS event carries the sequence number and timestamp
of the frame's last message. An event revealed by an unusable message carries that message's
sequence number and the timestamp of the last usable message, since its own bytes can't be
trusted; an unusable message also doesn't update that timestamp.

Only a packet whose message count is 0 or 0xFFFF is a heartbeat. A broken packet without any
complete block emits no token at all; its lost messages show up as a gap at the next packet.

### 9.8 Core output ports

```vhdl
m_rec_tdata  : OUT STD_LOGIC_VECTOR(255 DOWNTO 0);  -- book records, at most one per cycle
m_rec_tvalid : OUT STD_LOGIC;
m_evt_tdata  : OUT STD_LOGIC_VECTOR(255 DOWNTO 0);  -- status records
m_evt_tvalid : OUT STD_LOGIC;
```

No `tready`: the receiver (the UDP transmit stage, later) must accept every cycle. It has its
own FIFO, which is outside the measured latency.

## 10. Hazards: complete list

Each hazard has a name, used in code comments and test names.

| Name | Where | Problem | Example | Handling |
| --- | --- | --- | --- | --- |
| **order-table row read-after-write** | order table | row read before an earlier operation's write | A at seq 100, E on the same ref at seq 101, 2 cycles apart | write history, last 2 writes per table (§8.3) |
| **U hand-over** | order table | U_INSERT needs U_DELETE's result 1 cycle later | U changes ref 7 → ref 8 | direct register from U_DELETE's update stage |
| **stash in registers** | order table | two operations in a row use the stash | both buckets full twice in a row | the stash is registers, updated in the update stage |
| **ladder level read-after-write** | ladder copy 1 | level read before an earlier write | D at $289.44, then A at $289.44 2 cycles later | write history, last 2 writes (§9.4) |
| **best-shares reverse forwarding** | ladder copy 2 | best read sees a later operation's write | D empties the best bid; 2 cycles later E changes the new best level | the later operation's old value (§9.4) |
| **bitmap search timing** | bitmaps | search sees a later operation's bit | U_INSERT right after U_DELETE | none: a write takes effect after the cycle it is made in, and the earliest later write is in the search cycle |
| **flag snapshot** | counters / flags | flags see a later operation's counter change | off-ladder insert right after a record-producing operation | snapshot per operation (§9.4) |
| **stale snapshot** | stale bits | a record leaving after a gap shows stale for a message before the gap | gap revealed while earlier messages are still in the pipeline | snapshot at the decoder and in the order table's update stage |
| **locate learning** | symbol filter | message right after its R | R for AAPL, A for AAPL in the next cycle | registers update at the end of the R's cycle |
| **expected sequence number** | sequence check | consecutive messages | every cycle | `expected` updates in one cycle |
| **last-emitted register** | output | consecutive records of one book | — | updated in order, one record per cycle |

Every row has a directed stress test (§15).

## 11. Memory and device

| Part | Base configuration | Robust configuration |
| --- | --- | --- |
| Order table | 320 BRAM36 or 40 URAM | 640 BRAM36 or 80 URAM |
| Ladders (16 × 8,192 × 32 bits × 2 copies) | 256 BRAM36 (8K × 4 per BRAM, 8 per ladder copy) | same |
| Bitmaps (16 × 8,192 bits) | about 2,000 LUTs as LUTRAM | same |
| Stash, registers, counters | about 3,000 flip-flops | same |
| **Total** | **≈ 590 BRAM36**, or **40 URAM + ≈ 270 BRAM36** | ≈ 910 BRAM36, or 80 URAM + ≈ 270 BRAM36 |

The ladders can alternatively go into URAM by packing bid and ask of one book into one 72-bit
word (32 URAM for all).

| Board | Device | Base | Robust |
| --- | --- | --- | --- |
| ZCU102 | ZU9EG: 912 BRAM36, no URAM | ✓ all BRAM (65%) | ✗ |
| KCU116 | KU5P: 480 BRAM36, 64 URAM | ✓ table in URAM | ✗ |
| ZCU104 / ZCU106 | ZU7EV: 312 BRAM36, 96 URAM | ✓ table (and ladders) in URAM | ✓ table in URAM |
| VCU118 | VU9P: 2,160 BRAM36, 960 URAM | ✓ | ✓ |
| small parts | e.g. ZU3EG: 216 BRAM36 | ✗ | ✗ |

**Chosen board: ZCU102** (XCZU9EG-2FFVB1156E), with `TABLE_STYLE => "block"`. The step 9 timing
run (WNS +0.108 ns) was on the ZU7EG with the order table in URAM; the ZCU102 build must be
timed again. The board setup is in implementation §11.

If no fitting device is available, the fallback is a lighter ticker set from the same day
(AAPL, MSFT and TSLA account for 67K of the 100K peak), which keeps everything else real.

## 12. Latency budget

Cycle 0 is the cycle in which the beat holding a message's last byte is on the parser input.

| Stage | Cycles | Cumulative |
| --- | --- | --- |
| Parser (combinational) | 0 | 0 |
| Framer: token valid | 1 | 1 |
| Decoder | 1 | 2 |
| Order table (read, fetch, match, update) | 4 | 6 |
| Book engine | 7 | 13 |
| Output: record valid | 1 | **14 cycles = 89.6 ns** |
| U: second operation | +1 | **15 cycles = 96.0 ns** |
| D right after a U (held one cycle, §3) | +1 | **15 cycles = 96.0 ns** |
| In front: `xgmii_rx` (XGMII word → MAC output) | +2 | 16 cycles = 102.4 ns (U: 17) from the XGMII word |

The latency is fixed for each message type; the one exception is a D that directly follows a U
(§3), one cycle later. The order table's match stage was added for timing
(§8.2). Where timing closure needs more, the next candidates are a second cycle for the
best-level search (+1) and a second decoder cycle (+1); with both, 16 cycles (102 ns).

## 13. Errors and anomalies

| Event | Detected in | Counter | Effect |
| --- | --- | --- | --- |
| Frame not for us or invalid header | parser | dropped frames | dropped |
| Frame cut inside the MoldUDP64 header | framer | dropped frames | dropped |
| Broken MoldUDP64 packet (§6.4) | framer | malformed packets | rest of packet ignored; gap at the next packet |
| Bad FCS | parser, decoder | FCS errors | if accepted: all books stale, event |
| Duplicate message | decoder | duplicates | dropped (a heartbeat at or below the expected number changes nothing and isn't counted) |
| Gap (message or heartbeat) | decoder | gaps | all books stale, event (first time) |
| Unusable message (length doesn't match type) | decoder | unusable messages | all books stale, event (first time) |
| Insert failed (buckets and stash full) | order table | insert failures | that book stale |
| Ref ≥ 2³² | decoder / order table | ref errors | that book stale |
| Unknown ref | order table | unknown refs | that book stale |
| Inconsistent (ref exists on insert, too many shares removed) | order table | inconsistent | that book stale (clamped or ignored) |
| Order outside the window | decoder | off-ladder orders | counted; flag bit 1 while it matters |
| Unrepresentable price inside the window | decoder | unrepresentable prices | flag bit 1 while the order lives |
| Stash used | order table | stash inserts (diagnostic) | none |

Per-book stale: an anomaly in the order table means an order of that book is missing or
wrong, so only that book is marked. Stale bits are only cleared by reset. After a gap all
books are stale anyway, so later per-book anomalies (which are expected then) change nothing.

## 14. Configuration and reset

| Input | Meaning |
| --- | --- |
| `cfg_group_ip`, `cfg_port` | the feed's multicast group and UDP port |
| `cfg_ticker[0..7]` | 8 ASCII tickers, space-padded, in book-id order |
| `cfg_base[0..7]` | each book's window start in cents; software sets `max(100, reference − W/2)` from a reference price such as the previous close |

| Parameter | Base | Meaning |
| --- | --- | --- |
| `LADDER_BITS` | 13 | W = 8,192 levels (±$40.96 around the reference) |
| `BUCKET_BITS` | 13 | 8,192 buckets per table |
| `WAYS` | 8 | slots per bucket |
| `STASH` | 32 | stash entries |

**Why the window starts at $1.00 at the lowest.** US rules (Reg NMS Rule 612) allow sub-penny
prices only below $1. With the window above $1, every sub-penny price is below the window.
On the reference day all sub-penny prices were stub quotes under $0.43.

**Reset.** Synchronous, active high. It clears `expected` (to 1), all stale bits, the locate
registers, the counters, the stash, the bitmaps, the off-ladder counters and the last-emitted
registers. The two memories (order table, ladders) are cleared by a sweep that writes one row
per cycle on every port B: 8,192 cycles (52 µs). An output `busy` is high during the sweep;
frames arriving then are dropped whole (decided at their first beat, before the parser) and
counted once as dropped frames. Reset belongs before the session: the R
messages that teach the locate codes come only at the start of the day.

## 15. Verification

**Reference.** The hardware model (the rules of §13 and §16, the same hash, sizes and windows) gives the
expected records and events for every input. Comparison is bit-exact, per channel.

**Per module (cocotb, Questa):** each module gets its own testbench driven from the existing
vectors where they fit, and from directed operation streams for the order table and book
engine. End to end: pcap in, records out, compared with the model.

**Directed stress tests**, one or more per hazard and edge:

| Test | Covers |
| --- | --- |
| A then E/X/D on the same ref at minimum spacing; A, D, A on the same ref | order-table row and ladder level read-after-write |
| U with old and new in the same bucket; U at the same price; U changing price across a word boundary of L0 | U hand-over, ladder level read-after-write, bitmap search timing |
| Refs that collide in both buckets (the H3 hash is linear: `h(a ⊕ k) = h(a)` for any `k` with `h(k) = 0`, so colliding refs are easy to construct), until the stash fills and an insert fails | stash in registers, insert failure |
| D empties the best level while the next best is in another L0 word, another L1 bit, or absent | search |
| D empties the best, the next message (2 cycles later) changes the new best level | best-shares reverse forwarding |
| Orders at index 0, W−1, just below, just above; flag transitions in both directions | window, bit 1 |
| Sub-penny price inside the window, ref ≥ 2³², length mismatch, block length < 12 | anomalies |
| Back-to-back D messages at line rate for one book | book-message spacing, throughput |
| R followed immediately by an A for that locate | locate learning |
| Gap with messages in flight; FCS with padding after the IP packet | stale snapshot, event order |

**Assertions (VHDL `assert`, simulation only, in Questa):** the facts of §3 (one message end per beat; at most one token, operation and record per cycle;
book operations of different messages at least 2 cycles apart), stash count ≤ `STASH`, a
ladder level never negative, the bitmap bit equals (level ≠ 0) (checked against a shadow array
in simulation), the best index equals the highest (bid) or lowest (ask) set bit.

**Hardware** (implementation §11). First the vectors: tcpreplay plays them into the ZCU102,
an ILA captures the records, `scripts/compare_ila.py` compares them with the model. Then the
day: tcpreplay plays the day's pcap, the records leave over Ethernet (`record_tx`), are
captured on the PC and compared with the model. An on-chip latency histogram (§17, not built) would show one
spike per message type.

## 16. The hardware model

`itch/hw_model.py` (with `order_table.py`, `ladder.py`, `hashing.py`, `config.py`) implements
every rule of this document; it is the reference the RTL is compared with, bit-exact, record by
record. Compared with the ideal model (`model.py`, unlimited books) it has:

1. Status records on a separate event stream, one per 0→1 change of `stale_all`, instead of
   8 stale records; `expected_events.bin` next to `expected.bin`.
2. Per-book stale on insert failure, ref ≥ 2³², unknown ref and inconsistent messages
   (including a U whose old order belongs to another book).
3. The order table model (the H3 hash, sizes, 2-choice, stash) to know exactly when inserts fail.
4. Ladders with a window per book, off-ladder counters, flag bit 1, unrepresentable prices,
   and records that show the ladder's view.
5. `read_packet`: blocks shorter than 12 bytes are broken.
6. Decoder: a length that doesn't match the type is an unusable message (no exception): it is
   counted, makes all books stale and gives an event, like a gap.
7. Records for flag changes (bit 0 or 1), not only for top changes.
8. Configuration: tickers, bases (from a reference price per book), sizes.

It records each stage's output (tokens, operations, order-table results, tops) as "taps", so
every RTL module can be tested on its own (implementation §9.3).

The ideal model stays as a second reference: on the reference day, with the base
configuration, the hardware model must give the same tops as the ideal model, with no flags set
during regular hours.

## 17. Limitations and future work

| Item | Now | Future |
| --- | --- | --- |
| Gap recovery | books stay stale until reset | MoldUDP64 retransmission requests, or a GLIMPSE snapshot |
| Moving prices | fixed window of ±$40.96 per book; a book that moves further gets flag bit 1 (the reference day's biggest move was TSLA, −4.6%, $19.58) | a window that follows the best price, or a software-triggered rebuild of the ladder from the order table |
| Stocks under $1 | not supported (sub-penny ticks) | a ladder with $0.0001 ticks |
| Clock | 156.25 MHz, about 83 ns | a 2× core clock, synchronous 2:1 from the MAC clock: about 50 ns, and 4 cycles between book messages instead of 2 |
| MAC/PCS | vendor PCS (10G/25G Ethernet Subsystem, PCS/PMA only) and our own MAC receive path `xgmii_rx`, 2 cycles (12.8 ns) in front of the core | a custom low-latency PCS |
| Order table load | 2-choice without relocation, 77% on the reference day | cuckoo relocation in the background, for about 90% load |
| Best-price search | bitmap search and best-shares read after every operation (4 of the book engine's 7 cycles) | a sorted cache of the top 4 levels per side with their shares, refilled in the background: the new top is known in level update, about 10 cycles (64 ns) in total. It must be at least 3–4 deep, because a sweep can empty a level every 2 cycles while a refill takes about 5 |
| Latency histogram | not built; the latency is checked in simulation only | an on-chip histogram per message type |

## 18. Glossary

| Term | Meaning |
| --- | --- |
| beat | one 64-bit transfer on AXI4-Stream (8 bytes) |
| lane | one byte position within a beat, 0–7 |
| block | a MoldUDP64 message block: 2-byte length + message |
| token | the framer's output: one complete message, a heartbeat, or an FCS event |
| operation | a book change: INSERT, REMOVE, DELETE, U_DELETE, U_INSERT |
| bucket | one row of a hash table, with 8 slots |
| stash | a small register store for inserts whose two buckets are full |
| ladder | shares per price level, one per book and side |
| window | the price range a ladder covers: W levels from the book's base |
| off-ladder | an order whose price is outside the window (or unrepresentable) |
| forwarding | using a pending write's data instead of a memory read that is too old |
| reverse forwarding | using a later operation's old value, so a record doesn't see the future |
| stale | a book that may be wrong; set by gaps, FCS errors and anomalies, cleared by reset |

## Appendix: worked examples

These follow single messages through the mechanisms that are easiest to get wrong.

### A. Where the payload sits on the bus (§5.4)

Ethernet, IPv4 and UDP headers are 42 bytes, so payload byte `p` is frame byte `42 + p`, in
beat `(42 + p) / 8`, lane `(42 + p) % 8`:

```
beat 5 (bytes 40–47):  lane 0 1      | 2  3  4  5  6  7
                       UDP checksum  | p0 p1 p2 p3 p4 p5
beat 6 (bytes 48–55):  p6 p7 p8 p9 p10 p11 p12 p13
```

Realigning to lane 0 would need p6 and p7 from the next beat, so p0–p5 would wait a cycle.
It would not help the framer: with a D (19 bytes) as the first message and an A as the
second, the A starts at payload byte 43, which is lane 5 unaligned and lane 3 realigned. Any
lane is possible either way.

### B. Message assembly (§6.3)

A packet with sequence number 1000 holds a D (19 bytes), then an A (36 bytes):

| What | Frame bytes | Beat, lanes |
| --- | --- | --- |
| length `00 13` | 62–63 | 7: 6–7 |
| D | 64–82 | 8, 9, 10: 0–2 |
| length `00 24` | 83–84 | 10: 3–4 |
| A | 85–120 | 10: 5–7, 11–14, 15: 0 |

Beat 10 ends the D (buffer 0, lanes 0–2), holds the A's length (lanes 3–4) and starts the A
(buffer 1, lanes 5–7). With `offset = (frame byte of lane 0) − (message start)`, buffer byte
`j` takes lane `(j − offset) mod 8`:

| Message | Beat | offset | Writes |
| --- | --- | --- | --- |
| D (starts at 64) | 8, 9, 10 | 0, 8, 16 | D bytes 0–7, 8–15, 16–18 |
| A (starts at 85) | 10 | −5 | lanes 5–7 → A bytes 0–2 |
| A | 11 | 3 | lanes 0–7 → A bytes 3–10 |
| A | 15 | 35 | lane 0 → A byte 35 |

The D's token (seq 1000) is valid in the cycle after beat 10, the A's (seq 1001) in the cycle
after beat 15.

### C. Order table forwarding (§8.3)

An A for ref 7 and an E for ref 7 two cycles later; both hash to T0 bucket 5123:

| Cycle | A | E |
| --- | --- | --- |
| 10 | read: address 5123 | |
| 11 | fetch | |
| 12 | match: ref 7 not found, slot 3 free | read: address 5123 |
| 13 | update: write 5123 (ref 7 in slot 3, 100 shares); history ← {5123, row} | fetch: 5123 = the bucket being written → hit bit |
| 14 | | match: the memory row is old, the hit bit selects history(1): ref 7 found |
| 15 | | update: 100 − 40 = 60 |

### D. Best-price search (§9.2)

AAPL's base is $248.48, so index 4096 is $289.44. Bids at indices 4096, 4090 and 3000:

| Index | Price | L0 word, bit |
| --- | --- | --- |
| 4096 | $289.44 | 64, 0 |
| 4090 | $289.38 | 63, 58 |
| 3000 | $278.48 | 46, 56 |

L1 has bits 64, 63, 46. Highest L1 bit 64, highest bit of L0 word 64 is 0: best = 64 × 64 + 0
= 4096. A D empties $289.44: L0 word 64 becomes 0, L1 bit 64 is cleared. Next search: L1 bit
63, L0 bit 58: best = 4090 ($289.38).

### E. Why the ladder has two copies (§9.3)

Each operation on a ladder reads its level (level read), writes it two cycles later (level
update) and reads the best level another two cycles later (best read). With operations on one
ladder every 2 cycles, one cycle would need three accesses on one memory: op 1's best read,
op 2's write and op 3's level read. A memory has two ports. With
two copies, each port does one kind of access at a fixed point of every operation, so no port
is ever needed twice in a cycle.

### F. A gap (§7.1, §9.7)

Messages 1–100 are processed, `expected` = 101. The next packet starts at 106:

1. Message 106: `seq > expected` → gap counted, `stale_all` 0 → 1, a gap event is created
   (seq 106, its timestamp).
2. Message 106 is processed normally; its operation carries `stale_all` = 1, so its record has
   stale = 1. `expected` = 107.
3. Records of messages 1–100 still in the pipeline carry their own snapshot (stale = 0).
4. The event leaves through the delay line in cycle 14 of message 106, together with or before
   its record, and after the records of messages 1–100.
5. Later gaps are only counted.

