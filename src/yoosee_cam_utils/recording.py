"""Bounded local manual clip capture for configured cameras."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid

from .cli import media_input, redact


class ClipManager:
    def __init__(self, cameras, config):
        self.cameras = cameras
        self.enabled = bool(config.get("enabled", False))
        self.directory = Path(config.get("spool_dir", "~/.local/state/yoosee-cam-utils/recordings")).expanduser()
        self.max_bytes = int(config.get("max_bytes", 2 * 1024**3))
        self.reserve_bytes = int(config.get("reserve_bytes", 5 * 1024**3))
        self.max_seconds = int(config.get("max_seconds", 300))
        self.default_seconds = int(config.get("default_seconds", 30))
        if self.max_bytes < 1024**2 or self.reserve_bytes < 0 or not 1 <= self.default_seconds <= self.max_seconds <= 300:
            raise ValueError("invalid recording limits")
        if self.enabled:
            if self.directory.is_symlink():
                raise ValueError("recording directory must not be a symlink")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = None
        self.closed = False
        if self.enabled:
            self._remove_incomplete_parts()

    def _remove_incomplete_parts(self):
        for pattern in ("*.part.mkv", "*.json.part", "*.log"):
            for path in self.directory.rglob(pattern):
                if path.is_file() and not path.is_symlink():
                    path.unlink()
        for path in self.directory.rglob("*.mkv"):
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

    def start(self, camera_id, profile, seconds=None):
        if not self.enabled:
            raise ValueError("local recording is disabled")
        if camera_id not in self.cameras.workers:
            raise KeyError("unknown camera")
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            seconds = self.default_seconds if seconds is None else -1
        if not 1 <= seconds <= self.max_seconds:
            raise ValueError(f"clip duration must be 1..{self.max_seconds} seconds")
        if not isinstance(profile, str) or not profile or len(profile) > 100:
            raise ValueError("invalid stream profile")
        with self.lock:
            if self.closed:
                raise ValueError("recording service is shutting down")
            if self.active:
                raise ValueError("another clip is already recording")
            budget = self._budget()
            if budget < 1024**2:
                raise ValueError("recording quota or free-space reserve reached")
            job_id = uuid.uuid4().hex
            started = datetime.now(timezone.utc).isoformat()
            camera = self.cameras.cameras[camera_id]
            worker = self.cameras.get(camera_id)
            with worker.camera_lock:
                profiles = worker.camera.profiles()
                if profile not in [item["token"] for item in profiles]:
                    raise ValueError("unknown stream profile")
                uri = worker.camera.uri(profile)
            now = datetime.now(timezone.utc)
            camera_dir = self.directory / camera_id
            year_dir = camera_dir / now.strftime("%Y")
            output_dir = year_dir / now.strftime("%m")
            for directory in (camera_dir, year_dir, output_dir):
                if directory.is_symlink():
                    raise ValueError("recording path must not contain symlinks")
            output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            for directory in (camera_dir, year_dir, output_dir):
                if directory.is_symlink():
                    raise ValueError("recording path must not contain symlinks")
                directory.chmod(0o700)
            part = output_dir / f"{job_id}.part.mkv"
            final = output_dir / f"{job_id}.mkv"
            metadata = output_dir / f"{job_id}.json"
            job = {"id": job_id, "camera_id": camera_id, "state": "recording", "started_at": started, "seconds": seconds, "profile": profile, "path": final, "metadata": metadata, "part": part, "budget": budget, "process": None, "error": None}
            self.jobs[job_id] = job
            self.active = job_id
            thread = threading.Thread(target=self._capture, args=(job, camera, uri), daemon=True)
            job["thread"] = thread
            thread.start()
            return self._public(job)

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

    def _capture(self, job, camera, uri):
        log_path = job["part"].with_suffix(".log")
        try:
            with log_path.open("wb") as log:
                args = ["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "pipe,file,rtsp,tcp,udp,rtp", "-f", "concat", "-safe", "0", "-i", "pipe:0", "-t", str(job["seconds"]), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-fs", str(job["budget"]), "-f", "matroska", "-n", str(job["part"])]
                process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)
                with self.lock:
                    job["process"] = process
                    stopping = self.closed
                if stopping:
                    process.terminate()
                    raise ValueError("recording stopped during shutdown")
                try:
                    process.communicate(input=media_input(uri, camera.c), timeout=job["seconds"] + 30)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise ValueError("recording timed out")
            if process.returncode != 0:
                detail = log_path.read_text(errors="replace")[-1000:]
                raise ValueError(redact(detail, camera.c) or "camera capture failed")
            if not job["part"].is_file() or job["part"].stat().st_size == 0:
                raise ValueError("camera produced an empty clip")
            probe = self._probe(job["part"])
            if probe["duration_seconds"] < job["seconds"] - 2:
                raise ValueError("clip ended before the requested duration or reached its size quota")
            with job["part"].open("rb") as stream:
                os.fsync(stream.fileno())
            size = job["part"].stat().st_size
            digest = self._hash_file(job["part"])
            os.replace(job["part"], job["path"])
            ended = datetime.now(timezone.utc).isoformat()
            metadata = {"id": job["id"], "camera_id": job["camera_id"], "trigger": "manual", "started_at": job["started_at"], "ended_at": ended, "profile": job["profile"], **probe, "bytes": size, "sha256": digest}
            self._write_metadata(job["metadata"], metadata)
            with self.lock:
                job.update(state="complete", ended_at=ended, result=metadata, process=None)
        except Exception as error:
            with self.lock:
                job.update(state="failed", error=str(error)[:500], process=None)
            job["path"].unlink(missing_ok=True)
            job["metadata"].unlink(missing_ok=True)
            job["part"].unlink(missing_ok=True)
            job["metadata"].with_suffix(".json.part").unlink(missing_ok=True)
        finally:
            log_path.unlink(missing_ok=True)
            with self.lock:
                if self.active == job["id"]:
                    self.active = None

    @staticmethod
    def _public(job):
        result = {key: job.get(key) for key in ("id", "camera_id", "state", "started_at", "ended_at", "seconds", "error", "result") if job.get(key) is not None}
        return result

    def status(self, job_id, camera_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job and job["camera_id"] == camera_id:
                return self._public(job)
        path = self._clip_path(camera_id, job_id)
        if path is None:
            raise KeyError("unknown clip")
        return json.loads(path.with_suffix(".json").read_text()) | {"state": "complete"}

    def list_clips(self, camera_id):
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
                if item.get("camera_id") == camera_id and self._clip_path(camera_id, item.get("id")) is not None:
                    result.append(item)
            except (OSError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda item: item.get("started_at", ""), reverse=True)[:100]

    def _clip_path(self, camera_id, clip_id):
        if camera_id not in self.cameras.cameras or not isinstance(clip_id, str) or len(clip_id) != 32 or any(ch not in "0123456789abcdef" for ch in clip_id):
            return None
        camera_dir = self.directory / camera_id
        for path in camera_dir.rglob(f"{clip_id}.mkv") if camera_dir.exists() else ():
            if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(self.directory.resolve()):
                return path
        return None

    def clip_path(self, camera_id, clip_id):
        path = self._clip_path(camera_id, clip_id)
        if path is None:
            raise KeyError("unknown clip")
        return path

    def delete_clip(self, camera_id, clip_id):
        with self.lock:
            if self.active == clip_id:
                raise ValueError("cannot delete a clip while it is recording")
            path = self._clip_path(camera_id, clip_id)
            if path is None:
                raise KeyError("unknown clip")
            metadata = path.with_suffix(".json")
            path.unlink()
            if metadata.is_file() and not metadata.is_symlink():
                metadata.unlink()

    def stop_all(self):
        with self.lock:
            self.closed = True
            active = self.jobs.get(self.active) if self.active else None
            process = active.get("process") if active else None
            thread = active.get("thread") if active else None
        if process and process.poll() is None:
            process.terminate()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=8)
            if process and process.poll() is None:
                process.kill()
                thread.join(timeout=3)
