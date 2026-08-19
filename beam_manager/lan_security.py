from __future__ import annotations

import ipaddress
import socket
from collections import defaultdict, deque
from time import monotonic

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from beam_manager.config import get_settings


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PATHS = {
    "/",
    "/setup",
    "/favicon.ico",
    "/api/health",
    "/api/auth/login",
    "/api/auth/recover",
    "/api/auth/state",
    "/api/setup/status",
    "/api/setup/admin",
    "/api/appliance/desktop-status",
    # Read-only product/version metadata is part of the internal updater health
    # contract. Keep it reachable before Web-admin setup, just like /api/health.
    "/api/appliance/version",
}


def is_loopback(address: str | None) -> bool:
    if address == "testclient":  # Starlette's in-process transport; never a network peer.
        return True
    try:
        return bool(address and ipaddress.ip_address(address).is_loopback)
    except ValueError:
        return False


def is_private_lan(address: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(address or "")
        return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local
    except ValueError:
        return False


def is_public_path(path: str) -> bool:
    return path.startswith("/assets/") or path.startswith("/map-thumbnails/") or path in PUBLIC_PATHS


class LanSecurityMiddleware(BaseHTTPMiddleware):
    """Keep localhost frictionless while enforcing session + CSRF on private LAN."""

    _attempts: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        remote = request.client.host if request.client else None
        settings = get_settings()
        local = is_loopback(remote)
        request.state.lan_request = not local
        store = getattr(request.app.state, "phase5", None)
        path = request.url.path
        if local and not settings.require_auth:
            if store is not None and not store.admin_configured() and path == "/":
                return RedirectResponse("/setup", status_code=307)
            return await call_next(request)
        if not local and not is_private_lan(remote):
            return JSONResponse({"detail": "Acces reserve au reseau local prive"}, status_code=403)
        if not local and settings.lan_network and ipaddress.ip_address(remote or "") not in ipaddress.ip_network(settings.lan_network):
            return JSONResponse({"detail": "Acces hors du reseau LAN autorise"}, status_code=403)
        if not local:
            host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").casefold()
            computer = socket.gethostname().casefold()
            if host not in {computer, f"{computer}.local", "localhost"}:
                try:
                    if not ipaddress.ip_address(host).is_private:
                        raise ValueError
                except ValueError:
                    return JSONResponse({"detail": "Nom d'hote LAN refuse"}, status_code=400)

        if store is None or (not local and not store.settings().get("lan_enabled")):
            return JSONResponse({"detail": "Acces LAN desactive"}, status_code=403)

        if not store.admin_configured():
            if path == "/":
                return RedirectResponse("/setup", status_code=307)
            if path == "/api/setup/admin" and request.method == "POST":
                now = monotonic()
                attempts = self._attempts[f"setup:{remote or 'unknown'}"]
                while attempts and attempts[0] < now - 300:
                    attempts.popleft()
                if len(attempts) >= 10:
                    return JSONResponse({"detail": "Trop de tentatives; reessayez plus tard"}, status_code=429)
                attempts.append(now)
            if is_public_path(path):
                return await call_next(request)
            return JSONResponse(
                {"detail": "Configuration initiale requise", "setup_required": True},
                status_code=428,
            )

        if is_public_path(path):
            if path in {"/api/auth/login", "/api/auth/recover"}:
                now = monotonic()
                bucket = "recover" if path == "/api/auth/recover" else "login"
                attempts = self._attempts[f"{bucket}:{remote or 'unknown'}"]
                while attempts and attempts[0] < now - 300:
                    attempts.popleft()
                if len(attempts) >= 10:
                    return JSONResponse({"detail": "Trop de tentatives; reessayez plus tard"}, status_code=429)
                attempts.append(now)
            return await call_next(request)

        token = request.cookies.get("beam_manager_session")
        identity = store.session_identity(token)
        if identity is None:
            return JSONResponse({"detail": "Authentification LAN requise"}, status_code=401)
        request.state.auth_user = identity
        if request.method not in SAFE_METHODS:
            if identity["role"] != "admin" and path != "/api/auth/logout":
                return JSONResponse({"detail": "Action reservee aux administrateurs"}, status_code=403)
            csrf = request.headers.get("X-CSRF-Token")
            if not csrf or not store.validate_session(token, csrf):
                return JSONResponse({"detail": "Jeton CSRF invalide"}, status_code=403)
        return await call_next(request)
