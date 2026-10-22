"""Mock ad decision server (ADS).

* ``GET /vast?dur=30&event=1001``  VAST 4.2 pod filling ``dur`` seconds
* ``GET /wrapper?...``             VAST Wrapper pointing at ``/vast`` (tests wrapper handling)
* ``GET /hls/<id>/master.m3u8``    the ad creative as HLS (fMP4 segments)
* ``GET /media/<id>.ts``           the ad creative as MPEG-TS
* ``GET /mez/<id>.mp4``            the creative's mezzanine master (high quality,
  not encoded like the stream: an SSAI system conforms it before splicing)
* ``GET /track?...``, ``/error?...`` beacon sinks, logged

Creatives are the ``*.ts`` files in the media directory (optionally with a
``<id>.title`` sidecar). Their durations are measured from the TS itself. When
``hls/<id>/master.m3u8`` exists the VAST MediaFile points at the HLS package,
otherwise at the TS file. With ``--delivery mezzanine`` (the default) the VAST
carries only the VAST 4 ``<Mezzanine>`` element for creatives that have a
``mez/<id>.mp4`` master, imitating an SSAI integration where the stitcher
fetches the mezzanine and transcodes it to the stream's profile itself.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import subprocess
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import setup_logging
from .ts import PTS_HZ, find_pmt, get_pid, get_pts_dts, is_pusi, pts_diff, split_packets, video_stream

log = logging.getLogger("ads")

HLS_CONTENT_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".mp4": "video/mp4",
                     ".m4s": "video/iso.segment", ".ts": "video/MP2T"}


@dataclass
class Creative:
    ad_id: str
    title: str
    path: Path
    duration: float
    width: int = 1280
    height: int = 720
    hls: bool = False
    mezzanine: "Mezzanine | None" = None


@dataclass
class Mezzanine:
    path: Path
    width: int
    height: int
    fps: str
    codec: str  # RFC 6381, e.g. avc1.640028
    size: int


def probe_mezzanine(path: Path) -> Mezzanine:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,avg_frame_rate,codec_name,profile,level", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    v = json.loads(out)["streams"][0]
    profile = {"High": "6400", "Main": "4d40", "Constrained Baseline": "42e0"}.get(v.get("profile"), "6400")
    codec = f"avc1.{profile}{int(v.get('level', 40)):02x}" if v.get("codec_name") == "h264" else v.get("codec_name", "")
    num, _, den = v.get("avg_frame_rate", "0/1").partition("/")
    fps = f"{int(num) / int(den or 1):g}" if int(den or 1) else "?"
    return Mezzanine(path, v["width"], v["height"], fps, codec, path.stat().st_size)


def measure_duration(data: bytes) -> float:
    packets = split_packets(data)
    pmt = find_pmt(packets)
    video = video_stream(pmt) if pmt else None
    if video is None:
        raise ValueError("no video stream")
    pts = [p for p in (get_pts_dts(pkt)[0] for pkt in packets
                       if get_pid(pkt) == video.pid and is_pusi(pkt)) if p is not None]
    rel = sorted(pts_diff(p, pts[0]) for p in pts)
    steps = [b - a for a, b in zip(rel, rel[1:]) if b > a]
    frame = sorted(steps)[len(steps) // 2] if steps else 3000
    return (rel[-1] + frame) / PTS_HZ


def scan_library(media_dir: Path) -> list[Creative]:
    library = []
    for path in sorted(media_dir.glob("*.ts")):
        title_file = path.with_suffix(".title")
        title = title_file.read_text().strip() if title_file.exists() else path.stem
        hls = (media_dir / "hls" / path.stem / "master.m3u8").exists()
        mez_path = media_dir / "mez" / f"{path.stem}.mp4"
        mez = probe_mezzanine(mez_path) if mez_path.exists() else None
        library.append(Creative(path.stem, title, path, round(measure_duration(path.read_bytes()), 3),
                                hls=hls, mezzanine=mez))
    return library


def served_as(c: Creative, delivery: str) -> str:
    """What the VAST for this creative carries under ``delivery`` (see ``_media_files``)."""
    ready = "HLS" if c.hls else "MPEG-TS"
    if c.mezzanine is None or delivery == "streaming":
        return f"{ready} (ready to splice)"
    m = c.mezzanine
    mez = f"mezzanine {m.width}x{m.height}@{m.fps} {m.codec}, {m.size / 1e6:.1f} MB"
    return mez if delivery == "mezzanine" else f"{mez} + {ready}"


def select_pod(library: list[Creative], duration: float, start: int) -> list[Creative]:
    """Fill ``duration`` from the library, rotating the starting creative."""
    pod, total = [], 0.0
    for i in range(len(library)):
        c = library[(start + i) % len(library)]
        if total + c.duration <= duration + 0.25:
            pod.append(c)
            total += c.duration
    if not pod and library:
        pod = [min(library, key=lambda c: c.duration)]
    return pod


def _hms(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _cdata(url: str) -> str:
    return f"<![CDATA[{url}]]>"


def _media_file(c: Creative, base_url: str) -> str:
    if c.hls:
        mime, url = "application/x-mpegURL", f"{base_url}/hls/{c.ad_id}/master.m3u8"
    else:
        mime, url = "video/MP2T", f"{base_url}/media/{c.path.name}"
    return (f'<MediaFile delivery="streaming" type="{mime}" width="{c.width}" height="{c.height}" '
            f'bitrate="2600">{_cdata(url)}</MediaFile>')


def _mezzanine(c: Creative, base_url: str) -> str:
    m = c.mezzanine
    return (f'<Mezzanine delivery="progressive" type="video/mp4" width="{m.width}" height="{m.height}" '
            f'codec="{m.codec}" fileSize="{m.size}" mediaType="2D">'
            f'{_cdata(f"{base_url}/mez/{c.ad_id}.mp4")}</Mezzanine>')


def _media_files(c: Creative, base_url: str, delivery: str) -> str:
    """``delivery``: streaming (ready-to-splice file), mezzanine (master only), or both."""
    if c.mezzanine is None or delivery == "streaming":
        return _media_file(c, base_url)
    if delivery == "mezzanine":
        return _mezzanine(c, base_url)
    return _media_file(c, base_url) + "\n              " + _mezzanine(c, base_url)


def build_vast(pod: list[Creative], base_url: str, event: str, delivery: str = "streaming") -> str:
    ads = []
    for seq, c in enumerate(pod, 1):
        q = f"ad={c.ad_id}&break={urllib.parse.quote(event)}&seq={seq}"
        track = lambda ev: f"{base_url}/track?event={ev}&{q}&cb=[CACHEBUSTING]"  # noqa: E731
        tracking = "\n".join(
            f'              <Tracking event="{ev}">{_cdata(track(ev))}</Tracking>'
            for ev in ("start", "firstQuartile", "midpoint", "thirdQuartile", "complete")
        )
        ads.append(f"""  <Ad id={quoteattr(f"{c.ad_id}-{event}-{seq}")} sequence="{seq}">
    <InLine>
      <AdSystem version="0.1">SRT-VAST mock ADS</AdSystem>
      <AdServingId>{escape(f"{event}-{seq}")}</AdServingId>
      <AdTitle>{escape(c.title)}</AdTitle>
      <Error>{_cdata(f"{base_url}/error?code=[ERRORCODE]&{q}")}</Error>
      <Impression id="imp-{seq}">{_cdata(track("impression"))}</Impression>
      <Creatives>
        <Creative id={quoteattr(c.ad_id)} sequence="1">
          <Linear>
            <Duration>{_hms(c.duration)}</Duration>
            <TrackingEvents>
{tracking}
            </TrackingEvents>
            <MediaFiles>
              {_media_files(c, base_url, delivery)}
            </MediaFiles>
          </Linear>
        </Creative>
      </Creatives>
    </InLine>
  </Ad>""")
    body = "\n".join(ads)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<VAST version="4.2" xmlns="http://www.iab.com/VAST">\n{body}\n</VAST>\n'


def build_wrapper(tag_uri: str, base_url: str, event: str) -> str:
    q = f"break={urllib.parse.quote(event)}&wrapper=1"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<VAST version="4.2" xmlns="http://www.iab.com/VAST">
  <Ad id="wrapper-{escape(event)}">
    <Wrapper>
      <AdSystem>SRT-VAST mock ADS (wrapper)</AdSystem>
      <Impression>{_cdata(f"{base_url}/track?event=impression&{q}")}</Impression>
      <VASTAdTagURI>{_cdata(tag_uri)}</VASTAdTagURI>
    </Wrapper>
  </Ad>
</VAST>
"""


class AdServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, library: list[Creative], base_url: str, media_dir: Path,
                 delivery: str = "mezzanine") -> None:
        super().__init__(addr, Handler)
        self.library = library
        self.delivery = delivery
        self.by_id = {c.ad_id: c for c in library}
        self.hls_dir = (media_dir / "hls").resolve()
        self.by_name = {c.path.name: c for c in library}
        self.base_url = base_url.rstrip("/")
        self._rotation = itertools.count()
        self._lock = threading.Lock()

    def next_rotation(self) -> int:
        with self._lock:
            return next(self._rotation)


class Handler(BaseHTTPRequestHandler):
    server: AdServer

    def log_message(self, fmt, *args) -> None:
        log.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, code: int, body: bytes = b"", ctype: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        srv = self.server
        event = q.get("event", q.get("break", "0"))

        if url.path == "/vast":
            try:
                duration = float(q.get("dur", "30"))
            except ValueError:
                duration = 30.0
            pod = select_pod(srv.library, duration, srv.next_rotation())
            log.info("VAST request break=%s dur=%.1fs -> %s", event, duration,
                     ", ".join(f"{c.ad_id}({c.duration:g}s)" for c in pod) or "no fill")
            self._send(200, build_vast(pod, srv.base_url, event, srv.delivery).encode(), "application/xml")
        elif url.path == "/wrapper":
            inner = f"{srv.base_url}/vast?{url.query}"
            log.info("VAST wrapper request break=%s -> %s", event, inner)
            self._send(200, build_wrapper(inner, srv.base_url, event).encode(), "application/xml")
        elif url.path.startswith("/media/"):
            creative = srv.by_name.get(url.path.rsplit("/", 1)[-1])
            if creative is None:
                self._send(404, b"no such creative")
                return
            log.info("media %s -> %s", creative.path.name, self.address_string())
            self._send(200, creative.path.read_bytes(), "video/MP2T")
        elif url.path.startswith("/mez/"):
            name = url.path.rsplit("/", 1)[-1]
            creative = srv.by_id.get(name.removesuffix(".mp4"))
            if creative is None or creative.mezzanine is None or not name.endswith(".mp4"):
                self._send(404, b"no such mezzanine")
                return
            m = creative.mezzanine
            log.info("mezzanine %s (%dx%d@%s, %.1f MB) -> %s", name, m.width, m.height, m.fps,
                     m.size / 1e6, self.address_string())
            self._send(200, m.path.read_bytes(), "video/mp4")
        elif url.path.startswith("/hls/"):
            path = (srv.hls_dir / url.path[len("/hls/"):]).resolve()
            if not path.is_relative_to(srv.hls_dir) or not path.is_file():
                self._send(404, b"not found")
                return
            if path.suffix == ".m3u8":
                log.info("HLS %s -> %s", url.path, self.address_string())
            self._send(200, path.read_bytes(), HLS_CONTENT_TYPES.get(path.suffix, "application/octet-stream"))
        elif url.path == "/track":
            log.info("BEACON %-13s ad=%s break=%s seq=%s", q.get("event"), q.get("ad", "-"),
                     q.get("break"), q.get("seq", "-"))
            self._send(204)
        elif url.path == "/error":
            log.warning("ERROR beacon code=%s ad=%s break=%s", q.get("code"), q.get("ad"), q.get("break"))
            self._send(204)
        elif url.path == "/health":
            self._send(200, b"ok")
        else:
            self._send(404, b"not found")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Mock VAST ad decision server")
    p.add_argument("--media-dir", type=Path, default=Path("/ads"))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--public-url", default="http://ads:8080", help="base URL used in VAST responses")
    p.add_argument("--delivery", choices=("mezzanine", "streaming", "both"), default="mezzanine",
                   help="what the VAST MediaFiles carry: the mezzanine master, a ready-to-splice stream, or both")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    setup_logging(args.log_level)

    library = scan_library(args.media_dir)
    if not library:
        log.warning("no creatives in %s; every request will be a no-fill", args.media_dir)
    server = AdServer((args.host, args.port), library, args.public_url, args.media_dir, args.delivery)
    for c in library:
        log.info("creative %-5s %6.3fs  %-15s VAST delivers: %s", c.ad_id, c.duration, c.title,
                 served_as(c, args.delivery))
    log.info("mock ADS on %s:%d (public %s, delivery %s)", args.host, args.port, args.public_url, args.delivery)
    server.serve_forever()


if __name__ == "__main__":
    main()
