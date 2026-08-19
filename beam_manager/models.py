from datetime import datetime
from typing import Annotated, Literal
import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


SERVER_CONFIG_ALIASES = {
    "name": "Name",
    "description": "Description",
    "private": "Private",
    "max_players": "MaxPlayers",
    "max_cars": "MaxCars",
    "map_path": "Map",
    "tags": "Tags",
    "log_chat": "LogChat",
    "allow_guests": "AllowGuests",
    "port": "Port",
    "debug": "Debug",
    "ip": "IP",
    "information_packet": "InformationPacket",
    "resource_folder": "ResourceFolder",
}


def server_config_alias(field_name: str) -> str:
    return SERVER_CONFIG_ALIASES.get(field_name, field_name)


class ServerStatus(BaseModel):
    online: bool
    service_state: str
    server_host: str
    server_port: int
    checked_at: datetime


class ServiceActionResponse(BaseModel):
    action: Literal["start", "stop", "restart"]
    success: bool
    service_state: str
    message: str


class ServerConfigPublic(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=server_config_alias)

    name: str
    description: str
    private: bool
    max_players: int
    max_cars: int
    map_path: str
    tags: str
    log_chat: bool
    allow_guests: bool
    port: int
    debug: bool = False
    ip: str = "::"
    information_packet: bool = True
    resource_folder: str = "Resources"


class ServerConfigPatch(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        alias_generator=server_config_alias,
    )

    name: Annotated[str | None, Field(min_length=1, max_length=250)] = None
    description: Annotated[str | None, Field(max_length=1000)] = None
    private: bool | None = None
    max_players: Annotated[int | None, Field(ge=1, le=128)] = None
    max_cars: Annotated[int | None, Field(ge=1, le=100)] = None
    tags: Annotated[str | None, Field(max_length=100)] = None
    log_chat: bool | None = None
    allow_guests: bool | None = None
    port: Annotated[int | None, Field(ge=1, le=65535)] = None
    debug: bool | None = None
    ip: Annotated[str | None, Field(min_length=1, max_length=128)] = None
    information_packet: bool | None = None
    resource_folder: Annotated[str | None, Field(min_length=1, max_length=128)] = None

    @field_validator("name", "description", "tags", "ip", "resource_folder")
    @classmethod
    def reject_control_characters(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 and character not in "\t\n" for character in value):
            raise ValueError("Les caracteres de controle sont interdits")
        return value.strip() if value is not None else None

    @field_validator("ip")
    @classmethod
    def validate_bind_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(character.isspace() for character in value):
            raise ValueError("L'adresse d'ecoute ne peut pas contenir d'espaces")
        return value

    @field_validator("resource_folder")
    @classmethod
    def validate_resource_folder(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.replace("\\", "/").strip("/")
        if not cleaned or ".." in cleaned.split("/"):
            raise ValueError("Dossier de ressources invalide")
        return cleaned


class MapSelection(BaseModel):
    path: str = Field(min_length=1, max_length=256)


class ModItem(BaseModel):
    id: str
    file_name: str
    display_name: str
    internal_name: str | None = None
    type: Literal["map", "vehicle", "other", "unknown"]
    size: int
    modified_at: int
    active: bool
    remote_path: str
    map_path: str | None = None
    brand: str | None = None
    model: str | None = None
    author: str | None = None
    thumbnail_url: str | None = None


class OfficialMap(BaseModel):
    id: str
    name: str
    path: str
    official: bool = True
    active: bool = False


class MapsResponse(BaseModel):
    active_path: str
    official: list[OfficialMap]
    modded: list[ModItem]


class VehiclesResponse(BaseModel):
    items: list[ModItem]
    active_count: int
    total_count: int


class ModsResponse(BaseModel):
    items: list[ModItem]
    unidentified: list[ModItem]


class PendingItem(BaseModel):
    id: str
    kind: Literal["config", "map", "mod"]
    label: str
    created_at: datetime


class PendingResponse(BaseModel):
    count: int
    items: list[PendingItem]


class ApplyResponse(BaseModel):
    success: bool
    service_state: str
    message: str


class LiveVehicle(BaseModel):
    id: int
    model: str | None = None
    position: list[float] | None = None
    velocity: list[float] | None = None
    rotation: list[float] | None = None
    ping_ms: float | None = None
    speed_kmh: float | None = None


class LivePlayer(BaseModel):
    id: int
    name: str
    connected: bool
    connected_at: datetime
    session_seconds: int
    vehicle_count: int
    vehicles: list[LiveVehicle]


class LiveEvent(BaseModel):
    id: str
    type: Literal["player_join", "player_disconnect", "vehicle_spawn", "vehicle_edited"]
    timestamp: datetime
    player_id: int
    player_name: str
    vehicle_id: int | None = None
    vehicle_model: str | None = None


class MapCalibration(BaseModel):
    map_id: str
    image: str
    world_min_x: float
    world_max_x: float
    world_min_y: float
    world_max_y: float
    invert_x: bool = False
    invert_y: bool = True
    rotation: float = 0


class LiveSnapshot(BaseModel):
    available: bool
    stale: bool = False
    source: str = "beammp-lua"
    generated_at: datetime | None = None
    server_started_at: datetime | None = None
    uptime_seconds: int | None = None
    player_count: int = 0
    vehicle_count: int = 0
    players: list[LivePlayer] = Field(default_factory=list)
    events: list[LiveEvent] = Field(default_factory=list)
    map_calibration: MapCalibration | None = None
    message: str | None = None


class PlayerHistoryEntry(BaseModel):
    name: str
    first_seen: datetime
    last_seen: datetime
    connection_count: int
    last_disconnect: datetime | None = None
    vehicles_used: list[str] = Field(default_factory=list)


class PlayerHistoryResponse(BaseModel):
    players: list[PlayerHistoryEntry]
    timeline: list[LiveEvent]


class BackupCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=128)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = "".join(character for character in value if ord(character) >= 32).strip()
        return cleaned or None


