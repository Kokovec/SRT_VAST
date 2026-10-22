"""UDP plumbing between the Python components and srt-live-transmit.

Each container runs srt-live-transmit for the SRT leg and hands MPEG-TS to
Python over loopback UDP (7 x 188-byte packets per datagram). UDP decouples
the two sides: no back-pressure when an SRT peer is absent.
"""

from __future__ import annotations

import socket

from .ts import PACKET_SIZE

PACKETS_PER_DATAGRAM = 7


def parse_hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    return host or "0.0.0.0", int(port)


def udp_receiver(addr: str, rcvbuf: int = 4 << 20) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    sock.bind(parse_hostport(addr))
    return sock


class UdpPacketWriter:
    def __init__(self, addr: str) -> None:
        self.addr = parse_hostport(addr)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._pending = bytearray()

    def write(self, pkt) -> None:
        self._pending += pkt
        if len(self._pending) >= PACKETS_PER_DATAGRAM * PACKET_SIZE:
            self.flush()

    def flush(self) -> None:
        if self._pending:
            try:
                self.sock.sendto(self._pending, self.addr)
            except OSError:
                pass  # nobody listening yet; live stream, drop it
            self._pending = bytearray()
