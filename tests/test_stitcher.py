import unittest

from srtvast import scte35, vast
from srtvast.stitcher import Stitcher, build_pod
from srtvast.ts import (
    adts_frames, get_cc, get_pcr, get_pid, get_pts_dts, has_payload, is_pusi, parse_pes,
    pcr_diff, payload, pts_diff,
)
from tests.tsgen import AUDIO_FRAME, AUDIO_PID, FRAME, VIDEO_PID, StreamBuilder, to_bytes

CONTENT, AD, SLATE = 0xCC, 0xAA, 0x55


class FakeTracker:
    def __init__(self, ad, duration):
        self.events = []

    def start(self):
        self.events.append("start")

    def progress(self, seconds):
        pass

    def complete(self):
        self.events.append("complete")


def run(ad_frames=60, break_frames=60, out_frame=120, cue=None, loader_error=None, total=300,
        idr=(), slate_frames=None):
    """Content with a cue at frame 60 for a break starting at ``out_frame``."""
    content = StreamBuilder(CONTENT, idr=idr)
    ad = StreamBuilder(AD, start_pts=900_000, scte35=False)
    ad_bytes = to_bytes(ad.frames(0, ad_frames))
    slate = None
    if slate_frames:
        slate = to_bytes(StreamBuilder(SLATE, start_pts=5_000_000, scte35=False).frames(0, slate_frames))
    out, trackers = [], []

    def loader(brk, streams):
        if loader_error:
            raise loader_error
        return build_pod([(vast.Ad("test-ad"), ad_bytes)], streams)

    def tracker(ad_meta, duration):
        trackers.append(FakeTracker(ad_meta, duration))
        return trackers[-1]

    st = Stitcher(out.append, loader, spawn=lambda fn: fn(), tracker_factory=tracker, slate=slate)
    splice_pts = content.pts(out_frame)
    cue = cue or scte35.build_splice_insert(scte35.SpliceInsert(
        event_id=1, pts_time=splice_pts, duration=break_frames * FRAME))
    packets = content.frames(0, 60) + content.section(cue) + content.frames(60, total - 60)
    for p in packets:
        st.feed(p)
    return out, splice_pts, trackers


def pes_units(out, pid):
    units, cur = [], None
    for p in out:
        if get_pid(p) != pid or not has_payload(p):
            continue
        if is_pusi(p):
            cur = bytearray()
            units.append(cur)
        if cur is not None:
            cur += payload(p)
    return [parse_pes(u) for u in units]


def video_frames(out):
    return [(u.pts, u.payload[-1]) for u in pes_units(out, VIDEO_PID)]


def audio_frames(out):
    """(start pts, marker) of every ADTS frame in the output."""
    frames = []
    for u in pes_units(out, AUDIO_PID):
        start = u.pts
        for off, length, ticks in adts_frames(u.payload):
            frames.append((start, u.payload[off + 7]))
            start += round(ticks)
    return frames


