"""SCTE-35 driven, frame-accurate MPEG-TS ad stitcher.

Receives the content transport stream, watches the SCTE-35 PID for splice
cues, asks the ADS for a pod over VAST, and splices the ads into the
elementary-stream PIDs at the TS level:

* Video is cut on the exact frame. The last content frame is the one before
  the cue PTS, the first ad frame is presented at the cue PTS, and content
  resumes on the frame at cue PTS + break duration. Splice-out needs no
  content IDR (every ad starts with one). Splice-in needs an IDR on the
  return frame, i.e. a conditioned encoder, as in any SCTE-104/35 workflow;
  without one the break is held to the next content IDR and logged as an
  error.
* Audio is cut on whole audio frames (AAC ADTS). Each ad's audio is moved
  onto the content's audio frame grid (by at most half a frame, 10.7 ms at
  48 kHz, well inside broadcast lip-sync tolerance), each frame belongs to
  the side its midpoint falls on, and no frame may overlap the one before
  it. The audio is therefore gapless and overlap-free through every splice
  while the content audio is untouched. A PES that straddles a splice point
  is split and re-packetized (see ``Lane``).
* The break is filled to the frame. An ad running past the break end is cut
  on the frame, and a pod shorter than the break is padded with a slate
  encoded like the content.
* Ad PTS/DTS/PCR are rebased onto the content timeline so output time is
  continuous, continuity counters are rewritten per PID, and ad packets are
  paced by the content PCR so the output stays real time.

Ad creatives arrive as HLS (fMP4 or TS segments, see ``hls``), which is
downloaded and transmuxed back into a single transport stream, or as a plain
MPEG-TS file.

The ad media must be encoded to match the content (codec, resolution, frame
rate, no B-frames, audio format), which is how SSAI ad transcoding works in
practice.
"""

from __future__ import annotations

import argparse
import logging
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from . import hls, scte35, setup_logging, vast
from .transport import UdpForwarder, UdpPacketWriter, udp_receiver
from .ts import (
    PID_PAT, PTS_HZ, PTS_MOD, STREAM_TYPE_AAC_ADTS, STREAM_TYPE_SCTE35, VIDEO_STREAM_TYPES,
    PacketReader, SectionAssembler, adts_frames, build_pes, find_pmt, get_cc, get_pcr,
    get_pid, get_pts_dts, has_payload, is_keyframe, is_pusi, packetize_pes, parse_pat,
    parse_pes, parse_pmt, payload, pcr_diff, pts_diff, section_crc_ok, set_cc, set_pcr,
    set_pid, shift_pts_dts, split_packets,
)

log = logging.getLogger("stitcher")

PCR_HZ = PTS_HZ * 300
TOLERANCE = PTS_HZ // 60     # video timestamps this close are the same frame (half a frame at 30 fps)
STALL_WARN = 0.3             # seconds: warn when input stops arriving, or one chunk takes this long
MAX_LATE_CUE = PTS_HZ        # ignore cues whose splice time is >1 s in the past
HOLD_SPAN = PTS_HZ           # audio PES starting this close to a splice point are held until complete
SLATE_LEAD = 2 * PCR_HZ      # with no known break end, keep this much slate queued ahead
STRAGGLER = PCR_HZ // 2      # close a lane this long after its splice point even if its source went quiet


# --------------------------------------------------------------------------
# Ad pods
# --------------------------------------------------------------------------

@dataclass
class PodEntry:
    rel27: int        # emission time relative to the start of the pod (27 MHz)
    ad_index: int
    pid: int          # output (content) PID
    packet: bytes


@dataclass
class PodAd:
    ad: vast.Ad | None
    first_pts: int    # first video PTS in the ad's own timeline
    duration: int     # 90 kHz, a whole number of frames
    pts_base: int     # start offset within the pod, 90 kHz
    slate: bool = False

    @property
    def label(self) -> str:
        if self.slate:
            return "slate"
        if self.ad is None:
            return "ad"
        return f"{self.ad.ad_id} '{self.ad.title}'" if self.ad.title else self.ad.ad_id


@dataclass
class Pod:
    ads: list[PodAd] = field(default_factory=list)
    entries: list[PodEntry] = field(default_factory=list)

    @property
    def duration(self) -> int:
        return sum(a.duration for a in self.ads)


