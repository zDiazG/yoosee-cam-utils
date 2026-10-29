"""Authenticated LAN HTTPS viewer with independent, bounded camera workers."""
import argparse
import base64
from collections import defaultdict, deque
import hashlib
import hmac
import ipaddress
import json
import math
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
import uuid

from .cli import Camera, load_config, media_input, redact, PTZ, SCHEMA
from xml.sax.saxutils import escape


class Streams:
    def __init__(self, camera, directory):
        self.camera = camera
        self.directory = Path(directory).expanduser()
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Media reads must not wait behind camera SOAP calls. The camera can take
        # several seconds to answer PTZ requests, while the browser polls HLS often.
        self.state_lock = threading.RLock()
        self.camera_lock = threading.Lock()
        self.lifecycle_lock = threading.Lock()
        self.process = None
        self.stream_dir = None
        self.profile = None
        self.last_access = 0
        self.log = None

    def profiles(self):
        with self.camera_lock:
            return self.camera.profiles()

    def _stop_worker(self):
        with self.state_lock:
            process, log, stream_dir = self.process, self.log, self.stream_dir
            self.process = None
            self.log = None
            self.stream_dir = None
            self.profile = None
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
        if log:
            log.close()
        if stream_dir and stream_dir.parent == self.directory:
            shutil.rmtree(stream_dir)

    def stop(self):
        with self.lifecycle_lock:
            self._stop_worker()

    def start(self, profile):
        if not isinstance(profile, str) or not re.fullmatch(r"[\w.-]{1,100}", profile):
            raise ValueError("invalid profile")
        with self.lifecycle_lock:
            with self.state_lock:
                if self.process and self.process.poll() is None and self.profile == profile:
                    self.last_access = time.monotonic()
                    return self.status(measure=True)
            with self.camera_lock:
                profiles = self.camera.profiles()
                if profile not in [p["token"] for p in profiles]:
                    raise ValueError("unknown profile")
                uri = self.camera.uri(profile)
            with self.state_lock:
                previous_worker = self.process is not None
            self._stop_worker()
            if previous_worker:
                # Allow firmware to release the previous RTSP session before switching.
                time.sleep(1)
            stream_dir = self.directory / uuid.uuid4().hex
            stream_dir.mkdir(mode=0o700)
            log = (stream_dir / "worker.log").open("wb")
            args = ["ffmpeg", "-nostdin", "-v", "error", "-fflags", "+genpts", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp", "-f", "concat", "-safe", "0", "-i", "pipe:0", "-map", "0:v:0", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-hls_time", "2", "-hls_list_size", "6", "-hls_delete_threshold", "2", "-hls_flags", "delete_segments+independent_segments+omit_endlist", "-hls_segment_filename", str(stream_dir / "seg%09d.ts"), "-f", "hls", str(stream_dir / "index.m3u8")]
            try:
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
            except BaseException:
                log.close()
                shutil.rmtree(stream_dir)
                raise
            with self.state_lock:
                self.stream_dir, self.log, self.process = stream_dir, log, process
                self.profile = profile
                self.last_access = time.monotonic()
            try:
                process.stdin.write(media_input(uri, self.camera.c))
                process.stdin.close()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if (stream_dir / "index.m3u8").exists():
                        return self.status(measure=True)
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
                raise ValueError("video worker did not produce a playlist")
            except BaseException:
                if self.log:
                    self.log.flush()
                    detail = (stream_dir / "worker.log").read_text(errors="replace")[-2000:]
                    print("HLS startup failed: " + redact(detail, self.camera.c), file=sys.stderr, flush=True)
                self._stop_worker()
                raise

    def status(self, measure=False):
        with self.state_lock:
            active = bool(self.process and self.process.poll() is None)
            stream_dir = self.stream_dir if active else None
            result = {"active": active, "profile": self.profile, "playlist": f"/media/{self.stream_dir.name}/index.m3u8" if active else None}
        if measure and stream_dir:
            result["source"] = self._measure_latest_segment(stream_dir)
        return result

    @staticmethod
    def _measure_latest_segment(stream_dir):
        """Report metadata from one current HLS segment without transcoding it."""
        try:
            lines = (stream_dir / "index.m3u8").read_text().splitlines()
            name = next(line for line in reversed(lines) if re.fullmatch(r"seg[0-9]{9}\.ts", line))
            segment = stream_dir / name
            if segment.parent != stream_dir or not segment.is_file():
                return None
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,avg_frame_rate", "-of", "json", str(segment)],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if probe.returncode:
                return None
            streams = json.loads(probe.stdout).get("streams", [])
            if not streams:
                return None
            source = streams[0]
            numerator, denominator = source.get("avg_frame_rate", "0/1").split("/", 1)
            fps = float(numerator) / float(denominator)
            if not math.isfinite(fps) or fps <= 0:
                return None
            return {"width": int(source["width"]), "height": int(source["height"]), "fps": round(fps, 3)}
        except (OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired, json.JSONDecodeError, ZeroDivisionError):
            return None

    def media(self, stream_id, filename):
        with self.state_lock:
            if not self.stream_dir or stream_id != self.stream_dir.name or not re.fullmatch(r"index\.m3u8|seg[0-9]{9}\.ts", filename):
                raise FileNotFoundError()
            data = (self.stream_dir / filename).read_bytes()
            self.last_access = time.monotonic()
            return data

    def ptz(self, pan=0.0, tilt=0.0, duration=0.2, stop=False):
        with self.state_lock:
            token = self.profile
        with self.camera_lock:
            token = token or self.camera.token(None)
            if stop:
                self.camera.stop(token)
            else:
                if not (abs(pan) <= 0.5 and abs(tilt) <= 0.5 and 0.2 <= duration <= 1):
                    raise ValueError("PTZ limits exceeded")
                try:
                    body = f'<p:ProfileToken>{escape(token)}</p:ProfileToken><p:Velocity><t:PanTilt x="{pan:g}" y="{tilt:g}"/><t:Zoom x="0"/></p:Velocity><p:Timeout>PT{duration:g}S</p:Timeout>'
                    self.camera.call("ptz", PTZ, "ContinuousMove", body, addressing=True, prefix="p", extra_namespaces=f'xmlns:t="{SCHEMA}"')
                    time.sleep(duration)
                finally:
                    self.camera.stop(token)
            return {"stop_confirmed": True}


