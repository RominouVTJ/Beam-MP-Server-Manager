from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from pydantic import ValidationError

from beam_manager.models import (
    LiveEvent,
    LivePlayer,
    LiveSnapshot,
    LiveVehicle,
    PlayerHistoryEntry,
    PlayerHistoryResponse,
)
from beam_manager.ssh import SSHClient, SSHError


ALLOWED_EVENTS = {
    "player_join",
    "player_disconnect",
    "vehicle_spawn",
    "vehicle_edited",
}


def _datetime(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Horodatage de telemetrie invalide")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _vector(value: Any, length: int) -> list[float] | None:
    if not isinstance(value, list) or len(value) != length:
        return None
    result: list[float] = []
    for item in value:
        if not isinstance(item, (int, float)) or not math.isfinite(item):
            return None
        result.append(float(item))
    return result


def _clean_text(value: Any, maximum: int = 128) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character for character in value if ord(character) >= 32).strip()
    return cleaned[:maximum] or None


class PlayerHistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "processed": [], "players": {}, "timeline": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") == 1:
                return payload
        except (OSError, ValueError):
            pass
        return self._empty()

    def _save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def record(self, events: list[LiveEvent]) -> None:
        with self._lock:
            payload = self._load()
            processed = set(payload.get("processed", []))
            players = payload.setdefault("players", {})
            timeline = payload.setdefault("timeline", [])
            changed = False
            for event in events:
                if event.id in processed:
                    continue
                processed.add(event.id)
                timestamp = event.timestamp.isoformat()
                player = players.setdefault(
                    event.player_name,
                    {
                        "name": event.player_name,
                        "first_seen": timestamp,
                        "last_seen": timestamp,
                        "connection_count": 0,
                        "last_disconnect": None,
                        "vehicles_used": [],
                    },
                )
                player["last_seen"] = timestamp
                if event.type == "player_join":
                    player["connection_count"] = int(player.get("connection_count", 0)) + 1
                elif event.type == "player_disconnect":
                    player["last_disconnect"] = timestamp
                elif event.vehicle_model:
                    vehicles = set(player.get("vehicles_used", []))
                    vehicles.add(event.vehicle_model)
                    player["vehicles_used"] = sorted(vehicles, key=str.casefold)
                timeline.append(event.model_dump(mode="json"))
                changed = True
            if changed:
                payload["processed"] = list(processed)[-1000:]
                payload["timeline"] = timeline[-300:]
                self._save(payload)

    def read(self) -> PlayerHistoryResponse:
        with self._lock:
            payload = self._load()
        players: list[PlayerHistoryEntry] = []
        timeline: list[LiveEvent] = []
        for item in payload.get("players", {}).values():
            try:
                players.append(PlayerHistoryEntry.model_validate(item))
            except ValidationError:
                continue
        for item in payload.get("timeline", []):
            try:
                timeline.append(LiveEvent.model_validate(item))
            except ValidationError:
                continue
        players.sort(key=lambda item: item.last_seen, reverse=True)
        timeline.sort(key=lambda item: item.timestamp, reverse=True)
        return PlayerHistoryResponse(players=players, timeline=timeline[:200])


class TelemetryService:
    def __init__(self, ssh: SSHClient, remote_path: str, history_path: Path) -> None:
        self.ssh = ssh
        self.remote_path = remote_path
        self.history = PlayerHistoryStore(history_path)
        self._read_lock = asyncio.Lock()

    def _parse(self, payload: bytes) -> LiveSnapshot:
        raw = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1:
            raise ValueError("Schema de telemetrie non reconnu")
        generated_at = _datetime(raw.get("generated_at"))
        server_started_at = _datetime(raw.get("server_started_at"))
        players: list[LivePlayer] = []
        for raw_player in raw.get("players", []):
            if not isinstance(raw_player, dict):
                continue
            name = _clean_text(raw_player.get("name"), 64)
            if name is None:
                continue
            connected_at = _datetime(raw_player.get("connected_at"))
            vehicles: list[LiveVehicle] = []
            for raw_vehicle in raw_player.get("vehicles", []):
                if not isinstance(raw_vehicle, dict) or not isinstance(raw_vehicle.get("id"), int):
                    continue
                velocity = _vector(raw_vehicle.get("velocity"), 3)
                speed = None
                if velocity is not None:
                    speed = math.sqrt(sum(component * component for component in velocity)) * 3.6
                ping = raw_vehicle.get("ping_ms")
                if not isinstance(ping, (int, float)) or not math.isfinite(ping) or ping < 0:
                    ping = None
                vehicles.append(
                    LiveVehicle(
                        id=raw_vehicle["id"],
                        model=_clean_text(raw_vehicle.get("model"), 128),
                        position=_vector(raw_vehicle.get("position"), 3),
                        velocity=velocity,
                        rotation=_vector(raw_vehicle.get("rotation"), 4),
                        ping_ms=round(float(ping), 1) if ping is not None else None,
                        speed_kmh=round(speed, 1) if speed is not None else None,
                    )
                )
            now = datetime.now(timezone.utc)
            players.append(
                LivePlayer(
                    id=int(raw_player.get("id", -1)),
                    name=name,
                    connected=bool(raw_player.get("connected", True)),
                    connected_at=connected_at,
                    session_seconds=max(0, int((now - connected_at).total_seconds())),
                    vehicle_count=len(vehicles),
                    vehicles=vehicles,
                )
            )
        events: list[LiveEvent] = []
        session_key = server_started_at.isoformat()
        for raw_event in raw.get("events", []):
            if not isinstance(raw_event, dict) or raw_event.get("type") not in ALLOWED_EVENTS:
                continue
            player_name = _clean_text(raw_event.get("player_name"), 64)
            if player_name is None:
                continue
            event_number = raw_event.get("id")
            try:
                events.append(
                    LiveEvent(
                        id=f"{session_key}:{int(event_number)}",
                        type=raw_event["type"],
                        timestamp=_datetime(raw_event.get("timestamp")),
                        player_id=int(raw_event.get("player_id")),
                        player_name=player_name,
                        vehicle_id=(
                            int(raw_event["vehicle_id"])
                            if isinstance(raw_event.get("vehicle_id"), int)
                            else None
                        ),
                        vehicle_model=_clean_text(raw_event.get("vehicle_model"), 128),
                    )
                )
            except (TypeError, ValueError, ValidationError):
                continue
        self.history.record(events)
        age = (datetime.now(timezone.utc) - generated_at).total_seconds()
        return LiveSnapshot(
            available=True,
            stale=age > 5,
            generated_at=generated_at,
            server_started_at=server_started_at,
            uptime_seconds=max(0, int(raw.get("uptime_seconds", 0))),
            player_count=len(players),
            vehicle_count=sum(player.vehicle_count for player in players),
            players=players,
            events=events[-200:],
            map_calibration=None,
        )

    async def read(self) -> LiveSnapshot:
        async with self._read_lock:
            try:
                payload = await self.ssh.read_file(self.remote_path, max_bytes=2_000_000)
                return self._parse(payload)
            except (SSHError, OSError, ValueError, json.JSONDecodeError) as exc:
                return LiveSnapshot(
                    available=False,
                    stale=True,
                    message="Telemetrie BeamMP temporairement indisponible",
                )