class StitcherTests(unittest.TestCase):
    def assert_clean(self, out):
        last = {}
        for p in out:
            pid = get_pid(p)
            if has_payload(p) and pid in (VIDEO_PID, AUDIO_PID):
                if pid in last:
                    self.assertEqual(get_cc(p), (last[pid] + 1) & 0xF, "CC discontinuity")
                last[pid] = get_cc(p)
        pcrs = [get_pcr(p) for p in out if get_pcr(p) is not None]
        self.assertTrue(all(pcr_diff(b, a) > 0 for a, b in zip(pcrs, pcrs[1:])), "PCR not monotonic")
        pts = [p for p, _ in video_frames(out)]
        self.assertEqual([b - a for a, b in zip(pts, pts[1:])], [FRAME] * (len(pts) - 1), "video PTS gap")
        audio = audio_frames(out)
        for (a, _), (b, _) in zip(audio, audio[1:]):
            self.assertEqual(pts_diff(b, a), AUDIO_FRAME, f"audio not contiguous at {b}")

    def assert_segments(self, out, segments):
        """``segments`` = [(first pts, marker), ...]: every video frame and every audio
        frame (by its midpoint) must come from the source owning that time."""
        def owner(t):
            marker = CONTENT
            for start, m in segments:
                if t >= start:
                    marker = m
            return marker
        for pts, m in video_frames(out):
            self.assertEqual(m, owner(pts), f"video frame at {pts}")
        for start, m in audio_frames(out):
            self.assertEqual(m, owner(start + AUDIO_FRAME // 2), f"audio frame at {start}")

    def test_frame_accurate_splice_out_and_back(self):
        out, s, trackers = run()
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 60 * FRAME, CONTENT)])
        video = dict(video_frames(out))
        self.assertEqual(sum(m == AD for m in video.values()), 60)
        self.assertEqual(len(video), 300)
        self.assertEqual(trackers[0].events, ["start", "complete"])

    def test_audio_pes_straddling_the_splice_is_split(self):
        out, s, _ = run()
        audio = audio_frames(out)
        # Content audio runs right up to the splice and resumes right after it.
        last_content = max(t for t, m in audio if m == CONTENT and t < s)
        first_ad = min(t for t, m in audio if m == AD)
        self.assertEqual(first_ad, last_content + AUDIO_FRAME)       # gapless
        self.assertLess(abs(pts_diff(first_ad + AUDIO_FRAME // 2, s)), AUDIO_FRAME // 2 + 1)
        # The content audio grid sits half a frame off the ad's here, the worst case:
        # the ad audio is moved by exactly half a frame and A/V stays within that.
        self.assertEqual(pts_diff(first_ad - (s - AUDIO_FRAME), 0) % AUDIO_FRAME, AUDIO_FRAME // 2)

    def test_splice_out_mid_gop(self):
        # Break starts on frame 125 (no content IDR there) and returns on the IDR at 180.
        out, s, _ = run(out_frame=125, break_frames=55, ad_frames=55)
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 55 * FRAME, CONTENT)])

    def test_long_ad_is_cut_on_the_break_end_frame(self):
        out, s, trackers = run(ad_frames=90)
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 60 * FRAME, CONTENT)])
        self.assertEqual(trackers[0].events, ["start"])

    def test_short_pod_is_filled_with_slate(self):
        out, s, trackers = run(ad_frames=45, slate_frames=10)
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 45 * FRAME, SLATE), (s + 60 * FRAME, CONTENT)])
        self.assertEqual(trackers[0].events, ["start", "complete"])

    def test_unconditioned_return_is_held_to_next_idr(self):
        # Break end lands on frame 170, content IDRs are at 150 and 180 only.
        out, s, _ = run(break_frames=50, ad_frames=50, slate_frames=30)
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 50 * FRAME, SLATE), (s + 60 * FRAME, CONTENT)])

    def test_content_outside_the_break_is_untouched(self):
        content = StreamBuilder(CONTENT)
        source = content.frames(0, 60) + content.frames(60, 240)
        out, s, _ = run()
        e = s + 60 * FRAME
        strip = lambda p: bytes(p[:3]) + bytes([p[3] & 0xF0]) + bytes(p[4:])  # noqa: E731 - CC is rewritten
        for pid, margin in ((VIDEO_PID, 0), (AUDIO_PID, 8 * AUDIO_FRAME)):
            src, pts = [], None
            for p in source:
                if get_pid(p) == pid:
                    pts = get_pts_dts(p)[0] if is_pusi(p) else pts
                    src.append((pts, strip(p)))
            got = [strip(p) for p in out if get_pid(p) == pid]
            # Every PES ending before the splice-out, and every PES starting after
            # the splice-in, comes through bit for bit.
            head = [b for t, b in src if pts_diff(t, s - margin) < 0]
            tail = [b for t, b in src if pts_diff(t, e) >= 0]
            self.assertEqual(got[:len(head)], head, f"PID 0x{pid:X} before the break")
            self.assertEqual(got[-len(tail):], tail, f"PID 0x{pid:X} after the break")

    def test_failed_ad_decision_keeps_content(self):
        out, _, _ = run(loader_error=LookupError("no fill"))
        self.assert_clean(out)
        self.assertTrue(all(m == CONTENT for _, m in video_frames(out)))
        self.assertTrue(all(m == CONTENT for _, m in audio_frames(out)))

    def test_time_signal_with_segmentation(self):
        s = StreamBuilder(CONTENT).pts(120)
        seg = scte35.SegmentationDescriptor(event_id=5, type_id=0x34, duration=60 * FRAME)
        out, _, _ = run(cue=scte35.build_time_signal(s, [seg]))
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 60 * FRAME, CONTENT)])

    def test_cue_between_frames_snaps_to_the_next_frame(self):
        content = StreamBuilder(CONTENT)
        s = content.pts(120)
        cue = scte35.build_splice_insert(scte35.SpliceInsert(event_id=1, pts_time=s - 1000, duration=60 * FRAME))
        out, _, _ = run(cue=cue)
        self.assert_clean(out)
        self.assert_segments(out, [(s, AD), (s + 60 * FRAME, CONTENT)])


if __name__ == "__main__":
    unittest.main()