def append_media(pod: Pod, ad: vast.Ad | None, data: bytes, content_streams: dict[int, int],
                 slate: bool = False) -> PodAd:
    """Append one ad transport stream to the pod's paced packet list.

    ``content_streams`` maps content PID -> stream_type; ad PIDs are mapped to
    the content PID carrying the same stream type.
    """
    packets = split_packets(data)
    pmt = find_pmt(packets)
    if pmt is None:
        raise ValueError("ad media has no PAT/PMT")
    pid_map: dict[int, int] = {}
    free = dict(content_streams)
    for s in pmt.streams:
        match = next((pid for pid, st in free.items() if st == s.stream_type), None)
        if match is not None:
            pid_map[s.pid] = match
            del free[match]
    video = next((s for s in pmt.streams if s.stream_type in VIDEO_STREAM_TYPES), None)
    if video is None or video.pid not in pid_map:
        raise ValueError("ad video stream does not match the content")

    index = len(pod.ads)
    start27 = pod.duration * 300
    first_pcr = last_pcr = None
    video_pts: list[int] = []
    for pkt in packets:
        pid = get_pid(pkt)
        if pid not in pid_map:
            continue  # PSI, SDT, null and unmatched streams
        pcr = get_pcr(pkt)
        if pcr is not None:
            first_pcr = pcr if first_pcr is None else first_pcr
            last_pcr = pcr
        if pid == video.pid and is_pusi(pkt):
            pts, _ = get_pts_dts(pkt)
            if pts is not None:
                video_pts.append(pts)
        rel = pcr_diff(last_pcr, first_pcr) if last_pcr is not None else 0
        set_pid(pkt, pid_map[pid])
        pod.entries.append(PodEntry(start27 + rel, index, pid_map[pid], bytes(pkt)))
    if not video_pts:
        raise ValueError("ad media has no timestamped video")

    first = video_pts[0]
    rel_pts = sorted(pts_diff(p, first) for p in video_pts)
    steps = [b - a for a, b in zip(rel_pts, rel_pts[1:]) if b > a]
    frame = sorted(steps)[len(steps) // 2] if steps else 3000
    pa = PodAd(ad, first, rel_pts[-1] + frame, pod.duration, slate)
    pod.ads.append(pa)
    return pa


def build_pod(media: list[tuple[vast.Ad | None, bytes]], content_streams: dict[int, int]) -> Pod:
    """Turn downloaded ad transport streams into one paced packet list."""
    pod = Pod()
    for ad, data in media:
        append_media(pod, ad, data, content_streams)
    return pod


class VastPodLoader:
    """Default pod loader: the SSAI "ad decision -> creative" leg.

    VAST request, then per ad, in parallel:

    * a VAST 4 ``<Mezzanine>`` (with ``conform_cmd`` set): fetch the master and
      transcode it to the stream's profile. The result is cached per creative
      (mezzanine URL), so only the first break that uses a creative pays for
      the transcode; a transcode still running when another break asks for the
      same creative is shared, not repeated;
    * otherwise a ready-to-splice MediaFile: HLS (transmuxed to TS) or MPEG-TS,
      downloaded and cached.

    The call returns when every ad is ready. If that is after the splice point,
    the stitcher keeps the break on content; the transcodes still finish and
    land in the cache for the next break.
    """

    def __init__(self, url_template: str, timeout: float = 3.0, conform_cmd: str | None = None,
                 workers: int = 4) -> None:
        self.url_template = url_template
        self.timeout = timeout
        self.conform_cmd = conform_cmd
        self._pool = ThreadPoolExecutor(workers, thread_name_prefix="creative")
        self._jobs: dict[str, Future] = {}   # media/mezzanine URL -> TS bytes (done = cached)
        self._lock = threading.Lock()

    def __call__(self, brk: "Break", content_streams: dict[int, int]) -> Pod:
        seconds = (brk.duration or 30 * PTS_HZ) / PTS_HZ
        url = self.url_template.format(duration=f"{seconds:g}", event_id=brk.event_id,
                                       cachebuster=random.randint(0, 99_999_999))
        log.info("VAST request %s", url)
        ads = vast.fetch_ads(url, timeout=self.timeout)
        if not ads:
            raise LookupError("VAST response had no ads")
        jobs = []
        for ad in ads:
            job = self._job(ad, brk.event_id)
            if job is None:
                log.warning("ad %s has no mezzanine or ready-to-splice media file, skipping", ad.ad_id)
                vast.fire(ad.errors, error_code=403)
            else:
                jobs.append((ad, job))
        media = []
        for ad, job in jobs:
            try:
                media.append((ad, job.result()))
            except Exception as exc:  # noqa: BLE001 - drop this ad, keep the rest of the pod
                log.warning("ad %s: creative not available: %s", ad.ad_id, exc)
                vast.fire(ad.errors, error_code=405)
        if not media:
            raise LookupError("no playable ads in VAST response")
        return build_pod(media, content_streams)

    def _job(self, ad: vast.Ad, event_id: int) -> Future | None:
        if ad.mezzanine and self.conform_cmd:
            source, work = ad.mezzanine[0], self._conform
        else:
            source, work = ad.pick_media(), self._download
            if source is None:
                return None
        name = _creative_name(source.url)
        with self._lock:
            job = self._jobs.get(source.url)
            if job is not None and not (job.done() and job.exception()):
                if job.done():
                    log.info("break %d: creative %s cache hit", event_id, name)
                else:
                    log.info("break %d: creative %s already being prepared, waiting for it", event_id, name)
                return job
            job = self._pool.submit(work, source, name, event_id)
            self._jobs[source.url] = job
            return job

    def _conform(self, mez: vast.MediaFile, name: str, event_id: int) -> bytes:
        started = time.monotonic()
        data = vast.http_get(mez.url, self.timeout * 10)
        fetched = time.monotonic()
        log.info("break %d: mezzanine %s fetched, %.1f MB %s %dx%d, in %.2fs", event_id, name, len(data) / 1e6,
                 mez.mime_type or "?", mez.width, mez.height, fetched - started)
        with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
            f.write(data)
            f.flush()
            proc = subprocess.run([self.conform_cmd, f.name], capture_output=True, timeout=300)
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"conform failed ({proc.returncode}): {proc.stderr.decode(errors='replace')[-300:]}")
        log.info("break %d: creative %s conformed to the stream profile in %.2fs (%.1f MB TS), cached",
                 event_id, name, time.monotonic() - fetched, len(proc.stdout) / 1e6)
        return proc.stdout

    def _download(self, mf: vast.MediaFile, name: str, event_id: int) -> bytes:
        if hls.is_hls(mf.mime_type, mf.url):
            return hls.load_ts(mf.url, lambda u: vast.http_get(u, self.timeout * 4))
        data = vast.http_get(mf.url, self.timeout * 4)
        log.info("break %d: downloaded %s (%d bytes)", event_id, mf.url, len(data))
        return data


