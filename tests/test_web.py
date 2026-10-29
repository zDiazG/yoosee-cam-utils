import base64
import hashlib
import json
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from unittest.mock import Mock

from yoosee_cam_utils.web import make_server, Streams


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.key, cls.cert = cls.root / "key.pem", cls.root / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(cls.key), "-out", str(cls.cert)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        cls.key.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        asset = self.root / "player.js"
        asset.write_text("test-only player")
        self.web = dict(bind="127.0.0.1", port=0, allowed_networks=["127.0.0.0/8"], tls_key=str(self.key), tls_cert=str(self.cert), hls_js=str(asset), hls_js_sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
        camera = Mock(c={"username": "admin", "password": "test-only-password"})
        self.streams = Mock()
        self.streams.profiles.return_value = [{"token": "profile0"}]
        self.streams.ptz.return_value = {"stop_confirmed": True}
        self.server = make_server(camera, self.web, self.streams)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.context = ssl.create_default_context(cafile=str(self.cert))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, authorized=False, data=None, extra=None):
        headers = extra or {}
        if authorized:
            headers["Authorization"] = "Basic " + base64.b64encode(b"admin:test-only-password").decode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
        return urllib.request.urlopen(req, context=self.context, timeout=5)

    def test_page_requires_server_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/")
        self.assertEqual(e.exception.code, 401)
        e.exception.close()

    def test_fake_frontend_cookie_does_not_bypass_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/api/profiles", extra={"Cookie": "admin=true; authenticated=1"})
        self.assertEqual(e.exception.code, 401)
        e.exception.close()
        self.streams.profiles.assert_not_called()

    def test_authenticated_profiles(self):
        with self.request("/api/profiles", authorized=True) as r:
            self.assertEqual(json.load(r), [{"token": "profile0"}])

    def test_repeated_invalid_auth_is_rate_limited(self):
        for _ in range(12):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request("/")
            self.assertEqual(e.exception.code, 401)
            e.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/")
        self.assertEqual(e.exception.code, 429)
        e.exception.close()

    def test_post_rejects_cross_origin(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/api/ptz", authorized=True, data={"pan": .1}, extra={"Origin": "https://example.test", "X-Camera-Client": "1"})
        self.assertEqual(e.exception.code, 403)
        e.exception.close()
        self.streams.ptz.assert_not_called()

    def test_authenticated_ptz_post(self):
        with self.request("/api/ptz", authorized=True, data={"pan": .1}, extra={"X-Camera-Client": "1"}) as r:
            self.assertTrue(json.load(r)["stop_confirmed"])
        self.streams.ptz.assert_called_once()

    def test_media_requires_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/media/id/seg000000001.ts")
        self.assertEqual(e.exception.code, 401)
        e.exception.close()
        self.streams.media.assert_not_called()

    def test_allowlist_rejects_other_network(self):
        import ipaddress
        self.server.networks = [ipaddress.ip_network("192.0.2.0/24")]
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/", authorized=True)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()

    def test_actual_media_rejects_log_and_traversal(self):
        streams = Streams(Mock(), self.root / "cache")
        streams.stream_dir = streams.directory / "sample"
        streams.stream_dir.mkdir()
        for filename in ("../worker.log", "worker.log", "../../config"):
            with self.assertRaises(FileNotFoundError):
                streams.media("sample", filename)

    def test_media_reads_continue_while_ptz_camera_call_waits(self):
        camera = Mock()
        streams = Streams(camera, self.root / "ptz-cache")
        streams.profile = "profile0"
        streams.stream_dir = streams.directory / "active"
        streams.stream_dir.mkdir()
        (streams.stream_dir / "seg000000001.ts").write_bytes(b"sample segment")
        entered = threading.Event()
        release = threading.Event()

        def blocked_stop(_token):
            entered.set()
            release.wait(3)

        camera.stop.side_effect = blocked_stop
        worker = threading.Thread(target=lambda: streams.ptz(stop=True), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        payload = streams.media("active", "seg000000001.ts")
        elapsed = time.monotonic() - started
        release.set()
        worker.join(1)
        self.assertEqual(payload, b"sample segment")
        self.assertLess(elapsed, 0.25)
        self.assertFalse(worker.is_alive())

    def test_media_reads_continue_while_profile_lookup_waits(self):
        camera = Mock(c={"username": "admin", "password": "test-only-password"})
        streams = Streams(camera, self.root / "startup-cache")
        streams.stream_dir = streams.directory / "previous"
        streams.stream_dir.mkdir()
        (streams.stream_dir / "index.m3u8").write_text("#EXTM3U\n")
        entered = threading.Event()
        release = threading.Event()

        def blocked_profiles():
            entered.set()
            release.wait(3)
            return [{"token": "other"}]

        camera.profiles.side_effect = blocked_profiles
        result = []

        def start_invalid_profile():
            try:
                streams.start("profile0")
            except ValueError as exc:
                result.append(str(exc))

        worker = threading.Thread(target=start_invalid_profile, daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        payload = streams.media("previous", "index.m3u8")
        elapsed = time.monotonic() - started
        release.set()
        worker.join(1)
        self.assertEqual(payload, b"#EXTM3U\n")
        self.assertLess(elapsed, 0.25)
        self.assertEqual(result, ["unknown profile"])
        self.assertFalse(worker.is_alive())

    def test_ptz_request_is_well_formed_and_always_stops(self):
        camera = Mock()
        streams = Streams(camera, self.root / "ptz-xml-cache")
        streams.profile = "profile0"

        streams.ptz(pan=0.25, duration=0.2)

        operation, namespace, action, body = camera.call.call_args.args
        self.assertEqual((operation, action), ("ptz", "ContinuousMove"))
        ET.fromstring(f'<root xmlns:m="{namespace}" xmlns:p="{namespace}" xmlns:t="http://www.onvif.org/ver10/schema">{body}</root>')
        self.assertTrue(camera.call.call_args.kwargs["addressing"])
        self.assertEqual(camera.call.call_args.kwargs["prefix"], "p")
        self.assertIn('<p:Velocity><t:PanTilt x="0.25" y="0"/><t:Zoom x="0"/></p:Velocity>', body)
        self.assertIn("<p:Timeout>PT0.2S</p:Timeout>", body)
        camera.stop.assert_called_once_with("profile0")

    def test_ptz_stops_when_addressed_move_fails(self):
        camera = Mock()
        streams = Streams(camera, self.root / "ptz-failure-cache")
        streams.profile = "profile0"
        camera.call.side_effect = OSError("transport failure")

        with self.assertRaisesRegex(OSError, "transport failure"):
            streams.ptz(pan=0.25, duration=0.2)

        camera.stop.assert_called_once_with("profile0")

    def test_ptz_rejects_duration_outside_ui_range(self):
        camera = Mock()
        streams = Streams(camera, self.root / "ptz-duration-cache")
        streams.profile = "profile0"

        with self.assertRaisesRegex(ValueError, "PTZ limits exceeded"):
            streams.ptz(pan=0.25, duration=0.1)

        camera.call.assert_not_called()
        camera.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
