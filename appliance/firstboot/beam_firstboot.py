#!/usr/bin/env python3
"""Root-only, resumable graphical First Run for the BeamMP appliance."""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import queue
import re
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence


LOGGER = logging.getLogger("beam-appliance-firstboot")


CONFIG_DIR = Path("/etc/beam-appliance")
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_DIR = Path("/var/lib/beam-appliance")
PROGRESS_PATH = STATE_DIR / "firstboot-state.json"
MARKER_PATH = STATE_DIR / "firstboot-complete"
MANAGER_ENV_PATH = Path("/etc/beam-manager/beam-manager.env")
PAIRING_CODE_PATH = Path("/var/lib/beam-manager/setup-pairing.secret")
KEYBOARD_TEST = "A / Q    Z / W    @    1234567890    ! ? # $ % & *"
SUPPORTED_LOCALES = {"fr_FR.UTF-8", "en_GB.UTF-8", "en_US.UTF-8"}
SUPPORTED_KEYBOARDS = {"fr", "gb", "us"}
US_TIMEZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
)
SUPPORTED_TIMEZONES = {"Europe/Paris", "Europe/London", *US_TIMEZONES}
RESERVED_USERNAMES = {
    "root", "daemon", "bin", "sys", "sync", "games", "man", "lp", "mail",
    "news", "uucp", "proxy", "www-data", "backup", "list", "irc", "_apt",
    "nobody", "systemd-network", "systemd-timesync", "messagebus", "sshd",
    "beammanager", "beammpserver", "builder", "beamadmin",
}
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")
STAGES = (
    "new", "language_selected", "region_selected", "keyboard_applied",
    "keyboard_confirmed", "localization_applied", "account_pending",
    "account_created", "manager_verified", "complete",
)


class FirstBootError(RuntimeError):
    """Expected, user-safe First Run failure."""


@dataclass(frozen=True)
class Region:
    key: str
    country: str
    language: str
    locale: str
    timezone: str
    keyboard_layout: str


REGIONS = {
    "france": Region("france", "France", "fr", "fr_FR.UTF-8", "Europe/Paris", "fr"),
    "united-kingdom": Region("united-kingdom", "United Kingdom", "en", "en_GB.UTF-8", "Europe/London", "gb"),
    "united-states": Region("united-states", "United States", "en", "en_US.UTF-8", "America/New_York", "us"),
}

GUI_KEYBOARDS = {
    "fr": "Français - AZERTY",
    "gb": "English UK - QWERTY",
    "us": "English US - QWERTY",
}
GUI_TIMEZONES = {
    "Europe/Paris": "Paris",
    "Europe/London": "London",
    "America/New_York": "New York",
    "America/Chicago": "Chicago",
    "America/Denver": "Denver",
    "America/Los_Angeles": "Los Angeles",
}


@dataclass
class Selection:
    language: str
    region_key: str
    country: str
    locale: str
    timezone: str
    keyboard_layout: str
    maintenance_username: str = ""

    @classmethod
    def from_region(cls, region: Region, language: str | None = None) -> "Selection":
        return cls(
            language=language or region.language,
            region_key=region.key,
            country=region.country,
            locale=region.locale,
            timezone=region.timezone,
            keyboard_layout=region.keyboard_layout,
        )

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "Selection":
        return cls(**{key: str(value.get(key, "")) for key in cls.__dataclass_fields__})


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Executor(Protocol):
    def execute(
        self,
        command: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
    ) -> CommandResult: ...


