"""SCTE-35 cue injector for the test source.

Sits between ffmpeg (which produces the content MPEG-TS) and the SRT sender:
adds an SCTE-35 PID to the PMT and inserts splice cues for a fixed break
schedule. Splice times and durations are whole frames of the content
timeline. The encoder is conditioned for the same schedule (``source.sh``
forces an IDR on every splice-out and splice-in frame), which is what a
broadcast encoder does on an SCTE-104 trigger.
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import scte35, setup_logging
from .transport import UdpPacketWriter
from .ts import (
    PID_PAT, PTS_HZ, STREAM_TYPE_SCTE35, EsInfo, PacketReader, SectionAssembler,
    build_pmt, get_pid, get_pts_dts, is_keyframe, is_pusi, packetize_section,
    parse_pat, parse_pmt, pts_diff, section_crc_ok, video_stream,
)

log = logging.getLogger("source")

REGISTRATION_CUEI = bytes([0x05, 0x04]) + b"CUEI"


class CueInjector:
    def __init__(self, write, scte35_pid: int = 0x1F4, first_break: float = 20.0,
                 interval: float = 60.0, duration: float = 30.0,
                 prerolls: tuple[float, ...] = (15.0, 10.0, 5.0, 2.0), mode: str = "splice_insert",
                 first_event_id: int = 1001) -> None:
        self.write = write
        self.scte35_pid = scte35_pid
        self.first_break = first_break
        self.interval = interval
        self.duration = round(duration * PTS_HZ)
        self.prerolls = sorted(prerolls, reverse=True)
        self.mode = mode
        self.first_event_id = first_event_id

        self.pmt_pid: int | None = None
        self.video_pid: int | None = None
        self.video_type = 0
        self.base_pts: int | None = None
        self.latest_pts: int | None = None
        self.frame_ticks: int | None = None
        self._asm: dict[int, SectionAssembler] = {}
        self._pmt_cc = 0
        self._scte_cc = 0
        self._break = 0
        self._sent: set[tuple[int, str, float]] = set()

    def feed(self, pkt: bytearray) -> None:
        pid = get_pid(pkt)
        if pid == PID_PAT:
            for sec in self._sections(pid, pkt):
                if sec[0] == 0x00 and (programs := parse_pat(sec)):
                    self.pmt_pid = next(iter(programs.values()))
            self.write(pkt)
            return
        if pid == self.pmt_pid:
            # Replace each PMT with one that also announces the SCTE-35 PID.
            for sec in self._sections(pid, pkt):
                if sec[0] == 0x02:
                    packets, self._pmt_cc = packetize_section(pid, self._augment(sec), self._pmt_cc)
                    for p in packets:
                        self.write(p)
            return
        self.write(pkt)
        if pid == self.video_pid and is_pusi(pkt):
            pts, _ = get_pts_dts(pkt)
            if pts is None:
                return
            if self.latest_pts is not None and 0 < pts_diff(pts, self.latest_pts) <= PTS_HZ // 10:
                self.frame_ticks = pts_diff(pts, self.latest_pts)
            self.latest_pts = pts
            if self.base_pts is None and is_keyframe(pkt, self.video_type):
                self.base_pts = pts
                log.info("content timeline starts at pts=%d; first break at +%.0fs, every %.0fs",
                         pts, self.first_break, self.interval)
            if self.base_pts is not None and self.frame_ticks:
                self._schedule()

    def _sections(self, pid: int, pkt) -> list[bytes]:
        return [s for s in self._asm.setdefault(pid, SectionAssembler()).push(pkt) if section_crc_ok(s)]

    def _augment(self, sec: bytes) -> bytes:
        pmt = parse_pmt(sec)
        video = video_stream(pmt)
        if video is not None:
            self.video_pid, self.video_type = video.pid, video.stream_type
        if not any(s.stream_type == STREAM_TYPE_SCTE35 for s in pmt.streams):
            pmt.streams.append(EsInfo(STREAM_TYPE_SCTE35, self.scte35_pid))
        if b"CUEI" not in pmt.program_info:
            pmt.program_info += REGISTRATION_CUEI
        return build_pmt(pmt)

    # -- schedule ----------------------------------------------------------

    def _frames(self, seconds: float) -> int:
        """``seconds`` rounded to whole content frames, in 90 kHz ticks."""
        return round(seconds * PTS_HZ / self.frame_ticks) * self.frame_ticks

    def _signals(self, n: int) -> list[tuple[str, int]]:
        out = self.base_pts + self._frames(self.first_break + n * self.interval)
        signals = [("out", out)]
        if self.mode == "time_signal":
            signals.append(("in", out + self._frames(self.duration / PTS_HZ)))
        return signals

    def _schedule(self) -> None:
        n = self._break
        signals = self._signals(n)
        for kind, at in signals:
            lead = pts_diff(at, self.latest_pts) / PTS_HZ
            due = [r for r in self.prerolls if lead <= r and (n, kind, r) not in self._sent]
            if due and lead >= 0:
                self._send(n, kind, at, lead)
                self._sent.update((n, kind, r) for r in due)
        if pts_diff(self.latest_pts, signals[-1][1]) > 0:
            self._break += 1
            self._sent = {k for k in self._sent if k[0] > n}

    def _send(self, n: int, kind: str, at: int, lead: float) -> None:
        eid = self.first_event_id + n
        duration = self._frames(self.duration / PTS_HZ)
        if self.mode == "time_signal":
            seg = scte35.SegmentationDescriptor(
                event_id=eid,
                type_id=0x34 if kind == "out" else 0x35,  # provider placement opportunity start/end
                duration=duration if kind == "out" else None,
                upid_type=0x09, upid=f"SRTVAST-{eid}".encode(),
                segment_num=1, segments_expected=1,
            )
            section = scte35.build_time_signal(at, [seg])
        else:
            section = scte35.build_splice_insert(scte35.SpliceInsert(
                event_id=eid, out_of_network=True, pts_time=at, duration=duration,
                auto_return=True, unique_program_id=1, avail_num=1, avails_expected=1,
            ))
        packets, self._scte_cc = packetize_section(self.scte35_pid, section, self._scte_cc)
        for p in packets:
            self.write(p)
        log.info("cue %s %-3s event=%d splice pts=%d (frame %d) in %.1fs duration=%.3fs (%d frames)",
                 self.mode, kind, eid, at, pts_diff(at, self.base_pts) // self.frame_ticks, lead,
                 duration / PTS_HZ, duration // self.frame_ticks,
                 extra={"detail": {"kind": "scte35", "direction": "sent", "pid": f"0x{self.scte35_pid:X}",
                                   "section": scte35.describe(section)}})


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Insert SCTE-35 cues into an MPEG-TS read from stdin")
    p.add_argument("--udp", help="send to host:port (default: stdout)")
    p.add_argument("--scte35-pid", type=lambda v: int(v, 0), default=0x1F4)
    p.add_argument("--first-break", type=float, default=20.0, help="seconds into the stream")
    p.add_argument("--interval", type=float, default=60.0, help="seconds between breaks")
    p.add_argument("--duration", type=float, default=30.0, help="break duration, seconds")
    p.add_argument("--preroll", default="15,10,5,2", help="send the cue this many seconds ahead (repeats)")
    p.add_argument("--mode", choices=("splice_insert", "time_signal"), default="splice_insert")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    setup_logging(args.log_level)

    if args.udp:
        out = UdpPacketWriter(args.udp)
        write = out.write
    else:
        stdout = sys.stdout.buffer
        write = stdout.write
    injector = CueInjector(
        write, args.scte35_pid, args.first_break, args.interval, args.duration,
        tuple(float(v) for v in args.preroll.split(",") if v), args.mode,
    )
    reader = PacketReader()
    stdin = sys.stdin.buffer
    while chunk := stdin.read1(65536):
        for pkt in reader.feed(chunk):
            injector.feed(pkt)
        if args.udp:
            out.flush()


if __name__ == "__main__":
    main()
