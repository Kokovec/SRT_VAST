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
* ``--preview-udp`` receives a copy of the stitcher's *incoming* stream and
  re-encodes it to a small, low-bitrate preview (``/preview.ts``: 320x180,
  15 fps, ~200 kb/s, no audio), so the page can show the pre-stitch feed next
  to the output for a fraction of a second full stream.
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
from typing import Callable

from . import setup_logging
from .auth import SESSION_COOKIE, AuthError, Identity, JwtVerifier, cookie_value
from .transport import udp_receiver
from .ts import PTS_HZ, PacketReader, find_pmt, get_pid, get_pts_dts, is_pusi, pts_diff, video_stream

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


class VideoClock:
    """The first video PTS in each chunk of one transport stream."""

    def __init__(self) -> None:
        self._held: list = []  # packets until the PMT has been seen
        self._vpid: int | None = None

    def pts(self, packets) -> int | None:
        first = None
        for pkt in packets:
            if self._vpid is None:
                self._held.append(pkt)
                pmt = find_pmt(self._held)
                video = video_stream(pmt) if pmt else None
                if video is not None:
                    self._vpid, self._held = video.pid, []
                elif len(self._held) > 2000:
                    self._held = self._held[-200:]
                continue
            if first is None and get_pid(pkt) == self._vpid and is_pusi(pkt):
                first = get_pts_dts(pkt)[0]
        return first


class StreamAligner:
    """Holds back whichever feed is ahead, so both publish the same content time together.

    The program feed and the incoming-stream preview carry the same timestamps
    (the stitcher keeps the content timeline through ad breaks; the preview keeps
    them with -copyts). For each program frame the aligner works out when the
    preview delivers that same timestamp (extrapolating if it has not yet),
    smooths the difference, and delays the earlier feed by it.
    """

    def __init__(self, hubs: dict[str, Hub], max_delay: float = 1.5, smoothing: float = 0.05,
                 clock=time.monotonic, start: bool = True) -> None:
        self.hubs = hubs
        self.max_delay = max_delay
        self.smoothing = smoothing
        self.clock = clock
        self.offset = 0.0  # seconds the preview delivers a given frame after the program feed
        self._history: dict[str, collections.deque] = {k: collections.deque(maxlen=120) for k in hubs}
        self._queues: dict[str, collections.deque] = {k: collections.deque() for k in hubs}
        self._cond = threading.Condition()
        if start:
            threading.Thread(target=self._release, daemon=True, name="align").start()

    def delay(self, name: str) -> float:
        lag = self.offset if name == "main" else -self.offset
        return min(self.max_delay, max(0.0, lag))

    def offer(self, name: str, data: bytes, pts: int | None) -> None:
        now = self.clock()
        with self._cond:
            if pts is not None:
                self._history[name].append((pts, now))
                if name == "main":
                    self._measure(pts, now)
            self._queues[name].append((now + self.delay(name), data))
            self._cond.notify()

    def _measure(self, pts: int, now: float) -> None:
        preview = self._history.get("preview")
        if not preview or len(preview) < 2 or now - preview[-1][1] > 2.0:
            self.offset += self.smoothing * (0.0 - self.offset)  # no live preview: drift back to no delay
            return
        last_pts, last_t = preview[-1]
        ahead = pts_diff(pts, last_pts)
        if ahead >= 0:
            when = last_t + ahead / PTS_HZ          # preview has not shown this frame yet
        else:
            older = [(p, t) for p, t in preview if pts_diff(p, pts) <= 0]
            if not older:
                return                               # older than the preview history
            p0, t0 = older[-1]
            when = t0 + pts_diff(pts, p0) / PTS_HZ
        diff = when - now
        if abs(diff) <= 5.0:                         # ignore jumps (a feed restarted)
            self.offset += self.smoothing * (diff - self.offset)

    def _release(self) -> None:
        while True:
            with self._cond:
                while True:
                    now = self.clock()
                    due = []
                    for name, queue in self._queues.items():
                        while queue and queue[0][0] <= now:
                            due.append((name, queue.popleft()[1]))
                    if due:
                        break
                    heads = [q[0][0] for q in self._queues.values() if q]
                    self._cond.wait(timeout=min(heads) - now if heads else 1.0)
            for name, data in due:
                self.hubs[name].publish(data)


