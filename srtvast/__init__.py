"""SRT + SCTE-35 + VAST server-side ad insertion proof of concept."""

import json
import logging
import os
import socket


class UdpEventHandler(logging.Handler):
    """Sends each log record as a JSON datagram (to the web viewer).

    Fire-and-forget: if the target is down or not resolvable, records are dropped.
    """

    def __init__(self, targets: str, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.targets = []
        for target in filter(None, (t.strip() for t in targets.split(","))):
            host, _, port = target.rpartition(":")
            self.targets.append((host or "127.0.0.1", int(port)))
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.dumps({
                "t": record.created,
                "level": record.levelname,
                "logger": record.name,
                "msg": record.getMessage(),
            }).encode()
        except Exception:  # noqa: BLE001 - never let logging break the stream
            return
        for target in self.targets:
            try:
                self.sock.sendto(payload, target)
            except OSError:
                pass

    def close(self) -> None:
        self.sock.close()
        super().close()


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)-9s %(message)s",
        datefmt="%H:%M:%S",
    )
    if targets := os.environ.get("EVENTS_UDP"):
        logging.getLogger().addHandler(UdpEventHandler(targets))
