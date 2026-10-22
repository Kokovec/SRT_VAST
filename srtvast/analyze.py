"""Check a recorded transport stream for splice damage.

Reports continuity-counter errors, PTS jumps per PES stream, PCR jumps,
audio continuity at the frame level (AAC ADTS: overlaps and gaps between
consecutive frames), and for every SCTE-35 break whether the output has an
IDR on the exact splice-out and splice-in frames. A clean, frame-accurate
stitch shows continuous video PTS, no audio overlaps, audio gaps shorter than
one frame only at splice points, and IDRs on both splice frames.

    python -m srtvast.analyze out/received.ts
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from . import scte35
from .ts import (
    PTS_HZ, PTS_MOD, STREAM_TYPE_AAC_ADTS, STREAM_TYPE_SCTE35, VIDEO_STREAM_TYPES,
    SectionAssembler, adts_frames, find_pmt, get_cc, get_pcr, get_pid, get_pts_dts,
    has_payload, is_keyframe, is_pusi, parse_pes, payload, pcr_diff, pts_diff, split_packets,
)


def analyze(data: bytes) -> list[str]:
    packets = split_packets(data)
    pmt = find_pmt(packets)
    if pmt is None:
        return ["no PAT/PMT found"]
    types = {s.pid: s.stream_type for s in pmt.streams}
    video_pid = next((p for p, t in types.items() if t in VIDEO_STREAM_TYPES), None)

    lines = [f"{len(packets)} packets, streams: "
             + ", ".join(f"0x{p:X}(type 0x{t:02X})" for p, t in types.items())]
    cc_last: dict[int, int] = {}
    cc_errors: Counter = Counter()
    pts_seen: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    pcrs: list[int] = []
    asm = SectionAssembler()
    cues = []
    breaks: dict[int, tuple[int, int | None]] = {}   # event_id -> (out pts, duration)
    pes_data: dict[int, list[bytearray]] = defaultdict(list)

    for pkt in packets:
        pid = get_pid(pkt)
        if pid in types and has_payload(pkt):
            if pid in cc_last and get_cc(pkt) != (cc_last[pid] + 1) & 0x0F:
                cc_errors[pid] += 1
            cc_last[pid] = get_cc(pkt)
        pcr = get_pcr(pkt)
        if pcr is not None:
            pcrs.append(pcr)
        if types.get(pid) == STREAM_TYPE_SCTE35:
            for sec in asm.push(pkt):
                try:
                    cue = scte35.parse(sec).cue()
                except ValueError as exc:
                    cues.append(f"invalid section: {exc}")
                    continue
                if cue and cue.kind == "out" and cue.pts is not None:
                    breaks.setdefault(cue.event_id, (cue.pts, cue.duration))
                if cue:
                    cues.append(f"{cue.source} {cue.kind} event={cue.event_id} pts={cue.pts} "
                                f"duration={cue.duration / PTS_HZ if cue.duration else None}")
        elif pid in types and is_pusi(pkt):
            pts, _ = get_pts_dts(pkt)
            if pts is not None:
                pts_seen[pid].append((pts, pid == video_pid and is_keyframe(pkt, types[pid])))
        if types.get(pid) == STREAM_TYPE_AAC_ADTS and has_payload(pkt):
            if is_pusi(pkt):
                pes_data[pid].append(bytearray())
            if pes_data[pid]:
                pes_data[pid][-1] += payload(pkt)

    origin = pts_seen[video_pid][0][0] if pts_seen.get(video_pid) else 0
    for pid, seen in pts_seen.items():
        deltas = [pts_diff(b[0], a[0]) for a, b in zip(seen, seen[1:])]
        if not deltas:
            continue
        nominal = Counter(deltas).most_common(1)[0][0]
        slack = max(nominal // 2, 1) if pid == video_pid else max(nominal * 3, PTS_HZ // 10)
        jumps = [(i, d) for i, d in enumerate(deltas) if abs(d - nominal) > slack]
        span = pts_diff(seen[-1][0], seen[0][0]) / PTS_HZ
        lines.append(f"PID 0x{pid:X}: {len(seen)} PES over {span:.2f}s, nominal step {nominal} ticks, "
                     f"{len(jumps)} PTS jump(s), {cc_errors[pid]} CC error(s)")
        for i, d in jumps[:20]:
            at = pts_diff(seen[i + 1][0], origin) / PTS_HZ
            kf = " (keyframe)" if seen[i + 1][1] else ""
            lines.append(f"    t={at:9.3f}s  step {d:+d} ticks ({d / PTS_HZ:+.3f}s){kf}")

    for pid, units in pes_data.items():
        lines += _audio_report(pid, units, origin)
    if video_pid in pts_seen:
        lines += _splice_report(breaks, pts_seen[video_pid], origin)

    pcr_steps = [pcr_diff(b, a) for a, b in zip(pcrs, pcrs[1:])]
    bad_pcr = [d for d in pcr_steps if d < 0 or d > 27_000_000 // 10]
    lines.append(f"PCR: {len(pcrs)} values, {len(bad_pcr)} jump(s) (>100 ms or backwards)")
    lines.append(f"SCTE-35: {len(cues)} section(s)")
    lines += [f"    {c}" for c in cues[:20]]
    return lines


def _audio_report(pid: int, units: list[bytearray], origin: int) -> list[str]:
    frames: list[tuple[int, float]] = []  # (pts, duration)
    for raw in units:
        pes = parse_pes(raw)
        parsed = adts_frames(pes.payload) if pes is not None and pes.pts is not None else None
        if not parsed:
            continue
        start = 0.0
        for _, _, ticks in parsed:
            frames.append(((pes.pts + round(start)) % PTS_MOD, ticks))
            start += ticks
    if len(frames) < 2:
        return []
    overlaps, gaps = [], []
    for (a, dur), (b, _) in zip(frames, frames[1:]):
        gap = pts_diff(b, a) - dur
        if gap < -1:
            overlaps.append((b, gap))
        elif gap > 1:
            gaps.append((b, gap))
    frame = frames[0][1]
    lines = [f"PID 0x{pid:X} audio: {len(frames)} AAC frames ({frame:.0f} ticks), {len(overlaps)} overlap(s), "
             f"{len(gaps)} gap(s), largest gap {max((g for _, g in gaps), default=0) / 90:.1f} ms "
             f"({'under' if all(g < frame for _, g in gaps) else 'NOT under'} one frame)"]
    for at, gap in (overlaps + gaps)[:20]:
        kind = "overlap" if gap < 0 else "gap"
        lines.append(f"    t={pts_diff(at, origin) / PTS_HZ:9.3f}s  {kind} {abs(gap) / 90:.1f} ms")
    return lines


def _splice_report(breaks: dict[int, tuple[int, int | None]], video: list[tuple[int, bool]],
                   origin: int) -> list[str]:
    frames = {pts: key for pts, key in video}
    first, last = video[0][0], video[-1][0]
    lines = []
    for eid, (out, duration) in sorted(breaks.items()):
        if pts_diff(out, first) < 0 or pts_diff(out, last) > 0:
            continue
        points = [("out", out)] + ([("in", (out + duration) % PTS_MOD)] if duration else [])
        parts = []
        for kind, at in points:
            if pts_diff(at, last) > 0:
                parts.append(f"{kind} not recorded")
            elif at not in frames:
                parts.append(f"{kind} pts={at} NO FRAME AT THAT PTS")
            else:
                parts.append(f"{kind} pts={at} {'IDR' if frames[at] else 'NOT AN IDR'}")
        count = sum(1 for p in frames if duration and 0 <= pts_diff(p, out) < duration)
        lines.append(f"break {eid} at t={pts_diff(out, origin) / PTS_HZ:.3f}s: " + ", ".join(parts)
                     + (f", {count} frames inside" if duration else ""))
    return lines


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("file")
    args = p.parse_args(argv)
    with open(args.file, "rb") as f:
        print("\n".join(analyze(f.read())))


if __name__ == "__main__":
    main()