def pull_srt(url: str, publish: Callable[[bytes, int | None], None]) -> None:
    """Keep an SRT caller connected to the stitcher; publish whole TS packets (with their video PTS)."""
    while True:
        proc = subprocess.Popen(["srt-live-transmit", "-q", "-a:no", url, "file://con"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        reader, clock, connected, received = PacketReader(), VideoClock(), False, 0
        while chunk := proc.stdout.read1(65536):
            packets = reader.feed(chunk)
            if not packets:
                continue
            if not connected:
                connected = True
                log.info("monitor feed connected: %s", url)
            received += len(chunk)
            publish(b"".join(packets), clock.pts(packets))
        proc.wait()
        if connected:
            log.warning("monitor feed lost after %.1f MB, reconnecting", received / 1e6)
        time.sleep(1)


PREVIEW_ENCODE = [
    "-map", "0:v:0", "-an", "-sn", "-dn",
    "-vf", "scale=320:180:flags=fast_bilinear,fps=15",
    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline",
    "-g", "30", "-b:v", "200k", "-maxrate", "230k", "-bufsize", "230k",
    "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1", "pipe:1",
]


def run_preview(addr: str, publish: Callable[[bytes, int | None], None]) -> None:
    """Re-encode the tapped incoming stream to a small preview; restart on any exit."""
    host, _, port = addr.rpartition(":")
    url = f"udp://{host or '0.0.0.0'}:{port}?fifo_size=50000&overrun_nonfatal=1&timeout=5000000"
    while True:
        # -copyts: keep the incoming timestamps, so preview and output share one timeline.
        proc = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-copyts",
                                 "-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32768", "-analyzeduration", "0",
                                 "-i", url, *PREVIEW_ENCODE], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        reader, clock, started = PacketReader(), VideoClock(), False
        while chunk := proc.stdout.read1(65536):
            packets = reader.feed(chunk)
            if packets:
                if not started:
                    started = True
                    log.info("incoming-stream preview running (udp %s)", addr)
                publish(b"".join(packets), clock.pts(packets))
        proc.wait()
        if started:
            log.warning("incoming-stream preview stopped (input gone?), restarting")
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
                        "auth": self.server.verifier is not None, "preview": self.server.preview is not None})
        elif path == "/live.ts":
            self._live(who, self.server.video, "watching")
        elif path == "/preview.ts" and self.server.preview is not None:
            self._live(who, self.server.preview, "previewing")
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

    def _live(self, who: Identity, hub: Hub, what: str) -> None:
        q, _ = hub.subscribe()
        name = who.email or self.address_string()
        log.info("viewer %s %s (%d now)", name, what, len(hub))
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

    def __init__(self, addr, video: Hub, events: Hub, verifier: JwtVerifier | None = None,
                 preview: Hub | None = None) -> None:
        super().__init__(addr, Handler)
        self.video = video
        self.events = events
        self.preview = preview
        self.aligner: StreamAligner | None = None
        self.verifier = verifier
        self.last_active = time.time()  # when the last stream closed (or the server started)

    def touch(self) -> None:
        self.last_active = time.time()

    def status(self) -> dict:
        open_streams = len(self.video) + len(self.events) + (len(self.preview) if self.preview else 0)
        idle = 0.0 if open_streams else time.time() - self.last_active
        status = {"streams": open_streams, "watching": len(self.video), "idle_seconds": round(idle, 1)}
        if self.aligner is not None:
            status["preview_lag_ms"] = round(self.aligner.offset * 1000)
        return status


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Web monitor for the stitched output and event log")
    p.add_argument("--srt", default="srt://stitcher:9002?mode=caller&latency=40",
                   help="stitcher monitor output to pull")
    p.add_argument("--events-udp", default="0.0.0.0:5100", help="where components send JSON log events")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--backlog", type=int, default=500, help="events replayed to a newly opened page")
    p.add_argument("--auth-issuer", default=os.environ.get("AUTH_ISSUER"),
                   help="token issuer, e.g. https://cognito-idp.us-west-2.amazonaws.com/<pool id>; enables sign-in")
    p.add_argument("--auth-audience", default=os.environ.get("AUTH_AUDIENCE"),
                   help="expected token audience (the Cognito app client ID)")
    p.add_argument("--preview-udp", default=os.environ.get("PREVIEW_UDP", ""),
                   help="receive the stitcher's incoming-stream tap here (e.g. 0.0.0.0:5200) and serve /preview.ts")
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
    preview, aligner = None, None
    if args.preview_udp:
        preview = Hub(maxsize=500)       # ~0.2 Mb/s: generous
        aligner = StreamAligner({"main": video, "preview": preview})
        publish_main = lambda data, pts: aligner.offer("main", data, pts)        # noqa: E731
        publish_preview = lambda data, pts: aligner.offer("preview", data, pts)  # noqa: E731
        threading.Thread(target=run_preview, args=(args.preview_udp, publish_preview), daemon=True,
                         name="preview").start()
    else:
        publish_main = lambda data, pts: video.publish(data)  # noqa: E731
    threading.Thread(target=pull_srt, args=(args.srt, publish_main), daemon=True, name="srt").start()
    threading.Thread(target=receive_events, args=(args.events_udp, events), daemon=True, name="events").start()
    server = ViewerServer((args.host, args.port), video, events, verifier, preview)
    server.aligner = aligner
    if args.tls_port:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.tls_cert, args.tls_key)
        tls = ViewerServer((args.host, args.tls_port), video, events, verifier, preview)
        tls.aligner = aligner
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
