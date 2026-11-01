import io
import threading
import unittest

from yoosee_cam_utils.live import LiveBuffer


def box(kind, payload=b""):
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


class LiveTests(unittest.TestCase):
    def test_mp4_parser_preserves_initialization_and_complete_fragments(self):
        initialization = box(b"ftyp", b"isom") + box(b"moov", box(b"avcC", b"\x01\x64\x00\x28") + box(b"mp4a"))
        fragment = box(b"moof", b"metadata") + box(b"mdat", b"encoded video")
        live = LiveBuffer()
        live.read(io.BytesIO(initialization + fragment))
        self.assertEqual(live.initialization, initialization)
        self.assertEqual(live.mime, 'video/mp4; codecs="avc1.640028,mp4a.40.2"')
        self.assertEqual(live.fragments[0][2], fragment)
        self.assertFalse(live.failed)

    def test_truncated_and_oversized_boxes_fail_with_bounded_memory(self):
        for content in (b"partial", box(b"mdat", b"payload")[:-1], (LiveBuffer.MAX_BOX_BYTES + 1).to_bytes(4, "big") + b"mdat"):
            with self.subTest(content=content[:8]):
                live = LiveBuffer()
                live.read(io.BytesIO(content))
                self.assertTrue(live.failed)
                self.assertTrue(live.closed)
                self.assertFalse(live.fragments)

    def test_slow_client_disconnects_instead_of_growing_backlog(self):
        live = LiveBuffer()
        live.MAX_CACHE_BYTES = 6
        live.initialization = b"init"
        live.publish(b"aaa")
        chunks = live.chunks()
        self.assertEqual(next(chunks), b"init")
        self.assertEqual(next(chunks), b"aaa")
        live.publish(b"bbb")
        live.publish(b"ccc")
        live.publish(b"ddd")
        self.assertLessEqual(live.bytes, 6)
        with self.assertRaisesRegex(ValueError, "fell behind"):
            next(chunks)

    def test_subscribers_do_not_consume_each_others_frames(self):
        live = LiveBuffer()
        live.initialization = b"init"
        live.publish(b"frame")
        first, second = live.chunks(), live.chunks()
        for subscriber in (first, second):
            self.assertEqual(next(subscriber), b"init")
            self.assertEqual(next(subscriber), b"frame")
        live.close()
        for subscriber in (first, second):
            with self.assertRaises(StopIteration):
                next(subscriber)

    def test_close_wakes_waiting_subscriber(self):
        live = LiveBuffer()
        live.initialization = b"init"
        live.publish(b"frame")
        subscriber = live.chunks()
        next(subscriber)
        next(subscriber)
        finished = threading.Event()
        def wait():
            list(subscriber)
            finished.set()
        worker = threading.Thread(target=wait)
        worker.start()
        live.close()
        worker.join(timeout=1)
        self.assertTrue(finished.is_set())


if __name__ == "__main__":
    unittest.main()
