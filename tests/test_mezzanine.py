import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from srtvast import vast
from srtvast.ads import Creative, Mezzanine, build_vast
from srtvast.stitcher import Break, VastPodLoader
from tests.tsgen import AUDIO_PID, VIDEO_PID, StreamBuilder, to_bytes

MEZ = Mezzanine(Path("ad1.mp4"), 1920, 1080, "25", "avc1.640028", 1234)


def creative(**kw):
    return Creative("ad1", "ACME", Path("ad1.ts"), 2.0, mezzanine=MEZ, **kw)


class MezzanineVastTests(unittest.TestCase):
    def test_mezzanine_only_delivery(self):
        xml = build_vast([creative()], "http://ads", "9", delivery="mezzanine")
        ad = vast.parse_vast(xml)[0]
        self.assertEqual(ad.media_files, [])
        self.assertEqual(len(ad.mezzanine), 1)
        m = ad.mezzanine[0]
        self.assertEqual((m.url, m.mime_type, m.width, m.height, m.delivery),
                         ("http://ads/mez/ad1.mp4", "video/mp4", 1920, 1080, "progressive"))
        self.assertIsNone(ad.pick_media())

    def test_both_and_streaming_delivery(self):
        both = vast.parse_vast(build_vast([creative()], "http://ads", "9", delivery="both"))[0]
        self.assertEqual((len(both.media_files), len(both.mezzanine)), (1, 1))
        streaming = vast.parse_vast(build_vast([creative()], "http://ads", "9"))[0]
        self.assertEqual((len(streaming.media_files), len(streaming.mezzanine)), (1, 0))


class ConformLoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "ad.ts").write_bytes(to_bytes(StreamBuilder(0xAA, scte35=False).frames(0, 60)))
        self.calls = tmp / "calls"
        self.cmd = tmp / "conform.sh"
        # Stand-in for the ffmpeg conform: records the input it was given, emits a matching TS.
        self.cmd.write_text(f'#!/bin/sh\necho "$1" >> {self.calls}\ncat {tmp / "ad.ts"}\n')
        self.cmd.chmod(self.cmd.stat().st_mode | stat.S_IEXEC)
        self.xml = build_vast([creative()], "http://ads", "9", delivery="mezzanine")

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, loader, event_id):
        with mock.patch.object(vast, "http_get", side_effect=lambda url, *_: self.xml.encode() if "vast" in url else b"MEZ"):
            return loader(Break(event_id, 0, 60 * 3000), {VIDEO_PID: 0x1B, AUDIO_PID: 0x0F})

    def test_mezzanine_is_conformed_once_then_cached(self):
        loader = VastPodLoader("http://ads/vast?dur={duration}", conform_cmd=str(self.cmd))
        first = self.load(loader, 1)
        second = self.load(loader, 2)
        self.assertEqual(len(first.ads), 1)
        self.assertEqual(first.ads[0].duration, 60 * 3000)
        self.assertEqual(len(second.ads), 1)
        self.assertEqual(len(self.calls.read_text().splitlines()), 1, "second break must hit the cache")

    def test_failed_conform_is_retried(self):
        bad = Path(self.tmp.name) / "bad.sh"
        bad.write_text("#!/bin/sh\nexit 1\n")
        bad.chmod(0o755)
        loader = VastPodLoader("http://ads/vast", conform_cmd=str(bad))
        with mock.patch.object(vast, "fire"), self.assertRaises(LookupError):
            self.load(loader, 1)
        loader.conform_cmd = str(self.cmd)
        self.assertEqual(len(self.load(loader, 2).ads), 1)


if __name__ == "__main__":
    unittest.main()
