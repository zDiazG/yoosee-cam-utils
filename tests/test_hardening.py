from contextlib import closing
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import sqlite3
import ssl
import stat
import subprocess
import tempfile
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import Mock, patch

from yoosee_cam_utils.accounts import UserStore
from yoosee_cam_utils.cli import Camera, CameraAuthError
from yoosee_cam_utils.web import (CameraRegistry, Handler, SessionStore, apply_credential_overrides,
                                  make_server, reload_config)

from tests.fakes import FakeOnvifCamera

PW = "test-only-password"
CAMS = ("cam01", "cam02", "cam03")
LOOPBACK = ipaddress.ip_address("127.0.0.1")


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.key, cls.cert = cls.root / "key.pem", cls.root / "cert.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(cls.key), "-out", str(cls.cert)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        cls.key.chmod(0o600)
        cls.asset = cls.root / "player.js"
        cls.asset.write_text("test-only player")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def web_config(self, **extra):
        web = dict(bind="127.0.0.1", port=0, allowed_networks=["127.0.0.0/8"],
                   auth_database=str(self.root / f"users-{uuid.uuid4().hex}.sqlite3"),
                   tls_key=str(self.key), tls_cert=str(self.cert), hls_js=str(self.asset),
                   hls_js_sha256=hashlib.sha256(self.asset.read_bytes()).hexdigest())
        web.update(extra)
        return web

    @staticmethod
    def mock_streams():
        streams = Mock()
        streams.profiles.return_value = [{"token": "profile0"}]
        streams.status.return_value = {"active": False, "profile": None, "playlist": None}
        streams.start.return_value = {"active": True, "profile": "profile0", "playlist": None}
        streams.ptz.return_value = {"stop_confirmed": True}
        return streams

    def start(self, cameras=None, streams=None, bootstrap=True, **web_extra):
        cameras = cameras or {"cam01": Mock(c={"username": "admin", "password": PW})}
        streams = streams or {name: self.mock_streams() for name in cameras}
        self.web = self.web_config(**web_extra)
        self.streams = streams
        self.server = make_server(cameras, self.web, streams)
        if bootstrap:
            self.server.users.bootstrap_admin("admin", PW)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.url = f"https://127.0.0.1:{self.server.server_address[1]}"
        self.context = ssl.create_default_context(cafile=str(self.cert))
        self.sessions = {}
        return self.server

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def post_login(self, username, password, extra=None):
        headers = {"Content-Type": "application/json", "X-Camera-Client": "1", "Origin": self.url, **(extra or {})}
        request = urllib.request.Request(self.url + "/api/auth/login", method="POST", headers=headers,
                                         data=json.dumps({"username": username, "password": password}).encode())
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=5) as response:
                return response.status, json.load(response), response.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None, ""

    def login(self, username, password):
        status, body, cookie = self.post_login(username, password)
        self.assertEqual(status, 200)
        token = cookie.split("watchpost_session=", 1)[1].split(";", 1)[0]
        self.sessions[username] = (token, body["csrf_token"])
        return token

    def call(self, username, path, data=None, password=PW):
        if username not in self.sessions:
            self.login(username, password)
        token, csrf = self.sessions[username]
        headers = {"Cookie": f"watchpost_session={token}", "X-CSRF-Token": csrf, "X-Camera-Client": "1", "Origin": self.url}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, headers=headers,
                                         data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=8) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            error.close()
            return error.code, None


class ConnectionHardeningTests(ServerCase):
    def test_idle_sockets_do_not_stall_accept_thread(self):
        self.start()
        idle = [socket.create_connection(("127.0.0.1", self.server.server_address[1])) for _ in range(20)]
        self.addCleanup(lambda: [s.close() for s in idle])
        time.sleep(0.3)
        started = time.monotonic()
        with urllib.request.urlopen(self.url + "/login", context=self.context, timeout=5) as response:
            self.assertEqual(response.status, 200)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_peer_outside_allowlist_closed_before_tls(self):
        self.start()
        with self.server.users._session() as db:
            db.execute("DELETE FROM allowed_networks")
            db.execute("INSERT INTO allowed_networks(cidr,source) VALUES ('198.19.99.0/24','admin')")
        created = []
        original = Handler.setup
        def spy(handler):
            created.append(handler.client_address)
            return original(handler)
        with patch.object(Handler, "setup", spy):
            with self.assertRaises((urllib.error.URLError, ssl.SSLError, ConnectionError)):
                urllib.request.urlopen(self.url + "/login", context=self.context, timeout=5)
            time.sleep(0.2)
        self.assertEqual(created, [])

    def test_trusted_proxy_peer_is_not_filtered_by_allowlist(self):
        self.start(allowed_networks=["198.18.0.0/15"], trusted_proxies=["127.0.0.0/8"])
        with urllib.request.urlopen(self.url + "/login", context=self.context, timeout=5) as response:
            self.assertEqual(response.status, 200)


