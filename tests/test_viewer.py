import json
import logging
import socket
import unittest

from srtvast import UdpEventHandler
from srtvast.viewer import Hub


class ViewerTests(unittest.TestCase):
    def test_hub_backlog_and_fanout(self):
        hub = Hub(maxsize=10, backlog=2)
        for i in range(3):
            hub.publish(i)
        q, backlog = hub.subscribe()
        self.assertEqual(backlog, [1, 2])
        hub.publish(3)
        self.assertEqual(q.get_nowait(), 3)

    def test_slow_subscriber_is_dropped(self):
        hub = Hub(maxsize=2)
        q, _ = hub.subscribe()
        for i in range(3):
            hub.publish(i)
        self.assertFalse(hub.subscribed(q))
        self.assertEqual(len(hub), 0)

    def test_log_records_sent_as_json(self):
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(2)
        handler = UdpEventHandler(f"127.0.0.1:{sink.getsockname()[1]},unresolvable.invalid:5100")
        logger = logging.getLogger("stitcher-test")
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
        try:
            logger.info(">>> SPLICE OUT break %d", 1001)
            event = json.loads(sink.recv(65536))
        finally:
            logger.removeHandler(handler)
            handler.close()
            sink.close()
        self.assertEqual((event["logger"], event["level"], event["msg"]),
                         ("stitcher-test", "INFO", ">>> SPLICE OUT break 1001"))


if __name__ == "__main__":
    unittest.main()


class PreviewTests(unittest.TestCase):
    def test_forwarder_sends_datagrams_as_is(self):
        from srtvast.transport import UdpForwarder
        sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sink.bind(("127.0.0.1", 0))
        sink.settimeout(2)
        fwd = UdpForwarder(f"localhost:{sink.getsockname()[1]}")
        self.assertTrue(fwd.wait_resolved(5))
        try:
            fwd.send(b"\x47" + b"\x00" * 187)
            self.assertEqual(sink.recv(2048), b"\x47" + b"\x00" * 187)
        finally:
            sink.close()
            fwd.sock.close()

    def test_preview_route_absent_unless_enabled(self):
        import threading
        import urllib.error
        import urllib.request
        from srtvast.viewer import ViewerServer
        server = ViewerServer(("127.0.0.1", 0), Hub(10), Hub(10))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/preview.ts", timeout=5)
            self.assertEqual(ctx.exception.code, 404)
            with urllib.request.urlopen(base + "/api/session", timeout=5) as r:
                self.assertFalse(json.loads(r.read())["preview"])
        finally:
            server.shutdown()
            server.server_close()


class AlignerTests(unittest.TestCase):
    def run_feeds(self, preview_lag):
        """Program frames at 30 fps; preview frames (15 fps) delivered `preview_lag` s later."""
        from srtvast.viewer import StreamAligner
        now = [0.0]
        a = StreamAligner({"main": Hub(10), "preview": Hub(10)}, smoothing=0.2, clock=lambda: now[0], start=False)
        events = []
        for f in range(300):
            pts = 900_000 + f * 3000
            events.append((f / 30, "main", pts))
            if f % 2 == 0:
                events.append((f / 30 + preview_lag, "preview", pts))
        for t, name, pts in sorted(events):
            now[0] = t
            a.offer(name, b"x", pts)
        return a

    def test_program_waits_for_a_slower_preview(self):
        a = self.run_feeds(preview_lag=0.08)
        self.assertAlmostEqual(a.offset, 0.08, delta=0.01)
        self.assertAlmostEqual(a.delay("main"), 0.08, delta=0.01)
        self.assertEqual(a.delay("preview"), 0.0)

    def test_preview_waits_for_a_slower_program(self):
        a = self.run_feeds(preview_lag=-0.12)
        self.assertAlmostEqual(a.delay("preview"), 0.12, delta=0.01)
        self.assertEqual(a.delay("main"), 0.0)


class ForwarderTests(unittest.TestCase):
    def test_send_never_blocks_on_an_unresolvable_target(self):
        import time
        from srtvast.transport import UdpForwarder
        fwd = UdpForwarder("no-such-host.invalid:5200")
        started = time.monotonic()
        for _ in range(200):
            fwd.send(b"\x47" * 188)
        self.assertLess(time.monotonic() - started, 0.1)
        fwd.sock.close()


class EventDetailTests(unittest.TestCase):
    def test_detail_travels_with_the_event(self):
        from srtvast import event_dict
        rec = logging.LogRecord("stitcher", logging.INFO, __file__, 1, "VAST response %s", ("ok",), None)
        rec.detail = {"kind": "vast", "xml": "<VAST/>"}
        self.assertEqual(event_dict(rec)["detail"], {"kind": "vast", "xml": "<VAST/>"})
        plain = logging.LogRecord("stitcher", logging.INFO, __file__, 1, "x", (), None)
        self.assertNotIn("detail", event_dict(plain))
