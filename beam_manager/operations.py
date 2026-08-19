from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone

from beam_manager.models import OperationItem


class OperationManager:
    _items: dict[str, OperationItem] = {}
    _cancelled: set[str] = set()
    _worker_lock = asyncio.Lock()

    def create(self, kind: str, title: str, stage: str = "En attente") -> OperationItem:
        identifier = secrets.token_urlsafe(18)
        item = OperationItem(
            id=identifier,
            kind=kind,
            title=title,
            state="queued",
            stage=stage,
            created_at=datetime.now(timezone.utc),
        )
        self._items[identifier] = item
        return item

    def get(self, identifier: str) -> OperationItem | None:
        return self._items.get(identifier)

    def list(self) -> list[OperationItem]:
        return sorted(self._items.values(), key=lambda item: item.created_at, reverse=True)[:100]

    def cancel(self, identifier: str) -> OperationItem:
        item = self._items.get(identifier)
        if item is None:
            raise LookupError("Operation introuvable")
        if item.state in {"queued", "downloading", "analyzing", "transferring"}:
            self._cancelled.add(identifier)
        return item

    def cancelled(self, identifier: str) -> bool:
        return identifier in self._cancelled

    def finish(self, identifier: str) -> None:
        self._cancelled.discard(identifier)
