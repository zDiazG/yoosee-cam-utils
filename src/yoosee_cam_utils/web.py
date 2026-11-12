"""Authenticated LAN HTTPS viewer with independent, bounded camera workers."""
import argparse
from collections import defaultdict, deque
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import ssl
import subprocess
import sys
import threading
import time
import tomllib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import uuid

from .cli import Camera, CameraAuthError, load_config, media_input, redact, looks_like_auth_failure, PTZ, SCHEMA
from .accounts import UserStore
from .camera_credentials import CredentialStore
from .recording import ClipManager
from .live import LiveBuffer
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
        self.live = None
        self.live_thread = None

    def profiles(self):
        with self.camera_lock:
            return self.camera.profiles()

    def _stop_worker(self):
        with self.state_lock:
            process, log, stream_dir = self.process, self.log, self.stream_dir
            live, live_thread = self.live, self.live_thread
            self.process = None
            self.log = None
            self.stream_dir = None
            self.profile = None
            self.live = self.live_thread = None
        if live:
            live.close()
        if process:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()
        if live_thread:
            live_thread.join(timeout=2)
        if process and process.stdout:
            process.stdout.close()
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
            tee = (
                f"[f=hls:hls_time=2:hls_list_size=6:hls_delete_threshold=2:"
                f"hls_flags=delete_segments+independent_segments+omit_endlist:"
                f"hls_segment_filename={stream_dir / 'seg%09d.ts'}]{stream_dir / 'index.m3u8'}|"
                "[f=mp4:movflags=+empty_moov+default_base_moof+frag_keyframe:"
                "frag_duration=200000:flush_packets=1]pipe:1"
            )
            args = ["ffmpeg", "-nostdin", "-v", "error", "-fflags", "+genpts", "-analyzeduration", "1000000", "-probesize", "262144", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp", "-f", "concat", "-safe", "0", "-i", "pipe:0",
                    "-map", "0:v:0", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-max_interleave_delta", "100000", "-flags", "+global_header", "-f", "tee", tee]
            try:
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=0)
            except BaseException:
                log.close()
                shutil.rmtree(stream_dir)
                raise
            with self.state_lock:
                self.stream_dir, self.log, self.process = stream_dir, log, process
                self.live = LiveBuffer()
                self.live_thread = threading.Thread(target=self.live.read, args=(process.stdout,), daemon=True)
                self.live_thread.start()
                self.profile = profile
                self.last_access = time.monotonic()
            try:
                process.stdin.write(media_input(uri, self.camera.c) + b"option analyzeduration 1000000\noption probesize 262144\noption max_delay 100000\n")
                process.stdin.close()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if self.live.initialization and self.live.fragments and not self.live.closed:
                        return self.status(measure=True)
                    if self.live.failed:
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.2)
                raise ValueError("video worker did not produce live video")
            except BaseException:
                if self.log:
                    self.log.flush()
                    detail = (stream_dir / "worker.log").read_text(errors="replace")[-2000:]
                    print("Video startup failed: " + redact(detail, self.camera.c), file=sys.stderr, flush=True)
                    if looks_like_auth_failure(detail):
                        self._stop_worker()
                        raise CameraAuthError("camera rejected the credentials")
                self._stop_worker()
                raise

    def status(self, measure=False):
        with self.state_lock:
            active = bool(self.process and self.process.poll() is None)
            stream_dir = self.stream_dir if active else None
            result = {"active": active, "profile": self.profile, "playlist": f"/media/{self.stream_dir.name}/index.m3u8" if active else None}
            if active and self.live and self.live.initialization and not self.live.closed:
                result.update(live=f"/live/{self.stream_dir.name}.mp4", mime=self.live.mime)
        if measure and stream_dir:
            result["source"] = self._measure_latest_segment(stream_dir)
        return result

    def live_media(self, stream_id):
        with self.state_lock:
            if not self.stream_dir or stream_id != self.stream_dir.name or not self.live or self.live.closed:
                raise FileNotFoundError()
            return self.live

    def touch(self):
        with self.state_lock:
            self.last_access = time.monotonic()

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
        self.auth_failures = {}
        self.auth_failure_ids = {}
        self.error_lock = threading.Lock()
        self.credentials_store = None

    def _mark_auth_failed(self, camera_id):
        with self.error_lock:
            if camera_id not in self.auth_failures:
                self.auth_failure_ids[camera_id] = self.auth_failure_ids.get(camera_id, 0) + 1
            self.auth_failures[camera_id] = True
            self.errors[camera_id] = "Camera rejected the saved credentials"

    def clear_auth_failure(self, camera_id):
        with self.error_lock:
            self.auth_failures.pop(camera_id, None)
            self.errors.pop(camera_id, None)

    def auth_failed_ids(self):
        with self.error_lock:
            return [camera_id for camera_id in self.cameras if self.auth_failures.get(camera_id)]

    def default_profile(self, camera_id):
        """Configured default, else the profile with the most pixels."""
        camera = self.cameras[camera_id]
        configured = camera.c.get("default_profile")
        profiles = self.get(camera_id).profiles()
        tokens = [item["token"] for item in profiles]
        if configured in tokens:
            return configured
        def pixels(item):
            video = item.get("VideoEncoderConfiguration", {})
            try:
                return int(video.get("Width", 0)) * int(video.get("Height", 0))
            except (TypeError, ValueError):
                return 0
        if not tokens:
            raise ValueError("camera reports no profiles")
        return max(profiles, key=pixels)["token"]

    def verify_credentials(self, camera_id, username, password):
        """Test credentials with a throwaway ONVIF client; never touches the live worker."""
        candidate = Camera(dict(self.cameras[camera_id].c, username=username, password=password))
        candidate.profiles()
        return True

    def set_credentials(self, camera_id, username, password):
        self.cameras[camera_id].c["username"] = username
        self.cameras[camera_id].c["password"] = password
        self.clear_auth_failure(camera_id)
        if self.credentials_store:
            self.credentials_store.save(camera_id, username, password)

    def try_credentials(self, username, password, camera_ids=None):
        """Apply a verified username/password to every camera that rejected the stored one."""
        fixed = []
        for camera_id in (camera_ids if camera_ids is not None else self.auth_failed_ids()):
            if camera_id not in self.cameras:
                continue
            try:
                self.verify_credentials(camera_id, username, password)
            except (OSError, ValueError):
                continue
            self.set_credentials(camera_id, username, password)
            fixed.append(camera_id)
        return fixed

    def apply(self, cameras, directory):
        """Reload: add new cameras, drop removed ones, keep unchanged workers running."""
        added, removed = [], []
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) for name in cameras) or not cameras:
            raise ValueError("invalid camera IDs")
        for name in list(self.cameras):
            if name not in cameras:
                self.workers[name].stop()
                self.workers.pop(name); self.cameras.pop(name)
                removed.append(name)
        for name, camera in cameras.items():
            if name in self.cameras:
                current = self.cameras[name].c
                if all(current.get(key) == value for key, value in camera.c.items() if key not in ("username", "password")):
                    continue
                self.workers[name].stop()
                self.workers.pop(name); self.cameras.pop(name)
            self.cameras[name] = camera
            self.workers[name] = Streams(camera, Path(directory).expanduser() / name)
            added.append(name)
        return added, removed

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
        except CameraAuthError:
            self._mark_auth_failed(camera_id)
            raise
        except (OSError, ValueError):
            with self.error_lock:
                self.errors[camera_id] = "Camera operation failed"
            raise
        if operation != "status":
            with self.error_lock:
                self.errors.pop(camera_id, None)
                self.auth_failures.pop(camera_id, None)
        if operation in ("status", "start"):
            result = dict(result)
            if result.get("playlist"):
                result["playlist"] = result["playlist"].replace("/media/", f"/media/{camera_id}/", 1)
            if result.get("live"):
                result["live"] = result["live"].replace("/live/", f"/live/{camera_id}/", 1)
        return result

    def summary(self):
        result = []
        for camera_id, camera in self.cameras.items():
            state = dict(self.get(camera_id).status())
            # Inventory/status does not contact a camera or acquire its RPC lock.
            state.pop("playlist", None)
            with self.error_lock:
                error = self.errors.get(camera_id)
            result.append({"id": camera_id, "name": camera.c.get("name", camera_id), **state, "error": error,
                           "auth_failed": bool(self.auth_failures.get(camera_id)),
                           "auth_failure_id": self.auth_failure_ids.get(camera_id, 0)})
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


