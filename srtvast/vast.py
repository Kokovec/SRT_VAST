"""Minimal VAST 2/3/4 client: fetch, follow wrappers, pick media, fire beacons."""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Callable

from .hls import HLS_MIME_TYPES

log = logging.getLogger("vast")

TS_MIME_TYPES = ("video/mp2t",)


@dataclass
class MediaFile:
    url: str
    mime_type: str = ""
    delivery: str = ""
    width: int = 0
    height: int = 0
    bitrate: int = 0


@dataclass
class Ad:
    ad_id: str
    sequence: int | None = None
    title: str = ""
    duration: float | None = None  # seconds, from <Duration>
    media_files: list[MediaFile] = field(default_factory=list)
    impressions: list[str] = field(default_factory=list)
    tracking: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def pick_media(self, preference=(HLS_MIME_TYPES, TS_MIME_TYPES),
                   extensions=(".m3u8", ".ts")) -> MediaFile | None:
        """First media file of the most preferred kind: HLS, then MPEG-TS.

        Matched on MIME type, falling back to the URL's extension.
        """
        for mime_types, ext in zip(preference, extensions):
            for m in self.media_files:
                if m.mime_type.lower() in mime_types:
                    return m
            for m in self.media_files:
                if urllib.parse.urlsplit(m.url).path.lower().endswith(ext):
                    return m
        return None


@dataclass
class Wrapper:
    ad_tag_uri: str
    sequence: int | None = None
    impressions: list[str] = field(default_factory=list)
    tracking: dict[str, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Parsing (namespace-agnostic, VAST 4 documents may declare xmlns)
# --------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name]


def _child(el, name: str):
    found = _children(el, name) if el is not None else []
    return found[0] if found else None


def _descendants(el, name: str) -> list:
    return [e for e in el.iter() if _local(e.tag) == name]


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_duration(text: str) -> float | None:
    try:
        h, m, s = text.strip().split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def _tracking(el) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in _descendants(el, "Tracking"):
        event, url = t.get("event"), _text(t)
        if event and url:
            out.setdefault(event, []).append(url)
    return out


def parse_vast(xml: bytes | str) -> list[Ad | Wrapper]:
    root = ET.fromstring(xml)
    if _local(root.tag) != "VAST":
        raise ValueError(f"root element is <{_local(root.tag)}>, not <VAST>")
    items: list[Ad | Wrapper] = []
    for ad_el in _children(root, "Ad"):
        seq = ad_el.get("sequence")
        sequence = int(seq) if seq and seq.isdigit() else None
        inline, wrapper = _child(ad_el, "InLine"), _child(ad_el, "Wrapper")
        body = inline if inline is not None else wrapper
        if body is None:
            continue
        impressions = [_text(e) for e in _children(body, "Impression") if _text(e)]
        errors = [_text(e) for e in _children(body, "Error") if _text(e)]
        tracking = _tracking(body)
        if inline is not None:
            linear = next(iter(_descendants(inline, "Linear")), None)
            media = [
                MediaFile(_text(m), m.get("type", ""), m.get("delivery", ""),
                          _int(m.get("width")), _int(m.get("height")), _int(m.get("bitrate")))
                for m in _descendants(inline, "MediaFile") if _text(m)
            ]
            items.append(Ad(
                ad_id=ad_el.get("id", ""),
                sequence=sequence,
                title=_text(_child(inline, "AdTitle")),
                duration=parse_duration(_text(_child(linear, "Duration"))) if linear is not None else None,
                media_files=media,
                impressions=impressions,
                tracking=tracking,
                errors=errors,
            ))
        else:
            uri = _text(_child(wrapper, "VASTAdTagURI"))
            if uri:
                items.append(Wrapper(uri, sequence, impressions, tracking, errors))
    return items


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def http_get(url: str, timeout: float = 2.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "srt-vast-stitcher/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_ads(url: str, timeout: float = 2.0, max_wrappers: int = 5,
              fetch: Callable[[str], bytes] | None = None) -> list[Ad]:
    """Fetch a VAST tag, resolve wrappers and return the ads to play, in order."""
    fetch = fetch or (lambda u: http_get(u, timeout))
    return _resolve(url, fetch, max_wrappers)


def _resolve(url: str, fetch: Callable[[str], bytes], depth: int) -> list[Ad]:
    if depth < 0:
        raise ValueError("VAST wrapper limit exceeded")
    ads: list[Ad] = []
    for item in parse_vast(fetch(url)):
        if isinstance(item, Ad):
            ads.append(item)
            continue
        try:
            inner = _resolve(item.ad_tag_uri, fetch, depth - 1)
        except Exception as exc:  # noqa: BLE001 - any failure means "no ad"
            log.warning("wrapper %s failed: %s", item.ad_tag_uri, exc)
            fire(item.errors, error_code=303)
            continue
        for ad in inner:
            ad.impressions = item.impressions + ad.impressions
            ad.errors = item.errors + ad.errors
            for event, urls in item.tracking.items():
                ad.tracking[event] = urls + ad.tracking.get(event, [])
            if ad.sequence is None:
                ad.sequence = item.sequence
            ads.append(ad)
    pod = sorted((a for a in ads if a.sequence is not None), key=lambda a: a.sequence)
    return pod if pod else ads[:1]


# --------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------

def expand_macros(url: str, error_code: int | None = None) -> str:
    url = url.replace("[CACHEBUSTING]", f"{random.randint(0, 99_999_999):08d}")
    url = url.replace("[TIMESTAMP]", urllib.parse.quote(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    if error_code is not None:
        url = url.replace("[ERRORCODE]", str(error_code))
    return url


def fire(urls: list[str], error_code: int | None = None, timeout: float = 2.0) -> None:
    """Fire beacons in the background; failures are logged, never raised."""
    if not urls:
        return

    def run() -> None:
        for u in urls:
            try:
                http_get(expand_macros(u, error_code), timeout)
            except Exception as exc:  # noqa: BLE001
                log.warning("beacon failed %s: %s", u, exc)

    threading.Thread(target=run, daemon=True).start()


class AdTracker:
    """Fires impression and linear tracking events as an ad plays out."""

    QUARTILES = ((0.25, "firstQuartile"), (0.5, "midpoint"), (0.75, "thirdQuartile"))

    def __init__(self, ad: Ad, duration: float, fire_fn=fire) -> None:
        self.ad = ad
        self.duration = duration
        self._fire = fire_fn
        self.fired: list[str] = []

    def _event(self, name: str) -> None:
        if name in self.fired:
            return
        self.fired.append(name)
        urls = self.ad.impressions if name == "impression" else self.ad.tracking.get(name, [])
        log.info("tracking %-13s ad=%s urls=%d", name, self.ad.ad_id, len(urls))
        self._fire(urls)

    def start(self) -> None:
        self._event("impression")
        self._event("start")

    def progress(self, seconds: float) -> None:
        for fraction, name in self.QUARTILES:
            if seconds >= fraction * self.duration:
                self._event(name)

    def complete(self) -> None:
        self.progress(self.duration)
        self._event("complete")

    def error(self, code: int) -> None:
        self._fire(self.ad.errors, error_code=code)
