"""Bounded fragmented-MP4 broadcast for authenticated MediaSource clients."""
from collections import deque
import threading
import time


class LiveBuffer:
    MAX_BOX_BYTES = 4 * 1024 * 1024
    MAX_CACHE_BYTES = 8 * 1024 * 1024
    MAX_CACHE_SECONDS = 12

    def __init__(self):
        self.condition = threading.Condition()
        self.initialization = b""
        self.mime = None
        self.fragments = deque()
        self.bytes = 0
        self.sequence = 0
        self.closed = False
        self.failed = False
        self.clients = threading.BoundedSemaphore(8)

    @staticmethod
    def _read_exact(stream, count):
        result = bytearray()
        while len(result) < count:
            chunk = stream.read(count - len(result))
            if not chunk:
                if result:
                    raise ValueError("Incomplete MP4 box")
                return None
            result.extend(chunk)
        return bytes(result)

    def read(self, stream):
        initialization, fragment = b"", b""
        try:
            while True:
                header = self._read_exact(stream, 8)
                if header is None:
                    break
                size, kind = int.from_bytes(header[:4], "big"), header[4:]
                if not 8 <= size <= self.MAX_BOX_BYTES:
                    raise ValueError("Invalid MP4 box size")
                payload = self._read_exact(stream, size - 8) if size > 8 else b""
                if payload is None:
                    raise ValueError("Incomplete MP4 box")
                box = header + payload
                if kind in (b"ftyp", b"moov"):
                    initialization += box
                    if len(initialization) > self.MAX_BOX_BYTES:
                        raise ValueError("MP4 initialization is too large")
                    if kind == b"moov":
                        offset = box.find(b"avcC")
                        if offset < 4 or offset + 8 > len(box):
                            raise ValueError("Live video requires H.264")
                        codec = "avc1." + box[offset + 5:offset + 8].hex()
                        if b"mp4a" in box:
                            codec += ",mp4a.40.2"
                        with self.condition:
                            self.initialization = initialization
                            self.mime = f'video/mp4; codecs="{codec}"'
                            self.condition.notify_all()
                elif kind == b"moof":
                    fragment = box
                elif kind == b"mdat" and fragment:
                    self.publish(fragment + box)
                    fragment = b""
        except (OSError, ValueError):
            self.failed = True
        finally:
            self.close()
            stream.close()

    def publish(self, fragment):
        now = time.monotonic()
        with self.condition:
            if self.closed:
                return
            self.sequence += 1
            self.fragments.append((self.sequence, now, fragment))
            self.bytes += len(fragment)
            while self.fragments and (self.bytes > self.MAX_CACHE_BYTES or now - self.fragments[0][1] > self.MAX_CACHE_SECONDS):
                self.bytes -= len(self.fragments.popleft()[2])
            self.condition.notify_all()

    def chunks(self):
        with self.condition:
            if not self.initialization or not self.fragments:
                raise ValueError("Live video is not ready")
            initialization = self.initialization
            sequence = self.fragments[0][0] - 1
        yield initialization
        while True:
            with self.condition:
                if self.closed:
                    return
                if self.fragments and sequence < self.fragments[0][0] - 1:
                    raise ValueError("Live client fell behind")
                available = [(number, data) for number, _, data in self.fragments if number > sequence]
                if not available:
                    self.condition.wait(timeout=1)
            if not available:
                yield b""
            for number, data in available:
                sequence = number
                yield data

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
