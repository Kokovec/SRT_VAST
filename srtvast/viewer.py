"""Web monitor: the stitched output plus a real-time event log, in a browser.

* Pulls the stitcher's monitor SRT output and re-serves it, untouched, as an
  HTTP MPEG-TS stream (``/live.ts``); the page plays it with mpegts.js (Media
  Source Extensions), so what you watch is the stitched TS itself.
* Collects the JSON log events every component sends over UDP (see
  ``srtvast.UdpEventHandler``) and pushes them to the page over Server-Sent
  Events (``/events``), with a backlog for late joiners.
* With ``--auth-issuer`` + ``--auth-audience`` (Cognito user pool + app
  client), every request must carry a valid session token in the
  ``srtvast_session`` cookie (set by the starter Lambda after sign-in);
  signed-out visitors are sent to ``/_auth/login``. Streams end when the
  user's session does, and ``/api/session`` tells the page who is signed in
  until when.
* ``--tls-port`` adds an HTTPS listener: the hop from CloudFront to the host.
* ``/api/status`` reports how many streams are open and for how long nobody
  has watched; the host's idle watchdog stops the instance on it.

    python -m srtvast.viewer --srt srt://stitcher:9002 --port 8090
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import math
import os
import queue
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import setup_logging
from .auth import SESSION_COOKIE, AuthError, Identity, JwtVerifier, cookie_value
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
        if path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        if path == "/api/status":  # counts only; polled by the host's idle watchdog
            self._json(self.server.status())
            return
        who = self._identity()
        if who is None:
            return
        if path in ("/", "/index.html"):
            self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/session":
            self._json({"email": who.email, "expires": None if math.isinf(who.expires) else who.expires,
                        "auth": self.server.verifier is not None})
        elif path == "/live.ts":
            self._live(who)
        elif path == "/events":
            self._events(who)
        else:
            self._send(404, b"not found", "text/plain")

    def _identity(self) -> Identity | None:
        """The signed-in user, or None after answering. Open access without a verifier.

        Pages redirect to sign-in; API and stream requests get 401 (never a 5xx,
        which CloudFront would treat as "origin down" and fail over).
        """
        verifier = self.server.verifier
        if verifier is None:
            return Identity("", math.inf)
        token = cookie_value(self.headers.get("Cookie"), SESSION_COOKIE)
        try:
            if not token:
                raise AuthError("not signed in")
            return verifier.verify(token)
        except AuthError as exc:
            path = self.path.split("?", 1)[0]
            log.info("unauthenticated %s %s: %s", self.address_string(), path, exc)
            if path in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/_auth/login")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(401, b"sign in required\n", "text/plain")
            return None

    def _json(self, data) -> None:
        self._send(200, json.dumps(data).encode(), "application/json")

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

    def _live(self, who: Identity) -> None:
        hub = self.server.video
        q, _ = hub.subscribe()
        name = who.email or self.address_string()
        log.info("viewer %s watching (%d now)", name, len(hub))
        self._stream_headers("video/mp2t")
        try:
            while time.time() < who.expires:
                try:
                    self.wfile.write(q.get(timeout=1.0))
                except queue.Empty:
                    if not hub.subscribed(q):
                        break  # dropped for falling behind
            else:
                log.info("viewer %s: session ended, stream closed", name)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            hub.unsubscribe(q)
            self.server.touch()
            log.info("viewer %s left", name)

    def _events(self, who: Identity) -> None:
        hub = self.server.events
        q, backlog = hub.subscribe()
        self._stream_headers("text/event-stream")
        try:
            self.wfile.write(b"event: backlog\ndata: " + json.dumps(backlog).encode() + b"\n\n")
            last = time.monotonic()
            while True:
                if time.time() >= who.expires:
                    self.wfile.write(b"event: expired\ndata: {}\n\n")
                    break
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
            self.server.touch()


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, video: Hub, events: Hub, verifier: JwtVerifier | None = None) -> None:
        super().__init__(addr, Handler)
        self.video = video
        self.events = events
        self.verifier = verifier
        self.last_active = time.time()  # when the last stream closed (or the server started)

    def touch(self) -> None:
        self.last_active = time.time()

    def status(self) -> dict:
        open_streams = len(self.video) + len(self.events)
        idle = 0.0 if open_streams else time.time() - self.last_active
        return {"streams": open_streams, "watching": len(self.video), "idle_seconds": round(idle, 1)}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Web monitor for the stitched output and event log")
    p.add_argument("--srt", default="srt://stitcher:9002?mode=caller&latency=200",
                   help="stitcher monitor output to pull")
    p.add_argument("--events-udp", default="0.0.0.0:5100", help="where components send JSON log events")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--backlog", type=int, default=500, help="events replayed to a newly opened page")
    p.add_argument("--auth-issuer", default=os.environ.get("AUTH_ISSUER"),
                   help="token issuer, e.g. https://cognito-idp.us-west-2.amazonaws.com/<pool id>; enables sign-in")
    p.add_argument("--auth-audience", default=os.environ.get("AUTH_AUDIENCE"),
                   help="expected token audience (the Cognito app client ID)")
    p.add_argument("--tls-port", type=int, default=int(os.environ.get("TLS_PORT", 0)),
                   help="also serve HTTPS on this port (the CloudFront origin)")
    p.add_argument("--tls-cert", default=os.environ.get("TLS_CERT"), help="certificate chain (PEM)")
    p.add_argument("--tls-key", default=os.environ.get("TLS_KEY"), help="private key (PEM)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    setup_logging(args.log_level)
    if bool(args.auth_issuer) != bool(args.auth_audience):
        p.error("--auth-issuer and --auth-audience go together")
    if args.tls_port and not (args.tls_cert and args.tls_key):
        p.error("--tls-port needs --tls-cert and --tls-key")
    verifier = JwtVerifier(args.auth_issuer, args.auth_audience) if args.auth_issuer else None

    video = Hub(maxsize=2000)            # ~20 s of 2.6 Mb/s in 1316-byte chunks
    events = Hub(maxsize=1000, backlog=args.backlog)
    logging.getLogger().addHandler(HubLogHandler(events))
    threading.Thread(target=pull_srt, args=(args.srt, video), daemon=True, name="srt").start()
    threading.Thread(target=receive_events, args=(args.events_udp, events), daemon=True, name="events").start()
    server = ViewerServer((args.host, args.port), video, events, verifier)
    if args.tls_port:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        tls = ViewerServer((args.host, args.tls_port), video, events, verifier)
        tls.socket = context.wrap_socket(tls.socket, server_side=True)
        tls.last_active = server.last_active
        server.touch = tls.touch = _shared_touch(server, tls)
        threading.Thread(target=tls.serve_forever, daemon=True, name="https").start()
        log.info("viewer https on %s:%d (%s)", args.host, args.tls_port, args.tls_cert)
    log.info("viewer on http://%s:%d (feed %s, events udp %s, %s)", args.host, args.port, args.srt, args.events_udp,
             f"sign-in via {verifier.issuer}" if verifier else "no authentication")
    server.serve_forever()


def _shared_touch(*servers: ViewerServer):
    """Both listeners share the hubs; keep their idle clocks in step too."""
    def touch() -> None:
        now = time.time()
        for s in servers:
            s.last_active = now
    return touch


if __name__ == "__main__":
    main()
