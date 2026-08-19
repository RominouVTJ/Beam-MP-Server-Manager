from __future__ import annotations

import asyncio
import httpx
import ctypes
import io
import json
import os
import platform
import re
import socket
import sqlite3
import sys
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Annotated, AsyncIterator, Literal
from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
import qrcode
import tomlkit

from beam_manager.config import Settings, get_settings
from beam_manager.backend import ServerBackend, build_server_backend
from beam_manager.backups import BackupService
from beam_manager.inventory import InventoryService
from beam_manager.logs import LogService
from beam_manager.network import PublicIPService
from beam_manager.beammp_updates import BeamMPUpdateService
from beam_manager.operations import OperationManager
from beam_manager.lan_security import LanSecurityMiddleware, is_loopback
from beam_manager.phase5 import APP_VERSION, Phase5Store, local_lan_address
from beam_manager.models import (
    ApplyResponse,
    BackupCreateRequest,
    BackupPreview,
    BackupRestoreResponse,
    BackupSummary,
    BeamMPRelease,
    BeamMPUpdateRequest,
    BackupsResponse,
    BatchModsRequest,
    LogsResponse,
    MapSelection,
    LiveSnapshot,
    MapsResponse,
    ModsResponse,
    PendingResponse,
    PlayerHistoryResponse,
    ServerConfigPatch,
    ServerConfigPublic,
    ServerStatus,
    ServiceActionResponse,
    UploadAnalysis,
    UploadInstallRequest,
    UploadJob,
    OperationItem,
    VehiclesResponse,
    CalibrationSave,
    BeamMPBootstrapRequest,
    BootstrapAdminRequest,
    AdminRecoveryRequest,
    AuthKeyUpdateRequest,
    UserCreateRequest,
    UserPasswordResetRequest,
    UserRoleRequest,
    UserEnabledRequest,
    SecurityCodeRequest,
    LoginRequest,
    ManagerSettingsPatch,
    ServerProfileCreate,
)
from beam_manager.official_maps import official_maps
from beam_manager.pending import PendingStore
from beam_manager.server_config import ServerConfigService
from beam_manager.ssh import SSHClient, SSHError
from beam_manager.telemetry import TelemetryService
from beam_manager.uploads import UploadService

STATIC_DIR = Path(__file__).resolve().parent / "frontend"
_RECORDED_UPLOAD_JOBS: set[str] = set()
_NOTIFIED_LIVE_EVENTS: dict[str, set[str]] = {}


@lru_cache
def get_phase5_store() -> Phase5Store:
    base = get_settings()
    store = Phase5Store(base.data_dir / "beamserver.db", base.data_dir, base.session_secret_file)
    store.ensure_primary_profile(base)
    return store


def get_active_settings() -> Settings:
    base = get_settings()
    store = get_phase5_store()
    profile = store.selected_profile()
    root = profile["beam_root"].rstrip("/")
    data_dir = store.profile_data_dir(profile["id"])
    return base.model_copy(
        update={
            "server_host": profile["host"],
            "server_port": profile["beam_port"],
            "ssh_user": profile["ssh_user"],
            "ssh_key_path": Path(profile["ssh_key_path"]),
            "systemd_service": profile["service"],
            "beam_root": root,
            "server_config_path": f"{root}/ServerConfig.toml",
            "active_mods_path": f"{root}/Resources/Client",
            "disabled_mods_path": f"{root}/DisabledMods",
            "backups_path": f"{root}/Backups",
            "log_path": f"{root}/Server.log",
            "old_log_path": f"{root}/Server.old.log",
            "telemetry_path": f"{root}/Resources/Server/BeamServerManager/data/telemetry.json",
            "data_dir": data_dir,
        }
    )


def get_server_backend(settings: Annotated[Settings, Depends(get_active_settings)]) -> ServerBackend:
    profile = get_phase5_store().selected_profile()
    return build_server_backend(settings, profile)


# Compatibility seam for extensions written before the LocalLinuxBackend migration.
get_ssh_client = get_server_backend


def get_config_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
) -> ServerConfigService:
    return ServerConfigService(client, settings.server_config_path)


def get_inventory_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
) -> InventoryService:
    return InventoryService(
        client,
        settings.active_mods_path,
        settings.disabled_mods_path,
        settings.data_dir,
    )


def get_pending_store(
    settings: Annotated[Settings, Depends(get_active_settings)],
) -> PendingStore:
    return PendingStore(settings.data_dir / "pending.json")


def get_telemetry_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
) -> TelemetryService:
    return TelemetryService(
        client,
        settings.telemetry_path,
        settings.data_dir / "players.json",
    )


def get_backup_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
) -> BackupService:
    return BackupService(
        client,
        settings.backups_path,
        settings.server_config_path,
        settings.active_mods_path,
        settings.disabled_mods_path,
        settings.log_path,
    )


def get_log_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
) -> LogService:
    return LogService(client, settings.log_path, settings.old_log_path)


def get_upload_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
    inventory: Annotated[InventoryService, Depends(get_inventory_service)],
    backups: Annotated[BackupService, Depends(get_backup_service)],
) -> UploadService:
    return UploadService(
        client,
        inventory,
        backups,
        settings.data_dir,
        settings.active_mods_path,
        settings.upload_max_bytes,
    )


def get_operation_manager() -> OperationManager:
    return OperationManager()


@lru_cache
def get_public_ip_service() -> PublicIPService:
    return PublicIPService()


def get_beammp_update_service(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
    backups: Annotated[BackupService, Depends(get_backup_service)],
    operations: Annotated[OperationManager, Depends(get_operation_manager)],
) -> BeamMPUpdateService:
    return BeamMPUpdateService(
        client,
        backups,
        operations,
        settings.data_dir,
        settings.log_path,
        settings.old_log_path,
        settings.backups_path,
        settings.server_host,
        settings.server_port,
        settings.internet_timeout,
    )


async def read_status(client: SSHClient, settings: Settings) -> ServerStatus:
    result = await client.execute_service_command("status")
    state = result.stdout.strip().lower() or "unknown"
    return ServerStatus(
        online=result.exit_code == 0 and state == "active",
        service_state=state,
        server_host=settings.server_host,
        server_port=settings.server_port,
        checked_at=datetime.now(timezone.utc),
    )


def _map_id_from_path(map_path: str) -> str:
    parts = [part for part in map_path.replace("\\", "/").split("/") if part]
    if len(parts) >= 2 and parts[0].casefold() == "levels":
        return parts[1]
    return "unknown"


def _demo_enabled() -> bool:
    return bool(get_settings().allow_demo and get_phase5_store().settings().get("demo_mode"))


def _demo_config() -> ServerConfigPublic:
    return ServerConfigPublic(
        Name="MODE DEMO · BeamMP", Description="Données simulées de développement",
        Private=True, MaxPlayers=8, MaxCars=3, Map="/levels/gridmap_v2/info.json",
        Tags="Demo", LogChat=False, AllowGuests=True, Port=30814,
    )


