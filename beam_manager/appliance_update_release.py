from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from packaging.version import InvalidVersion, Version


REPOSITORY = "RominouVTJ/Beam-MP-Server-Manager"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases"
SAFE_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


@dataclass(frozen=True)
class ApplianceUpdateRelease:
    available: bool
    installed_version: str
    latest_version: str | None = None
    update_available: bool = False
    release_url: str = RELEASE_URL
    asset_name: str | None = None
    asset_url: str | None = None
    asset_sha256: str | None = None
    message: str | None = None


def expected_asset_name(version: str) -> str:
    return f"Beam-MP-Server-Manager-v{version}.update.zip"


def _newer(current: str, candidate: str) -> bool:
    try:
        return Version(candidate) > Version(current)
    except InvalidVersion:
        return False


def parse_release_payload(raw: object, installed_version: str) -> ApplianceUpdateRelease:
    if not isinstance(raw, dict):
        return ApplianceUpdateRelease(False, installed_version, message="Invalid GitHub release response")
    tag = str(raw.get("tag_name") or "").strip()
    latest = tag[1:] if tag.startswith("v") else tag
    try:
        Version(latest)
    except InvalidVersion:
        return ApplianceUpdateRelease(False, installed_version, message="Latest GitHub release has an invalid version")

    expected = expected_asset_name(latest)
    asset = next(
        (
            item
            for item in raw.get("assets", [])
            if isinstance(item, dict) and item.get("name") == expected
        ),
        None,
    )
    release_url = str(raw.get("html_url") or RELEASE_URL)
    if not asset:
        return ApplianceUpdateRelease(
            True,
            installed_version,
            latest_version=latest,
            update_available=False,
            release_url=release_url,
            message="Latest release has no appliance update package",
        )

    asset_url = str(asset.get("browser_download_url") or "")
    parsed = urlparse(asset_url)
    if parsed.scheme != "https" or parsed.hostname not in SAFE_DOWNLOAD_HOSTS:
        return ApplianceUpdateRelease(
            True,
            installed_version,
            latest_version=latest,
            update_available=False,
            release_url=release_url,
            message="Latest release contains an unexpected update download URL",
        )

    digest = str(asset.get("digest") or "")
    checksum = digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
    if checksum is not None and (len(checksum) != 64 or any(c not in "0123456789abcdefABCDEF" for c in checksum)):
        checksum = None

    return ApplianceUpdateRelease(
        True,
        installed_version,
        latest_version=latest,
        update_available=_newer(installed_version, latest) and bool(checksum),
        release_url=release_url,
        asset_name=expected,
        asset_url=asset_url,
        asset_sha256=checksum.casefold() if checksum else None,
        message=None if checksum else "Latest update asset has no GitHub SHA-256 digest",
    )


async def discover_latest_update(
    installed_version: str,
    *,
    timeout: float = 15.0,
) -> ApplianceUpdateRelease:
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": f"Beam-MP-Server-Manager/{installed_version}",
                "Accept": "application/vnd.github+json",
            },
        ) as client:
            response = await client.get(API_URL)
            response.raise_for_status()
            return parse_release_payload(response.json(), installed_version)
    except (httpx.HTTPError, ValueError):
        return ApplianceUpdateRelease(
            False,
            installed_version,
            message="Official GitHub releases are currently unavailable",
        )


async def download_release_asset(
    release: ApplianceUpdateRelease,
    destination: Path,
    *,
    timeout: float = 120.0,
    max_bytes: int = 128 * 1024 * 1024,
) -> int:
    if not release.update_available or not release.asset_url or not release.asset_sha256:
        raise ValueError("No validated appliance update asset is available")
    parsed = urlparse(release.asset_url)
    if parsed.scheme != "https" or parsed.hostname not in SAFE_DOWNLOAD_HOSTS:
        raise ValueError("Update asset URL is not allowed")

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    written = 0
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": f"Beam-MP-Server-Manager/{release.installed_version}"},
        ) as client:
            async with client.stream("GET", release.asset_url) as response:
                response.raise_for_status()
                final = urlparse(str(response.url))
                if final.scheme != "https" or final.hostname not in SAFE_DOWNLOAD_HOSTS:
                    raise ValueError("Update asset redirected to an untrusted host")
                with destination.open("xb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError("Update asset exceeds the allowed size")
                        output.write(chunk)
                        digest.update(chunk)
                    output.flush()
        if written <= 0:
            raise ValueError("Downloaded update asset is empty")
        if digest.hexdigest().casefold() != release.asset_sha256.casefold():
            raise ValueError("Downloaded update asset SHA-256 does not match GitHub")
        destination.chmod(0o600)
        return written
    except Exception:
        destination.unlink(missing_ok=True)
        raise
