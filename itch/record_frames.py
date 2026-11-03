"""UDP frames of the board's record output (implementation section 12.8a): build them (for
tests) and take them apart (for the capture on the PC).

    bytes  0-13   Ethernet: destination MAC, source MAC, 0x0800
    bytes 14-33   IPv4, no options, DF, TTL 64, UDP; id = frame counter (low 16 bits)
    bytes 34-41   UDP, checksum 0
    bytes 42-55   batch header: 0x4952 ("IR"), frame counter (32), record count (16),
                  records dropped so far (32), 0x0000
    bytes 56-     record count x 32-byte records (record.py layout)
"""

import ipaddress
import struct
from dataclasses import dataclass

from itch.framing import ipv4_checksum
from itch.record import SIZE, Record

MAGIC = 0x4952
BATCH_HEADER = ">H I H I H"
HEADER_BYTES = 56
DEFAULT_PORT = 5000


@dataclass(frozen=True, slots=True)
class Addresses:
    dst_mac: bytes = b"\xff" * 6
    src_mac: bytes = bytes.fromhex("020000000002")
    dst_ip: str = "255.255.255.255"
    src_ip: str = "10.0.0.2"
    dst_port: int = DEFAULT_PORT
    src_port: int = DEFAULT_PORT


@dataclass(frozen=True, slots=True)
class RecordFrame:
    frame_counter: int
    dropped: int
    records: list[Record]


def build(addresses: Addresses, frame_counter: int, dropped: int, records: list[bytes]) -> bytes:
    """The frame the board sends, without FCS; records as 32-byte strings."""
    payload = struct.pack(BATCH_HEADER, MAGIC, frame_counter, len(records), dropped, 0) + b"".join(records)
    udp = struct.pack(">H H H H", addresses.src_port, addresses.dst_port, 8 + len(payload), 0)
    ip = struct.pack(">B B H H H B B H 4s 4s", 0x45, 0, 20 + len(udp) + len(payload), frame_counter & 0xFFFF,
                     0x4000, 64, 17, 0, ipaddress.IPv4Address(addresses.src_ip).packed,
                     ipaddress.IPv4Address(addresses.dst_ip).packed)
    ip = ip[:10] + ipv4_checksum(ip).to_bytes(2, "big") + ip[12:]
    return addresses.dst_mac + addresses.src_mac + b"\x08\x00" + ip + udp + payload


def parse(frame: bytes, port: int = DEFAULT_PORT) -> RecordFrame | None:
    """A record frame taken apart, or None if the frame is something else (another port, no
    magic). Raises ValueError for a record frame with inconsistent lengths or checksum."""
    if len(frame) < HEADER_BYTES or frame[12:14] != b"\x08\x00" or frame[23] != 17:
        return None
    if int.from_bytes(frame[36:38], "big") != port:
        return None
    magic, frame_counter, count, dropped, _ = struct.unpack(BATCH_HEADER, frame[42:56])
    if magic != MAGIC:
        return None
    if ipv4_checksum(frame[14:34]) != 0:
        raise ValueError(f"frame {frame_counter}: bad IPv4 header checksum")
    ip_length = int.from_bytes(frame[16:18], "big")
    if ip_length != 42 + SIZE * count or len(frame) < 14 + ip_length:
        raise ValueError(f"frame {frame_counter}: IP length {ip_length} for {count} records, frame {len(frame)} bytes")
    if int.from_bytes(frame[38:40], "big") != 22 + SIZE * count:
        raise ValueError(f"frame {frame_counter}: UDP length does not match {count} records")
    records = [Record.from_bytes(frame[HEADER_BYTES + SIZE * i : HEADER_BYTES + SIZE * (i + 1)]) for i in range(count)]
    return RecordFrame(frame_counter, dropped, records)
