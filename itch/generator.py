"""Stimulus generator: small, self-checking test cases for the packet parser and sequence check.

A case is a list of Ethernet frames, each marked with whether the packet parser must accept
it, plus the model counters the case is built to produce. The expected records come from
running the model on the frames (scripts/make_vectors.py); the counters are stated here,
independently, so a case also checks the model.

Frame-level faults are built as decoys: a rejected frame carries the payload of the next
real packet, which is then sent correctly. A parser that wrongly accepts a decoy sees that
packet twice (duplicates); one that wrongly drops a good frame sees a gap. Either way the
output differs from the expected records.

Message content comes from Market, a small stateful source of valid ITCH messages: orders
on the tracked tickers near a fixed mid price (books never cross), plus filler traffic on
untracked tickers that the symbol filter must drop.
"""

import ipaddress
import random
import struct
from dataclasses import dataclass, field, replace

from itch.config import Config
from itch.framing import (
    DEFAULT_FEED,
    END_OF_SESSION,
    ETHERNET_HEADER,
    ETHERTYPE_IPV4,
    IPV4_HEADER,
    MAX_PAYLOAD,
    MIN_FRAME,
    MOLD_HEADER,
    MOLD_HEADER_SIZE,
    PAYLOAD_OFFSET,
    PROTOCOL_UDP,
    UDP_HEADER,
    Feed,
    MoldPacket,
    ipv4_checksum,
)
from itch.messages import (
    AddOrder,
    AddOrderMPID,
    OrderCancel,
    OrderDelete,
    OrderExecuted,
    OrderExecutedWithPrice,
    OrderReplace,
    StockDirectory,
    SystemEvent,
)

TICKERS = ["AAPL", "MSFT", "TSLA", "QQQ", "NFLX", "COST", "SIRI", "ZM"]
FILLER_TICKERS = ["IBM", "GE", "F"]  # announced but not tracked
SESSION = "ITCHFPGA01"
OPEN_NS = (9 * 3600 + 30 * 60) * 1_000_000_000  # 09:30, ns since midnight

# Counters every case states; any it doesn't mention must be 0.
# Counters every case states (the hardware model's, without the plain totals); any it doesn't
# mention must be 0.
COUNTERS = (
    "dropped_frames",
    "malformed_packets",
    "fcs_errors",
    "duplicates",
    "gaps",
    "unusable_messages",
    "insert_failures",
    "ref_errors",
    "unknown_refs",
    "inconsistent",
    "off_ladder_orders",
    "unrep_prices",
    "stash_inserts",
)

ETHERTYPE_ARP = 0x0806
ETHERTYPE_IPV6 = 0x86DD
ETHERTYPE_VLAN = 0x8100
PROTOCOL_ICMP = 1
PROTOCOL_TCP = 6
IP_RESERVED = 0x8000  # reserved flag bit: not checked
IP_DF = 0x4000  # don't fragment: allowed
IP_MF = 0x2000  # more fragments: a fragment


# Messages


@dataclass(slots=True)
class LiveOrder:
    ticker: str
    side: str
    price: int
    shares: int


