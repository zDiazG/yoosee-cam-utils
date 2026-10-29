import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from yoosee_cam_utils.recording import ClipManager


class Camera:
    c = {"username": "camera-user", "password": "camera-secret"}

    def profiles(self):
        return [{"token": "main"}, {"token": "sub"}]

    def uri(self, token):
        return f"rtsp://camera.invalid/{token}"


class Worker:
    def __init__(self, camera):
        self.camera = camera
        self.camera_lock = threading.Lock()


class Registry:
    def __init__(self):
        camera = Camera()
        self.cameras = {"cam01": camera}
        self.workers = {"cam01": Worker(camera)}

    def get(self, camera_id):
        return self.workers[camera_id]


class FakeProcess:
    def __init__(self, args, returncode=0, gate=None):
        self.args = args
        self.returncode = None
        self.result = returncode
        self.gate = gate

    def communicate(self, input=None, timeout=None):
        self.input = input
        if self.gate:
            self.gate.wait(timeout=2)
        if self.result == 0:
            Path(self.args[-1]).write_bytes(b"verified test media")
        self.returncode = self.result
        return b"", b""

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = Registry()
        self.config = {"enabled": True, "spool_dir": str(self.root / "recordings"), "max_bytes": 4 * 1024**2, "reserve_bytes": 0, "default_seconds": 2, "max_seconds": 10}
        self.manager = ClipManager(self.registry, self.config)
        self.probe = {"width": 1920, "height": 1080, "fps": 15.0, "duration_seconds": 2.0}

    def tearDown(self):
        self.manager.stop_all()
        self.temp.cleanup()

    def wait_for_job(self, job_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status = self.manager.status(job_id, "cam01")
            if status["state"] in ("complete", "failed"):
                return status
            time.sleep(.01)
        self.fail("recording job did not finish")

    def test_manual_capture_is_bounded_promoted_and_hash_recorded(self):
        process = FakeProcess
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=lambda args, **kwargs: process(args)), patch.object(ClipManager, "_probe", return_value=self.probe):
            job = self.manager.start("cam01", "main", 2)
            result = self.wait_for_job(job["id"])
        self.assertEqual(result["state"], "complete")
        media_path = self.manager.clip_path("cam01", job["id"])
        metadata = json.loads(media_path.with_suffix(".json").read_text())
        self.assertEqual(metadata["trigger"], "manual")
        self.assertEqual(metadata["profile"], "main")
        self.assertEqual(metadata["sha256"], hashlib.sha256(media_path.read_bytes()).hexdigest())
        self.assertEqual(metadata["bytes"], media_path.stat().st_size)
        self.assertEqual(self.manager.list_clips("cam01")[0]["id"], job["id"])
        self.assertFalse(list(self.manager.directory.rglob("*.part.mkv")))

    def test_ffmpeg_never_receives_camera_credentials_in_argv(self):
        captured = {}
        def factory(args, **kwargs):
            captured["args"] = args
            return FakeProcess(args)
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=factory), patch.object(ClipManager, "_probe", return_value=self.probe):
            job = self.manager.start("cam01", "main", 2)
            self.wait_for_job(job["id"])
        self.assertNotIn("camera-secret", " ".join(captured["args"]))
        self.assertIn("-fs", captured["args"])

    def test_clip_is_rejected_when_spool_budget_is_exhausted(self):
        with patch.object(self.manager, "_budget", return_value=1024**2 - 1):
            with self.assertRaisesRegex(ValueError, "quota or free-space"):
                self.manager.start("cam01", "main", 2)

    def test_only_one_clip_job_can_run_at_a_time(self):
        gate = threading.Event()
        factory = lambda args, **kwargs: FakeProcess(args, gate=gate)
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=factory), patch.object(ClipManager, "_probe", return_value=self.probe):
            first = self.manager.start("cam01", "main", 2)
            deadline = time.monotonic() + 1
            while self.manager.jobs[first["id"]]["process"] is None and time.monotonic() < deadline:
                time.sleep(.01)
            with self.assertRaisesRegex(ValueError, "another clip"):
                self.manager.start("cam01", "sub", 2)
            gate.set()
            self.assertEqual(self.wait_for_job(first["id"])["state"], "complete")

    def test_disabled_recorder_creates_no_directory_or_file(self):
        root = self.root / "disabled"
        manager = ClipManager(self.registry, {"enabled": False, "spool_dir": str(root)})
        self.assertFalse(root.exists())
        with self.assertRaisesRegex(ValueError, "disabled"):
            manager.start("cam01", "main", 2)

    def test_delete_removes_only_the_selected_clip_and_sidecar(self):
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=lambda args, **kwargs: FakeProcess(args)), patch.object(ClipManager, "_probe", return_value=self.probe):
            first = self.manager.start("cam01", "main", 2)
            result = self.wait_for_job(first["id"])
        self.assertEqual(result["state"], "complete")
        path = self.manager.clip_path("cam01", first["id"])
        metadata = path.with_suffix(".json")
        self.manager.delete_clip("cam01", first["id"])
        self.assertFalse(path.exists())
        self.assertFalse(metadata.exists())
        self.assertEqual(self.manager.list_clips("cam01"), [])
        with self.assertRaises(KeyError):
            self.manager.delete_clip("cam01", first["id"])

    def test_startup_removes_incomplete_and_orphaned_media_but_keeps_valid_clips(self):
        folder = self.root / "recordings" / "cam01" / "2026" / "10"
        folder.mkdir(parents=True)
        (folder / "interrupted.part.mkv").write_bytes(b"partial")
        (folder / "interrupted.json.part").write_text("{}")
        (folder / "orphan.mkv").write_bytes(b"orphan")
        clip_id = "c" * 32
        (folder / f"{clip_id}.mkv").write_bytes(b"verified")
        (folder / f"{clip_id}.json").write_text(json.dumps({"id": clip_id, "camera_id": "cam01"}))
        manager = ClipManager(self.registry, self.config)
        self.assertFalse((folder / "interrupted.part.mkv").exists())
        self.assertFalse((folder / "interrupted.json.part").exists())
        self.assertFalse((folder / "orphan.mkv").exists())
        self.assertTrue((folder / f"{clip_id}.mkv").exists())
        manager.stop_all()


if __name__ == "__main__":
    unittest.main()
