from __future__ import annotations

import json
import re
import stat
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

import tomlkit

from beam_manager.inventory import InventoryService, _safe_join
from beam_manager.models import (
    BackupMod,
    BackupPreview,
    BackupRestoreResponse,
    BackupSummary,
    ConfigDifference,
)
from beam_manager.server_config import ServerConfigService
from beam_manager.ssh import SSHClient, SSHError


BACKUP_ID = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d{2})?$")


class BackupService:
    def __init__(
        self,
        ssh: SSHClient,
        root: str,
        config_path: str,
        active_root: str,
        disabled_root: str,
        log_path: str,
    ) -> None:
        self.ssh = ssh
        self.root = root.rstrip("/")
        self.config_path = config_path
        self.active_root = active_root.rstrip("/")
        self.disabled_root = disabled_root.rstrip("/")
        self.log_path = log_path

    def _path(self, backup_id: str, child: str | None = None) -> str:
        if not BACKUP_ID.fullmatch(backup_id):
            raise SSHError("Identifiant de sauvegarde invalide")
        base = f"{self.root}/{backup_id}"
        if child is None:
            return base
        normalized = PurePosixPath(child)
        if normalized.is_absolute() or ".." in normalized.parts:
            raise SSHError("Chemin de sauvegarde invalide")
        return f"{base}/{normalized.as_posix()}"

    @staticmethod
    def _copy(sftp: Any, source: str, destination: str) -> None:
        with sftp.open(source, "rb") as reader, sftp.open(destination, "wb") as writer:
            while chunk := reader.read(1024 * 1024):
                writer.write(chunk)
            writer.flush()

    @staticmethod
    def _remove_tree(sftp: Any, path: str) -> None:
        for item in sftp.listdir_attr(path):
            child = f"{path}/{item.filename}"
            if stat.S_ISDIR(item.st_mode):
                BackupService._remove_tree(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(path)

    @staticmethod
    def _public_config(payload: bytes) -> dict[str, Any]:
        document = tomlkit.parse(payload.decode("utf-8-sig"))
        section = document.get("General", document)
        allowed = (
            "Name",
            "Description",
            "Private",
            "MaxPlayers",
            "MaxCars",
            "Map",
            "Tags",
            "LogChat",
            "AllowGuests",
            "Port",
        )
        result: dict[str, Any] = {}
        for key in allowed:
            value = section.get(key)
            result[key] = value.unwrap() if hasattr(value, "unwrap") else value
        return result

    async def ensure_root(self) -> bool:
        return await self.ssh.ensure_directory(self.root, mode=0o2750)

    async def create(
        self,
        name: str | None,
        reason: str,
        include_files: list[str] | None = None,
    ) -> BackupSummary:
        await self.ensure_root()
        include_files = include_files or []

        def create_sync(sftp: Any) -> BackupSummary:
            timestamp = datetime.now().astimezone()
            base_id = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
            backup_id = base_id
            for suffix in range(1, 100):
                try:
                    sftp.stat(f"{self.root}/{backup_id}")
                except FileNotFoundError:
                    break
                backup_id = f"{base_id}-{suffix:02d}"
            backup_path = self._path(backup_id)
            sftp.mkdir(backup_path, mode=0o750)
            try:
                with sftp.open(self.config_path, "rb") as config_source:
                    config_payload = config_source.read(2_000_001)
                if len(config_payload) > 2_000_000:
                    raise SSHError("ServerConfig.toml depasse la taille autorisee")
                with sftp.open(f"{backup_path}/ServerConfig.toml", "wb") as target:
                    target.write(config_payload)
                active = [
                    BackupMod(name=item.filename, size=item.st_size)
                    for item in sftp.listdir_attr(self.active_root)
                    if stat.S_ISREG(item.st_mode) and item.filename.lower().endswith(".zip")
                ]
                disabled = [
                    BackupMod(name=item.filename, size=item.st_size)
                    for item in sftp.listdir_attr(self.disabled_root)
                    if stat.S_ISREG(item.st_mode) and item.filename.lower().endswith(".zip")
                ]
                version = None
                try:
                    log_size = sftp.stat(self.log_path).st_size
                    with sftp.open(self.log_path, "rb") as log:
                        log.seek(max(0, log_size - 200_000))
                        log_text = log.read(200_000).decode("utf-8", "replace")
                    match = re.search(r"BeamMP Server v([0-9][0-9.]+)", log_text)
                    version = match.group(1) if match else None
                except OSError:
                    pass

                copied: list[str] = []
                if include_files:
                    files_root = f"{backup_path}/files"
                    sftp.mkdir(files_root, mode=0o750)
                    for state_name in ("active", "disabled"):
                        sftp.mkdir(f"{files_root}/{state_name}", mode=0o750)
                    for remote_path in include_files:
                        if remote_path.startswith(f"{self.active_root}/"):
                            state_name, root = "active", self.active_root
                        elif remote_path.startswith(f"{self.disabled_root}/"):
                            state_name, root = "disabled", self.disabled_root
                        else:
                            raise SSHError("Fichier hors des dossiers de mods autorises")
                        filename = PurePosixPath(remote_path).name
                        if _safe_join(root, filename) != remote_path:
                            raise SSHError("Chemin de mod incoherent")
                        self._copy(
                            sftp,
                            remote_path,
                            f"{files_root}/{state_name}/{filename}",
                        )
                        copied.append(f"{state_name}/{filename}")

                public = self._public_config(config_payload)
                summary = BackupSummary(
                    id=backup_id,
                    created_at=timestamp,
                    name=name or "Sauvegarde automatique",
                    reason=reason,
                    map_path=str(public.get("Map") or ""),
                    active_mods=sorted(active, key=lambda item: item.name.casefold()),
                    disabled_mods=sorted(disabled, key=lambda item: item.name.casefold()),
                    beammp_version=version,
                    backed_up_files=copied,
                )
                manifest = {
                    "schema_version": 1,
                    **summary.model_dump(mode="json"),
                }
                with sftp.open(f"{backup_path}/manifest.json", "wb") as manifest_file:
                    manifest_file.write(
                        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
                    )
                return summary
            except Exception:
                self._remove_tree(sftp, backup_path)
                raise

        return await self.ssh.run_sftp(create_sync)

    def _read_summary(self, sftp: Any, backup_id: str) -> BackupSummary:
        manifest_path = self._path(backup_id, "manifest.json")
        with sftp.open(manifest_path, "rb") as handle:
            payload = handle.read(1_000_001)
        if len(payload) > 1_000_000:
            raise SSHError("Manifest de sauvegarde trop volumineux")
        try:
            raw = json.loads(payload.decode("utf-8"))
            if raw.get("schema_version") != 1:
                raise ValueError
            return BackupSummary.model_validate(raw)
        except (ValueError, UnicodeError) as exc:
            raise SSHError("Manifest de sauvegarde invalide") from exc

    async def list(self) -> list[BackupSummary]:
        await self.ensure_root()

        def list_sync(sftp: Any) -> list[BackupSummary]:
            summaries: list[BackupSummary] = []
            for item in sftp.listdir_attr(self.root):
                if not stat.S_ISDIR(item.st_mode) or not BACKUP_ID.fullmatch(item.filename):
                    continue
                try:
                    summaries.append(self._read_summary(sftp, item.filename))
                except SSHError:
                    continue
            return sorted(summaries, key=lambda item: item.created_at, reverse=True)

        return await self.ssh.run_sftp(list_sync)

    async def preview(self, backup_id: str) -> BackupPreview:
        def preview_sync(sftp: Any) -> BackupPreview:
            summary = self._read_summary(sftp, backup_id)
            with sftp.open(self._path(backup_id, "ServerConfig.toml"), "rb") as handle:
                restored = self._public_config(handle.read(2_000_001))
            with sftp.open(self.config_path, "rb") as handle:
                current = self._public_config(handle.read(2_000_001))
            changes = [
                ConfigDifference(field=key, current=current.get(key), restored=restored.get(key))
                for key in restored
                if key != "Map" and current.get(key) != restored.get(key)
            ]
            current_active = {
                item.filename
                for item in sftp.listdir_attr(self.active_root)
                if stat.S_ISREG(item.st_mode) and item.filename.lower().endswith(".zip")
            }
            current_disabled = {
                item.filename
                for item in sftp.listdir_attr(self.disabled_root)
                if stat.S_ISREG(item.st_mode) and item.filename.lower().endswith(".zip")
            }
            saved_active = {item.name for item in summary.active_mods}
            saved_disabled = {item.name for item in summary.disabled_mods}
            backed = {PurePosixPath(item).name for item in summary.backed_up_files}
            available = current_active | current_disabled | backed
            return BackupPreview(
                backup=summary,
                config_changes=changes,
                activate_mods=sorted(saved_active - current_active, key=str.casefold),
                disable_mods=sorted(current_active - saved_active, key=str.casefold),
                unavailable_mods=sorted((saved_active | saved_disabled) - available, key=str.casefold),
            )

        return await self.ssh.run_sftp(preview_sync)

    async def delete(self, backup_id: str) -> None:
        target = self._path(backup_id)

        def delete_sync(sftp: Any) -> None:
            self._read_summary(sftp, backup_id)
            self._remove_tree(sftp, target)

        await self.ssh.run_sftp(delete_sync)

    async def restore(
        self,
        backup_id: str,
        config_service: ServerConfigService,
        inventory_service: InventoryService,
    ) -> BackupRestoreResponse:
        preview = await self.preview(backup_id)
        safety = await self.create(
            name=f"Avant restauration {backup_id}",
            reason="automatic-before-restore",
        )
        backup_config = await self.ssh.read_file(
            self._path(backup_id, "ServerConfig.toml"), max_bytes=2_000_000
        )
        await config_service.restore_payload(backup_config)

        moved = 0
        inventory = await inventory_service.scan()
        saved_active = {item.name for item in preview.backup.active_mods}
        by_name = {item.file_name: item for item in inventory}
        for item in inventory:
            should_be_active = item.file_name in saved_active
            if item.active != should_be_active:
                source_root = self.active_root if item.active else self.disabled_root
                target_root = self.active_root if should_be_active else self.disabled_root
                await self.ssh.move_file(
                    _safe_join(source_root, item.file_name),
                    _safe_join(target_root, item.file_name),
                )
                moved += 1

        missing = {
            item.name for item in preview.backup.active_mods + preview.backup.disabled_mods
        } - set(by_name)
        backed_by_name = {
            PurePosixPath(item).name: item for item in preview.backup.backed_up_files
        }
        for filename in missing:
            relative = backed_by_name.get(filename)
            if relative is None:
                continue
            destination_root = self.active_root if filename in saved_active else self.disabled_root
            source = self._path(backup_id, f"files/{relative}")
            destination = _safe_join(destination_root, filename)
            await self.ssh.run_sftp(
                lambda sftp, src=source, dst=destination: self._copy(sftp, src, dst)
            )
            moved += 1
        await inventory_service.scan()
        return BackupRestoreResponse(
            success=True,
            message="Sauvegarde restauree; un redemarrage BeamMP est recommande",
            safety_backup_id=safety.id,
            restored_config=True,
            moved_mods=moved,
        )
