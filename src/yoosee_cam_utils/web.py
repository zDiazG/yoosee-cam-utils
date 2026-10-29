"""Authenticated LAN HTTPS viewer with independent, bounded camera workers."""
import argparse
import base64
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
import sqlite3
import stat
import ssl
import subprocess
import sys
import threading
import time
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import uuid

from .cli import Camera, load_config, media_input, redact, PTZ, SCHEMA
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
            args = ["ffmpeg", "-nostdin", "-v", "error", "-fflags", "+genpts", "-analyzeduration", "1000000", "-probesize", "262144", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp", "-f", "concat", "-safe", "0", "-i", "pipe:0",
                    "-map", "0:v:0", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-max_interleave_delta", "100000", "-hls_time", "2", "-hls_list_size", "6", "-hls_delete_threshold", "2", "-hls_flags", "delete_segments+independent_segments+omit_endlist", "-hls_segment_filename", str(stream_dir / "seg%09d.ts"), "-f", "hls", str(stream_dir / "index.m3u8"),
                    "-map", "0:v:0", "-map", "0:a?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k", "-max_interleave_delta", "100000", "-movflags", "+empty_moov+default_base_moof+frag_keyframe", "-frag_duration", "200000", "-flush_packets", "1", "-f", "mp4", "pipe:1"]
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


