import hashlib
import ipaddress
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
import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from yoosee_cam_utils.web import make_server, Streams, CameraRegistry, Handler
from yoosee_cam_utils.web import SessionStore


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
        self.web = dict(bind="127.0.0.1", port=0, allowed_networks=["127.0.0.0/8"], auth_database=str(self.root / f"viewer-users-{uuid.uuid4().hex}.sqlite3"), tls_key=str(self.key), tls_cert=str(self.cert), hls_js=str(asset), hls_js_sha256=hashlib.sha256(asset.read_bytes()).hexdigest())
        camera = Mock(c={"username": "admin", "password": "test-only-password"})
        self.streams = Mock()
        self.streams.profiles.return_value = [{"token": "profile0"}]
        self.streams.status.return_value = {"active": False, "profile": None, "playlist": None}
        self.streams.ptz.return_value = {"stop_confirmed": True}
        self.server = make_server(camera, self.web, self.streams)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.context = ssl.create_default_context(cafile=str(self.cert))
        self.test_sessions = {}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, path, authorized=False, data=None, extra=None, username="admin", password="test-only-password"):
        headers = dict(extra or {})
        session_key = (username, password)
        if authorized and session_key not in self.test_sessions:
            self._login(username, password)
        if authorized:
            token, csrf = self.test_sessions[session_key]
            headers.setdefault("Cookie", f"watchpost_session={token}")
            headers.setdefault("X-CSRF-Token", csrf)
            headers.setdefault("X-Camera-Client", "1")
            headers.setdefault("Origin", self.url)
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
        try:
            return urllib.request.urlopen(req, context=self.context, timeout=5)
        except urllib.error.HTTPError as error:
            if authorized and error.code == 401:
                error.close()
                self.test_sessions.pop(session_key, None)
                self._login(username, password)
                token, csrf = self.test_sessions[session_key]
                headers["Cookie"] = f"watchpost_session={token}"
                headers["X-CSRF-Token"] = csrf
                req = urllib.request.Request(self.url + path, headers=headers, data=json.dumps(data).encode() if data is not None else None)
                return urllib.request.urlopen(req, context=self.context, timeout=5)
            raise

    def _login(self, username, password):
        body = json.dumps({"username": username, "password": password}).encode()
        request = urllib.request.Request(self.url + "/api/auth/login", data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Camera-Client": "1", "Origin": self.url,
        })
        with urllib.request.urlopen(request, context=self.context, timeout=5) as response:
            result = json.load(response)
            cookie = response.headers.get("Set-Cookie", "")
            token = cookie.split("watchpost_session=", 1)[1].split(";", 1)[0]
            self.test_sessions[(username, password)] = (token, result["csrf_token"])

    def test_page_requires_server_auth(self):
        with self.request("/") as response:
            self.assertEqual(response.geturl().split("/")[-1], "login")
            self.assertIn("Sign in", response.read().decode())
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/api/cameras")
        self.assertEqual(e.exception.code, 401)
        e.exception.close()

    def test_public_assets_revalidate_without_password_hashing(self):
        for path in ("/app.js", "/style.css", "/vendor/hls.min.js"):
            with patch.object(self.server.users, "authenticate", side_effect=AssertionError("asset must not authenticate")):
                with self.request(path) as response:
                    self.assertEqual(response.status, 200)
                    self.assertTrue(response.read())
                    etag = response.headers["ETag"]
                    self.assertIn("public", response.headers["Cache-Control"])
                with self.assertRaises(urllib.error.HTTPError) as result:
                    self.request(path, extra={"If-None-Match": etag})
                self.assertEqual(result.exception.code, 304)
                self.assertEqual(result.exception.read(), b"")
                result.exception.close()
        # API content remains private and uncached.
        with self.assertRaises(urllib.error.HTTPError) as result:
            self.request("/api/cameras")
        self.assertEqual(result.exception.code, 401)
        self.assertEqual(result.exception.headers["Cache-Control"], "no-store")
        result.exception.close()

    def test_fake_frontend_cookie_does_not_bypass_auth(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/api/profiles", extra={"Cookie": "admin=true; authenticated=1; watchpost_session=fake"})
        self.assertEqual(e.exception.code, 401)
        e.exception.close()
        self.streams.profiles.assert_not_called()

    def test_authenticated_profiles(self):
        with self.request("/api/profiles", authorized=True) as r:
            self.assertEqual(json.load(r), [{"token": "profile0"}])

    def test_cookie_session_avoids_rehashing_password_on_requests(self):
        self._login("admin", "test-only-password")
        with patch.object(self.server.users, "authenticate", side_effect=AssertionError("password hash must run only at sign-in")):
            with self.request("/api/profiles", authorized=True) as response:
                self.assertEqual(json.load(response), [{"token": "profile0"}])

    def test_session_expiry_uses_server_idle_timeout(self):
        self._login("admin", "test-only-password")
        token, _ = self.test_sessions[("admin", "test-only-password")]
        with patch.object(SessionStore, "IDLE_SECONDS", 0):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/auth/status", extra={"Cookie": f"watchpost_session={token}"})
            self.assertEqual(error.exception.code, 401)
            error.exception.close()

    def test_watcher_capability_skips_session_timeouts(self):
        data={"username":"watcher-user","password":"watcher-session-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"],"capabilities":["watcher"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        self._login("watcher-user", "watcher-session-password")
        token, _ = self.test_sessions[("watcher-user", "watcher-session-password")]
        with patch.object(SessionStore, "IDLE_SECONDS", 0), patch.object(SessionStore, "ABSOLUTE_SECONDS", 0):
            with self.request("/api/auth/status", extra={"Cookie": f"watchpost_session={token}"}) as response:
                self.assertTrue(json.load(response)["watcher"])

    def test_session_store_bounds_memory_without_invalidating_existing_sessions(self):
        users = Mock()
        users.session_user.side_effect = lambda username, _address: {
            "username": username, "watcher": username.startswith("watcher")
        }
        sessions = SessionStore(users)
        sessions.MAX_SESSIONS = 2
        first = sessions.issue({"username": "alice"}, ipaddress.ip_address("127.0.0.1"))
        second = sessions.issue({"username": "bob"}, ipaddress.ip_address("127.0.0.1"))
        third = sessions.issue({"username": "carol"}, ipaddress.ip_address("127.0.0.1"))
        self.assertEqual(len(sessions.items), 2)
        self.assertIsNone(third)
        self.assertIsNotNone(sessions.get(first[0], ipaddress.ip_address("127.0.0.1")))
        self.assertIsNotNone(sessions.get(second[0], ipaddress.ip_address("127.0.0.1")))
        sessions.items.clear()
        watcher_one = sessions.issue({"username": "watcher-one"}, ipaddress.ip_address("127.0.0.1"))
        watcher_two = sessions.issue({"username": "watcher-two"}, ipaddress.ip_address("127.0.0.1"))
        watcher_three = sessions.issue({"username": "watcher-three"}, ipaddress.ip_address("127.0.0.1"))
        self.assertIsNotNone(watcher_one)
        self.assertIsNotNone(watcher_two)
        self.assertIsNone(watcher_three)
        self.assertEqual(len(sessions.items), 2)

    def test_forwarded_address_is_used_only_for_exact_trusted_proxy(self):
        handler = SimpleNamespace(
            client_address=("192.0.2.1", 1234),
            server=SimpleNamespace(trusted_proxies=[ipaddress.ip_network("192.0.2.1/32")]),
            headers={"X-Forwarded-For": "192.0.2.1"},
        )
        self.assertEqual(str(Handler.get_client_address(handler)), "192.0.2.1")
        handler.client_address = ("192.0.2.1", 1234)
        self.assertEqual(str(Handler.get_client_address(handler)), "192.0.2.1")
        handler.client_address = ("192.0.2.1", 1234)
        handler.headers = {"X-Forwarded-For": "192.0.2.1, 192.0.2.1"}
        with self.assertRaises(ValueError):
            Handler.get_client_address(handler)

    def test_session_cookie_rejects_non_ascii_token_without_collision(self):
        self.assertIsNone(self.server.sessions.get("abcé", ipaddress.ip_address("127.0.0.1")))

    def test_mutation_requires_csrf_token(self):
        self._login("admin", "test-only-password")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/ptz", authorized=True, data={"pan":.1}, extra={"X-CSRF-Token":"wrong"})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        self.streams.ptz.assert_not_called()

    def test_stream_status_reports_measured_source_fps(self):
        stream_dir = self.root / "measured-stream"
        stream_dir.mkdir()
        (stream_dir / "index.m3u8").write_text("#EXTM3U\nseg000000001.ts\n")
        (stream_dir / "seg000000001.ts").write_bytes(b"sample")
        result = subprocess.CompletedProcess([], 0, '{"streams":[{"width":1920,"height":1080,"avg_frame_rate":"15/1"}]}', "")
        with patch("yoosee_cam_utils.web.subprocess.run", return_value=result) as probe:
            measured = Streams._measure_latest_segment(stream_dir)
        self.assertEqual(measured, {"width": 1920, "height": 1080, "fps": 15.0})
        self.assertEqual(probe.call_args.kwargs["timeout"], 3)

    def test_source_fps_probe_failure_does_not_fail_status(self):
        stream_dir = self.root / "unavailable-stream"
        stream_dir.mkdir()
        (stream_dir / "index.m3u8").write_text("#EXTM3U\nseg000000001.ts\n")
        (stream_dir / "seg000000001.ts").write_bytes(b"sample")
        with patch("yoosee_cam_utils.web.subprocess.run", side_effect=subprocess.TimeoutExpired("ffprobe", 3)):
            self.assertIsNone(Streams._measure_latest_segment(stream_dir))

    def test_repeated_invalid_auth_is_rate_limited(self):
        for _ in range(12):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.request("/api/auth/login", data={"username":"admin","password":"wrong-password"}, extra={"Origin":self.url,"X-Camera-Client":"1"})
            self.assertEqual(e.exception.code, 401)
            e.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/api/auth/login", data={"username":"admin","password":"wrong-password"}, extra={"Origin":self.url,"X-Camera-Client":"1"})
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
        with self.server.users._session() as db:
            db.execute("DELETE FROM allowed_networks")
            db.execute("INSERT INTO allowed_networks(cidr) VALUES ('192.0.2.0/24')")
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.request("/", authorized=True)
        self.assertEqual(e.exception.code, 403)
        e.exception.close()

    def test_forwarded_address_header_cannot_bypass_source_allowlist(self):
        with self.server.users._session() as db:
            db.execute("DELETE FROM allowed_networks")
            db.execute("INSERT INTO allowed_networks(cidr) VALUES ('127.0.0.2/32')")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/", authorized=True, extra={"X-Forwarded-For":"127.0.0.2"})
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def add_second_camera(self):
        second = Mock()
        second.profiles.return_value = [{"token": "profile-other"}]
        second.status.return_value = {"active": True, "profile": "profile-other", "playlist": "/media/stream-other/index.m3u8"}
        second.start.return_value = second.status.return_value
        second.ptz.return_value = {"stop_confirmed": True}
        cameras = {"cam01": Mock(c={"name": "First"}), "cam02": Mock(c={"name": "Second"})}
        self.streams.status.return_value = {"active": False, "profile": None, "playlist": None}
        self.server.cameras = CameraRegistry(cameras, streams={"cam01": self.streams, "cam02": second})
        return second

    def test_scoped_camera_operations_select_only_requested_worker(self):
        second = self.add_second_camera()
        with self.request("/api/cameras/cam02/start", authorized=True, data={"profile": "profile-other"}, extra={"X-Camera-Client": "1"}) as response:
            self.assertEqual(json.load(response)["playlist"], "/media/cam02/stream-other/index.m3u8")
        second.start.assert_called_once_with("profile-other")
        self.streams.start.assert_not_called()
        with self.request("/api/cameras/cam02/stop", authorized=True, data={}, extra={"X-Camera-Client": "1"}) as response:
            self.assertFalse(json.load(response)["active"])
        second.stop.assert_called_once_with()
        self.streams.stop.assert_not_called()

    def test_camera_inventory_is_local_and_excludes_credentials(self):
        second = self.add_second_camera()
        with self.request("/api/cameras", authorized=True) as response:
            result = json.load(response)
        self.assertEqual([camera["id"] for camera in result], ["cam01", "cam02"])
        self.assertTrue(result[1]["active"])
        self.assertNotIn("playlist", result[1])
        self.assertNotIn("password", json.dumps(result))
        second.profiles.assert_not_called()
        self.streams.profiles.assert_not_called()

    def test_admin_can_create_scoped_viewer_without_exposing_hashes(self):
        data={"username":"viewer1","password":"a-long-test-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}) as response:
            self.assertTrue(json.load(response)["created"])
        with self.request("/api/admin/users",authorized=True) as response:
            users=json.load(response)
        created=next(user for user in users if user["username"]=="viewer1")
        self.assertEqual(created["role"],"viewer")
        self.assertEqual(created["cameras"],{"cam01":"viewer"})
        self.assertNotIn("password",json.dumps(users))
        self.assertNotIn("password_hash",json.dumps(users))
        with self.request("/api/cameras",authorized=True,username="viewer1",password="a-long-test-password") as response:
            self.assertEqual([item["id"] for item in json.load(response)],["cam01"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/cameras/cam01/start",authorized=True,data={"profile":"profile0"},extra={"X-Camera-Client":"1"},username="viewer1",password="a-long-test-password")
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_camera_admin_can_control_only_assigned_camera(self):
        self.add_second_camera()
        self.server.clips = Mock()
        self.server.clips.start.return_value = {"id":"a"*32,"state":"recording","max_seconds":3600}
        self.server.clips.stop.return_value = {"id":"a"*32,"state":"stopped"}
        data={"username":"camadmin","password":"another-long-test-password","role":"camera_admin","camera_permissions":{"cam02":"camera_admin"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        with self.request("/api/cameras/cam02/ptz",authorized=True,data={"pan":0.25},extra={"X-Camera-Client":"1"},username="camadmin",password="another-long-test-password") as response:
            self.assertTrue(json.load(response)["stop_confirmed"])
        self.server.clips.status.return_value={"camera_id":"cam02","state":"idle"}
        with self.request("/api/cameras/cam02/recording",authorized=True,username="camadmin",password="another-long-test-password") as response:
            self.assertEqual(json.load(response)["state"],"idle")
        self.server.clips.status.assert_called_once_with("cam02",None,"camadmin")
        with self.request("/api/cameras/cam02/recording?session_id="+"b"*32,authorized=True,username="camadmin",password="another-long-test-password") as response:
            self.assertEqual(json.load(response)["state"],"idle")
        self.assertEqual(self.server.clips.status.call_args_list[-1].args,("cam02","b"*32,"camadmin"))
        with self.assertRaises(urllib.error.HTTPError) as admin_error:
            self.request("/api/admin/users",authorized=True,username="camadmin",password="another-long-test-password")
        self.assertEqual(admin_error.exception.code,403)
        admin_error.exception.close()
        with self.request("/api/cameras/cam02/recording/start",authorized=True,data={"profile":"profile-other","max_seconds":3600},extra={"X-Camera-Client":"1"},username="camadmin",password="another-long-test-password") as response:
            self.assertEqual(response.status,202)
        self.server.clips.start.assert_called_once_with("cam02","profile-other",3600,"camadmin")
        with self.request("/api/cameras/cam02/recording/stop",authorized=True,data={"session_id":"a"*32},extra={"X-Camera-Client":"1"},username="camadmin",password="another-long-test-password") as response:
            self.assertEqual(json.load(response)["state"],"stopped")
        self.server.clips.stop.assert_called_once_with("cam02","a"*32,"camadmin")
        with self.assertRaises(urllib.error.HTTPError) as record_error:
            self.request("/api/cameras/cam01/recording/start",authorized=True,data={"profile":"profile0","max_seconds":3600},extra={"X-Camera-Client":"1"},username="camadmin",password="another-long-test-password")
        self.assertEqual(record_error.exception.code,403)
        record_error.exception.close()
        with self.request("/api/cameras",authorized=True,username="camadmin",password="another-long-test-password") as response:
            items=json.load(response)
        self.assertEqual([item["id"] for item in items],["cam02"])
        self.assertTrue(items[0]["can_control"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/cameras/cam01/ptz",authorized=True,data={"pan":0.25},extra={"X-Camera-Client":"1"},username="camadmin",password="another-long-test-password")
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_camera_viewer_is_not_shown_unassigned_cameras(self):
        self.add_second_camera()
        data={"username":"onecam","password":"viewer-access-password","role":"viewer","camera_permissions":{"cam02":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        with self.request("/api/cameras",authorized=True,username="onecam",password="viewer-access-password") as response:
            self.assertEqual([item["id"] for item in json.load(response)],["cam02"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/cameras/cam01/status",authorized=True,username="onecam",password="viewer-access-password")
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_viewer_can_play_and_record_own_clips(self):
        clip_id="a"*32
        clip_path=self.root/"authorized-clip.mp4";clip_path.write_bytes(b"private camera recording")
        self.server.clips=Mock()
        self.server.clips.list_clips.return_value=[{"id":clip_id,"camera_id":"cam01","trigger":"manual"}]
        self.server.clips.status.return_value={"id":clip_id,"camera_id":"cam01","state":"complete"}
        self.server.clips.clip_path.return_value=clip_path
        self.server.clips.clip_metadata.return_value={"owner":"clip-viewer","display_name":"cam01_Wed_20261029_193023_000001_FHD"}
        self.server.clips.start.return_value={"id":"c"*32,"state":"recording"}
        self.server.clips.stop.return_value={"id":"c"*32,"state":"stopped"}
        data={"username":"clip-viewer","password":"viewer-clip-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}): pass
        with self.request("/api/cameras/cam01/clips",authorized=True,username="clip-viewer",password="viewer-clip-password") as response:
            self.assertEqual(json.load(response)[0]["id"],clip_id)
        self.server.clips.list_clips.assert_called_once_with("cam01",include_hidden=False,hidden_only=False,since_seconds=None,limit=None)
        with self.request(f"/api/cameras/cam01/clips/{clip_id}",authorized=True,username="clip-viewer",password="viewer-clip-password") as response:
            self.assertEqual(json.load(response)["state"],"complete")
        self.server.clips.status.assert_called_once_with("cam01",clip_id,"clip-viewer")
        with self.request(f"/recordings/cam01/{clip_id}",authorized=True,username="clip-viewer",password="viewer-clip-password") as response:
            self.assertEqual(response.read(),b"private camera recording")
            self.assertEqual(response.headers["Content-Disposition"],'inline; filename="cam01_Wed_20261029_193023_000001_FHD.mp4"')
            self.assertEqual(response.headers["Accept-Ranges"],"bytes")
        with self.request(f"/recordings/cam01/{clip_id}",authorized=True,extra={"Range":"bytes=0-6"},username="clip-viewer",password="viewer-clip-password") as response:
            self.assertEqual(response.status,206)
            self.assertEqual(response.read(),b"private")
            self.assertEqual(response.headers["Content-Range"],"bytes 0-6/24")
        with self.request("/api/cameras/cam01/recording/start",authorized=True,data={"profile":"profile0","max_seconds":3600},extra={"X-Camera-Client":"1"},username="clip-viewer",password="viewer-clip-password") as response:
            self.assertEqual(json.load(response)["state"],"recording")
        self.server.clips.start.assert_called_once_with("cam01","profile0",3600,"clip-viewer")
        with self.request("/api/cameras/cam01/recording/stop",authorized=True,data={"session_id":"c"*32},extra={"X-Camera-Client":"1"},username="clip-viewer",password="viewer-clip-password") as response:
            self.assertEqual(json.load(response)["state"],"stopped")
        self.server.clips.stop.assert_called_once_with("cam01","c"*32,"clip-viewer")
        with self.request(f"/api/cameras/cam01/clips/{clip_id}/delete",authorized=True,data={},extra={"X-Camera-Client":"1"},username="clip-viewer",password="viewer-clip-password") as response:
            self.assertTrue(json.load(response)["deleted"])
        self.server.clips.delete_clip.assert_called_once_with("cam01",clip_id)

    def test_viewer_cannot_delete_another_users_clip(self):
        clip_id="a"*32
        self.server.clips=Mock()
        self.server.clips.clip_metadata.return_value={"owner":"another-user"}
        data={"username":"clip-viewer","password":"viewer-clip-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}): pass
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(f"/api/cameras/cam01/clips/{clip_id}/delete",authorized=True,data={},extra={"X-Camera-Client":"1"},username="clip-viewer",password="viewer-clip-password")
        self.assertEqual(error.exception.code,403)
        error.exception.close()
        self.server.clips.delete_clip.assert_not_called()

    def test_camera_admin_can_delete_assigned_clip_only(self):
        self.server.clips=Mock()
        data={"username":"clip-admin","password":"clip-admin-password","role":"camera_admin","camera_permissions":{"cam01":"camera_admin"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}): pass
        clip_id="b"*32
        self.server.clips.clip_metadata.return_value={"owner":"clip-owner"}
        with self.request(f"/api/cameras/cam01/clips/{clip_id}/delete",authorized=True,data={},extra={"X-Camera-Client":"1"},username="clip-admin",password="clip-admin-password") as response:
            self.assertTrue(json.load(response)["deleted"])
        self.server.clips.delete_clip.assert_called_once_with("cam01",clip_id)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(f"/api/cameras/cam02/clips/{clip_id}/delete",authorized=True,data={},extra={"X-Camera-Client":"1"},username="clip-admin",password="clip-admin-password")
        self.assertEqual(error.exception.code,404)
        error.exception.close()

    def test_camera_admin_can_hide_clip_for_assigned_camera(self):
        self.server.clips=Mock()
        data={"username":"clip-admin","password":"clip-admin-password","role":"camera_admin","camera_permissions":{"cam01":"camera_admin"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}): pass
        clip_id="d"*32
        with self.request(f"/api/media/clips/cam01/{clip_id}/hide",authorized=True,data={},extra={"X-Camera-Client":"1"},username="clip-admin",password="clip-admin-password") as response:
            self.assertTrue(json.load(response)["updated"])
        self.server.clips.set_hidden.assert_called_once_with("cam01",clip_id,True,"clip-admin")

    def test_user_source_ip_restriction_is_enforced(self):
        data={"username":"one-source","password":"source-policy-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.2/32"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/",authorized=True,username="one-source",password="source-policy-password")
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_web_admin_source_restriction_is_enforced(self):
        data={"username":"restricted-admin","password":"restricted-admin-password","role":"web_admin","camera_permissions":{},"networks":["127.0.0.2/32"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/admin/users",authorized=True,username="restricted-admin",password="restricted-admin-password")
        self.assertEqual(error.exception.code,403)
        error.exception.close()

    def test_admin_can_reset_password_without_regranting_camera_access(self):
        data={"username":"reset-user","password":"initial-user-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        update={"username":"reset-user","password":"replacement-user-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users/access",authorized=True,data=update,extra={"X-Camera-Client":"1"}) as response:
            self.assertTrue(json.load(response)["updated"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/cameras",authorized=True,username="reset-user",password="initial-user-password")
        self.assertEqual(error.exception.code,401)
        error.exception.close()
        with self.request("/api/cameras",authorized=True,username="reset-user",password="replacement-user-password") as response:
            self.assertEqual([camera["id"] for camera in json.load(response)], ["cam01"])

    def test_global_default_route_is_rejected_for_allowlist(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/admin/networks/add",authorized=True,data={"cidr":"0.0.0.0/0"},extra={"X-Camera-Client":"1"})
        self.assertEqual(error.exception.code,400)
        error.exception.close()

    def test_admin_can_disable_account_and_revocation_takes_effect_next_request(self):
        data={"username":"viewer2","password":"yet-another-long-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["127.0.0.0/8"]}
        with self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"}):
            pass
        with self.request("/api/admin/users/disable",authorized=True,data={"username":"viewer2"},extra={"X-Camera-Client":"1"}) as response:
            self.assertTrue(json.load(response)["updated"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/",authorized=True,username="viewer2",password="yet-another-long-password")
        self.assertEqual(error.exception.code,401)
        error.exception.close()

    def test_user_network_must_be_within_global_allowlist_and_cannot_lock_out_admin(self):
        data={"username":"outside","password":"a-different-long-password","role":"viewer","camera_permissions":{"cam01":"viewer"},"networks":["192.0.2.5/32"]}
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/admin/users",authorized=True,data=data,extra={"X-Camera-Client":"1"})
        self.assertEqual(error.exception.code,400)
        error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/admin/networks/remove",authorized=True,data={"cidr":"127.0.0.0/8"},extra={"X-Camera-Client":"1"})
        self.assertEqual(error.exception.code,409)
        self.assertIn("administrator source",json.load(error.exception)["error"])
        error.exception.close()

    def test_user_network_validation_accepts_mixed_ipv4_and_ipv6_allowlists(self):
        users = self.server.users
        with users._session() as db:
            db.execute("INSERT INTO allowed_networks(cidr) VALUES ('fd00::/64')")
        users.update_access("admin", roles=["web_admin"], camera_permissions={},
                            networks=["127.0.0.1/32", "fd00::1/128"], camera_ids=["cam01"])
        admin = next(user for user in users.inventory() if user["username"] == "admin")
        self.assertEqual(set(admin["networks"]), {"127.0.0.1/32", "fd00::1/128"})

    def test_live_connection_rechecks_account_and_source_revocation(self):
        users = self.server.users
        users.create_user("liveviewer", "long-test-password", roles=["viewer"],
                          camera_permissions={"01": "viewer"}, networks="127.0.0.1, 127.0.0.2",
                          camera_ids=["cam01"])
        address = ipaddress.ip_address("127.0.0.1")
        self.assertTrue(users.can_continue_viewing("liveviewer", address, "cam01"))
        self.assertFalse(users.can_continue_viewing("liveviewer", address, "cam02"))
        users.set_enabled("liveviewer", False)
        self.assertFalse(users.can_continue_viewing("liveviewer", address, "cam01"))
        users.set_enabled("liveviewer", True)
        with users._session() as db:
            db.execute("DELETE FROM allowed_networks")
        self.assertFalse(users.can_continue_viewing("liveviewer", address, "cam01"))

    def test_user_database_has_restrictive_permissions(self):
        self.assertEqual(self.server.users.path.stat().st_mode & 0o777,0o600)

    def test_scoped_media_uses_only_requested_camera_and_requires_auth(self):
        second = self.add_second_camera()
        second.media.return_value = b"video segment"
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/media/cam02/stream-other/seg000000001.ts")
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        second.media.assert_not_called()
        with self.request("/media/cam02/stream-other/seg000000001.ts", authorized=True) as response:
            self.assertEqual(response.read(), b"video segment")
        second.media.assert_called_once_with("stream-other", "seg000000001.ts")
        self.streams.media.assert_not_called()

    def test_unknown_camera_is_rejected_without_dispatch(self):
        second = self.add_second_camera()
        for path, data in (("/api/cameras/unknown/status", None), ("/api/cameras/unknown/start", {"profile": "profile0"}), ("/media/unknown/stream/seg000000001.ts", None)):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(path, authorized=True, data=data, extra={"X-Camera-Client": "1"})
            self.assertEqual(error.exception.code, 404)
            error.exception.close()
        second.start.assert_not_called()
        self.streams.start.assert_not_called()

    def test_failure_is_reported_only_for_affected_camera(self):
        second = self.add_second_camera()
        self.streams.profiles.side_effect = OSError("private internal transport detail")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/cameras/cam01/profiles", authorized=True)
        self.assertEqual(error.exception.code, 503)
        error.exception.close()
        with self.request("/api/cameras/cam02/profiles", authorized=True) as response:
            self.assertEqual(json.load(response), [{"token": "profile-other"}])
        with self.request("/api/cameras/cam01/status", authorized=True) as response:
            self.assertFalse(json.load(response)["active"])
        with self.request("/api/cameras", authorized=True) as response:
            result = json.load(response)
        self.assertEqual(result[0]["error"], "Camera operation failed")
        self.assertIsNone(result[1]["error"])
        self.assertNotIn("private internal", json.dumps(result))

    def test_slow_camera_rpc_does_not_block_other_camera_media(self):
        first = Streams(Mock(), self.root / "registry-first")
        second = Streams(Mock(), self.root / "registry-second")
        second.stream_dir = second.directory / "active"
        second.stream_dir.mkdir()
        (second.stream_dir / "seg000000001.ts").write_bytes(b"independent segment")
        registry = CameraRegistry({"cam01": first.camera, "cam02": second.camera}, streams={"cam01": first, "cam02": second})
        entered, release = threading.Event(), threading.Event()
        def blocked_profiles():
            entered.set()
            release.wait(3)
            return []
        first.camera.profiles.side_effect = blocked_profiles
        worker = threading.Thread(target=lambda: registry.invoke("cam01", "profiles"), daemon=True)
        worker.start()
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            self.assertEqual(registry.get("cam02").media("active", "seg000000001.ts"), b"independent segment")
            self.assertFalse(registry.invoke("cam02", "status")["active"])
            self.assertLess(time.monotonic() - started, .25)
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_registry_shutdown_attempts_other_workers_after_failure(self):
        first, second = Mock(), Mock()
        first.stop.side_effect = OSError("cleanup failure")
        registry = CameraRegistry({"cam01": Mock(), "cam02": Mock()}, streams={"cam01": first, "cam02": second})
        with self.assertRaisesRegex(OSError, "Camera worker cleanup failed"):
            registry.stop_all()
        second.stop.assert_called_once_with()

    def test_registry_rejects_unsafe_or_unmatched_camera_ids(self):
        for camera_id in ("../cam", "", "cam/name", "cam%2Fname"):
            with self.assertRaises(ValueError):
                CameraRegistry({camera_id: Mock()}, streams={camera_id: Mock()}, default_camera=camera_id)
        with self.assertRaises(ValueError):
            CameraRegistry({"cam01": Mock()}, streams={"other": Mock()})

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
