from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from beam_manager import __version__
from beam_manager.backend import ServerBackend
from beam_manager.config import Settings
from beam_manager.main import (
    _demo_enabled,
    _require_admin,
    app,
    get_active_settings,
    get_phase5_store,
    get_server_backend,
)
from beam_manager.ssh import SSHError
from beam_manager.telemetry import TelemetryService


_MAP_THUMBNAIL_DIR = Path(
    os.environ.get(
        "BEAM_MANAGER_MAP_THUMBNAIL_DIR",
        "/usr/local/share/beam-appliance/map-thumbnails",
    )
)
_MAP_THUMBNAIL_NAME = re.compile(r"^[A-Za-z0-9_-]+\.(?:webp|jpe?g|png)$", re.IGNORECASE)


class LiveControlRequest(BaseModel):
    action: Literal["say", "kick", "remove_vehicle"]
    player_id: int | None = Field(default=None, ge=0)
    vehicle_id: int | None = Field(default=None, ge=0)
    message: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=200)

    @staticmethod
    def _clean(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = "".join(character for character in value if ord(character) >= 32).strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_action_fields(self) -> "LiveControlRequest":
        self.message = self._clean(self.message)
        self.reason = self._clean(self.reason)
        if self.action == "say" and self.message is None:
            raise ValueError("Le message ne peut pas etre vide")
        if self.action in {"kick", "remove_vehicle"} and self.player_id is None:
            raise ValueError("Le joueur cible est requis")
        if self.action == "remove_vehicle" and self.vehicle_id is None:
            raise ValueError("Le vehicule cible est requis")
        return self


async def _current_live(settings: Settings, client: ServerBackend):
    service = TelemetryService(
        client,  # type: ignore[arg-type]
        settings.telemetry_path,
        settings.data_dir / "players.json",
    )
    return await service.read()


@app.get("/api/appliance/version")
async def appliance_version() -> dict[str, str]:
    return {
        "product": "Beam-MP-Server-Manager",
        "version": __version__,
    }


@app.get("/map-thumbnails/{filename}", include_in_schema=False)
async def appliance_map_thumbnail(filename: str) -> FileResponse:
    """Serve factory-bundled BeamNG map previews outside the replaceable app tree."""
    if not _MAP_THUMBNAIL_NAME.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Map thumbnail not found")
    path = _MAP_THUMBNAIL_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Map thumbnail not found")
    return FileResponse(path)


@app.post("/api/live/control")
async def live_control(
    payload: LiveControlRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_active_settings)],
    client: Annotated[ServerBackend, Depends(get_server_backend)],
) -> dict[str, object]:
    identity = _require_admin(request)
    if _demo_enabled():
        return {"accepted": True, "demo": True, "action": payload.action}

    try:
        status = await client.execute_service_command("status")
    except SSHError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if status.exit_code != 0 or status.stdout.strip().casefold() != "active":
        raise HTTPException(status_code=409, detail="Le serveur BeamMP doit etre en ligne")

    snapshot = await _current_live(settings, client)
    if not snapshot.available:
        raise HTTPException(status_code=503, detail="La telemetrie Live est indisponible")

    player = None
    if payload.player_id is not None:
        player = next((item for item in snapshot.players if item.id == payload.player_id), None)
        if player is None:
            raise HTTPException(status_code=404, detail="Le joueur n'est plus connecte")
    if payload.action == "remove_vehicle":
        assert player is not None and payload.vehicle_id is not None
        if not any(vehicle.id == payload.vehicle_id for vehicle in player.vehicles):
            raise HTTPException(status_code=404, detail="Le vehicule n'est plus present")

    data_dir = f"{settings.beam_root.rstrip('/')}/Resources/Server/BeamServerManager/data"
    command_path = f"{data_dir}/control.json"
    try:
        # The bridge consumes this file once per second. Refuse to overwrite a
        # command that has not been consumed yet so clicks cannot silently race.
        try:
            await client.read_file(command_path, max_bytes=64_000)
        except SSHError:
            pass
        else:
            raise HTTPException(status_code=409, detail="Une commande Live est deja en attente")

        await client.ensure_directory(data_dir, mode=0o2770)
        command_id = secrets.token_hex(12)
        command = {
            "schema_version": 1,
            "id": command_id,
            "action": payload.action,
            "player_id": payload.player_id,
            "vehicle_id": payload.vehicle_id,
            "message": payload.message,
            "reason": payload.reason,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
        await client.write_file_atomic(
            command_path,
            json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
    except HTTPException:
        raise
    except (OSError, SSHError) as exc:
        raise HTTPException(status_code=502, detail="Impossible d'envoyer la commande a BeamMP") from exc

    target = player.name if player is not None else "tous les joueurs"
    get_phase5_store().app_log(
        "INFO",
        f"Commande Live {payload.action} demandee par {identity['username']} cible={target}",
    )
    return {
        "accepted": True,
        "id": command_id,
        "action": payload.action,
        "target": target,
    }


# Register Linux appliance self-update routes only after the core app and auth
# helpers above are fully initialized.  The module itself reports unsupported
# on non-appliance hosts, preserving a single ASGI entrypoint for future editions.
from beam_manager import appliance_update_api as _appliance_update_api  # noqa: E402,F401