def _creative_name(url: str) -> str:
    path = urllib.parse.urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or url


# --------------------------------------------------------------------------
# Splice timeline
# --------------------------------------------------------------------------

@dataclass
class Splice:
    """Splice points of one break on the output (content) timeline, 90 kHz."""
    event_id: int
    out: int
    in_: int | None             # None while unknown: no duration yet, or waiting for a content IDR
    pod: Pod
    in_confirmed: bool = False  # the content IDR at ``in_`` has arrived
    waiting_idr: bool = False   # unconditioned content: the break is held to the next content IDR

    def ad_window(self, k: int) -> tuple[int, int]:
        """Output slot of pod entry ``k``, clipped to the break end."""
        pa = self.pod.ads[k]
        lo = (self.out + pa.pts_base) % PTS_MOD
        hi = (lo + pa.duration) % PTS_MOD
        if self.in_ is not None and pts_diff(hi, self.in_) > 0:
            hi = self.in_
        return lo, hi

    def window(self, segment: str, k: int) -> tuple[int | None, int | None]:
        if segment == "pre":
            return None, self.out
        if segment == "post":
            return self.in_, None
        return self.ad_window(k)

    def truncated(self, k: int) -> bool:
        pa = self.pod.ads[k]
        return self.in_ is not None and pts_diff((self.out + pa.pts_base + pa.duration) % PTS_MOD,
                                                 self.in_) > TOLERANCE


# --------------------------------------------------------------------------
# Per-PID output
# --------------------------------------------------------------------------

@dataclass
class Unit:
    """One PES on a lane, from the content or the pod."""
    segment: str                  # "pre" (content before the break), "pod", "post" (content after)
    hold: bool                    # evaluate when complete instead of streaming it through
    pts: int | None
    splice: Splice | None
    ad_index: int = -1
    packets: list[bytearray] = field(default_factory=list)
    sent: int = 0
    done: bool = False


