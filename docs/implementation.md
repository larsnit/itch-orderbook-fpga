# Implementation Guide

2026-10-15 · Lars Nitschke

`architecture.md` says **what** the hardware does and **why**. This guide says **how** to build
it: files, shared types, module ports, coding rules, memories, timing rules, testbenches, and
the order of work with a definition of done for each step. Read `architecture.md` (at least
§1–3 and §10) first.

Anything both documents define must agree. If a change is needed while building, change
`architecture.md` first, then this guide, then the code.

**Language and tools:** RTL in **VHDL-2008**, simulated in **Questa**, synthesised with
**Vivado**. Testbenches in **Python with cocotb**. Assertions as plain VHDL `ASSERT`, in
simulation-only checker entities.

---

## 1. Repository layout

```
rtl/
├── itch_pkg.vhd         constants, types, records, conversion and helper functions (§3)
├── itch_hash_pkg.vhd    H3 masks, generated from Python (§3.6), not edited by hand
├── itch_top.vhd         xgmii_rx + itch_core + regs_axil: XGMII in, records out
├── xgmii_rx.vhd         MAC receive path: XGMII from the PCS -> frames for packet_parser (step 10)
├── xgmii_tx.vhd         MAC transmit path: frames -> XGMII with preamble, FCS, gap (record output)
├── record_tx.vhd        record output: merge, record_fifo, record_packer, xgmii_tx (§11.8a)
│   ├── record_fifo.vhd  asynchronous FIFO, Gray pointers
│   └── record_packer.vhd  records -> Ethernet/IPv4/UDP frames
├── itch_core.vhd        the core: busy gate, stages, counters; configuration as ports
├── packet_parser.vhd
├── framer.vhd
├── decoder.vhd
├── h3_hash.vhd
├── price_unit.vhd       price → cents → position, ladder index
├── order_table.vhd      pipeline, slot choice, U hand-over, stale bits, clearing sweep
│   ├── ot_bank.vhd      one table: memory + write history
│   └── ot_stash.vhd     32 entries in registers, parallel compare
├── book_engine.vhd
│   ├── ladder_side.vhd  one book side: 2 ladder copies, bitmaps, counters, best, its own search
│   └── bitmap_search.vhd  combinational: L1 -> L0 word -> best index, direction per instance
├── output_stage.vhd
├── event_delay.vhd
├── counters.vhd
├── regs_axil.vhd        AXI4-Lite register block (§7)
├── ram_sdp.vhd          generic simple dual-port memory (§5)
└── lutram.vhd           asynchronous-read memory (§5)
sim/
├── checkers/            <module>_chk.vhd: simulation-only assertion entities (§9.4)
└── wrappers/            <module>_tb_top.vhd: DUT + checker, record ports flattened (§9.2)
tb/
├── common/              shared cocotb library (§9.1)
├── itch_pkg/            one directory per module: test_<module>.py (cocotb tests + launcher)
├── memories/            ram_sdp and lutram
├── counters/
├── axis/                driver and monitor of tb/common, through a register
├── packet_parser/
├── framer/
├── …
└── core/                end to end
scripts/
├── gen_hash_pkg.py      writes rtl/itch_hash_pkg.vhd from the Python masks
├── board_config.py     register writes for the board (devmem or xsct)
├── compare_ila.py      ILA capture (CSV) against the model
├── compare_capture.py  record frames captured with tcpdump against the model
└── make_example.py     a small hand-made pcap and its expected records
```

## 2. Coding rules

**Case.** VHDL is not case-sensitive, so case carries meaning for the reader:

| Upper case | Lower case |
| --- | --- |
| keywords (`ENTITY`, `PORT`, `IN`, `OUT`, `SIGNAL`, `PROCESS`, `IF`, `DOWNTO`, …) | signals, ports, variables |
| standard types (`STD_LOGIC`, `STD_LOGIC_VECTOR`, `UNSIGNED`, `NATURAL`, `STRING`, …) | entities, architectures, instances |
| our own types (`TOKEN_T`, `OP_T`, …) | functions and procedures (`be`, `to_slv`, `rising_edge`, `to_integer`) |
| constants and generics (`NUM_BOOKS`, `POS_INSIDE`, `WIDTH`, `LATENCY`, …) | libraries and packages (`ieee.numeric_std`, `itch_pkg`) |
| predefined attributes (`'LENGTH`, `'RANGE`, `'HIGH`) | tool attributes, as the tool spells them (`ram_style`) |

A generic never has the name of a package constant (`NUM_COUNTERS` is a constant, so
`counters` has no such generic).

**Other rules:**

- VHDL-2008, synthesisable subset. One entity per file, file name = entity name, architecture
  name `rtl`. Libraries: `ieee.std_logic_1164` and `ieee.numeric_std` only.
- One clock `clk` (156.25 MHz), one reset `rst`: synchronous, active high. Only control state
  is reset (valid bits, counters, state machines, history valid bits); data paths are not.
- Registers in `PROCESS(clk)` with `IF rising_edge(clk)`; combinational logic in `PROCESS(ALL)`
  or concurrent statements. Every signal assigned in a combinational process gets a default
  at its top, so no latches.
- Numbers are `UNSIGNED` (or `SIGNED`); `STD_LOGIC_VECTOR` only for raw bits and memory data.
- Every pipeline signal has a valid bit; stages never stall and have no `ready`.
- Pipeline registers are named after their stage: `op_delay(k)`, `op_match` and `op_update` in the order
  table, `stages(k)` with the stage constants `AT_LEVEL_UPDATE` … `AT_RESULT` in the book engine. Counter strobes end in
  `_pulse`. No numbered codes in names or comments: refer to facts and hazards by their names
  from architecture §3 and §10.
- All shared types and constants come from `itch_pkg`; a module never redefines a field layout.
- No `ASSERT` in RTL files: assertions live in `sim/checkers/` (§9.4).

## 3. Shared types: `itch_pkg.vhd`

This package is the contract between modules. Records that cross into a memory or into the
testbench are converted with `to_slv` / `to_<record>` functions in the package. The Python
mirror `tb/common/structs.py` uses the same order. **Bit order**, one rule each:

- **Records:** first field in the most significant bits (`structs.pack` shifts each new field in
  at the bottom, so the first ends on top).
- **Arrays and byte buses:** element 0 in the least significant bits. Message byte `j` and
  output-record byte `j` are in bits `8j+7 DOWNTO 8j` (`structs.message_data`,
  `structs.record_bus`: `int.from_bytes(..., "little")`); bucket slot `i` is in row bits
  `(i+1)·84−1 DOWNTO i·84`.

`to_slv` concatenates the fields in order; `to_<record>` takes them back from the top with a
private procedure `take(bits, high, field)` per field, in the same order. Both accept vectors
with any index range (`to_entry` gets row slices such as `row(167 DOWNTO 84)`).

### 3.1 Constants

```vhdl
LIBRARY ieee;
USE ieee.std_logic_1164.ALL;
USE ieee.numeric_std.ALL;

PACKAGE itch_pkg IS

    CONSTANT NUM_BOOKS   : NATURAL := 8;
    CONSTANT LADDER_BITS : NATURAL := 13;   -- W = 8,192 levels (cents)
    CONSTANT BUCKET_BITS : NATURAL := 13;   -- 8,192 buckets per table
    CONSTANT WAYS        : NATURAL := 8;    -- slots per bucket
    CONSTANT STASH       : NATURAL := 32;   -- stash entries
    CONSTANT MSG_BYTES   : NATURAL := 40;   -- bytes of a message the framer keeps
    CONSTANT RAM_LATENCY : NATURAL := 2;    -- address cycle → data cycle (§5)
    CONSTANT EVENT_DELAY : NATURAL := 11;   -- registers between decoder and output stage (§8.7)

    CONSTANT NO_BID_PRICE : UNSIGNED(31 DOWNTO 0) := x"00000000";
    CONSTANT NO_ASK_PRICE : UNSIGNED(31 DOWNTO 0) := x"FFFFFFFF";

    FUNCTION clog2(n : POSITIVE) RETURN NATURAL;
```

### 3.2 Codes

Fields that are stored in memory or seen by the testbench use fixed bit codes rather than
enumerated types, so their encoding is known everywhere:

```vhdl
    SUBTYPE TOKEN_KIND_T IS STD_LOGIC_VECTOR(1 DOWNTO 0);
    CONSTANT TOK_MSG       : TOKEN_KIND_T := "00";
    CONSTANT TOK_HEARTBEAT : TOKEN_KIND_T := "01";
    CONSTANT TOK_FCS       : TOKEN_KIND_T := "10";

    SUBTYPE OP_KIND_T IS STD_LOGIC_VECTOR(2 DOWNTO 0);
    CONSTANT OP_INSERT   : OP_KIND_T := "000";
    CONSTANT OP_REMOVE   : OP_KIND_T := "001";
    CONSTANT OP_DELETE   : OP_KIND_T := "010";
    CONSTANT OP_U_DELETE : OP_KIND_T := "011";
    CONSTANT OP_U_INSERT : OP_KIND_T := "100";

    SUBTYPE POSITION_T IS STD_LOGIC_VECTOR(1 DOWNTO 0);
    CONSTANT POS_INSIDE : POSITION_T := "00";
    CONSTANT POS_BELOW  : POSITION_T := "01";
    CONSTANT POS_ABOVE  : POSITION_T := "10";
    CONSTANT POS_UNREP  : POSITION_T := "11";

    SUBTYPE CHANGE_T IS STD_LOGIC_VECTOR(1 DOWNTO 0);
    CONSTANT CHG_NONE   : CHANGE_T := "00";
    CONSTANT CHG_ADD    : CHANGE_T := "01";
    CONSTANT CHG_REMOVE : CHANGE_T := "10";

    CONSTANT SIDE_BID : STD_LOGIC := '0';   -- ITCH "B"
    CONSTANT SIDE_ASK : STD_LOGIC := '1';   -- ITCH "S"

    SUBTYPE MSG_TYPE_T IS STD_LOGIC_VECTOR(7 DOWNTO 0);   -- message byte 0
    CONSTANT TYPE_ADD       : MSG_TYPE_T := x"41";        -- A; also TYPE_ADD_MPID (F), TYPE_EXECUTED (E),
    ...                                                   -- TYPE_EXECUTED_PRICE (C), TYPE_CANCEL (X), TYPE_DELETE (D),
                                                          -- TYPE_REPLACE (U), TYPE_DIRECTORY (R), SIDE_SELL ("S")
```

### 3.3 Message bytes

A token carries message bytes 0–39 with **byte `j` in bits `8·j + 7 DOWNTO 8·j`** (the same lane
order as AXI4-Stream). ITCH fields are big-endian, so a field is assembled from its bytes:

```vhdl
    SUBTYPE MSG_DATA_T IS STD_LOGIC_VECTOR(MSG_BYTES*8 - 1 DOWNTO 0);

    -- count bytes starting at offset, most significant byte first
    FUNCTION be(data : MSG_DATA_T; offset : NATURAL; count : POSITIVE) RETURN UNSIGNED;
```

```vhdl
    FUNCTION be(data : MSG_DATA_T; offset : NATURAL; count : POSITIVE) RETURN UNSIGNED IS
        VARIABLE result : UNSIGNED(8*count - 1 DOWNTO 0);
    BEGIN
        FOR i IN 0 TO count - 1 LOOP
            result(8*(count - i) - 1 DOWNTO 8*(count - i - 1)) := UNSIGNED(data(8*(offset + i) + 7 DOWNTO 8*(offset + i)));
        END LOOP;
        RETURN result;
    END FUNCTION;
```

Example: the order ref of an A is `be(data, 11, 8)`, its price `be(data, 32, 4)`.

### 3.4 Interfaces between stages

```vhdl
    -- framer → decoder
    TYPE TOKEN_T IS RECORD
        valid     : STD_LOGIC;
        kind      : TOKEN_KIND_T;
        seq       : UNSIGNED(63 DOWNTO 0);     -- MSG: its number; HEARTBEAT: the next message's
        len       : UNSIGNED(15 DOWNTO 0);
        data      : MSG_DATA_T;                -- byte j in bits 8j+7 DOWNTO 8j
        t_arrival : UNSIGNED(15 DOWNTO 0);     -- cycle counter at the message's last byte
    END RECORD;

    -- decoder → order table
    TYPE OP_T IS RECORD
        valid         : STD_LOGIC;
        kind          : OP_KIND_T;
        book          : UNSIGNED(2 DOWNTO 0);  -- from the symbol filter
        side          : STD_LOGIC;             -- INSERT only; U_INSERT takes it from U_DELETE
        order_ref     : UNSIGNED(31 DOWNTO 0);
        ref_error     : STD_LOGIC;             -- the 64-bit ref has bits above 31 set
        bucket0       : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);  -- h0(ref)
        bucket1       : UNSIGNED(BUCKET_BITS - 1 DOWNTO 0);  -- h1(ref)
        shares        : UNSIGNED(31 DOWNTO 0); -- INSERT/U_INSERT: order shares; REMOVE: shares removed
        seq           : UNSIGNED(63 DOWNTO 0);
        timestamp     : UNSIGNED(47 DOWNTO 0);
        t_arrival     : UNSIGNED(15 DOWNTO 0);
        stale_all     : STD_LOGIC;             -- snapshot at the decoder
        record_enable : STD_LOGIC;             -- '0' for U_DELETE
    END RECORD;

    -- price unit → order table, arrives in the update stage together with its operation
    TYPE PRICE_INFO_T IS RECORD
        position : POSITION_T;
        index    : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
    END RECORD;

    -- one order-table slot: 84 bits
    TYPE ENTRY_T IS RECORD
        valid     : STD_LOGIC;
        order_ref : UNSIGNED(31 DOWNTO 0);
        book      : UNSIGNED(2 DOWNTO 0);
        side      : STD_LOGIC;
        position  : POSITION_T;
        index     : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        shares    : UNSIGNED(31 DOWNTO 0);
    END RECORD;
    CONSTANT ENTRY_WIDTH : NATURAL := 1 + 32 + 3 + 1 + 2 + LADDER_BITS + 32;   -- 84
    CONSTANT ROW_WIDTH   : NATURAL := WAYS * ENTRY_WIDTH;                        -- 672

    TYPE BUCKET_T IS ARRAY (0 TO WAYS - 1) OF ENTRY_T;   -- slot i at row bits (i+1)*84-1 DOWNTO i*84

    -- order table → book engine
    TYPE OT_RESULT_T IS RECORD
        valid         : STD_LOGIC;
        change        : CHANGE_T;              -- NONE for unknown refs, failures, duplicates
        book          : UNSIGNED(2 DOWNTO 0);
        side          : STD_LOGIC;
        position      : POSITION_T;
        index         : UNSIGNED(LADDER_BITS - 1 DOWNTO 0);
        shares        : UNSIGNED(31 DOWNTO 0); -- shares added or removed
        order_gone    : STD_LOGIC;             -- the whole order left the table
        book_stale    : STD_LOGIC;             -- this book's stale bit after this operation
        stale_all     : STD_LOGIC;
        record_enable : STD_LOGIC;
        seq           : UNSIGNED(63 DOWNTO 0);
        timestamp     : UNSIGNED(47 DOWNTO 0);
        t_arrival     : UNSIGNED(15 DOWNTO 0);
    END RECORD;

    -- book engine → output stage
    TYPE TOP_T IS RECORD
        valid         : STD_LOGIC;
        book          : UNSIGNED(2 DOWNTO 0);
        flags         : STD_LOGIC_VECTOR(7 DOWNTO 0);   -- bit 0 stale, bit 1 top outside window
        bid_price     : UNSIGNED(31 DOWNTO 0);
        bid_shares    : UNSIGNED(31 DOWNTO 0);
        ask_price     : UNSIGNED(31 DOWNTO 0);
        ask_shares    : UNSIGNED(31 DOWNTO 0);
        record_enable : STD_LOGIC;
        seq           : UNSIGNED(63 DOWNTO 0);
        timestamp     : UNSIGNED(47 DOWNTO 0);
        t_arrival     : UNSIGNED(15 DOWNTO 0);
    END RECORD;

    -- decoder → event delay → output stage
    TYPE EVENT_T IS RECORD
        valid     : STD_LOGIC;
        flags     : STD_LOGIC_VECTOR(7 DOWNTO 0);   -- bit 0 stale, bit 2 gap, bit 3 FCS, bit 4 unusable
        seq       : UNSIGNED(63 DOWNTO 0);
        timestamp : UNSIGNED(47 DOWNTO 0);
    END RECORD;

    -- configuration
    TYPE TICKER_ARRAY_T IS ARRAY (0 TO NUM_BOOKS - 1) OF STD_LOGIC_VECTOR(63 DOWNTO 0);  -- first letter in 63 DOWNTO 56
    TYPE BASE_ARRAY_T   IS ARRAY (0 TO NUM_BOOKS - 1) OF UNSIGNED(31 DOWNTO 0);          -- window start, cents

    -- conversions (first field = most significant bits)
    FUNCTION to_slv(e : ENTRY_T) RETURN STD_LOGIC_VECTOR;           -- 84 bits
    FUNCTION to_entry(v : STD_LOGIC_VECTOR) RETURN ENTRY_T;
    FUNCTION to_slv(b : BUCKET_T) RETURN STD_LOGIC_VECTOR;          -- 672 bits
    FUNCTION to_bucket(v : STD_LOGIC_VECTOR) RETURN BUCKET_T;
    -- and to_slv / to_<record> for TOKEN_T, OP_T, OT_RESULT_T, TOP_T, EVENT_T (testbench wrappers)
```

### 3.5 Record packing

Byte `j` of the 32-byte record is in bits `8·j + 7 DOWNTO 8·j` of the 256-bit bus, with the
byte order of `record.py` (big-endian fields):

| Bytes | Field |
| --- | --- |
| 0 | book id (0xFF for status records) |
| 1 | flags |
| 2–7 | timestamp, 48 bits |
| 8–15 | sequence number |
| 16–19 / 20–23 | bid price / bid shares |
| 24–27 / 28–31 | ask price / ask shares |

