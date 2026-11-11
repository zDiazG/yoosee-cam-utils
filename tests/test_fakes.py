import time
import unittest

from yoosee_cam_utils.cli import Camera, CameraAuthError

from tests.fakes import FakeOnvifCamera


class FakeOnvifCameraTests(unittest.TestCase):
    def fake(self, **kw):
        cam = FakeOnvifCamera(**kw)
        self.addCleanup(cam.close)
        return cam

    def test_profiles_and_uri(self):
        fake = self.fake(rtsp_port=9554)
        cam = Camera(fake.camera_config())
        profiles = cam.profiles()
        self.assertEqual([p["token"] for p in profiles], ["profile_1", "profile_2"])
        self.assertEqual(profiles[0]["VideoEncoderConfiguration"]["Width"], "1920")
        self.assertEqual(profiles[1]["VideoEncoderConfiguration"]["Height"], "360")
        self.assertIn("AudioEncoderConfiguration", profiles[0])
        self.assertNotIn("AudioEncoderConfiguration", profiles[1])
        self.assertTrue(profiles[0]["ptz"])
        self.assertEqual(cam.uri("profile_1"), "rtsp://127.0.0.1:9554/profile_1")
        self.assertEqual(fake.requests, ["GetProfiles", "GetStreamUri"])

    def test_auth_failures(self):
        for mode in ("auth_fault", "http401"):
            fake = self.fake(mode=mode)
            with self.assertRaises(CameraAuthError, msg=mode):
                Camera(fake.camera_config()).profiles()

    def test_offline_and_slow(self):
        fake = self.fake(mode="offline")
        with self.assertRaises(OSError):
            Camera(fake.camera_config()).profiles()
        fake.mode, fake.delay = "slow", 0.7
        start = time.monotonic()
        self.assertTrue(Camera(fake.camera_config()).profiles())
        self.assertGreaterEqual(time.monotonic() - start, 0.7)

    def test_three_cameras_independent(self):
        a = self.fake(profiles=[{"token": "a1", "width": 100, "height": 50}])
        b = self.fake(profiles=[{"token": "b1"}, {"token": "b2"}], mode="auth_fault")
        c = self.fake(mode="offline")
        self.assertEqual(len({a.port, b.port, c.port}), 3)
        self.assertEqual([p["token"] for p in Camera(a.camera_config()).profiles()], ["a1"])
        with self.assertRaises(CameraAuthError):
            Camera(b.camera_config()).profiles()
        with self.assertRaises(OSError):
            Camera(c.camera_config()).profiles()
        b.mode = "ok"
        self.assertEqual([p["token"] for p in Camera(b.camera_config()).profiles()], ["b1", "b2"])
        self.assertEqual(len(a.requests), 1)

    def test_stop_recorded_after_ptz_sequence(self):
        fake = self.fake()
        cam = Camera(fake.camera_config())
        token = cam.token(None)
        cam.stop(token)
        self.assertEqual(fake.requests, ["GetProfiles", "Stop"])


if __name__ == "__main__":
    unittest.main()
