"""Stdlib-only fake ONVIF camera for tests (binds 127.0.0.1 only)."""
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

SOAP = "http://www.w3.org/2003/05/soap-envelope"
SCHEMA = "http://www.onvif.org/ver10/schema"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"

DEFAULT_PROFILES = [
    {"token": "profile_1", "name": "main", "width": 1920, "height": 1080, "audio": True, "ptz": True},
    {"token": "profile_2", "name": "sub", "width": 640, "height": 360, "audio": False, "ptz": True},
]


def _envelope(body):
    return f'<?xml version="1.0"?><s:Envelope xmlns:s="{SOAP}"><s:Body>{body}</s:Body></s:Envelope>'


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def _send(self, code, payload, ctype="application/soap+xml; charset=utf-8"):
        data = payload.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        cam = self.server.camera
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        operation = "unknown"
        try:
            root = ET.fromstring(raw)
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "Body" and len(node):
                    operation = node[0].tag.rsplit("}", 1)[-1]
                    break
        except ET.ParseError:
            pass
        with cam._lock:
            cam.requests.append(operation)
            mode, delay = cam.mode, cam.delay
        if mode == "offline":
            self.close_connection = True
            self.connection.close()
            return
        if mode == "http401":
            self._send(401, "Unauthorized", "text/plain")
            return
        if mode == "auth_fault":
            self._send(200, _envelope(
                f'<s:Fault><s:Code><s:Value>s:Sender</s:Value></s:Code>'
                f'<s:Reason><s:Text>NotAuthorized: Sender not authorized</s:Text></s:Reason></s:Fault>'))
            return
        if mode == "slow":
            time.sleep(delay)
        self._send(200, _envelope(cam._response(operation)))


class FakeOnvifCamera:
    def __init__(self, profiles=None, mode="ok", delay=1.0, rtsp_port=8554):
        self.profiles = [dict(p) for p in (profiles if profiles is not None else DEFAULT_PROFILES)]
        self.mode = mode
        self.delay = delay
        self.rtsp_port = rtsp_port
        self.requests = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.camera = self
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _response(self, operation):
        if operation == "GetDeviceInformation":
            return (f'<tds:GetDeviceInformationResponse xmlns:tds="{DEVICE}"><tds:Manufacturer>Fake</tds:Manufacturer>'
                    '<tds:Model>FakeCam</tds:Model><tds:FirmwareVersion>1.0</tds:FirmwareVersion>'
                    '<tds:SerialNumber>0001</tds:SerialNumber><tds:HardwareId>fake</tds:HardwareId>'
                    '</tds:GetDeviceInformationResponse>')
        if operation == "GetProfiles":
            items = ""
            for p in self.profiles:
                tok = escape(p["token"])
                audio = (f'<tt:AudioEncoderConfiguration token="a_{tok}"><tt:Name>audio</tt:Name>'
                         '<tt:Encoding>AAC</tt:Encoding></tt:AudioEncoderConfiguration>') if p.get("audio") else ""
                ptz = (f'<tt:PTZConfiguration token="ptz_{tok}"><tt:Name>ptz</tt:Name></tt:PTZConfiguration>'
                       if p.get("ptz", True) else "")
                items += (f'<trt:Profiles token="{tok}" fixed="true"><tt:Name>{escape(p.get("name", p["token"]))}</tt:Name>'
                          f'<tt:VideoEncoderConfiguration token="v_{tok}"><tt:Name>video</tt:Name><tt:Encoding>H264</tt:Encoding>'
                          f'<tt:Resolution><tt:Width>{p.get("width", 1920)}</tt:Width><tt:Height>{p.get("height", 1080)}</tt:Height></tt:Resolution>'
                          f'</tt:VideoEncoderConfiguration>{audio}{ptz}</trt:Profiles>')
            return f'<trt:GetProfilesResponse xmlns:trt="{MEDIA}" xmlns:tt="{SCHEMA}">{items}</trt:GetProfilesResponse>'
        if operation == "GetStreamUri":
            token = self.profiles[0]["token"] if self.profiles else "profile_1"
            return (f'<trt:GetStreamUriResponse xmlns:trt="{MEDIA}" xmlns:tt="{SCHEMA}"><trt:MediaUri>'
                    f'<tt:Uri>rtsp://127.0.0.1:{self.rtsp_port}/{escape(token)}</tt:Uri></trt:MediaUri></trt:GetStreamUriResponse>')
        if operation in ("ContinuousMove", "Stop"):
            return f'<tptz:{operation}Response xmlns:tptz="{PTZ}"/>'
        return f'<s:Fault xmlns:s="{SOAP}"><s:Reason><s:Text>unsupported operation</s:Text></s:Reason></s:Fault>'

    def camera_config(self, username="admin", password="fake-pass-1234", **overrides):
        config = {"host": "127.0.0.1", "onvif_port": self.port, "rtsp_port": self.rtsp_port,
                  "username": username, "password": password}
        config.update(overrides)
        return config

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
