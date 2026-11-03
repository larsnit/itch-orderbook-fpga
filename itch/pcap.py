"""Classic pcap files of Ethernet frames, as Wireshark, tcpdump and tcpreplay use them.

Writes nanosecond-resolution files. Reads both resolutions and byte orders, but not the
newer pcapng format (Wireshark can save as classic pcap: File > Save As > "pcap").
"""

import struct
from typing import BinaryIO, Iterable, Iterator

MAGIC_MICRO = 0xA1B2C3D4
MAGIC_NANO = 0xA1B23C4D
LINKTYPE_ETHERNET = 1
SNAPLEN = 65535

FILE_HEADER = "I H H i I I I"  # magic, version 2.4, timezone, accuracy, snaplen, link type
RECORD_HEADER = "I I I I"  # seconds, fraction, captured length, original length


def write_pcap(f: BinaryIO, frames: Iterable[tuple[int, bytes]]) -> None:
    """frames: (time in ns since 1970, frame bytes without FCS)."""
    f.write(struct.pack("<" + FILE_HEADER, MAGIC_NANO, 2, 4, 0, 0, SNAPLEN, LINKTYPE_ETHERNET))
    for time_ns, frame in frames:
        seconds, nanos = divmod(time_ns, 1_000_000_000)
        f.write(struct.pack("<" + RECORD_HEADER, seconds, nanos, len(frame), len(frame)))
        f.write(frame)


def read_pcap(f: BinaryIO) -> Iterator[tuple[int, bytes]]:
    """(time in ns since 1970, frame bytes) for every record."""
    header = f.read(24)
    for order in "<>":
        magic, *_, link_type = struct.unpack(order + FILE_HEADER, header)
        if magic in (MAGIC_MICRO, MAGIC_NANO):
            break
    else:
        raise ValueError("not a classic pcap file (pcapng is not supported)")
    if link_type != LINKTYPE_ETHERNET:
        raise ValueError(f"link type {link_type} is not Ethernet")
    fraction_ns = 1 if magic == MAGIC_NANO else 1000

    while record := f.read(16):
        seconds, fraction, length, _ = struct.unpack(order + RECORD_HEADER, record)
        frame = f.read(length)
        if len(frame) != length:
            raise EOFError("pcap ends inside a frame")
        yield seconds * 1_000_000_000 + fraction * fraction_ns, frame
