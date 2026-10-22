"""Web monitor: the stitched output plus a real-time event log, in a browser.

* Pulls the stitcher's monitor SRT output and re-serves it, untouched, as an
  HTTP MPEG-TS stream (``/live.ts``); the page plays it with mpegts.js (Media
  Source Extensions), so what you watch is the stitched TS itself.
* Collects the JSON log events every component sends over UDP (see
  ``srtvast.UdpEventHandler``) and pushes them to the page over Server-Sent
  Events (``/events``), with a backlog for late joiners.

    python -m srtvast.viewer --srt srt://stitcher:9002 --port 8090
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import setup_logging
from .transport import udp_receiver
from .ts import PacketReader

log = logging.getLogger("viewer")

WEB_DIR = Path(__file__).with_name("web")
HEARTBEAT = 15.0


class Hub:
    """Fans items out to subscriber queues. A subscriber that falls behind is dropped."""

    def __init__(self, maxsize: int, backlog: int = 0) -> None:
        self.maxsize = maxsize
        self.backlog: collections.deque = collections.deque(maxlen=backlog or None)
        self.keep_backlog = backlog > 0
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> tuple[queue.Queue, list]:
        q: queue.Queue = queue.Queue(self.maxsize)
        with self._lock:
            self._subs.add(q)
            return q, list(self.backlog)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def subscribed(self, q: queue.Queue) -> bool:
        with self._lock:
            return q in self._subs

    def publish(self, item) -> None:
        with self._lock:
            if self.keep_backlog:
                self.backlog.append(item)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(item)
            except queue.Full:
                self.unsubscribe(q)

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)


class HubLogHandler(logging.Handler):
    """Puts the viewer's own log records into the event stream."""

    def __init__(self, hub: Hub) -> None:
        super().__init__(logging.INFO)
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        self.hub.publish({"t": record.created, "level": record.levelname,
                          "logger": record.name, "msg": record.getMessage()})


def pull_srt(url: str, video: Hub) -> None:
    """Keep an SRT caller connected to the stitcher; publish whole TS packets."""
    while True:
        proc = subprocess.Popen(["srt-live-transmit", "-q", "-a:no", url, "file://con"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        reader, connected, received = PacketReader(), False, 0
        while chunk := proc.stdout.read1(65536):
            packets = reader.feed(chunk)
            if not packets:
                continue
            if not connected:
                connected = True
                log.info("monitor feed connected: %s", url)
            received += len(chunk)
            video.publish(b"".join(packets))
        proc.wait()
        if connected:
            log.warning("monitor feed lost after %.1f MB, reconnecting", received / 1e6)
        time.sleep(1)


def receive_events(addr: str, events: Hub) -> None:
    sock = udp_receiver(addr)
    while True:
        data = sock.recv(65536)
        try:
            event = json.loads(data)
        except ValueError:
            continue
        if isinstance(event, dict) and "msg" in event:
            events.publish(event)


class Handler(BaseHTTPRequestHandler):
    server: "ViewerServer"

    def log_message(self, fmt, *args) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/live.ts":
            self._live()
        elif path == "/events":
            self._events()
        elif path == "/health":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str) -> None:
        self._send(200, path.read_bytes(), ctype)

    def _stream_headers(self, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _live(self) -> None:
        hub = self.server.video
        q, _ = hub.subscribe()
        log.info("viewer %s watching (%d now)", self.address_string(), len(hub))
        self._stream_headers("video/mp2t")
        try:
            while True:
                try:
                    self.wfile.write(q.get(timeout=1.0))
                except queue.Empty:
                    if not hub.subscribed(q):
                        break  # dropped for falling behind
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub.unsubscribe(q)
            log.info("viewer %s left", self.address_string())

    def _events(self) -> None:
        hub = self.server.events
        q, backlog = hub.subscribe()
        self._stream_headers("text/event-stream")
        try:
            self.wfile.write(b"event: backlog\ndata: " + json.dumps(backlog).encode() + b"\n\n")
            last = time.monotonic()
            while True:
                try:
                    event = q.get(timeout=1.0)
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    last = time.monotonic()
                except queue.Empty:
                    if not hub.subscribed(q):
                        break
                    if time.monotonic() - last > HEARTBEAT:
                        self.wfile.write(b": ping\n\n")
                        last = time.monotonic()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub.unsubscribe(q)


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, video: Hub, events: Hub) -> None:
        super().__init__(addr, Handler)
        self.video = video
        self.events = events


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Web monitor for the stitched output and event log")
    p.add_argument("--srt", default="srt://stitcher:9002?mode=caller&latency=200",
                   help="stitcher monitor output to pull")
    p.add_argument("--events-udp", default="0.0.0.0:5100", help="where components send JSON log events")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--backlog", type=int, default=500, help="events replayed to a newly opened page")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    setup_logging(args.log_level)

    video = Hub(maxsize=2000)            # ~20 s of 2.6 Mb/s in 1316-byte chunks
    events = Hub(maxsize=1000, backlog=args.backlog)
    logging.getLogger().addHandler(HubLogHandler(events))
    threading.Thread(target=pull_srt, args=(args.srt, video), daemon=True, name="srt").start()
    threading.Thread(target=receive_events, args=(args.events_udp, events), daemon=True, name="events").start()
    server = ViewerServer((args.host, args.port), video, events)
    log.info("viewer on http://%s:%d (feed %s, events udp %s)", args.host, args.port, args.srt, args.events_udp)
    server.serve_forever()


if __name__ == "__main__":
    main()
