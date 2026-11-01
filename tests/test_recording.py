import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from yoosee_cam_utils.recording import ClipManager, MAX_SESSION_SECONDS


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
    def __init__(self, args, gate=None):
        self.args = args
        self.returncode = None
        self.signalled = False
        self.gate = gate
        self.input = b""

    def _finish(self):
        if self.returncode is not None:
            return self.returncode
        Path(self.args[-1]).write_text(f"duration={self.args[self.args.index('-t') + 1]}")
        self.returncode = -2 if self.signalled else 0
        return self.returncode

    def wait(self, timeout=None):
        # Popen.wait completes the process but doesn't wait for a stdin pipe reader.
        self.returncode = -2 if self.signalled else 0
        return self.returncode

    def communicate(self, input=None, timeout=None):
        if input is not None:
            self.input += input
        if self.gate:
            self.gate.wait(timeout=2)
        self._finish()
        return b"", b""

    def poll(self):
        return self.returncode

    def send_signal(self, _signal):
        self.signalled = True
        if self.gate:
            self.gate.set()

    def terminate(self):
        self.send_signal(None)

    def kill(self):
        self.returncode = -9
        if self.gate:
            self.gate.set()

    def wait(self, timeout=None):
        return self.returncode


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.registry = Registry()
        self.config = {
            "enabled": True,
            "spool_dir": str(self.root / "recordings"),
            "max_bytes": 4 * 1024**2,
            "reserve_bytes": 0,
            "segment_seconds": 2,
            "default_session_seconds": 5,
        }
        self.manager = ClipManager(self.registry, self.config)

    def tearDown(self):
        self.manager.stop_all()
        self.temp.cleanup()

    @staticmethod
    def probe(path):
        seconds = float(Path(path).read_text().split("=", 1)[1])
        return {"width": 1920, "height": 1080, "fps": 15.0, "duration_seconds": seconds}

    def wait_for_session(self, session_id):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.manager.lock:
                session = self.manager.sessions[session_id]
                if session["state"] != "recording":
                    return self.manager.status("cam01", session_id)
            time.sleep(.01)
        self.fail("recording session did not finish")

    def test_session_splits_into_bounded_clips_and_records_hashes(self):
        processes = []
        def factory(args, **kwargs):
            process = FakeProcess(args)
            processes.append(process)
            return process
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=factory), patch.object(ClipManager, "_probe", side_effect=self.probe):
            session = self.manager.start("cam01", "main", 5)
            result = self.wait_for_session(session["id"])
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["max_seconds"], 5)
        self.assertEqual(result["segment_count"], 3)
        self.assertTrue(all(process.input.startswith(b"ffconcat version 1.0\nfile '") for process in processes))
        for process in processes:
            self.assertEqual(process.args[process.args.index("-c:v") + 1], "copy")
            self.assertEqual(process.args[process.args.index("-c:a") + 1], "aac")
        clips = self.manager.list_clips("cam01")
        self.assertEqual(len(clips), 3)
        self.assertTrue(all(item["session_id"] == session["id"] for item in clips))
        self.assertEqual(sorted(item["duration_seconds"] for item in clips), [1, 2, 2])
        for clip in clips:
            path = self.manager.clip_path("cam01", clip["id"])
            self.assertEqual(clip["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.suffix, ".mp4")
            self.assertEqual(clip["owner"], "admin")
            self.assertRegex(clip["id"], r"^[0-9a-f]{32}$")
            self.assertEqual(clip["storage_backend"], "local")
            self.assertEqual(clip["replication_state"], "local_only")
            self.assertRegex(clip["display_name"], r"cam01_[A-Z][a-z]{2}_\d{8}_\d{6}_FHD")
        self.assertFalse(list(self.manager.directory.rglob("*.part.mp4")))

    def test_duplicate_clips_get_individual_stable_ids_only_when_near_identical(self):
        clips = [
            {"id": "a" * 32, "started_at": "2026-10-29T12:00:00+00:00", "duration_seconds": 60, "width": 1920, "height": 1080},
            {"id": "b" * 32, "started_at": "2026-10-29T12:00:10+00:00", "duration_seconds": 55, "width": 1920, "height": 1080},
            {"id": "c" * 32, "started_at": "2026-10-29T12:00:00+00:00", "duration_seconds": 300, "width": 1920, "height": 1080},
            {"id": "d" * 32, "started_at": "2026-10-29T12:00:00+00:00", "duration_seconds": 60, "width": 640, "height": 360},
        ]
        ClipManager._annotate_duplicate_groups(clips)
        self.assertEqual(clips[0]["duplicate_count"], 2)
        self.assertEqual(clips[1]["duplicate_count"], 2)
        self.assertNotIn("duplicate_count", clips[2])
        self.assertNotIn("duplicate_count", clips[3])

    def test_stop_finalizes_the_current_segment_and_keeps_prior_segments(self):
        gate = threading.Event()
        factory = lambda args, **kwargs: FakeProcess(args, gate=gate)
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=factory), patch.object(ClipManager, "_probe", side_effect=self.probe):
            session = self.manager.start("cam01", "main", 60)
            deadline = time.monotonic() + 1
            while self.manager.sessions[session["id"]]["process"] is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.manager.stop("cam01", session["id"])
            result = self.wait_for_session(session["id"])
        self.assertEqual(result["state"], "stopped")
        self.assertEqual(result["stop_reason"], "Stopped by user")
        self.assertEqual(result["segment_count"], 1)
        self.assertEqual(len(self.manager.list_clips("cam01")), 1)

    def test_sessions_are_scoped_to_user_and_camera_and_can_restart(self):
        gate = threading.Event()
        factory = lambda args, **kwargs: FakeProcess(args, gate=gate)
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=factory), patch.object(ClipManager, "_probe", side_effect=self.probe):
            alice = self.manager.start("cam01", "main", 60, owner="alice")
            with self.assertRaisesRegex(ValueError, "already have an active recording"):
                self.manager.start("cam01", "sub", 60, owner="alice")
            bob = self.manager.start("cam01", "sub", 60, owner="bob")
            self.manager.stop("cam01", alice["id"], owner="alice")
            self.manager.stop("cam01", bob["id"], owner="bob")
            self.wait_for_session(alice["id"])
            self.wait_for_session(bob["id"])
            restarted = self.manager.start("cam01", "main", 60, owner="alice")
            self.manager.stop("cam01", restarted["id"], owner="alice")
            self.wait_for_session(restarted["id"])

    def test_session_limit_is_capped_at_twelve_hours(self):
        self.assertEqual(MAX_SESSION_SECONDS, 12 * 60 * 60)
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=lambda args, **kwargs: FakeProcess(args)), patch.object(ClipManager, "_probe", side_effect=self.probe):
            with self.assertRaisesRegex(ValueError, "session limit"):
                self.manager.start("cam01", "main", MAX_SESSION_SECONDS + 1)
            session = self.manager.start("cam01", "main", MAX_SESSION_SECONDS)
            self.manager.stop("cam01", session["id"])
            self.wait_for_session(session["id"])

    def test_quota_exhaustion_prevents_a_new_session(self):
        with patch.object(self.manager, "_budget", return_value=1024**2 - 1):
            with self.assertRaisesRegex(ValueError, "quota or free-space"):
                self.manager.start("cam01", "main", 2)

    def test_disabled_recorder_creates_no_directory_or_file(self):
        root = self.root / "disabled"
        manager = ClipManager(self.registry, {"enabled": False, "spool_dir": str(root)})
        self.assertFalse(root.exists())
        with self.assertRaisesRegex(ValueError, "disabled"):
            manager.start("cam01", "main", 2)

    def test_delete_removes_only_the_selected_clip_and_sidecar(self):
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=lambda args, **kwargs: FakeProcess(args)), patch.object(ClipManager, "_probe", side_effect=self.probe):
            session = self.manager.start("cam01", "main", 2)
            self.wait_for_session(session["id"])
        clips = self.manager.list_clips("cam01")
        target = clips[0]
        path = self.manager.clip_path("cam01", target["id"])
        metadata = path.with_suffix(".json")
        self.manager.delete_clip("cam01", target["id"])
        self.assertFalse(path.exists())
        self.assertFalse(metadata.exists())
        self.assertEqual(len(self.manager.list_clips("cam01")), 0)

    def test_hidden_clips_are_filtered_and_can_be_restored(self):
        with patch("yoosee_cam_utils.recording.subprocess.Popen", side_effect=lambda args, **kwargs: FakeProcess(args)), patch.object(ClipManager, "_probe", side_effect=self.probe):
            session = self.manager.start("cam01", "main", 2, owner="alice")
            self.wait_for_session(session["id"])
        clip = self.manager.list_clips("cam01")[0]
        self.manager.set_hidden("cam01", clip["id"], True, "camadmin")
        self.assertEqual(self.manager.list_clips("cam01"), [])
        hidden = self.manager.list_clips("cam01", include_hidden=True, hidden_only=True)
        self.assertEqual(hidden[0]["hidden_by"], "camadmin")
        self.manager.set_hidden("cam01", clip["id"], False, "camadmin")
        self.assertEqual(len(self.manager.list_clips("cam01")), 1)

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
