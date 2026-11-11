"""Viewer accounts, grants, source networks and persisted watcher sessions."""
from contextlib import contextmanager
import hashlib
import hmac
import ipaddress
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time


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

    SCHEMA_VERSION = 1
    CAPABILITIES = ("watcher", "record")
    persist_sessions = True
    WATCHER_SESSIONS_PER_USER = 2

    def __init__(self, path, networks=()):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            if self.path.is_symlink() or not self.path.is_file() or stat.S_IMODE(self.path.stat().st_mode) & 0o077:
                raise ValueError("viewer account database permissions must be 0600")
        self.lock = threading.RLock()
        self.config_networks = self._normalize_networks(list(networks))
        self.migration_notes = []
        with self._session() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY, salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('web_admin','camera_admin','viewer')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    session_version INTEGER NOT NULL DEFAULT 1,
                    bootstrap INTEGER NOT NULL DEFAULT 0,
                    camera_link INTEGER NOT NULL DEFAULT 0
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
                CREATE TABLE IF NOT EXISTS allowed_networks (
                    cidr TEXT PRIMARY KEY, source TEXT NOT NULL DEFAULT 'admin'
                );
                CREATE TABLE IF NOT EXISTS user_roles (
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    PRIMARY KEY(username,role)
                );
                CREATE TABLE IF NOT EXISTS user_capabilities (
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    capability TEXT NOT NULL CHECK(capability IN ('watcher','record')),
                    PRIMARY KEY(username,capability)
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY,
                    username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                    address TEXT NOT NULL, csrf TEXT NOT NULL,
                    session_version INTEGER NOT NULL, created REAL NOT NULL
                );
            """)
            # Additive migration: preserve the existing single-role grant for every
            # account, then let the newer UI store multiple grants independently.
            db.execute("INSERT OR IGNORE INTO user_roles(username,role) SELECT username,role FROM users")
            self._migrate(db)
            self._reconcile_networks(db, self.config_networks)
        self.path.chmod(0o600)

    def _migrate(self, db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version >= self.SCHEMA_VERSION:
            return
        def columns(table):
            return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "source" not in columns("allowed_networks"):
            db.execute("ALTER TABLE allowed_networks ADD COLUMN source TEXT NOT NULL DEFAULT 'admin'")
        for name, ddl in (("session_version", "INTEGER NOT NULL DEFAULT 1"), ("bootstrap", "INTEGER NOT NULL DEFAULT 0"),
                          ("camera_link", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns("users"):
                db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name='user_capabilities'").fetchone()[0]
        if "'record'" not in definition:
            db.execute("ALTER TABLE user_capabilities RENAME TO user_capabilities_old")
            db.execute("""CREATE TABLE user_capabilities (
                username TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
                capability TEXT NOT NULL CHECK(capability IN ('watcher','record')),
                PRIMARY KEY(username,capability))""")
            db.execute("INSERT INTO user_capabilities(username,capability) SELECT username,capability FROM user_capabilities_old")
            db.execute("DROP TABLE user_capabilities_old")
        # Rows that match today's configuration were put there by an older version.
        for cidr in self.config_networks:
            db.execute("UPDATE allowed_networks SET source='config' WHERE cidr=?", (cidr,))
        # Older versions copied the global list into the first administrator's own
        # networks. A copy is not a restriction the operator chose, so drop it.
        mirrored = set(self.config_networks)
        for (username,) in db.execute("SELECT username FROM users").fetchall():
            owned = {row[0] for row in db.execute("SELECT cidr FROM user_networks WHERE username=?", (username,))}
            if owned and owned == mirrored:
                db.execute("DELETE FROM user_networks WHERE username=?", (username,))
                self.migration_notes.append(f"cleared copied source networks for account {username}")
        # Recording used to be allowed for every viewer; keep that for existing accounts.
        db.execute("INSERT OR IGNORE INTO user_capabilities(username,capability) SELECT username,'record' FROM users")
        db.execute(f"PRAGMA user_version={int(self.SCHEMA_VERSION)}")

    @staticmethod
    def _reconcile_networks(db, networks):
        wanted = set(networks)
        current = {row[0]: row[1] for row in db.execute("SELECT cidr,source FROM allowed_networks")}
        for cidr, source in current.items():
            if source == "config" and cidr not in wanted:
                db.execute("DELETE FROM allowed_networks WHERE cidr=?", (cidr,))
        for cidr in wanted:
            if cidr not in current:
                db.execute("INSERT INTO allowed_networks(cidr,source) VALUES (?,'config')", (cidr,))

    def reconcile_config_networks(self, networks):
        """Make the config-sourced rows match the configuration file."""
        checked = self._normalize_networks(list(networks))
        with self.lock, self._session() as db:
            self._reconcile_networks(db, checked)
        self.config_networks = checked

    def needs_bootstrap(self):
        with self.lock, self._session() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    def bootstrap_admin(self, username, password):
        """Create the root web administrator on an empty database."""
        if not self._valid_username(username) or not isinstance(password, str) or not password:
            raise ValueError("invalid bootstrap account")
        with self.lock, self._session() as db:
            if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise ValueError("bootstrap is closed")
            salt = secrets.token_bytes(16)
            db.execute("INSERT INTO users(username,salt,password_hash,role,bootstrap) VALUES (?,?,?,'web_admin',1)",
                       (username, salt, self._hash(password, salt)))
            db.execute("INSERT INTO user_roles(username,role) VALUES (?,'web_admin')", (username,))
            db.execute("INSERT INTO user_capabilities(username,capability) VALUES (?,'record')", (username,))

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

    _dummy_salt = b"\x00" * 16

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
        checked_networks = cls._normalize_networks(networks, required=False) if networks else []
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
            known = bool(row and row["enabled"])
            try:
                # Hash for unknown and disabled accounts too, so timing does not reveal them.
                actual = self._hash(password, row["salt"] if known else self._dummy_salt)
            except (ValueError, MemoryError):
                return None
            if not known or not hmac.compare_digest(actual, row["password_hash"]):
                return None
            allowed = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM allowed_networks")]
            owned = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM user_networks WHERE username=?", (username,))]
            if not any(address in network for network in allowed) or (owned and not any(address in network for network in owned)):
                return {"username": username, "role": row["role"], "source_allowed": False}
            grants = {item[0]: item[1] for item in db.execute("SELECT camera_id,permission FROM user_cameras WHERE username=?", (username,))}
            roles = [item[0] for item in db.execute("SELECT role FROM user_roles WHERE username=? ORDER BY role", (username,))]
            return {"username": username, "role": row["role"], "roles": roles, "source_allowed": True, "cameras": grants}

    def session_user(self, username, address):
        """Reload current access grants without repeating the password hash."""
        with self.lock, self._session() as db:
            row = db.execute("SELECT role,enabled,session_version,camera_link FROM users WHERE username=?", (username,)).fetchone()
            if not row or not row["enabled"]:
                return None
            allowed = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM allowed_networks")]
            owned = [ipaddress.ip_network(item[0]) for item in db.execute("SELECT cidr FROM user_networks WHERE username=?", (username,))]
            if not any(address in network for network in allowed) or (owned and not any(address in network for network in owned)):
                return None
            grants = {item[0]: item[1] for item in db.execute("SELECT camera_id,permission FROM user_cameras WHERE username=?", (username,))}
            roles = [item[0] for item in db.execute("SELECT role FROM user_roles WHERE username=? ORDER BY role", (username,))]
            capabilities = [item[0] for item in db.execute("SELECT capability FROM user_capabilities WHERE username=? ORDER BY capability", (username,))]
            return {"username": username, "role": row["role"], "roles": roles, "cameras": grants,
                    "capabilities": capabilities, "watcher": "watcher" in capabilities,
                    "session_version": row["session_version"], "camera_link": bool(row["camera_link"])}

    @classmethod
    def _normalize_capabilities(cls, capabilities, record=None, existing=None):
        """Return the stored capability list.

        ``record`` True/False is explicit. None keeps the existing state on update,
        and on create enables recording unless the account is a watcher.
        """
        if capabilities is None:
            values = []
        elif isinstance(capabilities, str):
            values = re.split(r"[,;\s]+", capabilities.strip())
        elif isinstance(capabilities, (list, tuple, set)):
            values = list(capabilities)
        else:
            raise ValueError("Capabilities must be a list of text values.")
        result = []
        for value in values:
            if value == "":
                continue
            if value not in cls.CAPABILITIES:
                raise ValueError("Unknown account capability.")
            if value not in result:
                result.append(value)
        if record is None:
            record = ("record" in existing) if existing is not None else ("record" in result or "watcher" not in result)
        result = [item for item in result if item != "record"]
        if record:
            result.append("record")
        return result

    @classmethod
    def _validate_networks(cls, db, networks):
        values = cls._normalize_networks(networks, required=False) if networks else []
        allowed = [ipaddress.ip_network(row[0]) for row in db.execute("SELECT cidr FROM allowed_networks")]
        for value in values:
            network = ipaddress.ip_network(value)
            if not any(network.version == parent.version and network.subnet_of(parent) for parent in allowed):
                raise ValueError("user network is outside the global allowlist")
        return values

    def create_user(self, username, password, role=None, camera_permissions=None, networks=None, camera_ids=(), roles=None, capabilities=None, record=None, camera_link=False):
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
        checked_capabilities = self._normalize_capabilities(capabilities, record)
        legacy_role = "web_admin" if "web_admin" in selected_roles else "camera_admin" if "camera_admin" in selected_roles else "viewer"
        with self.lock, self._session() as db:
            checked_networks = self._validate_networks(db, checked_networks)
            self._insert_user(db, username, password, legacy_role, [], [])
            if camera_link:
                db.execute("UPDATE users SET camera_link=1 WHERE username=?", (username,))
            db.execute("DELETE FROM user_roles WHERE username=?", (username,))
            for selected_role in selected_roles:
                db.execute("INSERT INTO user_roles(username,role) VALUES (?,?)", (username, selected_role))
            for camera_id, permission in checked_permissions.items():
                db.execute("INSERT INTO user_cameras(username,camera_id,permission) VALUES (?,?,?)", (username, camera_id, permission))
            for network in checked_networks:
                db.execute("INSERT INTO user_networks(username,cidr) VALUES (?,?)", (username, network))
            for capability in checked_capabilities:
                db.execute("INSERT INTO user_capabilities(username,capability) VALUES (?,?)", (username, capability))

    def update_access(self, username, role=None, camera_permissions=None, networks=None, camera_ids=(), password=None, roles=None, capabilities=None, record=None, camera_link=None):
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
            existing = [item[0] for item in db.execute("SELECT capability FROM user_capabilities WHERE username=?", (username,))]
            checked_capabilities = self._normalize_capabilities(capabilities, record, existing)
            if camera_link is not None:
                db.execute("UPDATE users SET camera_link=? WHERE username=?", (int(bool(camera_link)), username))
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
            db.execute("DELETE FROM user_capabilities WHERE username=?", (username,))
            for camera_id, permission in checked_permissions.items():
                db.execute("INSERT INTO user_cameras(username,camera_id,permission) VALUES (?,?,?)", (username, camera_id, permission))
            for network in checked_networks:
                db.execute("INSERT INTO user_networks(username,cidr) VALUES (?,?)", (username, network))
            for capability in checked_capabilities:
                db.execute("INSERT INTO user_capabilities(username,capability) VALUES (?,?)", (username, capability))
            if password is not None:
                salt = secrets.token_bytes(16)
                db.execute("UPDATE users SET salt=?,password_hash=?,session_version=session_version+1 WHERE username=?", (salt, self._hash(password, salt), username))

    def inventory(self):
        with self.lock, self._session() as db:
            result = []
            for user in db.execute("SELECT username,role,enabled,bootstrap,camera_link FROM users ORDER BY username"):
                grants = {row[0]: row[1] for row in db.execute("SELECT camera_id,permission FROM user_cameras WHERE username=? ORDER BY camera_id", (user["username"],))}
                networks = [row[0] for row in db.execute("SELECT cidr FROM user_networks WHERE username=? ORDER BY cidr", (user["username"],))]
                roles = [row[0] for row in db.execute("SELECT role FROM user_roles WHERE username=? ORDER BY role", (user["username"],))]
                capabilities = [row[0] for row in db.execute("SELECT capability FROM user_capabilities WHERE username=? ORDER BY capability", (user["username"],))]
                result.append({"username": user["username"], "role": user["role"], "roles": roles, "capabilities": capabilities, "enabled": bool(user["enabled"]), "cameras": grants, "networks": networks,
                               "bootstrap": bool(user["bootstrap"]), "camera_link": bool(user["camera_link"])})
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
            db.execute("UPDATE users SET enabled=?, session_version=session_version+? WHERE username=?", (int(enabled), 0 if enabled else 1, username))
            if not enabled:
                db.execute("DELETE FROM sessions WHERE username=?", (username,))

    def is_global_network_allowed(self, address):
        with self.lock, self._session() as db:
            return any(address in ipaddress.ip_network(row[0]) for row in db.execute("SELECT cidr FROM allowed_networks"))

    def list_networks(self, detail=False):
        with self.lock, self._session() as db:
            rows = db.execute("SELECT cidr,source FROM allowed_networks ORDER BY cidr").fetchall()
            return [{"cidr": row[0], "source": row[1]} for row in rows] if detail else [row[0] for row in rows]

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
                db.execute("INSERT OR IGNORE INTO allowed_networks(cidr,source) VALUES (?,'admin')", (network,))

    def add_network(self, cidr):
        self.add_networks(cidr)

    def remove_network(self, cidr, current_address):
        parsed = self._normalize_networks(cidr)
        if len(parsed) != 1:
            raise ValueError("Remove one IP or CIDR at a time.")
        network = parsed[0]
        with self.lock, self._session() as db:
            remaining = [ipaddress.ip_network(row[0]) for row in db.execute("SELECT cidr FROM allowed_networks WHERE cidr<>?", (network,))]
            if current_address is not None and not any(current_address in item for item in remaining):
                raise ValueError("cannot remove the current administrator source network")
            row = db.execute("SELECT source FROM allowed_networks WHERE cidr=?", (network,)).fetchone()
            if row and row[0] == "config":
                raise ValueError("This network comes from the configuration file. Remove it there and reload.")
            db.execute("DELETE FROM allowed_networks WHERE cidr=?", (network,))

    def set_password(self, username, password):
        if not self._valid_username(username):
            raise ValueError("Invalid username.")
        try:
            self._password_bytes(password, minimum=12)
        except ValueError as error:
            raise ValueError("Passwords must be 12 to 256 characters and valid Unicode.") from error
        with self.lock, self._session() as db:
            if not db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise KeyError("unknown user")
            salt = secrets.token_bytes(16)
            db.execute("UPDATE users SET salt=?,password_hash=?,session_version=session_version+1 WHERE username=?",
                       (salt, self._hash(password, salt), username))
            db.execute("DELETE FROM sessions WHERE username=?", (username,))

    def password_matches(self, username, password):
        """Check a password without applying source-network rules (CLI and warnings)."""
        with self.lock, self._session() as db:
            row = db.execute("SELECT salt,password_hash FROM users WHERE username=?", (username,)).fetchone()
            if not row:
                return False
            return hmac.compare_digest(self._hash(password, row["salt"]), row["password_hash"])

    def persist_session(self, digest, username, address, csrf, version):
        with self.lock, self._session() as db:
            db.execute("INSERT OR REPLACE INTO sessions(digest,username,address,csrf,session_version,created) VALUES (?,?,?,?,?,?)",
                       (digest, username, str(address), csrf, version or 1, time.time()))
            stale = db.execute("SELECT digest FROM sessions WHERE username=? ORDER BY created DESC LIMIT -1 OFFSET ?",
                               (username, self.WATCHER_SESSIONS_PER_USER)).fetchall()
            for (old,) in stale:
                db.execute("DELETE FROM sessions WHERE digest=?", (old,))
            return [old for (old,) in stale]

    def load_session(self, digest):
        with self.lock, self._session() as db:
            row = db.execute("SELECT * FROM sessions WHERE digest=?", (digest,)).fetchone()
            return dict(row) if row else None

    def delete_session(self, digest):
        with self.lock, self._session() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (digest,))

    def delete_user_sessions(self, username):
        with self.lock, self._session() as db:
            db.execute("DELETE FROM sessions WHERE username=?", (username,))

    @staticmethod
    def can(user, camera_id, action):
        if "web_admin" in user.get("roles", [user.get("role")]):
            return True
        grant = user.get("cameras", {}).get(camera_id)
        if action == "view":
            return grant in ("viewer", "camera_admin")
        if action == "record":
            if grant == "camera_admin":
                return True
            return grant == "viewer" and "record" in user.get("capabilities", ("record",))
        if action == "start":
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
