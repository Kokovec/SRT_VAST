import unittest

from srtvast.ts import (
    PTS_MOD, EsInfo, Pmt, SectionAssembler, build_pmt, crc32_mpeg2, decode_timestamp,
    encode_timestamp, get_pcr, get_pts_dts, packetize_section, parse_pmt, pts_diff,
    section_crc_ok, set_pcr, shift_pts_dts,
)
from tests.tsgen import VIDEO_PID, pes, ts_packet


class TsTests(unittest.TestCase):
    def test_crc_check_value(self):
        self.assertEqual(crc32_mpeg2(b"123456789"), 0x0376E6E7)

    def test_timestamp_roundtrip_and_wrap(self):
        for ts in (0, 1, 126_000, PTS_MOD - 1):
            self.assertEqual(decode_timestamp(encode_timestamp(2, ts)), ts)
        self.assertEqual(pts_diff(5, PTS_MOD - 5), 10)
        self.assertEqual(pts_diff(PTS_MOD - 5, 5), -10)

    def test_pcr_and_pts_rewrite(self):
        pkt = ts_packet(VIDEO_PID, pes(0xE0, 900_000, 0xAA), pusi=True, rai=True, pcr=123_456_789)
        self.assertEqual(get_pcr(pkt), 123_456_789)
        set_pcr(pkt, 987_654_321)
        self.assertEqual(get_pcr(pkt), 987_654_321)
        shift_pts_dts(pkt, -800_000)
        self.assertEqual(get_pts_dts(pkt), (100_000, None))

    def test_pmt_roundtrip_through_packets(self):
        pmt = Pmt(1, 0x100, [EsInfo(0x1B, 0x100), EsInfo(0x86, 0x1F4, b"\x8a\x01\x00")], b"\x05\x04CUEI")
        sec = build_pmt(pmt)
        self.assertTrue(section_crc_ok(sec))
        packets, cc = packetize_section(0x1000, sec, 15)
        self.assertEqual(cc, 0)
        asm = SectionAssembler()
        out = [s for p in packets for s in asm.push(p)]
        self.assertEqual(out, [sec])
        self.assertEqual(parse_pmt(out[0]), pmt)

    def test_multi_packet_section(self):
        sec = build_pmt(Pmt(1, 0x100, [EsInfo(0x06, 0x200 + i, b"\x00" * 20) for i in range(12)]))
        packets, _ = packetize_section(0x1000, sec, 0)
        self.assertGreater(len(packets), 1)
        asm = SectionAssembler()
        self.assertEqual([s for p in packets for s in asm.push(p)], [sec])


if __name__ == "__main__":
    unittest.main()
