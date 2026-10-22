"""SCTE-35 splice_info_section parsing and building (ANSI/SCTE 35).

Supports splice_null, splice_insert and time_signal commands plus
segmentation_descriptor, which covers the cues ad insertion cares about.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ts import PTS_MOD, crc32_mpeg2, section_length

TABLE_ID = 0xFC
CUEI = 0x43554549

CMD_SPLICE_NULL = 0x00
CMD_SPLICE_INSERT = 0x05
CMD_TIME_SIGNAL = 0x06

DESC_AVAIL = 0x00
DESC_SEGMENTATION = 0x02

# segmentation_type_id values that open / close an ad opportunity.
SEG_OUT_TYPES = {0x22, 0x30, 0x32, 0x34, 0x36, 0x38, 0x3A}
SEG_IN_TYPES = {0x23, 0x31, 0x33, 0x35, 0x37, 0x39, 0x3B}
_SEG_SUB_SEGMENT_TYPES = {0x34, 0x36, 0x38, 0x3A}


class BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0  # in bits

    def read(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def read_bytes(self, n: int) -> bytes:
        start = self.pos >> 3
        self.pos += n * 8
        return bytes(self.data[start:start + n])

    @property
    def bytes_left(self) -> int:
        return len(self.data) - (self.pos >> 3)


class BitWriter:
    def __init__(self) -> None:
        self.value = 0
        self.bits = 0

    def write(self, value: int, n: int) -> "BitWriter":
        self.value = (self.value << n) | (int(value) & ((1 << n) - 1))
        self.bits += n
        return self

    def write_bytes(self, data: bytes) -> "BitWriter":
        for b in data:
            self.write(b, 8)
        return self

    def to_bytes(self) -> bytes:
        assert self.bits % 8 == 0, "unaligned bit string"
        return self.value.to_bytes(self.bits // 8, "big")


@dataclass
class SpliceInsert:
    event_id: int
    cancel: bool = False
    out_of_network: bool = True
    program_splice: bool = True
    splice_immediate: bool = False
    pts_time: int | None = None
    duration: int | None = None  # 90 kHz ticks
    auto_return: bool = True
    unique_program_id: int = 0
    avail_num: int = 0
    avails_expected: int = 0


@dataclass
class SegmentationDescriptor:
    event_id: int
    type_id: int = 0x34
    cancel: bool = False
    duration: int | None = None  # 90 kHz ticks
    upid_type: int = 0x00
    upid: bytes = b""
    segment_num: int = 0
    segments_expected: int = 0


@dataclass
class Cue:
    """A splice point normalised from either splice_insert or time_signal."""
    kind: str  # "out", "in" or "cancel"
    event_id: int
    pts: int | None  # pts_adjustment applied; None means "immediate"
    duration: int | None = None
    auto_return: bool = False
    source: str = ""


@dataclass
class SpliceInfo:
    command_type: int
    pts_adjustment: int = 0
    tier: int = 0xFFF
    splice_insert: SpliceInsert | None = None
    time_signal_pts: int | None = None
    segmentation: list[SegmentationDescriptor] = field(default_factory=list)
    descriptor_tags: list[int] = field(default_factory=list)

    def _adjust(self, pts: int | None) -> int | None:
        return None if pts is None else (pts + self.pts_adjustment) % PTS_MOD

    def cue(self) -> Cue | None:
        si = self.splice_insert
        if self.command_type == CMD_SPLICE_INSERT and si is not None:
            if si.cancel:
                return Cue("cancel", si.event_id, None, source="splice_insert")
            pts = None if si.splice_immediate else self._adjust(si.pts_time)
            return Cue("out" if si.out_of_network else "in", si.event_id, pts,
                       si.duration, si.auto_return, "splice_insert")
        if self.command_type == CMD_TIME_SIGNAL:
            pts = self._adjust(self.time_signal_pts)
            for seg in self.segmentation:
                source = f"time_signal/0x{seg.type_id:02X}"
                if seg.cancel:
                    return Cue("cancel", seg.event_id, None, source=source)
                if seg.type_id in SEG_OUT_TYPES:
                    return Cue("out", seg.event_id, pts, seg.duration, seg.duration is not None, source)
                if seg.type_id in SEG_IN_TYPES:
                    return Cue("in", seg.event_id, pts, source=source)
        return None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _read_splice_time(r: BitReader) -> int | None:
    if r.read(1):
        r.read(6)
        return r.read(33)
    r.read(7)
    return None


def _parse_splice_insert(r: BitReader) -> SpliceInsert:
    si = SpliceInsert(event_id=r.read(32))
    si.cancel = bool(r.read(1))
    r.read(7)
    if si.cancel:
        return si
    si.out_of_network = bool(r.read(1))
    si.program_splice = bool(r.read(1))
    duration_flag = r.read(1)
    si.splice_immediate = bool(r.read(1))
    r.read(4)  # event_id_compliance_flag + reserved
    if si.program_splice and not si.splice_immediate:
        si.pts_time = _read_splice_time(r)
    if not si.program_splice:
        # Component splice: use the first component's time as the splice time.
        for _ in range(r.read(8)):
            r.read(8)
            if not si.splice_immediate:
                t = _read_splice_time(r)
                if si.pts_time is None:
                    si.pts_time = t
    if duration_flag:
        si.auto_return = bool(r.read(1))
        r.read(6)
        si.duration = r.read(33)
    si.unique_program_id = r.read(16)
    si.avail_num = r.read(8)
    si.avails_expected = r.read(8)
    return si


def _parse_segmentation(body: bytes) -> SegmentationDescriptor | None:
    r = BitReader(body)
    if r.read(32) != CUEI:
        return None
    seg = SegmentationDescriptor(event_id=r.read(32))
    seg.cancel = bool(r.read(1))
    r.read(7)
    if seg.cancel:
        return seg
    program_flag = r.read(1)
    duration_flag = r.read(1)
    r.read(6)  # delivery_not_restricted_flag + restriction flags / reserved
    if not program_flag:
        for _ in range(r.read(8)):
            r.read(8 + 7 + 33)
    if duration_flag:
        seg.duration = r.read(40)
    seg.upid_type = r.read(8)
    seg.upid = r.read_bytes(r.read(8))
    seg.type_id = r.read(8)
    seg.segment_num = r.read(8)
    seg.segments_expected = r.read(8)
    return seg


def parse(section: bytes) -> SpliceInfo:
    """Parse a complete splice_info_section. Raises ValueError if invalid."""
    if len(section) < 3 or section[0] != TABLE_ID:
        raise ValueError("not a splice_info_section")
    total = 3 + section_length(section)
    if len(section) < total or total < 20:
        raise ValueError("truncated splice_info_section")
    if crc32_mpeg2(section[:total]) != 0:
        raise ValueError("SCTE-35 CRC mismatch")
    r = BitReader(section[3:total - 4])
    r.read(8)  # protocol_version
    if r.read(1):
        raise ValueError("encrypted SCTE-35 not supported")
    r.read(6)
    info = SpliceInfo(command_type=0, pts_adjustment=r.read(33))
    r.read(8)  # cw_index
    info.tier = r.read(12)
    command_length = r.read(12)
    info.command_type = r.read(8)
    command_start = r.pos
    if info.command_type == CMD_SPLICE_INSERT:
        info.splice_insert = _parse_splice_insert(r)
    elif info.command_type == CMD_TIME_SIGNAL:
        info.time_signal_pts = _read_splice_time(r)
    if command_length != 0xFFF:
        r.pos = command_start + command_length * 8
    loop_end = (r.pos >> 3) + 2 + r.read(16)
    data = r.data
    i = r.pos >> 3
    while i + 2 <= loop_end and i + 2 <= len(data):
        tag, length = data[i], data[i + 1]
        info.descriptor_tags.append(tag)
        if tag == DESC_SEGMENTATION:
            seg = _parse_segmentation(data[i + 2:i + 2 + length])
            if seg is not None:
                info.segmentation.append(seg)
        i += 2 + length
    return info


# --------------------------------------------------------------------------
# Building
# --------------------------------------------------------------------------

def _write_splice_time(w: BitWriter, pts: int | None) -> None:
    if pts is None:
        w.write(0, 1).write(0x7F, 7)
    else:
        w.write(1, 1).write(0x3F, 6).write(pts % PTS_MOD, 33)


def _build_section(command_type: int, command: bytes, descriptors: bytes,
                   pts_adjustment: int = 0, tier: int = 0xFFF) -> bytes:
    w = BitWriter()
    w.write(0, 8)                     # protocol_version
    w.write(0, 1).write(0, 6)         # encrypted_packet, encryption_algorithm
    w.write(pts_adjustment, 33)
    w.write(0, 8)                     # cw_index
    w.write(tier, 12)
    w.write(len(command), 12)
    w.write(command_type, 8)
    w.write_bytes(command)
    w.write(len(descriptors), 16)
    w.write_bytes(descriptors)
    body = w.to_bytes()
    length = len(body) + 4
    sec = bytearray([TABLE_ID, 0x30 | ((length >> 8) & 0x0F), length & 0xFF]) + body
    sec += crc32_mpeg2(sec).to_bytes(4, "big")
    return bytes(sec)


def build_splice_insert(si: SpliceInsert, pts_adjustment: int = 0) -> bytes:
    w = BitWriter().write(si.event_id, 32).write(si.cancel, 1).write(0x7F, 7)
    if not si.cancel:
        w.write(si.out_of_network, 1).write(1, 1).write(si.duration is not None, 1)
        w.write(si.splice_immediate, 1).write(1, 1).write(0x7, 3)
        if not si.splice_immediate:
            _write_splice_time(w, si.pts_time)
        if si.duration is not None:
            w.write(si.auto_return, 1).write(0x3F, 6).write(si.duration, 33)
        w.write(si.unique_program_id, 16).write(si.avail_num, 8).write(si.avails_expected, 8)
    return _build_section(CMD_SPLICE_INSERT, w.to_bytes(), b"", pts_adjustment)


def build_segmentation_descriptor(seg: SegmentationDescriptor) -> bytes:
    w = BitWriter().write(CUEI, 32).write(seg.event_id, 32).write(seg.cancel, 1).write(0x7F, 7)
    if not seg.cancel:
        w.write(1, 1).write(seg.duration is not None, 1).write(1, 1).write(0x1F, 5)
        if seg.duration is not None:
            w.write(seg.duration, 40)
        w.write(seg.upid_type, 8).write(len(seg.upid), 8).write_bytes(seg.upid)
        w.write(seg.type_id, 8).write(seg.segment_num, 8).write(seg.segments_expected, 8)
        if seg.type_id in _SEG_SUB_SEGMENT_TYPES:
            w.write(0, 8).write(0, 8)  # sub_segment_num, sub_segments_expected
    body = w.to_bytes()
    return bytes([DESC_SEGMENTATION, len(body)]) + body


def build_time_signal(pts: int | None, segmentation: list[SegmentationDescriptor],
                      pts_adjustment: int = 0) -> bytes:
    w = BitWriter()
    _write_splice_time(w, pts)
    descriptors = b"".join(build_segmentation_descriptor(s) for s in segmentation)
    return _build_section(CMD_TIME_SIGNAL, w.to_bytes(), descriptors, pts_adjustment)
