import unittest
from pathlib import Path

from srtvast import vast
from srtvast.ads import Creative, build_vast, build_wrapper, select_pod


def library():
    return [Creative("ad1", "ACME & Sons", Path("ad1.ts"), 15.0),
            Creative("ad2", "Widget", Path("ad2.ts"), 15.0),
            Creative("ad3", "Ten", Path("ad3.ts"), 10.0)]


class VastTests(unittest.TestCase):
    def test_inline_pod(self):
        xml = build_vast(library()[:2], "http://ads", "1001")
        ads = vast.fetch_ads("http://ads/vast", fetch=lambda url: xml.encode())
        self.assertEqual([a.sequence for a in ads], [1, 2])
        ad = ads[0]
        self.assertEqual((ad.title, ad.duration), ("ACME & Sons", 15.0))
        self.assertEqual(ad.pick_media().url, "http://ads/media/ad1.ts")
        self.assertIn("http://ads/track?event=impression&ad=ad1&break=1001&seq=1&cb=[CACHEBUSTING]", ad.impressions)
        self.assertEqual(sorted(ad.tracking), ["complete", "firstQuartile", "midpoint", "start", "thirdQuartile"])

    def test_hls_media_preferred(self):
        lib = [Creative("ad1", "A", Path("ad1.ts"), 15.0, hls=True)]
        ad = vast.fetch_ads("http://ads/vast", fetch=lambda url: build_vast(lib, "http://ads", "7").encode())[0]
        self.assertEqual(len(ad.media_files), 1)
        self.assertEqual((ad.media_files[0].mime_type, ad.pick_media().url),
                         ("application/x-mpegURL", "http://ads/hls/ad1/master.m3u8"))
        ad.media_files.insert(0, vast.MediaFile("http://ads/media/ad1.ts", "video/MP2T"))
        self.assertEqual(ad.pick_media().url, "http://ads/hls/ad1/master.m3u8")

    def test_wrapper_merges_tracking(self):
        docs = {
            "http://ads/wrapper": build_wrapper("http://ads/vast", "http://ads", "5").encode(),
            "http://ads/vast": build_vast(library()[:1], "http://ads", "5").encode(),
        }
        ads = vast.fetch_ads("http://ads/wrapper", fetch=docs.__getitem__)
        self.assertEqual(len(ads), 1)
        self.assertEqual(len(ads[0].impressions), 2)
        self.assertTrue(ads[0].impressions[0].startswith("http://ads/track?event=impression&break=5&wrapper=1"))

    def test_empty_vast(self):
        self.assertEqual(vast.fetch_ads("x", fetch=lambda u: b'<VAST version="4.2"/>'), [])

    def test_select_pod_fills_duration(self):
        lib = library()
        self.assertEqual([c.ad_id for c in select_pod(lib, 30, 0)], ["ad1", "ad2"])
        self.assertEqual(sum(c.duration for c in select_pod(lib, 25, 2)), 25)

    def test_tracker_quartiles(self):
        fired = []
        ad = vast.Ad("a", tracking={e: [e] for e in ("start", "midpoint", "complete")}, impressions=["imp"])
        t = vast.AdTracker(ad, 10.0, fire_fn=lambda urls, **_: fired.extend(urls))
        t.start()
        t.progress(5.0)
        t.progress(6.0)
        t.complete()
        self.assertEqual(fired, ["imp", "start", "midpoint", "complete"])
        self.assertEqual(t.fired, ["impression", "start", "firstQuartile", "midpoint", "thirdQuartile", "complete"])


if __name__ == "__main__":
    unittest.main()
