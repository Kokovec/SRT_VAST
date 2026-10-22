"""MPEG-TS packet, PES timestamp and PSI helpers (ISO/IEC 13818-1).

Packets are 188-byte ``bytearray`` objects and are edited in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PACKET_SIZE = 188
SYNC_BYTE = 0x47
PID_PAT = 0x0000
PID_NULL = 0x1FFF

PTS_HZ = 90_000
PTS_MOD = 1 << 33
PCR_MOD = PTS_MOD * 300

STREAM_TYPE_H264 = 0x1B
STREAM_TYPE_HEVC = 0x24
STREAM_TYPE_SCTE35 = 0x86
VIDEO_STREAM_TYPES = {0x01, 0x02, 0x10, STREAM_TYPE_H264, STREAM_TYPE_HEVC}

# PES stream_ids that carry no optional header (and therefore no PTS/DTS).
_NO_PES_HEADER = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}


# --------------------------------------------------------------------------
# Timestamp arithmetic
# --------------------------------------------------------------------------

def pts_diff(a: int, b: int) -> int:
    """Signed ``a - b`` in 90 kHz ticks, tolerant of 33-bit wrap."""
    d = (a - b) % PTS_MOD
    return d - PTS_MOD if d >= PTS_MOD // 2 else d


def pcr_diff(a: int, b: int) -> int:
    """Signed ``a - b`` in 27 MHz ticks, tolerant of PCR wrap."""
    d = (a - b) % PCR_MOD
    return d - PCR_MOD if d >= PCR_MOD // 2 else d


# --------------------------------------------------------------------------
# Packet header fields
# --------------------------------------------------------------------------

def get_pid(pkt) -> int:
    return ((pkt[1] & 0x1F) << 8) | pkt[2]


def set_pid(pkt: bytearray, pid: int) -> None:
    pkt[1] = (pkt[1] & 0xE0) | ((pid >> 8) & 0x1F)
    pkt[2] = pid & 0xFF


def is_pusi(pkt) -> bool:
    return bool(pkt[1] & 0x40)


def get_cc(pkt) -> int:
    return pkt[3] & 0x0F


def set_cc(pkt: bytearray, cc: int) -> None:
    pkt[3] = (pkt[3] & 0xF0) | (cc & 0x0F)


def has_payload(pkt) -> bool:
    return bool(pkt[3] & 0x10)


def has_adaptation(pkt) -> bool:
    return bool(pkt[3] & 0x20)


def payload_offset(pkt) -> int:
    if not has_payload(pkt):
        return PACKET_SIZE
    off = 4
    if has_adaptation(pkt):
        off += 1 + pkt[4]
    return min(off, PACKET_SIZE)


def payload(pkt):
    return pkt[payload_offset(pkt):]


def is_random_access(pkt) -> bool:
    return has_adaptation(pkt) and pkt[4] > 0 and bool(pkt[5] & 0x40)


def get_pcr(pkt) -> int | None:
    """PCR in 27 MHz units, or None if the packet carries none."""
    if not has_adaptation(pkt) or pkt[4] < 7 or not (pkt[5] & 0x10):
        return None
    b = pkt
    base = (b[6] << 25) | (b[7] << 17) | (b[8] << 9) | (b[9] << 1) | (b[10] >> 7)
    ext = ((b[10] & 0x01) << 8) | b[11]
    return base * 300 + ext


def set_pcr(pkt: bytearray, pcr: int) -> None:
    base, ext = divmod(pcr % PCR_MOD, 300)
    pkt[6] = (base >> 25) & 0xFF
    pkt[7] = (base >> 17) & 0xFF
    pkt[8] = (base >> 9) & 0xFF
    pkt[9] = (base >> 1) & 0xFF
    pkt[10] = ((base & 1) << 7) | 0x7E | ((ext >> 8) & 1)
    pkt[11] = ext & 0xFF


# --------------------------------------------------------------------------
# PES timestamps
# --------------------------------------------------------------------------

def decode_timestamp(b) -> int:
    return (((b[0] >> 1) & 0x07) << 30) | (b[1] << 22) | ((b[2] >> 1) << 15) | (b[3] << 7) | (b[4] >> 1)


def encode_timestamp(prefix: int, ts: int) -> bytes:
    ts %= PTS_MOD
    return bytes([
        ((prefix & 0x0F) << 4) | (((ts >> 30) & 0x07) << 1) | 1,
        (ts >> 22) & 0xFF,
        (((ts >> 15) & 0x7F) << 1) | 1,
        (ts >> 7) & 0xFF,
        ((ts & 0x7F) << 1) | 1,
    ])


def _timestamp_offsets(pkt) -> tuple[int | None, int | None]:
    if not is_pusi(pkt):
        return None, None
    o = payload_offset(pkt)
    if o + 14 > PACKET_SIZE or pkt[o] != 0 or pkt[o + 1] != 0 or pkt[o + 2] != 1:
        return None, None
    if pkt[o + 3] in _NO_PES_HEADER:
        return None, None
    flags = pkt[o + 7] >> 6
    pts_at = o + 9 if flags & 0x2 else None
    dts_at = o + 14 if flags == 0x3 and o + 19 <= PACKET_SIZE else None
    return pts_at, dts_at


def get_pts_dts(pkt) -> tuple[int | None, int | None]:
    pts_at, dts_at = _timestamp_offsets(pkt)
    pts = decode_timestamp(pkt[pts_at:pts_at + 5]) if pts_at is not None else None
    dts = decode_timestamp(pkt[dts_at:dts_at + 5]) if dts_at is not None else None
    return pts, dts


def shift_pts_dts(pkt: bytearray, offset: int) -> None:
    """Add ``offset`` (90 kHz) to the PES PTS/DTS that start in this packet."""
    for at in _timestamp_offsets(pkt):
        if at is not None:
            value = decode_timestamp(pkt[at:at + 5])
            pkt[at:at + 5] = encode_timestamp(pkt[at] >> 4, value + offset)


def contains_idr(pkt, stream_type: int = STREAM_TYPE_H264) -> bool:
    data = bytes(payload(pkt))
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(data):
            return False
        nal = data[i + 3]
        if stream_type == STREAM_TYPE_HEVC:
            if 16 <= (nal >> 1) & 0x3F <= 21:
                return True
        elif nal & 0x1F == 5:
            return True
        i += 3


def is_keyframe(pkt, stream_type: int = STREAM_TYPE_H264) -> bool:
    """True if this packet starts a PES carrying a random access point."""
    return is_pusi(pkt) and (is_random_access(pkt) or contains_idr(pkt, stream_type))


# --------------------------------------------------------------------------
# Whole PES packets
# --------------------------------------------------------------------------

@dataclass
class Pes:
    stream_id: int
    pts: int | None
    dts: int | None
    payload: bytes


def parse_pes(data) -> Pes | None:
    """Parse a reassembled PES packet (the concatenated TS payloads of one PES)."""
    if len(data) < 9 or data[0] != 0 or data[1] != 0 or data[2] != 1:
        return None
    sid = data[3]
    length = (data[4] << 8) | data[5]
    end = 6 + length if length else len(data)
    if sid in _NO_PES_HEADER:
        return Pes(sid, None, None, bytes(data[6:end]))
    flags = data[7] >> 6
    pts = decode_timestamp(data[9:14]) if flags & 0x2 else None
    dts = decode_timestamp(data[14:19]) if flags == 0x3 else None
    return Pes(sid, pts, dts, bytes(data[9 + data[8]:end]))


def build_pes(stream_id: int, pts: int, data: bytes) -> bytes:
    """A PES packet with a PTS only (data_alignment_indicator set)."""
    length = 3 + 5 + len(data)
    header = bytes([0, 0, 1, stream_id, (length >> 8) & 0xFF if length <= 0xFFFF else 0,
                    length & 0xFF if length <= 0xFFFF else 0, 0x84, 0x80, 5])
    return header + encode_timestamp(0x2, pts) + data


def packetize_pes(pid: int, pes: bytes) -> list[bytearray]:
    """Split a PES packet into TS packets, stuffing the last one via its adaptation field.

    Continuity counters are left at 0 for the caller to assign.
    """
    packets = []
    for i in range(0, len(pes), 184):
        chunk = pes[i:i + 184]
        stuffing = 184 - len(chunk)
        af = b""
        if stuffing == 1:
            af = b"\x00"
        elif stuffing > 1:
            af = bytes([stuffing - 1, 0x00]) + b"\xff" * (stuffing - 2)
        header = bytes([SYNC_BYTE, (0x40 if i == 0 else 0) | ((pid >> 8) & 0x1F), pid & 0xFF,
                        0x30 if af else 0x10])
        packets.append(bytearray(header + af + chunk))
    return packets


# --------------------------------------------------------------------------
# AAC ADTS audio frames
# --------------------------------------------------------------------------

ADTS_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)
STREAM_TYPE_AAC_ADTS = 0x0F


def adts_frames(data) -> list[tuple[int, int, float]] | None:
    """Split a PES payload into ADTS frames: (offset, length, duration in 90 kHz ticks).

    Returns None unless the payload is exactly a sequence of whole ADTS frames.
    """
    frames, i, n = [], 0, len(data)
    while i < n:
        if i + 7 > n or data[i] != 0xFF or (data[i + 1] & 0xF6) != 0xF0:
            return None
        length = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        rate_index = (data[i + 2] >> 2) & 0x0F
        if length < 7 or i + length > n or rate_index >= len(ADTS_SAMPLE_RATES):
            return None
        blocks = (data[i + 6] & 0x03) + 1
        frames.append((i, length, 1024 * blocks * PTS_HZ / ADTS_SAMPLE_RATES[rate_index]))
        i += length
    return frames or None


# --------------------------------------------------------------------------
# Packet stream framing
# --------------------------------------------------------------------------

class PacketReader:
    """Splits an arbitrary byte stream into 188-byte packets, resyncing on 0x47."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self.resyncs = 0

    def feed(self, data: bytes) -> list[bytearray]:
        buf = self._buf
        buf += data
        out = []
        i, n = 0, len(buf)
        while n - i >= PACKET_SIZE:
            if buf[i] != SYNC_BYTE:
                self.resyncs += 1
                j = buf.find(SYNC_BYTE, i + 1)
                i = n if j < 0 else j
                continue
            out.append(bytearray(buf[i:i + PACKET_SIZE]))
            i += PACKET_SIZE
        del buf[:i]
        return out


