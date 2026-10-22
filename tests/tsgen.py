"""Tiny synthetic MPEG-TS generator for tests.

Video: one single-packet PES per frame (30 fps), an IDR every ``gop`` frames
(or on the frames listed in ``idr``). Audio: AAC ADTS frames (48 kHz, 1920
ticks each), several per PES and muxed after the video of the same time,
like ffmpeg does, starting one frame before the video (encoder priming). Every payload byte is the stream's
``marker`` so tests can tell content, ad and slate apart in the output.
"""

from collections import defaultdict

from srtvast.ts import (
    EsInfo, Pmt, build_pat, build_pmt, encode_timestamp, packetize_section, set_pcr,
)

PMT_PID = 0x1000
VIDEO_PID = 0x100
AUDIO_PID = 0x101
SCTE35_PID = 0x1F4
FRAME = 3000        # 30 fps
AUDIO_FRAME = 1920  # 1024 samples at 48 kHz


def ts_packet(pid, payload, pusi=False, cc=0, rai=False, pcr=None):
    af = b""
    if rai or pcr is not None:
        af = bytes([(0x40 if rai else 0) | (0x10 if pcr is not None else 0)]) + (b"\x00" * 6 if pcr is not None else b"")
        af = bytes([len(af)]) + af
    payload = payload[:184 - len(af)]
    stuffing = 184 - len(af) - len(payload)
    if stuffing:
        if not af:
            af = b"\x00" if stuffing == 1 else bytes([stuffing - 1, 0x00]) + b"\xff" * (stuffing - 2)
        else:
            af = bytes([af[0] + stuffing]) + af[1:] + b"\xff" * stuffing
    header = bytes([0x47, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, (0x30 if af else 0x10) | cc])
    pkt = bytearray(header + af + payload)
    if pcr is not None:
        set_pcr(pkt, pcr)
    return pkt


def pes(stream_id, pts, marker, size=40, data=None):
    data = bytes([marker]) * size if data is None else data
    return b"\x00\x00\x01" + bytes([stream_id, 0, 0, 0x80, 0x80, 5]) + encode_timestamp(2, pts) + data


def adts_frame(marker, size=20):
    length = 7 + size
    header = bytes([0xFF, 0xF1, 0x4C, 0x80 | (length >> 11), (length >> 3) & 0xFF, ((length & 7) << 5) | 0x1F, 0xFC])
    return header + bytes([marker]) * size


def packetize(pid, data, cc_counter):
    """Split a PES over as many packets as it needs."""
    out = []
    for i in range(0, len(data), 184):
        out.append(cc_counter(pid, data[i:i + 184], i == 0))
    return out


class StreamBuilder:
    def __init__(self, marker, start_pts=126_000, gop=30, scte35=True, idr=(), audio_per_pes=8):
        self.marker = marker
        self.start_pts = start_pts
        self.gop = gop
        self.idr = set(idr)
        self.scte35 = scte35
        self.audio_per_pes = audio_per_pes
        self.audio_next = start_pts - AUDIO_FRAME  # priming frame before the first video frame
        self.cc = defaultdict(int)

    def _psi(self, pid, section):
        packets, self.cc[pid] = packetize_section(pid, section, self.cc[pid])
        return packets

    def _pkt(self, pid, payload, **kw):
        p = ts_packet(pid, payload, cc=self.cc[pid], **kw)
        self.cc[pid] = (self.cc[pid] + 1) & 0x0F
        return p

    def psi(self):
        streams = [EsInfo(0x1B, VIDEO_PID), EsInfo(0x0F, AUDIO_PID)]
        if self.scte35:
            streams.append(EsInfo(0x86, SCTE35_PID))
        return self._psi(0, build_pat({1: PMT_PID})) + self._psi(PMT_PID, build_pmt(Pmt(1, VIDEO_PID, streams)))

    def pts(self, frame):
        return self.start_pts + frame * FRAME

    def frames(self, start, count):
        out = []
        for f in range(start, start + count):
            if f % 10 == 0:
                out += self.psi()
            pts = self.pts(f)
            key = f in self.idr if self.idr else f % self.gop == 0
            out.append(self._pkt(VIDEO_PID, pes(0xE0, pts, self.marker), pusi=True,
                                 rai=key, pcr=(pts - 63_000) * 300))
            # Like ffmpeg, an audio PES is muxed once all its frames exist, i.e. after
            # the video of the same time; the last call's leftovers are flushed.
            end, last = self.pts(start + count), f == start + count - 1
            while True:
                n = min(self.audio_per_pes, -(-(end - self.audio_next) // AUDIO_FRAME))
                if n <= 0 or (self.audio_next + n * AUDIO_FRAME > pts + FRAME and not last):
                    break
                data = b"".join(adts_frame(self.marker) for _ in range(n))
                out += packetize(AUDIO_PID, pes(0xC0, self.audio_next, self.marker, data=data),
                                 lambda pid, chunk, pusi: self._pkt(pid, chunk, pusi=pusi))
                self.audio_next += n * AUDIO_FRAME
        return out

    def section(self, sec):
        return self._psi(SCTE35_PID, sec)


def to_bytes(packets):
    return b"".join(bytes(p) for p in packets)