class CameraRegistry:
    """Keep worker lifecycle and failures local to each configured camera."""

    def __init__(self, cameras, directory=None, streams=None, default_camera="cam01"):
        if not cameras or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in cameras):
            raise ValueError("invalid camera IDs")
        if default_camera not in cameras:
            raise ValueError("unknown default camera")
        self.default_camera = default_camera
        self.cameras = dict(cameras)
        self.workers = dict(streams) if streams is not None else {
            name: Streams(camera, Path(directory).expanduser() / name)
            for name, camera in cameras.items()
        }
        if set(self.workers) != set(self.cameras):
            raise ValueError("camera workers do not match camera IDs")
        self.errors = {}
        self.error_lock = threading.Lock()

    def get(self, camera_id):
        if camera_id not in self.workers:
            raise KeyError("unknown camera")
        return self.workers[camera_id]

    def invoke(self, camera_id, operation, *args):
        worker = self.get(camera_id)
        if operation not in ("profiles", "status", "start", "stop", "ptz"):
            raise ValueError("invalid camera operation")
        try:
            result = worker.status(measure=True) if operation == "status" else getattr(worker, operation)(*args)
        except (OSError, ValueError):
            with self.error_lock:
                self.errors[camera_id] = "Camera operation failed"
            raise
        if operation != "status":
            with self.error_lock:
                self.errors.pop(camera_id, None)
        if operation in ("status", "start"):
            result = dict(result)
            if result.get("playlist"):
                result["playlist"] = result["playlist"].replace("/media/", f"/media/{camera_id}/", 1)
        return result

    def summary(self):
        result = []
        for camera_id, camera in self.cameras.items():
            state = dict(self.get(camera_id).status())
            # Inventory/status does not contact a camera or acquire its RPC lock.
            state.pop("playlist", None)
            with self.error_lock:
                error = self.errors.get(camera_id)
            result.append({"id": camera_id, "name": camera.c.get("name", camera_id), **state, "error": error})
        return result

    def reap_idle(self, idle_seconds=120):
        for camera_id, worker in self.workers.items():
            # A slow startup for one camera must not delay cleanup of another.
            if not worker.lifecycle_lock.acquire(blocking=False):
                continue
            try:
                with worker.state_lock:
                    idle = worker.process and time.monotonic() - worker.last_access > idle_seconds
                if idle:
                    worker._stop_worker()
            except (OSError, ValueError):
                with self.error_lock:
                    self.errors[camera_id] = "Camera cleanup failed"
            finally:
                worker.lifecycle_lock.release()

    def stop_all(self):
        failures = []
        for worker in self.workers.values():
            try:
                worker.stop()
            except (OSError, ValueError) as error:
                failures.append(error)
        if failures:
            raise OSError("Camera worker cleanup failed") from failures[0]


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(15)
        try:
            return self.tls_context.wrap_socket(request, server_side=True), address
        except BaseException:
            request.close()
            raise


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, code, body=b"", content_type="application/json", challenge=False):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Camera viewer", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        ip = ipaddress.ip_address(self.client_address[0])
        if not any(ip in network for network in self.server.networks):
            self.respond(403); return False
        now = time.monotonic()
        with self.server.auth_lock:
            attempts = self.server.failures[str(ip)]
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 12:
                self.respond(429); return False
        try:
            scheme, value = self.headers.get("Authorization", "").split(" ", 1)
            raw = base64.b64decode(value, validate=True).decode()
            username, password = raw.split(":", 1)
            valid = scheme.lower() == "basic" and hmac.compare_digest(username.encode(), self.server.username.encode()) and hmac.compare_digest(password.encode(), self.server.password.encode())
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            with self.server.auth_lock:
                attempts.append(now)
            self.respond(401, challenge=True); return False
        return True

    def do_GET(self):
        if not self.authorized(): return
        path = urlsplit(self.path).path
        try:
            static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if path in static:
                name, mime = static[path]
                self.respond(200, (Path(__file__).parent / "static" / name).read_bytes(), mime)
            elif path == "/vendor/hls.min.js":
                self.respond(200, self.server.hls_js.read_bytes(), "text/javascript")
            elif path == "/api/cameras":
                self.respond(200, json.dumps(self.server.cameras.summary()).encode())
            elif re.fullmatch(r"/api/cameras/[^/]+/(profiles|status)", path):
                _, _, _, camera_id, operation = path.split("/")
                self.respond(200, json.dumps(self.server.cameras.invoke(camera_id, operation)).encode())
            elif path == "/api/profiles":
                self.respond(200, json.dumps(self.server.cameras.invoke(self.server.cameras.default_camera, "profiles")).encode())
            elif path == "/api/status":
                self.respond(200, json.dumps(self.server.cameras.invoke(self.server.cameras.default_camera, "status")).encode())
            elif path.startswith("/media/"):
                parts = path.split("/")
                if len(parts) == 4:
                    worker, stream_id, filename = self.server.streams, parts[2], parts[3]
                elif len(parts) == 5:
                    worker = self.server.cameras.get(parts[2])
                    stream_id, filename = parts[3], parts[4]
                else:
                    raise FileNotFoundError()
                mime = "application/vnd.apple.mpegurl" if filename.endswith(".m3u8") else "video/mp2t"
                self.respond(200, worker.media(stream_id, filename), mime)
            else: self.respond(404)
        except (FileNotFoundError, KeyError):
            self.respond(404)
        except (OSError, ValueError):
            self.respond(503, b'{"error":"Camera temporarily unavailable"}')

    def do_POST(self):
        if not self.authorized(): return
        origin = self.headers.get("Origin")
        if self.headers.get("X-Camera-Client") != "1" or (origin and origin != "https://" + self.headers.get("Host", "")):
            self.respond(403); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 2048 or self.headers.get_content_type() != "application/json":
                self.respond(400); return
            data = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            scoped = re.fullmatch(r"/api/cameras/([^/]+)/(start|stop|ptz)", path)
            if scoped:
                camera_id, operation = scoped.groups()
                try:
                    self.server.cameras.get(camera_id)
                except KeyError:
                    self.respond(404); return
                if operation == "start":
                    result = self.server.cameras.invoke(camera_id, "start", data["profile"])
                elif operation == "stop":
                    self.server.cameras.invoke(camera_id, "stop"); result = {"active": False}
                else:
                    result = self.server.cameras.invoke(camera_id, "ptz", float(data.get("pan", 0)), float(data.get("tilt", 0)), float(data.get("seconds", 0.2)), bool(data.get("stop", False)))
            elif path == "/api/start": result = self.server.cameras.invoke(self.server.cameras.default_camera, "start", data["profile"])
            elif path == "/api/stop":
                self.server.cameras.invoke(self.server.cameras.default_camera, "stop"); result = {"active": False}
            elif path == "/api/ptz":
                result = self.server.cameras.invoke(self.server.cameras.default_camera, "ptz", float(data.get("pan", 0)), float(data.get("tilt", 0)), float(data.get("seconds", 0.2)), bool(data.get("stop", False)))
            else:
                self.respond(404); return
            self.respond(200, json.dumps(result).encode())
        except (OSError, ValueError, KeyError, TypeError):
            self.respond(400, b'{"error":"Request failed; check camera availability and parameters"}')


