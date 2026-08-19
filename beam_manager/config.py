from __future__ import annotations

import os
import platform
import re
import sys
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Beam-MP-Server-Manager"
        return root / "data"
    if platform.system() == "Linux":
        return Path("/var/lib/beam-manager")
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Beam-MP-Server-Manager" / "data"


def _default_backend() -> str:
    return "local"


def _default_manager_host() -> str:
    return "0.0.0.0" if platform.system() == "Linux" else "127.0.0.1"


def _default_require_auth() -> bool:
    return platform.system() == "Linux"


def _default_secret_file() -> Path:
    if platform.system() == "Linux":
        return Path("/etc/beam-manager/session.secret")
    return _default_data_dir() / ".session-secret"


class Settings(BaseSettings):
    """Local application settings. Secrets are deliberately absent."""

    model_config = SettingsConfigDict(
        extra="ignore",
    )

    manager_host: str = Field(default_factory=_default_manager_host, alias="BEAM_MANAGER_HOST")
    manager_port: int = Field(default=8765, alias="BEAM_MANAGER_PORT")
    app_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parent.parent, alias="BEAM_MANAGER_APP_ROOT")
    require_auth: bool = Field(default_factory=_default_require_auth, alias="BEAM_MANAGER_REQUIRE_AUTH")
    lan_network: str = Field(default="", alias="BEAM_MANAGER_LAN_NETWORK")
    language: str = Field(default="en", alias="BEAM_MANAGER_LANGUAGE")
    timezone: str = Field(default="UTC", alias="BEAM_MANAGER_TIMEZONE")
    session_secret_file: Path = Field(default_factory=_default_secret_file, alias="BEAM_MANAGER_SESSION_SECRET_FILE")
    server_backend: str = Field(default_factory=_default_backend, alias="BEAM_SERVER_BACKEND")
    server_host: str = Field(default="127.0.0.1", alias="BEAM_SERVER_HOST")
    server_port: int = Field(default=30814, alias="BEAM_SERVER_PORT")
    ssh_user: str = Field(default="beammanager", alias="BEAM_SSH_USER")
    ssh_key_path: Path = Field(
        default_factory=lambda: Path.home() / ".ssh" / "id_ed25519",
        alias="BEAM_SSH_KEY_PATH",
    )
    ssh_timeout: float = Field(default=8.0, alias="BEAM_SSH_TIMEOUT")
    systemd_service: str = Field(default="beammp", alias="BEAM_SYSTEMD_SERVICE")
    beam_root: str = Field(default="/opt/beammp", alias="BEAM_ROOT")
    server_config_path: str = Field(
        default="/opt/beammp/ServerConfig.toml",
        alias="BEAM_CONFIG_PATH",
    )
    active_mods_path: str = Field(
        default="/opt/beammp/Resources/Client",
        alias="BEAM_ACTIVE_MODS_PATH",
    )
    disabled_mods_path: str = Field(
        default="/opt/beammp/DisabledMods",
        alias="BEAM_DISABLED_MODS_PATH",
    )
    backups_path: str = Field(default="/opt/beammp/Backups", alias="BEAM_BACKUPS_PATH")
    log_path: str = Field(default="/opt/beammp/Server.log", alias="BEAM_LOG_PATH")
    old_log_path: str = Field(default="/opt/beammp/Server.old.log", alias="BEAM_OLD_LOG_PATH")
    telemetry_path: str = Field(
        default="/opt/beammp/Resources/Server/BeamServerManager/data/telemetry.json",
        alias="BEAM_TELEMETRY_PATH",
    )
    upload_max_bytes: int = Field(
        default=2_147_483_648,
        alias="BEAM_UPLOAD_MAX_BYTES",
        ge=1_048_576,
        le=10_737_418_240,
    )
    internet_timeout: float = Field(
        default=15.0, alias="BEAM_INTERNET_TIMEOUT", ge=3, le=60
    )
    allow_demo: bool = Field(default=False, alias="BEAM_MANAGER_ALLOW_DEMO")
    beammp_github_repository: str = Field(
        default="BeamMP/BeamMP-Server", alias="BEAMMP_GITHUB_REPOSITORY"
    )
    data_dir: Path = Field(default_factory=_default_data_dir, alias="BEAM_MANAGER_DATA_DIR")

    @model_validator(mode="after")
    def public_bind_requires_authentication(self) -> "Settings":
        if self.manager_host == "0.0.0.0" and not self.require_auth:
            raise ValueError("L'ecoute LAN exige BEAM_MANAGER_REQUIRE_AUTH=true")
        return self

    @field_validator("manager_host")
    @classmethod
    def manager_must_be_loopback(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "0.0.0.0"}:
            raise ValueError("BEAM_MANAGER_HOST doit etre 127.0.0.1, localhost ou 0.0.0.0")
        return value

    @field_validator("server_backend")
    @classmethod
    def backend_is_known(cls, value: str) -> str:
        if value not in {"local", "ssh"}:
            raise ValueError("BEAM_SERVER_BACKEND doit etre local ou ssh")
        return value

    @field_validator("language")
    @classmethod
    def language_is_supported(cls, value: str) -> str:
        value = value.casefold()
        if value not in {"fr", "en"}:
            raise ValueError("BEAM_MANAGER_LANGUAGE doit etre fr ou en")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_is_known(cls, value: str) -> str:
        if value == "UTC":
            return value
        if not re.fullmatch(r"[A-Za-z_+-]+(?:/[A-Za-z0-9_+.-]+)+", value):
            raise ValueError("BEAM_MANAGER_TIMEZONE doit etre un fuseau IANA valide")
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            if platform.system() != "Windows":
                raise ValueError("BEAM_MANAGER_TIMEZONE doit etre un fuseau IANA valide") from exc
        return value

    @field_validator("lan_network")
    @classmethod
    def lan_network_is_private(cls, value: str) -> str:
        import ipaddress
        if not value.strip():
            return ""
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or not network.is_private:
            raise ValueError("BEAM_MANAGER_LAN_NETWORK doit etre un reseau IPv4 prive")
        return str(network)

    @field_validator("ssh_key_path", mode="before")
    @classmethod
    def expand_windows_environment(cls, value: object) -> Path:
        raw = str(value)
        for name, replacement in os.environ.items():
            raw = raw.replace(f"%{name}%", replacement)
        return Path(os.path.expanduser(os.path.expandvars(raw))).resolve()

    @field_validator("session_secret_file", mode="before")
    @classmethod
    def expand_session_secret_path(cls, value: object) -> Path:
        return Path(os.path.expanduser(os.path.expandvars(str(value)))).resolve()

    @field_validator("app_root", mode="before")
    @classmethod
    def expand_app_root(cls, value: object) -> Path:
        return Path(os.path.expanduser(os.path.expandvars(str(value)))).resolve()

    @field_validator("systemd_service")
    @classmethod
    def service_is_fixed_safe_name(cls, value: str) -> str:
        if value not in {"beammp", "beammp.service"}:
            raise ValueError("Seul le service beammp est autorise")
        return "beammp"

    @field_validator("beammp_github_repository")
    @classmethod
    def official_beammp_repository_only(cls, value: str) -> str:
        if value != "BeamMP/BeamMP-Server":
            raise ValueError("Seul le depot officiel BeamMP/BeamMP-Server est autorise")
        return value

    @field_validator(
        "beam_root",
        "server_config_path",
        "active_mods_path",
        "disabled_mods_path",
        "backups_path",
        "log_path",
        "old_log_path",
        "telemetry_path",
    )
    @classmethod
    def remote_paths_are_fixed_under_beammp(cls, value: str) -> str:
        if ".." in value.split("/") or not value.startswith("/opt/beammp"):
            raise ValueError("Les chemins distants doivent rester sous /opt/beammp")
        return value.rstrip("/") or "/"


@lru_cache
def get_settings() -> Settings:
    # An env file is opt-in and resolved explicitly, never relative to cwd.
    env_file = os.environ.get("BEAM_MANAGER_ENV_FILE")
    return Settings(_env_file=Path(env_file).expanduser().resolve() if env_file else None)
