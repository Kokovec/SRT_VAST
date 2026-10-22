import base64
import unittest

from srtvast import scte35


class Scte35Tests(unittest.TestCase):
    def test_parse_reference_splice_insert(self):
        # splice_insert example from the SCTE-35 specification (with avail_descriptor).
        sec = base64.b64decode("/DAvAAAAAAAA///wFAVIAACPf+/+c2nALv4AUsz1AAAAAAAKAAhDVUVJAAABNWLbowo=")
        info = scte35.parse(sec)
        self.assertEqual(info.command_type, scte35.CMD_SPLICE_INSERT)
        si = info.splice_insert
        self.assertEqual(si.event_id, 0x4800008F)
        self.assertTrue(si.out_of_network)
        self.assertEqual(si.pts_time, 0x07369C02E)  # bytes fe 73 69 c0 2e
        self.assertAlmostEqual(si.duration / 90000, 60.293567, places=5)
        self.assertEqual(info.descriptor_tags, [scte35.DESC_AVAIL])
        cue = info.cue()
        self.assertEqual((cue.kind, cue.event_id, cue.pts), ("out", 0x4800008F, si.pts_time))

    def test_splice_insert_roundtrip_with_adjustment(self):
        si = scte35.SpliceInsert(event_id=42, pts_time=1_000_000, duration=30 * 90000,
                                 unique_program_id=7, avail_num=1, avails_expected=2)
        info = scte35.parse(scte35.build_splice_insert(si, pts_adjustment=500))
        self.assertEqual(info.splice_insert, si)
        cue = info.cue()
        self.assertEqual((cue.kind, cue.pts, cue.duration, cue.auto_return), ("out", 1_000_500, 2_700_000, True))

    def test_immediate_cue_in(self):
        si = scte35.SpliceInsert(event_id=9, out_of_network=False, splice_immediate=True)
        cue = scte35.parse(scte35.build_splice_insert(si)).cue()
        self.assertEqual((cue.kind, cue.pts), ("in", None))

    def test_time_signal_segmentation_roundtrip(self):
        seg = scte35.SegmentationDescriptor(event_id=77, type_id=0x34, duration=15 * 90000,
                                            upid_type=0x09, upid=b"SIGNAL:abc", segment_num=1, segments_expected=1)
        info = scte35.parse(scte35.build_time_signal(123_456, [seg]))
        self.assertEqual(info.time_signal_pts, 123_456)
        self.assertEqual(info.segmentation, [seg])
        cue = info.cue()
        self.assertEqual((cue.kind, cue.event_id, cue.duration), ("out", 77, 15 * 90000))

        end = scte35.SegmentationDescriptor(event_id=77, type_id=0x35)
        self.assertEqual(scte35.parse(scte35.build_time_signal(999, [end])).cue().kind, "in")

    def test_bad_crc_rejected(self):
        sec = bytearray(scte35.build_splice_insert(scte35.SpliceInsert(event_id=1, pts_time=0)))
        sec[10] ^= 0xFF
        with self.assertRaises(ValueError):
            scte35.parse(bytes(sec))


if __name__ == "__main__":
    unittest.main()
