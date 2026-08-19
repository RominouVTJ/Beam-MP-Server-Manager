from __future__ import annotations

import asyncio
import hashlib
import json
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from packaging.version import InvalidVersion, Version

from beam_manager.backups import BackupService
from beam_manager.models import BeamMPRelease, OperationItem
from beam_manager.operations import OperationManager
from beam_manager.ssh import SSHClient, SSHError


VERSION_PATTERN = re.compile(r"BeamMP Server v(\d+\.\d+\.\d+)")
SAFE_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class BeamMPUpdateService:
    def __init__(
        self,
        ssh: SSHClient,
        backups: BackupService,
        operations: OperationManager,
        data_dir: Path,
        log_path: str,
        old_log_path: str,
        backups_root: str,
        server_host: str,
        server_port: int,
        timeout: float,
    ) -> None:
        self.ssh = ssh
        self.backups = backups
        self.operations = operations
        self.temp_dir = data_dir / "temp"
        self.log_path = log_path
        self.old_log_path = old_log_path
        self.binary_path = "/opt/beammp/BeamMP-Server"
        self.binary_backups = f"{backups_root.rstrip('/')}/binaries"
        self.server_host = server_host
        self.server_port = server_port
        self.timeout = timeout
        self._cached_release: tuple[float, BeamMPRelease] | None = None

    async def installed_version(self) -> str | None:
        for path in (self.log_path, self.old_log_path):
            try:
                payload = await self.ssh.read_file(path, max_bytes=2_000_000)
            except SSHError:
                continue
            matches = VERSION_PATTERN.findall(payload.decode("utf-8", "replace"))
            if matches:
                return matches[-1]
        return None

    async def platform(self) -> tuple[str | None, str | None]:
        try:
            os_release = (await self.ssh.read_file("/etc/os-release", max_bytes=100_000)).decode("utf-8", "replace")
            distro = re.search(r"^ID=(.+)$", os_release, re.M)
            version = re.search(r'^VERSION_ID="?([^"\n]+)', os_release, re.M)
            def read_header(sftp):
                with sftp.open(self.binary_path, "rb") as binary:
                    return binary.read(64)

            header = await self.ssh.run_sftp(read_header)
            architecture = "x86_64" if len(header) >= 20 and header[18:20] == b"\x3e\x00" else None
            platform = f"{(distro.group(1).strip() if distro else '').strip(chr(34))}.{version.group(1) if version else ''}.{architecture or ''}"
            return platform if platform.startswith("debian.") else None, architecture
        except (SSHError, UnicodeError):
            return None, None

    @staticmethod
    def _newer(installed: str | None, latest: str | None) -> bool | None:
        if not installed or not latest:
            return None
        try:
            return Version(latest.lstrip("v")) > Version(installed.lstrip("v"))
        except InvalidVersion:
            return None

    async def _html_release_fallback(
        self,
        client: httpx.AsyncClient,
        platform: str | None,
    ) -> dict:
        """Read GitHub's public official release HTML when its anonymous API is rate limited."""
        latest_response = await client.get("https://github.com/BeamMP/BeamMP-Server/releases/latest")
        latest_response.raise_for_status()
        latest_url = str(latest_response.url)
        parsed_latest = urlparse(latest_url)
        match = re.fullmatch(r"/BeamMP/BeamMP-Server/releases/tag/(v?[^/]+)", parsed_latest.path)
        if parsed_latest.hostname != "github.com" or not match:
            raise ValueError("Redirection de release GitHub inattendue")
        tag = match.group(1)
        expected_asset = f"BeamMP-Server.{platform}" if platform else None
        assets_response = await client.get(
            f"https://github.com/BeamMP/BeamMP-Server/releases/expanded_assets/{tag}"
        )
        assets_response.raise_for_status()
        soup = BeautifulSoup(assets_response.text, "html.parser")
        asset_data = None
        published_at = None
        if expected_asset:
            asset_link = next(
                (
                    link
                    for link in soup.select("a[href]")
                    if link.get_text(" ", strip=True) == expected_asset
                    and str(link.get("href", "")).startswith(
                        f"/BeamMP/BeamMP-Server/releases/download/{tag}/"
                    )
                ),
                None,
            )
            row = asset_link.find_parent("li") if asset_link else None
            digest_node = row.select_one("clipboard-copy[value^='sha256:']") if row else None
            time_node = row.select_one("relative-time[datetime]") if row else None
            if asset_link and digest_node:
                asset_url = urljoin("https://github.com", str(asset_link.get("href")))
                asset_size = None
                try:
                    head = await client.head(asset_url)
                    head.raise_for_status()
                    if urlparse(str(head.url)).hostname not in SAFE_DOWNLOAD_HOSTS:
                        raise ValueError("Redirection de binaire GitHub inattendue")
                    if head.headers.get("content-length"):
                        asset_size = int(head.headers["content-length"])
                except (httpx.HTTPError, ValueError):
                    asset_size = None
                asset_data = {
                    "name": expected_asset,
                    "browser_download_url": asset_url,
                    "size": asset_size,
                    "digest": str(digest_node.get("value", "")),
                }
                published_at = str(time_node.get("datetime")) if time_node else None
        return {
            "tag_name": tag,
            "published_at": published_at,
            "body": None,
            "html_url": latest_url,
            "assets": [asset_data] if asset_data else [],
        }

    async def check(self, refresh: bool = False) -> BeamMPRelease:
        installed, platform_data = await asyncio.gather(self.installed_version(), self.platform())
        platform, architecture = platform_data
        if not refresh and self._cached_release and time.time() - self._cached_release[0] < 900:
            cached = self._cached_release[1].model_copy(deep=True)
            cached.installed_version = installed
            cached.update_available = self._newer(installed, cached.latest_version)
            return cached
        release_url = "https://github.com/BeamMP/BeamMP-Server/releases"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                headers={"User-Agent": "Beam-MP-Server-Manager/0.10.0", "Accept": "application/vnd.github+json"},
                follow_redirects=True,
            ) as client:
                try:
                    response = await client.get("https://api.github.com/repos/BeamMP/BeamMP-Server/releases/latest")
                    response.raise_for_status()
                    raw = response.json()
                except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                    raw = await self._html_release_fallback(client, platform)
            latest = str(raw.get("tag_name") or "").lstrip("v") or None
            expected_asset = f"BeamMP-Server.{platform}" if platform else None
            asset = next((item for item in raw.get("assets", []) if item.get("name") == expected_asset), None)
            digest = str(asset.get("digest") or "") if asset else ""
            checksum = digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
            result = BeamMPRelease(
                installed_version=installed,
                latest_version=latest,
                update_available=self._newer(installed, latest),
                release_date=raw.get("published_at"),
                description=str(raw.get("body") or "")[:1200] or None,
                release_url=str(raw.get("html_url") or release_url),
                asset_name=asset.get("name") if asset else None,
                asset_url=asset.get("browser_download_url") if asset else None,
                asset_size=int(asset.get("size")) if asset and asset.get("size") else None,
                asset_sha256=checksum,
                platform=platform,
                update_supported=bool(asset and checksum and architecture == "x86_64"),
                message=(
                    None
                    if asset and checksum
                    else "Mise a jour automatique desactivee : binaire ou checksum officiel indisponible pour cette plateforme."
                ),
            )
            self._cached_release = (time.time(), result.model_copy(deep=True))
            return result
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return BeamMPRelease(
                installed_version=installed,
                release_url=release_url,
                available=False,
                update_supported=False,
                platform=platform,
                message="Impossible de joindre les releases officielles BeamMP.",
            )

    async def start_update(self, target_version: str) -> OperationItem:
        release = await self.check(refresh=True)
        normalized = target_version.lstrip("v")
        if release.latest_version != normalized:
            raise ValueError("La version cible ne correspond pas a la derniere release officielle")
        if not release.update_available:
            raise ValueError("BeamMP Server est deja a jour")
        if not release.update_supported or not release.asset_url or not release.asset_sha256:
            raise ValueError(release.message or "Mise a jour automatique non disponible")
        operation = self.operations.create("beammp_update", f"BeamMP Server {normalized}")
        asyncio.create_task(self._run_update(operation.id, release))
        return operation

    async def _run_update(self, operation_id: str, release: BeamMPRelease) -> None:
        operation = self.operations.get(operation_id)
        if operation is None:
            return
        local_path = self.temp_dir / f"BeamMP-Server-{release.latest_version}-{operation_id}"
        backup_path = f"{self.binary_backups}/BeamMP-Server-{release.installed_version or 'unknown'}"
        remote_temp = f"/opt/beammp/.BeamMP-Server-update-{operation_id}"
        rollback_needed = False
        try:
            async with self.operations._worker_lock:
                await self.backups.create(
                    name=f"Avant mise a jour BeamMP {release.latest_version}",
                    reason=f"automatic-before-beammp-update:{release.installed_version}->{release.latest_version}",
                )
                self.temp_dir.mkdir(parents=True, exist_ok=True)
                operation.state = "downloading"
                operation.stage = "Telechargement officiel GitHub"
                operation.total = release.asset_size
                started = time.monotonic()
                parsed = urlparse(release.asset_url or "")
                if parsed.scheme != "https" or parsed.hostname not in SAFE_DOWNLOAD_HOSTS:
                    raise ValueError("URL de release BeamMP non autorisee")
                digest = hashlib.sha256()
                async with httpx.AsyncClient(timeout=None, follow_redirects=True, headers={"User-Agent": "Beam-MP-Server-Manager/0.10.0"}) as client:
                    async with client.stream("GET", release.asset_url) as response:
                        response.raise_for_status()
                        if urlparse(str(response.url)).hostname not in SAFE_DOWNLOAD_HOSTS:
                            raise ValueError("Redirection de release BeamMP non autorisee")
                        with local_path.open("wb") as output:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if self.operations.cancelled(operation_id):
                                    raise asyncio.CancelledError
                                output.write(chunk)
                                digest.update(chunk)
                                operation.transferred += len(chunk)
                                operation.speed_bps = operation.transferred / max(time.monotonic() - started, 0.001)
                if digest.hexdigest().casefold() != release.asset_sha256.casefold():
                    raise ValueError("Le checksum officiel BeamMP ne correspond pas")
                if release.asset_size and local_path.stat().st_size != release.asset_size:
                    raise ValueError("La taille du binaire BeamMP ne correspond pas a la release")

                operation.state = "transferring"
                operation.stage = "Sauvegarde et transfert vers le serveur"
                await self.ssh.ensure_directory(self.binary_backups, mode=0o2750)

                def prepare_sync(sftp):
                    with sftp.open(self.binary_path, "rb") as source, sftp.open(backup_path, "wb") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                    with local_path.open("rb") as source, sftp.open(remote_temp, "wb") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                    if sftp.stat(remote_temp).st_size != local_path.stat().st_size:
                        raise SSHError("Le binaire transfere est incomplet")
                    backups = sorted(
                        (
                            item for item in sftp.listdir_attr(self.binary_backups)
                            if stat.S_ISREG(item.st_mode) and item.filename.startswith("BeamMP-Server-")
                        ),
                        key=lambda item: item.st_mtime,
                        reverse=True,
                    )
                    for item in backups[5:]:
                        sftp.remove(f"{self.binary_backups}/{item.filename}")

                await self.ssh.run_sftp(prepare_sync)
                if self.operations.cancelled(operation_id):
                    raise asyncio.CancelledError
                operation.stage = "Arret et remplacement securise"
                stopped = await self.ssh.execute_service_command("stop")
                if stopped.exit_code != 0:
                    raise SSHError("BeamMP n'a pas pu etre arrete")
                rollback_needed = True

                def replace_sync(sftp):
                    with sftp.open(remote_temp, "rb") as source, sftp.open(self.binary_path, "wb") as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                        target.flush()
                    sftp.remove(remote_temp)

                await self.ssh.run_sftp(replace_sync)
                operation.stage = "Redemarrage et validation"
                started_result = await self.ssh.execute_service_command("start")
                if started_result.exit_code != 0:
                    raise SSHError("Le nouveau BeamMP n'a pas demarre")
                if not await self._wait_healthy():
                    raise SSHError("Le nouveau BeamMP n'a pas passe les controles de sante")
                rollback_needed = False
                self._cached_release = None
                operation.state = "success"
                operation.stage = "Mise a jour validee"
                operation.message = "BeamMP mis a jour et revenu ONLINE"
        except asyncio.CancelledError:
            operation.state = "cancelled"
            operation.stage = "Annule"
            operation.message = "Mise a jour annulee avant remplacement"
        except Exception as exc:
            if rollback_needed:
                operation.state = "rollback"
                operation.stage = "Rollback automatique"
                try:
                    await self.ssh.execute_service_command("stop")

                    def rollback_sync(sftp):
                        with sftp.open(backup_path, "rb") as source, sftp.open(self.binary_path, "wb") as target:
                            while chunk := source.read(1024 * 1024):
                                target.write(chunk)
                            target.flush()

                    await self.ssh.run_sftp(rollback_sync)
                    await self.ssh.execute_service_command("start")
                    restored = await self._wait_healthy()
                    operation.state = "error"
                    operation.message = (
                        "La mise a jour a echoue. L'ancienne version a ete restauree."
                        if restored
                        else "La mise a jour et le rollback necessitent une intervention manuelle."
                    )
                except Exception:
                    operation.state = "error"
                    operation.message = "Rollback BeamMP incomplet; intervention manuelle requise"
            else:
                operation.state = "error"
                operation.stage = "Echec sans modification du serveur"
                operation.message = str(exc)
        finally:
            local_path.unlink(missing_ok=True)
            try:
                await self.ssh.run_sftp(lambda sftp: sftp.remove(remote_temp))
            except Exception:
                pass
            self.operations.finish(operation_id)

    async def _wait_healthy(self) -> bool:
        for _ in range(20):
            await asyncio.sleep(1)
            status = await self.ssh.execute_service_command("status")
            if status.exit_code != 0 or status.stdout.strip() != "active":
                continue
            try:
                payload = await self.ssh.read_file(
                    "/opt/beammp/Resources/Server/BeamServerManager/telemetry.json",
                    max_bytes=2_000_000,
                )
                generated = json.loads(payload.decode("utf-8")).get("generated_at")
                timestamp = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - timestamp).total_seconds() < 10:
                    return True
            except (SSHError, ValueError, UnicodeError, json.JSONDecodeError):
                continue
        return False