class Lane:
    """Output of one elementary-stream PID across a break.

    Every PES from the content or the pod is placed on the output timeline and
    kept only inside its source's window: content before the splice-out, each
    ad's slot inside the break, content again from the splice-in. Video PES
    are single frames and are kept or dropped whole. Audio PES are split into
    ADTS frames; a frame is kept if its midpoint is inside the window and it
    does not overlap the last frame sent, and a PES that is only partly kept
    is re-packetized. Pod audio is first shifted onto the content's audio
    frame grid so the frames on either side of a splice meet exactly. PES far
    from any splice point stream straight through.

    Output follows segment order (pre, pod, post): the pod waits until the
    content has passed the splice-out on this PID, the content waits until
    the pod has passed the splice-in and the content itself has (so a content
    PES straddling the splice-in, which can arrive after the video IDR, is
    still trimmed).
    """

    def __init__(self, pid: int, stream_type: int, emit: Callable[[bytearray], None]) -> None:
        self.pid = pid
        self.audio = stream_type == STREAM_TYPE_AAC_ADTS
        self.tol = 0 if self.audio else TOLERANCE
        self.emit = emit
        self.splice: Splice | None = None
        self.open: dict[str, Unit | None] = {"content": None, "pod": None}
        self.queues: dict[str, deque[Unit]] = {"pre": deque(), "pod": deque(), "post": deque()}
        self.live = "pre"
        self.content_closed = self.content_past_in = self.pod_past_in = self.pod_ended = False
        self.half = 0.0                   # half an audio frame, 90 kHz, learned from the stream
        self.grid: int | None = None      # start of a content audio frame (content frames are contiguous)
        self.last_end: int | None = None  # end of the last audio frame sent
        self.align: dict[int, int] = {}   # pod entry -> audio shift onto the content grid, 90 kHz
        self.splits = 0

    def arm(self, splice: Splice) -> None:
        self.splice = splice
        self.content_closed = self.content_past_in = self.pod_past_in = self.pod_ended = False
        self.align = {}

    def packet(self, source: str, pkt: bytearray, ad_index: int = -1) -> None:
        if is_pusi(pkt):
            self._finish(source)
            pts, _ = get_pts_dts(pkt)
            if self.audio and pts is not None:
                if source == "content":
                    self.grid = pts
                elif self.grid is not None and self.half:
                    pts = self._align(pkt, pts, ad_index)
            placed = self._place(source, pts, ad_index)
            if placed is not None:
                segment, hold = placed
                unit = Unit(segment, hold, pts, self.splice, ad_index, [pkt])
                self.open[source] = unit
                self.queues[segment].append(unit)
        elif self.open[source] is not None:
            self.open[source].packets.append(pkt)
        self._drain()

    def close_content(self) -> None:
        """The content has nothing more for the pre-break segment on this PID."""
        if self.splice is not None and not self.content_closed:
            self._finish("content")
            self.content_closed = True
            self._drain()

    def end_pod(self) -> None:
        self._finish("pod")
        self.pod_ended = True
        self._drain()

    def end_content_wait(self) -> None:
        """Stop waiting for content past the splice-in on this PID."""
        self.content_past_in = True
        self._drain()

    def _align(self, pkt: bytearray, pts: int, k: int) -> int:
        """Shift a pod audio PES onto the content audio frame grid (within half a frame)."""
        frame = 2 * self.half
        delta = round((pts_diff(self.grid, pts) % frame + self.half) % frame - self.half)
        if delta:
            shift_pts_dts(pkt, delta)
        self.align.setdefault(k, delta)
        return (pts + delta) % PTS_MOD

    def poke(self) -> None:
        self._drain()

    # -- placement ---------------------------------------------------------

    def _rel(self, pts: int, ref: int) -> float:
        """Position of the PES's first frame (audio: its midpoint) relative to ``ref``."""
        return pts_diff(pts, ref) + self.half

    def _place(self, source: str, pts: int | None, k: int) -> tuple[str, bool] | None:
        sp = self.splice
        span = HOLD_SPAN if self.audio else 0
        if source == "content":
            if sp is None:
                return "pre", False
            if pts is None:
                return None if self.content_closed else ("pre", False)
            if not self.content_closed and self._rel(pts, sp.out) < -self.tol:
                return "pre", self.audio and self._rel(pts, sp.out) > -span
            self.content_closed = True
            if sp.in_ is None:
                # Return point unknown. While waiting for a content IDR, keep what
                # arrives: it is trimmed once that IDR fixes the splice-in.
                return ("post", True) if sp.waiting_idr else None
            if self._rel(pts, sp.in_) < -self.tol - span:
                return None  # inside the break
            if self._rel(pts, sp.in_) - self.half >= -self.tol:
                self.content_past_in = True  # wholly after the splice-in: no straddler can follow
            return "post", True
        if sp is None or pts is None:
            return None
        if sp.in_ is not None and self._rel(pts, sp.in_) >= -self.tol:
            self.pod_past_in = True
            return None
        lo, hi = sp.ad_window(k)
        if self._rel(pts, lo) < -self.tol - span or self._rel(pts, hi) >= -self.tol:
            return None
        return "pod", self.audio and (self._rel(pts, lo) < span or self._rel(pts, hi) > -span)

    # -- release -----------------------------------------------------------

    def _finish(self, source: str) -> None:
        unit = self.open[source]
        if unit is not None:
            unit.done = True
            self.open[source] = None

    def _drain(self) -> None:
        while True:
            q = self.queues[self.live]
            while q:
                unit = q[0]
                if not unit.hold:
                    for p in unit.packets[unit.sent:]:
                        self.emit(p)
                    unit.sent = len(unit.packets)
                    if not unit.done:
                        return
                    if self.audio:
                        self._note_end(unit)
                elif not unit.done:
                    return
                else:
                    for p in self._evaluate(unit):
                        self.emit(p)
                q.popleft()
            if not self._advance():
                return

    def _advance(self) -> bool:
        sp = self.splice
        if sp is None:
            return False
        if self.live == "pre" and self.content_closed:
            self.live = "pod"
            return True
        if (self.live == "pod" and sp.in_confirmed and self.content_past_in
                and (self.pod_past_in or self.pod_ended)):
            # Break over on this PID: the content queued since the splice-in goes next.
            self.queues["pre"], self.queues["post"] = self.queues["post"], deque()
            self.live = "pre"
            self.splice = None
            self.open["pod"] = None
            return True
        return False

    # -- audio frames ------------------------------------------------------

    def _frames(self, unit: Unit):
        pes = parse_pes(b"".join(bytes(payload(p)) for p in unit.packets))
        frames = adts_frames(pes.payload) if pes is not None and pes.pts is not None else None
        if frames and not self.half:
            self.half = frames[0][2] / 2
        return pes, frames

    def _note_end(self, unit: Unit) -> None:
        if unit.pts is None:
            return
        _, frames = self._frames(unit)
        if frames:
            self.last_end = round(unit.pts + sum(f[2] for f in frames)) % PTS_MOD

    def _inside(self, pts: int, offset: float, lo: int | None, hi: int | None) -> bool:
        return ((lo is None or pts_diff(pts, lo) + offset >= -self.tol)
                and (hi is None or pts_diff(pts, hi) + offset < -self.tol))

    def _evaluate(self, unit: Unit) -> list[bytearray]:
        lo, hi = unit.splice.window(unit.segment, unit.ad_index) if unit.splice else (None, None)
        if not self.audio or unit.pts is None:
            return unit.packets if unit.pts is None or self._inside(unit.pts, 0, lo, hi) else []
        pes, frames = self._frames(unit)
        if not frames:  # not ADTS: keep or drop the PES as a whole
            return unit.packets if self._inside(unit.pts, 0, lo, hi) else []
        kept, start = [], 0.0
        for off, length, ticks in frames:
            fresh = self.last_end is None or pts_diff(unit.pts, self.last_end) + start >= -1
            if fresh and self._inside(unit.pts, start + ticks / 2, lo, hi):
                kept.append((start, off, length, ticks))
            start += ticks
        if not kept:
            return []
        first, last = kept[0], kept[-1]
        self.last_end = round(unit.pts + last[0] + last[3]) % PTS_MOD
        if len(kept) == len(frames):
            return unit.packets
        self.splits += 1
        data = b"".join(pes.payload[off:off + length] for _, off, length, _ in kept)
        return packetize_pes(self.pid, build_pes(pes.stream_id, round(unit.pts + first[0]) % PTS_MOD, data))


