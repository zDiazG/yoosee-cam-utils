import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

from yoosee_cam_utils import cli


class CameraTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = self.root / "secrets.env"
        self.env.write_text("CAM_USER=admin\nCAM_PASSWORD=test-secret!\n")
        self.env.chmod(0o600)
        self.config = self.root / "cameras.toml"
        self.config.write_text(f'credentials_file = "{self.env}"\n[cameras.cam01]\nhost="192.0.2.10"\nusername_env="CAM_USER"\npassword_env="CAM_PASSWORD"\n')
        self.c = cli.load_config(self.config, "cam01")

    def test_reject_public_credentials_file(self):
        self.env.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            cli.load_config(self.config, "cam01")

    def test_reject_invalid_port(self):
        with self.config.open("a") as f: f.write("onvif_port=70000\n")
        with self.assertRaisesRegex(ValueError, "onvif_port"):
            cli.load_config(self.config, "cam01")

    def test_reject_transport(self):
        with self.config.open("a") as f: f.write('rtsp_transport="shell"\n')
        with self.assertRaisesRegex(ValueError, "transport"):
            cli.load_config(self.config, "cam01")

    def test_dry_run_does_not_contact_camera(self):
        with patch.object(cli.Camera, "call", side_effect=AssertionError("network")), contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["--config", str(self.config), "--dry-run", "device"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["dry_run"])

    def test_uri_uses_configured_host_not_stale_advertisement(self):
        cam = cli.Camera(self.c)
        cam.call = Mock(return_value=ET.fromstring('<response><Uri>rtsp://192.0.2.99:554/onvif1</Uri></response>'))
        self.assertEqual(cam.uri("profile0"), "rtsp://192.0.2.10:554/onvif1")

    def test_profiles_keep_audio_and_video_encodings_separate(self):
        cam = cli.Camera(self.c)
        cam.call = Mock(return_value=ET.fromstring('<response><Profiles token="p0"><VideoEncoderConfiguration><Encoding>H264</Encoding></VideoEncoderConfiguration><AudioEncoderConfiguration><Encoding>G711</Encoding></AudioEncoderConfiguration></Profiles></response>'))
        p = cam.profiles()[0]
        self.assertEqual(p["VideoEncoderConfiguration"]["Encoding"], "H264")
        self.assertEqual(p["AudioEncoderConfiguration"]["Encoding"], "G711")

    def test_media_credentials_only_in_stdin(self):
        process = Mock(returncode=0, stdout=b'{"streams":[{"codec_type":"video","width":1920,"height":1080,"nb_read_packets":"3"}]}', stderr=b"")
        with patch.object(cli.subprocess, "run", return_value=process) as run:
            self.assertEqual(cli.media_run("rtsp://192.0.2.10/onvif1", self.c)["streams"][0]["width"], 1920)
        args, kwargs = run.call_args
        self.assertNotIn(self.c["password"], " ".join(args[0]))
        self.assertIn(b"test-secret%21", kwargs["input"])

    def test_probe_rejects_sdp_without_video_packets(self):
        process = Mock(returncode=0, stdout=b'{"streams":[{"codec_type":"video","width":0,"height":0}]}', stderr=b"")
        with patch.object(cli.subprocess, "run", return_value=process), self.assertRaisesRegex(ValueError, "No usable video"):
            cli.media_run("rtsp://192.0.2.10/onvif1", self.c)

    def test_stream_uses_stdin_credentials_and_binary_stdout(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (None, b"")
        with patch.object(cli.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(cli.live_media("rtsp://192.0.2.10/onvif1", self.c, seconds=5), 0)
        self.assertNotIn(self.c["password"], " ".join(popen.call_args.args[0]))
        self.assertNotIn("stdout", popen.call_args.kwargs)
        self.assertIn(b"test-secret%21", process.communicate.call_args.args[0])

    def test_view_uses_ffplay_without_transcoding(self):
        process = Mock(returncode=0)
        process.communicate.return_value = (None, b"")
        with patch.object(cli.subprocess, "Popen", return_value=process) as popen:
            self.assertEqual(cli.live_media("rtsp://192.0.2.10/onvif1", self.c, display=True), 0)
        self.assertEqual(popen.call_args.args[0][0], "ffplay")

    def test_media_error_redacts_password(self):
        process = Mock(returncode=1, stdout=b"", stderr=b"failed test-secret! test-secret%21")
        with patch.object(cli.subprocess, "run", return_value=process), self.assertRaises(ValueError) as error:
            cli.media_run("rtsp://192.0.2.10/onvif1", self.c)
        self.assertNotIn("test-secret", str(error.exception))

    def test_reject_concat_injection(self):
        with self.assertRaisesRegex(ValueError, "unsafe"):
            cli.media_input("rtsp://192.0.2.10/onvif1'\nfile /etc/passwd", self.c)

    def test_stop_falls_back_to_zero_velocity(self):
        cam = cli.Camera(self.c)
        cam.call = Mock(side_effect=[OSError("unsupported"), ET.fromstring("<ok/>")])
        cam.stop("profile0")
        self.assertEqual(cam.call.call_args_list[-1].args[2], "ContinuousMove")
        self.assertIn('x="0" y="0"', cam.call.call_args_list[-1].args[3])

    def test_move_rejects_unbounded_duration(self):
        with contextlib.redirect_stderr(io.StringIO()), patch.object(cli.Camera, "call", side_effect=AssertionError("network")):
            self.assertEqual(cli.main(["--config", str(self.config), "ptz-move", "--seconds", "20", "--execute"]), 2)

    def test_security_escapes_username_without_plaintext_password(self):
        xml = cli.security("a<&", "test-secret!")
        ET.fromstring(f'<root xmlns:s="{cli.SOAP}">{xml}</root>')
        self.assertIn("a&lt;&amp;", xml)
        self.assertNotIn("test-secret!", xml)

    def test_addressing_envelope_targets_requested_operation_and_service(self):
        cam = cli.Camera(self.c)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"<ok/>"
        cam.opener.open = Mock(return_value=response)

        cam.call("ptz", cli.PTZ, "ContinuousMove", "<m:ProfileToken/>", addressing=True)

        request = cam.opener.open.call_args.args[0]
        root = ET.fromstring(request.data)
        self.assertEqual(root.findtext(".//{http://schemas.xmlsoap.org/ws/2004/08/addressing}Action"), f"{cli.PTZ}/ContinuousMove")
        self.assertEqual(root.findtext(".//{http://schemas.xmlsoap.org/ws/2004/08/addressing}To"), "http://192.0.2.10:5000/onvif/ptz_service")


if __name__ == "__main__":
    unittest.main()