`itch_pkg` provides `pack_record(book, flags, timestamp, seq, bid_price, bid_shares, ask_price,
ask_shares) RETURN STD_LOGIC_VECTOR` (256 bits); the testbench decodes with `record.py`.

### 3.6 Hash masks

`scripts/gen_hash_pkg.py` writes `rtl/itch_hash_pkg.vhd` with the two H3 mask sets (16 × 64
bits each, seeds 0x1DC0FFEE and 0x2BADBEEF) from the same Python code the model uses:

```vhdl
PACKAGE itch_hash_pkg IS
    TYPE MASK_ARRAY_T IS ARRAY (0 TO 15) OF STD_LOGIC_VECTOR(63 DOWNTO 0);
    CONSTANT H3_MASK_0 : MASK_ARRAY_T := (x"B73E…", …);
    CONSTANT H3_MASK_1 : MASK_ARRAY_T := (x"EA62…", …);
END PACKAGE;
```

Bucket bit `i` of hash `t` is `XOR (ref64 AND H3_MASK_t(i))` (the VHDL-2008 reduction `XOR`),
computed over the **full 64-bit ref** from the message, so refs with high bits set still hash
consistently.

### 3.7 Counters

One constant per counter, in register-map order (§7) and in the order of `COUNTERS` in
`itch/hw_model.py`; the register offset is `0x100 + 4 × index`:

```vhdl
    CONSTANT CNT_DROPPED_FRAMES    : NATURAL := 0;
    CONSTANT CNT_MALFORMED_PACKETS : NATURAL := 1;
    ...
    CONSTANT CNT_EVENTS            : NATURAL := 15;
    CONSTANT NUM_COUNTERS          : NATURAL := 16;

    TYPE COUNTER_ARRAY_T IS ARRAY (0 TO NUM_COUNTERS - 1) OF UNSIGNED(31 DOWNTO 0);
```

`itch_core` wires each module's `<counter>_pulse` to bit `CNT_<counter>` of the `counters`
input. Sources of the counters without their own module port elsewhere: `MESSAGES` and `EVENTS`
from the decoder, `RECORDS` from the output stage.

## 4. Modules

Latency is counted from the module's input valid to its output valid.

| Module | In | Out | Latency | Notes |
| --- | --- | --- | --- | --- |
| `packet_parser` | MAC AXI4-Stream (64-bit) | payload AXI4-Stream, `m_fcs_error`, pulses | 0 | ports in architecture §5.1 |
| `framer` | payload stream, `m_fcs_error` | `TOKEN_T` | 1 | §8.1 |
| `decoder` | `TOKEN_T` | `OP_T`, `EVENT_T` | 1 | symbol filter, `h3_hash` ×2 (ref, new ref) |
| `price_unit` | token (price bytes), book from the decoder, `cfg_base` | `PRICE_INFO_T` | 4 from the token | §8.3; ready for the order table's update stage |
| `order_table` | `OP_T`, `PRICE_INFO_T` | `OT_RESULT_T` | 4 | stages read, fetch, match, update; 2 × `ot_bank`, `ot_stash` |
| `book_engine` | `OT_RESULT_T` | `TOP_T` | 7 | stages level read … result; 16 × `ladder_side`, one operation per cycle in total |
| `output_stage` | `TOP_T`, delayed `EVENT_T` | `m_rec`, `m_evt` (256-bit) | 1 | last-emitted register per book |
| `event_delay` | `EVENT_T` | `EVENT_T` | `EVENT_DELAY` | shift register |
| `counters` | `_pulse` strobes | `COUNTER_ARRAY_T` | — | §3.7; saturating, cleared by reset |
| `regs_axil` | AXI4-Lite | configuration, control, counter reads | — | §7 |
| `itch_core` | MAC stream, `cfg_*` ports | `m_rec`, `m_evt`, `counts`, `book_stale`, `stale_all`, `busy` | 14 (U, D right after U: 15) | instantiates everything but the register block |
| `itch_top` | XGMII in, AXI4-Lite | `m_rec`, `m_evt`, `rx_frame_end`, XGMII out (`tx_clk`) | 16 (U: 17) from the XGMII word | `xgmii_rx` + `itch_core` + `regs_axil` + `record_tx`; `rx_frame_end` (last beat of each frame) is for the ILA |
| `record_tx` | `m_rec`, `m_evt` (core clock) | XGMII (transmit clock) | — | §11.8a: merge, `record_fifo`, `record_packer`, `xgmii_tx` |
| `record_fifo` | 256-bit records (write clock) | records (read clock), level | — | asynchronous, Gray pointers through 2 `ASYNC_REG` registers; write while full dropped and flagged |
| `record_packer` | FIFO level and data | frame stream to `xgmii_tx` | — | up to 32 waiting records per frame, header computed in one cycle before the frame |
| `xgmii_rx` | 64-bit XGMII (`rx_mii_d`, `rx_mii_c`) | MAC stream | 2 | §8.8; inside `itch_top`, reset by `rst` only |
| `xgmii_tx` | frame stream with `tready` | 64-bit XGMII (`tx_mii_d`, `tx_mii_c`) | 1 | §8.9; in `record_tx` |

Ports: `clk`, `rst`, then inputs, then outputs; configuration inputs are prefixed `cfg_`;
counter strobes are outputs named `<counter>_pulse`.

```vhdl
ENTITY framer IS
    PORT (
        clk, rst        : IN  STD_LOGIC;
        s_tdata         : IN  STD_LOGIC_VECTOR(63 DOWNTO 0);
        s_tkeep         : IN  STD_LOGIC_VECTOR(7 DOWNTO 0);
        s_tvalid        : IN  STD_LOGIC;
        s_tlast         : IN  STD_LOGIC;
        s_fcs_error     : IN  STD_LOGIC;
        cycle_count     : IN  UNSIGNED(15 DOWNTO 0);   -- free-running, for t_arrival
        token           : OUT TOKEN_T;
        drop_pulse      : OUT STD_LOGIC;               -- frame cut inside the MoldUDP64 header
        malformed_pulse : OUT STD_LOGIC
    );
END ENTITY;

ENTITY decoder IS
    PORT (
        clk, rst        : IN  STD_LOGIC;
        cfg_ticker      : IN  TICKER_ARRAY_T;
        token           : IN  TOKEN_T;
        op              : OUT OP_T;
        event_out       : OUT EVENT_T;
        duplicate_pulse : OUT STD_LOGIC;
        gap_pulse       : OUT STD_LOGIC;
        unusable_pulse  : OUT STD_LOGIC;
        messages_pulse  : OUT STD_LOGIC;   -- a message in sequence (not a duplicate)
        events_pulse    : OUT STD_LOGIC;   -- an event was emitted
        all_books_stale : OUT STD_LOGIC    -- stale_all, for the STATUS register
    );
END ENTITY;

ENTITY price_unit IS
    PORT (
        clk        : IN  STD_LOGIC;
        cfg_base   : IN  BASE_ARRAY_T;
        token      : IN  TOKEN_T;        -- the decoder's input: price bytes by type
        op         : IN  OP_T;           -- the decoder's output: book, kind
        price_info : OUT PRICE_INFO_T    -- ready for the operation's update stage (held one more cycle for U_INSERT)
    );
END ENTITY;

ENTITY order_table IS
    GENERIC (
        TABLE_STYLE : STRING := "ultra"   -- "ultra" (URAM) or "block" (BRAM), passed to ram_sdp
    );
    PORT (
        clk, rst             : IN  STD_LOGIC;
        op                   : IN  OP_T;
        price_info           : IN  PRICE_INFO_T;
        result               : OUT OT_RESULT_T;
        insert_failure_pulse : OUT STD_LOGIC;
        ref_error_pulse      : OUT STD_LOGIC;
        unknown_ref_pulse    : OUT STD_LOGIC;
        inconsistent_pulse   : OUT STD_LOGIC;
        stash_insert_pulse   : OUT STD_LOGIC;
        off_ladder_pulse     : OUT STD_LOGIC;
        unrep_pulse          : OUT STD_LOGIC;
        book_stale           : OUT STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);   -- for the STATUS register
        busy                 : OUT STD_LOGIC                                    -- clearing sweep after reset
    );
END ENTITY;

ENTITY book_engine IS
    GENERIC (
        LADDER_STYLE : STRING := "block"   -- ladder memories: "block" (BRAM) or "ultra" (URAM)
    );
    PORT (
        clk, rst : IN  STD_LOGIC;
        cfg_base : IN  BASE_ARRAY_T;
        op       : IN  OT_RESULT_T;
        top      : OUT TOP_T;
        busy     : OUT STD_LOGIC
    );
END ENTITY;

ENTITY output_stage IS
    PORT (
        clk, rst     : IN  STD_LOGIC;
        top          : IN  TOP_T;
        event_in     : IN  EVENT_T;      -- already delayed
        m_rec_tdata  : OUT STD_LOGIC_VECTOR(255 DOWNTO 0);
        m_rec_tvalid : OUT STD_LOGIC;
        m_evt_tdata  : OUT STD_LOGIC_VECTOR(255 DOWNTO 0);
        m_evt_tvalid : OUT STD_LOGIC;
        records_pulse : OUT STD_LOGIC      -- a book record left
    );
END ENTITY;

ENTITY counters IS
    PORT (
        clk               : IN  STD_LOGIC;
        rst               : IN  STD_LOGIC;
        pulses            : IN  STD_LOGIC_VECTOR(NUM_COUNTERS - 1 DOWNTO 0);  -- bit CNT_*; CNT_DROPPED_FRAMES: the parser's drops
        framer_drop_pulse : IN  STD_LOGIC;   -- frame cut inside the MoldUDP64 header
        busy_drop_pulse   : IN  STD_LOGIC;   -- frame arrived during the clearing sweep
        counts            : OUT COUNTER_ARRAY_T
    );
END ENTITY;

ENTITY itch_core IS
    GENERIC (
        TABLE_STYLE  : STRING := "ultra";   -- order table memories
        LADDER_STYLE : STRING := "block"    -- ladder memories
    );
    PORT (
        clk, rst                                  : IN  STD_LOGIC;
        cfg_group_ip                              : IN  STD_LOGIC_VECTOR(31 DOWNTO 0);
        cfg_port                                  : IN  STD_LOGIC_VECTOR(15 DOWNTO 0);
        cfg_ticker                                : IN  TICKER_ARRAY_T;
        cfg_base                                  : IN  BASE_ARRAY_T;
        s_tdata, s_tkeep, s_tvalid, s_tlast, s_tuser   -- from the MAC, as packet_parser
        m_rec_tdata, m_rec_tvalid, m_evt_tdata, m_evt_tvalid   -- architecture 9.8
        counts                                    : OUT COUNTER_ARRAY_T;
        book_stale                                : OUT STD_LOGIC_VECTOR(NUM_BOOKS - 1 DOWNTO 0);
        stale_all                                 : OUT STD_LOGIC;
        busy                                      : OUT STD_LOGIC
    );
END ENTITY;
```