# --------------------------------------------------------------------------
# Stitcher
# --------------------------------------------------------------------------

@dataclass
class Break:
    event_id: int
    out_pts: int
    duration: int | None
    status: str = "loading"   # loading | ready | failed
    pod: Pod | None = None
    error: str = ""


@dataclass
class ActiveBreak:
    brk: Break
    splice: Splice
    trackers: list[vast.AdTracker | None]
    switch_pcr: int | None = None  # content PCR at the splice-out frame; None while armed
    idx: int = 0
    current_ad: int = -1
    pod_ended: bool = False
    planned_in: int | None = None  # splice-in frame that had no IDR


def _secs(ticks: int | None) -> str:
    return "?" if ticks is None else f"{ticks / PTS_HZ:.3f}s"


class Stitcher:
    def __init__(self, write: Callable[[bytearray], None],
                 load_pod: Callable[[Break, dict[int, int]], Pod],
                 spawn: Callable[[Callable[[], None]], None] | None = None,
                 tracker_factory=vast.AdTracker, slate: bytes | None = None) -> None:
        self.write = write
        self.load_pod = load_pod
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self.tracker_factory = tracker_factory
        self.slate = slate

        self.pmt_pid: int | None = None
        self.streams: dict[int, int] = {}   # ES pid -> stream_type (SCTE-35 excluded)
        self.scte35_pids: set[int] = set()
        self.video_pid: int | None = None
        self.video_type = 0
        self._asm: dict[int, SectionAssembler] = {}
        self.lanes: dict[int, Lane] = {}
        self.cc: dict[int, int] = {}

        self.latest_pcr: int | None = None
        self.latest_pts: int | None = None
        self.frame_ticks = 3000             # content video frame duration, measured
        self.pending: dict[int, Break] = {}
        self.active: ActiveBreak | None = None
        self.finished: set[int] = set()

    # -- input -------------------------------------------------------------

    def feed(self, pkt: bytearray) -> None:
        pid = get_pid(pkt)
        if pid == PID_PAT or pid == self.pmt_pid or pid in self.scte35_pids:
            for sec in self._asm.setdefault(pid, SectionAssembler()).push(pkt):
                self._on_section(pid, sec)
            self._emit(pkt, rewrite_cc=False)
            return
        lane = self.lanes.get(pid)
        if lane is None:
            self._emit(pkt, rewrite_cc=False)
            return
        pcr = get_pcr(pkt)
        if pcr is not None:
            self.latest_pcr = pcr
        if pid == self.video_pid and is_pusi(pkt):
            pts, _ = get_pts_dts(pkt)
            if pts is not None:
                self._on_content_frame(pts, is_keyframe(pkt, self.video_type))
        lane.packet("content", pkt)
        if self.active is not None and self.active.switch_pcr is not None:
            self._pump()

    def _on_section(self, pid: int, sec: bytes) -> None:
        if pid in self.scte35_pids and sec[0] == scte35.TABLE_ID:
            try:
                info = scte35.parse(sec)
            except ValueError as exc:
                log.warning("bad SCTE-35 section on PID 0x%X: %s", pid, exc)
                return
            self._on_splice_info(info)
            return
        if not section_crc_ok(sec):
            return
        if pid == PID_PAT and sec[0] == 0x00:
            programs = parse_pat(sec)
            if programs:
                pmt_pid = next(iter(programs.values()))
                if pmt_pid != self.pmt_pid:
                    log.info("PAT: program %d -> PMT PID 0x%X", next(iter(programs)), pmt_pid)
                    self.pmt_pid = pmt_pid
        elif pid == self.pmt_pid and sec[0] == 0x02:
            pmt = parse_pmt(sec)
            streams = {s.pid: s.stream_type for s in pmt.streams if s.stream_type != STREAM_TYPE_SCTE35}
            scte = {s.pid for s in pmt.streams if s.stream_type == STREAM_TYPE_SCTE35}
            if streams != self.streams or scte != self.scte35_pids:
                self.streams, self.scte35_pids = streams, scte
                video = next(((p, st) for p, st in streams.items() if st in VIDEO_STREAM_TYPES), None)
                self.video_pid, self.video_type = video if video else (None, 0)
                for p, st in streams.items():
                    self.lanes.setdefault(p, Lane(p, st, self._emit))
                log.info("PMT: streams %s, SCTE-35 PIDs %s",
                         ", ".join(f"0x{p:X}(type 0x{st:02X})" for p, st in streams.items()),
                         ", ".join(f"0x{p:X}" for p in scte) or "none")

    # -- cues --------------------------------------------------------------

    def _on_splice_info(self, info: scte35.SpliceInfo) -> None:
        cue = info.cue()
        if cue is None:
            return
        if cue.kind == "out":
            self._schedule_break(cue)
        elif cue.kind == "in":
            self._cue_in(cue)
        elif cue.kind == "cancel" and self.pending.pop(cue.event_id, None):
            log.info("SCTE-35 cancel: break %d dropped", cue.event_id)

    def _schedule_break(self, cue: scte35.Cue) -> None:
        eid = cue.event_id
        if eid in self.pending or eid in self.finished or (self.active and self.active.brk.event_id == eid):
            return  # cues are repeated ahead of the splice; first one wins
        out_pts = cue.pts if cue.pts is not None else self.latest_pts
        if out_pts is None:
            log.warning("cue %d: no content PTS yet, ignoring", eid)
            return
        lead = pts_diff(out_pts, self.latest_pts) if self.latest_pts is not None else 0
        if lead < -MAX_LATE_CUE:
            log.warning("cue %d: splice time already %.2fs in the past, ignoring", eid, -lead / PTS_HZ)
            self.finished.add(eid)
            return
        brk = Break(eid, out_pts, cue.duration)
        self.pending[eid] = brk
        log.info("SCTE-35 %s: break %d splice-out pts=%d (in %.2fs) duration=%s",
                 cue.source, eid, out_pts, lead / PTS_HZ, _secs(cue.duration))
        streams = dict(self.streams)

        def work() -> None:
            try:
                pod = self.load_pod(brk, streams)
                brk.pod, brk.status = pod, "ready"
                log.info("break %d: pod ready, %d ad(s), %s: %s", eid, len(pod.ads),
                         _secs(pod.duration), ", ".join(a.label for a in pod.ads))
            except Exception as exc:  # noqa: BLE001 - any failure falls back to content
                brk.status, brk.error = "failed", str(exc)
                log.warning("break %d: ad decision failed: %s", eid, exc)

        self.spawn(work)

    def _cue_in(self, cue: scte35.Cue) -> None:
        a = self.active
        if a is not None:
            sp = a.splice
            if sp.in_confirmed:
                return
            at = self._snap(cue.pts) if cue.pts is not None else self._snap((self.latest_pts or sp.out)
                                                                            + self.frame_ticks)
            if sp.in_ is None or pts_diff(at, sp.in_) < 0:
                sp.in_ = at
                sp.waiting_idr = False
                log.info("SCTE-35 %s: break %d returns to network at pts=%d (%s into the break)",
                         cue.source, a.brk.event_id, at, _secs(pts_diff(at, sp.out)))
        elif self.pending.pop(cue.event_id, None):
            log.info("SCTE-35 %s: break %d ended before it started", cue.source, cue.event_id)

    # -- splice points -----------------------------------------------------

    def _snap(self, pts: int) -> int:
        """The content frame presented at ``pts`` (the next one if it falls between frames)."""
        if self.latest_pts is None:
            return pts
        f = self.frame_ticks
        n = -((TOLERANCE - pts_diff(pts, self.latest_pts)) // f)
        return (self.latest_pts + n * f) % PTS_MOD

    def _on_content_frame(self, pts: int, idr: bool) -> None:
        if self.latest_pts is not None:
            step = pts_diff(pts, self.latest_pts)
            if 0 < step <= PTS_HZ // 10:
                self.frame_ticks = step
        self.latest_pts = pts
        self._arm_ready(pts)
        a = self.active
        if a is None:
            return
        sp = a.splice
        if a.switch_pcr is None:
            if pts_diff(pts, sp.out) >= -TOLERANCE:
                self._splice_out(a, pts)
            return
        if sp.in_confirmed:
            return
        if sp.waiting_idr:
            if idr:
                late = pts_diff(pts, a.planned_in) if a.planned_in is not None else 0
                sp.in_, sp.in_confirmed, sp.waiting_idr = pts, True, False
                log.warning("<<< SPLICE IN  break %d at pts=%d, %s late (unconditioned content)",
                            a.brk.event_id, pts, _secs(late))
        elif sp.in_ is not None and pts_diff(pts, sp.in_) >= -TOLERANCE:
            if idr:
                if pts != sp.in_:
                    log.warning("break %d: splice-in frame moved by %+d ticks", a.brk.event_id,
                                pts_diff(pts, sp.in_))
                sp.in_, sp.in_confirmed = pts, True
                log.info("<<< SPLICE IN  break %d at pts=%d after %s (%d frames), content IDR",
                         a.brk.event_id, pts, _secs(pts_diff(pts, sp.out)),
                         round(pts_diff(pts, sp.out) / self.frame_ticks))
            else:
                log.error("break %d: content has no IDR on the splice-in frame pts=%d (encoder not "
                          "conditioned for this break); holding the break to the next content IDR",
                          a.brk.event_id, pts)
                # Keep the break open (and the slate flowing) until that IDR.
                sp.waiting_idr, a.planned_in, sp.in_ = True, pts, None
                a.pod_ended = False
                for lane in self.lanes.values():
                    lane.pod_past_in = lane.pod_ended = lane.content_past_in = False

    def _arm_ready(self, pts: int) -> None:
        for eid, brk in list(self.pending.items()):
            lead = pts_diff(brk.out_pts, pts)
            if lead <= TOLERANCE:
                # The splice-out frame is here and this break was never armed.
                del self.pending[eid]
                self.finished.add(eid)
                if brk.duration is not None and -lead >= brk.duration:
                    log.warning("break %d: missed entirely", eid)
                elif brk.status != "ready":
                    log.warning("break %d: ads not ready at splice point (%s%s), staying on content",
                                eid, brk.status, f": {brk.error}" if brk.error else "")
                else:
                    log.warning("break %d: overlaps break %d, staying on content",
                                eid, self.active.brk.event_id if self.active else -1)
            elif brk.status == "ready" and self.active is None:
                del self.pending[eid]
                self._arm(brk)

    def _arm(self, brk: Break) -> None:
        f = self.frame_ticks
        out = self._snap(brk.out_pts)
        in_ = (out + round(brk.duration / f) * f) % PTS_MOD if brk.duration is not None else None
        sp = Splice(brk.event_id, out, in_, brk.pod)
        trackers = [self.tracker_factory(pa.ad, pa.duration / PTS_HZ) if pa.ad else None for pa in brk.pod.ads]
        self.active = ActiveBreak(brk, sp, trackers)
        for lane in self.lanes.values():
            lane.arm(sp)
        frames = "?" if in_ is None else str(round(pts_diff(in_, out) / f))
        log.info("break %d armed: splice-out pts=%d%s, splice-in pts=%s, %s frames; pod %s (%d frames)",
                 brk.event_id, out, "" if out == brk.out_pts else f" (cue {brk.out_pts} snapped to frame)",
                 in_, frames, _secs(brk.pod.duration), round(brk.pod.duration / f))

    def _splice_out(self, a: ActiveBreak, pts: int) -> None:
        sp = a.splice
        if pts != sp.out:
            log.warning("break %d: content frame grid moved, splice-out at pts=%d (%+d ticks)",
                        a.brk.event_id, pts, pts_diff(pts, sp.out))
            if sp.in_ is not None:
                sp.in_ = (sp.in_ + pts_diff(pts, sp.out)) % PTS_MOD
            sp.out = pts
        a.switch_pcr = self.latest_pcr or 0
        log.info(">>> SPLICE OUT break %d at pts=%d (%+d ticks vs cue), break %s",
                 a.brk.event_id, pts, pts_diff(pts, a.brk.out_pts),
                 _secs(None if sp.in_ is None else pts_diff(sp.in_, sp.out)))

    # -- ad playout --------------------------------------------------------

    def _pump(self) -> None:
        a = self.active
        sp = a.splice
        if self.latest_pcr is None:
            return
        elapsed = pcr_diff(self.latest_pcr, a.switch_pcr)
        self._fill_with_slate(a, elapsed)
        entries = sp.pod.entries
        while not a.pod_ended and a.idx < len(entries) and entries[a.idx].rel27 <= elapsed:
            self._emit_ad(a, entries[a.idx])
            a.idx += 1
        end = None if sp.in_ is None else pts_diff(sp.in_, sp.out) * 300
        if elapsed > STRAGGLER:
            for lane in self.lanes.values():
                lane.close_content()
        if not a.pod_ended:
            covered = end is not None and sp.pod.duration * 300 >= end
            exhausted = a.idx >= len(entries) and (covered or self.slate is None)
            if exhausted and not covered:
                log.warning("break %d: pod is %s short of the break and no slate is configured",
                            a.brk.event_id, _secs(None if end is None else end // 300 - sp.pod.duration))
            if exhausted or (sp.in_confirmed and elapsed > end + STRAGGLER):
                a.pod_ended = True
                for lane in self.lanes.values():
                    lane.end_pod()
        if sp.in_confirmed and elapsed > end + 2 * STRAGGLER:
            for lane in self.lanes.values():
                lane.end_content_wait()
        if sp.in_confirmed:
            for lane in self.lanes.values():
                lane.poke()
            if all(lane.splice is not sp for lane in self.lanes.values()):
                self._finish_break(a)

    def _fill_with_slate(self, a: ActiveBreak, elapsed: int) -> None:
        sp, pod = a.splice, a.splice.pod
        while self.slate is not None:
            if sp.in_ is not None:
                if pts_diff((sp.out + pod.duration) % PTS_MOD, sp.in_) >= 0:
                    return
            elif pod.entries and pod.entries[-1].rel27 - elapsed > SLATE_LEAD:
                return
            pa = append_media(pod, None, self.slate, self.streams, slate=True)
            a.trackers.append(None)
            log.debug("break %d: slate %s appended at +%s", a.brk.event_id, _secs(pa.duration), _secs(pa.pts_base))

    def _finish_break(self, a: ActiveBreak) -> None:
        sp = a.splice
        self._track_complete(a, a.current_ad)
        length = pts_diff(sp.in_, sp.out)
        frames = round(length / self.frame_ticks)
        splits = sum(lane.splits for lane in self.lanes.values())
        slate = max(0, length - sum(pa.duration for pa in sp.pod.ads if not pa.slate)) if self.slate else 0
        log.info("break %d done: pts %d..%d, %d frames (%s), slate %s, %d audio PES split at splice points",
                 a.brk.event_id, sp.out, sp.in_, frames, _secs(length), _secs(slate), splits)
        for lane in self.lanes.values():
            if lane.audio and lane.align:
                log.info("break %d: PID 0x%X ad audio moved onto the content audio grid by %s ms",
                         a.brk.event_id, lane.pid,
                         ", ".join(f"{d / 90:+.1f}" for _, d in sorted(lane.align.items())))
            lane.splits = 0
        self.finished.add(a.brk.event_id)
        self.active = None

    def _emit_ad(self, a: ActiveBreak, entry: PodEntry) -> None:
        sp = a.splice
        pa = sp.pod.ads[entry.ad_index]
        if entry.ad_index != a.current_ad:
            self._track_complete(a, a.current_ad)
            a.current_ad = entry.ad_index
            lo, hi = sp.ad_window(entry.ad_index)
            slot = max(0, pts_diff(hi, lo))
            if slot:
                note = f", cut to {_secs(slot)} at the break end" if slot < pa.duration - TOLERANCE else ""
                log.info("    %s %d/%d: %s at pts=%d (%s%s)", "slate" if pa.slate else "ad", entry.ad_index + 1,
                         len(sp.pod.ads), pa.label, lo, _secs(pa.duration), note)
                if a.trackers[entry.ad_index]:
                    a.trackers[entry.ad_index].start()
        tracker = a.trackers[entry.ad_index]
        if tracker:
            tracker.progress((entry.rel27 - pa.pts_base * 300) / PCR_HZ)

        pkt = bytearray(entry.packet)
        offset = sp.out + pa.pts_base - pa.first_pts
        shift_pts_dts(pkt, offset)
        pcr = get_pcr(pkt)
        if pcr is not None:
            set_pcr(pkt, pcr + offset * 300)
        lane = self.lanes.get(entry.pid)
        if lane is not None:
            lane.packet("pod", pkt, entry.ad_index)

    def _track_complete(self, a: ActiveBreak, index: int) -> None:
        if index >= 0 and a.trackers[index] and not a.splice.truncated(index):
            a.trackers[index].complete()
            a.trackers[index] = None

    # -- output ------------------------------------------------------------

    def _emit(self, pkt: bytearray, rewrite_cc: bool = True) -> None:
        if rewrite_cc and has_payload(pkt):
            pid = get_pid(pkt)
            prev = self.cc.get(pid)
            cc = get_cc(pkt) if prev is None else (prev + 1) & 0x0F
            set_cc(pkt, cc)
            self.cc[pid] = cc
        self.write(pkt)


# --------------------------------------------------------------------------
# Service entry point
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="SCTE-35 / VAST MPEG-TS ad stitcher")
    p.add_argument("--input-udp", default="0.0.0.0:5001", help="where srt-live-transmit delivers the content")
    p.add_argument("--output-udp", default="127.0.0.1:5002",
                   help="where to send the stitched stream (comma-separated for several)")
    p.add_argument("--record", help="also write the stitched stream to this file")
    p.add_argument("--ads-url", required=True,
                   help="VAST tag template; {duration}, {event_id}, {cachebuster} are filled in")
    p.add_argument("--slate", help="MPEG-TS slate (encoded like the content) used to fill short pods")
    p.add_argument("--conform", help="command that transcodes a mezzanine file to the stream profile "
                                     "(MPEG-TS on stdout); enables VAST <Mezzanine> handling")
    p.add_argument("--tap-udp", default="",
                   help="also forward the incoming (pre-stitch) stream here, e.g. viewer:5200 (comma-separated)")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    setup_logging(args.log_level)

    slate = None
    if args.slate:
        with open(args.slate, "rb") as f:
            slate = f.read()
        log.info("slate %s (%d bytes)", args.slate, len(slate))
    sock = udp_receiver(args.input_udp)
    outs = [UdpPacketWriter(addr) for addr in args.output_udp.split(",") if addr]
    taps = [UdpForwarder(addr) for addr in args.tap_udp.split(",") if addr]
    record = open(args.record, "wb") if args.record else None

    def write(pkt: bytearray) -> None:
        for out in outs:
            out.write(pkt)
        if record:
            record.write(pkt)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    stitcher = Stitcher(write, VastPodLoader(args.ads_url, conform_cmd=args.conform), slate=slate)
    reader = PacketReader()
    log.info("listening on udp %s, output udp %s, record %s, tap %s", args.input_udp, args.output_udp, args.record,
             args.tap_udp or "off")
    try:
        last_input = time.monotonic()
        while True:
            data = sock.recv(65536)
            received = time.monotonic()
            if received - last_input > STALL_WARN:
                log.warning("input stalled: nothing received for %.0f ms (upstream: source or SRT)",
                            (received - last_input) * 1000)
            for tap in taps:
                tap.send(data)
            for pkt in reader.feed(data):
                stitcher.feed(pkt)
            last_input = time.monotonic()
            if last_input - received > STALL_WARN:
                log.warning("slow processing: one input chunk took %.0f ms", (last_input - received) * 1000)
            for out in outs:
                out.flush()
    except KeyboardInterrupt:
        pass
    finally:
        if record:
            record.close()


if __name__ == "__main__":
    main()
