"""Quota-limited, segmented local recording sessions."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from .cli import media_input, redact


MAX_SEGMENT_SECONDS = 15 * 60
DEFAULT_SESSION_SECONDS = 60 * 60
MAX_SESSION_SECONDS = 12 * 60 * 60


class ClipManager:
    def __init__(self, cameras, config):
        self.cameras = cameras
        self.enabled = bool(config.get("enabled", False))
        self.directory = Path(config.get("spool_dir", "~/.local/state/yoosee-cam-utils/recordings")).expanduser()
        self.max_bytes = int(config.get("max_bytes", 2 * 1024**3))
        self.reserve_bytes = int(config.get("reserve_bytes", 5 * 1024**3))
        self.segment_seconds = int(config.get("segment_seconds", MAX_SEGMENT_SECONDS))
        self.default_session_seconds = int(config.get("default_session_seconds", DEFAULT_SESSION_SECONDS))
        self.max_concurrent_sessions = int(config.get("max_concurrent_sessions", 4))
        if (self.max_bytes < 1024**2 or self.reserve_bytes < 0 or
                not 1 <= self.segment_seconds <= MAX_SEGMENT_SECONDS or
                not 1 <= self.default_session_seconds <= 60 * 60 or
                not 1 <= self.max_concurrent_sessions <= 16):
            raise ValueError("invalid recording limits")
        if self.enabled:
            if self.directory.is_symlink():
                raise ValueError("recording directory must not be a symlink")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
        self.lock = threading.RLock()
        self.sessions = {}
        self.closed = False
        if self.enabled:
            self._recover_spool()

    def _recover_spool(self):
        for pattern in ("*.part.mkv", "*.part.mp4", "*.json.part", "*.log"):
            for path in self.directory.rglob(pattern):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        for path in (*self.directory.rglob("*.mkv"), *self.directory.rglob("*.mp4")):
            if path.is_symlink() or not path.is_file():
                continue
            metadata = path.with_suffix(".json")
            valid = False
            if metadata.is_file() and not metadata.is_symlink():
                try:
                    item = json.loads(metadata.read_text())
                    valid = item.get("id") == path.stem and item.get("camera_id") == path.relative_to(self.directory).parts[0]
                except (OSError, ValueError, TypeError):
                    pass
            if not valid:
                path.unlink()
                if metadata.is_file() and not metadata.is_symlink():
                    metadata.unlink()

    def _usage(self):
        total = 0
        for root, dirs, files in os.walk(self.directory, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
            for name in files:
                path = Path(root) / name
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
        return total

    def _budget(self):
        usage = self._usage()
        stats = os.statvfs(self.directory)
        free = stats.f_bavail * stats.f_frsize
        return min(self.max_bytes - usage - 64 * 1024, free - self.reserve_bytes - 64 * 1024)

    def _output_dir(self, camera_id):
        now = datetime.now(timezone.utc)
        camera_dir = self.directory / camera_id
        year_dir = camera_dir / now.strftime("%Y")
        output_dir = year_dir / now.strftime("%m")
        for path in (camera_dir, year_dir, output_dir):
            if path.is_symlink():
                raise ValueError("recording path must not contain symlinks")
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (camera_dir, year_dir, output_dir):
            if path.is_symlink():
                raise ValueError("recording path must not contain symlinks")
            path.chmod(0o700)
        return output_dir

    def start(self, camera_id, profile, max_seconds=None, owner="admin"):
        if not self.enabled:
            raise ValueError("local recording is disabled")
        if camera_id not in self.cameras.workers:
            raise KeyError("unknown camera")
        if max_seconds is None:
            max_seconds = self.default_session_seconds
        if not isinstance(max_seconds, int) or isinstance(max_seconds, bool) or not 1 <= max_seconds <= MAX_SESSION_SECONDS:
            raise ValueError(f"session limit must be 1..{MAX_SESSION_SECONDS} seconds")
        if not isinstance(profile, str) or not profile or len(profile) > 100:
            raise ValueError("invalid stream profile")
        if not isinstance(owner, str) or not owner or len(owner) > 64:
            raise ValueError("invalid recording owner")
        with self.lock:
            if self.closed:
                raise ValueError("recording service is shutting down")
            active = [item for item in self.sessions.values() if item["state"] == "recording"]
            if any(item["camera_id"] == camera_id and item["owner"] == owner for item in active):
                raise ValueError("you already have an active recording for this camera")
            if len(active) >= self.max_concurrent_sessions:
                raise ValueError("the recording concurrency limit has been reached")
            if self._budget() < 1024**2:
                raise ValueError("recording quota or free-space reserve reached")
            camera = self.cameras.cameras[camera_id]
            worker = self.cameras.get(camera_id)
            with worker.camera_lock:
                profiles = worker.camera.profiles()
                if profile not in [item["token"] for item in profiles]:
                    raise ValueError("unknown stream profile")
                uri = worker.camera.uri(profile)
            session_id = uuid.uuid4().hex
            session = {
                "id": session_id, "camera_id": camera_id, "profile": profile,
                "owner": owner,
                "state": "recording", "started_at": datetime.now(timezone.utc).isoformat(),
                "max_seconds": max_seconds, "segment_count": 0, "segments": [],
                "captured_seconds": 0,
                "error": None, "stop_reason": None, "stop_event": threading.Event(),
                "process": None, "current_part": None, "thread": None,
            }
            self.sessions[session_id] = session
            thread = threading.Thread(target=self._record_session, args=(session, camera, uri), daemon=True)
            session["thread"] = thread
            thread.start()
            return self._public_session(session)

    def stop(self, camera_id, session_id=None, owner=None):
        with self.lock:
            session = self.sessions.get(session_id) if session_id else next((
                item for item in reversed(tuple(self.sessions.values()))
                if item["camera_id"] == camera_id and item["state"] == "recording"
                and (owner is None or item["owner"] == owner)
            ), None)
            if session is None or session["camera_id"] != camera_id or (owner is not None and session["owner"] != owner):
                raise KeyError("no active recording session for this camera")
            session_id = session["id"]
            if session["state"] != "recording":
                return self._public_session(session)
            session["stop_reason"] = "Stopped by user"
            session["stop_event"].set()
            process = session.get("process")
        if process and process.poll() is None:
            process.send_signal(signal.SIGINT)
        return self.status(camera_id, session_id)

    def status(self, camera_id, session_id=None, owner=None):
        with self.lock:
            session = self.sessions.get(session_id) if session_id else next((
                item for item in reversed(tuple(self.sessions.values()))
                if item["camera_id"] == camera_id and (owner is None or item["owner"] == owner)
            ), None)
            if session is None or session["camera_id"] != camera_id or (owner is not None and session["owner"] != owner):
                return {"state": "idle", "camera_id": camera_id}
            return self._public_session(session)

    @staticmethod
    def _public_session(session):
        elapsed = max(0, int(max(time.monotonic() - session.get("monotonic_start", time.monotonic()), session.get("captured_seconds", 0))))
        return {
            key: session.get(key) for key in (
                "id", "camera_id", "profile", "state", "started_at", "max_seconds",
                "segment_count", "segments", "error", "stop_reason",
            ) if session.get(key) is not None
        } | {"elapsed_seconds": elapsed}

    @staticmethod
    def _probe(path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height,avg_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=8, check=True,
        )
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        video = next((item for item in streams if item.get("width") and item.get("height")), None)
        if video is None:
            raise ValueError("clip has no video stream")
        numerator, denominator = video.get("avg_frame_rate", "0/1").split("/", 1)
        fps = float(numerator) / float(denominator)
        duration = float(data.get("format", {}).get("duration", 0))
        if duration <= 0 or fps <= 0:
            raise ValueError("clip metadata is invalid")
        return {"width": int(video["width"]), "height": int(video["height"]), "fps": round(fps, 3), "duration_seconds": round(duration, 3)}

    @staticmethod
    def _hash_file(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _write_metadata(path, value):
        temporary = path.with_suffix(".json.part")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _finalize_segment(self, session, part, clip_id, index, started_at, expected_seconds, budget, camera, allow_short):
        if not part.is_file() or part.stat().st_size == 0:
            raise ValueError("camera produced an empty clip segment")
        part.chmod(0o600)
        probe = self._probe(part)
        if allow_short:
            if probe["duration_seconds"] < 1:
                raise ValueError("stopped segment was too short to keep")
        elif probe["duration_seconds"] < expected_seconds - 2:
            raise ValueError("clip segment ended before its planned boundary")
        size = part.stat().st_size
        if size > budget + 1024 * 1024:
            raise ValueError("segment exceeded its spool allocation")
        with part.open("rb") as stream:
            os.fsync(stream.fileno())
        final = part.with_name(f"{clip_id}.mp4")
        metadata_path = part.with_name(f"{clip_id}.json")
        digest = self._hash_file(part)
        local_started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).astimezone()
        quality = "FHD" if probe["width"] >= 1920 else "HD" if probe["width"] >= 1280 else "SD"
        display_name = f"{session['camera_id']}_{local_started:%a_%Y%m%d_%H%M%S}_{quality}"
        metadata = {
            "id": clip_id, "camera_id": session["camera_id"], "session_id": session["id"],
            "owner": session["owner"], "storage_backend": "local",
            "storage_key": str(final.relative_to(self.directory)),
            "replication_state": "local_only", "display_name": display_name,
            "segment_index": index, "trigger": "manual", "started_at": started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(), "profile": session["profile"],
            "session_limit_seconds": session["max_seconds"], **probe,
            "bytes": size, "sha256": digest,
        }
        os.replace(part, final)
        try:
            self._write_metadata(metadata_path, metadata)
        except Exception:
            final.unlink(missing_ok=True)
            raise
        summary = {key: metadata[key] for key in ("id", "owner", "segment_index", "started_at", "duration_seconds", "width", "height", "fps", "bytes", "sha256")}
        session["segments"].append(summary)
        session["segment_count"] = len(session["segments"])
        return probe

    def _record_session(self, session, camera, uri):
        session["monotonic_start"] = time.monotonic()
        try:
            while not session["stop_event"].is_set():
                elapsed = int(max(time.monotonic() - session["monotonic_start"], session["captured_seconds"]))
                remaining = session["max_seconds"] - elapsed
                if remaining <= 0:
                    session["stop_reason"] = "Session time limit reached"
                    break
                with self.lock:
                    concurrent = sum(item["state"] == "recording" for item in self.sessions.values())
                budget = self._budget() // max(1, concurrent)
                if budget < 1024**2:
                    session["stop_reason"] = "Recording quota or free-space reserve reached"
                    break
                segment_seconds = min(self.segment_seconds, remaining)
                segment_index = session["segment_count"] + 1
                clip_id = uuid.uuid4().hex
                output_dir = self._output_dir(session["camera_id"])
                part = output_dir / f"{clip_id}.part.mp4"
                log_path = output_dir / f"{clip_id}.log"
                session["current_part"] = part
                segment_started = datetime.now(timezone.utc).isoformat()
                with log_path.open("wb") as log:
                    args = [
                        "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp",
                        "-f", "concat", "-safe", "0", "-i", "pipe:0", "-t", str(segment_seconds),
                        "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-fs", str(budget),
                    "-movflags", "+faststart", "-f", "mp4", "-n", str(part),
                    ]
                    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
                    with self.lock:
                        session["process"] = process
                        stop_now = session["stop_event"].is_set() or self.closed
                    if stop_now and process.poll() is None:
                        process.send_signal(signal.SIGINT)
                    try:
                        process.communicate(input=media_input(uri, camera.c), timeout=segment_seconds + 30)
                    except subprocess.TimeoutExpired:
                        if process.poll() is None:
                            process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise ValueError("recording segment timed out")
                with self.lock:
                    session["process"] = None
                if not part.is_file() or part.stat().st_size == 0:
                    if session["stop_event"].is_set():
                        session["stop_reason"] = session["stop_reason"] or "Stopped by user"
                        break
                    raise ValueError("camera produced no clip segment")
                allow_short = session["stop_event"].is_set()
                if process.returncode != 0 and not allow_short:
                    detail = log_path.read_text(errors="replace")[-1000:]
                    if self._budget() < 1024**2:
                        session["stop_reason"] = "Recording quota or free-space reserve reached"
                        allow_short = True
                    else:
                        raise ValueError(redact(detail, camera.c) or "camera segment capture failed")
                try:
                    probe = self._finalize_segment(session, part, clip_id, segment_index, segment_started, segment_seconds, budget, camera, allow_short)
                finally:
                    part.unlink(missing_ok=True)
                    log_path.unlink(missing_ok=True)
                    with self.lock:
                        session["current_part"] = None
                if probe["duration_seconds"] < segment_seconds - 2 and not allow_short:
                    if self._budget() < 1024**2:
                        session["stop_reason"] = "Recording quota or free-space reserve reached"
                        break
                    raise ValueError("camera ended a segment before the planned boundary")
                session["captured_seconds"] += probe["duration_seconds"]
                if allow_short:
                    session["stop_reason"] = session["stop_reason"] or "Stopped by user"
                    break
            if session["state"] == "recording":
                session["state"] = "stopped" if session["stop_reason"] != "Session time limit reached" else "complete"
        except Exception as error:
            session["state"] = "failed"
            session["error"] = str(error)[:500]
            part = session.get("current_part")
            if part:
                part.unlink(missing_ok=True)
                part.with_suffix(".log").unlink(missing_ok=True)
        finally:
            with self.lock:
                session["process"] = None
                session["current_part"] = None
    def list_clips(self, camera_id, include_hidden=False, hidden_only=False, since_seconds=None, limit=None):
        if camera_id not in self.cameras.cameras:
            raise KeyError("unknown camera")
        result = []
        camera_dir = self.directory / camera_id
        if not camera_dir.exists():
            return result
        for path in camera_dir.rglob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                item = json.loads(path.read_text())
                if (item.get("camera_id") == camera_id
                        and (include_hidden or not item.get("hidden_at"))
                        and (not hidden_only or bool(item.get("hidden_at")))
                        and self._clip_path(camera_id, item.get("id")) is not None):
                    if since_seconds is not None:
                        started = datetime.fromisoformat(item["started_at"].replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - started).total_seconds() > since_seconds:
                            continue
                    result.append(item)
            except (OSError, ValueError, TypeError):
                continue
        result.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        self._annotate_duplicate_groups(result)
        return result[:limit] if limit else result

    @staticmethod
    def _annotate_duplicate_groups(clips):
        """Mark near-identical captures so the UI can show each clip's stable ID."""
        if len(clips) < 2:
            return
        starts = []
        for clip in clips:
            try:
                starts.append(datetime.fromisoformat(clip["started_at"].replace("Z", "+00:00")).timestamp())
            except (KeyError, OverflowError, TypeError, ValueError):
                starts.append(None)
        duplicate_counts = {}
        ordered = sorted((value, index) for index, value in enumerate(starts) if value is not None)
        for position, (left_start, left) in enumerate(ordered):
            try:
                left_duration = float(clips[left].get("duration_seconds", 0) or 0)
            except (TypeError, ValueError):
                continue
            if left_duration <= 0:
                continue
            left_end = left_start + left_duration
            for right_position in range(position + 1, len(ordered)):
                right_start, right = ordered[right_position]
                if right_start - left_start > 30:
                    break
                left_clip, right_clip = clips[left], clips[right]
                if (left_clip.get("width"), left_clip.get("height")) != (right_clip.get("width"), right_clip.get("height")):
                    continue
                try:
                    right_duration = float(right_clip.get("duration_seconds", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if right_duration <= 0 or max(left_duration, right_duration) > 2 * min(left_duration, right_duration):
                    continue
                right_end = right_start + right_duration
                if abs(left_end - right_end) > 30:
                    continue
                overlap = max(0, min(left_end, right_end) - max(left_start, right_start))
                if overlap / min(left_duration, right_duration) < 0.8:
                    continue
                duplicate_counts[left] = duplicate_counts.get(left, 1) + 1
                duplicate_counts[right] = duplicate_counts.get(right, 1) + 1

        for index, count in duplicate_counts.items():
            clips[index]["duplicate_count"] = count

    def _clip_path(self, camera_id, clip_id):
        if camera_id not in self.cameras.cameras or not isinstance(clip_id, str) or len(clip_id) != 32 or any(ch not in "0123456789abcdef" for ch in clip_id):
            return None
        camera_dir = self.directory / camera_id
        for path in camera_dir.rglob(f"{clip_id}.*") if camera_dir.exists() else ():
            if path.suffix not in (".mkv", ".mp4"):
                continue
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(self.directory.resolve()):
                return path
        return None

    def clip_path(self, camera_id, clip_id):
        path = self._clip_path(camera_id, clip_id)
        if path is None:
            raise KeyError("unknown clip")
        return path

    def clip_metadata(self, camera_id, clip_id):
        path = self._clip_path(camera_id, clip_id)
        if path is None:
            raise KeyError("unknown clip")
        metadata = path.with_suffix(".json")
        if metadata.is_symlink() or not metadata.is_file():
            raise KeyError("unknown clip")
        try:
            return json.loads(metadata.read_text())
        except (OSError, ValueError) as error:
            raise KeyError("unknown clip") from error

    def delete_clip(self, camera_id, clip_id):
        with self.lock:
            if any(session["state"] == "recording" and session["camera_id"] == camera_id and any(item["id"] == clip_id for item in session["segments"]) for session in self.sessions.values()):
                raise ValueError("cannot delete a clip from an active recording session")
            path = self._clip_path(camera_id, clip_id)
            if path is None:
                raise KeyError("unknown clip")
            metadata = path.with_suffix(".json")
            path.unlink()
            if metadata.is_file() and not metadata.is_symlink():
                metadata.unlink()

    def set_hidden(self, camera_id, clip_id, hidden, username):
        with self.lock:
            path = self._clip_path(camera_id, clip_id)
            if path is None:
                raise KeyError("unknown clip")
            metadata_path = path.with_suffix(".json")
            metadata = json.loads(metadata_path.read_text())
            if hidden:
                metadata["hidden_at"] = datetime.now(timezone.utc).isoformat()
                metadata["hidden_by"] = username
            else:
                metadata.pop("hidden_at", None)
                metadata.pop("hidden_by", None)
            self._write_metadata(metadata_path, metadata)

    def stop_all(self):
        with self.lock:
            self.closed = True
            sessions = [session for session in self.sessions.values() if session["state"] == "recording"]
            processes = [session.get("process") for session in sessions]
            threads = [session.get("thread") for session in sessions]
            for session in sessions:
                session["stop_reason"] = "Service shutting down"
                session["stop_event"].set()
        for process in processes:
            if process and process.poll() is None:
                process.send_signal(signal.SIGINT)
        for thread, process in zip(threads, processes):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=8)
                if process and process.poll() is None:
                    process.kill()
                    thread.join(timeout=3)