class BackupMod(BaseModel):
    name: str
    size: int


class BackupSummary(BaseModel):
    id: str
    created_at: datetime
    name: str
    reason: str
    map_path: str
    active_mods: list[BackupMod]
    disabled_mods: list[BackupMod]
    beammp_version: str | None = None
    backed_up_files: list[str] = Field(default_factory=list)


class BackupsResponse(BaseModel):
    items: list[BackupSummary]


class ConfigDifference(BaseModel):
    field: str
    current: str | int | bool | None
    restored: str | int | bool | None


class BackupPreview(BaseModel):
    backup: BackupSummary
    config_changes: list[ConfigDifference]
    activate_mods: list[str]
    disable_mods: list[str]
    unavailable_mods: list[str]


class BackupRestoreResponse(BaseModel):
    success: bool
    message: str
    safety_backup_id: str
    restored_config: bool
    moved_mods: int


class BatchModsRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    enabled: bool

    @field_validator("ids")
    @classmethod
    def valid_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("Les identifiants de mods doivent etre uniques")
        if any(not re.fullmatch(r"[0-9a-f]{20}", value) for value in values):
            raise ValueError("Identifiant de mod invalide")
        return values


class UploadAnalysis(BaseModel):
    token: str
    file_name: str
    display_name: str
    type: Literal["map", "vehicle", "other", "unknown"]
    size: int
    internal_name: str | None = None
    map_path: str | None = None
    author: str | None = None
    preview_url: str | None = None
    valid_zip: bool = True
    duplicate: bool = False
    existing_size: int | None = None
    existing_modified_at: int | None = None


class UploadInstallRequest(BaseModel):
    token: str = Field(min_length=32, max_length=64)
    replace: bool = False


class UploadJob(BaseModel):
    id: str
    file_name: str
    state: Literal["queued", "uploading", "success", "error", "cancelled"]
    transferred: int = 0
    total: int
    speed_bps: float = 0
    message: str | None = None
    item: ModItem | None = None


class LogEntry(BaseModel):
    id: str
    timestamp: str | None = None
    level: Literal["INFO", "WARNING", "ERROR"]
    message: str


class LogsResponse(BaseModel):
    source: str
    entries: list[LogEntry]
    message: str | None = None


class BeamMPRelease(BaseModel):
    installed_version: str | None = None
    latest_version: str | None = None
    update_available: bool | None = None
    release_date: datetime | None = None
    description: str | None = None
    release_url: str
    asset_name: str | None = None
    asset_url: str | None = None
    asset_size: int | None = None
    asset_sha256: str | None = None
    platform: str | None = None
    available: bool = True
    update_supported: bool = False
    message: str | None = None


class BeamMPUpdateRequest(BaseModel):
    target_version: str = Field(pattern=r"^v?\d+\.\d+\.\d+$")


class OperationItem(BaseModel):
    id: str
    kind: Literal["mod_install", "mod_update", "beammp_update"]
    title: str
    state: Literal["queued", "downloading", "analyzing", "transferring", "installed", "success", "error", "cancelled", "rollback"]
    stage: str
    transferred: int = 0
    total: int | None = None
    speed_bps: float = 0
    message: str | None = None
    created_at: datetime


