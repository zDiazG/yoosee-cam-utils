import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "scripts/setup.py"
PASSWORD = "S3cret-pw-Z9"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Path(self.tmp.name)
        self.env = {**os.environ, "HOME": str(self.t / "home")}
        for k in list(self.env):
            if k.startswith("YOOSEE_"):
                del self.env[k]

    def args(self, *extra):
        return [sys.executable, str(SETUP), "--camera-host", "192.0.2.10",
                "--config-dir", str(self.t / "cfg"), "--state-dir", str(self.t / "state"),
                "--repo-config", str(self.t / "repo/cameras.toml"),
                "--credentials-file", str(self.t / "creds.env"), *extra]

    def run_setup(self, args, stdin=PASSWORD + "\n"):
        return subprocess.run(args, input=stdin, capture_output=True, text=True, env=self.env, cwd=ROOT)

    def files(self):
        return sorted(str(p) for p in self.t.rglob("*") if p.is_file())

    def test_dry_run_writes_nothing(self):
        r = self.run_setup(self.args("--dry-run", "--password-stdin", "--install-service"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("dry-run", r.stdout)
        self.assertEqual(self.files(), [])
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)

    @unittest.skipUnless(shutil.which("openssl"), "openssl missing")
    def test_full_run_and_rerun(self):
        a = self.args("--password-stdin", "--skip-assets", "--skip-check")
        r = self.run_setup(a)
        if "dependency check failed" in r.stderr:
            self.skipTest("host lacks ffmpeg/ffprobe")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)
        creds = self.t / "creds.env"
        cfg = self.t / "repo/cameras.toml"
        key = self.t / "cfg/tls/server.key"
        cert = self.t / "cfg/tls/server.crt"
        self.assertEqual(stat.S_IMODE(creds.stat().st_mode), 0o600)
        self.assertIn(f"CAM01_PASSWORD={PASSWORD}", creds.read_text())
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cfg.parent.stat().st_mode), 0o700)
        self.assertNotIn(PASSWORD, cfg.read_text())
        data = tomllib.loads(cfg.read_text())
        cam = data["cameras"]["cam01"]
        self.assertEqual((cam["host"], cam["onvif_port"], cam["rtsp_port"]), ("192.0.2.10", 5000, 554))
        self.assertEqual(data["web"]["port"], 8780)
        self.assertEqual(data["web"]["allowed_networks"], ["127.0.0.0/8"])
        self.assertEqual(data["web"]["tls_key"], str(key))
        self.assertEqual(len(data["web"]["hls_js_sha256"]), 64)
        self.assertTrue(cert.is_file())
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        before = {f: Path(f).read_bytes() for f in self.files()}
        r2 = self.run_setup(a)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("skipped", r2.stdout)
        self.assertEqual({f: Path(f).read_bytes() for f in self.files()}, before)
        self.assertNotIn(PASSWORD, r2.stdout + r2.stderr)

    def test_missing_host_non_interactive_fails(self):
        a = self.args("--password-stdin")
        i = a.index("--camera-host")
        del a[i:i + 2]
        r = self.run_setup(a)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing required values", r.stderr)
        self.assertIn("--camera-host", r.stderr)
        self.assertEqual(self.files(), [])

    def test_install_check_passes(self):
        if not all(shutil.which(t) for t in ("ffmpeg", "ffprobe", "openssl")):
            self.skipTest("host lacks tools")
        r = subprocess.run(["sh", str(ROOT / "install.sh"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
