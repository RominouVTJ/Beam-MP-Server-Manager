from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Awaitable, Callable
from time import monotonic

import httpx


PublicIPFetcher = Callable[[], Awaitable[str | None]]


class PublicIPService:
    """Short, cached public IPv4 detection; never claims port reachability."""

    def __init__(
        self,
        *,
        timeout: float = 2.0,
        success_ttl: float = 300.0,
        failure_ttl: float = 60.0,
        fetcher: PublicIPFetcher | None = None,
    ) -> None:
        self.timeout = timeout
        self.success_ttl = success_ttl
        self.failure_ttl = failure_ttl
        self.fetcher = fetcher or self._fetch
        self._cached: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _fetch(self) -> str | None:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=False,
            headers={"User-Agent": "Beam-MP-Server-Manager/0.10.0"},
        ) as client:
            response = await client.get("https://api.ipify.org", params={"format": "json"})
            response.raise_for_status()
            value = response.json().get("ip")
            return str(value) if value else None

    @staticmethod
    def _validated(value: str | None) -> str | None:
        try:
            address = ipaddress.ip_address((value or "").strip())
        except ValueError:
            return None
        return str(address) if address.version == 4 and address.is_global else None

    async def detect(self, *, refresh: bool = False) -> str | None:
        now = monotonic()
        if not refresh and now < self._expires_at:
            return self._cached
        async with self._lock:
            now = monotonic()
            if not refresh and now < self._expires_at:
                return self._cached
            try:
                value = self._validated(await self.fetcher())
            except (OSError, ValueError, httpx.HTTPError, asyncio.TimeoutError):
                value = None
            self._cached = value
            self._expires_at = now + (self.success_ttl if value else self.failure_ttl)
            return value
