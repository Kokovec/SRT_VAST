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