def _record_live_notifications(snapshot: LiveSnapshot) -> None:
    profile_id = get_phase5_store().selected_profile()["id"]
    known = _NOTIFIED_LIVE_EVENTS.get(profile_id)
    event_ids = {event.id for event in snapshot.events}
    if known is None:
        _NOTIFIED_LIVE_EVENTS[profile_id] = event_ids
        return
    preferences = get_phase5_store().settings().get("notification_preferences", {})
    for event in snapshot.events:
        if event.id in known or event.type not in {"player_join", "player_disconnect"}:
            continue
        if preferences.get(event.type, True):
            action = "vient de rejoindre le serveur" if event.type == "player_join" else "vient de quitter le serveur"
            get_phase5_store().notify(event.type, "Activite joueur", f"{event.player_name} {action}.")
    known.update(event_ids)
    if len(known) > 2000:
        _NOTIFIED_LIVE_EVENTS[profile_id] = set(list(known)[-1000:])


async def _startup_checks(application: FastAPI) -> None:
    settings = get_active_settings()
    profile = get_phase5_store().selected_profile()
    ssh = build_server_backend(settings, profile)
    backups = BackupService(
        ssh,
        settings.backups_path,
        settings.server_config_path,
        settings.active_mods_path,
        settings.disabled_mods_path,
        settings.log_path,
    )
    updater = BeamMPUpdateService(
        ssh,
        backups,
        OperationManager(),
        settings.data_dir,
        settings.log_path,
        settings.old_log_path,
        settings.backups_path,
        settings.server_host,
        settings.server_port,
        settings.internet_timeout,
    )
    beammp_release = await updater.check()
    application.state.beammp_release = beammp_release
    application.state.updates_checked_at = datetime.now(timezone.utc)


async def _health_monitor(application: FastAPI) -> None:
    previous_online: bool | None = None
    while True:
        await asyncio.sleep(30)
        if _demo_enabled():
            previous_online = None
            continue
        settings = get_active_settings()
        profile = get_phase5_store().selected_profile()
        client = build_server_backend(settings, profile)
        try:
            current = await read_status(client, settings)
            if previous_online is True and not current.online:
                expected_until = getattr(application.state, "expected_stop_until", datetime.min.replace(tzinfo=timezone.utc))
                if datetime.now(timezone.utc) > expected_until:
                    get_phase5_store().notify("server_offline", "Serveur arrete de maniere inattendue", f"BeamMP est hors ligne depuis {current.checked_at.isoformat()}")
                    get_phase5_store().app_log("ERROR", "BeamMP est passe hors ligne sans action utilisateur")
                    if get_phase5_store().settings().get("auto_restart_after_crash"):
                        restarted = await client.execute_service_command("restart")
                        get_phase5_store().notify(
                            "server_restart", "Redemarrage automatique BeamMP",
                            "Redemarrage demande apres crash" if restarted.exit_code == 0 else "Le redemarrage automatique a echoue",
                        )
            elif previous_online is False and current.online:
                get_phase5_store().notify("server_restart", "BeamMP operationnel", "Le serveur est revenu en ligne")
            previous_online = current.online
            application.state.last_monitor_status = current
        except SSHError as exc:
            if previous_online is True:
                get_phase5_store().app_log("ERROR", str(exc))
            previous_online = False


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    if not STATIC_DIR.is_dir():
        raise RuntimeError("Le dossier frontend est introuvable")
    application.state.phase5 = get_phase5_store()
    task = asyncio.create_task(_startup_checks(application))
    monitor = asyncio.create_task(_health_monitor(application))
    yield
    for background in (task, monitor):
        if not background.done():
            background.cancel()


app = FastAPI(
    title="Beam-MP-Server-Manager API",
    version=APP_VERSION,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=lifespan,
)
app.state.phase5 = get_phase5_store()
app.add_middleware(LanSecurityMiddleware)


def _request_identity(request: Request) -> dict | None:
    identity = getattr(request.state, "auth_user", None)
    if identity is not None:
        return identity
    return get_phase5_store().session_identity(request.cookies.get("beam_manager_session"))


def _require_admin(request: Request) -> dict:
    identity = _request_identity(request)
    if identity is None or identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Action reservee aux administrateurs")
    return identity


def _require_security_code(code: str) -> None:
    if not get_phase5_store().verify_security_code(code):
        raise HTTPException(status_code=403, detail="Code de securite de l'appliance incorrect")


def _enabled_admin_count() -> int:
    return sum(1 for user in get_phase5_store().users() if user["role"] == "admin" and user["enabled"])


def _authenticated_response(
    token: str,
    csrf: str,
    expires: datetime,
    identity: dict,
    **extra: object,
) -> JSONResponse:
    response = JSONResponse(
        {
            "authenticated": True,
            "csrf_token": csrf,
            "expires_at": expires.isoformat(),
            "user": identity,
            **extra,
        }
    )
    response.set_cookie(
        "beam_manager_session", token, httponly=True, secure=False, samesite="strict",
        expires=expires, path="/",
    )
    response.set_cookie(
        "beam_manager_csrf", csrf, httponly=False, secure=False, samesite="strict",
        expires=expires, path="/",
    )
    return response


