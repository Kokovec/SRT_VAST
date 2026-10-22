"""Minimal HLS VOD client: resolve playlists, download segments, transmux to TS.

Ad creatives are delivered as HLS (master -> media playlist -> segments). The
stitcher splices at the MPEG-TS level, so the downloaded rendition is turned
back into a single transport stream:

* MPEG-TS segments are simply concatenated;
* fMP4/CMAF segments (``EXT-X-MAP`` init + ``moof``/``mdat`` fragments) are
  concatenated into one fragmented MP4 and remuxed to TS by ffmpeg with
  ``-c copy`` (H.264 to Annex B, AAC to ADTS). No re-encode.

Not supported (the mock ADS never produces them): encryption, byte ranges,
alternate audio renditions.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("hls")

HLS_MIME_TYPES = ("application/x-mpegurl", "application/vnd.apple.mpegurl")

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^",]*)')


@dataclass
class Variant:
    url: str
    bandwidth: int = 0
    resolution: str = ""
    codecs: str = ""


@dataclass
class Segment:
    url: str
    duration: float


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    init_url: str | None = None
    ended: bool = False

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)


def is_hls(mime_type: str, url: str) -> bool:
    return mime_type.lower() in HLS_MIME_TYPES or urllib.parse.urlsplit(url).path.lower().endswith(".m3u8")


def _attrs(text: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


def parse_playlist(text: str, base_url: str) -> list[Variant] | MediaPlaylist:
    """Parse a playlist: a master gives its variants, a media playlist its segments."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("not an HLS playlist")
    variants: list[Variant] = []
    media = MediaPlaylist()
    pending_variant: Variant | None = None
    pending_duration: float | None = None
    for line in lines[1:]:
        tag, _, value = line.partition(":")
        if tag == "#EXT-X-STREAM-INF":
            a = _attrs(value)
            pending_variant = Variant("", int(a.get("BANDWIDTH", 0)), a.get("RESOLUTION", ""), a.get("CODECS", ""))
        elif tag == "#EXTINF":
            pending_duration = float(value.split(",", 1)[0])
        elif tag == "#EXT-X-MAP":
            media.init_url = urllib.parse.urljoin(base_url, _attrs(value)["URI"])
        elif tag == "#EXT-X-KEY" and _attrs(value).get("METHOD", "NONE") != "NONE":
            raise ValueError("encrypted HLS is not supported")
        elif tag == "#EXT-X-BYTERANGE":
            raise ValueError("byte-range HLS is not supported")
        elif tag == "#EXT-X-ENDLIST":
            media.ended = True
        elif not line.startswith("#"):
            url = urllib.parse.urljoin(base_url, line)
            if pending_variant is not None:
                pending_variant.url = url
                variants.append(pending_variant)
                pending_variant = None
            elif pending_duration is not None:
                media.segments.append(Segment(url, pending_duration))
                pending_duration = None
    return variants if variants else media


def pick_variant(variants: list[Variant], max_bandwidth: int | None = None) -> Variant:
    """Highest bandwidth not above ``max_bandwidth`` (or the lowest if none fit)."""
    ordered = sorted(variants, key=lambda v: v.bandwidth)
    fitting = [v for v in ordered if max_bandwidth is None or v.bandwidth <= max_bandwidth]
    return fitting[-1] if fitting else ordered[0]


def resolve(url: str, fetch: Callable[[str], bytes], max_bandwidth: int | None = None,
            max_depth: int = 3) -> tuple[str, MediaPlaylist]:
    """Follow master playlists down to a media playlist."""
    for _ in range(max_depth):
        parsed = parse_playlist(fetch(url).decode("utf-8", "replace"), url)
        if isinstance(parsed, MediaPlaylist):
            if not parsed.segments:
                raise ValueError(f"{url}: media playlist has no segments")
            if not parsed.ended:
                log.warning("%s: no EXT-X-ENDLIST, using the %d segments listed", url, len(parsed.segments))
            return url, parsed
        variant = pick_variant(parsed, max_bandwidth)
        log.info("HLS master %s -> variant %s (%d bps %s)", url, variant.url, variant.bandwidth,
                 variant.resolution or "?")
        url = variant.url
    raise ValueError(f"{url}: playlist nesting too deep")


def transmux(init: bytes | None, segments: list[bytes]) -> bytes:
    """Turn downloaded segments into one MPEG-TS."""
    if init is None and all(s[:1] == b"\x47" for s in segments):
        return b"".join(segments)
    with tempfile.NamedTemporaryFile(suffix=".mp4") as mp4:
        if init:
            mp4.write(init)
        for s in segments:
            mp4.write(s)
        mp4.flush()
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mp4", "-i", mp4.name,
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", "pipe:1"],
            capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"transmux failed: {proc.stderr.decode(errors='replace').strip()[-300:]}")
    return proc.stdout


def load_ts(url: str, fetch: Callable[[str], bytes], max_bandwidth: int | None = None,
            remux: Callable[[bytes | None, list[bytes]], bytes] = transmux) -> bytes:
    """Download an HLS VOD rendition and return it as a single transport stream."""
    media_url, playlist = resolve(url, fetch, max_bandwidth)
    init = fetch(playlist.init_url) if playlist.init_url else None
    segments = [fetch(s.url) for s in playlist.segments]
    ts = remux(init, segments)
    log.info("HLS %s: %d segment(s), %.3fs, %s -> %d bytes TS", media_url, len(segments), playlist.duration,
             "fMP4 transmuxed" if init is not None else "TS", len(ts))
    return ts