**Dropped frames have three sources** (packet parser, framer, frames arriving while busy). Their
pulses belong to different frames and can come in the same cycle (the framer's pulse for frame N
near its end, the parser's for frame N+1 in its header beats), so `counters` adds them: that
counter grows by 0 to 3 per cycle. Every other counter has one source.

**Frames during the sweep** are dropped in `itch_core`, before the parser: a frame whose first
beat arrives while `busy` is high is dropped whole, also if `busy` ends before its last beat,
and counted once (`busy_drop_pulse` at its first beat). The parser never sees it, so it counts
nothing for it, not even a bad FCS.

## 5. Memories

Use the two generic entities everywhere, so latency is the same in every instance.

**`ram_sdp`: simple dual-port** (one read port, one write port; that is all any memory in this
design needs).

```vhdl
ENTITY ram_sdp IS
    GENERIC (
        WIDTH   : POSITIVE := 72;
        DEPTH   : POSITIVE := 512;
        LATENCY : POSITIVE := RAM_LATENCY;
        STYLE   : STRING   := "block"      -- "block" (BRAM) or "ultra" (URAM)
    );
    PORT (
        clk           : IN  STD_LOGIC;
        read_address  : IN  UNSIGNED(clog2(DEPTH) - 1 DOWNTO 0);
        read_data     : OUT STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0);   -- LATENCY cycles after the address
        write_enable  : IN  STD_LOGIC;
        write_address : IN  UNSIGNED(clog2(DEPTH) - 1 DOWNTO 0);
        write_data    : IN  STD_LOGIC_VECTOR(WIDTH - 1 DOWNTO 0)
    );
END ENTITY;
```

The read goes through a chain of `LATENCY` registers (`read_pipe(1 TO LATENCY)`): the memory
read register, then `LATENCY − 1` output registers, the first of which Vivado places inside the
BRAM (DOB_REG) or URAM (OREG). Instances leave `LATENCY` at its default, so changing
`RAM_LATENCY` changes every memory. A write in cycle *c* is seen by a read presented in cycle
*c + 1*.

- After synthesis, check in the utilisation report that Vivado placed the output register
  inside the memory and used the intended memory type. Large URAM cascades may need a third
  register: then `RAM_LATENCY` becomes 3 and §6 applies.
- Never rely on what the memory returns when a read and a write hit the same address in the
  same cycle; forwarding covers that case. In simulation `ram_sdp` returns X for it, so a missing forwarding
  makes a test fail instead of passing on the simulator's old-value behaviour.

**`lutram`: asynchronous read** (for the L0 bitmap words): same ports, generics `WIDTH` and
`DEPTH`, `ram_style` `"distributed"`. `read_data` follows `read_address` in the same cycle; a
write takes effect at the end of its cycle, so a read of that address in the same cycle still
returns the old word (what the bitmap search timing relies on).

**Clearing.** BRAM, URAM and LUTRAM can't be reset. After `rst`, the order table and every
ladder side run a sweep that writes zero rows on their write port, one row per cycle, and
hold `busy` high (8,192 cycles for the base sizes; the bitmaps need 128). The core drops
frames while busy and counts them.

## 6. Pipelines and timing closure

**Conventions.** A stage is one register boundary. Data and valid move together, every cycle.
Write down for every memory: the stage that presents its address (read), the stage that uses
its data (`RAM_LATENCY` stages later), and the stage that writes it.

**When a path fails timing, adding a register changes these rules, and they must be updated
together:**

| Rule | Formula | Base value |
| --- | --- | --- |
| Write history depth (order table, ladder copy 1) | stages from read to write (operations can be 1 cycle apart) | 2 |
| Best-shares reverse forwarding (ladder copy 2) | operations 1 … (stages from level update to best read) cycles behind | 1 and 2 behind |
| Event delay | (record valid cycle) − (decoder output valid cycle) − 1 (the output stage's register) | 14 − 2 − 1 = 11 |
| Latency table | architecture §12 | 14 / 15 |

The checkers (§9.4) catch the outcome: a wrong history depth or delay shows up as a failed
assertion or a scoreboard mismatch in the hazard tests.

**Likely critical paths** and the standard fix:

| Path | Fix (and its cost) |
| --- | --- |
| Decoder: field mux → H3 XOR trees → register | compute the hashes in a second decoder cycle (+1 latency for every message; the event delay is unchanged, since events also leave the decoder a cycle later) |
| Order table update stage: history mux → 16 + 32 comparators → slot choice → row build | **done** (first synthesis: 28 levels, −0.9 ns): history hit bits in the fetch stage, lookup in a separate match stage (+1 latency) |
| Bitmap search: L1 encoder → LUTRAM read → L0 encoder | **done** (no latency cost): one search per side, so the side multiplexers left the path. If it is still too slow: two stages (+1 latency; reverse forwarding then covers operations 1 to 3 behind) |
| `price_unit` multiplier | **done**: the product is registered (DSP) before the cents are used; 4 cycles |

## 7. Register block (AXI4-Lite)

32-bit registers, in the core clock domain. On a Zynq the processor's AXI port reaches it
through a clock converter (on the ZCU102: the SmartConnect, §11.4); on other boards through
JTAG-to-AXI. The block only answers while its clock runs and `rst` is low: on the board, check
the Ethernet link before the first access (§11.6). Configuration must only be
changed while the core is held in reset, before the session.

| Offset | Name | Access | Meaning |
| --- | --- | --- | --- |
| 0x000 | CONTROL | W | bit 0: soft reset (self-clearing) |
| 0x004 | STATUS | R | bit 0 busy, bit 1 `stale_all`, bits 15:8 per-book stale |
| 0x008 | VERSION | R | constant |
| 0x010 | GROUP_IP | RW | feed multicast group |
| 0x014 | PORT | RW | feed UDP port (bits 15:0) |
| 0x018 | TX_CONTROL | RW | bit 0: record output on (§11.8a); reset 0 |
| 0x020 / 0x024 | TX_DST_MAC | RW | record frames' destination MAC: HI bits 15:0 = bytes 0–1, LO = bytes 2–5, first byte highest; reset `FF:FF:FF:FF:FF:FF` |
| 0x028 / 0x02C | TX_SRC_MAC | RW | their source MAC, the same way; reset `02:00:00:00:00:02` |
| 0x030 | TX_DST_IP | RW | reset 255.255.255.255 |
| 0x034 | TX_SRC_IP | RW | reset 10.0.0.2 |
| 0x038 | TX_PORTS | RW | bits 31:16 source port, 15:0 destination port; reset 5000, 5000 |
| 0x0C0 | RECORDS_DROPPED | R | records the record output had no room for; cleared only by `rst`, not by the soft reset |
| 0x040 + 8·i | TICKER_HI[i] | RW | ticker i, characters 0–3 (character 0 in bits 31:24) |
| 0x044 + 8·i | TICKER_LO[i] | RW | ticker i, characters 4–7, space-padded |
| 0x080 + 4·i | BASE[i] | RW | window start of book i in cents, ≥ 100 |
| 0x100 | DROPPED_FRAMES | R | |
| 0x104 | MALFORMED_PACKETS | R | |
| 0x108 | FCS_ERRORS | R | |
| 0x10C | DUPLICATES | R | |
| 0x110 | GAPS | R | |
| 0x114 | UNUSABLE_MESSAGES | R | |
| 0x118 | INSERT_FAILURES | R | |
| 0x11C | REF_ERRORS | R | |
| 0x120 | UNKNOWN_REFS | R | |
| 0x124 | INCONSISTENT | R | |
| 0x128 | OFF_LADDER_ORDERS | R | inserts below or above the window |
| 0x12C | UNREP_PRICES | R | inserts with an unrepresentable price inside the window |
| 0x130 | STASH_INSERTS | R | |
| 0x134 | MESSAGES | R | messages in sequence |
| 0x138 | RECORDS | R | |
| 0x13C | EVENTS | R | |
| 0x200… | latency histogram | R | later |

All counters are 32-bit, saturating, cleared by reset.

Details (`regs_axil`):

- **Address:** 12 bits, word-aligned (bits 1:0 ignored). AXI4-Lite without `prot` signals.
  Address and data of a write may come in either order; a new write is accepted only after the
  previous response has been taken.
- **Responses:** a write to a read-only or unmapped address and a read of an unmapped address
  get SLVERR and change nothing. CONTROL reads 0. Byte strobes apply to every RW register.
- **Soft reset:** writing 1 to CONTROL bit 0 gives `itch_core` a one-cycle reset (`rst OR
  soft_reset` in `itch_top`). The sweep then runs by itself; software polls STATUS bit 0 until
  it is 0. The soft reset clears the counters and the core's state, not the configuration.
- **Reset values** (by `rst` only): GROUP_IP and PORT = the default feed (233.54.12.111, 26477),
  tickers = 8 spaces (match no R message, so all books stay empty), bases = 100.
- **VERSION** is the generic `VERSION` of `itch_top` (default `x"20261020"`), set at synthesis.
- Configuration writes take effect at once, also while the core runs; software must only
  configure before the session (then soft reset).

## 8. Details per module

These fill in what `architecture.md` leaves open.

### 8.1 Framer

State, per frame:

| Register | Meaning |
| --- | --- |
| `beat` | beat number within the frame (counts payload beats from beat 5) |
| `packet_seq`, `count` | from the header (beat 7) |
| `index` | messages emitted so far in this packet |
| `need` | bytes still missing of the current message; 0 = next is a length prefix |
| `prefix_hi`, `prefix_half` | first byte of a length prefix split across beats |
| `buffer_sel` | which assembly buffer the current message uses |
| `ignore` | set by a malformed packet: drop the rest of this payload |

Per beat, give every lane a role: (rest of the current message), (length prefix), (start of the
next message), or (ignored). Because there is only one message end per beat, the roles are:
the first `need` lanes, then 2 prefix lanes, then the rest. The next message's length is read
from the prefix lanes in the same cycle (or from `prefix_hi` plus lane 0). The checker asserts
that no second message ends in the same beat.

Messages longer than 40 bytes: bytes 40 and up are not written, the message still gets a token
(`len` tells the decoder), and still counts for `index`.

The FCS token: `s_fcs_error` goes through a fixed 3-cycle delay (two registers and the token
register), unless the framer dropped that frame. The frame's last message token comes at most 1
cycle after the frame's last beat, so the FCS token is always at least 2 cycles after it.

Token bytes beyond the message length are 0 (the model pads `data` with zeros): a new message
clears the rest of its buffer when it starts.

### 8.2 Decoder

- Sequence check, length check, R learning and the symbol filter all use the incoming token in
  the same cycle; their results are registered together.
- **U:** emit U_DELETE now and hold U_INSERT in a register for the next cycle, with the U's
  `stale_all` snapshot and `t_arrival`.
- **Spacing hold:** an operation that would leave in the cycle right after U_INSERT (only a D
  can: it ends 2 beats after a U that ends in lane 0–2) goes into the register `held` and
  leaves one cycle later, with its own `seq`, `timestamp`, `t_arrival` and `stale_all`. No message
  token can arrive while `held` is valid (asserted in the decoder): the held D ended in lane 5–7,
  the next block ends 2 or more beats later. The checkers (decoder, order table, core) assert
  that two operations in consecutive cycles are always U_DELETE then U_INSERT.
- Gap, FCS and unusable messages set `stale_all`; an `EVENT_T` is emitted only on its 0 → 1
  change, with the flags of the cause.
- `ref_error` = any of bits 63:32 of the message's ref set. The hash uses all 64 bits.

### 8.3 Price unit

It starts from the token, in parallel with the decoder: the price bytes sit at a fixed offset
per type (A, F: 32; U: 31). Cycle 1 reads the price, cycle 2 multiplies by 0x51EB851F
(registered, DSP) and takes the book from the decoder's output, cycle 3 forms
`cents = product >> 37` and whether `100 × cents = price`, cycle 4 gives position and index from
`cfg_base(book)` as in architecture §7.4. The result arrives exactly when its operation reaches
the order table's update stage. For a U, it is held one more cycle for U_INSERT's update stage.
For other operations it is ignored.

### 8.4 Order table

- **Lookup:** compare `order_ref` with all 16 bucket slots (valid only) and all stash entries
  in parallel. More than one hit can't happen; the checker asserts it.
- **Insert:** count valid slots in each bucket. Choose T0 if its count ≤ T1's, else T1; if the
  chosen count is 8, the stash; the free slot is the lowest-numbered invalid slot; in the stash,
  the lowest-numbered free entry.
- **Write only when a row changes**, and push only real writes into the history.
- `ref_error` on an INSERT or U_INSERT: no insert, `ref_error_pulse`, book stale. On a lookup:
  treated as an unknown ref.
- Per-book stale bits (8 registers) live here; `book_stale` in the result is the bit after the
  operation.
- **Result without a change** (unknown ref, failed or ignored insert): `book` of the operation,
  side bid, position inside, index 0, shares 0. A found REMOVE, DELETE or U_DELETE reports the
  stored order's book and side, also when the message's locate names another book (not counted).
- **Write history** (`ot_bank`): the writes of the last `RAM_LATENCY` cycles, one entry per
  cycle with a valid bit; the newest entry whose bucket matches replaces the memory row in the
  match stage. The bucket compares happen a cycle earlier (port `next_bucket`: the operation one
  stage before match) against the write of that cycle and the history; only the hit bits are
  registered. The sweep's writes go through it too (empty rows, so harmless).
- **Match / update split** (architecture §8.2): the match stage registers the forwarded rows,
  the 16 slot hits, the stash hits, index and entry, and a REMOVE's arithmetic for every slot
  and the stash entry: shares left, all used up, more than there are. The update stage only
  selects them; no carry chain is left in it. The update stage uses them, except for
  U_INSERT after a U_DELETE on the same bucket: then it takes the bank's `previous_write_row`,
  ANDs the hits with that row's valid bits (and the stash hits with the stash's current valid
  bits), and counts free slots on that row. The stash's free entry always comes from its
  current registers. (The code applies the hand-over to every operation; only U_INSERT can
  see a difference.) Hits are only dropped there, never added, so an operation 1 cycle after
  an insert would miss it: the decoder's spacing hold (§8.2 decoder) prevents that, and the
  checker asserts the spacing: two operations in consecutive cycles are always U_DELETE then
  U_INSERT.
- **Memory style:** generic `TABLE_STYLE`, default `"ultra"` (URAM); set `"block"` for a device
  without URAM (ZCU102). Simulation ignores it.
- **Busy:** operations that arrive during the sweep are dropped at the input (`op_delay(1).valid`
  is 0); the core drops frames while busy, so none should arrive.

### 8.5 Book engine

- The operation selects one of 16 `ladder_side` instances by `(book, side)`; each has its own
  memories, bitmaps, counters and best registers. The pipeline control and the forwarding logic
  can be shared.
- Inside positions use the ladder; other positions only update the counters (+1 on an insert,
  −1 when `order_gone`).
- The record needs both sides: the other side's best registers are read in the result stage.
  No later operation can have changed them yet (they update in the result stage of their own
  operation, which comes later), so no forwarding is needed.
- Prices in the result: `(cfg_base(book) + index) × 100`, or the empty values.
- **One shared pipeline** (`book_engine`) over 16 `ladder_side` instances, which hold the
  storage and their own `bitmap_search` (generic `HIGHEST`: bids search for the highest level,
  asks for the lowest). Every side searches every cycle; the search stage only takes its side's
  result (14 bits). Searching only the selected side put two wide multiplexers (128-bit L1,
  64-bit L0 word) into the middle of the path: 29 levels, −0.2 ns after synthesis. Side number = `book * 2 + side`. Read addresses and write data are broadcast to all
  16; the side number selects the write enables and the read data. Stage numbers are constants
  (`AT_LEVEL_UPDATE = RAM_LATENCY`, `AT_SEARCH`, `AT_BEST_READ`, `AT_RESULT = 2 * RAM_LATENCY + 2`);
  `stages(k)` holds the operation k cycles after level read, with what it has collected
  (counter snapshot, search result, forwarded best shares).
- **Write history** (copy 1): the level writes of the last `RAM_LATENCY` cycles, one entry per
  cycle with (side, index, shares before, shares after); the newest match gives the old level.
  The same entries give reverse forwarding its `old` values: the write of the cycle before
  (1 behind) and the write of this cycle (2 behind); the earlier one wins.
- **Counter snapshot:** `ladder_side` outputs `occupied` = counters > 0 including this cycle's
  change, so the level-read stage takes the snapshot directly. Counters are 18 bits (more than
  the order table's 131,104 entries).
- **Best registers** of the operation's side are written in the result stage of every
  operation, also when nothing changed (the search ran anyway).
- Levels add and subtract modulo 2^32, like the model; the checker asserts a level never goes
  below zero, and that every search result is the highest (bid) or lowest (ask) level that its
  own copy of the ladders holds.

### 8.6 Output stage

Compare `(bid, ask, flags(1 DOWNTO 0))` with the book's last-emitted register; emit if
different and `record_enable`. Status records come from `event_delay` on their own channel.

### 8.7 Event delay

A shift register of `EVENT_T`, `EVENT_DELAY` registers long, between the decoder's output
(valid in cycle 2 of the message) and the output stage, whose own register makes the status
record valid in cycle 2 + 11 + 1 = 14: the cycle in which an A's record would leave. Recompute
it whenever a stage's latency changes (§6).

### 8.8 XGMII receive (`xgmii_rx`)

The MAC's receive half, on top of the PCS of the 10G/25G Ethernet Subsystem (PCS/PMA only,
no MAC licence). It runs on the subsystem's `rx_clk_out_0`, like `itch_top`.

- **Frame boundaries:** a frame starts 7 bytes (preamble, SFD) after a start character `FB` in
  lane 0 or 4 and ends at the first control character. Only `FD` (terminate) ends it cleanly;
  any other (`FE` error, `07` idle, a new `FB`) cuts it and makes it bad. A new start is taken
  in any state, so a cut frame doesn't swallow the next one. Frames with no bytes give nothing.
- **Realignment:** for a lane-4 start the beat is `{word lanes 0-3, previous word lanes 4-7}`;
  every other step is the same for both cases on this 8-byte view.
- **Look-ahead:** a frame that ends exactly at the end of a view is only known as ended when the
  next view's lane 0 is a control character (lane 0 of the input word, lane 4 for a lane-4
  frame). Hence the previous-word register: 2 cycles from the word holding a beat's first byte
  to that beat.