class Market:
    """Valid ITCH messages for tracked and filler tickers, with order refs that stay consistent.

    Every method returns one raw message and advances the clock by step_ns. Methods that act
    on an existing order pick a random live one of the tracked tickers unless given a ref.
    """

    def __init__(self, tickers: list[str] = TICKERS, seed: int = 1, start_ns: int = OPEN_NS, step_ns: int = 1_000):
        self.rng = random.Random(seed)
        self.tickers = tickers
        self.time = start_ns
        self.step_ns = step_ns
        self.next_ref = 1
        self.next_match = 1
        all_tickers = tickers + FILLER_TICKERS
        self.locates = {ticker: 10 + i for i, ticker in enumerate(all_tickers)}
        self.mid = {ticker: (50 + 37 * i) * 10_000 for i, ticker in enumerate(all_tickers)}
        self.live: dict[int, LiveOrder] = {}

    def _header(self, ticker: str = "") -> tuple[int, int, int]:
        """Stock locate, tracking number, timestamp; the clock moves on by one step."""
        self.time += self.step_ns
        return self.locates.get(ticker, 0), 0, self.time

    def _pick(self, tracked: bool = True, min_shares: int = 1) -> int:
        refs = [
            ref for ref, order in self.live.items()
            if (order.ticker in self.tickers) == tracked and order.shares >= min_shares
        ]
        if not refs:
            raise LookupError("no live order to act on; add some first")
        return self.rng.choice(sorted(refs))

    def _price_near_mid(self, ticker: str, side: str) -> int:
        """1 to 5 cents below the mid for a bid, above it for an ask: books never cross."""
        cents = self.rng.randint(1, 5) * 100
        return self.mid[ticker] - cents if side == "B" else self.mid[ticker] + cents

    def tracked_live(self, min_shares: int = 1) -> int:
        return sum(order.ticker in self.tickers and order.shares >= min_shares for order in self.live.values())

    def forget(self, refs: list[int]) -> None:
        """Never act on these orders again, e.g. ones the receiver can't know about."""
        for ref in refs:
            self.live.pop(ref, None)

    # One message of each kind

    def system(self, code: str = "O") -> bytes:
        return SystemEvent(*self._header(), code).to_bytes()

    def directory(self, ticker: str) -> bytes:
        return StockDirectory(
            *self._header(ticker), ticker, "Q", "N", 100, "N", "C", "Z", "P", "N", "N", "1", "N", 0, " "
        ).to_bytes()

    def opening(self) -> list[bytes]:
        """Start of day: system event, then a directory entry for every ticker."""
        return [self.system("O")] + [self.directory(ticker) for ticker in self.tickers + FILLER_TICKERS]

    def add(
        self,
        ticker: str | None = None,
        side: str | None = None,
        price: int | None = None,
        shares: int | None = None,
        mpid: bool = False,
        tracked: bool = True,
        order_ref: int | None = None,
    ) -> bytes:
        if ticker is None:
            ticker = self.rng.choice(self.tickers if tracked else FILLER_TICKERS)
        side = side or self.rng.choice("BS")
        price = price or self._price_near_mid(ticker, side)
        shares = shares or self.rng.choice([1, 50, 100, 100, 200, 500])
        if order_ref is None:
            ref = self.next_ref
            self.next_ref += 1
        else:
            ref = order_ref
        self.live[ref] = LiveOrder(ticker, side, price, shares)
        if mpid:
            return AddOrderMPID(*self._header(ticker), ref, side, shares, ticker, price, "GSCO").to_bytes()
        return AddOrder(*self._header(ticker), ref, side, shares, ticker, price).to_bytes()

    def message(self, message_class, ticker: str, *fields) -> bytes:
        """Any message with the given fields after the header; Market's own state is not
        changed, so this can build messages that contradict it."""
        return message_class(*self._header(ticker), *fields).to_bytes()

    def _take(self, ref: int, shares: int) -> LiveOrder:
        order = self.live[ref]
        order.shares -= shares
        if order.shares == 0:
            del self.live[ref]
        return order

    def execute(self, ref: int | None = None, shares: int | None = None, with_price: bool = False) -> bytes:
        ref = ref or self._pick()
        shares = shares or self.rng.randint(1, self.live[ref].shares)
        order = self._take(ref, shares)
        match = self.next_match
        self.next_match += 1
        if with_price:
            return OrderExecutedWithPrice(*self._header(order.ticker), ref, shares, match, "Y", order.price + 1).to_bytes()
        return OrderExecuted(*self._header(order.ticker), ref, shares, match).to_bytes()

    def cancel(self, ref: int | None = None, shares: int | None = None) -> bytes:
        """A partial cancel: at least one share stays."""
        ref = ref or self._pick(min_shares=2)
        shares = shares or self.rng.randint(1, self.live[ref].shares - 1)
        order = self._take(ref, shares)
        return OrderCancel(*self._header(order.ticker), ref, shares).to_bytes()

    def delete(self, ref: int | None = None, tracked: bool = True) -> bytes:
        ref = ref or self._pick(tracked=tracked)
        order = self.live.pop(ref)
        return OrderDelete(*self._header(order.ticker), ref).to_bytes()

    def replace(self, ref: int | None = None, price: int | None = None, shares: int | None = None) -> bytes:
        ref = ref or self._pick()
        old = self.live.pop(ref)
        price = price or self._price_near_mid(old.ticker, old.side)
        shares = shares or self.rng.choice([50, 100, 300])
        new_ref = self.next_ref
        self.next_ref += 1
        self.live[new_ref] = LiveOrder(old.ticker, old.side, price, shares)
        return OrderReplace(*self._header(old.ticker), ref, new_ref, shares, price).to_bytes()

    # Mixes

    def of_type(self, msg_type: bytes) -> bytes:
        """One valid message of the given type, on a tracked ticker where the type has one.

        Raises LookupError if the type needs a live order and there is none to use.
        """
        if msg_type == b"S":
            return self.system("Q")  # start of market hours; the model ignores it
        if msg_type == b"R":
            return self.directory(self.rng.choice(FILLER_TICKERS))
        if msg_type == b"A":
            return self.add()
        if msg_type == b"F":
            return self.add(mpid=True)
        if msg_type == b"E":
            return self.execute()
        if msg_type == b"C":
            return self.execute(with_price=True)
        if msg_type == b"X":
            return self.cancel()
        if msg_type == b"D":
            return self.delete()
        if msg_type == b"U":
            return self.replace()
        raise ValueError(f"no generator for message type {msg_type!r}")

    def random(self) -> bytes:
        """A random valid message: mostly book activity on tracked tickers, some filler."""
        tracked_live = self.tracked_live()
        filler_live = len(self.live) - tracked_live
        roll = self.rng.random()
        if roll < 0.15 or (roll < 0.25 and filler_live < 20):
            return self.add(tracked=False)
        if roll < 0.25:
            return self.delete(tracked=False)
        if tracked_live < 10:
            return self.add(mpid=roll > 0.9)
        msg_type = self.rng.choice([b"A", b"A", b"A", b"F", b"E", b"E", b"C", b"X", b"D", b"D", b"U"])
        try:
            return self.of_type(msg_type)
        except LookupError:  # e.g. a cancel, but every order has a single share left
            return self.add()

    def batch(self, count: int) -> list[bytes]:
        return [self.random() for _ in range(count)]


# Frames


