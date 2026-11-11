from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from yoosee_cam_utils.accounts import UserStore
from yoosee_cam_utils.admin_cli import run

PW = "correct-horse-battery"


class AdminCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.db_path = root / "state" / "users.sqlite3"
        self.config = root / "config.toml"
        self.config.write_text(f'''
[web]
allowed_networks = ["192.0.2.0/24"]
auth_database = "{self.db_path}"

[cameras.cam01]
host = "192.0.2.10"

[cameras.cam02]
host = "192.0.2.11"
''')

    def cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", io.StringIO(stdin or "")):
            code = run(["--config", str(self.config), *args])
        self.assertNotIn("Traceback", err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def add(self, name, preset, password=PW, *extra):
        code, out, err = self.cli("users", "add", name, "--preset", preset, "--password-stdin", *extra, stdin=password + "\n")
        self.assertEqual(code, 0, err)

    def store(self):
        return UserStore(self.db_path, ["192.0.2.0/24"])

    def user(self, name):
        return next(u for u in self.store().inventory() if u["username"] == name)

    def raw(self, sql, *params):
        with closing(sqlite3.connect(self.db_path)) as db:
            return db.execute(sql, params).fetchall()

    def test_presets_create_expected_roles_grants_and_capabilities(self):
        self.add("root", "admin")
        self.add("ops", "camera-admin")
        self.add("guest", "viewer")
        self.add("lobby", "watcher", PW, "--cameras", "cam02")
        root, ops, guest, lobby = (self.user(n) for n in ("root", "ops", "guest", "lobby"))
        self.assertEqual((root["roles"], root["cameras"], root["capabilities"]), (["web_admin"], {}, ["record"]))
        self.assertEqual(ops["roles"], ["camera_admin", "viewer"])
        self.assertEqual(ops["cameras"], {"cam01": "camera_admin", "cam02": "camera_admin"})
        self.assertEqual(ops["capabilities"], ["record"])
        self.assertEqual((guest["roles"], guest["cameras"], guest["capabilities"]),
                         (["viewer"], {"cam01": "viewer", "cam02": "viewer"}, ["record"]))
        self.assertEqual((lobby["roles"], lobby["cameras"], lobby["capabilities"]), (["viewer"], {"cam02": "viewer"}, ["watcher"]))
        self.assertTrue(self.store().password_matches("guest", PW))
        self.add("nrec", "viewer", PW, "--no-record", "--cameras", "cam01")
        self.assertEqual(self.user("nrec")["capabilities"], [])

    def test_passwd_changes_hash_and_ends_persisted_sessions(self):
        self.add("root", "admin")
        self.add("lobby", "watcher")
        store = self.store()
        store.persist_session("digest1", "lobby", "192.0.2.5", "csrf", 1)
        before = self.raw("SELECT password_hash,session_version FROM users WHERE username='lobby'")[0]
        code, out, err = self.cli("users", "passwd", "lobby", "--password-stdin", stdin="another-long-password\n")
        self.assertEqual(code, 0, err)
        after = self.raw("SELECT password_hash,session_version FROM users WHERE username='lobby'")[0]
        self.assertNotEqual(before[0], after[0])
        self.assertEqual(after[1], before[1] + 1)
        self.assertIsNone(store.load_session("digest1"))
        self.assertTrue(store.password_matches("lobby", "another-long-password"))
        self.assertFalse(store.password_matches("lobby", PW))

    def test_disable_enable_and_session_removal(self):
        self.add("root", "admin")
        self.add("lobby", "watcher")
        store = self.store()
        store.persist_session("digest1", "lobby", "192.0.2.5", "csrf", 1)
        self.assertEqual(self.cli("users", "disable", "lobby")[0], 0)
        self.assertFalse(self.user("lobby")["enabled"])
        self.assertIsNone(store.load_session("digest1"))
        self.assertIsNone(store.authenticate("lobby", PW, __import__("ipaddress").ip_address("192.0.2.5")))
        self.assertEqual(self.cli("users", "enable", "lobby")[0], 0)
        self.assertTrue(self.user("lobby")["enabled"])

    def test_last_enabled_admin_cannot_be_disabled(self):
        self.add("root", "admin")
        code, out, err = self.cli("users", "disable", "root")
        self.assertEqual(code, 2)
        self.assertIn("last enabled Web administrator", err)
        self.assertTrue(self.user("root")["enabled"])
        self.add("second", "admin")
        self.assertEqual(self.cli("users", "disable", "root")[0], 0)
        self.assertEqual(self.cli("users", "disable", "second")[0], 2)

    def test_grant_and_revoke_keep_other_attributes(self):
        self.add("lobby", "watcher", PW, "--cameras", "cam01")
        self.assertEqual(self.cli("users", "grant", "lobby", "cam02")[0], 0)
        lobby = self.user("lobby")
        self.assertEqual(lobby["cameras"], {"cam01": "viewer", "cam02": "viewer"})
        self.assertEqual(lobby["capabilities"], ["watcher"])
        self.assertEqual(self.cli("users", "revoke", "lobby", "cam01")[0], 0)
        self.assertEqual(self.user("lobby")["cameras"], {"cam02": "viewer"})
        code, _, err = self.cli("users", "revoke", "lobby", "cam02")
        self.assertEqual(code, 2)
        self.assertEqual(self.user("lobby")["cameras"], {"cam02": "viewer"})

    def test_grant_to_camera_admin_uses_control_level(self):
        self.add("ops", "camera-admin", PW, "--cameras", "cam01")
        self.cli("users", "grant", "ops", "cam02")
        self.assertEqual(self.user("ops")["cameras"], {"cam01": "camera_admin", "cam02": "camera_admin"})

    def test_set_networks_and_clear(self):
        self.add("guest", "viewer")
        self.assertEqual(self.cli("users", "set-networks", "guest", "192.0.2.0/25")[0], 0)
        self.assertEqual(self.user("guest")["networks"], ["192.0.2.0/25"])
        code, _, err = self.cli("users", "set-networks", "guest", "203.0.113.0/24")
        self.assertEqual(code, 2)
        self.assertIn("outside the global allowlist", err)
        self.assertEqual(self.user("guest")["networks"], ["192.0.2.0/25"])
        self.assertEqual(self.cli("users", "set-networks", "guest", "-")[0], 0)
        self.assertEqual(self.user("guest")["networks"], [])

    def test_networks_add_list_remove(self):
        code, out, _ = self.cli("networks", "list")
        self.assertEqual((code, out), (0, "192.0.2.0/24\tconfig\n"))
        self.assertEqual(self.cli("networks", "add", "198.51.100.0/24")[0], 0)
        self.assertEqual(self.cli("networks", "list")[1].splitlines(), ["192.0.2.0/24\tconfig", "198.51.100.0/24\tadmin"])
        code, _, err = self.cli("networks", "remove", "192.0.2.0/24")
        self.assertEqual(code, 2)
        self.assertIn("configuration file", err)
        self.assertEqual(self.cli("networks", "remove", "198.51.100.0/24")[0], 0)
        self.assertEqual(self.cli("networks", "list")[1], "192.0.2.0/24\tconfig\n")
        self.assertEqual(self.cli("networks", "add", "not-a-network")[0], 2)

    def test_short_password_rejected(self):
        code, _, err = self.cli("users", "add", "weak", "--preset", "viewer", "--password-stdin", stdin="short-pw\n")
        self.assertEqual(code, 2)
        self.assertIn("12", err)
        self.assertEqual(self.store().inventory(), [])
        self.add("ok", "viewer")
        code, _, err = self.cli("users", "passwd", "ok", "--password-stdin", stdin="tooshort\n")
        self.assertEqual(code, 2)
        self.assertTrue(self.store().password_matches("ok", PW))

    def test_empty_stdin_password_rejected(self):
        code, _, err = self.cli("users", "add", "empty", "--preset", "viewer", "--password-stdin", stdin="")
        self.assertEqual(code, 2)
        self.assertEqual(self.store().inventory(), [])

    def test_unknown_user_and_camera_exit_cleanly(self):
        self.add("guest", "viewer")
        for args, stdin in ((("users", "passwd", "nobody", "--password-stdin"), PW + "\n"),
                            (("users", "enable", "nobody"), None),
                            (("users", "disable", "nobody"), None),
                            (("users", "grant", "nobody", "cam01"), None),
                            (("users", "set-networks", "nobody", "-"), None)):
            code, out, err = self.cli(*args, stdin=stdin)
            self.assertEqual(code, 2, args)
            self.assertIn("Unknown account", err)
        for args in (("users", "grant", "guest", "cam99"),
                     ("users", "add", "newbie", "--preset", "viewer", "--cameras", "cam99", "--password-stdin")):
            code, out, err = self.cli(*args, stdin=PW + "\n")
            self.assertEqual(code, 2, args)
            self.assertIn("cam99", err)
        self.assertEqual([u["username"] for u in self.store().inventory()], ["guest"])

    def test_duplicate_user_rejected(self):
        self.add("guest", "viewer")
        code, _, err = self.cli("users", "add", "guest", "--preset", "viewer", "--password-stdin", stdin=PW + "\n")
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_users_list_never_prints_secrets(self):
        self.add("root", "admin")
        self.add("lobby", "watcher")
        code, out, _ = self.cli("users", "list")
        self.assertEqual(code, 0)
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["username"] for r in rows], ["lobby", "root"])
        for row in rows:
            self.assertEqual(set(row), {"username", "roles", "capabilities", "enabled", "cameras", "networks"})
        for salt, digest in self.raw("SELECT salt,password_hash FROM users"):
            for secret in (salt, digest):
                self.assertNotIn(secret.hex(), out)
                self.assertNotIn(repr(secret), out)
        self.assertNotIn(PW, out)


if __name__ == "__main__":
    unittest.main()