class UserStore:
    """Persistent viewer accounts and per-user camera/source grants."""

    ROLE_LABELS = {
        "viewer": "Viewer",
        "camera_admin": "Camera administrator",
        "web_admin": "Web administrator",
    }
    ROLES = tuple(ROLE_LABELS)
    MAX_ACCESS_ITEMS = 64
    MAX_PASSWORD_CHARS = 256

    def __init__(self, path, bootstrap_username, bootstrap_password, camera_ids, networks):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            if self.path.is_symlink() or not self.path.is_file() or stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise ValueError("viewer account database permissions must be 0600")
        self.lock = threading.RLock()
        with self._session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('web_admin','camera_admin','viewer')),
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS user_cameras (
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    camera_id TEXT NOT NULL, permission TEXT NOT NULL CHECK(permission IN ('viewer','camera_admin')),
                    PRIMARY KEY(username,camera_id)
                );
                CREATE TABLE IF NOT EXISTS user_networks (
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    cidr TEXT NOT NULL, PRIMARY KEY(username,cidr)
                );
                CREATE TABLE IF NOT EXISTS allowed_networks (cidr TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS user_roles (
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    PRIMARY KEY(username,role)
                );
            """)
            # Additive migration: preserve the existing single-role grant for every
            # account, then let the newer UI store multiple grants independently.
            db.execute("INSERT OR IGNORE INTO user_roles(username,role) SELECT username,role FROM users")
            for network in networks:
                db.execute("INSERT OR IGNORE INTO allowed_networks(cidr) VALUES (?)", (str(ipaddress.ip_network(network, strict=False)),))
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                if not bootstrap_username or not bootstrap_password:
                    raise ValueError("bootstrap viewer administrator is missing")
                self._insert_user(db, bootstrap_username, bootstrap_password, "web_admin", camera_ids, [])
                for network in networks:
                    db.execute("INSERT OR IGNORE INTO user_networks(username,cidr) VALUES (?,?)", (bootstrap_username, str(ipaddress.ip_network(network, strict=False))))
        self.path.chmod(0o600)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _session(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @classmethod
    def _password_bytes(cls, password, minimum=0):
        if not isinstance(password, str) or len(password) < minimum or len(password) > cls.MAX_PASSWORD_CHARS:
            raise ValueError("Passwords must be within the allowed length.")
        try:
            encoded = password.encode("utf-8")
        except UnicodeError as error:
            raise ValueError("Password must use valid Unicode text.") from error
        if len(encoded) > 1024:
            raise ValueError("Password is too long when encoded.")
        return encoded

    @classmethod
    def _hash(cls, password, salt):
        return hashlib.scrypt(cls._password_bytes(password), salt=salt, n=2**14, r=8, p=1, dklen=32)

    @classmethod
    def role_options(cls):
        return [{"id": role, "label": cls.ROLE_LABELS[role]} for role in cls.ROLES]

    @classmethod
    def _normalize_roles(cls, roles=None, role=None):
        value = roles if roles is not None else role
        if isinstance(value, str):
            values = re.split(r"[,;\s]+", value.strip())
        elif isinstance(value, (list, tuple, set)):
            values = list(value)
        else:
            raise ValueError("Select at least one permission role.")
        normalized = []
        for item in values:
            if not isinstance(item, str):
                raise ValueError("Permission roles must be text values.")
            token = item.strip().lower()
            if not token:
                continue
            if token == "all":
                normalized.extend(cls.ROLES)
            elif token not in cls.ROLES:
                raise ValueError("One or more permission roles are unknown.")
            else:
                normalized.append(token)
        result = [item for item in cls.ROLES if item in set(normalized)]
        if not result:
            raise ValueError("Select at least one permission role.")
        return result

    @classmethod
    def _tokens(cls, value, label, *, required=True):
        if isinstance(value, str):
            values = re.split(r"[,;\s]+", value.strip()) if value.strip() else []
        elif isinstance(value, (list, tuple, set)):
            values = []
            for item in value:
                if not isinstance(item, str):
                    raise ValueError(f"{label} entries must be text values.")
                values.extend(re.split(r"[,;\s]+", item.strip()) if item.strip() else [])
        else:
            raise ValueError(f"Enter {label.lower()} as a comma-separated list.")
        values = [item.strip() for item in values if item.strip()]
        if len(values) > cls.MAX_ACCESS_ITEMS:
            raise ValueError(f"Use no more than {cls.MAX_ACCESS_ITEMS} {label.lower()} entries.")
        if required and not values:
            raise ValueError(f"Enter at least one {label.lower()} entry.")
        return values

    @classmethod
    def _normalize_networks(cls, networks, *, required=True):
        values = cls._tokens(networks, "IP or CIDR", required=required)
        result = []
        for value in values:
            try:
                parsed = ipaddress.ip_network(value, strict=False)
            except ValueError as error:
                raise ValueError("Enter valid IPv4 or IPv6 addresses/CIDRs, separated by commas.") from error
            if parsed.prefixlen == 0:
                raise ValueError("Default-route networks are not allowed.")
            canonical = str(parsed)
            if canonical not in result:
                result.append(canonical)
        return result

    @classmethod
    def _resolve_camera_id(cls, value, camera_ids):
        token = value.strip()
        if token in camera_ids:
            return token
        if token.isdecimal():
            number = int(token)
            matches = [camera_id for camera_id in camera_ids
                       if (match := re.search(r"(\d+)$", camera_id)) and int(match.group(1)) == number]
            if len(matches) == 1:
                return matches[0]
        raise ValueError("A camera ID does not match a configured camera.")

    @classmethod
    def _normalize_camera_permissions(cls, camera_permissions, camera_ids):
        if not isinstance(camera_permissions, dict):
            raise ValueError("Enter camera access as camera IDs with viewer or administrator access.")
        result = {}
        for raw_id, permission in camera_permissions.items():
            if not isinstance(raw_id, str) or not isinstance(permission, str):
                raise ValueError("Camera access entries must contain text IDs and permissions.")
            if permission not in ("viewer", "camera_admin"):
                raise ValueError("Camera access must be Viewer or Camera administrator.")
            if raw_id.strip().lower() == "all":
                result.update({camera_id: permission for camera_id in camera_ids})
                continue
            camera_id = cls._resolve_camera_id(raw_id, camera_ids)
            if result.get(camera_id) != "camera_admin" or permission == "camera_admin":
                result[camera_id] = permission
        return result

    @classmethod
    def _validate_account(cls, roles, camera_permissions, networks, camera_ids):
        roles = cls._normalize_roles(roles)
        permissions = cls._normalize_camera_permissions(camera_permissions, camera_ids)
        if "camera_admin" not in roles and any(value == "camera_admin" for value in permissions.values()):
            raise ValueError("Select Camera administrator to grant camera control.")
        if "web_admin" not in roles and not permissions:
            raise ValueError("Select camera IDs for a non-web-administrator account.")
        if "web_admin" not in roles and "camera_admin" in roles and not any(
            value == "camera_admin" for value in permissions.values()
        ):
            raise ValueError("Add at least one camera ID under Control camera IDs.")
        if "viewer" not in roles and "camera_admin" not in roles and permissions:
            raise ValueError("Camera grants require Viewer or Camera administrator.")
        checked_networks = cls._normalize_networks(networks)
        return roles, permissions, checked_networks

    @staticmethod
    def _valid_username(username):
        return isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", username) is not None

    def _insert_user(self, db, username, password, role, camera_ids, networks):
        if not self._valid_username(username) or role not in self.ROLES or not isinstance(password, str) or len(password) < 8:
            raise ValueError("invalid viewer account")
        salt = secrets.token_bytes(16)
        db.execute("INSERT INTO users(username,salt,password_hash,role) VALUES (?,?,?,?)", (username, salt, self._hash(password, salt), role))
        db.execute("INSERT OR IGNORE INTO user_roles(username,role) VALUES (?,?)", (username, role))
        if role != "web_admin":
            for camera_id in camera_ids:
                db.execute("INSERT INTO user_cameras(username,camera_id,permission) VALUES (?,?,?)", (username, camera_id, "camera_admin" if role == "camera_admin" else "viewer"))
        for network in networks:
            db.execute("INSERT INTO user_networks(username,cidr) VALUES (?,?)", (username, str(ipaddress.ip_network(network, strict=False))))

    def authenticate(self, username, password, address):
        if not self._valid_username(username) or not isinstance(password, str):
            return None
        with self.lock, self._session() as db:
            row = db.execute("SELECT salt,password_hash,role,enabled FROM users WHERE username=?", (username,)).fetchone()
            if not row or not row["enabled"]:
                return None
            try:
                actual = self._hash(password, row["salt"])
            except (ValueError, MemoryError):
                return None
            if not hmac.compare_digest(actual, row["password_hash"]):
                return None
            allowed = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM allowed_networks")]
            owned = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM user_networks WHERE username=?", (username,))]
            if not any(address in network for network in allowed) or (owned and not any(address in network for network in owned)):
                return {"username": username, "role": row["role"], "source_allowed": False}
            grants = {item[0]: item[1] for item in db.execute("SELECT camera_id,permission FROM user_cameras WHERE username=?", (username,))}
            roles = [item[0] for item in db.execute("SELECT role FROM user_roles WHERE username=? ORDER BY role", (username,))]
            return {"username": username, "role": row["role"], "roles": roles, "source_allowed": True, "cameras": grants}

    @classmethod
    def _validate_networks(cls, db, networks):
        values = cls._normalize_networks(networks)
        allowed = [ipaddress.ip_network(row[0]) for row in db.execute("SELECT cidr FROM allowed_networks")]
        for value in values:
            network = ipaddress.ip_network(value)
            if not any(network.subnet_of(parent) for parent in allowed):
                raise ValueError("user network is outside the global allowlist")
        return values

    def create_user(self, username, password, role=None, camera_permissions=None, networks=None, camera_ids=(), roles=None):
        if not self._valid_username(username):
            raise ValueError("Use a username with 1 to 64 letters, numbers, dots, underscores, or hyphens.")
        try:
            self._password_bytes(password, minimum=12)
        except ValueError as error:
            raise ValueError("Passwords must be 12 to 256 characters and valid Unicode.") from error
        selected_roles, checked_permissions, checked_networks = self._validate_account(
            roles if roles is not None else role,
            camera_permissions if camera_permissions is not None else {},
            networks,
            camera_ids,
        )
        legacy_role = "web_admin" if "web_admin" in selected_roles else "camera_admin" if "camera_admin" in selected_roles else "viewer"
        with self.lock, self._session() as db:
            checked_networks = self._validate_networks(db, checked_networks)
            self._insert_user(db, username, password, legacy_role, [], [])
            db.execute("DELETE FROM user_roles WHERE username=?", (username,))
            for selected_role in selected_roles:
                db.execute("INSERT INTO user_roles(username,role) VALUES (?,?)", (username, selected_role))
            for camera_id, permission in checked_permissions.items():
                db.execute("INSERT INTO user_cameras(username,camera_id,permission) VALUES (?,?,?)", (username, camera_id, permission))
            for network in checked_networks:
                db.execute("INSERT INTO user_networks(username,cidr) VALUES (?,?)", (username, network))

    def update_access(self, username, role=None, camera_permissions=None, networks=None, camera_ids=(), password=None, roles=None):
        if not self._valid_username(username):
            raise ValueError("Invalid username.")
        selected_roles, checked_permissions, checked_networks = self._validate_account(
            roles if roles is not None else role,
            camera_permissions if camera_permissions is not None else {},
            networks,
            camera_ids,
        )
        if password is not None:
            try:
                self._password_bytes(password, minimum=12)
            except ValueError as error:
                raise ValueError("Passwords must be 12 to 256 characters and valid Unicode.") from error
        legacy_role = "web_admin" if "web_admin" in selected_roles else "camera_admin" if "camera_admin" in selected_roles else "viewer"
        with self.lock, self._session() as db:
            row = db.execute("SELECT enabled FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise KeyError("unknown user")
            had_web_admin = db.execute("SELECT 1 FROM user_roles WHERE username=? AND role='web_admin'", (username,)).fetchone() is not None
            has_web_admin = "web_admin" in selected_roles
            if row["enabled"] and had_web_admin and not has_web_admin:
                active = db.execute("SELECT COUNT(DISTINCT u.username) FROM users u JOIN user_roles r ON r.username=u.username WHERE r.role='web_admin' AND u.enabled=1").fetchone()[0]
                if active <= 1:
                    raise ValueError("You cannot remove the last enabled Web administrator.")
            checked_networks = self._validate_networks(db, checked_networks)
            db.execute("UPDATE users SET role=? WHERE username=?", (legacy_role, username))
            db.execute("DELETE FROM user_roles WHERE username=?", (username,))
            for selected_role in selected_roles:
                db.execute("INSERT INTO user_roles(username,role) VALUES (?,?)", (username, selected_role))
            db.execute("DELETE FROM user_cameras WHERE username=?", (username,))
            db.execute("DELETE FROM user_networks WHERE username=?", (username,))
            for camera_id, permission in checked_permissions.items():
                db.execute("INSERT INTO user_cameras(username,camera_id,permission) VALUES (?,?,?)", (username, camera_id, permission))
            for network in checked_networks:
                db.execute("INSERT INTO user_networks(username,cidr) VALUES (?,?)", (username, network))
            if password is not None:
                salt = secrets.token_bytes(16)
                db.execute("UPDATE users SET salt=?,password_hash=? WHERE username=?", (salt, self._hash(password, salt), username))

    def inventory(self):
        with self.lock, self._session() as db:
            result = []
            for user in db.execute("SELECT username,role,enabled FROM users ORDER BY username"):
                grants = {row[0]: row[1] for row in db.execute("SELECT camera_id,permission FROM user_cameras WHERE username=? ORDER BY camera_id", (user["username"],))}
                networks = [row[0] for row in db.execute("SELECT cidr FROM user_networks WHERE username=? ORDER BY cidr", (user["username"],))]
                roles = [row[0] for row in db.execute("SELECT role FROM user_roles WHERE username=? ORDER BY role", (user["username"],))]
                result.append({"username": user["username"], "role": user["role"], "roles": roles, "enabled": bool(user["enabled"]), "cameras": grants, "networks": networks})
            return result

    def set_enabled(self, username, enabled):
        with self.lock, self._session() as db:
            if not self._valid_username(username):
                raise ValueError("Invalid username.")
            row = db.execute("SELECT enabled FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                raise KeyError("unknown user")
            has_web_admin = db.execute("SELECT 1 FROM user_roles WHERE username=? AND role='web_admin'", (username,)).fetchone() is not None
            if not enabled and has_web_admin and row["enabled"]:
                active = db.execute("SELECT COUNT(DISTINCT u.username) FROM users u JOIN user_roles r ON r.username=u.username WHERE r.role='web_admin' AND u.enabled=1").fetchone()[0]
                if active <= 1:
                    raise ValueError("You cannot disable the last enabled Web administrator.")
            db.execute("UPDATE users SET enabled=? WHERE username=?", (int(enabled), username))

    def is_global_network_allowed(self, address):
        with self.lock, self._session() as db:
            return any(address in ipaddress.ip_network(row[0]) for row in db.execute("SELECT cidr FROM allowed_networks"))

    def list_networks(self):
        with self.lock, self._session() as db:
            return [row[0] for row in db.execute("SELECT cidr FROM allowed_networks ORDER BY cidr")]

    def can_continue_viewing(self, username, address, camera_id):
        """Recheck long-lived media grants without repeating password hashing."""
        with self.lock, self._session() as db:
            row = db.execute("SELECT enabled FROM users WHERE username=?", (username,)).fetchone()
            if not row or not row["enabled"]:
                return False
            allowed = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM allowed_networks")]
            owned = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM user_networks WHERE username=?", (username,))]
            if not any(address in network for network in allowed) or (owned and not any(address in network for network in owned)):
                return False
            if db.execute("SELECT 1 FROM user_roles WHERE username=? AND role='web_admin'", (username,)).fetchone():
                return True
            return db.execute("SELECT 1 FROM user_cameras WHERE username=? AND camera_id=?", (username, camera_id)).fetchone() is not None

    def add_networks(self, networks):
        normalized = self._normalize_networks(networks)
        with self.lock, self._session() as db:
            for network in normalized:
                db.execute("INSERT OR IGNORE INTO allowed_networks(cidr) VALUES (?)", (network,))

    def add_network(self, cidr):
        self.add_networks(cidr)

    def remove_network(self, cidr, current_address):
        parsed = self._normalize_networks(cidr)
        if len(parsed) != 1:
            raise ValueError("Remove one IP or CIDR at a time.")
        network = parsed[0]
        with self.lock, self._session() as db:
            remaining = [ipaddress.ip_network(row[0]) for row in db.execute("SELECT cidr FROM allowed_networks WHERE cidr<>?", (network,))]
            if not any(current_address in item for item in remaining):
                raise ValueError("cannot remove the current administrator source network")
            db.execute("DELETE FROM allowed_networks WHERE cidr=?", (network,))

    @staticmethod
    def can(user, camera_id, action):
        if "web_admin" in user.get("roles", [user.get("role")]):
            return True
        grant = user.get("cameras", {}).get(camera_id)
        if action == "view":
            return grant in ("viewer", "camera_admin")
        if action == "record":
            return grant in ("viewer", "camera_admin")
        return grant == "camera_admin"

    @staticmethod
    def can_manage_clip(user, camera_id, owner):
        if owner == user.get("username"):
            return True
        if "web_admin" in user.get("roles", [user.get("role")]):
            return True
        return user.get("cameras", {}).get(camera_id) == "camera_admin"

    @staticmethod
    def can_manage_hidden(user, camera_id):
        if "web_admin" in user.get("roles", [user.get("role")]):
            return True
        return user.get("cameras", {}).get(camera_id) == "camera_admin"


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

    def respond(self, code, body=b"", content_type="application/json", challenge=False, extra_headers=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
        if challenge:
            self.send_header("WWW-Authenticate", 'Basic realm="Camera viewer", charset="UTF-8"')
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
                    if not self.server.users.can_continue_viewing(self.auth_user["username"], ipaddress.ip_address(self.client_address[0]), camera_id):
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

    def authorized(self):
        ip = ipaddress.ip_address(self.client_address[0])
        if not self.server.users.is_global_network_allowed(ip):
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
            user = self.server.users.authenticate(username, password, ip) if scheme.lower() == "basic" else None
        except (ValueError, UnicodeError):
            user = None
        if not user:
            with self.server.auth_lock:
                attempts.append(now)
            self.respond(401, challenge=True); return False
        if not user["source_allowed"]:
            self.respond(403); return False
        self.auth_user = user
        return True

    def can_camera(self, camera_id, action):
        if not self.server.users.can(self.auth_user, camera_id, action):
            self.respond(403); return False
        return True

    def require_web_admin(self):
        if "web_admin" not in self.auth_user.get("roles", [self.auth_user.get("role")]):
            self.respond(403); return False
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
                visible = [item | {"can_control": self.server.users.can(self.auth_user, item["id"], "control")} for item in self.server.cameras.summary() if self.server.users.can(self.auth_user, item["id"], "view")]
                recording_enabled = bool(getattr(self.server.clips, "enabled", False))
                visible = [item | {"can_record": recording_enabled and self.server.users.can(self.auth_user, item["id"], "record")} for item in visible]
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
                    self.respond(200, json.dumps(self.server.users.list_networks()).encode())
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
        if not self.authorized(): return
        origin = self.headers.get("Origin")
        if self.headers.get("X-Camera-Client") != "1" or (origin and origin != "https://" + self.headers.get("Host", "")):
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
            path = urlsplit(self.path).path
            scoped = re.fullmatch(r"/api/cameras/([^/]+)/(start|stop|ptz)", path)
            if scoped:
                camera_id, operation = scoped.groups()
                try:
                    self.server.cameras.get(camera_id)
                except KeyError:
                    self.respond(404); return
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
                        list(self.server.cameras.cameras), roles=data.get("roles"),
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
                        roles=data.get("roles"),
                    )
                except ValueError as error:
                    self.respond(400, json.dumps({"error": str(error)}).encode()); return
                except KeyError:
                    self.respond(404, b'{"error":"User not found."}'); return
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
                    self.server.users.remove_network(data.get("cidr"), ipaddress.ip_address(self.client_address[0]))
                except ValueError as error:
                    self.respond(409, json.dumps({"error": str(error)}).encode()); return
                result = {"removed": True}
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
    auth_camera = cameras[default_camera]
    server.users = UserStore(
        web.get("auth_database", "~/.local/state/yoosee-cam-utils/viewer-users.sqlite3"),
        auth_camera.c["username"], auth_camera.c["password"], list(cameras), web["allowed_networks"],
    )
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
        recording = config.get("recording", {})
        print(json.dumps({"bind": web["bind"], "port": web["port"], "tls": True, "auth": "server-side basic", "allowed_networks": web["allowed_networks"], "cameras": camera_ids, "default_camera": default_camera, "manual_recording": {"enabled": bool(recording.get("enabled", False)), "max_bytes": int(recording.get("max_bytes", 2 * 1024**3)), "reserve_bytes": int(recording.get("reserve_bytes", 5 * 1024**3)), "segment_seconds": int(recording.get("segment_seconds", 900)), "default_session_seconds": int(recording.get("default_session_seconds", 3600))}})); return
    server = make_server(cameras, web, default_camera=default_camera, recording=config.get("recording", {}))
    def reap():
        while True:
            time.sleep(10)
            server.cameras.reap_idle()
    threading.Thread(target=reap, daemon=True).start()
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


if __name__ == "__main__":
    main()
