from __future__ import annotations

import asyncio
import secrets
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from beam_manager.backups import BackupService
from beam_manager.inventory import (
    IMAGE_EXTENSIONS,
    InventoryService,
    _friendly_name,
    _metadata_value,
    _safe_join,
)
from beam_manager.models import ModItem, UploadAnalysis, UploadJob
from beam_manager.ssh import SSHClient, SSHError


class UploadCancelled(RuntimeError):
    pass


class UploadService:
    _analyses: dict[str, UploadAnalysis] = {}
    _paths: dict[str, Path] = {}
    _jobs: dict[str, UploadJob] = {}
    _cancelled: set[str] = set()

    @classmethod
    def has_active_job(cls) -> bool:
        return any(job.state in {"queued", "uploading"} for job in cls._jobs.values())

    def __init__(
        self,
        ssh: SSHClient,
        inventory: InventoryService,
        backups: BackupService,
        data_dir: Path,
        active_root: str,
        max_bytes: int,
    ) -> None:
        self.ssh = ssh
        self.inventory = inventory
        self.backups = backups
        self.temp_dir = data_dir / "uploads"
        self.active_root = active_root.rstrip("/")
        self.max_bytes = max_bytes

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        value = (filename or "").strip()
        if (
            not value
            or PurePosixPath(value.replace("\\", "/")).name != value
            or any(ord(character) < 32 for character in value)
            or len(value) > 220
            or not value.casefold().endswith(".zip")
        ):
            raise ValueError("Seuls les fichiers ZIP avec un nom valide sont acceptes")
        return value

    @staticmethod
    def _validate_members(archive: zipfile.ZipFile, max_bytes: int) -> list[zipfile.ZipInfo]:
        infos: list[zipfile.ZipInfo] = []
        total_uncompressed = 0
        for info in archive.infolist():
            normalized = info.filename.replace("\\", "/")
            path = PurePosixPath(normalized)
            if (
                path.is_absolute()
                or ".." in path.parts
                or normalized.startswith("/")
                or "\x00" in normalized
            ):
                raise ValueError("Le ZIP contient un chemin interne interdit")
            if info.file_size > max(max_bytes * 4, 4 * 1024**3):
                raise ValueError("Le ZIP contient un fichier anormalement volumineux")
            total_uncompressed += info.file_size
            if total_uncompressed > max(max_bytes * 10, 10 * 1024**3):
                raise ValueError("Le contenu decompresse annonce est trop volumineux")
            if not info.is_dir():
                infos.append(info)
        if not infos:
            raise ValueError("Le ZIP est vide")
        return infos

    def _extract_preview(
        self,
        archive: zipfile.ZipFile,
        infos: list[zipfile.ZipInfo],
        token: str,
        mod_type: str,
        internal_name: str | None,
    ) -> str | None:
        candidates = [
            info
            for info in infos
            if PurePosixPath(info.filename).suffix.casefold() in IMAGE_EXTENSIONS
            and 0 < info.file_size <= 12_000_000
        ]
        candidates.sort(
            key=lambda info: self.inventory._preview_score(
                info.filename, mod_type, internal_name
            ),
            reverse=True,
        )
        output = self.temp_dir / f"{token}.webp"
        for info in candidates[:12]:
            try:
                with archive.open(info) as source, Image.open(source) as image:
                    image.load()
                    if image.width * image.height > 40_000_000:
                        continue
                    image.thumbnail((960, 540), Image.Resampling.LANCZOS)
                    if image.mode not in {"RGB", "RGBA"}:
                        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    image.save(output, "WEBP", quality=84, method=4)
                return f"/api/uploads/{token}/preview"
            except (UnidentifiedImageError, OSError, ValueError):
                continue
        output.unlink(missing_ok=True)
        return None

    async def analyze(self, upload: UploadFile) -> UploadAnalysis:
        filename = self._safe_filename(upload.filename)
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        path = self.temp_dir / f"{token}.zip"
        total = 0
        try:
            with path.open("xb") as target:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ValueError("Le fichier depasse la limite d'upload configuree")
                    target.write(chunk)
            if total == 0:
                raise ValueError("Le fichier ZIP est vide")

            with zipfile.ZipFile(path) as archive:
                infos = self._validate_members(archive, self.max_bytes)
                names = [info.filename for info in infos]
                mod_type, internal_name, map_path = self.inventory._classify(names)
                metadata = self.inventory._read_metadata(
                    archive, names, mod_type, internal_name
                )
                fallback = _friendly_name(Path(filename).stem)
                display_name = _metadata_value(
                    metadata, "name", "title", "displayName", "display_name"
                ) or (_friendly_name(internal_name) if internal_name else fallback)
                author = _metadata_value(metadata, "author", "authors", "creator")
                preview_url = self._extract_preview(
                    archive, infos, token, mod_type, internal_name
                )

            existing = next(
                (
                    item
                    for item in await self.inventory.scan()
                    if item.file_name.casefold() == filename.casefold()
                ),
                None,
            )
            result = UploadAnalysis(
                token=token,
                file_name=filename,
                display_name=display_name,
                type=mod_type,
                size=total,
                internal_name=internal_name,
                map_path=map_path,
                author=author,
                preview_url=preview_url,
                duplicate=existing is not None,
                existing_size=existing.size if existing else None,
                existing_modified_at=existing.modified_at if existing else None,
            )
            self._analyses[token] = result
            self._paths[token] = path
            return result
        except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            path.unlink(missing_ok=True)
            raise ValueError("Le fichier n'est pas un ZIP valide") from exc
        except Exception:
            path.unlink(missing_ok=True)
            (self.temp_dir / f"{token}.webp").unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

    def preview_path(self, token: str) -> Path | None:
        if token not in self._analyses:
            return None
        path = (self.temp_dir / f"{token}.webp").resolve()
        if path.parent != self.temp_dir.resolve() or not path.is_file():
            return None
        return path

    async def start(self, token: str, replace: bool) -> UploadJob:
        analysis = self._analyses.get(token)
        local_path = self._paths.get(token)
        if analysis is None or local_path is None or not local_path.is_file():
            raise ValueError("Analyse d'upload expiree ou introuvable")
        inventory = await self.inventory.scan()
        existing = next(
            (item for item in inventory if item.file_name.casefold() == analysis.file_name.casefold()),
            None,
        )
        if existing and not replace:
            raise FileExistsError("Ce mod existe deja; confirmez explicitement son remplacement")
        if existing:
            await self.backups.create(
                name=f"Avant remplacement {existing.file_name}",
                reason="automatic-before-replace",
                include_files=[existing.remote_path],
            )
        job_id = secrets.token_urlsafe(18)
        job = UploadJob(
            id=job_id,
            file_name=analysis.file_name,
            state="queued",
            total=analysis.size,
        )
        self._jobs[job_id] = job
        asyncio.create_task(self._run(job_id, token, existing))
        return job

    async def _run(self, job_id: str, token: str, existing: ModItem | None) -> None:
        job = self._jobs[job_id]
        analysis = self._analyses[token]
        local_path = self._paths[token]
        # New content is installed into the disabled library first. Replacing an existing
        # item preserves its current distribution state so a live selected map is not
        # accidentally unpublished. BeamMP sends every ZIP in Resources/Client to clients.
        target_active = bool(existing is not None and existing.active)
        target_root = self.inventory.active_root if target_active else self.inventory.disabled_root
        destination = _safe_join(target_root, analysis.file_name)
        temporary = f"{destination}.beam-manager-upload-{job_id}"
        started = time.monotonic()

        def upload_sync(sftp: Any) -> None:
            transferred = 0
            try:
                with local_path.open("rb") as source, sftp.open(temporary, "wb") as target:
                    while chunk := source.read(1024 * 1024):
                        if job_id in self._cancelled:
                            raise UploadCancelled
                        target.write(chunk)
                        transferred += len(chunk)
                        elapsed = max(time.monotonic() - started, 0.001)
                        job.transferred = transferred
                        job.speed_bps = transferred / elapsed
                    target.flush()
                if existing is not None:
                    current = _safe_join(
                        self.inventory.active_root if existing.active else self.inventory.disabled_root,
                        existing.file_name,
                    )
                    try:
                        sftp.remove(current)
                    except FileNotFoundError:
                        pass
                sftp.rename(temporary, destination)
            except Exception:
                try:
                    sftp.remove(temporary)
                except OSError:
                    pass
                raise

        try:
            job.state = "uploading"
            await self.ssh.run_sftp(upload_sync)
            refreshed = await self.inventory.scan()
            item = next(
                (
                    candidate
                    for candidate in refreshed
                    if candidate.active == target_active
                    and candidate.file_name.casefold() == analysis.file_name.casefold()
                ),
                None,
            )
            if item is None:
                raise SSHError("L'upload distant n'a pas pu etre confirme")
            job.transferred = job.total
            job.item = item
            job.state = "success"
            job.message = "Contenu installe mais non distribue; activez-le ou selectionnez-le avant de redemarrer BeamMP"
        except UploadCancelled:
            job.state = "cancelled"
            job.message = "Upload annule"
        except Exception as exc:
            job.state = "error"
            job.message = str(exc)
        finally:
            local_path.unlink(missing_ok=True)
            (self.temp_dir / f"{token}.webp").unlink(missing_ok=True)
            self._paths.pop(token, None)
            self._analyses.pop(token, None)
            self._cancelled.discard(job_id)

    def job(self, job_id: str) -> UploadJob | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> UploadJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError("Upload introuvable")
        if job.state in {"queued", "uploading"}:
            self._cancelled.add(job_id)
        return job

    def discard(self, token: str) -> None:
        path = self._paths.pop(token, None)
        if path:
            path.unlink(missing_ok=True)
        (self.temp_dir / f"{token}.webp").unlink(missing_ok=True)
        self._analyses.pop(token, None)
