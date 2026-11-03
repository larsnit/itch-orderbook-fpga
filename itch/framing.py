"""Network framing around ITCH messages: MoldUDP64 packets inside Ethernet/IPv4/UDP frames.

Builds frames for the stimulus and parses them for the golden model. Only what the project
needs: IPv4 without options or fragments, no VLAN tags, no UDP checksum (0 = unused, allowed
in IPv4). Frames are without FCS, as the MAC delivers them and as pcap files store them.

    Ethernet (14) | IPv4 (20) | UDP (8) | MoldUDP64 header (20) | len | msg | len | msg | ...
"""

import ipaddress
import struct
from dataclasses import dataclass
from typing import Iterable, Iterator

from itch.messages import timestamp_of

# MoldUDP64

MOLD_HEADER = ">10s Q H"  # session, sequence number of the first message, message count
MOLD_HEADER_SIZE = struct.calcsize(MOLD_HEADER)
END_OF_SESSION = 0xFFFF  # message count that marks the session's end; carries no messages

# Smallest ITCH message (S, W): a shorter block can't be a message, so the packet is broken.
MIN_MESSAGE = 12

# Largest UDP payload in a 1500-byte IP packet: 1500 - 20 (IPv4) - 8 (UDP).
MAX_PAYLOAD = 1472


@dataclass(frozen=True, slots=True)
class MoldPacket:
    session: str
    seq: int  # of the first message; for a packet without messages, of the next one
    messages: tuple[bytes, ...]

    def to_bytes(self) -> bytes:
        header = struct.pack(MOLD_HEADER, self.session.encode("ascii").ljust(10), self.seq, len(self.messages))
        blocks = b"".join(len(m).to_bytes(2, "big") + m for m in self.messages)
        return header + blocks

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MoldPacket":
        """Strict: raises ValueError unless the packet is exactly well formed."""
        parsed = read_packet(raw)
        if parsed is None:
            raise ValueError(f"MoldUDP64 packet of {len(raw)} bytes is shorter than its header")
        packet, well_formed = parsed
        if not well_formed:
            raise ValueError(f"MoldUDP64 packet {packet.seq}: blocks don't match its length")
        return packet


def read_packet(raw: bytes) -> tuple[MoldPacket, bool] | None:
    """Read a packet the way a streaming framer does: (packet, well formed) or None.

    None if raw is shorter than the header. Otherwise the packet holds every complete
    message block up to the first broken one (length below MIN_MESSAGE, or running past
    the end), and well formed is False if a block was broken or bytes are left over after
    the last one.
    Hardware passes messages on as they arrive, so it can't take back the earlier blocks
    of a packet that turns out to be broken; the messages after the break are lost, which
    the next packet then shows as a gap.
    """
    if len(raw) < MOLD_HEADER_SIZE:
        return None
    session, seq, count = struct.unpack_from(MOLD_HEADER, raw)
    if count == END_OF_SESSION:
        count = 0

    messages = []
    pos = MOLD_HEADER_SIZE
    well_formed = True
    for _ in range(count):
        if pos + 2 > len(raw):
            well_formed = False
            break
        length = int.from_bytes(raw[pos : pos + 2], "big")
        if length < MIN_MESSAGE or pos + 2 + length > len(raw):
            well_formed = False
            break
        messages.append(raw[pos + 2 : pos + 2 + length])
        pos += 2 + length
    if pos != len(raw):
        well_formed = False
    packet = MoldPacket(session.decode("ascii", errors="replace").rstrip(" "), seq, tuple(messages))
    return packet, well_formed


def packetize(
    messages: Iterable[bytes],
    session: str,
    first_seq: int = 1,
    max_payload: int = MAX_PAYLOAD,
    max_delay_ns: int | None = None,
) -> Iterator[MoldPacket]:
    """Group consecutive messages into packets, numbering them from first_seq.

    A packet is closed when the next message wouldn't fit in max_payload bytes, or, with
    max_delay_ns, when it is more than that much later than the packet's first message.
    """
    seq = first_seq
    batch: list[bytes] = []
    size = MOLD_HEADER_SIZE

    for raw in messages:
        too_big = size + 2 + len(raw) > max_payload
        too_late = max_delay_ns is not None and batch and timestamp_of(raw) - timestamp_of(batch[0]) > max_delay_ns
        if batch and (too_big or too_late):
            yield MoldPacket(session, seq, tuple(batch))
            seq += len(batch)
            batch, size = [], MOLD_HEADER_SIZE
        batch.append(raw)
        size += 2 + len(raw)

    if batch:
        yield MoldPacket(session, seq, tuple(batch))


