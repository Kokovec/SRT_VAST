"""SRT + SCTE-35 + VAST server-side ad insertion proof of concept."""

import json
import logging
import os
import socket


MAX_EVENT_BYTES = 60_000


def event_dict(record: logging.LogRecord) -> dict:
    """A log record as a web-monitor event. ``extra={"detail": {...}}`` adds structured data
    (decoded SCTE-35 sections, VAST requests and XML) that the page can expand."""
    event = {"t": record.created, "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
    detail = getattr(record, "detail", None)
    if detail is not None:
        event["detail"] = detail
    return event


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
            payload = json.dumps(event_dict(record)).encode()
            if len(payload) > MAX_EVENT_BYTES:  # stay inside one UDP datagram
                event = event_dict(record)
                event["detail"] = {"kind": event["detail"].get("kind"), "error": "detail too large to send"}
                payload = json.dumps(event).encode()
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
