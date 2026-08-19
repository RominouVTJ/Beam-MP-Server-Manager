from __future__ import annotations

import argparse
import ctypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

import pystray
import uvicorn
from PIL import Image, ImageDraw

from beam_manager.config import get_settings
from beam_manager.phase5 import Phase5Store


APP_TITLE = "Beam-MP-Server-Manager"
MUTEX_NAME = "Local\\Beam-MP-Server-Manager.Tray.0.7"


def _configure_logging() -> logging.Logger:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Beam-MP-Server-Manager" / "logs"
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("beam_manager.launcher")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(RotatingFileHandler(root / "launcher.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    return logger


LOGGER = _configure_logging()


def _api(path: str, method: str = "GET", timeout: float = 4.0) -> dict[str, Any] | None:
    settings = get_settings()
    request = urllib.request.Request(
        f"http://127.0.0.1:{settings.manager_port}{path}", method=method,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except (OSError, ValueError, urllib.error.URLError):
        return None


def _backend_ready() -> bool:
    return _api("/api/health", timeout=1.0) == {"status": "ok"}


def _status() -> str:
    payload = _api("/api/server/status")
    if payload is None:
        return "ERREUR"
    return "ONLINE" if payload.get("online") else "OFFLINE"


def _icon_image(state: str) -> Image.Image:
    color = {"ONLINE": "#54e58b", "OFFLINE": "#7d8993", "ERREUR": "#ff5b62"}.get(state, "#d39b41")
    image = Image.new("RGBA", (64, 64), "#0b1015")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill="#121a22", outline="#2d3a46", width=2)
    draw.polygon(((17, 16), (33, 16), (27, 30), (43, 30), (24, 51), (29, 35), (14, 35)), fill="#d39b41")
    draw.ellipse((45, 45, 58, 58), fill=color, outline="#0b1015", width=2)
    return image


class WindowsApplication:
    def __init__(self, startup: bool = False) -> None:
        self.settings = get_settings()
        self.store = Phase5Store(self.settings.data_dir / "beamserver.db", self.settings.data_dir)
        self.store.ensure_primary_profile(self.settings)
        self.startup = startup
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.state = "ERREUR"
        self.last_notification_id = 0
        self.icon = pystray.Icon(
            "beam-server-manager", _icon_image(self.state), APP_TITLE,
            menu=pystray.Menu(
                pystray.MenuItem(APP_TITLE, self.open, default=True),
                pystray.MenuItem(lambda _: f"Etat BeamMP : {self.state}", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Demarrer BeamMP", lambda *_: self.service_action("start")),
                pystray.MenuItem("Arreter BeamMP", lambda *_: self.service_action("stop")),
                pystray.MenuItem("Redemarrer BeamMP", lambda *_: self.service_action("restart")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quitter Beam-MP-Server-Manager", self.quit),
            ),
        )

    def start_backend(self) -> bool:
        if _backend_ready():
            return False
        from beam_manager.main import app as fastapi_app
        host = "0.0.0.0" if self.store.settings().get("lan_enabled") else "127.0.0.1"
        config = uvicorn.Config(
            fastapi_app, host=host, port=self.settings.manager_port,
            log_level="warning", access_log=False, log_config=None,
        )
        self.server = uvicorn.Server(config)
        def run_server() -> None:
            try:
                self.server.run()
            except Exception:
                LOGGER.exception("Le backend embarque s'est arrete avec une erreur")
        self.server_thread = threading.Thread(target=run_server, name="beam-manager-api", daemon=True)
        self.server_thread.start()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if _backend_ready():
                return True
            time.sleep(0.2)
        raise RuntimeError("Le backend Beam-MP-Server-Manager ne repond pas")

    def open(self, *_: Any) -> None:
        webbrowser.open(f"http://127.0.0.1:{self.settings.manager_port}", new=2)

    def service_action(self, action: str) -> None:
        def worker() -> None:
            result = _api(f"/api/server/actions/{action}", method="POST", timeout=25)
            title = "Action BeamMP terminee" if result and result.get("success") else "Action BeamMP impossible"
            message = result.get("message", "Consultez le manager") if result else "Le backend ne repond pas"
            try:
                self.icon.notify(message, title)
            except NotImplementedError:
                pass
            self.refresh_status()
        threading.Thread(target=worker, daemon=True).start()

    def refresh_status(self) -> None:
        state = _status()
        if state != self.state:
            self.state = state
            self.icon.icon = _icon_image(state)
            self.icon.title = f"{APP_TITLE} - {state}"
            self.icon.update_menu()

    def monitor(self) -> None:
        while not self.stop_event.wait(15):
            if not _backend_ready() and (self.server_thread is None or not self.server_thread.is_alive()):
                try:
                    self.start_backend()
                    self.icon.notify("Le backend local a ete relance", APP_TITLE)
                except Exception:
                    LOGGER.exception("Relance du backend impossible")
            self.refresh_status()
            payload = _api("/api/notifications") or {}
            items = payload.get("items", [])
            new_items = [item for item in items if int(item.get("id", 0)) > self.last_notification_id]
            if items:
                self.last_notification_id = max(self.last_notification_id, max(int(item.get("id", 0)) for item in items))
            preferences = self.store.settings().get("notification_preferences", {})
            if preferences.get("windows"):
                for item in reversed(new_items[-3:]):
                    if preferences.get(item.get("kind"), True):
                        try:
                            self.icon.notify(item.get("message", ""), item.get("title", APP_TITLE))
                        except NotImplementedError:
                            break

    def quit(self, *_: Any) -> None:
        self.stop_event.set()
        if self.server is not None:
            self.server.should_exit = True
        self.icon.stop()

    def run(self) -> None:
        self.start_backend()
        self.refresh_status()
        initial = _api("/api/notifications") or {}
        if initial.get("items"):
            self.last_notification_id = max(int(item.get("id", 0)) for item in initial["items"])
        threading.Thread(target=self.monitor, name="beam-manager-monitor", daemon=True).start()
        if not self.startup and self.store.settings().get("open_browser_on_start", True):
            self.open()
        self.icon.run()


def _single_instance() -> Any:
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    already_exists = ctypes.windll.kernel32.GetLastError() == 183
    return None if already_exists else handle


def _prefer_project_data_for_local_build() -> None:
    """Keep the user's validated Phase 1-4 database when running from this project dist/."""
    if not getattr(sys, "frozen", False) or os.environ.get("BEAM_MANAGER_DATA_DIR"):
        return
    executable = Path(sys.executable).resolve()
    if len(executable.parents) >= 3:
        project = executable.parents[2]
        candidate = project / "data"
        if (project / "frontend").is_dir() and (candidate / "beamserver.db").is_file():
            os.environ["BEAM_MANAGER_DATA_DIR"] = str(candidate)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--startup", action="store_true")
    args, _ = parser.parse_known_args()
    _prefer_project_data_for_local_build()
    mutex = _single_instance()
    if mutex is None:
        if _backend_ready() and not args.startup:
            webbrowser.open(f"http://127.0.0.1:{get_settings().manager_port}", new=2)
        return 0
    try:
        WindowsApplication(startup=args.startup).run()
        return 0
    except Exception:
        LOGGER.exception("Arret inattendu du lanceur")
        return 1
    finally:
        ctypes.windll.kernel32.ReleaseMutex(mutex)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