class BootstrapTests(ServerCase):
    def two_cameras(self):
        return {"cam01": Mock(c={"username": "first-user", "password": "first-password-1"}),
                "cam02": Mock(c={"username": "second-user", "password": "second-password-2"})}

    def test_bootstrap_with_second_camera_credentials_then_closed(self):
        self.start(self.two_cameras(), bootstrap=False)
        self.assertTrue(self.server.users.needs_bootstrap())
        self.assertEqual(self.post_login("nobody", "wrong-password-xx")[0], 401)
        self.assertEqual(self.post_login("first-user", "second-password-2")[0], 401)
        self.assertTrue(self.server.users.needs_bootstrap())
        self.assertEqual(self.post_login("second-user", "second-password-2")[0], 200)
        self.streams["cam02"].profiles.assert_called()
        users = self.server.users.inventory()
        self.assertEqual([(u["username"], u["role"], u["bootstrap"]) for u in users], [("second-user", "web_admin", True)])
        # Bootstrap is closed: the other camera's pair no longer creates anything.
        self.assertEqual(self.post_login("first-user", "first-password-1")[0], 401)
        self.assertEqual(len(self.server.users.inventory()), 1)

    def test_bootstrap_requires_reachable_camera(self):
        self.start(self.two_cameras(), bootstrap=False)
        self.streams["cam02"].profiles.side_effect = OSError("camera offline")
        self.assertEqual(self.post_login("second-user", "second-password-2")[0], 401)
        self.assertTrue(self.server.users.needs_bootstrap())
        self.assertEqual(self.server.users.inventory(), [])


class NetworkOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "users.sqlite3"

    def test_config_networks_reconcile_and_admin_rows_survive(self):
        store = UserStore(self.path, ["203.0.113.0/24"])
        store.add_networks("198.19.5.0/24")
        self.assertEqual(store.list_networks(detail=True),
                         [{"cidr": "198.19.5.0/24", "source": "admin"}, {"cidr": "203.0.113.0/24", "source": "config"}])
        store.reconcile_config_networks(["203.0.113.0/24", "198.51.100.0/24"])
        self.assertEqual({r["cidr"]: r["source"] for r in store.list_networks(detail=True)},
                         {"198.19.5.0/24": "admin", "203.0.113.0/24": "config", "198.51.100.0/24": "config"})
        store.reconcile_config_networks(["198.51.100.0/24"])
        self.assertEqual({r["cidr"]: r["source"] for r in store.list_networks(detail=True)},
                         {"198.19.5.0/24": "admin", "198.51.100.0/24": "config"})
        with self.assertRaises(ValueError):
            store.remove_network("198.51.100.0/24", None)
        store.remove_network("198.19.5.0/24", None)
        self.assertEqual(store.list_networks(), ["198.51.100.0/24"])

    def test_account_without_own_networks_follows_config_cidr(self):
        store = UserStore(self.path, ["203.0.113.0/24"])
        store.create_user("root-admin", "long-enough-password", roles=["web_admin"])
        new_client = ipaddress.ip_address("198.51.100.5")
        self.assertFalse(store.authenticate("root-admin", "long-enough-password", new_client)["source_allowed"])
        store.reconcile_config_networks(["203.0.113.0/24", "198.51.100.0/24"])
        result = store.authenticate("root-admin", "long-enough-password", new_client)
        self.assertTrue(result["source_allowed"])
        self.assertIsNotNone(store.session_user("root-admin", new_client))

    def build_legacy(self):
        db = sqlite3.connect(self.path)
        db.executescript("""
            CREATE TABLE users (username TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('web_admin','camera_admin','viewer')), enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE user_cameras (username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                camera_id TEXT NOT NULL, permission TEXT NOT NULL CHECK(permission IN ('viewer','camera_admin')),
                PRIMARY KEY(username,camera_id));
            CREATE TABLE user_networks (username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                cidr TEXT NOT NULL, PRIMARY KEY(username,cidr));
            CREATE TABLE allowed_networks (cidr TEXT PRIMARY KEY);
            CREATE TABLE user_roles (username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                role TEXT NOT NULL, PRIMARY KEY(username,role));
            CREATE TABLE user_capabilities (username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                capability TEXT NOT NULL CHECK(capability IN ('watcher')), PRIMARY KEY(username,capability));
        """)
        salt = b"s" * 16
        for name, role in (("boss", "web_admin"), ("kiosk", "viewer"), ("limited", "viewer")):
            db.execute("INSERT INTO users VALUES (?,?,?,?,1)", (name, salt, UserStore._hash("legacy-password-1", salt), role))
            db.execute("INSERT INTO user_roles VALUES (?,?)", (name, role))
        db.execute("INSERT INTO user_cameras VALUES ('kiosk','cam01','viewer')")
        db.execute("INSERT INTO user_cameras VALUES ('limited','cam01','viewer')")
        db.execute("INSERT INTO user_capabilities VALUES ('kiosk','watcher')")
        for cidr in ("203.0.113.0/24", "198.18.0.0/15"):
            db.execute("INSERT INTO user_networks VALUES ('boss',?)", (cidr,))
            db.execute("INSERT INTO allowed_networks VALUES (?)", (cidr,))
        db.execute("INSERT INTO allowed_networks VALUES ('198.51.100.0/24')")
        db.execute("INSERT INTO user_networks VALUES ('limited','203.0.113.0/24')")
        db.commit(); db.close()
        self.path.chmod(0o600)

    def test_legacy_database_migrates_once(self):
        self.build_legacy()
        store = UserStore(self.path, ["203.0.113.0/24", "198.18.0.0/15"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIn("source", [r[1] for r in db.execute("PRAGMA table_info(allowed_networks)")])
        self.assertEqual({r["cidr"]: r["source"] for r in store.list_networks(detail=True)},
                         {"203.0.113.0/24": "config", "198.18.0.0/15": "config", "198.51.100.0/24": "admin"})
        inventory = {u["username"]: u for u in store.inventory()}
        self.assertEqual(inventory["boss"]["networks"], [])
        self.assertEqual(inventory["limited"]["networks"], ["203.0.113.0/24"])
        self.assertEqual(inventory["kiosk"]["capabilities"], ["record", "watcher"])
        self.assertEqual(inventory["boss"]["capabilities"], ["record"])
        self.assertEqual(inventory["kiosk"]["cameras"], {"cam01": "viewer"})
        self.assertEqual(inventory["boss"]["roles"], ["web_admin"])
        self.assertTrue(any("boss" in note for note in store.migration_notes))
        self.assertTrue(store.authenticate("boss", "legacy-password-1", ipaddress.ip_address("198.18.1.1"))["source_allowed"])
        before = store.inventory()
        again = UserStore(self.path, ["203.0.113.0/24", "198.18.0.0/15"])
        self.assertEqual(again.migration_notes, [])
        self.assertEqual(again.inventory(), before)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)


class CapabilityTests(unittest.TestCase):
    def test_record_permission_matrix(self):
        can = UserStore.can
        grants = {"cam01": "viewer"}
        self.assertTrue(can({"roles": ["viewer"], "cameras": grants, "capabilities": ["record"]}, "cam01", "record"))
        self.assertFalse(can({"roles": ["viewer"], "cameras": grants, "capabilities": []}, "cam01", "record"))
        self.assertFalse(can({"roles": ["viewer"], "cameras": grants, "capabilities": ["watcher"]}, "cam01", "record"))
        self.assertTrue(can({"roles": ["camera_admin"], "cameras": {"cam01": "camera_admin"}, "capabilities": []}, "cam01", "record"))
        self.assertTrue(can({"roles": ["web_admin"], "cameras": {}, "capabilities": []}, "cam09", "record"))
        # No grant on the camera means no recording, whatever the capability says.
        self.assertFalse(can({"roles": ["viewer"], "cameras": grants, "capabilities": ["record"]}, "cam02", "record"))

    def test_create_user_record_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = UserStore(Path(tmp) / "u.sqlite3", ["127.0.0.0/8"])
            perms = {"cam01": "viewer"}
            store.create_user("plain", "long-enough-password", roles=["viewer"], camera_permissions=perms, camera_ids=CAMS, record=None)
            store.create_user("watch", "long-enough-password", roles=["viewer"], camera_permissions=perms, camera_ids=CAMS,
                              capabilities=["watcher"], record=None)
            store.create_user("watchrec", "long-enough-password", roles=["viewer"], camera_permissions=perms, camera_ids=CAMS,
                              capabilities=["watcher"], record=True)
            store.create_user("norec", "long-enough-password", roles=["viewer"], camera_permissions=perms, camera_ids=CAMS, record=False)
            caps = {u["username"]: u["capabilities"] for u in store.inventory()}
            self.assertEqual(caps, {"plain": ["record"], "watch": ["watcher"], "watchrec": ["record", "watcher"], "norec": []})
            loop = ipaddress.ip_address("127.0.0.1")
            self.assertTrue(UserStore.can(store.session_user("plain", loop), "cam01", "record"))
            self.assertFalse(UserStore.can(store.session_user("watch", loop), "cam01", "record"))
            self.assertFalse(UserStore.can(store.session_user("norec", loop), "cam01", "record"))


class WatcherSessionTests(ServerCase):
    def setUp(self):
        self.start()
        self.server.users.create_user("kiosk", "kiosk-password-1", roles=["viewer"],
                                      camera_permissions={"cam01": "viewer"}, camera_ids=CAMS, capabilities=["watcher"])

    def restarted(self):
        return SessionStore(self.server.users)

    def test_watcher_session_survives_restart_but_normal_does_not(self):
        watcher = self.login("kiosk", "kiosk-password-1")
        admin = self.login("admin", PW)
        fresh = self.restarted()
        self.assertIsNotNone(fresh.get(watcher, LOOPBACK))
        self.assertIsNone(fresh.get(admin, LOOPBACK))
        self.assertIsNone(fresh.get(watcher, ipaddress.ip_address("127.0.0.2")))

    def test_third_watcher_login_evicts_oldest(self):
        tokens = [self.login("kiosk", "kiosk-password-1") for _ in range(3)]
        digests = [SessionStore.digest(t) for t in tokens]
        self.assertIsNone(self.server.sessions.get(tokens[0], LOOPBACK))
        self.assertIsNone(self.server.users.load_session(digests[0]))
        self.assertIsNotNone(self.server.users.load_session(digests[1]))
        self.assertIsNotNone(self.server.users.load_session(digests[2]))
        mine = [k for k, v in self.server.sessions.items.items() if v["username"] == "kiosk"]
        self.assertEqual(len(mine), 2)
        with self.server.users._session() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE username='kiosk'").fetchone()[0], 2)
        self.assertIsNone(self.restarted().get(tokens[0], LOOPBACK))
        self.assertIsNotNone(self.restarted().get(tokens[2], LOOPBACK))

    def test_password_change_and_disable_invalidate_persisted_sessions(self):
        token = self.login("kiosk", "kiosk-password-1")
        self.server.users.set_password("kiosk", "brand-new-password")
        self.assertIsNone(self.restarted().get(token, LOOPBACK))
        self.assertIsNone(self.server.sessions.get(token, LOOPBACK))
        token = self.login("kiosk", "brand-new-password")
        self.assertIsNotNone(self.restarted().get(token, LOOPBACK))
        self.server.users.set_enabled("kiosk", False)
        self.assertIsNone(self.restarted().get(token, LOOPBACK))
        self.server.users.set_enabled("kiosk", True)
        self.assertIsNone(self.restarted().get(token, LOOPBACK))

    def test_stale_session_version_is_rejected_and_row_removed(self):
        token = self.login("kiosk", "kiosk-password-1")
        with self.server.users._session() as db:
            db.execute("UPDATE users SET session_version=session_version+1 WHERE username='kiosk'")
        self.assertIsNone(self.restarted().get(token, LOOPBACK))
        self.assertIsNone(self.server.users.load_session(SessionStore.digest(token)))


class LoginHardeningTests(ServerCase):
    def test_unknown_user_still_pays_for_scrypt(self):
        self.start()
        original = UserStore._hash
        with patch.object(UserStore, "_hash", side_effect=original) as spy:
            self.assertIsNone(self.server.users.authenticate("ghost", "whatever-password", LOOPBACK))
            self.assertEqual(spy.call_count, 1)
            self.assertIsNone(self.server.users.authenticate("admin", "wrong-password-xx", LOOPBACK))
            self.assertEqual(spy.call_count, 2)

    def test_allowlist_checked_before_authentication(self):
        self.start(allowed_networks=["198.18.0.0/15"], trusted_proxies=["127.0.0.0/8"])
        with patch.object(self.server.users, "authenticate", side_effect=AssertionError("must not authenticate")):
            status, _, cookie = self.post_login("admin", PW, {"X-Forwarded-For": "8.8.8.8"})
        self.assertEqual((status, cookie), (403, ""))
        status, _, _ = self.post_login("admin", PW, {"X-Forwarded-For": "198.18.2.3"})
        self.assertEqual(status, 200)
        self.assertEqual(self.post_login("admin", PW, {"X-Forwarded-For": "198.18.2.3, 8.8.8.8"})[0], 400)


class CameraAuthFailureTests(ServerCase):
    def registry(self):
        cameras = {n: Mock(c={"username": "u", "password": "p"}) for n in ("cam01", "cam02", "cam03")}
        return CameraRegistry(cameras, streams={n: self.mock_streams() for n in cameras})

    def test_failure_episode_counts_once_and_clears_on_success(self):
        registry = self.registry()
        registry.workers["cam02"].start.side_effect = CameraAuthError("rejected")
        for _ in range(3):
            with self.assertRaises(CameraAuthError):
                registry.invoke("cam02", "start", "profile0")
        state = {item["id"]: item for item in registry.summary()}
        self.assertTrue(state["cam02"]["auth_failed"])
        self.assertEqual(state["cam02"]["auth_failure_id"], 1)
        for other in ("cam01", "cam03"):
            self.assertFalse(state[other]["auth_failed"])
            self.assertEqual(state[other]["auth_failure_id"], 0)
            self.assertIsNone(state[other]["error"])
        registry.workers["cam02"].start.side_effect = None
        registry.invoke("cam02", "start", "profile0")
        state = {item["id"]: item for item in registry.summary()}
        self.assertFalse(state["cam02"]["auth_failed"])
        self.assertIsNone(state["cam02"]["error"])
        registry.workers["cam02"].start.side_effect = CameraAuthError("rejected again")
        with self.assertRaises(CameraAuthError):
            registry.invoke("cam02", "start", "profile0")
        self.assertEqual({i["id"]: i["auth_failure_id"] for i in registry.summary()}["cam02"], 2)

    def test_credentials_endpoint(self):
        fake = FakeOnvifCamera(mode="ok")
        self.addCleanup(fake.close)
        cameras = {"cam01": Mock(c={"username": "old", "password": "old-password-1"}),
                   "cam02": Mock(c={"username": "old", "password": "old-password-2"})}
        self.start(cameras)
        self.server.users.create_user("viewer1", "viewer-password-1", roles=["viewer"], camera_permissions={"cam01": "viewer"}, camera_ids=CAMS)
        good = "correct-password-9"
        def verify(camera_id, username, password):
            fake.mode = "ok" if password == good else "auth_fault"
            return Camera(fake.camera_config(username, password)).profiles() is not None
        registry = self.server.cameras
        registry._mark_auth_failed("cam01")
        with patch.object(registry, "verify_credentials", side_effect=verify):
            self.assertEqual(self.call("viewer1", "/api/admin/cameras/credentials",
                                       {"camera_id": "cam01", "username": "n", "password": good}, "viewer-password-1")[0], 403)
            self.assertEqual(self.call("admin", "/api/admin/cameras/credentials",
                                       {"camera_id": "cam01", "username": "n", "password": "wrong-password-0"})[0], 409)
            self.assertEqual(registry.cameras["cam01"].c["password"], "old-password-1")
            self.assertTrue(registry.auth_failures["cam01"])
            status, body = self.call("admin", "/api/admin/cameras/credentials",
                                     {"camera_id": "cam01", "username": "newuser", "password": good})
        self.assertEqual((status, body), (200, {"updated": ["cam01"]}))
        self.assertEqual(registry.cameras["cam01"].c["username"], "newuser")
        self.assertEqual(registry.cameras["cam01"].c["password"], good)
        self.assertEqual(registry.cameras["cam02"].c["password"], "old-password-2")
        self.assertFalse(registry.auth_failures.get("cam01"))
        saved = Path(self.web["auth_database"]).with_name("camera-credentials.json")
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(json.loads(saved.read_text()), {"cam01": {"username": "newuser", "password": good}})
        # The next process start picks the saved override up over the env-file values.
        restarted_cameras = {"cam01": Mock(c={"username": "old", "password": "old-password-1"}),
                             "cam02": Mock(c={"username": "old", "password": "old-password-2"})}
        again = make_server(restarted_cameras, self.web, {n: self.mock_streams() for n in restarted_cameras})
        self.addCleanup(again.server_close)
        self.assertEqual(restarted_cameras["cam01"].c, {"username": "newuser", "password": good})
        self.assertEqual(restarted_cameras["cam02"].c["password"], "old-password-2")

    def test_apply_credential_overrides_ignores_unknown_cameras(self):
        store = Mock(load=Mock(return_value={"cam09": {"username": "x", "password": "y"}}))
        camera = Mock(c={"username": "a", "password": "b"})
        apply_credential_overrides({"cam01": camera}, store)
        self.assertEqual(camera.c, {"username": "a", "password": "b"})


class ReloadTests(ServerCase):
    def write_config(self, cameras, networks=("127.0.0.0/8",), env=None):
        env_file = self.root / f"{uuid.uuid4().hex}.env"
        env_file.write_text("".join(f"{k}={v}\n" for k, v in (env or {}).items()))
        env_file.chmod(0o600)
        lines = [f'credentials_file = "{env_file}"', "", "[web]",
                 "allowed_networks = [" + ", ".join(f'"{n}"' for n in networks) + "]",
                 f'cache_dir = "{self.root / "cache"}"', ""]
        for name, host in cameras.items():
            lines += [f"[cameras.{name}]", f'host = "{host}"', f'username_env = "{name.upper()}_U"',
                      f'password_env = "{name.upper()}_P"', ""]
        path = self.root / f"{uuid.uuid4().hex}.toml"
        path.write_text("\n".join(lines))
        return path

    def env(self, *names):
        result = {}
        for name in names:
            result[f"{name.upper()}_U"], result[f"{name.upper()}_P"] = f"user-{name}", f"pass-{name}-123456"
        return result

    def setUp(self):
        env = self.env("cam01", "cam02", "cam03")
        first = self.write_config({"cam01": "192.0.2.1", "cam02": "192.0.2.2"}, env=env)
        from yoosee_cam_utils.cli import load_config
        cameras = {n: Camera(load_config(first, n)) for n in ("cam01", "cam02")}
        self.web = self.web_config(cache_dir=str(self.root / "cache"))
        self.server = make_server(cameras, self.web)
        self.addCleanup(self.server.server_close)
        self.server.users.reconcile_config_networks(["127.0.0.0/8"])
        self.env_values = env

    def stop(self):  # no serving thread in these tests
        pass

    def test_reload_adds_removes_and_keeps_unchanged_worker(self):
        worker = self.server.cameras.workers["cam01"]
        path = self.write_config({"cam01": "192.0.2.1", "cam03": "192.0.2.3"}, ("127.0.0.0/8", "192.0.2.0/24"), self.env_values)
        added, removed = reload_config(self.server, path)
        self.assertEqual((added, removed), (["cam03"], ["cam02"]))
        self.assertEqual(sorted(self.server.cameras.cameras), ["cam01", "cam03"])
        self.assertIs(self.server.cameras.workers["cam01"], worker)
        self.assertEqual(self.server.cameras.cameras["cam03"].c["username"], "user-cam03")
        self.assertEqual({r["cidr"]: r["source"] for r in self.server.users.list_networks(detail=True)},
                         {"127.0.0.0/8": "config", "192.0.2.0/24": "config"})

    def test_invalid_reload_leaves_state_unchanged(self):
        snapshot = (dict(self.server.cameras.workers), self.server.users.list_networks())
        bad_toml = self.root / "bad.toml"
        bad_toml.write_text("this is [[ not toml")
        with self.assertRaises(tomllib.TOMLDecodeError):
            reload_config(self.server, bad_toml)
        missing_creds = self.write_config({"cam01": "192.0.2.1", "cam03": "192.0.2.3"}, env=self.env("cam01"))
        with self.assertRaises(ValueError):
            reload_config(self.server, missing_creds)
        bad_network = self.write_config({"cam01": "192.0.2.1"}, ("not-a-cidr",), self.env_values)
        with self.assertRaises(ValueError):
            reload_config(self.server, bad_network)
        self.assertEqual((dict(self.server.cameras.workers), self.server.users.list_networks()), snapshot)

    def test_camera_with_active_recording_is_not_removed(self):
        worker = self.server.cameras.workers["cam02"]
        with self.server.clips.lock:
            self.server.clips.sessions["s1"] = {"camera_id": "cam02", "state": "recording"}
        path = self.write_config({"cam01": "192.0.2.1"}, env=self.env_values)
        added, removed = reload_config(self.server, path)
        self.assertEqual((added, removed), ([], []))
        self.assertIs(self.server.cameras.workers["cam02"], worker)
        with self.server.clips.lock:
            self.server.clips.sessions["s1"]["state"] = "stopped"
        self.assertEqual(reload_config(self.server, path), ([], ["cam02"]))


class MultiCameraTests(ServerCase):
    def setUp(self):
        cameras = {n: Mock(c={"username": "admin", "password": PW}) for n in ("cam01", "cam02", "cam03")}
        self.start(cameras)
        users = self.server.users
        users.create_user("v02", "viewer-password-02", roles=["viewer"], camera_permissions={"cam02": "viewer"}, camera_ids=CAMS)
        users.create_user("ca03", "cadmin-password-03", roles=["camera_admin"], camera_permissions={"cam03": "camera_admin"}, camera_ids=CAMS)
        self.v02 = ("v02", "viewer-password-02")
        self.ca03 = ("ca03", "cadmin-password-03")

    def cameras_for(self, who):
        username, password = who
        status, body = self.call(username, "/api/cameras", password=password)
        self.assertEqual(status, 200)
        return {item["id"]: item for item in body}

    def test_viewer_only_sees_and_starts_granted_camera(self):
        visible = self.cameras_for(self.v02)
        self.assertEqual(list(visible), ["cam02"])
        self.assertTrue(visible["cam02"]["can_start"])
        self.assertFalse(visible["cam02"]["can_control"])
        self.assertEqual(self.call("v02", "/api/cameras/cam01/start", {"profile": "profile0"})[0], 403)
        self.assertEqual(self.call("v02", "/api/cameras/cam01/stop", {})[0], 403)
        self.assertEqual(self.call("v02", "/api/status")[0], 403)  # default camera is cam01
        self.streams["cam01"].start.assert_not_called()
        status, body = self.call("v02", "/api/cameras/cam02/start", {})
        self.assertEqual(status, 200)
        self.streams["cam02"].start.assert_called_once_with("profile0")
        self.assertEqual(self.call("v02", "/api/cameras/cam02/stop", {})[0], 403)

    def test_camera_admin_controls_only_own_camera(self):
        self.assertEqual(list(self.cameras_for(self.ca03)), ["cam03"])
        self.assertTrue(self.cameras_for(self.ca03)["cam03"]["can_control"])
        self.assertEqual(self.call("ca03", "/api/cameras/cam01/start", {"profile": "profile0"}, self.ca03[1])[0], 403)
        self.assertEqual(self.call("ca03", "/api/cameras/cam01/stop", {})[0], 403)
        self.assertEqual(self.call("ca03", "/api/cameras/cam03/stop", {})[0], 200)
        self.streams["cam03"].stop.assert_called_once()
        self.streams["cam01"].stop.assert_not_called()

    def test_web_admin_sees_all_and_auth_failure_is_scoped(self):
        self.server.cameras._mark_auth_failed("cam02")
        admin = self.cameras_for(("admin", PW))
        self.assertEqual(sorted(admin), ["cam01", "cam02", "cam03"])
        for item in admin.values():
            self.assertTrue(item["can_start"] and item["can_control"])
        self.assertEqual({k: (v["auth_failed"], v["auth_failure_id"]) for k, v in admin.items()},
                         {"cam01": (False, 0), "cam02": (True, 1), "cam03": (False, 0)})
        for who in (self.v02, self.ca03):
            for item in self.cameras_for(who).values():
                self.assertFalse(item["auth_failed"])
                self.assertNotIn("auth_failure_id", item)
        self.server.cameras._mark_auth_failed("cam03")
        self.assertFalse(self.cameras_for(self.ca03)["cam03"]["auth_failed"])
        self.assertTrue(self.cameras_for(("admin", PW))["cam03"]["auth_failed"])


if __name__ == "__main__":
    unittest.main()