def make_server(camera, web, streams=None, default_camera="cam01"):
    asset = Path(web["hls_js"]).expanduser()
    if hashlib.sha256(asset.read_bytes()).hexdigest() != web["hls_js_sha256"]:
        raise ValueError("HLS asset digest mismatch")
    key = Path(web["tls_key"]).expanduser()
    if key.stat().st_mode & 0o077: raise ValueError("TLS private key must have mode 0600")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(Path(web["tls_cert"]).expanduser()), str(key))
    cameras = camera if isinstance(camera, dict) else {default_camera: camera}
    workers = streams if isinstance(streams, dict) else {default_camera: streams} if streams is not None else None
    registry = CameraRegistry(cameras, web.get("cache_dir"), workers, default_camera)
    server = ViewerServer((web["bind"], web["port"]), Handler)
    server.tls_context = context
    server.cameras = registry
    server.streams = server.cameras.get(default_camera)
    auth_camera = cameras[default_camera]
    server.username, server.password = auth_camera.c["username"], auth_camera.c["password"]
    server.networks = [ipaddress.ip_network(n) for n in web["allowed_networks"]]
    server.auth_lock = threading.Lock()
    server.failures = defaultdict(deque)
    server.hls_js = asset
    return server


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--camera", help="Default camera for legacy viewer routes (all configured cameras remain available)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    config = tomllib.loads(Path(a.config).read_text())
    camera_ids = list(config["cameras"])
    default_camera = a.camera or ("cam01" if "cam01" in camera_ids else camera_ids[0] if camera_ids else "")
    if default_camera not in camera_ids:
        p.error("unknown default camera")
    cameras = {name: Camera(load_config(a.config, name)) for name in camera_ids}
    web = config["web"]
    if a.dry_run:
        print(json.dumps({"bind": web["bind"], "port": web["port"], "tls": True, "auth": "server-side basic", "allowed_networks": web["allowed_networks"], "cameras": camera_ids, "default_camera": default_camera})); return
    server = make_server(cameras, web, default_camera=default_camera)
    def reap():
        while True:
            time.sleep(10)
            server.cameras.reap_idle()
    threading.Thread(target=reap, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        try:
            server.cameras.stop_all()
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