@app.get("/api/health")
async def health(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[ServerBackend, Depends(get_ssh_client)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
) -> dict[str, object]:
    database_ok = filesystem_ok = False
    beammp_state = "unknown"
    try:
        with get_phase5_store().connect() as db:
            database_ok = db.execute("SELECT 1").fetchone()[0] == 1
    except (OSError, sqlite3.Error):
        pass
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        filesystem_ok = os.access(settings.data_dir, os.R_OK | os.W_OK | os.X_OK)
    except OSError:
        pass
    authkey_configured = False
    try:
        authkey_configured = await config_service.authkey_configured()
    except SSHError:
        pass
    if authkey_configured:
        try:
            result = await client.execute_service_command("status")
            beammp_state = result.stdout.strip().lower() or "unknown"
        except SSHError:
            beammp_state = "unknown"
    manager_ok = database_ok and filesystem_ok
    return {
        "manager": "ok" if manager_ok else "degraded",
        "database": "ok" if database_ok else "error",
        "beammp": "not_configured" if not authkey_configured else "online" if beammp_state == "active" else "offline",
        "authkey_configured": authkey_configured,
        "filesystem": "ok" if filesystem_ok else "error",
    }


@app.get("/api/appliance/desktop-status")
async def appliance_desktop_status(
    request: Request,
    service: Annotated[ServerConfigService, Depends(get_config_service)],
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[ServerBackend, Depends(get_ssh_client)],
) -> dict[str, object]:
    if not is_loopback(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Etat bureau disponible uniquement en local")
    store = get_phase5_store()
    values = store.settings()
    try:
        authkey = await service.authkey_configured()
    except SSHError:
        authkey = False
    beammp = "not_configured"
    if authkey:
        try:
            current = await read_status(client, settings)
            beammp = "online" if current.online else "offline"
        except SSHError:
            beammp = "offline"
    address = local_lan_address()
    return {
        "manager": "online",
        "beammp": beammp,
        "authkey_configured": authkey,
        "users_total": len(store.users()),
        "active_sessions": store.active_session_count(),
        "security_code": store.security_code() if values.get("firstboot_complete") else None,
        "web_url": f"http://{address}:{get_settings().manager_port}" if address else None,
        "language": values.get("default_language", "en"),
    }


@app.get("/api/server/status", response_model=ServerStatus)
async def server_status(
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
) -> ServerStatus:
    if _demo_enabled():
        return ServerStatus(online=True, service_state="demo", server_host="MODE DEMO", server_port=30814, checked_at=datetime.now(timezone.utc))
    try:
        return await read_status(client, settings)
    except SSHError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc


@app.get("/api/server/config", response_model=ServerConfigPublic)
async def server_config(
    service: Annotated[ServerConfigService, Depends(get_config_service)],
) -> ServerConfigPublic:
    if _demo_enabled():
        return _demo_config()
    try:
        return await service.read_public()
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.patch("/api/server/config", response_model=ServerConfigPublic)
async def update_server_config(
    patch: ServerConfigPatch,
    service: Annotated[ServerConfigService, Depends(get_config_service)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
    backups: Annotated[BackupService, Depends(get_backup_service)],
) -> ServerConfigPublic:
    try:
        before = await service.read_public()
        changed = [
            key
            for key, value in patch.model_dump(exclude_none=True).items()
            if getattr(before, key) != value
        ]
        if changed:
            await backups.create(
                name="Avant modification de la configuration",
                reason="automatic-before-config",
            )
        updated = await service.update_public(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if changed:
        pending.add("config", f"Configuration : {', '.join(changed)}")
    return updated


@app.get("/api/maps", response_model=MapsResponse)
async def maps_catalog(
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
    inventory_service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> MapsResponse:
    if _demo_enabled():
        config = _demo_config()
        return MapsResponse(active_path=config.map_path, official=official_maps(config.map_path), modded=[])
    try:
        config, inventory = await asyncio.gather(
            config_service.read_public(), inventory_service.scan()
        )
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return MapsResponse(
        active_path=config.map_path,
        official=official_maps(config.map_path),
        modded=[item for item in inventory if item.type == "map"],
    )


@app.post("/api/maps/select", response_model=ServerConfigPublic)
async def select_map(
    selection: MapSelection,
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
    inventory_service: Annotated[InventoryService, Depends(get_inventory_service)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
) -> ServerConfigPublic:
    try:
        before, inventory = await asyncio.gather(
            config_service.read_public(), inventory_service.scan()
        )
        allowed_paths = {item.path for item in official_maps(before.map_path)}
        allowed_paths.update(
            item.map_path
            for item in inventory
            if item.type == "map" and item.map_path
        )
        if selection.path not in allowed_paths:
            raise ValueError("Cette carte n'est pas installee sur le serveur")
        selected_mod = next(
            (item for item in inventory if item.type == "map" and item.map_path == selection.path),
            None,
        )
        if selected_mod is not None and not selected_mod.active:
            # Selecting a modded map publishes its ZIP to Resources/Client. BeamMP only
            # reloads client resources on server restart, which is already represented by
            # the pending-change workflow below.
            await inventory_service.set_enabled(selected_mod.id, True)
        updated = await config_service.select_map(selection.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if before.map_path != updated.map_path:
        try:
            pending.add("map", f"Map : {before.map_path} → {updated.map_path}")
        except OSError as exc:
            # The map is already committed. A secondary notification failure must
            # not turn that success into an ambiguous HTTP 500 for the operator.
            try:
                get_phase5_store().app_log(
                    "ERROR",
                    f"Map modifiee mais pending change non enregistre: {type(exc).__name__}",
                )
            except OSError:
                pass
    return updated


@app.get("/api/vehicles", response_model=VehiclesResponse)
async def vehicles_catalog(
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> VehiclesResponse:
    if _demo_enabled():
        return VehiclesResponse(items=[], active_count=0, total_count=0)
    try:
        items = [item for item in await service.scan() if item.type == "vehicle"]
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return VehiclesResponse(
        items=items,
        active_count=sum(item.active for item in items),
        total_count=len(items),
    )


@app.get("/api/mods", response_model=ModsResponse)
async def mods_catalog(
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> ModsResponse:
    if _demo_enabled():
        return ModsResponse(items=[], unidentified=[])
    try:
        inventory = await service.scan()
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ModsResponse(
        items=[item for item in inventory if item.type == "other"],
        unidentified=[item for item in inventory if item.type == "unknown"],
    )


@app.post("/api/mods/{mod_id}/{operation}")
async def toggle_mod(
    mod_id: str,
    operation: Literal["enable", "disable"],
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
) -> dict[str, object]:
    enabled = operation == "enable"
    try:
        item = await service.set_enabled(mod_id, enabled)
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    verb = "Activé" if enabled else "Désactivé"
    pending.add("mod", f"{verb} : {item.display_name}")
    return {
        "success": True,
        "message": f"{item.display_name} est maintenant {'actif' if enabled else 'désactivé'}",
        "item": item,
    }


@app.post("/api/mods/batch")
async def batch_mods(
    request: BatchModsRequest,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    backups: Annotated[BackupService, Depends(get_backup_service)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
) -> dict[str, object]:
    try:
        await backups.create(
            name=f"Avant operation groupee sur {len(request.ids)} mods",
            reason="automatic-before-batch",
        )
        items = await service.set_enabled_batch(request.ids, request.enabled)
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    verb = "Activation" if request.enabled else "Desactivation"
    pending.add("mod", f"{verb} groupee : {len(items)} mod(s)")
    return {"success": True, "count": len(items), "items": items}


@app.delete("/api/mods/{mod_id}")
async def delete_mod(
    mod_id: str,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    backups: Annotated[BackupService, Depends(get_backup_service)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
) -> dict[str, object]:
    try:
        item = await service.find(mod_id)
        backup = await backups.create(
            name=f"Avant suppression {item.file_name}",
            reason="automatic-before-delete",
            include_files=[item.remote_path],
        )
        deleted = await service.delete(mod_id)
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pending.add("mod", f"Suppression definitive : {deleted.display_name}")
    return {"success": True, "backup_id": backup.id, "item": deleted}


@app.get("/api/thumbnails/{mod_id}", include_in_schema=False)
async def thumbnail(
    mod_id: str,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> FileResponse:
    path = service.thumbnail_path(mod_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Miniature introuvable")
    return FileResponse(path, media_type="image/webp")


@app.post("/api/uploads/analyze", response_model=UploadAnalysis)
async def analyze_upload(
    file: Annotated[UploadFile, File()],
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadAnalysis:
    try:
        return await service.analyze(file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/uploads/{token}/preview", include_in_schema=False)
async def upload_preview(
    token: str,
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> FileResponse:
    path = service.preview_path(token)
    if path is None:
        raise HTTPException(status_code=404, detail="Preview introuvable")
    return FileResponse(path, media_type="image/webp")


@app.post("/api/uploads/install", response_model=UploadJob)
async def install_upload(
    request: UploadInstallRequest,
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadJob:
    try:
        return await service.start(request.token, request.replace)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, SSHError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/uploads/jobs/{job_id}", response_model=UploadJob)
async def upload_job(
    job_id: str,
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadJob:
    job = service.job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Upload introuvable")
    if job.state == "success" and job.item and job.id not in _RECORDED_UPLOAD_JOBS:
        get_phase5_store().notify("upload", "Upload termine", f"{job.item.file_name} est installe sur le serveur")
        _RECORDED_UPLOAD_JOBS.add(job.id)
    return job


@app.post("/api/uploads/jobs/{job_id}/cancel", response_model=UploadJob)
async def cancel_upload(
    job_id: str,
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> UploadJob:
    try:
        return service.cancel(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/uploads/{token}", status_code=204)
async def discard_upload(
    token: str,
    service: Annotated[UploadService, Depends(get_upload_service)],
) -> Response:
    service.discard(token)
    return Response(status_code=204)


@app.get("/api/pending", response_model=PendingResponse)
async def pending_changes(
    pending: Annotated[PendingStore, Depends(get_pending_store)],
) -> PendingResponse:
    return pending.list()


@app.get("/api/live", response_model=LiveSnapshot)
async def live_snapshot(
    service: Annotated[TelemetryService, Depends(get_telemetry_service)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
) -> LiveSnapshot:
    if _demo_enabled():
        now = datetime.now(timezone.utc)
        return LiveSnapshot.model_validate({
            "available": True, "source": "MODE DEMO", "generated_at": now,
            "server_started_at": now - timedelta(minutes=42), "uptime_seconds": 2520,
            "player_count": 1, "vehicle_count": 1,
            "players": [{"id": 9001, "name": "Pilote Demo", "connected": True, "connected_at": now - timedelta(minutes=18), "session_seconds": 1080, "vehicle_count": 1, "vehicles": [{"id": 1, "model": "ETK 800 · DEMO", "position": [120.0, -45.0, 0.0], "velocity": [24.0, 2.0, 0.0], "rotation": [0.0, 0.0, 0.2, 0.98], "ping_ms": 28, "speed_kmh": 86.7}]}],
            "events": [{"id": "demo:1", "type": "vehicle_spawn", "timestamp": now, "player_id": 9001, "player_name": "Pilote Demo", "vehicle_id": 1, "vehicle_model": "ETK 800 · DEMO"}],
            "message": "MODE DEMO · aucune donnée serveur réelle",
        })
    snapshot = await service.read()
    _record_live_notifications(snapshot)
    if snapshot.available:
        try:
            config = await config_service.read_public()
            map_id = _map_id_from_path(config.map_path)
            calibration = get_phase5_store().calibration(map_id)
            if calibration:
                snapshot.map_calibration = calibration
        except SSHError:
            pass
    return snapshot


@app.get("/api/players/history", response_model=PlayerHistoryResponse)
async def player_history(
    service: Annotated[TelemetryService, Depends(get_telemetry_service)],
) -> PlayerHistoryResponse:
    await service.read()
    return service.history.read()


@app.get("/api/live/stream", include_in_schema=False)
async def live_stream(
    request: Request,
    service: Annotated[TelemetryService, Depends(get_telemetry_service)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
) -> StreamingResponse:
    async def events():
        while not await request.is_disconnected():
            snapshot = await live_snapshot(service, config_service)
            if snapshot.available:
                try:
                    config = await config_service.read_public()
                    calibration = get_phase5_store().calibration(_map_id_from_path(config.map_path))
                    if calibration:
                        snapshot.map_calibration = calibration
                except SSHError:
                    pass
            payload = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
            yield f"event: telemetry\ndata: {payload}\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/logs", response_model=LogsResponse)
async def logs(
    service: Annotated[LogService, Depends(get_log_service)],
    limit: Annotated[int, Query(ge=1, le=2000)] = 300,
) -> LogsResponse:
    try:
        return await service.read(limit)
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/backups", response_model=BackupsResponse)
async def list_backups(
    service: Annotated[BackupService, Depends(get_backup_service)],
) -> BackupsResponse:
    try:
        return BackupsResponse(items=await service.list())
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/backups", response_model=BackupSummary)
async def create_backup(
    request: BackupCreateRequest,
    service: Annotated[BackupService, Depends(get_backup_service)],
):
    try:
        return await service.create(request.name, "manual")
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/backups/{backup_id}", response_model=BackupPreview)
async def preview_backup(
    backup_id: str,
    service: Annotated[BackupService, Depends(get_backup_service)],
) -> BackupPreview:
    try:
        return await service.preview(backup_id)
    except SSHError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/backups/{backup_id}/restore", response_model=BackupRestoreResponse)
async def restore_backup(
    backup_id: str,
    service: Annotated[BackupService, Depends(get_backup_service)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
    inventory_service: Annotated[InventoryService, Depends(get_inventory_service)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
) -> BackupRestoreResponse:
    try:
        result = await service.restore(backup_id, config_service, inventory_service)
    except SSHError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    pending.add("config", f"Restauration : {backup_id}")
    return result


@app.delete("/api/backups/{backup_id}", status_code=204)
async def delete_backup(
    backup_id: str,
    service: Annotated[BackupService, Depends(get_backup_service)],
) -> Response:
    try:
        await service.delete(backup_id)
    except SSHError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=204)


@app.get("/api/beammp/version", response_model=BeamMPRelease)
@app.get("/api/beammp/update-check", response_model=BeamMPRelease)
async def beammp_version(
    service: Annotated[BeamMPUpdateService, Depends(get_beammp_update_service)],
    refresh: bool = False,
) -> BeamMPRelease:
    return await service.check(refresh)


@app.post("/api/beammp/update", response_model=OperationItem)
async def update_beammp(
    request: BeamMPUpdateRequest,
    service: Annotated[BeamMPUpdateService, Depends(get_beammp_update_service)],
) -> OperationItem:
    try:
        return await service.start_update(request.target_version)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/operations", response_model=list[OperationItem])
async def operations(
    manager: Annotated[OperationManager, Depends(get_operation_manager)],
) -> list[OperationItem]:
    return manager.list()


@app.post("/api/operations/{operation_id}/cancel", response_model=OperationItem)
async def cancel_operation(
    operation_id: str,
    manager: Annotated[OperationManager, Depends(get_operation_manager)],
) -> OperationItem:
    try:
        return manager.cancel(operation_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/apply", response_model=ApplyResponse)
async def apply_and_restart(
    client: Annotated[SSHClient, Depends(get_ssh_client)],
    pending: Annotated[PendingStore, Depends(get_pending_store)],
    operations: Annotated[OperationManager, Depends(get_operation_manager)],
) -> ApplyResponse:
    if UploadService.has_active_job() or any(item.state in {"queued", "downloading", "analyzing", "transferring", "rollback"} for item in operations.list()):
        raise HTTPException(status_code=409, detail="Une operation importante est en cours")
    try:
        reachable = await client.execute_service_command("status")
        if reachable.exit_code not in {0, 3}:
            raise SSHError("Le serveur BeamMP ne repond pas correctement")
        restarted = await client.execute_service_command("restart")
        if restarted.exit_code != 0:
            raise SSHError("Le redemarrage de beammp.service a echoue")
        service_state = "unknown"
        for _ in range(15):
            await asyncio.sleep(1)
            checked = await client.execute_service_command("status")
            service_state = checked.stdout.strip().lower() or "unknown"
            if checked.exit_code == 0 and service_state == "active":
                pending.clear()
                return ApplyResponse(
                    success=True,
                    service_state=service_state,
                    message="Changements appliques et serveur BeamMP operationnel",
                )
        raise SSHError("BeamMP n'est pas revenu en ligne apres le redemarrage")
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/api/server/actions/{action}",
    response_model=ServiceActionResponse,
)
async def service_action(
    action: Literal["start", "stop", "restart"],
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[SSHClient, Depends(get_ssh_client)],
    operations: Annotated[OperationManager, Depends(get_operation_manager)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
) -> ServiceActionResponse:
    if _demo_enabled():
        return ServiceActionResponse(action=action, success=True, service_state="demo", message="MODE DEMO · aucune commande distante executee")
    if action in {"start", "restart"}:
        try:
            configured = await config_service.authkey_configured()
        except SSHError:
            configured = False
        if not configured:
            raise HTTPException(status_code=428, detail="Renseignez l'AuthKey BeamMP avant de demarrer le serveur")
    if UploadService.has_active_job() or any(item.state in {"queued", "downloading", "analyzing", "transferring", "rollback"} for item in operations.list()):
        raise HTTPException(status_code=409, detail="Une operation importante est en cours")
    if action in {"stop", "restart"}:
        app.state.expected_stop_until = datetime.now(timezone.utc) + timedelta(minutes=2)
    try:
        result = await client.execute_service_command(action)
        if result.exit_code != 0:
            raise SSHError("systemctl a refuse l'operation")
        await asyncio.sleep(1.5)
        current = await read_status(client, settings)
    except SSHError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    expected_online = action in {"start", "restart"}
    success = current.online is expected_online
    response = ServiceActionResponse(
        action=action,
        success=success,
        service_state=current.service_state,
        message=(
            "Service BeamMP operationnel"
            if success and expected_online
            else "Service BeamMP arrete"
            if success
            else "L'action a ete executee mais l'etat attendu n'est pas confirme"
        ),
    )
    if action == "restart" and response.success:
        get_phase5_store().notify("server_restart", "BeamMP redemarre", "Le service BeamMP est de nouveau operationnel")
    return response


# Phase 5: local product shell, LAN security and daily-management services.


@app.get("/api/auth/state")
async def auth_state(request: Request) -> dict:
    store = get_phase5_store()
    local = is_loopback(request.client.host if request.client else None)
    token = request.cookies.get("beam_manager_session")
    identity = store.session_identity(token)
    bypass = local and not get_settings().require_auth
    return {
        "local": local,
        "lan_enabled": store.settings()["lan_enabled"],
        "admin_configured": store.admin_configured(),
        "authenticated": bypass or identity is not None,
        "login_required": not bypass,
        "user": identity,
    }

@app.post("/api/auth/login")
async def login(payload: LoginRequest, request: Request) -> Response:
    store = get_phase5_store()
    remote = request.client.host if request.client else "unknown"
    result = store.authenticate_user(payload.username, payload.password, payload.trust_device)
    if result is None:
        store.app_log("WARNING", f"Connexion refusee utilisateur={payload.username} adresse={remote}")
        raise HTTPException(status_code=401, detail="Mot de passe incorrect")
    token, csrf, expires, identity = result
    store.app_log("INFO", f"Connexion reussie utilisateur={identity['username']} adresse={remote}")
    return _authenticated_response(token, csrf, expires, identity)


@app.post("/api/auth/recover")
async def recover_admin(payload: AdminRecoveryRequest, request: Request) -> dict[str, bool]:
    store = get_phase5_store()
    _require_security_code(payload.security_code.get_secret_value())
    try:
        target = store.user(payload.username)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Administrateur introuvable") from exc
    if target["role"] != "admin":
        raise HTTPException(status_code=403, detail="La recuperation par code de securite est reservee aux administrateurs")
    try:
        store.set_user_password(payload.username, payload.password.get_secret_value())
        store.set_user_enabled(payload.username, True)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    remote = request.client.host if request.client else "unknown"
    store.app_log("WARNING", f"Recuperation administrateur par code de securite utilisateur={payload.username} adresse={remote}")
    return {"success": True}


@app.get("/api/setup/status")
async def setup_status(
    service: Annotated[ServerConfigService, Depends(get_config_service)],
) -> dict[str, object]:
    store = get_phase5_store()
    try:
        authkey_configured = await service.authkey_configured()
    except SSHError:
        authkey_configured = False
    values = store.settings()
    return {
        "setup_required": not store.admin_configured(),
        "admin_configured": store.admin_configured(),
        "authkey_configured": authkey_configured,
        "web_setup_complete": bool(values.get("web_setup_complete")),
        "default_language": values.get("default_language", "en"),
        "pairing_required": store.pairing_required(),
    }


@app.post("/api/setup/admin")
async def setup_admin(payload: BootstrapAdminRequest, request: Request) -> Response:
    store = get_phase5_store()
    if store.admin_configured():
        raise HTTPException(status_code=409, detail="Le compte administrateur est deja configure")
    if not store.settings().get("firstboot_complete"):
        raise HTTPException(status_code=425, detail="Terminez d'abord le First Run sur la console")
    pairing_code = payload.pairing_code.get_secret_value() if payload.pairing_code else None
    if not store.verify_setup_pairing_code(pairing_code):
        raise HTTPException(status_code=403, detail="Code d'appairage incorrect")
    password = payload.password.get_secret_value()
    try:
        store.bootstrap_admin(payload.username, password)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = store.authenticate_user(payload.username, password, False)
    if result is None:
        raise HTTPException(status_code=500, detail="Creation de session impossible")
    # The security code is permanent and is not consumed after bootstrap.
    token, csrf, expires, identity = result
    remote = request.client.host if request.client else "unknown"
    store.app_log("INFO", f"Configuration administrateur terminee utilisateur={identity['username']} adresse={remote}")
    return _authenticated_response(token, csrf, expires, identity, admin_configured=True)


@app.post("/api/setup/beammp")
async def setup_beammp(
    payload: BeamMPBootstrapRequest,
    service: Annotated[ServerConfigService, Depends(get_config_service)],
    client: Annotated[ServerBackend, Depends(get_ssh_client)],
) -> dict[str, object]:
    store = get_phase5_store()
    if not store.admin_configured():
        raise HTTPException(status_code=428, detail="Configurez d'abord le compte administrateur")
    _require_security_code(payload.security_code.get_secret_value())
    try:
        configured = await service.configure_initial(
            server_name=payload.server_name,
            authkey=payload.authkey.get_secret_value(),
            max_players=payload.max_players,
            max_cars=payload.max_cars,
            private=payload.private,
            map_path=payload.map_path,
        )
        started = await client.execute_service_command("start")
        if started.exit_code != 0:
            raise SSHError("Le service BeamMP n'a pas demarre")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store._set_internal_setting("web_setup_complete", True)
    store.app_log("INFO", "Configuration initiale BeamMP terminee; AuthKey presente")
    return {
        "configured": True,
        "authkey_configured": True,
        "service_started": True,
        "config": configured.model_dump(mode="json", by_alias=True),
    }


@app.post("/api/beammp/authkey")
async def set_beammp_authkey(payload: AuthKeyUpdateRequest, request: Request, service: Annotated[ServerConfigService, Depends(get_config_service)]) -> dict[str, bool]:
    identity = _require_admin(request)
    _require_security_code(payload.security_code.get_secret_value())
    try:
        await service.set_authkey(payload.authkey.get_secret_value())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SSHError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    get_phase5_store().app_log("INFO", f"AuthKey BeamMP ajoutee ou remplacee par {identity['username']}")
    return {"authkey_configured": True}


@app.get("/api/users")
async def list_web_users(request: Request) -> dict[str, object]:
    _require_admin(request)
    store = get_phase5_store()
    return {"items": store.users(), "active_sessions": store.active_session_count()}


@app.post("/api/users")
async def create_web_user(payload: UserCreateRequest, request: Request) -> dict[str, object]:
    identity = _require_admin(request)
    _require_security_code(payload.security_code.get_secret_value())
    try:
        user = get_phase5_store().add_user(payload.username, payload.password.get_secret_value(), payload.role)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_phase5_store().app_log("INFO", f"Utilisateur Web {user['username']} cree par {identity['username']}")
    return user


@app.post("/api/users/{username}/password")
async def reset_web_user_password(username: str, payload: UserPasswordResetRequest, request: Request) -> dict[str, bool]:
    identity = _require_admin(request)
    _require_security_code(payload.security_code.get_secret_value())
    try:
        get_phase5_store().set_user_password(username, payload.password.get_secret_value())
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    get_phase5_store().app_log("INFO", f"Mot de passe Web reinitialise pour {username} par {identity['username']}")
    return {"success": True}


@app.post("/api/users/{username}/role")
async def change_web_user_role(username: str, payload: UserRoleRequest, request: Request) -> dict[str, bool]:
    identity = _require_admin(request)
    _require_security_code(payload.security_code.get_secret_value())
    store = get_phase5_store()
    current = store.user(username)
    if current["role"] == "admin" and current["enabled"] and payload.role != "admin" and _enabled_admin_count() <= 1:
        raise HTTPException(status_code=409, detail="Impossible de retirer le dernier administrateur actif")
    try:
        store.set_user_role(username, payload.role)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.app_log("INFO", f"Role Web modifie pour {username} par {identity['username']}")
    return {"success": True}


@app.post("/api/users/{username}/enabled")
async def set_web_user_enabled(username: str, payload: UserEnabledRequest, request: Request) -> dict[str, bool]:
    identity = _require_admin(request)
    _require_security_code(payload.security_code.get_secret_value())
    store = get_phase5_store()
    current = store.user(username)
    if current["role"] == "admin" and current["enabled"] and not payload.enabled and _enabled_admin_count() <= 1:
        raise HTTPException(status_code=409, detail="Impossible de desactiver le dernier administrateur actif")
    try:
        store.set_user_enabled(username, payload.enabled)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.app_log("INFO", f"Etat utilisateur Web modifie pour {username} par {identity['username']}")
    return {"success": True}


@app.delete("/api/users/{username}")
async def delete_web_user(username: str, payload: SecurityCodeRequest, request: Request) -> dict[str, bool]:
    identity = _require_admin(request)
    _require_security_code(payload.security_code.get_secret_value())
    store = get_phase5_store()
    current = store.user(username)
    if current["role"] == "admin" and current["enabled"] and _enabled_admin_count() <= 1:
        raise HTTPException(status_code=409, detail="Impossible de supprimer le dernier administrateur actif")
    if username.casefold() == identity["username"].casefold():
        raise HTTPException(status_code=409, detail="Utilisez un autre administrateur pour supprimer ce compte")
    try:
        store.delete_user(username)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.app_log("INFO", f"Utilisateur Web {username} supprime par {identity['username']}")
    return {"success": True}


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict:
    local = is_loopback(request.client.host if request.client else None)
    store = get_phase5_store()
    identity = store.session_identity(request.cookies.get("beam_manager_session"))
    bypass = local and not get_settings().require_auth
    return {"authenticated": bypass or identity is not None, "local": local, "user": identity}


@app.post("/api/auth/logout")
async def logout(request: Request) -> Response:
    get_phase5_store().logout(request.cookies.get("beam_manager_session"))
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("beam_manager_session", path="/")
    response.delete_cookie("beam_manager_csrf", path="/")
    return response


@app.get("/api/manager/settings")
async def manager_settings() -> dict:
    values = get_phase5_store().settings()
    values["demo_allowed"] = get_settings().allow_demo
    values["lan_url"] = f"http://{values['lan_address']}:{get_settings().manager_port}" if values.get("lan_address") else None
    profile = get_phase5_store().selected_profile()
    public_address = values.get("public_server_address")
    values["public_server_endpoint"] = (
        f"{public_address}:{profile['beam_port']}"
        if values.get("web_access_enabled") and public_address else None
    )
    return values


@app.get("/api/network/status")
async def network_status(
    public_ip_service: Annotated[PublicIPService, Depends(get_public_ip_service)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
    refresh: bool = False,
) -> dict[str, object]:
    values = get_phase5_store().settings()
    profile = get_phase5_store().selected_profile()
    try:
        config = await config_service.read_public()
        visibility = "unlisted" if config.private else "listed"
    except SSHError:
        visibility = "unknown"
    public_ip = await public_ip_service.detect(refresh=refresh)
    configured_address = str(values.get("public_server_address") or "").strip() or None
    reachability = "unknown" if public_ip or configured_address else "lan_only"
    return {
        "lan_ip": local_lan_address(),
        "beammp_port": int(profile["beam_port"]),
        "public_ip": public_ip,
        "configured_public_address": configured_address,
        "visibility": visibility,
        "reachability": reachability,
        "internet_tested": False,
    }


@app.post("/api/network/connectivity-check")
async def connectivity_check(
    client: Annotated[ServerBackend, Depends(get_server_backend)],
    settings: Annotated[Settings, Depends(get_active_settings)],
    config_service: Annotated[ServerConfigService, Depends(get_config_service)],
    public_ip_service: Annotated[PublicIPService, Depends(get_public_ip_service)],
) -> dict[str, object]:
    config = await config_service.read_public()
    status = await read_status(client, settings)
    writer = None
    tcp_listening = False
    tcp_error: str | None = None
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", config.port), timeout=2.0
        )
        tcp_listening = True
    except (OSError, asyncio.TimeoutError) as exc:
        tcp_error = type(exc).__name__
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    public_ip = await public_ip_service.detect(refresh=True)
    external_status = "not_run"
    external_details: str | None = None
    if status.online and public_ip:
        try:
            async with httpx.AsyncClient(
                timeout=8.0,
                follow_redirects=False,
                headers={"User-Agent": "Beam-MP-Server-Manager/0.10.0"},
            ) as http:
                response = await http.get(
                    f"https://check.beammp.com/api/v2/beammp/{public_ip}/{config.port}"
                )
                response.raise_for_status()
                payload = response.json()
                external_status = str(payload.get("status") or "unknown").casefold()
                details = payload.get("details")
                external_details = str(details)[:1000] if details else None
        except (httpx.HTTPError, ValueError, TypeError):
            external_status = "unavailable"

    return {
        "service_online": status.online,
        "service_state": status.service_state,
        "tcp_listening": tcp_listening,
        "tcp_error": tcp_error,
        "local_ok": status.online and tcp_listening,
        "public_ip": public_ip,
        "port": config.port,
        "external_status": external_status,
        "external_details": external_details,
        "external_ok": external_status == "ok",
        "checker": "CheckBeamMP",
    }

@app.patch("/api/manager/settings")
async def update_manager_settings(payload: ManagerSettingsPatch) -> dict:
    if payload.demo_mode and not get_settings().allow_demo:
        raise HTTPException(status_code=403, detail="Le mode demo doit etre autorise explicitement dans l'environnement de developpement")
    try:
        values = get_phase5_store().update_settings(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    values["restart_required"] = "lan_enabled" in payload.model_fields_set
    values["lan_url"] = f"http://{values['lan_address']}:{get_settings().manager_port}" if values.get("lan_address") else None
    profile = get_phase5_store().selected_profile()
    public_address = values.get("public_server_address")
    values["public_server_endpoint"] = (
        f"{public_address}:{profile['beam_port']}"
        if values.get("web_access_enabled") and public_address else None
    )
    return values


def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" -m beam_manager.windows_app --startup'


@app.post("/api/manager/autostart/{enabled}")
async def set_autostart(enabled: bool, request: Request) -> dict:
    if not is_loopback(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Le demarrage Windows se configure sur le PC maitre")
    if platform.system() != "Windows":
        raise HTTPException(status_code=400, detail="Fonction disponible sous Windows uniquement")
    import winreg
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, "Beam-MP-Server-Manager", 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, "Beam-MP-Server-Manager")
            except FileNotFoundError:
                pass
    get_phase5_store()._set_internal_setting("autostart_enabled", enabled)
    return {"enabled": enabled}


@app.get("/api/manager/qr", include_in_schema=False)
async def lan_qr() -> Response:
    values = get_phase5_store().settings()
    address = values.get("lan_address")
    if not values.get("lan_enabled") or not address:
        raise HTTPException(status_code=404, detail="Acces LAN inactif")
    image = qrcode.make(f"http://{address}:{get_settings().manager_port}")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return Response(buffer.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/servers")
async def server_profiles() -> dict:
    return {"items": get_phase5_store().profiles(), "selected": get_phase5_store().selected_profile()["id"]}


async def _test_profile(payload: ServerProfileCreate) -> dict:
    client = SSHClient(
        payload.host, payload.ssh_user, Path(payload.ssh_key_path).expanduser().resolve(),
        get_settings().ssh_timeout, port=payload.ssh_port, service=payload.service,
    )
    try:
        result = await client.execute_service_command("status")
    except (SSHError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    detected: dict[str, object] = {
        "service": payload.service.removesuffix(".service"),
        "config_path": f"{payload.beam_root.rstrip('/')}/ServerConfig.toml",
        "resources_path": f"{payload.beam_root.rstrip('/')}/Resources",
        "port": payload.beam_port,
        "beammp_version": None,
    }
    try:
        raw = await client.read_file(str(detected["config_path"]), max_bytes=1_000_000)
        document = tomlkit.parse(raw.decode("utf-8-sig"))
        general = document.get("General", document)
        detected["port"] = int(general.get("Port", payload.beam_port))
    except (SSHError, UnicodeError, ValueError, TypeError):
        pass
    try:
        detected["resource_directories"] = await client.list_remote_directory(str(detected["resources_path"]))
    except SSHError:
        detected["resource_directories"] = []
    try:
        detected["beammp_version"] = await client.beammp_version(payload.beam_root)
    except SSHError:
        pass
    return {"reachable": True, "service_state": result.stdout.strip().lower() or "unknown", "detected": detected}


@app.post("/api/servers/test")
async def test_server_profile(payload: ServerProfileCreate) -> dict:
    return await _test_profile(payload)


@app.post("/api/servers")
async def add_server_profile(payload: ServerProfileCreate) -> dict:
    test = await _test_profile(payload)
    try:
        profile = get_phase5_store().add_profile(payload.model_dump())
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise HTTPException(status_code=400, detail="Ce profil existe deja ou contient une valeur invalide") from exc
    return {"profile": profile, "test": test}


@app.post("/api/servers/{profile_id}/select")
async def select_server_profile(profile_id: str) -> dict:
    try:
        return get_phase5_store().select_profile(profile_id)
    except (ValueError, LookupError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _diagnostic_payload(client: ServerBackend, settings: Settings, telemetry: TelemetryService) -> dict:
    checked_at = datetime.now(timezone.utc).isoformat()
    backend_ok = False
    backend_name = get_phase5_store().selected_profile().get("backend", "ssh")
    beam_state = "unknown"
    try:
        result = await client.execute_service_command("status")
        backend_ok = True
        beam_state = result.stdout.strip().lower() or "unknown"
    except SSHError:
        pass
    port_ok = False
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(settings.server_host, settings.server_port), 2.5)
        writer.close()
        await writer.wait_closed()
        port_ok = True
    except (OSError, asyncio.TimeoutError):
        pass
    snapshot = await telemetry.read() if backend_ok else LiveSnapshot(available=False, stale=True)
    free = None
    if backend_ok:
        try:
            free = await client.disk_free_bytes(settings.beam_root)
        except SSHError:
            pass
    return {
        "checked_at": checked_at,
        "host": {"ok": backend_ok, "label": "Hote local joignable" if backend_name == "local" and backend_ok else ("Serveur distant joignable" if backend_ok else "Injoignable")},
        "backend": {"ok": backend_ok, "kind": backend_name, "label": ("LocalLinuxBackend" if backend_name == "local" else "SSHBackend") if backend_ok else "Erreur"},
        "beammp": {"ok": beam_state == "active", "state": beam_state},
        "port": {"ok": port_ok, "port": settings.server_port},
        "telemetry": {"ok": snapshot.available and not snapshot.stale, "stale": snapshot.stale},
        "disk": {"ok": free is not None, "free_bytes": free},
    }


@app.get("/api/diagnostic")
async def diagnostic(
    client: Annotated[SSHClient, Depends(get_ssh_client)],
    settings: Annotated[Settings, Depends(get_active_settings)],
    telemetry: Annotated[TelemetryService, Depends(get_telemetry_service)],
) -> dict:
    return await _diagnostic_payload(client, settings, telemetry)


@app.get("/api/diagnostic/report")
async def diagnostic_report(
    client: Annotated[SSHClient, Depends(get_ssh_client)],
    settings: Annotated[Settings, Depends(get_active_settings)],
    telemetry: Annotated[TelemetryService, Depends(get_telemetry_service)],
) -> Response:
    health = await _diagnostic_payload(client, settings, telemetry)
    payload = get_phase5_store().support_report(health)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(raw, media_type="application/json", headers={"Content-Disposition": "attachment; filename=beamserver-diagnostic.json"})


@app.get("/api/manager/logs")
async def manager_logs(limit: Annotated[int, Query(ge=1, le=1000)] = 300) -> dict:
    return {"source": "Beam-MP-Server-Manager", "entries": get_phase5_store().app_logs(limit)}


@app.get("/api/notifications")
async def notifications() -> dict:
    items = get_phase5_store().notifications()
    return {"items": items, "unread": sum(1 for item in items if item["read_at"] is None)}


@app.post("/api/notifications/read")
async def read_notifications() -> dict[str, bool]:
    get_phase5_store().mark_notifications_read()
    return {"success": True}


@app.post("/api/minimap/image")
async def upload_minimap_image(map_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9_.-]{1,128}$")], file: Annotated[UploadFile, File()]) -> dict:
    raw = await file.read(25_000_001)
    if len(raw) > 25_000_000:
        raise HTTPException(status_code=413, detail="Image trop volumineuse")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            source.verify()
        with Image.open(io.BytesIO(raw)) as source:
            image = source.convert("RGB")
            if image.width < 320 or image.height < 180 or image.width > 12000 or image.height > 12000:
                raise ValueError("Dimensions de minimap non prises en charge")
            image.thumbnail((4096, 4096), Image.Resampling.LANCZOS)
            root = get_phase5_store().profile_data_dir(get_phase5_store().selected_profile()["id"]) / "maps" / map_id
            root.mkdir(parents=True, exist_ok=True)
            target = root / "map.webp"
            temporary = root / "map.tmp.webp"
            image.save(temporary, "WEBP", quality=90, method=4)
            temporary.replace(target)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Image de minimap invalide") from exc
    return {"map_id": map_id, "image": f"{map_id}/map.webp", "url": f"/api/minimap/images/{map_id}"}


@app.get("/api/minimap/images/{map_id}", include_in_schema=False)
async def minimap_image(map_id: str) -> FileResponse:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", map_id):
        raise HTTPException(status_code=404)
    target = get_phase5_store().profile_data_dir(get_phase5_store().selected_profile()["id"]) / "maps" / map_id / "map.webp"
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Image introuvable")
    return FileResponse(target, media_type="image/webp", headers={"Cache-Control": "private, max-age=300"})


@app.get("/api/minimap/calibrations")
async def list_calibrations() -> dict:
    return {"items": get_phase5_store().calibrations()}


@app.post("/api/minimap/calibrations")
async def save_calibration(payload: CalibrationSave) -> dict:
    expected = get_phase5_store().profile_data_dir(get_phase5_store().selected_profile()["id"]) / "maps" / payload.map_id / "map.webp"
    if not expected.is_file() or payload.image != f"{payload.map_id}/map.webp":
        raise HTTPException(status_code=400, detail="Importez d'abord l'image de cette map")
    try:
        return get_phase5_store().save_calibration(payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/manager/export", include_in_schema=False)
async def export_manager_configuration() -> Response:
    raw = get_phase5_store().export_bytes()
    return Response(raw, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=beamserver-backup.zip"})


@app.post("/api/manager/import/preview")
async def preview_manager_import(file: Annotated[UploadFile, File()]) -> dict:
    raw = await file.read(100_000_001)
    try:
        return get_phase5_store().stage_import(raw)
    except (ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/manager/import/{token}/apply")
async def apply_manager_import(token: str) -> dict:
    try:
        return get_phase5_store().apply_staged_import(token)
    except (ValueError, zipfile.BadZipFile, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/search")
async def global_search(
    q: Annotated[str, Query(min_length=2, max_length=100)],
    inventory: Annotated[InventoryService, Depends(get_inventory_service)],
    telemetry: Annotated[TelemetryService, Depends(get_telemetry_service)],
) -> dict:
    needle = q.casefold().strip()
    mods, live = await asyncio.gather(inventory.scan(), telemetry.read())
    profiles = [item for item in get_phase5_store().profiles() if needle in item["name"].casefold() or needle in item["host"].casefold()]
    installed = [item.model_dump(mode="json") for item in mods if needle in f"{item.display_name} {item.file_name}".casefold()][:8]
    players = [item.model_dump(mode="json") for item in live.players if needle in item.name.casefold()][:8]
    maps = [item for item in installed if item["type"] == "map"]
    return {"query": q, "groups": {"mods": installed, "maps": maps, "players": players, "servers": profiles[:8]}}


@app.get("/api/beamng/local")
async def local_beamng() -> dict:
    candidates: list[Path] = []
    program_files = os.environ.get("PROGRAMFILES(X86)") or os.environ.get("PROGRAMFILES")
    if program_files:
        candidates.append(Path(program_files) / "Steam" / "steamapps" / "common" / "BeamNG.drive")
    candidates.append(Path("C:/Program Files (x86)/Steam/steamapps/common/BeamNG.drive"))
    root = next((item for item in candidates if (item / "Bin64" / "BeamNG.drive.x64.exe").is_file()), None)
    version = _windows_file_version(root / "Bin64" / "BeamNG.drive.x64.exe") if root else None
    return {"detected": root is not None, "path": str(root) if root else None, "version": version, "method": "Steam + metadonnees de l'executable" if root else None, "read_only": True}


def _windows_file_version(executable: Path) -> str | None:
    if platform.system() != "Windows" or not executable.is_file():
        return None
    class FixedInfo(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32), ("struct_version", ctypes.c_uint32),
            ("file_version_ms", ctypes.c_uint32), ("file_version_ls", ctypes.c_uint32),
            ("product_version_ms", ctypes.c_uint32), ("product_version_ls", ctypes.c_uint32),
            ("file_flags_mask", ctypes.c_uint32), ("file_flags", ctypes.c_uint32),
            ("file_os", ctypes.c_uint32), ("file_type", ctypes.c_uint32),
            ("file_subtype", ctypes.c_uint32), ("file_date_ms", ctypes.c_uint32),
            ("file_date_ls", ctypes.c_uint32),
        ]
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(str(executable), None)
        if not size:
            return None
        buffer = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(str(executable), 0, size, buffer):
            return None
        pointer = ctypes.c_void_p()
        length = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
            return None
        info = ctypes.cast(pointer, ctypes.POINTER(FixedInfo)).contents
        parts = (
            info.product_version_ms >> 16, info.product_version_ms & 0xFFFF,
            info.product_version_ls >> 16, info.product_version_ls & 0xFFFF,
        )
        return ".".join(map(str, parts)).rstrip(".0")
    except (AttributeError, OSError, ValueError):
        return None


@app.get("/api/about")
async def about() -> dict:
    return {"name": "Beam-MP-Server-Manager", "version": APP_VERSION, "update_source_configured": False}


app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/setup", include_in_schema=False)
async def setup_page() -> Response:
    if get_phase5_store().settings().get("web_setup_complete"):
        return RedirectResponse("/", status_code=307)
    return FileResponse(STATIC_DIR / "setup.html")


@app.get("/", include_in_schema=False)
async def index() -> Response:
    if not get_phase5_store().admin_configured():
        return RedirectResponse("/setup", status_code=307)
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=204)
