"""App-managed camera credential overrides, saved after a camera accepted them."""
import json
import os
from pathlib import Path
import threading


class CredentialStore:
    """Atomic 0600 JSON file beside the account database.

    The operator's env file is never rewritten. Values here override it at startup.
    """

    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.lock = threading.Lock()

    def load(self):
        try:
            if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
                raise ValueError("camera credential file permissions must be 0600")
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("camera credential file is invalid")
        return {key: value for key, value in data.items()
                if isinstance(value, dict) and isinstance(value.get("username"), str) and isinstance(value.get("password"), str)}

    def save(self, camera_id, username, password):
        with self.lock:
            data = self.load()
            data[camera_id] = {"username": username, "password": password}
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.exists():
                backup = self.path.with_name(self.path.name + ".bak")
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(self.path.read_text())
            temporary = self.path.with_name(self.path.name + ".part")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