class SessionStore:
    """Opaque browser sessions; only token digests are kept.

    Normal sessions live in memory and end on restart. Watcher sessions are also
    stored in the account database so an unattended display survives a restart.
    """
    IDLE_SECONDS = 60 * 60
    ABSOLUTE_SECONDS = 3 * 60 * 60
    MAX_SESSIONS = 512

    def __init__(self, users):
        self.users = users
        self.persist = getattr(users, "persist_sessions", False) is True
        self.lock = threading.RLock()
        self.items = {}

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def issue(self, user, address, extra=None):
        current = self.users.session_user(user["username"], address)
        if current is None:
            return None
        token = secrets.token_urlsafe(32)
        key = self.digest(token)
        csrf = secrets.token_urlsafe(32)
        now = time.monotonic()
        watcher = bool(current.get("watcher", False))
        with self.lock:
            expired = [session_key for session_key, item in self.items.items()
                       if not item["watcher"] and
                       (now - item["activity"] >= self.IDLE_SECONDS or now - item["created"] >= self.ABSOLUTE_SECONDS)]
            for session_key in expired:
                del self.items[session_key]
            if watcher:
                # A kiosk that signs in repeatedly replaces its own oldest session.
                own = [k for k, item in self.items.items() if item["username"] == user["username"] and item["watcher"]]
                own.sort(key=lambda k: self.items[k]["created"])
                for old in own[:max(0, len(own) - (UserStore.WATCHER_SESSIONS_PER_USER - 1))]:
                    del self.items[old]
            if len(self.items) >= self.MAX_SESSIONS:
                return None
            self.items[key] = {"username": user["username"], "address": str(address), "csrf": csrf,
                               "created": now, "activity": now, "watcher": watcher,
                               "version": current.get("session_version"), **(extra or {})}
        if watcher and self.persist:
            for old in self.users.persist_session(key, user["username"], address, csrf, current.get("session_version")):
                with self.lock:
                    self.items.pop(old, None)
        return token, csrf, current

    def revoke(self, key):
        with self.lock:
            self.items.pop(key, None)
        if self.persist:
            self.users.delete_session(key)

    def revoke_user(self, username):
        with self.lock:
            for key in [key for key, item in self.items.items() if item["username"] == username]:
                del self.items[key]
        if self.persist:
            self.users.delete_user_sessions(username)

    def _restore(self, key, address):
        """Load a persisted watcher session after a restart."""
        row = self.users.load_session(key)
        if not row or row["address"] != str(address):
            return None
        now = time.monotonic()
        item = {"username": row["username"], "address": row["address"], "csrf": row["csrf"], "created": now,
                "activity": now, "watcher": True, "version": row["session_version"]}
        with self.lock:
            if len(self.items) >= self.MAX_SESSIONS:
                return None
            self.items[key] = item
        return item

    def get(self, token, address):
        if not token or len(token) > 128:
            return None
        try:
            key = self.digest(token)
        except UnicodeEncodeError:
            return None
        now = time.monotonic()
        with self.lock:
            item = self.items.get(key)
            if item is None and self.persist:
                item = self._restore(key, address)
            if not item or item["address"] != str(address):
                return None
            user = self.users.session_user(item["username"], address)
            expired = (user is None or item.get("version") != user.get("session_version") or
                       (not user.get("watcher") and
                        (now - item["activity"] >= self.IDLE_SECONDS or now - item["created"] >= self.ABSOLUTE_SECONDS)))
            if expired:
                self.items.pop(key, None)
                if self.persist:
                    self.users.delete_session(key)
                return None
            return key, item, user

    def activity(self, token, address):
        current = self.get(token, address)
        if not current:
            return None
        key, item, user = current
        with self.lock:
            if key not in self.items:
                return None
            item["activity"] = time.monotonic()
        return key, item, user


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def get_request(self):
        # Do not handshake here: this runs on the accept thread, so one silent
        # client would stall every other connection until the timeout.
        request, address = super().get_request()
        request.settimeout(15)
        try:
            return self.tls_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False), address
        except BaseException:
            request.close()
            raise

    def verify_request(self, request, client_address):
        """Drop peers outside the allowlist before any TLS work."""
        try:
            peer = ipaddress.ip_address(client_address[0])
            if any(peer in network for network in self.trusted_proxies):
                return True
            return self.users.is_global_network_allowed(peer)
        except (ValueError, OSError):
            return False

    def handle_error(self, request, client_address):
        # TLS failures and resets from scanners are expected noise; real bugs are not.
        if not isinstance(sys.exc_info()[1], OSError):
            super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def setup(self):
        handshake = getattr(self.request, "do_handshake", None)
        if handshake:
            handshake()
        super().setup()

    def respond(self, code, body=b"", content_type="application/json", extra_headers=(), cache_control="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if code != 304:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # The browser can cancel a slow camera operation while it is running.
            pass

    def respond_clip(self, path, camera_id, clip_id, display_name=None):
        size = path.stat().st_size
        start, end, code = 0, size - 1, 200
        request_range = self.headers.get("Range")
        if request_range:
            if len(request_range) > 128:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", request_range.strip())
            if not match or not any(match.groups()):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            first, last = match.groups()
            if first:
                start = int(first)
                end = int(last) if last else size - 1
            else:
                suffix = int(last)
                start = max(0, size - suffix)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, size - 1)
            code = 206
        length = end - start + 1
        suffix = path.suffix.lower()
        mime = "video/mp4" if suffix == ".mp4" else "video/x-matroska"
        filename = re.sub(r"[^A-Za-z0-9_.-]", "_", display_name or f"{camera_id}_{clip_id}")[:120]
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Disposition", f'inline; filename="{filename}{suffix}"')
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except OSError:
            # Range reads are cancelled when a browser seeks or closes its player.
            self.close_connection = True

    def respond_live(self, camera_id, stream_id):
        worker = self.server.cameras.get(camera_id)
        live = worker.live_media(stream_id)
        if not live.clients.acquire(blocking=False):
            self.respond(429, b'{"error":"Too many live viewers."}')
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            check_at = 0
            for chunk in live.chunks():
                if time.monotonic() >= check_at:
                    session = self.server.sessions.get(self.session_token, self.get_client_address())
                    if not session or not self.server.users.can(session[2], camera_id, "view"):
                        break
                    check_at = time.monotonic() + 2
                worker.touch()
                if chunk:
                    self.wfile.write(chunk)
        except (OSError, ValueError):
            # A cancelled or slow browser is local to this subscriber.
            self.close_connection = True
        finally:
            live.clients.release()

    def request_token(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            return cookie["watchpost_session"].value if "watchpost_session" in cookie else None
        except (KeyError, ValueError):
            return None

    def get_client_address(self):
        peer = ipaddress.ip_address(self.client_address[0])
        if not any(peer in network for network in self.server.trusted_proxies):
            return peer
        forwarded = self.headers.get("X-Forwarded-For", "")
        values = [value.strip() for value in forwarded.split(",") if value.strip()]
        if len(values) != 1:
            raise ValueError("trusted proxy must send one canonical client address")
        return ipaddress.ip_address(values[0])

    def authorized(self):
        try:
            ip = self.get_client_address()
        except ValueError:
            self.respond(400); return False
        if not self.server.users.is_global_network_allowed(ip):
            self.respond(403); return False
        now = time.monotonic()
        with self.server.auth_lock:
            attempts = self.server.failures[str(ip)]
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(attempts) >= 12:
                self.respond(429); return False
        token = self.request_token()
        session = self.server.sessions.get(token, ip)
        if not session:
            self.respond(401, b'{"error":"Your session has expired. Sign in again."}')
            return False
        self.session_key, _, self.auth_user = session
        self.session_token = token
        return True

    def valid_origin(self):
        origin = self.headers.get("Origin", "")
        try:
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment
                    or parsed.username is not None or parsed.password is not None):
                return False
            return parsed.netloc == self.headers.get("Host", "") or origin in self.server.allowed_origins
        except ValueError:
            return False

    def valid_csrf(self):
        current = self.server.sessions.get(self.request_token(), self.get_client_address())
        return bool(current and hmac.compare_digest(current[1]["csrf"], self.headers.get("X-CSRF-Token", "")))

    def can_camera(self, camera_id, action):
        if not self.server.users.can(self.auth_user, camera_id, action):
            self.respond(403); return False
        return True

    def require_admin_quiet(self):
        return "web_admin" in self.auth_user.get("roles", [self.auth_user.get("role")])

    def require_web_admin(self):
        if "web_admin" not in self.auth_user.get("roles", [self.auth_user.get("role")]):
            self.respond(403); return False
        return True

    def do_GET(self):
        path = urlsplit(self.path).path
        # These files contain application code only, never account or camera data.
        # Keep them cacheable even when a user's authentication expires.
        static = {"/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        if path in static or path in ("/vendor/hls.min.js", "/login.js", "/watch.js"):
            try:
                if path == "/vendor/hls.min.js":
                    asset, mime = self.server.hls_js, "text/javascript"
                else:
                    name, mime = (path[1:], "text/javascript") if path in ("/login.js", "/watch.js") else static[path]
                    asset = Path(__file__).parent / "static" / name
                body = asset.read_bytes()
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
                cached = self.headers.get("If-None-Match") in (etag, "*")
                self.respond(304 if cached else 200, b"" if cached else body, mime,
                             extra_headers=(("ETag", etag),),
                             cache_control="public, max-age=0, must-revalidate")
            except OSError:
                self.respond(404)
            return
        if path == "/login":
            self.respond(200, (Path(__file__).parent / "static" / "login.html").read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/auth/status":
            try:
                ip = self.get_client_address()
            except ValueError:
                self.respond(400); return
            session = self.server.sessions.get(self.request_token(), ip)
            if not session:
                self.respond(401, b'{"error":"Sign in required."}'); return
            self.respond(200, json.dumps({"username": session[2]["username"], "watcher": session[2].get("watcher", False),
                                          "csrf_token": session[1]["csrf"], "admin": "web_admin" in session[2].get("roles", []),
                                          "password_matches_camera": bool(session[1].get("password_matches_camera"))}).encode()); return
        if path in ("/", "/watch"):
            try:
                ip = self.get_client_address()
            except ValueError:
                self.respond(400); return
            session = self.server.sessions.get(self.request_token(), ip)
            if not session:
                self.send_response(302)
                self.send_header("Location", "/login")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.session_key, _, self.auth_user = session
            self.session_token = self.request_token()
        elif not self.authorized():
            return
        try:
            if path == "/":
                self.respond(200, (Path(__file__).parent / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/watch":
                self.respond(200, (Path(__file__).parent / "static" / "watch.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/cameras":
                visible = [item | {"can_control": self.server.users.can(self.auth_user, item["id"], "control")} for item in self.server.cameras.summary() if self.server.users.can(self.auth_user, item["id"], "view")]
                recording_enabled = bool(getattr(self.server.clips, "enabled", False))
                visible = [item | {"can_record": recording_enabled and self.server.users.can(self.auth_user, item["id"], "record"),
                                   "can_start": self.server.users.can(self.auth_user, item["id"], "start")} for item in visible]
                if not self.require_admin_quiet():
                    visible = [{k: v for k, v in item.items() if k not in ("auth_failure_id",)} | {"auth_failed": False} for item in visible]
                self.respond(200, json.dumps(visible).encode())
            elif path == "/api/media/clips":
                query = parse_qs(urlsplit(self.path).query)
                hidden = query.get("hidden", ["0"])[0] == "1"
                result = []
                for camera in self.server.cameras.cameras:
                    if not self.server.users.can(self.auth_user, camera, "view"):
                        continue
                    if hidden and not self.server.users.can_manage_hidden(self.auth_user, camera):
                        continue
                    clips = self.server.clips.list_clips(camera, include_hidden=hidden, hidden_only=hidden)
                    for clip in clips:
                        clip["can_delete"] = self.server.users.can_manage_clip(self.auth_user, camera, clip.get("owner"))
                        clip["can_hide"] = self.server.users.can_manage_hidden(self.auth_user, camera)
                        result.append(clip)
                result.sort(key=lambda item: item.get("started_at", ""), reverse=True)
                is_admin = any(self.server.users.can_manage_hidden(self.auth_user, camera) for camera in self.server.cameras.cameras)
                self.respond(200, json.dumps({"clips": result, "can_view_hidden": is_admin}).encode())
            elif path == "/api/admin/users":
                if self.require_web_admin():
                    self.respond(200, json.dumps(self.server.users.inventory()).encode())
            elif path == "/api/admin/roles":
                if self.require_web_admin():
                    self.respond(200, json.dumps(self.server.users.role_options()).encode())
            elif path == "/api/admin/networks":
                if self.require_web_admin():
                    detail = parse_qs(urlsplit(self.path).query).get("detail", ["0"])[0] == "1"
                    self.respond(200, json.dumps(self.server.users.list_networks(detail)).encode())
            elif re.fullmatch(r"/live/[A-Za-z0-9_-]+/[a-f0-9]{32}\.mp4", path):
                camera_id, filename = path.split("/")[2:]
                if not self.can_camera(camera_id, "view"): return
                self.respond_live(camera_id, filename[:-4])
            elif re.fullmatch(r"/api/cameras/[^/]+/(profiles|status)", path):
                _, _, _, camera_id, operation = path.split("/")
                if not self.can_camera(camera_id, "view"): return
                self.respond(200, json.dumps(self.server.cameras.invoke(camera_id, operation)).encode())
            elif re.fullmatch(r"/api/cameras/[^/]+/clips(?:/[a-f0-9]{32})?", path):
                parts = path.split("/")
                camera_id = parts[3]
                if not self.can_camera(camera_id, "view"): return
                if len(parts) == 5:
                    query = parse_qs(urlsplit(self.path).query)
                    hidden = query.get("hidden", ["0"])[0] == "1"
                    if hidden and not self.server.users.can_manage_hidden(self.auth_user, camera_id):
                        self.respond(403); return
                    recent = query.get("recent", ["0"])[0] == "1"
                    clips = self.server.clips.list_clips(camera_id, include_hidden=hidden, hidden_only=hidden,
                        since_seconds=24 * 60 * 60 if recent else None, limit=10 if recent else None)
                    for clip in clips:
                        clip["can_delete"] = self.server.users.can_manage_clip(self.auth_user, camera_id, clip.get("owner"))
                        clip["can_hide"] = self.server.users.can_manage_hidden(self.auth_user, camera_id)
                    self.respond(200, json.dumps(clips).encode())
                else:
                    self.respond(200, json.dumps(self.server.clips.status(camera_id, parts[5], self.auth_user["username"])).encode())
            elif re.fullmatch(r"/api/cameras/[^/]+/recording", path):
                camera_id = path.split("/")[3]
                try:
                    self.server.cameras.get(camera_id)
                except KeyError:
                    self.respond(404); return
                if not self.can_camera(camera_id, "view"): return
                session_id = parse_qs(urlsplit(self.path).query).get("session_id", [None])[0]
                self.respond(200, json.dumps(self.server.clips.status(camera_id, session_id, self.auth_user["username"])).encode())
            elif re.fullmatch(r"/recordings/[^/]+/[a-f0-9]{32}", path):
                _, _, camera_id, clip_id = path.split("/")
                if not self.can_camera(camera_id, "view"): return
                metadata = self.server.clips.clip_metadata(camera_id, clip_id)
                if metadata.get("hidden_at") and not self.server.users.can_manage_hidden(self.auth_user, camera_id):
                    self.respond(404); return
                self.respond_clip(self.server.clips.clip_path(camera_id, clip_id), camera_id, clip_id, metadata.get("display_name"))
            elif path == "/api/profiles":
                if not self.can_camera(self.server.cameras.default_camera, "view"): return
                self.respond(200, json.dumps(self.server.cameras.invoke(self.server.cameras.default_camera, "profiles")).encode())
            elif path == "/api/status":
                if not self.can_camera(self.server.cameras.default_camera, "view"): return
                self.respond(200, json.dumps(self.server.cameras.invoke(self.server.cameras.default_camera, "status")).encode())
            elif path.startswith("/media/"):
                parts = path.split("/")
                if len(parts) == 4:
                    if not self.can_camera(self.server.cameras.default_camera, "view"): return
                    worker, stream_id, filename = self.server.streams, parts[2], parts[3]
                elif len(parts) == 5:
                    if not self.can_camera(parts[2], "view"): return
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
        path = urlsplit(self.path).path
        if path == "/api/auth/login":
            self.login()
            return
        if not self.authorized(): return
        if self.headers.get("X-Camera-Client") != "1" or not self.valid_origin() or not self.valid_csrf():
            self.respond(403); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 16384:
                self.respond(413, b'{"error":"Request is too large."}'); return
            if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
                self.respond(400, b'{"error":"Send a valid JSON request."}'); return
            try:
                data = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, RecursionError):
                self.respond(400, b'{"error":"Send valid, shallow JSON."}'); return
            if not isinstance(data, dict):
                self.respond(400, b'{"error":"The request must be a JSON object."}'); return
            if path == "/api/auth/logout":
                self.server.sessions.revoke(self.session_key)
                self.respond(200, b'{"logged_out":true}', extra_headers=(("Set-Cookie", "watchpost_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"),)); return
            if path == "/api/auth/activity":
                if not self.server.sessions.activity(self.session_token, self.get_client_address()):
                    self.respond(401, b'{"error":"Your session has expired. Sign in again."}'); return
                self.respond(200, b'{"active":true}'); return
            scoped = re.fullmatch(r"/api/cameras/([^/]+)/(start|stop|ptz)", path)
            if scoped:
                camera_id, operation = scoped.groups()
                try:
                    self.server.cameras.get(camera_id)
                except KeyError:
                    self.respond(404); return
                if operation == "start" and not self.server.users.can(self.auth_user, camera_id, "control"):
                    # Viewers may start an idle stream, always on the default profile.
                    if not self.can_camera(camera_id, "start"): return
                    current = self.server.cameras.invoke(camera_id, "status")
                    result = current if current.get("active") else self.server.cameras.invoke(
                        camera_id, "start", self.server.cameras.default_profile(camera_id))
                    self.respond(200, json.dumps(result).encode()); return
                if not self.can_camera(camera_id, "control"): return
                if operation == "start":
                    result = self.server.cameras.invoke(camera_id, "start", data["profile"])
                elif operation == "stop":
                    self.server.cameras.invoke(camera_id, "stop"); result = {"active": False}
                else:
                    result = self.server.cameras.invoke(camera_id, "ptz", float(data.get("pan", 0)), float(data.get("tilt", 0)), float(data.get("seconds", 0.2)), bool(data.get("stop", False)))
            elif re.fullmatch(r"/api/cameras/[^/]+/clips/[a-f0-9]{32}/delete", path):
                parts = path.split("/")
                camera_id = parts[3]
                try:
                    self.server.cameras.get(camera_id)
                except KeyError:
                    self.respond(404); return
                if not self.can_camera(camera_id, "view"): return
                try:
                    metadata = self.server.clips.clip_metadata(camera_id, parts[5])
                    if not self.server.users.can_manage_clip(self.auth_user, camera_id, metadata.get("owner")):
                        self.respond(403); return
                    self.server.clips.delete_clip(camera_id, parts[5])
                    self.respond(200, b'{"deleted":true}'); return
                except (ValueError, OSError) as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
                except KeyError:
                    self.respond(404); return
            elif re.fullmatch(r"/api/cameras/[^/]+/clips/[a-f0-9]{32}/(hide|restore)", path):
                parts = path.split("/")
                camera_id, clip_id, operation = parts[3], parts[5], parts[6]
                try:
                    if not self.server.users.can_manage_hidden(self.auth_user, camera_id):
                        self.respond(403); return
                    self.server.clips.set_hidden(camera_id, clip_id, operation == "hide", self.auth_user["username"])
                    self.respond(200, json.dumps({"hidden": operation == "hide"}).encode()); return
                except KeyError:
                    self.respond(404); return
                except (ValueError, OSError) as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
            elif re.fullmatch(r"/api/media/clips/[^/]+/[a-f0-9]{32}/(hide|restore|delete)", path):
                parts = path.split("/")
                camera_id, clip_id, operation = parts[4], parts[5], parts[6]
                if not self.can_camera(camera_id, "view"): return
                try:
                    metadata = self.server.clips.clip_metadata(camera_id, clip_id)
                    if operation == "delete":
                        if not self.server.users.can_manage_clip(self.auth_user, camera_id, metadata.get("owner")):
                            self.respond(403); return
                        self.server.clips.delete_clip(camera_id, clip_id)
                    else:
                        if not self.server.users.can_manage_hidden(self.auth_user, camera_id):
                            self.respond(403); return
                        self.server.clips.set_hidden(camera_id, clip_id, operation == "hide", self.auth_user["username"])
                    self.respond(200, json.dumps({"updated": True}).encode()); return
                except KeyError:
                    self.respond(404); return
                except (ValueError, OSError) as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
            elif re.fullmatch(r"/api/cameras/[^/]+/recording/(start|stop)", path):
                camera_id, operation = path.split("/")[3], path.rsplit("/", 1)[1]
                try:
                    self.server.cameras.get(camera_id)
                except KeyError:
                    self.respond(404); return
                if not self.can_camera(camera_id, "record"): return
                try:
                    if operation == "start":
                        result = self.server.clips.start(camera_id, data.get("profile"), data.get("max_seconds"), self.auth_user["username"])
                    else:
                        result = self.server.clips.stop(camera_id, data.get("session_id"), self.auth_user["username"])
                except (ValueError, OSError) as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
                except KeyError:
                    self.respond(404); return
                self.respond(202, json.dumps(result).encode()); return
            elif path == "/api/admin/users":
                if not self.require_web_admin(): return
                try:
                    self.server.users.create_user(
                        data.get("username"), data.get("password"), data.get("role"),
                        data.get("camera_permissions", {}), data.get("networks"),
                        list(self.server.cameras.cameras), roles=data.get("roles"), capabilities=data.get("capabilities"),
                        record=data.get("can_record"), camera_link=bool(data.get("camera_link", False)),
                    )
                except ValueError as error:
                    self.respond(400, json.dumps({"error": str(error)}).encode()); return
                except sqlite3.IntegrityError:
                    self.respond(409, b'{"error":"That username already exists."}'); return
                result = {"created": True}
            elif path == "/api/admin/users/access":
                if not self.require_web_admin(): return
                try:
                    self.server.users.update_access(
                        data.get("username"), data.get("role"), data.get("camera_permissions", {}),
                        data.get("networks"), list(self.server.cameras.cameras), data.get("password") or None,
                        roles=data.get("roles"), capabilities=data.get("capabilities"),
                        record=data.get("can_record"), camera_link=data.get("camera_link"),
                    )
                except ValueError as error:
                    self.respond(400, json.dumps({"error": str(error)}).encode()); return
                except KeyError:
                    self.respond(404, b'{"error":"User not found."}'); return
                self.server.sessions.revoke_user(data.get("username"))
                result = {"updated": True}
            elif path in ("/api/admin/users/disable", "/api/admin/users/enable"):
                if not self.require_web_admin(): return
                username = data.get("username")
                if username == self.auth_user["username"] and path.endswith("disable"):
                    self.respond(409, b'{"error":"You cannot disable your current account."}'); return
                try:
                    self.server.users.set_enabled(username, path.endswith("enable"))
                except ValueError as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
                except KeyError:
                    self.respond(404, b'{"error":"User not found."}'); return
                if path.endswith("disable"):
                    self.server.sessions.revoke_user(username)
                result = {"updated": True}
            elif path == "/api/admin/networks/add":
                if not self.require_web_admin(): return
                try:
                    self.server.users.add_networks(data.get("cidrs", data.get("cidr")))
                except ValueError as error:
                    self.respond(400, json.dumps({"error": str(error)}).encode()); return
                result = {"added": True}
            elif path == "/api/admin/networks/remove":
                if not self.require_web_admin(): return
                try:
                    self.server.users.remove_network(data.get("cidr"), self.get_client_address())
                except ValueError as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
                result = {"removed": True}
            elif path == "/api/admin/cameras/credentials":
                if not self.require_web_admin(): return
                username, password = data.get("username"), data.get("password")
                if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
                    self.respond(400, b'{"error":"Enter the camera username and password."}'); return
                target = data.get("camera_id")
                registry = self.server.cameras
                if target == "all":
                    ids = registry.auth_failed_ids()
                elif isinstance(target, str) and target in registry.cameras:
                    ids = [target]
                else:
                    self.respond(404); return
                fixed = registry.try_credentials(username, password, ids)
                if not fixed:
                    self.respond(409, b'{"error":"No camera accepted these credentials."}'); return
                result = {"updated": fixed}
            elif path == "/api/start":
                if not self.can_camera(self.server.cameras.default_camera, "control"): return
                result = self.server.cameras.invoke(self.server.cameras.default_camera, "start", data["profile"])
            elif path == "/api/stop":
                if not self.can_camera(self.server.cameras.default_camera, "control"): return
                self.server.cameras.invoke(self.server.cameras.default_camera, "stop"); result = {"active": False}
            elif path == "/api/ptz":
                if not self.can_camera(self.server.cameras.default_camera, "control"): return
                result = self.server.cameras.invoke(self.server.cameras.default_camera, "ptz", float(data.get("pan", 0)), float(data.get("tilt", 0)), float(data.get("seconds", 0.2)), bool(data.get("stop", False)))
            else:
                self.respond(404); return
            self.respond(200, json.dumps(result).encode())
        except (OSError, ValueError, KeyError, TypeError, sqlite3.IntegrityError):
            self.respond(400, b'{"error":"Request failed; check camera availability and parameters"}')

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 16384 or self.headers.get_content_type() != "application/json":
            raise ValueError("Send a valid JSON request.")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("The request must be a JSON object.")
        return data

    def bootstrap_if_needed(self, username, password):
        """Create the root web administrator from any camera that accepts the credentials."""
        users, registry = self.server.users, self.server.cameras
        if not users.needs_bootstrap():
            return
        with self.server.bootstrap_lock:
            if not users.needs_bootstrap():
                return
            for camera_id, camera in registry.cameras.items():
                stored = camera.c
                same_user = hmac.compare_digest(str(stored.get("username", "")).encode(), username.encode())
                same_password = hmac.compare_digest(str(stored.get("password", "")).encode(), password.encode())
                if not (same_user and same_password):
                    continue
                try:
                    registry.get(camera_id).profiles()
                except (OSError, ValueError):
                    continue
                users.bootstrap_admin(username, password)
                return

    def login(self):
        if not self.valid_origin() or self.headers.get("X-Camera-Client") != "1":
            self.respond(403); return
        try:
            ip = self.get_client_address()
        except ValueError:
            self.respond(400); return
        if not self.server.users.is_global_network_allowed(ip):
            self.respond(403); return
        now = time.monotonic()
        with self.server.auth_lock:
            attempts = self.server.failures[str(ip)]
            while attempts and now - attempts[0] > 60:
                attempts.popleft()
            if len(self.server.failures) > 1024:
                for key in [key for key, value in self.server.failures.items() if not value]:
                    del self.server.failures[key]
            if len(attempts) >= 12:
                self.respond(429, b'{"error":"Too many failed sign-in attempts."}'); return
        try:
            data = self.read_json()
            username, password = data.get("username"), data.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError("Enter a username and password.")
            if not self.server.auth_hash_semaphore.acquire(blocking=False):
                self.respond(429, b'{"error":"Sign-in is busy. Try again shortly."}'); return
            try:
                self.bootstrap_if_needed(username, password)
                user = self.server.users.authenticate(username, password, ip)
            finally:
                self.server.auth_hash_semaphore.release()
        except (ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
            user = None
        if user and not user.get("source_allowed"):
            self.respond(403, b'{"error":"This source address is not allowed for the account."}'); return
        if not user:
            with self.server.auth_lock:
                self.server.failures[str(ip)].append(now)
            self.respond(401, b'{"error":"Invalid sign-in or source address."}'); return
        # In-memory comparison only; the password is not stored.
        matches = any(hmac.compare_digest(str(c.c.get("password", "")).encode(), password.encode())
                      for c in self.server.cameras.cameras.values())
        issued = self.server.sessions.issue(user, ip, {"password_matches_camera": matches})
        if not issued:
            self.respond(429, b'{"error":"Session could not be created. Try again later."}'); return
        token, csrf, current = issued
        if current.get("camera_link") and self.server.cameras.auth_failed_ids():
            threading.Thread(target=self.server.cameras.try_credentials, args=(username, password), daemon=True).start()
        if current.get("watcher"):
            max_age = "; Max-Age=7776000"
        else:
            max_age = "; Max-Age=10800"
        cookie = f"watchpost_session={token}; Path=/; Secure; HttpOnly; SameSite=Strict{max_age}"
        self.respond(200, json.dumps({"username": current["username"], "watcher": current.get("watcher", False), "csrf_token": csrf}).encode(),
                     extra_headers=(("Set-Cookie", cookie),))


def _https_origins(origins):
    if not isinstance(origins, (list, tuple)):
        raise ValueError("web.allowed_origins must be a list of canonical HTTPS origins without paths")
    result = set()
    for origin in origins:
        try:
            if not isinstance(origin, str):
                raise ValueError
            parsed = urlsplit(origin)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.netloc != parsed.netloc.lower()
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError
            _ = parsed.port  # Reject malformed ports during startup, not on a request.
        except (TypeError, ValueError) as error:
            raise ValueError("web.allowed_origins must be a list of canonical HTTPS origins without paths") from error
        result.add(origin)
    return result


def apply_credential_overrides(cameras, store):
    """Saved credentials replace the env-file values for the cameras they name."""
    for camera_id, value in store.load().items():
        camera = cameras.get(camera_id)
        if camera is not None and isinstance(getattr(camera, "c", None), dict):
            camera.c["username"], camera.c["password"] = value["username"], value["password"]


def reload_config(server, config_path):
    """Apply networks, origins, proxies and the camera list without a restart."""
    config = tomllib.loads(Path(config_path).read_text())
    web = config["web"]
    cameras = {name: Camera(load_config(config_path, name)) for name in config["cameras"]}
    apply_credential_overrides(cameras, server.cameras.credentials_store)
    origins = _https_origins(web.get("allowed_origins", []))
    proxies = [ipaddress.ip_network(value, strict=False) for value in web.get("trusted_proxies", [])]
    server.users.reconcile_config_networks(web["allowed_networks"])
    server.allowed_origins, server.trusted_proxies = origins, proxies
    with server.clips.lock:
        recording = {s["camera_id"] for s in server.clips.sessions.values() if s["state"] == "recording"}
    for name in recording:
        if name not in cameras and name in server.cameras.cameras:
            cameras[name] = server.cameras.cameras[name]
    return server.cameras.apply(cameras, web.get("cache_dir"))


def make_server(camera, web, streams=None, default_camera="cam01", recording=None):
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
    server.clips = ClipManager(server.cameras, recording or {})
    database = web.get("auth_database", "~/.local/state/yoosee-cam-utils/viewer-users.sqlite3")
    server.users = UserStore(database, web["allowed_networks"])
    registry.credentials_store = CredentialStore(Path(database).expanduser().with_name("camera-credentials.json"))
    apply_credential_overrides(cameras, registry.credentials_store)
    server.bootstrap_lock = threading.Lock()
    try:
        server.trusted_proxies = [ipaddress.ip_network(value, strict=False) for value in web.get("trusted_proxies", [])]
    except (TypeError, ValueError) as error:
        raise ValueError("web.trusted_proxies must contain IP addresses or CIDRs") from error
    server.allowed_origins = _https_origins(web.get("allowed_origins", []))
    server.auth_lock = threading.Lock()
    server.failures = defaultdict(deque)
    server.auth_hash_semaphore = threading.BoundedSemaphore(2)
    server.sessions = SessionStore(server.users)
    server.hls_js = asset
    return server


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("users", "networks"):
        from .admin_cli import run
        return run(argv)
    p = argparse.ArgumentParser(description=__doc__, epilog="Account commands: yoosee-web users|networks --help")
    p.add_argument("--config", required=True)
    p.add_argument("--camera", help="Default camera for legacy viewer routes (all configured cameras remain available)")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    config = tomllib.loads(Path(a.config).read_text())
    camera_ids = list(config["cameras"])
    default_camera = a.camera or ("cam01" if "cam01" in camera_ids else camera_ids[0] if camera_ids else "")
    if default_camera not in camera_ids:
        p.error("unknown default camera")
    cameras = {name: Camera(load_config(a.config, name)) for name in camera_ids}
    web = config["web"]
    if a.dry_run:
        recording = config.get("recording", {})
        print(json.dumps({"bind": web["bind"], "port": web["port"], "tls": True, "auth": "cookie sessions", "allowed_networks": web["allowed_networks"], "cameras": camera_ids, "default_camera": default_camera, "manual_recording": {"enabled": bool(recording.get("enabled", False)), "max_bytes": int(recording.get("max_bytes", 2 * 1024**3)), "reserve_bytes": int(recording.get("reserve_bytes", 5 * 1024**3)), "segment_seconds": int(recording.get("segment_seconds", 900)), "default_session_seconds": int(recording.get("default_session_seconds", 3600))}})); return 0
    server = make_server(cameras, web, default_camera=default_camera, recording=config.get("recording", {}))
    for note in server.users.migration_notes:
        print("Account migration: " + note, file=sys.stderr, flush=True)
    if server.users.needs_bootstrap():
        print("No accounts yet: the first sign-in with a configured camera's username and password creates the web administrator.", file=sys.stderr, flush=True)
    def reap():
        while True:
            time.sleep(10)
            server.cameras.reap_idle()
    threading.Thread(target=reap, daemon=True).start()
    reload_requested = threading.Event()
    def reloader():
        while True:
            reload_requested.wait()
            reload_requested.clear()
            try:
                added, removed = reload_config(server, a.config)
                print(f"Configuration reloaded; cameras added: {added or 'none'}, removed: {removed or 'none'}", file=sys.stderr, flush=True)
            except Exception as error:  # An invalid file must never stop the running service.
                print("Configuration reload failed; keeping the running configuration: " + redact(str(error), {}), file=sys.stderr, flush=True)
    threading.Thread(target=reloader, daemon=True).start()
    signal.signal(signal.SIGHUP, lambda *_: reload_requested.set())
    try:
        server.serve_forever()
    finally:
        try:
            server.clips.stop_all()
        finally:
            try:
                server.cameras.stop_all()
            finally:
                server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
