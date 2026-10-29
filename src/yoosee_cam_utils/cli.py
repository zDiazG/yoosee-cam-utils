"""Small stdlib CLI. Credentials are never placed in FFmpeg arguments."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

SOAP = "http://www.w3.org/2003/05/soap-envelope"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
TOKEN = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0"
MSG = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0"
DEVICE = "http://www.onvif.org/ver10/device/wsdl"
MEDIA = "http://www.onvif.org/ver10/media/wsdl"
PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
SCHEMA = "http://www.onvif.org/ver10/schema"


def env_file(path):
    path = Path(path).expanduser()
    if path.stat().st_mode & 0o077:
        raise ValueError("credentials file must have mode 0600")
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value
    return values


def load_config(path, camera):
    config = tomllib.loads(Path(path).read_text())
    c = config["cameras"][camera]
    host = c["host"]
    if not host or any(ch in host for ch in "/@?# \n\r"):
        raise ValueError("invalid camera host")
    for key, default in (("onvif_port", 5000), ("rtsp_port", 554)):
        value = c.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise ValueError(f"invalid {key}")
        c[key] = value
    if c.get("rtsp_transport", "tcp") not in ("tcp", "udp"):
        raise ValueError("rtsp_transport must be tcp or udp")
    secrets = env_file(config["credentials_file"])
    c["username"] = os.environ.get(c["username_env"], secrets.get(c["username_env"], ""))
    c["password"] = os.environ.get(c["password_env"], secrets.get(c["password_env"], ""))
    if not c["username"] or not c["password"]:
        raise ValueError("missing camera credentials")
    return c


def redact(text, config):
    for secret in (config.get("password", ""), urllib.parse.quote(config.get("password", ""), safe="")):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def elements(root, name):
    return [n for n in root.iter() if n.tag.rsplit("}", 1)[-1] == name]


def security(username, password):
    nonce = os.urandom(16)
    created = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        f'<w:Security xmlns:w="{WSSE}" xmlns:u="{WSU}" s:mustUnderstand="1"><w:UsernameToken>'
        f'<w:Username>{escape(username)}</w:Username><w:Password Type="{TOKEN}#PasswordDigest">{digest}</w:Password>'
        f'<w:Nonce EncodingType="{MSG}#Base64Binary">{base64.b64encode(nonce).decode()}</w:Nonce>'
        f'<u:Created>{created}</u:Created></w:UsernameToken></w:Security>'
    )


class Camera:
    def __init__(self, config):
        self.c = config
        self.last = 0.0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, service, namespace, operation, children="", addressing=False, prefix="m", extra_namespaces=""):
        time.sleep(max(0, 0.4 - (time.monotonic() - self.last)))
        path = self.c.get(f"{service}_path", f"/onvif/{service}_service")
        if not path.startswith("/") or "\n" in path or "\r" in path:
            raise ValueError("invalid service path")
        url = f'http://{self.c["host"]}:{self.c["onvif_port"]}{path}'
        declarations = f' xmlns:{prefix}="{namespace}"'
        if extra_namespaces:
            declarations += f" {extra_namespaces}"
        body = f'<{prefix}:{operation}{declarations}>{children}</{prefix}:{operation}>'
        header = security(self.c["username"], self.c["password"])
        if addressing:
            wsa = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
            target = escape(url)
            header = (
                f'<a:Action xmlns:a="{wsa}">{namespace}/{operation}</a:Action>'
                f'<a:MessageID xmlns:a="{wsa}">urn:uuid:{uuid.uuid4()}</a:MessageID>'
                f'<a:To xmlns:a="{wsa}">{target}</a:To>{header}'
            )
        xml = f'<s:Envelope xmlns:s="{SOAP}"><s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>'
        request = urllib.request.Request(url, data=xml.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8", "Connection": "close"})
        try:
            with self.opener.open(request, timeout=6) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("ONVIF response exceeds limit")
            root = ET.fromstring(raw)
            if elements(root, "Fault"):
                raise ValueError("camera returned an ONVIF fault")
            return root
        finally:
            self.last = time.monotonic()

    def profiles(self):
        root = self.call("media", MEDIA, "GetProfiles")
        result = []
        for profile in elements(root, "Profiles"):
            item = {"token": profile.get("token"), "ptz": bool(elements(profile, "PTZConfiguration"))}
            for kind in ("VideoEncoderConfiguration", "AudioEncoderConfiguration"):
                nodes = elements(profile, kind)
                if nodes:
                    item[kind] = {n.tag.rsplit("}", 1)[-1]: n.text for n in nodes[0].iter() if n.text and n.text.strip()}
            result.append(item)
        return result

    def uri(self, profile):
        children = f'<m:StreamSetup><Stream xmlns="{SCHEMA}">RTP-Unicast</Stream><Transport xmlns="{SCHEMA}"><Protocol>RTSP</Protocol></Transport></m:StreamSetup><m:ProfileToken>{escape(profile)}</m:ProfileToken>'
        root = self.call("media", MEDIA, "GetStreamUri", children)
        uris = elements(root, "Uri")
        if not uris or not uris[0].text:
            raise ValueError("camera did not return a stream URI")
        if any(ch in uris[0].text for ch in "\r\n'"):
            raise ValueError("unsafe RTSP URI")
        original = urllib.parse.urlsplit(uris[0].text)
        if original.scheme != "rtsp" or not original.path:
            raise ValueError("invalid RTSP URI")
        # Several cameras advertise stale addresses; connect only to configured host.
        return urllib.parse.urlunsplit(("rtsp", f'{self.c["host"]}:{self.c["rtsp_port"]}', original.path, original.query, ""))

    def token(self, requested):
        if requested:
            return requested
        profiles = self.profiles()
        if not profiles or not profiles[0]["token"]:
            raise ValueError("no usable media profile")
        return profiles[0]["token"]

    def stop(self, token):
        try:
            return self.call("ptz", PTZ, "Stop", f'<m:ProfileToken>{escape(token)}</m:ProfileToken><m:PanTilt>true</m:PanTilt><m:Zoom>true</m:Zoom>')
        except (OSError, ValueError):
            # Some firmware only implements ContinuousMove, including zero velocity.
            return self.call("ptz", PTZ, "ContinuousMove", f'<m:ProfileToken>{escape(token)}</m:ProfileToken><m:Velocity><PanTilt xmlns="{SCHEMA}" x="0" y="0"/></m:Velocity><m:Timeout>PT0.1S</m:Timeout>')


def media_input(uri, config):
    if any(ch in uri for ch in "\r\n'"):
        raise ValueError("unsafe RTSP URI")
    url = urllib.parse.urlsplit(uri)
    netloc = urllib.parse.quote(config["username"], safe="") + ":" + urllib.parse.quote(config["password"], safe="") + "@" + url.netloc
    authenticated = urllib.parse.urlunsplit((url.scheme, netloc, url.path, url.query, ""))
    transport = config.get("rtsp_transport", "tcp")
    return (f"ffconcat version 1.0\nfile '{authenticated}'\noption rtsp_transport {transport}\noption timeout 6000000\n").encode()


def media_run(uri, config, seconds=None, output=None):
    binary = "ffprobe" if output is None else "ffmpeg"
    args = [binary, "-v", "error", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp", "-f", "concat", "-safe", "0", "-i", "pipe:0"]
    if output is None:
        args += ["-count_packets", "-read_intervals", "%+2", "-show_streams", "-of", "json"]
    else:
        args += ["-t", str(seconds), "-map", "0", "-c", "copy", "-n", str(output)]
    process = subprocess.run(args, input=media_input(uri, config), capture_output=True, timeout=(seconds or 0) + 30)
    if process.returncode:
        raise ValueError(redact(process.stderr.decode(errors="replace"), config)[-1600:])
    if output is None:
        result = json.loads(process.stdout)
        video = [s for s in result.get("streams", []) if s.get("codec_type") == "video"]
        if not any(s.get("width", 0) > 0 and s.get("height", 0) > 0 and int(s.get("nb_read_packets", "0").replace("N/A", "0")) > 0 for s in video):
            raise ValueError("No usable video packets received; check RTSP transport and routing")
        return result
    return {"output": str(output), "bytes": Path(output).stat().st_size}


def live_media(uri, config, display=False, seconds=0):
    args = ["ffplay" if display else "ffmpeg", "-v", "error", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp", "-f", "concat", "-safe", "0", "-i", "pipe:0"]
    if display:
        args += ["-window_title", "Local camera", "-autoexit"]
    else:
        if seconds:
            args += ["-t", str(seconds)]
        args += ["-map", "0", "-c", "copy", "-f", "matroska", "pipe:1"]
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        _, error = process.communicate(media_input(uri, config))
    except KeyboardInterrupt:
        process.terminate()
        try:
            _, error = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            _, error = process.communicate()
        return 130
    if error:
        print(redact(error.decode(errors="replace"), config), file=sys.stderr)
    return 0 if process.returncode == 0 else 2


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config/local/cameras.toml")
    p.add_argument("--camera", default="cam01")
    p.add_argument("--dry-run", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("doctor", "device", "profiles"):
        sub.add_parser(name)
    for name in ("uri", "probe", "record", "view", "stream", "ptz-status", "ptz-stop", "ptz-move"):
        s = sub.add_parser(name)
        s.add_argument("--profile")
        if name == "record":
            s.add_argument("--seconds", type=int, default=10)
            s.add_argument("--output", type=Path, required=True)
        if name == "stream":
            s.add_argument("--seconds", type=int, default=0, help="0 streams until interrupted")
        if name == "ptz-move":
            s.add_argument("--pan", type=float, default=0)
            s.add_argument("--tilt", type=float, default=0)
            s.add_argument("--seconds", type=float, default=0.2)
            s.add_argument("--execute", action="store_true")
    a = p.parse_args(argv)
    c = None
    try:
        c = load_config(a.config, a.camera)
        if a.command == "record" and not 1 <= a.seconds <= 300:
            raise ValueError("record duration must be 1..300 seconds")
        if a.command == "stream" and not 0 <= a.seconds <= 3600:
            raise ValueError("stream duration must be 0..3600 seconds")
        if a.command == "ptz-move" and not (0 < a.seconds <= 2 and abs(a.pan) <= 1 and abs(a.tilt) <= 1):
            raise ValueError("PTZ duration must be 0..2 seconds and velocities -1..1")
        if a.dry_run or (a.command == "ptz-move" and not a.execute):
            print(json.dumps({"dry_run": True, "camera": a.camera, "command": a.command}))
            return 0
        cam = Camera(c)
        if a.command == "doctor":
            result = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")), "ports": {}}
            for port in (c["onvif_port"], c["rtsp_port"]):
                try:
                    with socket.create_connection((c["host"], port), timeout=3): result["ports"][port] = True
                except OSError: result["ports"][port] = False
        elif a.command == "device":
            root = cam.call("device", DEVICE, "GetDeviceInformation")
            result = {n.tag.rsplit("}", 1)[-1]: n.text for n in root.iter() if n.text and n.text.strip()}
        elif a.command == "profiles":
            result = cam.profiles()
        else:
            token = cam.token(a.profile)
            if a.command in ("uri", "probe", "record", "view", "stream"):
                uri = cam.uri(token)
                if a.command in ("view", "stream"):
                    return live_media(uri, c, a.command == "view", getattr(a, "seconds", 0))
                if a.command == "uri": result = {"profile": token, "uri": uri}
                elif a.command == "probe": result = media_run(uri, c)
                else: result = media_run(uri, c, a.seconds, a.output)
            elif a.command == "ptz-status":
                root = cam.call("ptz", PTZ, "GetStatus", f'<m:ProfileToken>{escape(token)}</m:ProfileToken>')
                result = {n.tag.rsplit("}", 1)[-1]: n.text for n in root.iter() if n.text and n.text.strip()}
            elif a.command == "ptz-stop":
                cam.stop(token); result = {"stopped": True}
            else:
                try:
                    velocity = f'<m:ProfileToken>{escape(token)}</m:ProfileToken><m:Velocity><PanTilt xmlns="{SCHEMA}" x="{a.pan}" y="{a.tilt}"/></m:Velocity><m:Timeout>PT{a.seconds}S</m:Timeout>'
                    cam.call("ptz", PTZ, "ContinuousMove", velocity)
                    time.sleep(a.seconds)
                finally:
                    cam.stop(token)
                result = {"move_requested": True, "stop_confirmed": True}
        print(redact(json.dumps(result, indent=2), c))
        return 0
    except (OSError, ValueError, KeyError, ET.ParseError, subprocess.SubprocessError) as exc:
        print(redact(str(exc), c or {}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
