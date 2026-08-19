from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from beam_manager.models import PendingItem, PendingResponse


class PendingStore:
    """Small durable local list of server changes awaiting a restart."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = Lock()

    def _load(self) -> list[PendingItem]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [PendingItem.model_validate(item) for item in raw]
        except (OSError, ValueError):
            return []

    def _save(self, items: list[PendingItem]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in items],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def list(self) -> PendingResponse:
        with self._lock:
            items = self._load()
        return PendingResponse(count=len(items), items=items)

    def add(self, kind: str, label: str) -> PendingResponse:
        item = PendingItem(
            id=uuid4().hex,
            kind=kind,
            label=label,
            created_at=datetime.now(timezone.utc),
        )
        with self._lock:
            items = self._load()
            items.append(item)
            self._save(items)
        return PendingResponse(count=len(items), items=items)

    def clear(self) -> None:
        with self._lock:
            self._save([])
