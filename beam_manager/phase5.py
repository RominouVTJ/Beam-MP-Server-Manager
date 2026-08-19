from __future__ import annotations

import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import sqlite3
import sys
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


from beam_manager import __version__


APP_VERSION = __version__
DEFAULT_PROFILE_ID = "primary"
SESSION_MINUTES = 60
TRUSTED_SESSION_DAYS = 14
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_SERVICE = re.compile(r"^[A-Za-z0-9_.@-]{1,80}(?:\.service)?$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_lan_address() -> str | None:
    """Return a private IPv4 address without sending application data."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError:
        try:
            address = socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    finally:
        probe.close()
    try:
        ip = ipaddress.ip_address(address)
        return address if ip.version == 4 and ip.is_private and not ip.is_loopback else None
    except ValueError:
        return None


def _safe_remote_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value.startswith("/") or ".." in path.parts or "\x00" in value or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise ValueError("Le chemin BeamMP doit etre un chemin Linux absolu sans '..'")
    return value.rstrip("/") or "/"


def _profile_id(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:48]
    return value or f"server-{secrets.token_hex(3)}"


class Phase5Store:
    """Durable Phase 5 state. Secrets never leave the dedicated auth tables."""

    def __init__(self, database: Path, data_dir: Path, session_secret_file: Path | None = None) -> None:
        self.database = database
        self.data_dir = data_dir
        self.session_secret_file = session_secret_file or data_dir / ".session-secret"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._session_secret = self._load_session_secret()
        self._password_hasher = PasswordHasher()
        self._initialize()

    def _load_session_secret(self) -> bytes:
        path = self.session_secret_file
        if path.is_file():
            secret = path.read_bytes().strip()
            if len(secret) < 32:
                raise RuntimeError("Le secret de session est trop court")
            return secret
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(48)
        try:
            with path.open("xb") as handle:
                handle.write(secret)
            path.chmod(0o600)
        except FileExistsError:
            secret = path.read_bytes().strip()
        return secret

    @property
    def setup_pairing_path(self) -> Path:
        return self.data_dir / "setup-pairing.secret"

    @staticmethod
    def _normalize_pairing_code(value: str) -> str:
        upper = value.strip().upper()
        raw = re.sub(r"[\s\-\u2010-\u2015]+", "", upper)
        if len(raw) in {12, 20} and re.fullmatch(r"[A-Z2-9]+", raw):
            return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))
        return upper

    def ensure_setup_pairing_code(self) -> str:
        """Create or return the permanent appliance security/recovery code."""
        path = self.setup_pairing_path
        if path.is_symlink():
            raise RuntimeError("Chemin d'appairage non securise")
        if path.is_file():
            code = self._normalize_pairing_code(path.read_text(encoding="ascii"))
            if not re.fullmatch(r"[A-Z2-9]{4}(?:-[A-Z2-9]{4}){2}(?:(?:-[A-Z2-9]{4}){2})?", code):
                raise RuntimeError("Code d'appairage invalide")
            return code
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        raw = "".join(secrets.choice(alphabet) for _ in range(12))
        code = "-".join(raw[index:index + 4] for index in range(0, 12, 4))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="ascii") as handle:
                handle.write(code + "\n")
            path.chmod(0o600)
        except FileExistsError:
            return self.ensure_setup_pairing_code()
        return code

    def pairing_required(self) -> bool:
        return bool(self.settings().get("firstboot_complete") and not self.admin_configured())

    def verify_setup_pairing_code(self, supplied: str | None) -> bool:
        if not supplied or not self.setup_pairing_path.is_file() or self.setup_pairing_path.is_symlink():
            return False
        try:
            expected = self._normalize_pairing_code(self.setup_pairing_path.read_text(encoding="ascii"))
        except OSError:
            return False
        return hmac.compare_digest(expected, self._normalize_pairing_code(supplied))

    def consume_setup_pairing_code(self) -> None:
        """Compatibility no-op: the appliance security code is permanent."""
        return

    def security_code(self) -> str:
        return self.ensure_setup_pairing_code()

    def verify_security_code(self, supplied: str | None) -> bool:
        return self.verify_setup_pairing_code(supplied)

    def active_session_count(self) -> int:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            row = db.execute("SELECT COUNT(*) AS count FROM auth_sessions WHERE expires_at > ?", (now,)).fetchone()
        return int(row["count"] if row else 0)

    def _token_hash(self, value: str) -> str:
        return hmac.new(self._session_secret, value.encode("utf-8"), hashlib.sha256).hexdigest()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS manager_settings(
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS server_profiles(
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    host TEXT NOT NULL,
                    ssh_port INTEGER NOT NULL,
                    ssh_user TEXT NOT NULL,
                    ssh_key_path TEXT NOT NULL,
                    beam_root TEXT NOT NULL,
                    service TEXT NOT NULL,
                    beam_port INTEGER NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','viewer')),
                    enabled INTEGER NOT NULL DEFAULT 1,
                    password_changed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_credentials(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_sessions(
                    token_hash TEXT PRIMARY KEY,
                    csrf_hash TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    trusted INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    profile_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    read_at TEXT
                );
                CREATE TABLE IF NOT EXISTS map_calibrations(
                    profile_id TEXT NOT NULL,
                    map_id TEXT NOT NULL,
                    image TEXT NOT NULL,
                    world_min_x REAL NOT NULL,
                    world_max_x REAL NOT NULL,
                    world_min_y REAL NOT NULL,
                    world_max_y REAL NOT NULL,
                    invert_x INTEGER NOT NULL DEFAULT 0,
                    invert_y INTEGER NOT NULL DEFAULT 1,
                    rotation REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id,map_id)
                );
                CREATE TABLE IF NOT EXISTS collections(
                    id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    image TEXT,
                    map_path TEXT NOT NULL,
                    active_mods_json TEXT NOT NULL,
                    disabled_mods_json TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS classification_overrides(
                    profile_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    resource_id TEXT NOT NULL,
                    manager_subtype TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(profile_id,provider,resource_id)
                );
                CREATE TABLE IF NOT EXISTS app_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            profile_columns = {row["name"] for row in db.execute("PRAGMA table_info(server_profiles)")}
            if "backend" not in profile_columns:
                db.execute("ALTER TABLE server_profiles ADD COLUMN backend TEXT NOT NULL DEFAULT 'ssh'")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(auth_sessions)")}
            if session_columns and "user_id" not in session_columns:
                db.execute("DROP TABLE auth_sessions")
                db.execute(
                    """CREATE TABLE auth_sessions(
                    token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    password_changed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    trusted INTEGER NOT NULL, created_at TEXT NOT NULL)"""
                )
            defaults = {
                "lan_enabled": False,
                "web_access_enabled": False,
                "public_server_address": "",
                "autostart_enabled": False,
                "open_browser_on_start": True,
                "auto_restart_after_crash": False,
                "demo_mode": False,
                "default_language": os.environ.get("BEAM_MANAGER_LANGUAGE", "en") if os.environ.get("BEAM_MANAGER_LANGUAGE", "en") in {"en", "fr"} else "en",
                "country": "",
                "locale": "",
                "timezone": os.environ.get("BEAM_MANAGER_TIMEZONE", "UTC"),
                "keyboard_layout": "",
                "firstboot_complete": False,
                "web_setup_complete": False,
                "notification_preferences": {
                    "player_join": True,
                    "player_disconnect": True,
                    "server_offline": True,
                    "mod_updates": False,
                    "errors": True,
                    "windows": True,
                },
            }
            now = utc_now().isoformat()
            for key, value in defaults.items():
                db.execute(
                    "INSERT OR IGNORE INTO manager_settings(key,value_json,updated_at) VALUES(?,?,?)",
                    (key, json.dumps(value), now),
                )

    def ensure_primary_profile(self, settings: Any) -> None:
        now = utc_now().isoformat()
        with self.connect() as db:
            if db.execute("SELECT 1 FROM server_profiles LIMIT 1").fetchone():
                return
            db.execute(
                """INSERT INTO server_profiles(
                    id,name,host,ssh_port,ssh_user,ssh_key_path,beam_root,service,
                    beam_port,selected,created_at,updated_at,backend
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    DEFAULT_PROFILE_ID,
                    "Beam-server",
                    settings.server_host,
                    22,
                    settings.ssh_user,
                    str(settings.ssh_key_path),
                    settings.beam_root,
                    settings.systemd_service,
                    settings.server_port,
                    1,
                    now,
                    now,
                    settings.server_backend,
                ),
            )

    def settings(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("SELECT key,value_json FROM manager_settings").fetchall()
        values = {row["key"]: json.loads(row["value_json"]) for row in rows}
        values.update(
            {
                "version": APP_VERSION,
                "admin_configured": self.admin_configured(),
                "lan_address": local_lan_address(),
            }
        )
        return values

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "lan_enabled",
            "web_access_enabled",
            "public_server_address",
            "open_browser_on_start",
            "auto_restart_after_crash",
            "demo_mode",
            "notification_preferences",
            "default_language",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"Parametre non modifiable: {sorted(unknown)[0]}")
        if changes.get("lan_enabled") and not self.admin_configured():
            raise ValueError("Configurez le compte administrateur avant d'activer le LAN")
        current = self.settings()
        if "default_language" in changes and changes["default_language"] not in {"en", "fr"}:
            raise ValueError("Langue non prise en charge")
        web_access_enabled = changes.get("web_access_enabled", current.get("web_access_enabled", False))
        public_address = changes.get("public_server_address", current.get("public_server_address", ""))
        if web_access_enabled and not public_address:
            raise ValueError("Renseignez l'IP publique ou le domaine avant d'afficher l'acces Internet")
        now = utc_now().isoformat()
        with self.connect() as db:
            for key, value in changes.items():
                db.execute(
                    """INSERT INTO manager_settings(key,value_json,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (key, json.dumps(value), now),
                )
        return self.settings()

    def apply_firstboot_settings(self, values: dict[str, str]) -> dict[str, Any]:
        """Persist the root wizard's non-secret system profile in one transaction."""
        required = {"default_language", "country", "locale", "timezone", "keyboard_layout"}
        if set(values) != required:
            raise ValueError("Profil First Run incomplet")
        if values["default_language"] not in {"en", "fr"}:
            raise ValueError("Langue First Run non prise en charge")
        if values["locale"] not in {"fr_FR.UTF-8", "en_GB.UTF-8", "en_US.UTF-8"}:
            raise ValueError("Locale First Run non prise en charge")
        if values["keyboard_layout"] not in {"fr", "gb", "us"}:
            raise ValueError("Clavier First Run non pris en charge")
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # The appliance is intentionally LAN-closed before First Run. Once the
            # console wizard is complete, LAN access must open so the user can reach
            # /setup with the one-time pairing code. This internal transition is safe
            # before an admin exists because bootstrap remains protected by pairing.
            firstboot_values = {**values, "firstboot_complete": True, "lan_enabled": True}
            for key, value in firstboot_values.items():
                db.execute(
                    """INSERT INTO manager_settings(key,value_json,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                    (key, json.dumps(value), now),
                )
        return self.settings()

    def _set_internal_setting(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO manager_settings(key,value_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (key, json.dumps(value), utc_now().isoformat()),
            )

    def admin_configured(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1 LIMIT 1").fetchone() is not None

    @staticmethod
    def _validate_password(username: str, password: str) -> None:
        if not 12 <= len(password) <= 256:
            raise ValueError("Le mot de passe doit contenir entre 12 et 256 caracteres")
        if any(ord(character) < 32 or ord(character) == 127 for character in password):
            raise ValueError("Le mot de passe contient un caractere de controle interdit")
        if password.casefold() == username.casefold():
            raise ValueError("Le mot de passe ne peut pas etre identique au nom utilisateur")
        weak = {
            "password1234", "password123!", "motdepasse123", "azerty123456",
            "qwerty123456", "adminadmin12", "123456789012", "beammp123456",
        }
        if password.casefold() in weak:
            raise ValueError("Ce mot de passe est trop courant")

    def bootstrap_admin(self, username: str, password: str) -> dict[str, Any]:
        username = self._validate_username(username)
        self._validate_password(username, password)
        now = utc_now().isoformat()
        password_hash = self._password_hasher.hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users WHERE role='admin' AND enabled=1 LIMIT 1").fetchone():
                raise FileExistsError("Le bootstrap administrateur est deja termine")
            db.execute(
                "INSERT INTO users(username,password_hash,role,enabled,password_changed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (username, password_hash, "admin", 1, now, now, now),
            )
        return self.user(username)

    @staticmethod
    def _password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)

    def set_admin_password(self, password: str) -> None:
        """Compatibility helper for local tests; production account management uses the CLI."""
        with self.connect() as db:
            row = db.execute("SELECT 1 FROM users WHERE username='admin'").fetchone()
        if row:
            self.set_user_password("admin", password)
        else:
            self.add_user("admin", password, "admin")

    def authenticate(self, password: str, trusted: bool) -> tuple[str, str, datetime] | None:
        result = self.authenticate_user("admin", password, trusted)
        return result[:3] if result else None

    def authenticate_user(self, username: str, password: str, trusted: bool) -> tuple[str, str, datetime, dict[str, Any]] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (username.strip(),)).fetchone()
        if row is None or not row["enabled"]:
            return None
        try:
            if not self._password_hasher.verify(row["password_hash"], password):
                return None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return None
        if self._password_hasher.check_needs_rehash(row["password_hash"]):
            self.set_user_password(row["username"], password)
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        expires = utc_now() + (timedelta(days=TRUSTED_SESSION_DAYS) if trusted else timedelta(minutes=SESSION_MINUTES))
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (utc_now().isoformat(),))
            db.execute(
                """INSERT INTO auth_sessions(
                token_hash,csrf_hash,user_id,password_changed_at,expires_at,trusted,created_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    self._token_hash(token), self._token_hash(csrf), row["id"],
                    row["password_changed_at"],
                    expires.isoformat(),
                    int(trusted),
                    utc_now().isoformat(),
                ),
            )
        return token, csrf, expires, {"username": row["username"], "role": row["role"]}

    def validate_session(self, token: str | None, csrf: str | None = None) -> bool:
        if not token:
            return False
        token_hash = self._token_hash(token)
        with self.connect() as db:
            row = db.execute(
                """SELECT s.csrf_hash,s.expires_at,s.password_changed_at,u.enabled,u.password_changed_at AS current_password_changed_at
                FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?""", (token_hash,)
            ).fetchone()
        if (row is None or not row["enabled"] or row["password_changed_at"] != row["current_password_changed_at"]
                or datetime.fromisoformat(row["expires_at"]) <= utc_now()):
            return False
        return csrf is None or hmac.compare_digest(
            row["csrf_hash"], self._token_hash(csrf)
        )

    def session_identity(self, token: str | None) -> dict[str, Any] | None:
        if not self.validate_session(token):
            return None
        with self.connect() as db:
            row = db.execute(
                """SELECT u.username,u.role FROM auth_sessions s JOIN users u ON u.id=s.user_id
                WHERE s.token_hash=?""", (self._token_hash(token or ""),)
            ).fetchone()
        return dict(row) if row else None

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._token_hash(token),))

    @staticmethod
    def _validate_username(username: str) -> str:
        value = username.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", value):
            raise ValueError("Le nom utilisateur doit contenir 3 a 64 lettres, chiffres, points, tirets ou underscores")
        return value

    def add_user(self, username: str, password: str, role: str = "admin") -> dict[str, Any]:
        username = self._validate_username(username)
        if role not in {"admin", "viewer"}:
            raise ValueError("Role utilisateur invalide")
        self._validate_password(username, password)
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO users(username,password_hash,role,enabled,password_changed_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (username, self._password_hasher.hash(password), role, 1, now, now, now),
            )
        return self.user(username)

    def user(self, username: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT username,role,enabled,created_at,updated_at FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        if row is None:
            raise LookupError("Utilisateur introuvable")
        return dict(row) | {"enabled": bool(row["enabled"])}

    def users(self) -> list[dict[str, Any]]:
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (now,))
            rows = db.execute(
                """SELECT u.username,u.role,u.enabled,u.created_at,u.updated_at,
                (SELECT COUNT(*) FROM auth_sessions s WHERE s.user_id=u.id AND s.expires_at>?) AS active_sessions,
                (SELECT MAX(s.created_at) FROM auth_sessions s WHERE s.user_id=u.id) AS last_login
                FROM users u ORDER BY u.username COLLATE NOCASE""", (now,)
            ).fetchall()
        return [dict(row) | {"enabled": bool(row["enabled"])} for row in rows]

    def set_user_password(self, username: str, password: str) -> None:
        username = self._validate_username(username)
        self._validate_password(username, password)
        now = utc_now().isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE users SET password_hash=?,password_changed_at=?,updated_at=? WHERE username=? COLLATE NOCASE",
                (self._password_hasher.hash(password), now, now, username),
            )
            if cursor.rowcount != 1:
                raise LookupError("Utilisateur introuvable")
            db.execute("DELETE FROM auth_sessions WHERE user_id=(SELECT id FROM users WHERE username=? COLLATE NOCASE)", (username,))

    def set_user_enabled(self, username: str, enabled: bool) -> None:
        with self.connect() as db:
            cursor = db.execute("UPDATE users SET enabled=?,updated_at=? WHERE username=? COLLATE NOCASE", (int(enabled), utc_now().isoformat(), username))
            if cursor.rowcount != 1:
                raise LookupError("Utilisateur introuvable")
            if not enabled:
                db.execute("DELETE FROM auth_sessions WHERE user_id=(SELECT id FROM users WHERE username=? COLLATE NOCASE)", (username,))

    def set_user_role(self, username: str, role: str) -> None:
        if role not in {"admin", "viewer"}:
            raise ValueError("Role utilisateur invalide")
        with self.connect() as db:
            cursor = db.execute("UPDATE users SET role=?,updated_at=? WHERE username=? COLLATE NOCASE", (role, utc_now().isoformat(), username))
            if cursor.rowcount != 1:
                raise LookupError("Utilisateur introuvable")
            db.execute("DELETE FROM auth_sessions WHERE user_id=(SELECT id FROM users WHERE username=? COLLATE NOCASE)", (username,))

    def delete_user(self, username: str) -> None:
        with self.connect() as db:
            cursor = db.execute("DELETE FROM users WHERE username=? COLLATE NOCASE", (username,))
            if cursor.rowcount != 1:
                raise LookupError("Utilisateur introuvable")

    @staticmethod
    def _profile(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "name": row["name"], "host": row["host"],
            "ssh_port": row["ssh_port"], "ssh_user": row["ssh_user"],
            "ssh_key_path": row["ssh_key_path"], "beam_root": row["beam_root"],
            "service": row["service"], "beam_port": row["beam_port"],
            "selected": bool(row["selected"]), "backend": row["backend"],
        }

    def profiles(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM server_profiles ORDER BY selected DESC,name COLLATE NOCASE").fetchall()
        return [self._profile(row) for row in rows]

    def selected_profile(self) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM server_profiles WHERE selected=1 LIMIT 1").fetchone()
            if row is None:
                row = db.execute("SELECT * FROM server_profiles ORDER BY created_at LIMIT 1").fetchone()
        if row is None:
            raise LookupError("Aucun profil serveur configure")
        return self._profile(row)

    def add_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        identifier = payload.get("id") or _profile_id(payload["name"])
        if not _PROFILE_ID.fullmatch(identifier):
            raise ValueError("Identifiant de serveur invalide")
        service = payload["service"].removesuffix(".service")
        if not _SERVICE.fullmatch(service):
            raise ValueError("Nom de service systemd invalide")
        root = _safe_remote_path(payload["beam_root"])
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO server_profiles(
                id,name,host,ssh_port,ssh_user,ssh_key_path,beam_root,service,
                beam_port,selected,created_at,updated_at,backend
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identifier, payload["name"].strip(), payload["host"].strip(),
                    int(payload.get("ssh_port", 22)), payload["ssh_user"].strip(),
                    str(Path(payload["ssh_key_path"]).expanduser().resolve()), root,
                    service, int(payload.get("beam_port", 30814)), 0, now, now, "ssh",
                ),
            )
        return next(item for item in self.profiles() if item["id"] == identifier)

    def select_profile(self, identifier: str) -> dict[str, Any]:
        if not _PROFILE_ID.fullmatch(identifier):
            raise ValueError("Identifiant de serveur invalide")
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM server_profiles WHERE id=?", (identifier,)).fetchone():
                raise LookupError("Profil serveur introuvable")
            db.execute("UPDATE server_profiles SET selected=CASE WHEN id=? THEN 1 ELSE 0 END", (identifier,))
        return self.selected_profile()

    def profile_data_dir(self, identifier: str) -> Path:
        if identifier == DEFAULT_PROFILE_ID:
            return self.data_dir
        path = (self.data_dir / "servers" / identifier).resolve()
        if self.data_dir.resolve() not in path.parents:
            raise ValueError("Dossier de profil invalide")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM notifications WHERE profile_id=? ORDER BY id DESC LIMIT ?",
                (profile, min(max(limit, 1), 200)),
            ).fetchall()
        return [dict(row) for row in rows]

    def notify(self, kind: str, title: str, message: str) -> dict[str, Any]:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO notifications(profile_id,kind,title,message,created_at) VALUES(?,?,?,?,?)",
                (profile, kind[:40], title[:160], message[:1000], utc_now().isoformat()),
            )
            identifier = cursor.lastrowid
        return next(item for item in self.notifications() if item["id"] == identifier)

    def mark_notifications_read(self) -> None:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            db.execute(
                "UPDATE notifications SET read_at=? WHERE profile_id=? AND read_at IS NULL",
                (utc_now().isoformat(), profile),
            )

    def save_calibration(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.selected_profile()["id"]
        if payload["world_min_x"] == payload["world_max_x"] or payload["world_min_y"] == payload["world_max_y"]:
            raise ValueError("Les points de calibration doivent couvrir une surface")
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                """INSERT INTO map_calibrations VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(profile_id,map_id) DO UPDATE SET image=excluded.image,
                world_min_x=excluded.world_min_x,world_max_x=excluded.world_max_x,
                world_min_y=excluded.world_min_y,world_max_y=excluded.world_max_y,
                invert_x=excluded.invert_x,invert_y=excluded.invert_y,
                rotation=excluded.rotation,updated_at=excluded.updated_at""",
                (
                    profile, payload["map_id"], payload["image"], payload["world_min_x"],
                    payload["world_max_x"], payload["world_min_y"], payload["world_max_y"],
                    int(payload.get("invert_x", False)), int(payload.get("invert_y", True)),
                    float(payload.get("rotation", 0)), now,
                ),
            )
        calibration = self.calibration(payload["map_id"])
        root = self.profile_data_dir(profile) / "maps" / payload["map_id"]
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / "map.tmp.json"
        temporary.write_text(json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(root / "map.json")
        return calibration

    def calibration(self, map_id: str) -> dict[str, Any] | None:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM map_calibrations WHERE profile_id=? AND map_id=?", (profile, map_id)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["invert_x"] = bool(result["invert_x"])
        result["invert_y"] = bool(result["invert_y"])
        return result

    def calibrations(self) -> list[dict[str, Any]]:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM map_calibrations WHERE profile_id=? ORDER BY map_id", (profile,)
            ).fetchall()
        return [dict(row) | {"invert_x": bool(row["invert_x"]), "invert_y": bool(row["invert_y"])} for row in rows]

    def create_collection(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile = self.selected_profile()["id"]
        identifier = secrets.token_hex(10)
        now = utc_now().isoformat()
        with self.connect() as db:
            db.execute(
                "INSERT INTO collections VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier, profile, payload["name"].strip(), payload.get("description", "").strip(),
                    payload.get("image"), payload["map_path"], json.dumps(payload["active_mods"]),
                    json.dumps(payload["disabled_mods"]), json.dumps(payload.get("config", {})), now, now,
                ),
            )
        return self.collection(identifier)

    @staticmethod
    def _collection(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["active_mods"] = json.loads(result.pop("active_mods_json"))
        result["disabled_mods"] = json.loads(result.pop("disabled_mods_json"))
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def collections(self) -> list[dict[str, Any]]:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            rows = db.execute("SELECT * FROM collections WHERE profile_id=? ORDER BY updated_at DESC", (profile,)).fetchall()
        return [self._collection(row) for row in rows]

    def collection(self, identifier: str) -> dict[str, Any]:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            row = db.execute("SELECT * FROM collections WHERE id=? AND profile_id=?", (identifier, profile)).fetchone()
        if row is None:
            raise LookupError("Collection introuvable")
        return self._collection(row)

    def set_classification_override(self, provider: str, resource_id: str, subtype: str) -> None:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            db.execute(
                """INSERT INTO classification_overrides VALUES(?,?,?,?,?)
                ON CONFLICT(profile_id,provider,resource_id) DO UPDATE SET
                manager_subtype=excluded.manager_subtype,updated_at=excluded.updated_at""",
                (profile, provider, resource_id, subtype, utc_now().isoformat()),
            )

    def classification_override(self, provider: str, resource_id: str) -> str | None:
        profile = self.selected_profile()["id"]
        with self.connect() as db:
            row = db.execute(
                "SELECT manager_subtype FROM classification_overrides WHERE profile_id=? AND provider=? AND resource_id=?",
                (profile, provider, resource_id),
            ).fetchone()
        return row["manager_subtype"] if row else None

    def app_log(self, level: str, message: str) -> None:
        cleaned = re.sub(r"(?i)(authkey|password|token|secret)(\s*[:=]\s*)\S+", r"\1\2[MASQUE]", message)
        with self.connect() as db:
            db.execute("INSERT INTO app_events(level,message,created_at) VALUES(?,?,?)", (level, cleaned[:2000], utc_now().isoformat()))
            db.execute("DELETE FROM app_events WHERE id NOT IN (SELECT id FROM app_events ORDER BY id DESC LIMIT 2000)")

    def app_logs(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM app_events ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 1000),)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def export_bytes(self) -> bytes:
        """Portable configuration only; credentials and sessions are intentionally excluded."""
        allowed = (
            "manager_settings", "server_profiles", "notifications", "map_calibrations",
            "collections", "classification_overrides", "app_events",
        )
        payload: dict[str, Any] = {"format": 1, "app_version": APP_VERSION, "exported_at": utc_now().isoformat(), "tables": {}}
        with self.connect() as db:
            for table in allowed:
                rows = [dict(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]
                if table == "server_profiles":
                    for row in rows:
                        row["ssh_key_path"] = ""
                payload["tables"][table] = rows
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("beamserver-manager.json", json.dumps(payload, ensure_ascii=False, indent=2))
            maps = self.data_dir / "maps"
            if maps.is_dir():
                for file in maps.rglob("*"):
                    if file.is_file() and file.stat().st_size <= 25_000_000:
                        archive.write(file, f"maps/{file.relative_to(maps).as_posix()}")
        return buffer.getvalue()

    @staticmethod
    def inspect_export(raw: bytes) -> dict[str, Any]:
        if len(raw) > 100_000_000:
            raise ValueError("Export trop volumineux")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if "beamserver-manager.json" not in names or any(".." in PurePosixPath(name).parts for name in names):
                raise ValueError("Archive Beam-MP-Server-Manager invalide")
            payload = json.loads(archive.read("beamserver-manager.json"))
        if payload.get("format") != 1 or not isinstance(payload.get("tables"), dict):
            raise ValueError("Format d'export non pris en charge")
        counts = {name: len(rows) for name, rows in payload["tables"].items() if isinstance(rows, list)}
        return {"format": 1, "app_version": payload.get("app_version"), "counts": counts, "contains_secrets": any(name in payload["tables"] for name in ("admin_credentials", "auth_sessions"))}

    def stage_import(self, raw: bytes) -> dict[str, Any]:
        preview = self.inspect_export(raw)
        if preview["contains_secrets"]:
            raise ValueError("L'archive contient des tables sensibles interdites")
        token = secrets.token_urlsafe(24)
        root = self.data_dir / "imports"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{token}.zip").write_bytes(raw)
        return preview | {"token": token}

    def apply_staged_import(self, token: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", token):
            raise ValueError("Jeton d'import invalide")
        source = self.data_dir / "imports" / f"{token}.zip"
        if not source.is_file():
            raise ValueError("Import expire ou introuvable")
        raw = source.read_bytes()
        self.inspect_export(raw)
        backup_root = self.data_dir / "import-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / f"before-import-{utc_now().strftime('%Y%m%d-%H%M%S')}.zip"
        backup.write_bytes(self.export_bytes())
        allowed = {
            "manager_settings", "server_profiles", "notifications", "map_calibrations",
            "collections", "classification_overrides", "app_events",
        }
        imported: dict[str, int] = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            payload = json.loads(archive.read("beamserver-manager.json"))
            with self.connect() as db:
                for table, rows in payload["tables"].items():
                    if table not in allowed or not isinstance(rows, list):
                        continue
                    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
                    count = 0
                    for row in rows:
                        if not isinstance(row, dict) or set(row) != columns:
                            continue
                        if table == "manager_settings" and row.get("key") in {"lan_enabled", "autostart_enabled", "demo_mode"}:
                            continue
                        if table == "server_profiles":
                            row["selected"] = 0
                            if not row.get("ssh_key_path"):
                                existing = db.execute("SELECT ssh_key_path FROM server_profiles WHERE id=?", (row.get("id"),)).fetchone()
                                row["ssh_key_path"] = existing["ssh_key_path"] if existing else "A_CONFIGURER"
                        names = sorted(columns)
                        placeholders = ",".join("?" for _ in names)
                        db.execute(
                            f"INSERT OR REPLACE INTO {table}({','.join(names)}) VALUES({placeholders})",
                            tuple(row[name] for name in names),
                        )
                        count += 1
                    imported[table] = count
            maps_root = (self.data_dir / "maps").resolve()
            for name in archive.namelist():
                path = PurePosixPath(name)
                if not name.startswith("maps/") or path.name == "" or ".." in path.parts:
                    continue
                target = (self.data_dir / Path(*path.parts)).resolve()
                if maps_root != target and maps_root not in target.parents:
                    raise ValueError("Chemin de minimap interdit dans l'archive")
                if archive.getinfo(name).file_size > 25_000_000:
                    raise ValueError("Image de minimap trop volumineuse")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM server_profiles WHERE selected=1").fetchone():
                chosen = db.execute(
                    "SELECT id FROM server_profiles ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,created_at LIMIT 1",
                    (DEFAULT_PROFILE_ID,),
                ).fetchone()
                if chosen:
                    db.execute("UPDATE server_profiles SET selected=CASE WHEN id=? THEN 1 ELSE 0 END", (chosen["id"],))
        source.unlink(missing_ok=True)
        return {"imported": imported, "safety_backup": str(backup), "secrets_preserved": True}

    def support_report(self, health: dict[str, Any]) -> dict[str, Any]:
        profile = self.selected_profile()
        return {
            "manager_version": APP_VERSION,
            "generated_at": utc_now().isoformat(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "selected_server": {k: profile[k] for k in ("id", "name", "host", "beam_port", "service")},
            "health": health,
            "recent_errors": [item for item in self.app_logs(100) if item["level"] == "ERROR"][-20:],
            "secrets_redacted": True,
        }


def encode_report(report: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(report, ensure_ascii=False, indent=2).encode()).decode()