class SystemExecutor:
    def execute(
        self,
        command: Sequence[str],
        *,
        stdin: str | None = None,
        check: bool = True,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(command),
                input=stdin,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except OSError as exc:
            raise FirstBootError(f"Unable to execute {Path(command[0]).name}") from exc
        if check and completed.returncode != 0:
            message = completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "command failed"
            raise FirstBootError(f"{Path(command[0]).name}: {message}")
        return CommandResult(completed.returncode, completed.stdout.strip(), completed.stderr.strip())


def validate_username(username: str) -> str:
    value = username.strip()
    if not USERNAME_PATTERN.fullmatch(value):
        raise ValueError("Use 3-32 lowercase letters, digits, '_' or '-', starting with a letter")
    if value in RESERVED_USERNAMES or value.startswith("systemd-"):
        raise ValueError("This maintenance username is reserved")
    return value


def validate_selection(selection: Selection) -> None:
    if selection.language not in {"en", "fr"}:
        raise ValueError("Unsupported language")
    if selection.region_key not in REGIONS:
        raise ValueError("Unsupported region")
    if selection.country != REGIONS[selection.region_key].country:
        raise ValueError("Country does not match the selected region")
    if selection.locale not in SUPPORTED_LOCALES:
        raise ValueError("Unsupported locale")
    if selection.keyboard_layout not in SUPPORTED_KEYBOARDS:
        raise ValueError("Unsupported keyboard layout")
    if selection.timezone not in SUPPORTED_TIMEZONES:
        raise ValueError("Invalid IANA timezone")


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 256:
        raise ValueError("Maintenance password must contain 12-256 characters")
    if any(character in password for character in ("\0", "\n", "\r")):
        raise ValueError("Maintenance password contains an unsupported control character")


def _secure_directory(path: Path, mode: int, strict_owner: bool) -> None:
    if path.is_symlink():
        raise FirstBootError(f"Unsafe symlink directory: {path}")
    if not path.exists():
        path.mkdir(mode=mode, parents=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise FirstBootError(f"Not a directory: {path}")
    if strict_owner and (info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022):
        raise FirstBootError(f"Unsafe ownership or permissions: {path}")


def atomic_write(path: Path, payload: bytes, *, mode: int, strict_owner: bool) -> None:
    _secure_directory(path.parent, 0o755 if path.parent == CONFIG_DIR else 0o700, strict_owner)
    if path.is_symlink():
        raise FirstBootError(f"Refusing symlink target: {path}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: dict[str, object], *, mode: int, strict_owner: bool) -> None:
    atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=mode,
        strict_owner=strict_owner,
    )


def read_json(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise FirstBootError(f"Refusing symlink state: {path}")
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FirstBootError(f"Invalid state file: {path}") from exc
    if not isinstance(value, dict):
        raise FirstBootError(f"Invalid state object: {path}")
    return value


def stage_index(stage: str) -> int:
    try:
        return STAGES.index(stage)
    except ValueError:
        return 0


def apply_keyboard(layout: str, executor: Executor) -> None:
    if layout not in SUPPORTED_KEYBOARDS:
        raise ValueError("Unsupported keyboard layout")

    # First Run starts before the normal D-Bus/polkit stack is guaranteed to
    # be available. Persist Debian's keyboard configuration directly, then
    # apply the layout to the running Xorg session immediately.
    keyboard_payload = (
        'XKBMODEL="pc105"\n'
        f'XKBLAYOUT="{layout}"\n'
        'XKBVARIANT=""\n'
        'XKBOPTIONS=""\n'
        'BACKSPACE="guess"\n'
    )
    keyboard_tmp = "/etc/default/.beam-keyboard.tmp"
    executor.execute(("/usr/bin/tee", keyboard_tmp), stdin=keyboard_payload)
    executor.execute(("/usr/bin/chmod", "0644", keyboard_tmp))
    executor.execute(("/usr/bin/mv", "-f", keyboard_tmp, "/etc/default/keyboard"))
    persisted = executor.execute(
        ("/usr/bin/grep", "-E", rf'^XKBLAYOUT="{re.escape(layout)}"$', "/etc/default/keyboard"),
        check=False,
    )
    if persisted.returncode != 0:
        raise FirstBootError("Persistent keyboard verification failed")

    display = os.environ.get("DISPLAY")
    if display:
        executor.execute(("/usr/bin/setxkbmap", "-display", display, "-layout", layout))
        status_output = executor.execute(("/usr/bin/setxkbmap", "-display", display, "-query")).stdout
        if not re.search(rf"^layout:\s*{re.escape(layout)}\b", status_output, re.MULTILINE):
            raise FirstBootError("X11 keyboard verification failed")
    else:
        loadkeys = executor.execute(("/usr/bin/test", "-x", "/usr/bin/loadkeys"), check=False)
        if loadkeys.returncode == 0:
            executor.execute(("/usr/bin/loadkeys", layout))

def _normalized_locale(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold().replace("utf8", "utf-8"))


def apply_localization(selection: Selection, executor: Executor) -> None:
    validate_selection(selection)
    # Debian locale-gen does not take a locale name as a selector.
    # It generates locales enabled in /etc/locale.gen. Ensure the selected
    # V1 locale is enabled there, then generate while preserving existing ones.
    locale_gen_line = f"{selection.locale} UTF-8"
    enabled = executor.execute(
        ("/usr/bin/grep", "-Fqx", locale_gen_line, "/etc/locale.gen"),
        check=False,
    )
    if enabled.returncode != 0:
        executor.execute(("/usr/bin/tee", "-a", "/etc/locale.gen"), stdin=locale_gen_line + "\n")
    executor.execute(("/usr/sbin/locale-gen", "--keep-existing"))
    # Avoid update-locale during early boot: write the standard Debian
    # locale file directly, then verify the persisted LANG value.
    locale_payload = f"LANG={selection.locale}\n"
    locale_tmp = "/etc/default/.beam-locale.tmp"
    executor.execute(("/usr/bin/tee", locale_tmp), stdin=locale_payload)
    executor.execute(("/usr/bin/chmod", "0644", locale_tmp))
    executor.execute(("/usr/bin/mv", "-f", locale_tmp, "/etc/default/locale"))
    persisted_locale = executor.execute(
        ("/usr/bin/grep", "-F", f"LANG={selection.locale}", "/etc/default/locale"),
        check=False,
    )
    if persisted_locale.returncode != 0:
        raise FirstBootError("Persistent locale verification failed")

    # Keep First Run independent from systemd-timedated/D-Bus as well.
    zoneinfo = f"/usr/share/zoneinfo/{selection.timezone}"
    executor.execute(("/usr/bin/test", "-f", zoneinfo))
    executor.execute(("/usr/bin/ln", "-snf", zoneinfo, "/etc/localtime"))
    timezone_tmp = "/etc/.beam-timezone.tmp"
    executor.execute(("/usr/bin/tee", timezone_tmp), stdin=f"{selection.timezone}\n")
    executor.execute(("/usr/bin/chmod", "0644", timezone_tmp))
    executor.execute(("/usr/bin/mv", "-f", timezone_tmp, "/etc/timezone"))

    locales = executor.execute(("/usr/bin/locale", "-a")).stdout.splitlines()
    expected = _normalized_locale(selection.locale)
    if expected not in {_normalized_locale(value) for value in locales}:
        raise FirstBootError("Locale verification failed")
    timezone_target = executor.execute(("/usr/bin/readlink", "-f", "/etc/localtime")).stdout.strip()
    if timezone_target != zoneinfo:
        raise FirstBootError("Timezone symlink verification failed")
    timezone_value = executor.execute(("/usr/bin/cat", "/etc/timezone")).stdout.strip()
    if timezone_value != selection.timezone:
        raise FirstBootError("Timezone verification failed")

def create_maintenance_user(
    username: str,
    password: str,
    executor: Executor,
    *,
    allow_existing: bool,
) -> None:
    value = validate_username(username)
    validate_password(password)
    existing = executor.execute(("/usr/bin/getent", "passwd", value), check=False).returncode == 0
    if existing and not allow_existing:
        raise ValueError("This Linux account already exists")
    if not existing:
        executor.execute((
            "/usr/sbin/useradd", "--create-home", "--shell", "/bin/bash",
            "--groups", "sudo", "--", value,
        ))
    else:
        executor.execute((
            "/usr/sbin/usermod", "--shell", "/bin/bash", "--append", "--groups", "sudo", "--", value,
        ))
    # The secret is carried only on an anonymous stdin pipe, never in argv or a file.
    executor.execute(("/usr/sbin/chpasswd",), stdin=f"{value}:{password}\n")
    executor.execute(("/usr/bin/passwd", "--lock", "root"))


def update_manager_environment(path: Path, language: str, timezone: str, *, strict_owner: bool) -> None:
    if path.is_symlink():
        raise FirstBootError(f"Refusing symlink environment: {path}")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise FirstBootError("Manager environment is unavailable") from exc
    replacements = {
        "BEAM_MANAGER_LANGUAGE": language,
        "BEAM_MANAGER_TIMEZONE": timezone,
    }
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key in replacements:
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in replacements.items() if key not in seen)
    atomic_write(path, ("\n".join(output) + "\n").encode(), mode=0o644, strict_owner=strict_owner)


def default_manager_health() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=2) as response:
            payload = json.load(response)
        return response.status == 200 and payload.get("manager") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


class FirstBootEngine:
    def __init__(
        self,
        *,
        executor: Executor | None = None,
        config_path: Path = CONFIG_PATH,
        progress_path: Path = PROGRESS_PATH,
        marker_path: Path = MARKER_PATH,
        manager_env_path: Path = MANAGER_ENV_PATH,
        pairing_code_path: Path = PAIRING_CODE_PATH,
        manager_health: Callable[[], bool] = default_manager_health,
        sleep: Callable[[float], None] = time.sleep,
        strict_owner: bool | None = None,
    ) -> None:
        self.executor = executor or SystemExecutor()
        self.config_path = config_path
        self.progress_path = progress_path
        self.marker_path = marker_path
        self.manager_env_path = manager_env_path
        self.pairing_code_path = pairing_code_path
        self.pairing_code = ""
        self.manager_health = manager_health
        self.sleep = sleep
        self.strict_owner = (os.name == "posix" and getattr(os, "geteuid", lambda: 1)() == 0) if strict_owner is None else strict_owner

    def complete(self) -> bool:
        if self.marker_path.is_symlink():
            raise FirstBootError("Unsafe First Run marker symlink")
        return self.marker_path.is_file()

    def progress(self) -> dict[str, object]:
        value = read_json(self.progress_path)
        if value and value.get("format") != 1:
            raise FirstBootError("Unsupported First Run state format")
        return value

    def selection(self) -> Selection | None:
        value = self.progress().get("selection")
        return Selection.from_dict(value) if isinstance(value, dict) else None

    def _record(self, stage: str, selection: Selection) -> None:
        validate_selection(selection)
        atomic_json(
            self.progress_path,
            {"format": 1, "stage": stage, "selection": asdict(selection)},
            mode=0o600,
            strict_owner=self.strict_owner,
        )

    def record_choice(self, stage: str, selection: Selection) -> None:
        if stage not in {"language_selected", "region_selected"}:
            raise ValueError("Invalid choice stage")
        self._record(stage, selection)

    def ensure_keyboard(self, selection: Selection) -> None:
        progress = self.progress()
        current = Selection.from_dict(progress.get("selection", {})) if progress else None
        if stage_index(str(progress.get("stage", "new"))) < stage_index("keyboard_applied") or not current or current.keyboard_layout != selection.keyboard_layout:
            apply_keyboard(selection.keyboard_layout, self.executor)
            self._record("keyboard_applied", selection)

    def confirm_keyboard(self, selection: Selection) -> None:
        self._record("keyboard_confirmed", selection)

    def ensure_localization(self, selection: Selection) -> None:
        progress = self.progress()
        if stage_index(str(progress.get("stage", "new"))) < stage_index("localization_applied"):
            apply_localization(selection, self.executor)
            self._record("localization_applied", selection)

    def ensure_account(self, selection: Selection, password: str) -> None:
        username = validate_username(selection.maintenance_username)
        selection.maintenance_username = username
        progress = self.progress()
        stage = str(progress.get("stage", "new"))
        if stage_index(stage) >= stage_index("account_created"):
            return
        prior_selection = Selection.from_dict(progress.get("selection", {})) if progress else None
        allow_existing = stage == "account_pending" and prior_selection is not None and prior_selection.maintenance_username == username
        self._record("account_pending", selection)
        create_maintenance_user(username, password, self.executor, allow_existing=allow_existing)
        self._record("account_created", selection)

    def resume_account_if_password_is_set(self, selection: Selection) -> bool:
        """Close the crash window after chpasswd without replacing its password."""
        progress = self.progress()
        if str(progress.get("stage", "new")) != "account_pending":
            return False
        username = validate_username(selection.maintenance_username)
        status_result = self.executor.execute(("/usr/bin/passwd", "--status", username), check=False)
        fields = status_result.stdout.split()
        if status_result.returncode != 0 or len(fields) < 2 or fields[1] != "P":
            return False
        self.executor.execute((
            "/usr/sbin/usermod", "--shell", "/bin/bash", "--append", "--groups", "sudo", "--", username,
        ))
        self.executor.execute(("/usr/bin/passwd", "--lock", "root"))
        self._record("account_created", selection)
        return True

    def _persist_config(self, selection: Selection, complete: bool) -> None:
        atomic_json(
            self.config_path,
            {
                "format": 1,
                "default_language": selection.language,
                "country": selection.country,
                "locale": selection.locale,
                "timezone": selection.timezone,
                "keyboard_layout": selection.keyboard_layout,
                "firstboot_complete": complete,
            },
            mode=0o644,
            strict_owner=self.strict_owner,
        )

    def ensure_manager(self, selection: Selection) -> None:
        progress = self.progress()
        if stage_index(str(progress.get("stage", "new"))) >= stage_index("manager_verified"):
            self.pairing_code = self._read_pairing_code()
            return
        self._persist_config(selection, False)
        update_manager_environment(
            self.manager_env_path, selection.language, selection.timezone,
            strict_owner=self.strict_owner,
        )

        # The First Run unit intentionally keeps NoNewPrivileges=yes. Running
        # runuser/su from that hardened process makes PAM/setuid transitions
        # unreliable. Ask PID 1 to create a separate transient service owned
        # by beammanager instead, while piping stdout back so the one-time
        # pairing code is never written to the journal.
        sync = self.executor.execute((
            "/usr/bin/systemd-run",
            "--quiet",
            "--wait",
            "--pipe",
            "--collect",
            "--uid=beammanager",
            "--gid=beammanager",
            "--setenv=BEAM_MANAGER_DATA_DIR=/var/lib/beam-manager",
            "--setenv=BEAM_MANAGER_SESSION_SECRET_FILE=/var/lib/beam-manager/session.secret",
            "/opt/beam-manager/.venv/bin/beam-manager", "firstboot-sync",
            "--language", selection.language,
            "--country", selection.country,
            "--locale", selection.locale,
            "--timezone", selection.timezone,
            "--keyboard", selection.keyboard_layout,
        ))
        match = re.search(r"^SETUP_PAIRING_CODE=([A-Z2-9]{4}(?:-[A-Z2-9]{4}){4})$", sync.stdout, re.MULTILINE)
        self.pairing_code = match.group(1) if match else self._read_pairing_code()
        if not self.pairing_code:
            raise FirstBootError("Manager pairing code was not created")
        self.executor.execute(("/usr/bin/systemctl", "restart", "beam-manager.service"))
        for _ in range(30):
            if self.manager_health():
                self._record("manager_verified", selection)
                return
            self.sleep(1)
        raise FirstBootError("Manager did not become ready")


    def _read_pairing_code(self) -> str:
        if self.pairing_code_path.is_symlink():
            raise FirstBootError("Unsafe Manager pairing path")
        try:
            code = self.pairing_code_path.read_text(encoding="ascii").strip().upper()
        except OSError:
            return ""
        if not re.fullmatch(r"[A-Z2-9]{4}(?:-[A-Z2-9]{4}){2}(?:(?:-[A-Z2-9]{4}){2})?", code):
            raise FirstBootError("Invalid Manager pairing code")
        return code

    def finalize(self, selection: Selection) -> None:
        if self.complete():
            return
        self.ensure_manager(selection)
        self._persist_config(selection, True)
        self._record("complete", selection)
        atomic_write(
            self.marker_path,
            b"Beam-MP-Server-Manager First Run complete\n",
            mode=0o600,
            strict_owner=self.strict_owner,
        )


GUI_WORDS = {
    "fr": {
        "steps": ("Langue", "Localisation", "Clavier", "Finalisation"),
        "back": "Retour", "continue": "Continuer", "retry": "Réessayer",
        "location_title": "Où se trouve votre serveur ?",
        "country": "Pays / Région", "timezone": "Fuseau horaire",
        "keyboard_title": "Choisissez votre clavier",
        "keyboard_test": "Testez votre clavier",
        "keyboard_help": "AZERTY    @    1234567890    caractères spéciaux",
        "account_title": "Compte Linux de maintenance",
        "username": "Identifiant", "password": "Mot de passe",
        "confirmation": "Confirmation", "show_password": "Afficher les mots de passe",
        "password_rule": "12 caractères minimum. Réservé à la maintenance exceptionnelle.",
        "finalizing": "Configuration du système",
        "progress": ("Langue", "Clavier", "Fuseau horaire", "Configuration réseau", "Démarrage du Manager"),
        "finished": "Configuration système terminée.",
        "browser": "Après le redémarrage, le code de sécurité sera disponible dans la fenêtre de l’appliance.",
        "pairing": "Code de sécurité de l’appliance", "finish": "Terminer et redémarrer",
        "security_warning": "IMPORTANT : conservez ce code. Il sera requis pour récupérer un compte administrateur et autoriser les modifications sensibles.",
        "no_network": "Aucune adresse réseau détectée. Vérifiez votre connexion réseau.",
        "error": "Une opération n’a pas pu être terminée. Vous pouvez réessayer.",
    },
    "en": {
        "steps": ("Language", "Location", "Keyboard", "Finalization"),
        "back": "Back", "continue": "Continue", "retry": "Retry",
        "location_title": "Where is your server located?",
        "country": "Country / Region", "timezone": "Timezone",
        "keyboard_title": "Choose your keyboard",
        "keyboard_test": "Test your keyboard",
        "keyboard_help": "QWERTY    @    1234567890    special characters",
        "account_title": "Linux maintenance account",
        "username": "Username", "password": "Password",
        "confirmation": "Confirmation", "show_password": "Show passwords",
        "password_rule": "At least 12 characters. For exceptional maintenance only.",
        "finalizing": "System configuration",
        "progress": ("Language", "Keyboard", "Timezone", "Network configuration", "Starting Manager"),
        "finished": "System configuration complete.",
        "browser": "After reboot, the security code will be available in the appliance window.",
        "pairing": "Appliance security code", "finish": "Finish and reboot",
        "security_warning": "IMPORTANT: keep this code. It is required for administrator recovery and sensitive changes.",
        "no_network": "No network address detected. Check your network connection.",
        "error": "An operation could not be completed. You can retry.",
    },
}


class FirstBootGUIModel:
    """Display-independent state adapter shared by Tk and unit tests."""

    def __init__(self, engine: FirstBootEngine) -> None:
        self.engine = engine
        progress = engine.progress()
        self.stage = str(progress.get("stage", "new"))
        self.selection = engine.selection()
        self.page = self._resume_page()
        self.keyboard_test_hint = KEYBOARD_TEST

    @property
    def language(self) -> str:
        return self.selection.language if self.selection else "fr"

    def _resume_page(self) -> str:
        if self.engine.complete() or stage_index(self.stage) >= stage_index("localization_applied"):
            return "finalization"
        if stage_index(self.stage) >= stage_index("region_selected"):
            return "keyboard"
        if stage_index(self.stage) >= stage_index("language_selected"):
            return "location"
        return "language"

    def select_language(self, language: str) -> None:
        if language not in {"fr", "en"}:
            raise ValueError("Unsupported language")
        region = REGIONS["france" if language == "fr" else "united-kingdom"]
        self.selection = Selection.from_region(region, language)
        self.engine.record_choice("language_selected", self.selection)
        self.stage = "language_selected"
        self.page = "location"

    def select_region(self, region_key: str) -> None:
        if not self.selection or region_key not in REGIONS:
            raise ValueError("Unsupported region")
        self.selection = Selection.from_region(REGIONS[region_key], self.selection.language)
        self.engine.record_choice("region_selected", self.selection)
        self.stage = "region_selected"

    def select_timezone(self, timezone: str) -> None:
        if not self.selection or timezone not in SUPPORTED_TIMEZONES:
            raise ValueError("Unsupported timezone")
        self.selection.timezone = timezone
        self.engine.record_choice("region_selected", self.selection)
        self.stage = "region_selected"

    def select_keyboard(self, layout: str) -> None:
        if not self.selection or layout not in GUI_KEYBOARDS:
            raise ValueError("Unsupported keyboard layout")
        self.selection.keyboard_layout = layout
        self.engine.ensure_keyboard(self.selection)
        self.stage = "keyboard_applied"

    def confirm_keyboard(self) -> None:
        if not self.selection:
            raise ValueError("Missing selection")
        self.engine.confirm_keyboard(self.selection)
        self.engine.ensure_localization(self.selection)
        self.stage = "localization_applied"
        self.page = "finalization"

    def validate_account(self, username: str, password: str, confirmation: str) -> str:
        try:
            validate_username(username)
            validate_password(password)
            if password != confirmation:
                return "Passwords do not match / Les mots de passe diffèrent"
        except ValueError as exc:
            return str(exc)
        return ""

    def create_account(self, username: str, password: str) -> None:
        if not self.selection:
            raise ValueError("Missing selection")
        self.selection.maintenance_username = validate_username(username)
        self.engine.ensure_account(self.selection, password)
        self.stage = "account_created"
        self.page = "finalization"

    def diagnostic_context(self, stage: str, action: str, exc: BaseException) -> str:
        return f"stage={stage} action={action} exception_type={type(exc).__name__}"


class FirstBootGUI:
    """Fullscreen local Tk interface. It creates no network listener or shell."""

    BG = "#111714"
    PANEL = "#1a221e"
    PANEL_ACTIVE = "#243029"
    GREEN = "#62d98b"
    GREEN_DARK = "#2f8f58"
    TEXT = "#f4f7f5"
    MUTED = "#9cadA3"
    ERROR = "#ff7b7b"

    def __init__(
        self,
        engine: FirstBootEngine,
        *,
        root=None,
        fullscreen: bool = True,
        start_finalization: bool = True,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.engine = engine
        self.model = FirstBootGUIModel(engine)
        self.root = root or tk.Tk()
        self.fullscreen = fullscreen
        self.start_finalization = start_finalization
        self.current_page = ""
        self.error_message = ""
        self._finalization_started = False
        self._messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.root.title("BEAM-MP-SERVER-MANAGER First Run")
        self.root.configure(bg=self.BG, cursor="arrow")
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        if fullscreen:
            # The dedicated X session intentionally has no window manager, so
            # enforce the screen-sized undecorated geometry directly as well as
            # setting the conventional fullscreen hint.
            self.root.overrideredirect(True)
            self.root.geometry(f"{self.root.winfo_screenwidth()}x{self.root.winfo_screenheight()}+0+0")
            self.root.attributes("-fullscreen", True)
            self.root.after(100, self.root.focus_force)
        executor = getattr(engine, "executor", None)
        if executor is not None:
            executor.execute(("/usr/bin/xset", "s", "off"), check=False)
            executor.execute(("/usr/bin/xset", "-dpms"), check=False)
        self.root.minsize(900, 600)
        self.shell = tk.Frame(self.root, bg=self.BG)
        self.shell.pack(fill="both", expand=True)
        self.show_page(self.model.page)

    @property
    def words(self) -> dict[str, object]:
        return GUI_WORDS[self.model.language]

    def run(self) -> None:
        self.root.mainloop()

    def _clear(self) -> None:
        for child in self.shell.winfo_children():
            child.destroy()

    def _header(self, active: int) -> None:
        tk = self.tk
        header = tk.Frame(self.shell, bg=self.BG)
        header.pack(fill="x", padx=54, pady=(30, 12))
        tk.Label(
            header, text="BEAM-MP-SERVER-MANAGER", bg=self.BG, fg=self.TEXT,
            font=("DejaVu Sans", 23, "bold"),
        ).pack(anchor="w")
        steps = tk.Frame(header, bg=self.BG)
        steps.pack(fill="x", pady=(22, 0))
        for index, label in enumerate(self.words["steps"], 1):
            color = self.GREEN if index <= active else self.MUTED
            tk.Label(
                steps, text=f"{index}  {label}", bg=self.BG, fg=color,
                font=("DejaVu Sans", 11, "bold" if index == active else "normal"),
            ).pack(side="left", expand=True)

    def _body(self):
        frame = self.tk.Frame(self.shell, bg=self.PANEL, padx=46, pady=32)
        frame.pack(fill="both", expand=True, padx=54, pady=18)
        return frame

    def _title(self, parent, text: str, subtitle: str = "") -> None:
        self.tk.Label(parent, text=text, bg=self.PANEL, fg=self.TEXT, font=("DejaVu Sans", 25, "bold")).pack(anchor="w")
        if subtitle:
            self.tk.Label(parent, text=subtitle, bg=self.PANEL, fg=self.MUTED, font=("DejaVu Sans", 13)).pack(anchor="w", pady=(8, 24))

    def _button(self, parent, text: str, command, *, primary: bool = False, width: int = 22):
        return self.tk.Button(
            parent, text=text, command=command, width=width, padx=16, pady=13,
            bg=self.GREEN if primary else self.PANEL_ACTIVE,
            activebackground="#78e8a0" if primary else "#314037",
            fg="#07110b" if primary else self.TEXT,
            activeforeground="#07110b" if primary else self.TEXT,
            relief="flat", bd=0, cursor="hand2", font=("DejaVu Sans", 12, "bold"),
        )

    def _footer(self, *, back=None, forward=None, retry=None) -> None:
        frame = self.tk.Frame(self.shell, bg=self.BG)
        frame.pack(fill="x", padx=54, pady=(0, 28))
        if back:
            self._button(frame, self.words["back"], back).pack(side="left")
        if retry:
            self._button(frame, self.words["retry"], retry).pack(side="right", padx=(12, 0))
        if forward:
            self._button(frame, self.words["continue"], forward, primary=True).pack(side="right")

    def _show_error(self, parent) -> None:
        if self.error_message:
            self.tk.Label(
                parent, text=self.error_message, bg=self.PANEL, fg=self.ERROR,
                wraplength=900, justify="left", font=("DejaVu Sans", 12, "bold"),
            ).pack(anchor="w", pady=(18, 0))

    def _safe(self, action: str, callback, *, rerender=None) -> bool:
        try:
            callback()
            self.error_message = ""
            return True
        except Exception as exc:
            context = self.model.diagnostic_context(self.model.stage, action, exc)
            LOGGER.exception("First Run action failed %s", context)
            self.error_message = str(self.words["error"])
            if rerender:
                rerender()
            return False

    def show_page(self, page: str) -> None:
        getattr(self, f"show_{page}")()

    def show_language(self) -> None:
        self.current_page = "language"
        self._clear()
        self._header(1)
        body = self._body()
        self._title(body, "Bienvenue / Welcome", "Choisissez votre langue  ·  Choose your language")
        choices = self.tk.Frame(body, bg=self.PANEL)
        choices.pack(expand=True)
        self._button(choices, "Français", lambda: self._choose_language("fr"), primary=True, width=28).pack(pady=10)
        self._button(choices, "English", lambda: self._choose_language("en"), primary=True, width=28).pack(pady=10)

    def _choose_language(self, language: str) -> None:
        if self._safe("select_language", lambda: self.model.select_language(language)):
            self.show_location()

    def show_location(self) -> None:
        self.current_page = "location"
        self._clear()
        self._header(2)
        body = self._body()
        self._title(body, str(self.words["location_title"]))
        self.tk.Label(body, text=self.words["country"], bg=self.PANEL, fg=self.TEXT, font=("DejaVu Sans", 14, "bold")).pack(anchor="w", pady=(6, 8))
        countries = self.tk.Frame(body, bg=self.PANEL)
        countries.pack(fill="x")
        selected_region = self.model.selection.region_key if self.model.selection else ""
        for key, region in REGIONS.items():
            self._button(countries, ("✓ " if key == selected_region else "") + region.country, lambda value=key: self._choose_region(value), width=22).pack(side="left", padx=(0, 12))
        self.tk.Label(body, text=self.words["timezone"], bg=self.PANEL, fg=self.TEXT, font=("DejaVu Sans", 14, "bold")).pack(anchor="w", pady=(28, 8))
        zones = self.tk.Frame(body, bg=self.PANEL)
        zones.pack(fill="x")
        selected_zone = self.model.selection.timezone if self.model.selection else ""
        for value, label in GUI_TIMEZONES.items():
            self._button(zones, ("✓ " if value == selected_zone else "") + label, lambda zone=value: self._choose_timezone(zone), width=14).pack(side="left", padx=(0, 8))
        self._show_error(body)
        self._footer(back=self.show_language, forward=self._continue_to_keyboard)

    def _choose_region(self, region: str) -> None:
        if self._safe("select_region", lambda: self.model.select_region(region)):
            self.show_location()

    def _choose_timezone(self, timezone: str) -> None:
        if self._safe("select_timezone", lambda: self.model.select_timezone(timezone)):
            self.show_location()

    def _continue_to_keyboard(self) -> None:
        if not self.model.selection:
            return
        if self._safe("apply_keyboard", lambda: self.model.select_keyboard(self.model.selection.keyboard_layout), rerender=self.show_location):
            self.show_keyboard()

    def select_keyboard(self, layout: str) -> None:
        if self._safe("apply_keyboard", lambda: self.model.select_keyboard(layout), rerender=self.show_keyboard):
            self.show_keyboard()

    def show_keyboard(self) -> None:
        self.current_page = "keyboard"
        self._clear()
        self._header(3)
        body = self._body()
        self._title(body, str(self.words["keyboard_title"]))
        options = self.tk.Frame(body, bg=self.PANEL)
        options.pack(fill="x", pady=(8, 24))
        selected = self.model.selection.keyboard_layout if self.model.selection else ""
        for layout, label in GUI_KEYBOARDS.items():
            self._button(options, ("✓ " if layout == selected else "") + label, lambda value=layout: self.select_keyboard(value), width=25).pack(side="left", padx=(0, 12))
        self.tk.Label(body, text=self.words["keyboard_test"], bg=self.PANEL, fg=self.TEXT, font=("DejaVu Sans", 14, "bold")).pack(anchor="w")
        self.tk.Label(body, text=self.words["keyboard_help"], bg=self.PANEL, fg=self.MUTED, font=("DejaVu Sans", 11)).pack(anchor="w", pady=(5, 8))
        test_entry = self.tk.Entry(body, font=("DejaVu Sans", 17), bg="#f4f7f5", fg="#111714", insertbackground="#111714", relief="flat")
        test_entry.pack(fill="x", ipady=10)
        test_entry.focus_set()
        self._show_error(body)
        retry = (lambda: self.select_keyboard(selected)) if self.error_message and selected else None
        self._footer(back=self.show_location, forward=self._confirm_keyboard, retry=retry)

    def _confirm_keyboard(self) -> None:
        if self._safe("confirm_keyboard_and_localization", self.model.confirm_keyboard, rerender=self.show_keyboard):
            self.show_finalization()

    def show_account(self) -> None:
        if self.model.stage == "account_pending" and self.model.selection and self.model.selection.maintenance_username:
            try:
                if self.engine.resume_account_if_password_is_set(self.model.selection):
                    self.model.stage = "account_created"
                    self.model.page = "finalization"
                    self.show_finalization()
                    return
            except Exception as exc:
                context = self.model.diagnostic_context(self.model.stage, "resume_account", exc)
                LOGGER.exception("First Run account resume failed %s", context)
                self.error_message = str(self.words["error"])
        self.current_page = "account"
        self._clear()
        self._header(4)
        body = self._body()
        self._title(body, str(self.words["account_title"]), str(self.words["password_rule"]))
        self.username_var = self.tk.StringVar(value=self.model.selection.maintenance_username if self.model.selection else "")
        self.password_var = self.tk.StringVar()
        self.confirmation_var = self.tk.StringVar()
        self.account_error_var = self.tk.StringVar()
        fields = (("username", self.username_var, ""), ("password", self.password_var, "•"), ("confirmation", self.confirmation_var, "•"))
        self.password_entries = []
        for key, variable, mask in fields:
            self.tk.Label(body, text=self.words[key], bg=self.PANEL, fg=self.TEXT, font=("DejaVu Sans", 12, "bold")).pack(anchor="w", pady=(12, 5))
            entry = self.tk.Entry(body, textvariable=variable, show=mask, font=("DejaVu Sans", 15), bg="#f4f7f5", fg="#111714", insertbackground="#111714", relief="flat")
            entry.pack(fill="x", ipady=8)
            if mask:
                self.password_entries.append(entry)
        show_var = self.tk.BooleanVar(value=False)
        self.tk.Checkbutton(
            body, text=self.words["show_password"], variable=show_var,
            command=lambda: [entry.configure(show="" if show_var.get() else "•") for entry in self.password_entries],
            bg=self.PANEL, fg=self.TEXT, selectcolor=self.PANEL_ACTIVE, activebackground=self.PANEL,
            activeforeground=self.TEXT, font=("DejaVu Sans", 11),
        ).pack(anchor="w", pady=(10, 0))
        self.tk.Label(body, textvariable=self.account_error_var, bg=self.PANEL, fg=self.ERROR, font=("DejaVu Sans", 11, "bold")).pack(anchor="w", pady=(8, 0))
        for variable in (self.username_var, self.password_var, self.confirmation_var):
            variable.trace_add("write", lambda *_: self._validate_account_live())
        self._show_error(body)
        self._footer(back=self.show_keyboard, forward=self._create_account)

    def _validate_account_live(self) -> str:
        message = self.model.validate_account(self.username_var.get(), self.password_var.get(), self.confirmation_var.get())
        self.account_error_var.set(message)
        return message

    def _create_account(self) -> None:
        if self._validate_account_live():
            return
        username = self.username_var.get()
        password = self.password_var.get()
        try:
            if self._safe("create_account", lambda: self.model.create_account(username, password), rerender=self.show_account):
                self.password_var.set("")
                self.confirmation_var.set("")
                password = ""
                self.show_finalization()
        finally:
            password = ""

    def show_finalization(self) -> None:
        self.current_page = "finalization"
        self._clear()
        self._header(4)
        body = self._body()
        self._title(body, str(self.words["finalizing"]))
        self.progress_labels = []
        for index, label in enumerate(self.words["progress"]):
            prefix = "✓" if index < 3 else ("●" if index == 3 else "○")
            widget = self.tk.Label(body, text=f"{prefix}  {label}", bg=self.PANEL, fg=self.GREEN if index < 3 else self.TEXT, font=("DejaVu Sans", 14))
            widget.pack(anchor="w", pady=6)
            self.progress_labels.append(widget)
        style = self.ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Beam.Horizontal.TProgressbar", troughcolor=self.PANEL_ACTIVE, background=self.GREEN, bordercolor=self.PANEL_ACTIVE, lightcolor=self.GREEN, darkcolor=self.GREEN)
        self.progress_bar = self.ttk.Progressbar(body, style="Beam.Horizontal.TProgressbar", maximum=100, value=65)
        self.progress_bar.pack(fill="x", pady=(24, 8), ipady=5)
        self._show_error(body)
        if self.start_finalization and not self._finalization_started:
            self.root.after(100, self._begin_finalization)
        elif self.error_message:
            self._footer(back=self.show_keyboard, retry=self._begin_finalization)

    def _begin_finalization(self) -> None:
        if self._finalization_started or not self.model.selection:
            return
        self._finalization_started = True
        self.error_message = ""

        def worker() -> None:
            try:
                try:
                    from beam_console_status import local_ip
                except ImportError:
                    from appliance.firstboot.beam_console_status import local_ip
                address = local_ip()
                self._messages.put(("network", address))
                self.engine.finalize(self.model.selection)
                self.model.stage = "complete"
                self._messages.put(("complete", address))
            except Exception as exc:
                context = self.model.diagnostic_context(self.model.stage, "finalize", exc)
                LOGGER.exception("First Run finalization failed %s", context)
                self._messages.put(("error", None))

        threading.Thread(target=worker, name="beam-firstboot-finalize", daemon=True).start()
        self.root.after(100, self._poll_finalization)

    def _poll_finalization(self) -> None:
        try:
            while True:
                event, value = self._messages.get_nowait()
                if event == "network":
                    self.progress_labels[3].configure(text=f"✓  {self.words['progress'][3]}", fg=self.GREEN)
                    self.progress_labels[4].configure(text=f"●  {self.words['progress'][4]}", fg=self.TEXT)
                    self.progress_bar.configure(value=82)
                elif event == "complete":
                    self._show_complete(value if isinstance(value, str) else None)
                    return
                elif event == "error":
                    self._finalization_started = False
                    self.error_message = str(self.words["error"])
                    self.show_finalization()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_finalization)

    def _show_complete(self, address: str | None) -> None:
        self.current_page = "complete"
        self._clear()
        self._header(4)
        body = self._body()
        self._title(body, str(self.words["finished"]), str(self.words["browser"]))
        details = ["Manager        ONLINE", "BeamMP         " + ("NON CONFIGURÉ" if self.model.language == "fr" else "NOT CONFIGURED")]
        if address:
            details.extend((f"LAN IP         {address}", f"Web            http://{address}:8765"))
        else:
            details.append(str(self.words["no_network"]))
        self.tk.Label(body, text="\n".join(details), bg=self.PANEL, fg=self.TEXT, justify="left", font=("DejaVu Sans Mono", 15)).pack(anchor="w", pady=(24, 12))

        footer = self.tk.Frame(self.shell, bg=self.BG)
        footer.pack(fill="x", padx=54, pady=(0, 28))
        self._button(footer, str(self.words["finish"]), self._finish_to_desktop, primary=True).pack(side="right")

    def _finish_to_desktop(self) -> None:
        self.engine.executor.execute(("/usr/bin/systemd-run", "--quiet", "--collect", "--on-active=2s", "/usr/bin/systemctl", "reboot"), check=False)
        self.root.destroy()


def run_gui(engine: FirstBootEngine) -> Selection | None:
    if engine.complete():
        return None
    application = FirstBootGUI(engine)
    application.run()
    return application.model.selection


class Console:
    def clear(self) -> None:
        print("\033c", end="", flush=True)

    def text(self, prompt: str) -> str:
        return input(prompt)

    def secret(self, prompt: str) -> str:
        return getpass.getpass(prompt)

    def choose(self, title: str, options: Sequence[tuple[str, str]]) -> str:
        while True:
            print(f"\n{title}\n")
            for index, (_, label) in enumerate(options, 1):
                print(f"  {index}. {label}")
            answer = input("\n> ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1][0]


TEXT = {
    "fr": {
        "region": "Pays / Région",
        "keyboard": "Disposition clavier",
        "test": "TESTEZ VOTRE CLAVIER",
        "confirm_keyboard": "Le clavier est-il correct ?",
        "yes": "Confirmer",
        "change": "Changer de clavier",
        "locale": "Locale",
        "timezone": "Fuseau horaire",
        "username": "Identifiant de maintenance : ",
        "password": "Mot de passe de maintenance : ",
        "confirmation": "Confirmation : ",
        "finished": "Configuration système terminée.",
        "browser": "Terminez l'installation depuis votre navigateur.",
        "pairing": "Code d'appairage Web",
        "no_network": "Aucune adresse réseau détectée. Vérifiez votre connexion réseau.",
    },
    "en": {
        "region": "Country / Region",
        "keyboard": "Keyboard layout",
        "test": "TEST YOUR KEYBOARD",
        "confirm_keyboard": "Is the keyboard correct?",
        "yes": "Confirm",
        "change": "Change keyboard",
        "locale": "Locale",
        "timezone": "Timezone",
        "username": "Maintenance username: ",
        "password": "Maintenance password: ",
        "confirmation": "Confirmation: ",
        "finished": "System configuration complete.",
        "browser": "After reboot, the security code will be available in the appliance window.",
        "pairing": "Web pairing code",
        "no_network": "No network address detected. Check your network connection.",
    },
}


def _prompt_default(console: Console, label: str, default: str, validator: Callable[[str], bool]) -> str:
    while True:
        value = console.text(f"{label} [{default}]: ").strip() or default
        if validator(value):
            return value
        print("Invalid value / Valeur invalide")


def run_interactive(engine: FirstBootEngine, console: Console | None = None) -> Selection | None:
    ui = console or Console()
    if engine.complete():
        return None
    progress = engine.progress()
    stage = str(progress.get("stage", "new"))
    selection = engine.selection()
    if stage_index(stage) < stage_index("language_selected") or selection is None:
        ui.clear()
        print("BEAM-MP-SERVER-MANAGER\n\nBienvenue / Welcome\n\nChoisissez votre langue\nChoose your language")
        language = ui.choose("", (("fr", "Français"), ("en", "English")))
        selection = Selection.from_region(REGIONS["france" if language == "fr" else "united-kingdom"], language)
        engine.record_choice("language_selected", selection)
        stage = "language_selected"
    language = selection.language
    words = TEXT[language]

    if stage_index(stage) < stage_index("region_selected"):
        ui.clear()
        key = ui.choose(words["region"], tuple((key, region.country) for key, region in REGIONS.items()))
        selection = Selection.from_region(REGIONS[key], language)
        engine.record_choice("region_selected", selection)
        stage = "region_selected"

    if stage_index(stage) < stage_index("keyboard_confirmed"):
        while True:
            selection.keyboard_layout = _prompt_default(ui, words["keyboard"], selection.keyboard_layout, lambda value: value in SUPPORTED_KEYBOARDS)
            engine.ensure_keyboard(selection)
            print(f"\n{words['test']}\n\n{KEYBOARD_TEST}\n")
            ui.text("> ")
            answer = ui.choose(words["confirm_keyboard"], (("yes", words["yes"]), ("change", words["change"])))
            if answer == "yes":
                engine.confirm_keyboard(selection)
                stage = "keyboard_confirmed"
                break
            engine.record_choice("region_selected", selection)

    if stage_index(stage) < stage_index("localization_applied"):
        selection.locale = _prompt_default(ui, words["locale"], selection.locale, lambda value: value in SUPPORTED_LOCALES)
        if selection.region_key == "united-states":
            selection.timezone = ui.choose(words["timezone"], tuple((value, value) for value in US_TIMEZONES))
        else:
            selection.timezone = _prompt_default(ui, words["timezone"], selection.timezone, lambda value: value in SUPPORTED_TIMEZONES)
        engine.ensure_localization(selection)
        stage = "localization_applied"

    if stage_index(stage) < stage_index("account_created"):
        if stage == "account_pending" and selection.maintenance_username:
            selection.maintenance_username = validate_username(selection.maintenance_username)
            print(f"{words['username']}{selection.maintenance_username}")
            if engine.resume_account_if_password_is_set(selection):
                stage = "account_created"
        else:
            while True:
                try:
                    selection.maintenance_username = validate_username(ui.text(words["username"]))
                    break
                except ValueError as exc:
                    print(exc)
        if stage != "account_created":
            while True:
                password = ui.secret(words["password"])
                confirmation = ui.secret(words["confirmation"])
                try:
                    if password != confirmation:
                        raise ValueError("Passwords do not match / Les mots de passe diffèrent")
                    validate_password(password)
                    break
                except ValueError as exc:
                    print(exc)
            try:
                engine.ensure_account(selection, password)
            finally:
                password = confirmation = ""

    engine.finalize(selection)
    ui.clear()
    try:
        from beam_console_status import local_ip
        address = local_ip()
    except ImportError:
        address = None
    print(f"BEAM-MP-SERVER-MANAGER\n\n{words['finished']}\n\nManager: ONLINE")
    if address:
        print(f"\nLAN IP: {address}\nWeb: http://{address}:8765")
    else:
        print(f"\n{words['no_network']}")
    print(f"\n{words['pairing']}: {engine.pairing_code}")
    print(f"\nBeamMP: {'NON CONFIGURÉ' if language == 'fr' else 'NOT CONFIGURED'}\n\n{words['browser']}")
    return selection


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    parser = argparse.ArgumentParser(description="Beam appliance First Run")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--gui", action="store_true", help="Run the local graphical First Run")
    modes.add_argument("--text", action="store_true", help="Run the maintenance text fallback")
    modes.add_argument("--run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Print supported profiles only")
    args = parser.parse_args()
    if args.dry_run:
        print(json.dumps({key: asdict(value) for key, value in REGIONS.items()}, indent=2))
        return 0
    if not (args.gui or args.text or args.run):
        parser.error("use --gui, --text or --dry-run")
    if os.name != "posix" or getattr(os, "geteuid", lambda: 1)() != 0:
        print("First Run must be started by its dedicated system service", flush=True)
        return 2
    try:
        engine = FirstBootEngine()
        if args.gui:
            run_gui(engine)
        else:
            run_interactive(engine)
        return 0
    except KeyboardInterrupt:
        LOGGER.warning("First Run interrupted stage=unknown action=main exception_type=KeyboardInterrupt")
        return 1
    except Exception as exc:
        LOGGER.exception(
            "Unhandled First Run failure stage=unknown action=main exception_type=%s",
            type(exc).__name__,
        )
        print(f"\nFirst Run paused safely: {exc}\nRestart the appliance to resume.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