def raw_frame(
    feed: Feed,
    payload: bytes,
    *,
    ip_id: int = 0,
    dst_mac: bytes | None = None,
    ethertype: int = ETHERTYPE_IPV4,
    vlan: int | None = None,
    version_ihl: int | None = None,
    options: bytes = b"",
    dscp: int = 0,
    flags_offset: int = 0,
    ttl: int = 64,
    protocol: int = PROTOCOL_UDP,
    checksum: int | None = None,
    checksum_xor: int = 0,
    src_ip: str | None = None,
    dst_ip: str | None = None,
    src_port: int | None = None,
    dst_port: int | None = None,
    ip_length_delta: int = 0,
    udp_length: int | None = None,
    udp_length_delta: int = 0,
    udp_checksum: int = 0,
    trailing: bytes = b"",
) -> bytes:
    """An Ethernet/IPv4/UDP frame like framing.build_frame, but with every header field open.

    With no keyword arguments it equals build_frame(feed, payload). checksum replaces the
    IPv4 checksum, checksum_xor flips bits in the correct one. trailing adds bytes after the
    IP packet, as Ethernet padding does.
    """
    if udp_length is None:
        udp_length = 8 + len(payload) + udp_length_delta
    ip_length = 20 + len(options) + 8 + len(payload) + ip_length_delta
    if version_ihl is None:
        version_ihl = 0x40 | (5 + len(options) // 4)
    src = ipaddress.IPv4Address(src_ip or feed.src_ip).packed
    dst = ipaddress.IPv4Address(dst_ip or feed.group).packed

    ip = struct.pack(IPV4_HEADER, version_ihl, dscp, ip_length, ip_id & 0xFFFF, flags_offset, ttl, protocol, 0, src, dst)
    ip += options
    if checksum is None:
        checksum = ipv4_checksum(ip) ^ checksum_xor
    ip = ip[:10] + checksum.to_bytes(2, "big") + ip[12:]

    if dst_mac is None:
        dst_mac = replace(feed, group=dst_ip).dst_mac() if dst_ip else feed.dst_mac()
    ethernet = struct.pack(ETHERNET_HEADER, dst_mac, feed.src_mac, ethertype)
    if vlan is not None:
        ethernet = ethernet[:12] + struct.pack(">H H H", ETHERTYPE_VLAN, vlan, ethertype)
    udp = struct.pack(UDP_HEADER, src_port or feed.src_port, dst_port or feed.dst_port, udp_length, udp_checksum)
    return (ethernet + ip + udp + payload + trailing).ljust(MIN_FRAME, b"\x00")


def plain_frame(ethertype: int, body: bytes, dst_mac: bytes = b"\xff" * 6) -> bytes:
    """A non-IPv4 frame, e.g. ARP or IPv6."""
    return (struct.pack(ETHERNET_HEADER, dst_mac, DEFAULT_FEED.src_mac, ethertype) + body).ljust(MIN_FRAME, b"\x00")


def arp_request() -> bytes:
    """Who has 10.0.0.2? Tell 10.0.0.1."""
    sender_ip = ipaddress.IPv4Address("10.0.0.1").packed
    target_ip = ipaddress.IPv4Address("10.0.0.2").packed
    body = struct.pack(">H H B B H 6s 4s 6s 4s", 1, 0x0800, 6, 4, 1, DEFAULT_FEED.src_mac, sender_ip, b"\x00" * 6, target_ip)
    return plain_frame(ETHERTYPE_ARP, body)


def ipv6_udp(payload: bytes, dst_port: int) -> bytes:
    """IPv6/UDP to the feed's port; the parser must drop it on the EtherType."""
    udp = struct.pack(UDP_HEADER, 10000, dst_port, 8 + len(payload), 0) + payload
    src = ipaddress.IPv6Address("fe80::1").packed
    dst = ipaddress.IPv6Address("ff02::1").packed
    ip6 = struct.pack(">I H B B 16s 16s", 0x6000_0000, len(udp), PROTOCOL_UDP, 64, src, dst)
    return plain_frame(ETHERTYPE_IPV6, ip6 + udp, dst_mac=bytes.fromhex("333300000001"))


def mold_blocks(messages: list[bytes]) -> bytes:
    """MoldUDP64 message blocks: 2-byte length, then the message, for each one."""
    return b"".join(len(raw).to_bytes(2, "big") + raw for raw in messages)


# Cases


@dataclass(slots=True)
class Frame:
    data: bytes
    accept: bool  # must the packet parser pass its payload on?
    note: str = ""
    decoy: bool = False  # rejected, and carries the payload of the next frame
    fcs_error: bool = False  # the MAC flags a bad FCS at the end of the frame


@dataclass(slots=True)
class Case:
    name: str
    description: str
    frames: list[Frame] = field(default_factory=list)
    expect: dict[str, int] = field(default_factory=dict)  # counters the case must produce; others are 0
    hardware_only: bool = False  # tests rules the ideal model doesn't have

    def expected_counters(self) -> dict[str, int]:
        return {name: self.expect.get(name, 0) for name in COUNTERS}


class Sender:
    """Numbers messages into MoldUDP64 packets and wraps them in frames, as the exchange does."""

    def __init__(self, case: Case, feed: Feed = DEFAULT_FEED, session: str = SESSION, first_seq: int = 1):
        self.case = case
        self.feed = feed
        self.session = session
        self.next_seq = first_seq
        self.ip_id = 0

    def packet(self, messages: list[bytes], seq: int | None = None) -> MoldPacket:
        """A packet; by default it continues the sequence, else it starts at seq."""
        if seq is None:
            seq = self.next_seq
        self.next_seq = max(self.next_seq, seq + len(messages))
        return MoldPacket(self.session, seq, tuple(messages))

    def heartbeat(self, seq: int | None = None) -> MoldPacket:
        return MoldPacket(self.session, self.next_seq if seq is None else seq, ())

    def mold_header(self, count: int) -> bytes:
        """A raw MoldUDP64 header at the next sequence number, for packets built by hand."""
        return struct.pack(MOLD_HEADER, self.session.encode("ascii").ljust(10), self.next_seq, count)

    def frame(self, payload: bytes, **faults) -> bytes:
        self.ip_id += 1
        return raw_frame(self.feed, payload, ip_id=self.ip_id, **faults)

    def send(self, packet: MoldPacket | bytes, note: str = "", **faults) -> None:
        """Append a frame the parser must accept."""
        payload = packet.to_bytes() if isinstance(packet, MoldPacket) else packet
        self.case.frames.append(Frame(self.frame(payload, **faults), True, note))

    def send_messages(self, messages: list[bytes]) -> None:
        """Pack messages into as few full packets as fit, and send them."""
        batch: list[bytes] = []
        size = MOLD_HEADER_SIZE
        for raw in messages:
            if batch and size + 2 + len(raw) > MAX_PAYLOAD:
                self.send(self.packet(batch))
                batch, size = [], MOLD_HEADER_SIZE
            batch.append(raw)
            size += 2 + len(raw)
        if batch:
            self.send(self.packet(batch))

    def reject(self, data: bytes, note: str) -> None:
        """Append a frame the parser must drop."""
        self.case.frames.append(Frame(data, False, note))

    def decoy(self, messages: list[bytes], note: str, **faults) -> None:
        """A faulty frame carrying the next packet, then that packet sent correctly."""
        packet = self.packet(messages)
        self.case.frames.append(Frame(self.frame(packet.to_bytes(), **faults), False, note, decoy=True))
        self.send(packet)


def _start(name: str, description: str, seed: int) -> tuple[Case, Market, Sender]:
    """A case that opens the day and puts a few orders on every tracked book."""
    case = Case(name, description)
    market = Market(seed=seed)
    sender = Sender(case)
    opening = market.opening()
    for ticker in TICKERS:
        opening += [market.add(ticker, "B"), market.add(ticker, "S")]
    sender.send_messages(opening + market.batch(40))
    return case, market, sender


# Frame-level cases


def clean() -> Case:
    """Baseline: valid traffic only."""
    case, market, sender = _start("clean", "valid traffic only: full packets of mixed book activity", seed=1)
    sender.send_messages(market.batch(2000))
    return case


def foreign_traffic() -> Case:
    case, market, sender = _start(
        "foreign_traffic", "other protocols and other UDP traffic between feed packets; all must be dropped", seed=2
    )
    decoys = [
        ("VLAN-tagged feed frame (802.1Q, out of scope)", dict(vlan=100)),
        ("TCP instead of UDP", dict(protocol=PROTOCOL_TCP)),
        ("ICMP instead of UDP", dict(protocol=PROTOCOL_ICMP)),
        ("other multicast group, same port", dict(dst_ip="233.54.12.112")),
        ("feed group, other UDP port", dict(dst_port=26478)),
        ("unicast destination, feed port", dict(dst_ip="10.0.0.2", dst_mac=bytes.fromhex("020000000002"))),
        ("broadcast destination, feed port", dict(dst_ip="255.255.255.255", dst_mac=b"\xff" * 6)),
    ]
    for note, faults in decoys:
        sender.decoy(market.batch(12), note, **faults)
        sender.reject(arp_request(), "ARP request")
        sender.reject(ipv6_udp(sender.heartbeat().to_bytes(), sender.feed.dst_port), "IPv6/UDP to the feed port")
        sender.send_messages(market.batch(30))
    case.expect = {"dropped_frames": len(decoys) * 3}
    return case


def bad_ipv4() -> Case:
    case, market, sender = _start(
        "bad_ipv4", "feed frames with invalid IPv4 or UDP headers; all must be dropped", seed=3
    )
    decoys = [
        ("IP version 6 in an IPv4 EtherType", dict(version_ihl=0x65)),
        ("IHL 6: 4 bytes of IP options", dict(options=b"\x01\x01\x01\x00")),
        ("IHL 4: header too short", dict(version_ihl=0x44)),
        ("more-fragments flag set", dict(flags_offset=IP_MF)),
        ("fragment offset 1", dict(flags_offset=0x0001)),
        ("last fragment: offset set, MF clear", dict(flags_offset=0x00B9)),
        ("IPv4 checksum off by one bit", dict(checksum_xor=0x0001)),
        ("IPv4 checksum 0", dict(checksum=0)),
        ("UDP length one too long", dict(udp_length_delta=1)),
        ("UDP length one too short", dict(udp_length_delta=-1)),
        ("UDP length below its header (4)", dict(udp_length=4)),
        ("IP length one longer than the frame", dict(ip_length_delta=1)),
    ]
    for note, faults in decoys:
        sender.decoy(market.batch(10), note, **faults)
        sender.send_messages(market.batch(20))

    # Shorter than the Ethernet, IPv4 and UDP headers (only possible from a MAC that passes runts).
    sender.reject(sender.frame(b"")[: PAYLOAD_OFFSET - 1], "41-byte runt: headers incomplete")
    sender.send_messages(market.batch(20))

    case.expect = {"dropped_frames": len(decoys) + 1}
    return case


def accepted_variants() -> Case:
    case, market, sender = _start(
        "accepted_variants", "feed frames with unusual but valid headers; all must be accepted", seed=4
    )
    variants = [
        ("don't-fragment flag set (normal for most senders)", dict(flags_offset=IP_DF)),
        ("reserved IP flag bit set", dict(flags_offset=IP_RESERVED)),
        ("DSCP/ECN byte set", dict(dscp=0xB8)),
        ("TTL 1", dict(ttl=1)),
        ("other source IP and port", dict(src_ip="192.168.7.9", src_port=1)),
        ("nonzero UDP checksum (not checked, even if wrong)", dict(udp_checksum=0x1234)),
        ("destination MAC not the group's (MAC is not checked)", dict(dst_mac=bytes.fromhex("01005e000001"))),
        ("4 bytes after the IP packet (Ethernet padding)", dict(trailing=b"\xaa\xbb\xcc\xdd")),
    ]
    for note, faults in variants:
        sender.send(sender.packet(market.batch(10)), note, **faults)
        sender.send_messages(market.batch(15))

    sender.ip_id = 0xFFFE  # the next frame gets IP id 0xFFFF, the one after wraps to 0
    sender.send(sender.packet(market.batch(10)), "IP id 0xFFFF")
    sender.send(sender.packet(market.batch(10)), "IP id wrapped to 0")
    return case


def fill(market: Market, block_bytes: int) -> list[bytes]:
    """Valid messages whose blocks (2-byte length + message) add up to exactly block_bytes.

    Uses adds (38 bytes per block, alternately filler and tracked), filler directory entries
    (41), filler deletes (21) and system events (14). 21 and 14 are multiples of 7, so the
    adds and directory entries pick the remainder mod 7; every target from 210 bytes up can
    be hit.
    """
    for directories in range(7):
        for deletes in range(7):
            for events in range(7):
                rest = block_bytes - directories * 41 - deletes * 21 - events * 14
                if rest < 0 or rest % 38 != 0:
                    continue
                adds = rest // 38
                filler_adds = (adds + 1) // 2
                if filler_adds < deletes:
                    continue  # each delete removes one of these filler adds
                messages = [market.add(tracked=i % 2 == 1) for i in range(adds)]
                messages += [market.directory(FILLER_TICKERS[i % len(FILLER_TICKERS)]) for i in range(directories)]
                messages += [market.system("Q") for _ in range(events)]
                messages += [market.delete(tracked=False) for _ in range(deletes)]
                return messages
    raise ValueError(f"can't fill {block_bytes} bytes")


def frame_sizes() -> Case:
    """Smallest and largest frames, and every frame length modulo the 8-byte bus width."""
    case, market, sender = _start(
        "frame_sizes", "heartbeat-only (62 B), one-message, exactly 1514 B, and frames of every length mod 8", seed=5
    )
    sender.send(sender.heartbeat(), "smallest feed frame: 62 bytes, no messages")
    sender.send(sender.packet([market.delete()]), "one Delete: 83 bytes")
    sender.send(sender.packet(fill(market, MAX_PAYLOAD - MOLD_HEADER_SIZE)), "1514 bytes: largest frame")

    # One frame for every remainder of the frame length divided by 8.
    remainders_done: set[int] = set()
    for block_bytes in range(210, 1400, 13):
        frame_length = PAYLOAD_OFFSET + MOLD_HEADER_SIZE + block_bytes
        if frame_length % 8 in remainders_done:
            continue
        remainders_done.add(frame_length % 8)
        sender.send(sender.packet(fill(market, block_bytes)), f"{frame_length} bytes: length mod 8 = {frame_length % 8}")
        if len(remainders_done) == 8:
            break
    sender.send_messages(market.batch(30))
    return case


def back_to_back() -> Case:
    case, market, sender = _start(
        "back_to_back", "300 small packets in a row (1-3 messages each) for minimum inter-frame gaps", seed=6
    )
    for i in range(300):
        sender.send(sender.packet(market.batch(1 + i % 3)))
    return case


ALIGNMENT_TYPES = [b"S", b"R", b"A", b"F", b"E", b"C", b"X", b"D", b"U"]


def alignment() -> Case:
    """Packets of [first, second, add] for every ordered pair of message types.

    The first block starts at byte 62 of the frame, the second right after the first, and
    so on; with every pair of sizes, length prefixes and messages start in every byte lane
    of the 64-bit bus, including a length prefix split across two bus words.
    """
    case, market, sender = _start(
        "alignment", "every ordered pair of message types, so blocks start in every byte lane", seed=7
    )
    for first in ALIGNMENT_TYPES:
        for second in ALIGNMENT_TYPES:
            if market.tracked_live(min_shares=2) < 4:
                sender.send(sender.packet([market.add(shares=500) for _ in range(6)]), "refill live orders")
            messages = [market.of_type(first), market.of_type(second), market.of_type(b"A")]
            sender.send(sender.packet(messages), f"{first.decode()} then {second.decode()}")
    return case


def malformed_mold() -> Case:
    """MoldUDP64 packets whose blocks don't match the payload length, and frames that end
    before their IP length.

    The framer passes on every complete block before the break. Each broken packet is
    followed by one that starts at the first lost message, so if the break is handled as
    specified, no gap and no duplicate appear.
    """
    case, market, sender = _start(
        "malformed_mold",
        "broken MoldUDP64 blocks and frames cut short: complete blocks before the break are kept",
        seed=8,
    )
    sender.reject(sender.frame(b"\x00" * 12), "UDP payload of 12 bytes: shorter than the MoldUDP64 header")

    def send_broken(note: str, payload: bytes, messages: list[bytes], kept: int, cut: int = 0) -> None:
        """Send a broken packet that delivers only its first `kept` messages, then the rest.

        cut removes bytes from the end of the frame; its IP length still counts them.
        """
        frame = sender.frame(payload)
        case.frames.append(Frame(frame[: len(frame) - cut], True, note))
        sender.next_seq += kept
        sender.send(sender.packet(messages[kept:]), "resend from the first lost message")
        sender.send_messages(market.batch(20))

    messages = market.batch(5)
    send_broken("last message cut by 3 bytes", sender.mold_header(5) + mold_blocks(messages)[:-3], messages, kept=4)

    messages = market.batch(5)
    send_broken("count says 6, only 5 blocks", sender.mold_header(6) + mold_blocks(messages), messages, kept=5)

    messages = market.batch(5)
    send_broken("count says 3, 5 blocks follow", sender.mold_header(3) + mold_blocks(messages), messages, kept=3)

    messages = market.batch(5)
    payload = sender.mold_header(5) + mold_blocks(messages[:3]) + b"\x00"
    send_broken("only one byte of the 4th length prefix", payload, messages, kept=3)

    messages = market.batch(5)
    payload = sender.mold_header(5) + mold_blocks(messages[:2]) + b"\x00\x00" + mold_blocks(messages[2:])
    send_broken("zero-length block in 3rd place", payload, messages, kept=2)

    messages = market.batch(5)
    payload = sender.mold_header(5) + mold_blocks(messages[:1]) + b"\x00\x05" + b"\x00" * 5 + mold_blocks(messages[1:])
    send_broken("block of 5 bytes in 2nd place: shorter than any ITCH message", payload, messages, kept=1)

    messages = market.batch(5)
    payload = sender.mold_header(5) + mold_blocks(messages)
    send_broken("frame ends 5 bytes before its IP length", payload, messages, kept=4, cut=5)

    messages = market.batch(5)
    payload = sender.mold_header(5) + mold_blocks(messages)
    cut = len(mold_blocks(messages[3:]))
    send_broken("frame ends right after the 3rd block", payload, messages, kept=3, cut=cut)

    # Cut inside the MoldUDP64 header: no message has started, so the frame is dropped.
    events = [market.system("Q") for _ in range(5)]  # never delivered, so nothing a book needs
    frame = sender.frame(sender.mold_header(5) + mold_blocks(events))
    sender.reject(frame[: PAYLOAD_OFFSET + 10], "frame ends inside the MoldUDP64 header")
    sender.send_messages(market.batch(20))

    case.expect = {"dropped_frames": 2, "malformed_packets": 8}
    return case


# Sequence cases


def order_ref(add: bytes) -> int:
    """The order reference of a raw A or F message (the same offset in both)."""
    return AddOrder.from_bytes(add[: AddOrder.size()]).order_ref


def gap() -> Case:
    """One lost packet: the next message reveals the gap, every book goes stale, and later
    messages for orders the lost packet added are counted as unknown refs."""
    case, market, sender = _start("gap", "one lost packet; later messages refer to orders it added", seed=10)
    lost_adds = [market.add("AAPL", "B", market.mid["AAPL"] - 50), market.add("MSFT", "S", market.mid["MSFT"] + 50)]
    sender.packet(lost_adds + [market.system("Q") for _ in range(3)])  # lost: never sent

    after = [market.delete(order_ref(lost_adds[0])), market.replace(order_ref(lost_adds[1]))]
    market.forget([market.next_ref - 1])  # the replacement order is unknown to the receiver too
    sender.send(sender.packet(after + market.batch(10)), "first packet after the gap")
    sender.send_messages(market.batch(100))
    case.expect = {"gaps": 1, "unknown_refs": 2}
    return case


def two_gaps() -> Case:
    case, market, sender = _start("two_gaps", "two lost packets; only the first gap emits stale records", seed=11)
    for _ in range(2):
        sender.packet([market.system("Q") for _ in range(3)])  # lost
        sender.send_messages(market.batch(60))
    case.expect = {"gaps": 2}
    return case


def duplicate_packet() -> Case:
    case, market, sender = _start("duplicate_packet", "a packet arrives twice; the copy is dropped", seed=12)
    packet = sender.packet(market.batch(7))
    sender.send(packet)
    sender.send(packet, "the same packet again")
    sender.send_messages(market.batch(50))
    case.expect = {"duplicates": 7}
    return case


def overlap() -> Case:
    case, market, sender = _start(
        "overlap", "a packet starts 2 messages before the expected one: 2 duplicates, 3 new", seed=13
    )
    first = market.batch(6)
    sender.send(sender.packet(first))
    sender.send(sender.packet(first[-2:] + market.batch(3), seq=sender.next_seq - 2), "starts 2 messages back")
    sender.send_messages(market.batch(50))
    case.expect = {"duplicates": 2}
    return case


def reordered() -> Case:
    case, market, sender = _start(
        "reordered", "two packets swapped: the later one reveals a gap, the earlier one is then duplicates", seed=14
    )
    early = sender.packet([market.system("Q") for _ in range(4)])  # no book changes, so losing them is harmless
    late = sender.packet(market.batch(6))
    sender.send(late, "arrives first")
    sender.send(early, "arrives second")
    sender.send_messages(market.batch(50))
    case.expect = {"gaps": 1, "duplicates": 4}
    return case


def heartbeats() -> Case:
    case, market, sender = _start("heartbeats", "heartbeats in sequence, and an old one; none changes anything", seed=15)
    for _ in range(5):
        sender.send(sender.heartbeat(), "heartbeat")
        sender.send_messages(market.batch(20))
    sender.send(sender.heartbeat(seq=sender.next_seq - 10), "old heartbeat: sequence number already passed")
    sender.send_messages(market.batch(20))
    return case


def heartbeat_gap() -> Case:
    case, market, sender = _start(
        "heartbeat_gap", "a lost packet followed by a heartbeat: the heartbeat reveals the gap", seed=16
    )
    sender.packet([market.system("Q") for _ in range(5)])  # lost
    sender.send(sender.heartbeat(), "heartbeat after the lost packet")
    sender.send(sender.heartbeat(), "second heartbeat: no new gap")
    sender.send_messages(market.batch(50))
    case.expect = {"gaps": 1}
    return case


def end_of_session() -> Case:
    case, market, sender = _start("end_of_session", "the session ends with a message count of 0xFFFF", seed=17)
    sender.send_messages(market.batch(50) + [market.system("C")])
    sender.send(sender.mold_header(END_OF_SESSION), "end of session")
    sender.send(sender.mold_header(END_OF_SESSION), "end of session, repeated")
    return case


def bad_fcs() -> Case:
    """Frames the MAC flags with a bad FCS, known only at the end of the frame."""
    case, market, sender = _start(
        "bad_fcs",
        "bad FCS: on a dropped frame only counted; on a feed frame its messages are applied, then every book goes stale",
        seed=19,
    )
    case.frames.append(Frame(arp_request(), False, "ARP request with a bad FCS: dropped, only counted", fcs_error=True))
    sender.send_messages(market.batch(30))

    packet = sender.packet(market.batch(8))
    note = "feed frame with a bad FCS: messages applied, then every book stale"
    case.frames.append(Frame(sender.frame(packet.to_bytes()), True, note, fcs_error=True))
    sender.send_messages(market.batch(30))

    packet = sender.packet(market.batch(8))
    note = "second feed frame with a bad FCS: already stale, no new stale records"
    case.frames.append(Frame(sender.frame(packet.to_bytes()), True, note, fcs_error=True))
    sender.send_messages(market.batch(30))

    case.expect = {"dropped_frames": 1, "fcs_errors": 3}
    return case


# Message-level cases: rules only the hardware model has


def generator_config() -> Config:
    """The hardware model's configuration for generated traffic: each book's reference is
    Market's mid price (the same for every seed), so normal traffic stays inside the window."""
    mids = Market().mid
    return Config(TICKERS, {ticker: mids[ticker] // 100 for ticker in TICKERS})


def colliding_refs(count: int, bucket_bits: int, base: int = 0x4000_0000) -> list[int]:
    """count different 32-bit refs that all hash to the same bucket in both tables.

    The H3 hash is linear: hash(a ^ k) = hash(a) ^ hash(k). So every k with hash(k) = 0 in
    both tables gives a ref base ^ k in base's buckets. Such k are found by Gaussian
    elimination over the 32 single-bit refs.
    """
    from itch.hashing import buckets

    def image(ref: int) -> int:
        bucket0, bucket1 = buckets(ref, bucket_bits)
        return bucket0 | bucket1 << bucket_bits

    kernel = [0]
    pivots: dict[int, tuple[int, int]] = {}  # leading bit of an image -> (image, ref that makes it)
    for bit in range(32):
        value, ref = image(1 << bit), 1 << bit
        while value:
            lead = value.bit_length() - 1
            if lead not in pivots:
                pivots[lead] = (value, ref)
                break
            value ^= pivots[lead][0]
            ref ^= pivots[lead][1]
        if value == 0:  # this combination of bits hashes to 0: extend the kernel
            kernel += [k ^ ref for k in kernel]
        if len(kernel) >= count:
            break
    if len(kernel) < count:
        raise ValueError(f"only {len(kernel)} colliding refs")
    return [base ^ k for k in kernel[:count]]


def unusable_messages() -> Case:
    case, market, sender = _start(
        "unusable_messages",
        "book messages whose length doesn't match their type: counted, all books stale, one status record",
        seed=20,
    )
    case.hardware_only = True
    too_long_add = market.message(AddOrder, "AAPL", 9_000_001, "B", 100, "AAPL", 500_000) + b"\x00"
    too_short_delete = market.message(OrderDelete, "MSFT", 1)[:18]  # a D is 19 bytes
    too_long_directory = market.directory("IBM") + b"\x00"
    sender.send_messages(market.batch(20) + [too_long_add] + market.batch(20))
    sender.send_messages([too_short_delete, too_long_directory] + market.batch(20))
    case.expect = {"unusable_messages": 3}
    return case


def window_edges() -> Case:
    """Orders at the edges of AAPL's window ($9.04 to $90.95 for its $50.00 reference), stub
    quotes, and the cases that set and clear flag bit 1 (top outside window)."""
    case, market, sender = _start(
        "window_edges", "orders at and beyond the window edges, stub quotes, sub-penny, flag transitions", seed=21
    )
    case.hardware_only = True
    dollar = 10_000
    messages = [
        market.add("AAPL", "B", 9_0400, 100),  # first ladder level
        market.add("AAPL", "S", 90_9500, 100),  # last ladder level
        market.add("AAPL", "B", 9_0300, 100),  # just below: off-ladder, harmless
        market.add("AAPL", "S", 90_9600, 100),  # just above: off-ladder, harmless
        market.add("AAPL", "B", 100, 100),  # $0.01 stub bid
        market.add("AAPL", "S", 199_999 * dollar + 9900, 100),  # $199,999.99 stub ask
    ]
    sender.send_messages(messages)

    high_bid = market.add("AAPL", "B", 95 * dollar, 100)  # a bid above the window: flag on
    sender.send_messages([high_bid])
    sender.send_messages([market.delete(order_ref(high_bid))])  # flag off

    sub_penny = market.add("AAPL", "S", 50 * dollar + 50, 100)  # $50.0050 inside: unrepresentable, flag on
    sender.send_messages([sub_penny])
    sender.send_messages([market.delete(order_ref(sub_penny))])  # flag off

    inside_bids = [ref for ref, order in market.live.items() if order.ticker == "AAPL" and order.side == "B" and order.price >= 9_0400]
    sender.send_messages([market.delete(ref) for ref in sorted(inside_bids)])  # only bids below are left: flag on
    sender.send_messages([market.add("AAPL", "B", 49 * dollar, 100)])  # an inside bid again: flag off
    case.expect = {"off_ladder_orders": 5, "unrep_prices": 1}
    return case


def book_anomalies() -> Case:
    """Order-table anomalies without a gap: each marks only its own book stale."""
    case, market, sender = _start(
        "book_anomalies",
        "unknown ref, too many shares removed, duplicate add, ref above 32 bits, U across books",
        seed=22,
    )
    case.hardware_only = True
    unknown = market.message(OrderDelete, "AAPL", 9_999_999)

    msft = market.add("MSFT", "B", market.mid["MSFT"] - 100, 100)
    over_cancel = market.message(OrderCancel, "MSFT", order_ref(msft), 150)
    market.forget([order_ref(msft)])  # the hardware removes all 100 shares

    tsla_ref = next(ref for ref, order in market.live.items() if order.ticker == "TSLA")
    duplicate = market.message(AddOrder, "TSLA", tsla_ref, "B", 100, "TSLA", market.mid["TSLA"] - 200)

    big_ref = 2**32 + 5
    big_add = market.message(AddOrder, "QQQ", big_ref, "S", 100, "QQQ", market.mid["QQQ"] + 100)
    big_delete = market.message(OrderDelete, "QQQ", big_ref)

    cost = market.add("COST", "S", market.mid["COST"] + 100, 100)
    replace_in_nflx = market.message(OrderReplace, "NFLX", order_ref(cost), 8_888_888, 100, market.mid["NFLX"] + 100)
    market.forget([order_ref(cost)])

    sender.send_messages([unknown, msft, over_cancel, duplicate, big_add, big_delete, cost, replace_in_nflx])
    sender.send_messages(market.batch(40))
    case.expect = {"unknown_refs": 2, "inconsistent": 3, "ref_errors": 1}
    return case


def table_overflow() -> Case:
    """49 SIRI orders whose refs share both buckets: 16 fill the buckets, 32 the stash, the
    49th fails and makes SIRI stale."""
    case, market, sender = _start(
        "table_overflow", "colliding refs fill both buckets and the stash; the last insert fails", seed=23
    )
    case.hardware_only = True
    refs = colliding_refs(49, generator_config().bucket_bits)
    price = market.mid["SIRI"] - 100
    sender.send_messages([market.add("SIRI", "B", price, 100, order_ref=ref) for ref in refs])
    market.forget([refs[-1]])  # it never got into the table
    sender.send_messages([market.delete(ref) for ref in refs[:10]])
    case.expect = {"insert_failures": 1, "stash_inserts": 32}
    return case


def late_join() -> Case:
    """Listening starts mid-session: the first packet is a gap, so the books start out stale."""
    case = Case("late_join", "the first packet received has sequence number 1000")
    market = Market(seed=18)
    sender = Sender(case, first_seq=1000)
    sender.send_messages(market.opening() + market.batch(200))
    case.expect = {"gaps": 1}
    return case


CASES = [
    clean,
    foreign_traffic,
    bad_ipv4,
    accepted_variants,
    frame_sizes,
    back_to_back,
    alignment,
    malformed_mold,
    gap,
    two_gaps,
    duplicate_packet,
    overlap,
    reordered,
    heartbeats,
    heartbeat_gap,
    end_of_session,
    bad_fcs,
    late_join,
    unusable_messages,
    window_edges,
    book_anomalies,
    table_overflow,
]


def all_cases() -> list[Case]:
    return [make_case() for make_case in CASES]


# Minimal cases for the first board runs: a few frames each, broadcast destination MAC


BROADCAST_MAC = b"\xff" * 6
MIN_BID = 49_9900  # $49.99
MIN_ASK = 50_0100  # $50.01


def _minimal(name: str, description: str) -> tuple[Case, Market, Sender]:
    case = Case(name, description)
    return case, Market(seed=30), Sender(case)


def _send_minimal(sender: Sender, messages: list[bytes], note: str) -> None:
    sender.send(sender.packet(messages), note, dst_mac=BROADCAST_MAC)


def min_frame() -> Case:
    case, market, sender = _minimal("min_frame", "one frame with one System Event: no records; needs no configuration")
    _send_minimal(sender, [market.system("O")], "seq 1: S start of messages")
    return case


def min_frames() -> Case:
    case, market, sender = _minimal("min_frames", "three frames with one System Event each, seq 1 to 3: no records")
    for code in "OSQ":
        _send_minimal(sender, [market.system(code)], f"seq {sender.next_seq}: S {code}")
    return case


def min_add() -> Case:
    case, market, sender = _minimal("min_add", "one frame: R for AAPL, then one AAPL bid: 1 record; needs the configuration")
    messages = [market.directory("AAPL"), market.add("AAPL", "B", MIN_BID, 100)]
    _send_minimal(sender, messages, "seq 1: R AAPL, seq 2: A AAPL buy 100 @ 49.99")
    return case


def min_book() -> Case:
    case, market, sender = _minimal(
        "min_book", "four frames: R for AAPL, a bid, an ask, the bid deleted: 3 records; needs the configuration"
    )
    _send_minimal(sender, [market.directory("AAPL")], "seq 1: R AAPL")
    bid = market.add("AAPL", "B", MIN_BID, 100)
    _send_minimal(sender, [bid], "seq 2: A AAPL buy 100 @ 49.99")
    _send_minimal(sender, [market.add("AAPL", "S", MIN_ASK, 200)], "seq 3: A AAPL sell 200 @ 50.01")
    _send_minimal(sender, [market.delete(order_ref(bid))], "seq 4: D the bid")
    return case


MINIMAL_CASES = [min_frame, min_frames, min_add, min_book]