class ManagerSettingsPatch(BaseModel):
    lan_enabled: bool | None = None
    web_access_enabled: bool | None = None
    public_server_address: str | None = Field(default=None, max_length=253)
    open_browser_on_start: bool | None = None
    auto_restart_after_crash: bool | None = None
    demo_mode: bool | None = None
    default_language: Literal["en", "fr"] | None = None
    notification_preferences: dict[
        Literal[
            "player_join",
            "player_disconnect",
            "server_offline",
            "mod_updates",
            "errors",
            "windows",
        ],
        bool,
    ] | None = None

    @field_validator("public_server_address")
    @classmethod
    def validate_public_server_address(cls, value: str | None) -> str | None:
        if value is None:
            return None
        address = value.strip()
        if not address:
            return ""
        if any(character.isspace() or ord(character) < 32 for character in address):
            raise ValueError("L'adresse Internet ne peut pas contenir d'espaces")
        if "://" in address or any(character in address for character in "/?#@"):
            raise ValueError("Saisissez uniquement une IP ou un nom d'hote, sans protocole ni port")
        return address


class AdminPasswordRequest(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=1, max_length=256)
    trust_device: bool = False


class BootstrapAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: SecretStr
    confirmation: SecretStr
    pairing_code: SecretStr | None = None

    @model_validator(mode="after")
    def passwords_match(self) -> "BootstrapAdminRequest":
        password = self.password.get_secret_value()
        if not 12 <= len(password) <= 256:
            raise ValueError("Le mot de passe doit contenir entre 12 et 256 caracteres")
        if password != self.confirmation.get_secret_value():
            raise ValueError("Les mots de passe ne correspondent pas")
        return self


class AdminRecoveryRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: SecretStr
    confirmation: SecretStr
    security_code: SecretStr

    @model_validator(mode="after")
    def passwords_match(self) -> "AdminRecoveryRequest":
        if self.password.get_secret_value() != self.confirmation.get_secret_value():
            raise ValueError("Les mots de passe ne correspondent pas")
        return self


class AuthKeyUpdateRequest(BaseModel):
    authkey: SecretStr
    security_code: SecretStr


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: SecretStr
    confirmation: SecretStr
    role: Literal["admin", "viewer"] = "viewer"
    security_code: SecretStr

    @model_validator(mode="after")
    def passwords_match(self) -> "UserCreateRequest":
        if self.password.get_secret_value() != self.confirmation.get_secret_value():
            raise ValueError("Les mots de passe ne correspondent pas")
        return self


class UserPasswordResetRequest(BaseModel):
    password: SecretStr
    confirmation: SecretStr
    security_code: SecretStr

    @model_validator(mode="after")
    def passwords_match(self) -> "UserPasswordResetRequest":
        if self.password.get_secret_value() != self.confirmation.get_secret_value():
            raise ValueError("Les mots de passe ne correspondent pas")
        return self


class UserRoleRequest(BaseModel):
    role: Literal["admin", "viewer"]
    security_code: SecretStr


class UserEnabledRequest(BaseModel):
    enabled: bool
    security_code: SecretStr


class SecurityCodeRequest(BaseModel):
    security_code: SecretStr


class BeamMPBootstrapRequest(BaseModel):
    server_name: str = Field(min_length=1, max_length=250)
    authkey: SecretStr
    security_code: SecretStr
    max_players: int = Field(default=8, ge=1, le=1000)
    max_cars: int = Field(default=1, ge=1, le=100)
    private: bool = True
    map_path: str = Field(default="/levels/gridmap_v2/info.json", pattern=r"^/levels/[A-Za-z0-9_.-]+/info\.json$")

    @field_validator("server_name")
    @classmethod
    def clean_server_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ord(character) < 32 for character in cleaned):
            raise ValueError("Nom de serveur invalide")
        return cleaned

    @field_validator("authkey")
    @classmethod
    def authkey_is_not_empty(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().strip()) < 8:
            raise ValueError("AuthKey BeamMP invalide")
        return value


class ServerProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    ssh_key_path: str = Field(min_length=1, max_length=1024)
    beam_root: str = Field(default="/opt/beammp", min_length=1, max_length=512)
    service: str = Field(default="beammp", min_length=1, max_length=80)
    beam_port: int = Field(default=30814, ge=1, le=65535)

    @field_validator("name", "host", "ssh_key_path", "beam_root", "service")
    @classmethod
    def clean_profile_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or any(ord(character) < 32 for character in cleaned):
            raise ValueError("Valeur de profil invalide")
        return cleaned


class CalibrationSave(BaseModel):
    map_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    image: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.\-/]+$")
    world_min_x: float
    world_max_x: float
    world_min_y: float
    world_max_y: float
    invert_x: bool = False
    invert_y: bool = True
    rotation: float = Field(default=0, ge=-360, le=360)
