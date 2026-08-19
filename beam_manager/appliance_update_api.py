from __future__ import annotations

import json
import os
import platform
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import File, HTTPException, Request, UploadFile

from beam_manager import __version__
from beam_manager.appliance_update_package import UpdatePackageError, validate_update_package
from beam_manager.appliance_update_payload import UpdatePayloadError, stage_update_payload
from beam_manager.appliance_update_release import discover_latest_update, download_release_asset
from beam_manager.main import _require_admin, app


UPDATE_ROOT = Path("/var/lib/beam-manager/appliance-updates")
UPLOAD_ROOT = UPDATE_ROOT / "uploads"
STAGING_ROOT = UPDATE_ROOT / "staged"
RESULT_PATH = Path("/var/lib/beam-appliance/update/last-result.json")
HELPER_PATH = Path("/usr/local/sbin/beam-appliance-apply-update")
MAX_PACKAGE_BYTES = 128 * 1024 * 1024


def appliance_update_supported() -> bool:
    return (
        platform.system() == "Linux"
        and HELPER_PATH.is_file()
        and os.access(HELPER_PATH, os.X_OK)
    )


def read_last_result(path: Path = RESULT_PATH) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    allowed = {"status", "target_version", "previous_version", "message", "updated_at"}
    return {key: raw[key] for key in allowed if key in raw}


async def save_update_upload(upload: UploadFile, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = 0
    try:
        with target.open("xb") as output:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_PACKAGE_BYTES:
                    raise UpdatePackageError("Update package exceeds the 128 MiB limit")
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        target.chmod(0o600)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    if written == 0:
        target.unlink(missing_ok=True)
        raise UpdatePackageError("Update package is empty")
    return written


def schedule_staged_update(source_root: Path, target_version: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/usr/bin/sudo",
            "-n",
            str(HELPER_PATH),
            str(source_root),
            target_version,
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _record_schedule(identity: dict[str, object], target_version: str) -> None:
    try:
        from beam_manager.main import get_phase5_store

        get_phase5_store().app_log(
            "INFO",
            f"Appliance update {target_version} scheduled by {identity['username']}",
        )
    except Exception:
        pass


def _stage_and_schedule(package_path: Path, token: str, identity: dict[str, object]) -> dict[str, object]:
    stage_path = STAGING_ROOT / token
    try:
        validated = validate_update_package(package_path)
        staged = stage_update_payload(validated, stage_path, current_version=__version__)
        result = schedule_staged_update(staged.source_root, staged.version)
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "Privileged update helper failed"
            raise UpdatePackageError(detail)
        _record_schedule(identity, staged.version)
        return {
            "accepted": True,
            "target_version": staged.version,
            "file_count": staged.file_count,
            "expanded_bytes": staged.expanded_bytes,
            "message": "Update scheduled; the Manager may restart and temporarily disconnect",
        }
    except Exception:
        shutil.rmtree(stage_path, ignore_errors=True)
        raise


@app.get("/api/appliance/update/status")
async def appliance_update_status(request: Request) -> dict[str, object]:
    _require_admin(request)
    supported = appliance_update_supported()
    release = await discover_latest_update(__version__, timeout=6.0)
    return {
        "supported": supported,
        "installed_version": __version__,
        "available_version": release.latest_version,
        "update_available": bool(supported and release.update_available),
        "release_available": release.available,
        "release_url": release.release_url,
        "last_result": read_last_result(),
        "message": (
            release.message
            if supported
            else "Appliance self-update is not installed on this host"
        ),
    }


@app.post("/api/appliance/update/install-latest")
async def appliance_update_install_latest(request: Request) -> dict[str, object]:
    identity = _require_admin(request)
    if not appliance_update_supported():
        raise HTTPException(status_code=409, detail="Appliance self-update is not available on this host")

    release = await discover_latest_update(__version__, timeout=15.0)
    if not release.update_available:
        raise HTTPException(status_code=409, detail=release.message or "No newer validated update is available")

    token = secrets.token_hex(12)
    package_path = UPLOAD_ROOT / f"{token}.update.zip"
    try:
        await download_release_asset(
            release,
            package_path,
            timeout=120.0,
            max_bytes=MAX_PACKAGE_BYTES,
        )
        return _stage_and_schedule(package_path, token, identity)
    except (UpdatePackageError, UpdatePayloadError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail="Unable to download or schedule appliance update") from exc
    finally:
        package_path.unlink(missing_ok=True)


@app.post("/api/appliance/update/upload")
async def appliance_update_upload(
    request: Request,
    package: UploadFile = File(...),
) -> dict[str, object]:
    identity = _require_admin(request)
    if not appliance_update_supported():
        raise HTTPException(status_code=409, detail="Appliance self-update is not available on this host")

    token = secrets.token_hex(12)
    upload_path = UPLOAD_ROOT / f"{token}.update.zip"
    try:
        await save_update_upload(package, upload_path)
        return _stage_and_schedule(upload_path, token, identity)
    except (UpdatePackageError, UpdatePayloadError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=500, detail="Unable to stage or schedule appliance update") from exc
    finally:
        upload_path.unlink(missing_ok=True)
