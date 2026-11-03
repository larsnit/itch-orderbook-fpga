"""Cross-check the generated frames with an independent parser (Scapy), if it's installed."""

import pytest

from itch.framing import DEFAULT_FEED, frame_payload
from itch.generator import all_cases

scapy = pytest.importorskip("scapy.all")


def ip_checksum_ok(ip) -> bool:
    """Let Scapy compute the header checksum itself and compare."""
    copy = scapy.IP(bytes(ip)[: ip.ihl * 4])
    del copy.chksum
    return scapy.IP(bytes(copy)).chksum == ip.chksum


@pytest.fixture(scope="module")
def frames():
    return [(case.name, frame) for case in all_cases() for frame in case.frames]


def test_accepted_frames_parse_as_feed_udp(frames):
    for name, frame in frames:
        if not frame.accept:
            continue
        packet = scapy.Ether(frame.data)
        ip, udp = packet[scapy.IP], packet[scapy.UDP]
        assert ip.dst == DEFAULT_FEED.group and udp.dport == DEFAULT_FEED.dst_port, (name, frame.note)
        assert ip.ihl == 5 and ip.frag == 0 and not ip.flags.MF, (name, frame.note)
        assert ip_checksum_ok(ip), (name, frame.note)
        assert bytes(udp.payload)[: udp.len - 8] == frame_payload(frame.data, DEFAULT_FEED), (name, frame.note)


def test_checksum_decoys_fail_in_scapy_too(frames):
    decoys = [frame for _, frame in frames if "checksum" in frame.note and not frame.accept]
    assert len(decoys) == 2
    for frame in decoys:
        assert not ip_checksum_ok(scapy.Ether(frame.data)[scapy.IP]), frame.note