- **FCS:** stays in the frame; `packet_parser` drops everything after the IP packet. `m_tuser`
  = 1 on the last beat if the CRC-32 over all bytes including the FCS isn't the residue
  `0xDEBB20E3` (register value, no final inversion), or the frame was cut. The CRC over the
  last view is computed for 1 … 8 bytes in parallel and selected by the byte count.
- **Reference:** `tb/common/xgmii.py` (encoder and `decode`); the test also checks `decode`
  against the encoded frames.

### 8.9 XGMII transmit (`xgmii_tx`)

The mirror of `xgmii_rx`, for the record output. States: waiting → start word → data →
(tail word) → gap.

- **Input:** AXI4-Stream with `tready`, a frame of at least 60 bytes without FCS, contiguous
  from lane 0. `tready` is 1 only while data beats are taken: not in the start word's cycle,
  not during the tail word and the gap.
- **Start:** always lane 0: `FB 55 55 55 55 55 55 D5` in one word, then the data.
- **End:** on the last beat with `n` bytes, the word holds the data, the 4 FCS bytes
  (`NOT crc`, byte 0 = bits 7:0) and `FD` if `n + 4 ≤ 7`; otherwise the FCS rest, `FD` and idle
  go out in a tail word. The CRC over the last beat is computed for its byte count in the same
  cycle, as in `xgmii_rx`.
