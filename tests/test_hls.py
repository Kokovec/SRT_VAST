import unittest

from srtvast import hls

MASTER = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
hi/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-MAP:URI="init.mp4"
#EXTINF:2.000000,
seg_000.m4s
#EXTINF:1.5,
seg_001.m4s
#EXT-X-ENDLIST
"""


class HlsTests(unittest.TestCase):
    def test_master(self):
        variants = hls.parse_playlist(MASTER, "http://ads/hls/ad1/master.m3u8")
        self.assertEqual([v.url for v in variants],
                         ["http://ads/hls/ad1/low/index.m3u8", "http://ads/hls/ad1/hi/index.m3u8"])
        self.assertEqual(variants[1].codecs, "avc1.4d401f,mp4a.40.2")
        self.assertEqual(hls.pick_variant(variants).bandwidth, 2_800_000)
        self.assertEqual(hls.pick_variant(variants, 1_000_000).bandwidth, 800_000)
        self.assertEqual(hls.pick_variant(variants, 1).bandwidth, 800_000)

    def test_media(self):
        pl = hls.parse_playlist(MEDIA, "http://ads/hls/ad1/index.m3u8")
        self.assertEqual(pl.init_url, "http://ads/hls/ad1/init.mp4")
        self.assertEqual([s.url for s in pl.segments],
                         ["http://ads/hls/ad1/seg_000.m4s", "http://ads/hls/ad1/seg_001.m4s"])
        self.assertTrue(pl.ended)
        self.assertAlmostEqual(pl.duration, 3.5)

    def test_load_ts_follows_master_and_fetches_init_first(self):
        files = {
            "http://ads/m.m3u8": MASTER.encode(),
            "http://ads/hi/index.m3u8": MEDIA.encode(),
            "http://ads/hi/init.mp4": b"INIT",
            "http://ads/hi/seg_000.m4s": b"S0",
            "http://ads/hi/seg_001.m4s": b"S1",
        }
        fetched = []

        def fetch(url):
            fetched.append(url)
            return files[url]

        out = hls.load_ts("http://ads/m.m3u8", fetch, remux=lambda init, segs: init + b"|" + b"".join(segs))
        self.assertEqual(out, b"INIT|S0S1")
        self.assertEqual(fetched[:3], ["http://ads/m.m3u8", "http://ads/hi/index.m3u8", "http://ads/hi/init.mp4"])

    def test_ts_segments_are_concatenated(self):
        self.assertEqual(hls.transmux(None, [b"\x47a", b"\x47b"]), b"\x47a\x47b")

    def test_rejects_unsupported(self):
        enc = MEDIA.replace("#EXT-X-MAP", '#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXT-X-MAP')
        with self.assertRaises(ValueError):
            hls.parse_playlist(enc, "http://ads/x.m3u8")
        with self.assertRaises(ValueError):
            hls.parse_playlist("<html>", "http://ads/x.m3u8")

    def test_is_hls(self):
        self.assertTrue(hls.is_hls("application/x-mpegURL", "http://ads/x"))
        self.assertTrue(hls.is_hls("", "http://ads/x/master.m3u8?cb=1"))
        self.assertFalse(hls.is_hls("video/MP2T", "http://ads/x.ts"))


if __name__ == "__main__":
    unittest.main()