# Ethernet / IPv4 / UDP

ETHERNET_HEADER = ">6s 6s H"  # destination MAC, source MAC, EtherType
IPV4_HEADER = ">B B H H H B B H 4s 4s"  # version+IHL, DSCP, length, id, flags+offset, TTL, protocol, checksum, src, dst
UDP_HEADER = ">H H H H"  # source port, destination port, length, checksum
ETHERTYPE_IPV4 = 0x0800
PROTOCOL_UDP = 17
IPV4_NO_OPTIONS = 0x45  # version 4, header length 5 words = 20 bytes
PAYLOAD_OFFSET = 14 + 20 + 8
MIN_FRAME = 60  # Ethernet minimum without FCS; shorter frames are padded


@dataclass(frozen=True, slots=True)
class Feed:
    """Addresses of one multicast feed: what the generator sends and the FPGA accepts."""

    src_mac: bytes
    src_ip: str
    group: str  # multicast destination IP
    src_port: int
    dst_port: int

    def dst_mac(self) -> bytes:
        """Multicast MAC: 01:00:5e followed by the low 23 bits of the group address."""
        ip = ipaddress.IPv4Address(self.group).packed
        return bytes([0x01, 0x00, 0x5E, ip[1] & 0x7F, ip[2], ip[3]])


DEFAULT_FEED = Feed(
    src_mac=bytes.fromhex("020000000001"),
    src_ip="10.0.0.1",
    group="233.54.12.111",
    src_port=10000,
    dst_port=26477,
)


def ipv4_checksum(header: bytes) -> int:
    """Ones' complement of the ones' complement sum of the header's 16-bit words."""
    total = sum(struct.unpack(f">{len(header) // 2}H", header))
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def build_frame(feed: Feed, payload: bytes, ip_id: int = 0) -> bytes:
    udp_length = 8 + len(payload)
    ip_length = 20 + udp_length
    src = ipaddress.IPv4Address(feed.src_ip).packed
    dst = ipaddress.IPv4Address(feed.group).packed

    ip = struct.pack(IPV4_HEADER, IPV4_NO_OPTIONS, 0, ip_length, ip_id & 0xFFFF, 0, 64, PROTOCOL_UDP, 0, src, dst)
    ip = ip[:10] + ipv4_checksum(ip).to_bytes(2, "big") + ip[12:]

    frame = (
        struct.pack(ETHERNET_HEADER, feed.dst_mac(), feed.src_mac, ETHERTYPE_IPV4)
        + ip
        + struct.pack(UDP_HEADER, feed.src_port, feed.dst_port, udp_length, 0)
        + payload
    )
    return frame.ljust(MIN_FRAME, b"\x00")


def frame_payload(frame: bytes, feed: Feed) -> bytes | None:
    """The UDP payload if the frame belongs to the feed and its headers are valid, else None.

    Every check is decided by the end of the UDP header, before any payload is passed on,
    except one: whether the frame is as long as its IP length says. That is only known when
    the frame ends, so a frame that ends early still gives the payload bytes that arrived;
    read_packet then finds the cut block, as the hardware framer does.
    """
    if len(frame) <= PAYLOAD_OFFSET:  # not one payload byte: only a frame cut short
        return None
    _, _, ethertype = struct.unpack_from(ETHERNET_HEADER, frame)
    if ethertype != ETHERTYPE_IPV4:
        return None

    ip = frame[14:34]
    version_ihl, _, ip_length, _, flags_offset, _, protocol, _, _, dst = struct.unpack(IPV4_HEADER, ip)
    if version_ihl != IPV4_NO_OPTIONS or protocol != PROTOCOL_UDP:
        return None
    if flags_offset & 0x3FFF:  # more-fragments bit or a fragment offset: a fragment
        return None
    if ipv4_checksum(ip) != 0:  # a valid header sums to 0, checksum included
        return None
    if dst != ipaddress.IPv4Address(feed.group).packed:
        return None

    _, dst_port, udp_length, _ = struct.unpack_from(UDP_HEADER, frame, 34)
    if dst_port != feed.dst_port or udp_length != ip_length - 20:
        return None
    if udp_length < 8 + MOLD_HEADER_SIZE:  # no room for the MoldUDP64 header
        return None
    return frame[PAYLOAD_OFFSET : 14 + ip_length]  # shorter if the frame ends early