- **Gap:** at least 12 idle bytes before the next start: 1 idle word if the terminate is in
  lanes 0–3, else 2 (12–19 idle bytes back to back).
- **Underrun:** `tvalid` = 0 inside a frame cuts it with `FE` (the receiver sees a bad frame);
  a simulation warning reports it. The record packer never does this.
- **Reference:** the test decodes the wire with `tb/common/xgmii.decode` and checks bytes, FCS,
  start lane and gap lengths.

## 9. Testbenches

### 9.1 Shared library (`tb/common/`)

| File | Contents |
| --- | --- |
| `runner.py` | `run()`: builds and runs one testbench from a pytest function, on Questa or nvc (`SIM`) |
| `axis.py` | 64-bit AXI4-Stream driver (optional `tvalid` gaps, X on `tdata` while idle, `tuser` on the last beat) and monitors: `AxisMonitor` (frames, any `tkeep` pattern), `StreamMonitor` (cycle and value of every valid beat) |
| `structs.py` | pack/unpack for every `itch_pkg` record, field order = the package's `to_slv` order |
| `axil.py` | AXI4-Lite master: random gaps between the channels, checks that a response stays stable until it is taken |
| `scoreboard.py` | compares two streams, stops at the first mismatch and prints the surrounding items; dict items are compared on the expected item's fields only (the model has no `t_arrival`) |
| `vectors.py` | loads `data/vectors/<case>.pcap`, `.json` and the expected records (`$ITCH_VECTORS` overrides the directory) |
| `taps.py` | runs the hardware model and returns each stage's stream as field dicts (§9.3); `record_from_bus` |

**Timing convention** in every testbench: inputs are driven after the falling edge, outputs are
sampled right after the rising edge (the values that edge registers).

### 9.2 Per-module test

cocotb drives a **wrapper** per module (`sim/wrappers/<module>_tb_top.vhd`). It instantiates the
module and its checker, and turns record ports into `STD_LOGIC_VECTOR` ports with the package's
`to_slv` functions, because flat vectors are what cocotb handles reliably. Arrays of configuration
(tickers, bases) are split out of their flat port in **one process** with a loop, never in a
`FOR GENERATE` of slice assignments: Questa 2019.2 left some generate slices at `'U'` after
cocotb wrote the port. Modules without
record ports (`ram_sdp`, `lutram`) are tested directly.