def split_packets(data: bytes) -> list[bytearray]:
    return PacketReader().feed(data)


# --------------------------------------------------------------------------
# PSI sections
# --------------------------------------------------------------------------

def _make_crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


_CRC_TABLE = _make_crc_table()


def crc32_mpeg2(data) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def section_length(sec) -> int:
    return ((sec[1] & 0x0F) << 8) | sec[2]


def section_crc_ok(sec) -> bool:
    return crc32_mpeg2(sec[:3 + section_length(sec)]) == 0


class SectionAssembler:
    """Reassembles PSI / SCTE-35 sections from the packets of one PID."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._active = False

    def push(self, pkt) -> list[bytes]:
        data = payload(pkt)
        if not data:
            return []
        out: list[bytes] = []
        if is_pusi(pkt):
            pointer = data[0]
            rest = data[1:]
            if self._active:
                self._buf += rest[:pointer]
                out += self._drain()
            self._buf = bytearray(rest[pointer:])
            self._active = True
        elif self._active:
            self._buf += data
        else:
            return []
        out += self._drain()
        return out

    def _drain(self) -> list[bytes]:
        out = []
        buf = self._buf
        while len(buf) >= 3:
            if buf[0] == 0xFF:  # stuffing: rest of the packet is padding
                buf.clear()
                self._active = False
                break
            length = 3 + section_length(buf)
            if len(buf) < length:
                break
            out.append(bytes(buf[:length]))
            del buf[:length]
        return out


def packetize_section(pid: int, section: bytes, cc: int) -> tuple[list[bytearray], int]:
    """Split a section into TS packets. Returns (packets, next continuity counter)."""
    data = b"\x00" + section  # pointer_field
    packets = []
    first = True
    while data or first:
        chunk, data = data[:184], data[184:]
        header = bytes([SYNC_BYTE, (0x40 if first else 0) | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10 | cc])
        packets.append(bytearray(header + chunk + b"\xff" * (184 - len(chunk))))
        cc = (cc + 1) & 0x0F
        first = False
    return packets, cc


def _finish_section(table_id: int, body: bytes) -> bytes:
    length = len(body) + 4
    sec = bytearray([table_id, 0xB0 | ((length >> 8) & 0x0F), length & 0xFF]) + body
    sec += crc32_mpeg2(sec).to_bytes(4, "big")
    return bytes(sec)


@dataclass
class EsInfo:
    stream_type: int
    pid: int
    descriptors: bytes = b""


@dataclass
class Pmt:
    program_number: int
    pcr_pid: int
    streams: list[EsInfo] = field(default_factory=list)
    program_info: bytes = b""
    version: int = 0


def parse_pat(sec) -> dict[int, int]:
    """Returns {program_number: pmt_pid} (network PID entries are skipped)."""
    end = 3 + section_length(sec) - 4
    programs = {}
    for i in range(8, end - 3, 4):
        number = (sec[i] << 8) | sec[i + 1]
        pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
        if number != 0:
            programs[number] = pid
    return programs


def build_pat(programs: dict[int, int], ts_id: int = 1, version: int = 0) -> bytes:
    body = bytearray([ts_id >> 8, ts_id & 0xFF, 0xC1 | ((version & 0x1F) << 1), 0, 0])
    for number, pid in programs.items():
        body += bytes([number >> 8, number & 0xFF, 0xE0 | (pid >> 8), pid & 0xFF])
    return _finish_section(0x00, bytes(body))


def parse_pmt(sec) -> Pmt:
    end = 3 + section_length(sec) - 4
    info_len = ((sec[10] & 0x0F) << 8) | sec[11]
    pmt = Pmt(
        program_number=(sec[3] << 8) | sec[4],
        version=(sec[5] >> 1) & 0x1F,
        pcr_pid=((sec[8] & 0x1F) << 8) | sec[9],
        program_info=bytes(sec[12:12 + info_len]),
    )
    i = 12 + info_len
    while i + 5 <= end:
        es_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
        pmt.streams.append(EsInfo(sec[i], ((sec[i + 1] & 0x1F) << 8) | sec[i + 2], bytes(sec[i + 5:i + 5 + es_len])))
        i += 5 + es_len
    return pmt


def build_pmt(pmt: Pmt) -> bytes:
    pn, pcr, info = pmt.program_number, pmt.pcr_pid, pmt.program_info
    body = bytearray([
        pn >> 8, pn & 0xFF, 0xC1 | ((pmt.version & 0x1F) << 1), 0, 0,
        0xE0 | (pcr >> 8), pcr & 0xFF, 0xF0 | (len(info) >> 8), len(info) & 0xFF,
    ]) + info
    for s in pmt.streams:
        d = s.descriptors
        body += bytes([s.stream_type, 0xE0 | (s.pid >> 8), s.pid & 0xFF, 0xF0 | (len(d) >> 8), len(d) & 0xFF]) + d
    return _finish_section(0x02, bytes(body))


# --------------------------------------------------------------------------
# Whole-file helpers
# --------------------------------------------------------------------------

def find_pmt(packets) -> Pmt | None:
    """Locate the first program's PMT in a list of packets."""
    pat_asm, pmt_asm, pmt_pid = SectionAssembler(), SectionAssembler(), None
    for pkt in packets:
        pid = get_pid(pkt)
        if pid == PID_PAT and pmt_pid is None:
            for sec in pat_asm.push(pkt):
                if sec[0] == 0x00 and section_crc_ok(sec):
                    programs = parse_pat(sec)
                    if programs:
                        pmt_pid = next(iter(programs.values()))
        elif pmt_pid is not None and pid == pmt_pid:
            for sec in pmt_asm.push(pkt):
                if sec[0] == 0x02 and section_crc_ok(sec):
                    return parse_pmt(sec)
    return None


def video_stream(pmt: Pmt) -> EsInfo | None:
    return next((s for s in pmt.streams if s.stream_type in VIDEO_STREAM_TYPES), None)