Each `tb/<module>/test_<module>.py` holds the cocotb tests and a launcher function that
builds and runs them through `tb/common/runner.py` (cocotb's Python runner; no Makefiles). Run
the file with Python; pytest is not needed for the hardware tests:

```python
def test_packet_parser():
    run("packet_parser_tb_top",
        [RTL / "itch_pkg.vhd", RTL / "packet_parser.vhd",
         CHECKERS / "packet_parser_chk.vhd", WRAPPERS / "packet_parser_tb_top.vhd"],
        __file__)


if __name__ == "__main__":
    test_packet_parser()
```

```
python3.11 tb/packet_parser/test_packet_parser.py            (Questa, the default)
SIM=nvc python3.11 tb/packet_parser/test_packet_parser.py
```

The launcher is named `test_<module>` so that `python3.11 -m pytest` also finds it: that runs
the model tests and every testbench in one go. Generics are passed as `parameters` (for example
`ram_sdp`, run with `LATENCY` 2 and 3). The build goes to `tb/<module>/sim_build/`.

### 9.3 References per stage

The hardware model provides each stage's expected output, so every module is tested on its
own:

| Module | Reference |
| --- | --- |
| `packet_parser` | `framing.frame_payload` per frame (accept, payload bytes) |
| `framer` | model tokens (from `read_packet`, with sequence numbers, heartbeats, FCS) |
| `decoder` | model operations and events |
| `order_table` | model order-table results for a given operation stream |
| `book_engine` | model tops for a given order-table result stream |
| `itch_core` | `expected.bin` and `expected_events.bin` |

### 9.4 Assertions

Each module has a checker entity `sim/checkers/<module>_chk.vhd`, instantiated only in the
wrapper. It watches the module's ports and, through VHDL-2008 **external names**, its internal
pipeline signals, for example:

```vhdl
ALIAS op_update IS << SIGNAL .order_table_tb_top.dut.op_update : OP_T >>;
...
ASSERT NOT (rec_valid = '1' AND other_rec_valid = '1')
    REPORT "one record per cycle violated" SEVERITY FAILURE;
```

What the checkers assert (named after architecture §3):

- **One message end per beat** (framer).
- **One book operation per cycle**, **one record per cycle**, and the **book-message spacing**
  (operations of different messages at least 2 cycles apart).
- Order table: at most one lookup hit; stash count ≤ `STASH`.
- Book engine: a level never negative; bitmap bit = (level ≠ 0) against a shadow array kept by
  the checker; the best index = highest (bid) or lowest (ask) set bit.
- Core: record cycle − last-byte cycle = 14 (U and a held D: 15) for every record.

### 9.5 How much to simulate

All vectors and directed tests for every module; for the order table and book engine also
random operation streams against the model; and the first 1–5 million messages of the
reference day for each module and end to end. The full day (about 270 million messages) is
checked on the hardware.

## 10. Order of work and definition of done

| Step | Work | Done when |
| --- | --- | --- |
| 0 | Hardware model (architecture §16) with stage taps; `gen_hash_pkg.py`; `structs.py` | **done** (2026-10-15): pytest passes; `compare_models.py` on the full reference day passes |
| 1 | `itch_pkg`, `tb/common`, `ram_sdp`, `lutram`, `counters` | **done** (2026-10-16, nvc): every record packs like `structs.py` field by field and round-trips; `pack_record` matches `record.py`; `ram_sdp` data comes exactly `LATENCY` cycles after the address for 2 and 3; `lutram` reads in the same cycle; counters saturate and sum the three drop sources. Still to run: the same tests on Questa |
| 2 | `packet_parser` | all frame vectors match `frame_payload`, pulses match the counters; checker clean; 1M-frame day sample |
| 3 | `framer` | tokens match on all vectors (`alignment`, `malformed_mold`, `frame_sizes`, `heartbeats`, `end_of_session`, `bad_fcs`) and on the day sample |
| 4 | `decoder`, `h3_hash`, `price_unit` | operations and events match; R learning, unusable messages, U split |
| 5 | `order_table` | hazard tests for order-table row read-after-write, U hand-over and stash in registers; colliding refs up to a failed insert; random streams; day sample |
| 6 | `book_engine` | hazard tests for ladder level read-after-write, best-shares reverse forwarding, bitmap search timing and flag snapshot; search and window edges; random streams; day sample |
| 7 | `output_stage`, `event_delay`, `itch_core` | records and events match end to end on all vectors (back to back and with gaps) and the day sample; counters and stale bits match the model; record order by sequence number; latency check clean; frames during the sweep dropped whole |
| 8 | `regs_axil`, `itch_top` | register map test (reset values, every RW register, byte strobes, SLVERR, a write offered while a response waits); vectors run with configuration, soft reset, counters and STATUS only over the bus |
| 9 | Synthesis and timing on the target device | **done** (2026-10-20, Vivado 2019.2, xczu7eg-ffvc1156-2-e, out of context): routed WNS +0.108 ns, no latches; 31,212 LUTs (13.6 %), 240 BRAM tiles (77 %), 40 URAM (42 %). Worst paths: order-table stash hit → `used_up`/`too_many` (12 levels), ladder read → L1 summary bit (14 levels, +0.197 ns) |
| 10 | MAC wrapper, board bring-up, replay | `xgmii_rx` **done** (2026-10-21, nvc): every frame length and end lane, starts in lanes 0 and 4 back to back, the vector frames, bad FCS, error and idle cuts, a start inside a frame, empty frames, ordered sets, reset; beat timing exact. Board: ZCU102 (ZU9EG, `TABLE_STYLE => "block"`), block design with Zynq MPSoC, SmartConnect, 10G/25G Ethernet Subsystem (PCS/PMA only), `itch_top` (XGMII in, `xgmii_rx` inside; top test drives XGMII), ILA. Still to do: block design, timing on the ZU9EG, record output, replay; done when records captured from the board match the model |

### 10.1 Bring-up (step 10, first part)

The board setup and the bring-up procedure are in §11. Order: link up → registers reachable →
vectors replayed and captured with the ILA, compared with `scripts/compare_ila.py` → then
`record_tx` for the full day.

## 11. Board setup (ZCU102)

The test setup for step 10. The block design is built by hand in Vivado 2024.2 (simulation and
the step 9 timing run were on 2019.2). The block design and the Ethernet subsystem's
configuration are not in the repository.

### 11.1 Hardware

| Part | What |
| --- | --- |
| Board | ZCU102, XCZU9EG-2FFVB1156E: 912 BRAM36, no URAM, so `itch_top` gets `TABLE_STYLE => "block"` (about 600 BRAM36) |
| Line | one SFP+ cage of the ZCU102, 10GBASE-R, to a PC with a 10G NIC (DAC cable or SR optics) |
| PC | Linux, `tcpreplay` plays the vector pcaps (and later the day) into the board |
| Debug | JTAG over USB: bitstream, ILA in the Hardware Manager, `xsct` |

### 11.2 Block design

| Block | Job |
| --- | --- |
| Zynq UltraScale+ MPSoC (`zynq_ultra_ps_e_0`) | ARM; AXI master (`M_AXI_HPM0_FPD`) for all registers; `pl_clk0` (100 MHz), `pl_resetn0`. HPM1 and the PL-to-PS interrupt switched off |
| AXI SmartConnect | 1 slave interface, 2 master interfaces, 2 clocks; routes by address and crosses from 100 MHz to the core clock |
| 10G/25G Ethernet Subsystem | PCS/PMA only (no MAC licence), 10G, one GT. Serial line ↔ XGMII (`mii_rx_0` out, `mii_tx_0` in) |
| `itch_top_0` (module reference, VHDL-2008) | `xgmii_rx` + `itch_core` + `regs_axil` + `record_tx` |
| Processor System Reset A, B, C | one synchronized reset per clock domain (§11.4) |
| ILA | records and frame ends (§11.5) |

Connections:

```
SFP+ ─ gt_serial_port ─► Ethernet subsystem ─ mii_rx_0 (rx_mii_d/c) ─► itch_top_0 rx_mii_d/c
                                    ▲ mii_tx_0 ◄─ itch_top_0 tx_mii_d/c (idle while the output is off)
Zynq M_AXI_HPM0_FPD ─► SmartConnect S00_AXI
                       SmartConnect M00_AXI ─► Ethernet subsystem s_axi_0
                       SmartConnect M01_AXI ─► itch_top_0 s_axi
itch_top_0 m_rec_*, m_evt_*, rx_frame_end ─► ILA
```

- **Top level:** the block design's HDL wrapper (`design_1_wrapper`, Set as Top), not
  `itch_top.vhd`; otherwise every port of `itch_top` becomes a pin and placement fails.
- **External ports:** only `gt_serial_port` and `gt_ref_clk`; pins from the board's constraints.
- **Outputs must have a load:** without the ILA on `m_rec_*`/`m_evt_*` Vivado removes the core.
- **Generics on `itch_top_0`:** `TABLE_STYLE = "block"`; `LADDER_STYLE = "block"`, `VERSION` as needed.
- **Constraints:** add `constraints/record_cdc.xdc` to the project (clock crossings of the
  record output; without it the crossings are timed as if the two clocks were related).

### 11.3 Clocks

| Clock | Source | Drives |
| --- | --- | --- |
| `pl_clk0`, 100 MHz | Zynq | `maxihpm0_fpd_aclk`, SmartConnect `aclk`, Ethernet `s_axi_aclk_0` and `dclk` (the IP's DRP/free-running clock setting must say 100 MHz), reset A |
| `gt_refclk_p/n`, 156.25 MHz | board oscillator | Ethernet `gt_ref_clk` |
| `rx_clk_out_0`, 156.25 MHz | Ethernet subsystem | Ethernet `rx_core_clk_0`, `itch_top_0` `clk`, SmartConnect `aclk1`, ILA, reset B |
| `tx_mii_clk_0`, 156.25 MHz | Ethernet subsystem | `itch_top_0` `tx_clk` (record output, after its clock-crossing FIFO), reset C |

The core runs on the receive clock: no crossing between XGMII and the core.

### 11.4 Resets

A reset must be released on an edge of the clock of the logic it resets, so one Processor
System Reset per clock domain:

| Block | Clock | Inputs | Outputs |
| --- | --- | --- | --- |
| A | `pl_clk0` | `ext_reset_in` ← `pl_resetn0` | `interconnect_aresetn` → SmartConnect `aresetn`; `peripheral_aresetn` → Ethernet `s_axi_aresetn_0`; `peripheral_reset` → Ethernet `sys_reset` |
| B | `rx_clk_out_0` | `ext_reset_in` ← `pl_resetn0` (active low, default); `aux_reset_in` ← `user_rx_reset_0` (set *Aux Reset Active High*) | `peripheral_reset` → `itch_top_0` `rst` |
| C | `tx_mii_clk_0` | `ext_reset_in` ← `pl_resetn0` (active low, default); `aux_reset_in` ← `user_tx_reset_0` (set *Aux Reset Active High*) | `peripheral_reset` → `itch_top_0` `tx_rst` |

`itch_top_0` leaves reset only when the ARM side is up and the Ethernet receive side is ready.
`slowest_sync_clk` is always the clock of the logic the block resets (the name comes from
MicroBlaze systems): the block releases its outputs on an edge of that clock, so all flip-flops
of that domain leave reset in the same cycle. `pl_clk0` (100 MHz, ARM PLL) and the Ethernet
clocks (156.25 MHz from the GT) are unrelated, so each domain needs its own block.
The soft reset (CONTROL bit 0) resets the core, not the configuration, not `xgmii_rx` and not
the record output. `record_tx` crosses `rst` and `tx_rst` into each other's domain, so either
one resets the record FIFO on both sides.

### 11.5 ILA

| Probe | Signal | Type |
| --- | --- | --- |
| probe0 | `m_rec_tdata` (256) | data |
| probe1 | `m_rec_tvalid` | data and trigger |
| probe2 | `m_evt_tdata` (256) | data |
| probe3 | `m_evt_tvalid` | data and trigger |
| probe4 | `rx_frame_end` | data and trigger |

Native, clock `rx_clk_out_0`, depth 4096, capture control on, 1 comparator per probe. In the
Hardware Manager: trigger and capture condition `probe1 OR probe3 OR probe4`, trigger position
0. A vector gives 50–500 records; the frame ends of the later replay loops (duplicates for the
core, no records) fill the buffer so the capture completes. `compare_ila.py` keeps only the
samples with a tvalid.

### 11.6 Address map and software access

| Slave | Base (Address Editor) | Size |
| --- | --- | --- |
| Ethernet subsystem `s_axi_0` | `0xA000_0000` | 4K (register map in PG210) |
| `itch_top_0` `s_axi` | `0xA001_0000` | 4K (§7) |

The bases are what the Address Editor assigned; check them there. Software on the ARM (Linux
`devmem` / a C program via `/dev/mem`, or bare-metal / `xsct` `mrd`/`mwr`):

1. Check the link in the Ethernet subsystem's status registers (RX block lock / RX status).
   Without link, `itch_top_0` can be held in reset (`user_rx_reset_0`) or lose its clock
   (`rx_clk_out_0`), and an access to it then hangs the bus.
2. Read VERSION (`0x008`): `itch_top`'s `VERSION` generic.
3. Write GROUP_IP, PORT, the 8 tickers and the 8 bases (§7), then CONTROL = 1 (soft reset).
4. Read STATUS until bit 0 (busy) is 0: the sweep takes 8,192 cycles (52 µs).
5. Replay; read the counters (`0x100`…) and STATUS.

`scripts/board_config.py` prints steps 2–4 as `devmem` or `xsct` commands for a `config.json`.
A C program for the ARM that does the same (and reads the counters) is the next software piece.

### 11.7 Bring-up order

1. **Link:** the PC's NIC reports link (`ethtool <nic>`: `Link detected: yes`, 10000Mb/s) and
   the Ethernet subsystem's status registers show RX block lock / RX status. Nothing else
   makes sense before this.
2. **ILA:** with link, replay a vector and check that `rx_frame_end` fires and records come out
   (per vector below).
3. **Registers:** if frames arrive but no records come out, read itch_top's counters and STATUS
   (§7): dropped frames, FCS errors, malformed packets, messages, records tell where in the
   chain they stop.

Per vector:

1. Configure as in §11.6 (`config.json` of the vectors).
2. Arm the ILA, then on the PC: `sudo tcpreplay -i <nic> --loop=20 data/vectors/<vector>.pcap`.
3. Hardware Manager Tcl console:
   `write_hw_ila_data -force -csv_file records.csv [upload_hw_ila_data hw_ila_1]`
4. `python3.11 scripts/compare_ila.py records.csv <vector> --loops 20`: `match`, or the first
   wrong record with its neighbours.

Expected records come from the model over the frames as the NIC sends them: good FCS (the NIC
computes it, so `bad_fcs` differs from its `.bin`) and padded to 60 bytes. Start with `clean`.
The full day doesn't fit an ILA: for that, the record output (§11.8a).

With the record output, per vector or pcap:

1. Configure as above, plus TX_CONTROL = 1 (the addresses keep their broadcast defaults unless
   written; change them only while TX_CONTROL = 0).
2. On the PC, before the replay: `sudo tcpdump -i <nic> -B 262144 -w records.pcap udp port 5000`
3. `sudo tcpreplay -i <nic> --loop=20 data/vectors/<vector>.pcap`, then stop tcpdump.
4. `python3.11 scripts/compare_capture.py records.pcap <vector> --loops 20`: frame counters
   without gaps, dropped records, then `match` or the first wrong record.

### 11.8a Record output over Ethernet

The ILA holds a few thousand samples; for full runs the records go back to the PC on the same
cable (10G is full duplex), as a debug and verification tap, not part of the latency path:

```
rx_clk_out_0 domain                  | tx_mii_clk_0 domain
m_evt, m_rec -> merge -> record_fifo -+-> record_packer -> xgmii_tx -> tx_mii_d/c -> PC
               (status    (512,        |
                first)     drops counted)
```

Frame (built by `record_packer`, taken apart by `itch/record_frames.py`): Ethernet (14 bytes),
IPv4 (20: DF, TTL 64, id = frame counter, checksum computed), UDP (8, checksum 0), batch header
(14: `0x4952`, frame counter 32 bits, record count 16, records dropped so far 32, `0x0000`),
then n × 32-byte records (§3.5 byte order). 56 header bytes = 7 beats, so every record is 4
whole beats. n = what is waiting when the frame starts, at most 32 (1,080 bytes); no timeout.

- **Design:** own async FIFO (512 records, Gray pointers), broadcast defaults
  (`FF:FF:FF:FF:FF:FF`, `255.255.255.255:5000`, source `02:00:00:00:00:02`, 10.0.0.2:5000),
  registers for all addresses (§7), TX_CONTROL default off, RECORDS_DROPPED. TX_CONTROL gates
  the FIFO writes: with the output off nothing is queued or dropped. Merge: a status record
  goes before a record of the same cycle, which waits one cycle (records are at least 2 cycles
  apart; an assertion checks it). The addresses cross into the transmit domain without a
  handshake: change them only while the output is off.
- **Status (2026-10-26, nvc):** `xgmii_tx`, `record_fifo`, `record_packer`, `record_tx` done
  and inside `itch_top`; `scripts/compare_capture.py` (tcpdump capture → records → model;
  frame-counter gaps = frames lost on the PC). Tests: FIFO with both clock ratios and full;
  `record_tx` with sparse and dense records, overflow (arrived + dropped = sent, full frames of
  32), output off; `itch_top` sends four vectors' records back, compared with the model.
  13 injected bugs caught.
- **Timing on the ZU9EG** (2026-10-27, Vivado 2024.2, `synth.tcl` out of context, order table in
  BRAM, with the record output): routed WNS +0.283 ns, no latches; 33,482 LUTs (12.2 %), 543 BRAM
  tiles (59.5 %). Worst path as on the ZU7: ladder read → L1 summary bit (15 levels).
- **Latency of the record output** (simulation, both clocks 6.4 ns): a record at `m_rec` in
  cycle 0 → start word on `tx_mii_d` in cycle 9 (merge 1, FIFO write 1, Gray sync 2–3, packer
  decision and header 2, `xgmii_tx` start 1) → 7 header words, 4 record words → terminate in
  cycle 21. From the XGMII word of a message's last byte: 16 + 21 = 37 cycles (237 ns) to the
  end of its record frame, before the PCS. Each further record in the frame adds 4 cycles.
- **`dropped_frames` on the board:** includes everything else on the link (the PC's ARP,
  IPv6, mDNS, LLDP). It is informational: the analysis requires the other counters to match
  the model exactly and `dropped_frames` ≥ the model's value. Foreign frames never produce
  records. A split into FOREIGN / DROPPED counters is possible later.
- **Record output on the board (2026-10-28):** all vectors replayed once and captured with
  tcpdump, `compare_all_captures.py`: 24 of 24 cases match the model record by record,
  including the status records of `gap`, `heartbeat_gap`, `late_join`, `reordered`,
  `two_gaps`, `unusable_messages`; no frames lost, nothing dropped. Not yet captured:
  `bad_fcs`, `min_frames`. Next: the reference day.
- **Whole day on the board (2026-10-29), first run:** 785,103 record frames, nothing dropped;
  the first 2,401 records match, then a mismatch at seq 546545 (QQQ): `U 317911 → 318355`
  followed directly by `D 318355` in the same packet. The U ended in lane 0–2, so the D's
  operation reached the order table 1 cycle after U_INSERT; its match stage compared against
  the row before U_INSERT's write and found nothing (unknown ref, book stale, 900 shares left in
  the book). Architecture §3 had assumed operations of different messages are always 2 cycles
  apart; the decoder tests spaced tokens by the current message's length, not the next one's,
  so they never produced the case. Fix (2026-10-30): the decoder holds such an operation one
  cycle (§8.2); the decoder test now spaces tokens by byte position (25 holds over the
  vectors), `tb/core` `delete_right_after_replace` covers every end lane and a D on a
  neighbour in the same row, the core checker asserts the spacing. Next: replay the day again.
- **First board results (2026-10-26):** link up, `clean` replayed 20 times: 524 records,
  2,068 messages, 39,292 duplicates (19 × 2,068), no gaps or errors, as the model expects.

### 11.8 Pitfalls seen so far

| Symptom | Cause | Fix |
| --- | --- | --- |
| Utilization shows only the Ethernet subsystem | the IP's own out-of-context run, or the core removed for lack of a load | look at `synth_1`/`impl_1` (`report_utilization -hierarchical`); ILA on the outputs |
| `[Place 30-58]` 690 IO ports for 328 pins | `itch_top.vhd` set as top | block design wrapper as top |
| No link, no `tx_mii_clk_0` | `dclk` not running | `dclk` ← `pl_clk0` |
| Garbage sent on the fibre | `tx_mii_d/c_0` unconnected (reads as data 00) | connect `itch_top_0` `tx_mii_d/c` (idle while the record output is off) |
| ARM hangs on a register read | `itch_top_0` in reset or `rx_clk_out_0` stopped (no link) | check the link first |
| Ethernet subsystem registers don't answer (read hangs or returns garbage) | `s_axi_aresetn_0` unconnected: the block design ties it to a constant | `s_axi_aresetn_0` ← reset A `peripheral_aresetn`; Validate Design lists unconnected reset pins as warnings |

