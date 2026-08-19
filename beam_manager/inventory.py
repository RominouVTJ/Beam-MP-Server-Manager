from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError

from beam_manager.models import ModItem
from beam_manager.ssh import RemoteFile, SSHClient, SSHError


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
PREVIEW_WORDS = ("preview", "thumbnail", "default", "main", "cover", "screenshot")
OTHER_ROOTS = ("scripts/", "lua/", "gameplay/", "ui/", "sounds/", "art/")


def _safe_join(root: str, file_name: str) -> str:
    if (
        not file_name
        or file_name in {".", ".."}
        or PurePosixPath(file_name).name != file_name
        or "/" in file_name
        or "\\" in file_name
        or "\x00" in file_name
    ):
        raise SSHError("Nom de mod distant invalide")
    candidate = f"{root.rstrip('/')}/{file_name}"
    if not candidate.startswith(f"{root.rstrip('/')}/"):
        raise SSHError("Chemin de mod interdit")
    return candidate


def _friendly_name(value: str) -> str:
    cleaned = re.sub(r"[_\-.]+", " ", value).strip()
    return " ".join(part.capitalize() for part in cleaned.split()) or value


def _metadata_value(data: dict[str, Any], *keys: str) -> str | None:
    lowered = {str(key).casefold(): value for key, value in data.items()}
    for key in keys:
        value = lowered.get(key.casefold())
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            values = [str(item).strip() for item in value if str(item).strip()]
            if values:
                return ", ".join(values)
        if isinstance(value, dict):
            nested = _metadata_value(value, "name", "author", "value")
            if nested:
                return nested
    return None


class InventoryService:
    def __init__(
        self,
        ssh: SSHClient,
        active_root: str,
        disabled_root: str,
        data_dir: Path,
    ) -> None:
        self.ssh = ssh
        self.active_root = active_root.rstrip("/")
        self.disabled_root = disabled_root.rstrip("/")
        self.cache_path = data_dir / "inventory-cache.json"
        self.thumbnail_dir = data_dir / "thumbnails"
        self._lock = asyncio.Lock()

    async def ensure_directories(self) -> bool:
        return await self.ssh.ensure_directory(self.disabled_root, mode=0o2775)

    def _load_cache(self) -> dict[str, Any]:
        if not self.cache_path.is_file():
            return {"version": 1, "entries": {}}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if payload.get("version") == 1 and isinstance(payload.get("entries"), dict):
                return payload
        except (OSError, ValueError):
            pass
        return {"version": 1, "entries": {}}

    def _save_cache(self, entries: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.cache_path)

    @staticmethod
    def _mod_id(active: bool, file_name: str) -> str:
        state = "active" if active else "disabled"
        return hashlib.sha256(f"{state}\0{file_name}".encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _classify(names: list[str]) -> tuple[str, str | None, str | None]:
        normalized = [name.replace("\\", "/").lstrip("/") for name in names]
        map_info = next(
            (
                match
                for name in normalized
                if (match := re.fullmatch(r"levels/([^/]+)/info\.json", name, re.I))
            ),
            None,
        )
        map_level = next(
            (
                match
                for name in normalized
                if (
                    match := re.match(
                        r"levels/([^/]+)/.+\.(?:ter|mis|level\.json)$", name, re.I
                    )
                )
            ),
            None,
        )
        vehicle_file = next(
            (
                match
                for name in normalized
                if (match := re.match(r"vehicles/([^/]+)/.+\.jbeam$", name, re.I))
            ),
            None,
        )

        has_map = map_info is not None or map_level is not None
        has_vehicle = vehicle_file is not None
        if has_map and has_vehicle:
            return "unknown", None, None
        if has_map:
            internal = (map_info or map_level).group(1)
            path = f"/levels/{internal}/info.json" if map_info else None
            return "map", internal, path
        if has_vehicle:
            return "vehicle", vehicle_file.group(1), None
        if any(name.casefold().startswith(OTHER_ROOTS) for name in normalized):
            return "other", None, None
        return "unknown", None, None

    @staticmethod
    def _read_metadata(
        archive: zipfile.ZipFile,
        names: list[str],
        mod_type: str,
        internal_name: str | None,
    ) -> dict[str, Any]:
        normalized = {name.replace("\\", "/").lstrip("/").casefold(): name for name in names}
        candidates: list[str] = []
        if internal_name and mod_type == "map":
            candidates.append(f"levels/{internal_name}/info.json".casefold())
        if internal_name and mod_type == "vehicle":
            candidates.append(f"vehicles/{internal_name}/info.json".casefold())
        candidates.extend(("info.json", "mod_info.json", "modinfo.json"))
        for candidate in candidates:
            real_name = normalized.get(candidate)
            if not real_name:
                continue
            try:
                info = archive.getinfo(real_name)
                if info.file_size > 1_000_000:
                    continue
                with archive.open(info) as handle:
                    raw = handle.read(1_000_001)
                if len(raw) > 1_000_000:
                    continue
                parsed = json.loads(raw.decode("utf-8-sig"))
                if isinstance(parsed, dict):
                    return parsed
            except (KeyError, UnicodeError, ValueError, OSError):
                continue
        return {}

    @staticmethod
    def _preview_score(name: str, mod_type: str, internal_name: str | None) -> int:
        normalized = name.replace("\\", "/").casefold()
        base = PurePosixPath(normalized).stem
        score = 0
        for index, word in enumerate(PREVIEW_WORDS):
            if word in base:
                score += 100 - index * 8
        if internal_name:
            prefix = "levels" if mod_type == "map" else "vehicles"
            if normalized.startswith(f"{prefix}/{internal_name.casefold()}/"):
                score += 35
        if "screenshot" in normalized or "preview" in normalized:
            score += 20
        if any(word in normalized for word in ("icon", "material", "decal", "texture")):
            score -= 30
        return score

    def _extract_thumbnail(
        self,
        archive: zipfile.ZipFile,
        infos: list[zipfile.ZipInfo],
        mod_id: str,
        mod_type: str,
        internal_name: str | None,
    ) -> str | None:
        candidates = [
            info
            for info in infos
            if PurePosixPath(info.filename).suffix.casefold() in IMAGE_EXTENSIONS
            and 0 < info.file_size <= 20_000_000
        ]
        candidates.sort(
            key=lambda info: self._preview_score(info.filename, mod_type, internal_name),
            reverse=True,
        )
        self.thumbnail_dir.mkdir(parents=True, exist_ok=True)
        output = self.thumbnail_dir / f"{mod_id}.webp"
        for info in candidates[:8]:
            try:
                with archive.open(info) as handle:
                    raw = handle.read(20_000_001)
                if len(raw) > 20_000_000:
                    continue
                with Image.open(io.BytesIO(raw)) as source:
                    source.load()
                    if source.width < 160 or source.height < 90:
                        continue
                    image = source.convert("RGB")
                    image.thumbnail((720, 405), Image.Resampling.LANCZOS)
                    image.save(output, "WEBP", quality=84, method=4)
                return f"/api/thumbnails/{mod_id}"
            except (UnidentifiedImageError, OSError, ValueError):
                continue
        output.unlink(missing_ok=True)
        return None

    def _inspect_zip(
        self,
        sftp: Any,
        remote_path: str,
        remote_file: RemoteFile,
        active: bool,
    ) -> ModItem:
        mod_id = self._mod_id(active, remote_file.name)
        fallback = _friendly_name(Path(remote_file.name).stem)
        try:
            with sftp.open(remote_path, "rb") as remote_handle:
                with zipfile.ZipFile(remote_handle) as archive:
                    infos = [info for info in archive.infolist() if not info.is_dir()]
                    names = [info.filename for info in infos]
                    mod_type, internal_name, map_path = self._classify(names)
                    metadata = self._read_metadata(archive, names, mod_type, internal_name)
                    display_name = _metadata_value(
                        metadata, "name", "title", "displayName", "display_name"
                    ) or (_friendly_name(internal_name) if internal_name else fallback)
                    brand = _metadata_value(metadata, "brand", "make")
                    model = _metadata_value(metadata, "model")
                    author = _metadata_value(
                        metadata, "author", "authors", "creator", "creators"
                    )
                    thumbnail_url = self._extract_thumbnail(
                        archive, infos, mod_id, mod_type, internal_name
                    )
        except (zipfile.BadZipFile, OSError, SSHError):
            mod_type = "unknown"
            internal_name = None
            map_path = None
            display_name = fallback
            brand = model = author = thumbnail_url = None

        return ModItem(
            id=mod_id,
            file_name=remote_file.name,
            display_name=display_name,
            internal_name=internal_name,
            type=mod_type,
            size=remote_file.size,
            modified_at=remote_file.modified_at,
            active=active,
            remote_path=remote_path,
            map_path=map_path,
            brand=brand,
            model=model,
            author=author,
            thumbnail_url=thumbnail_url,
        )

    async def scan(self) -> list[ModItem]:
        async with self._lock:
            await self.ensure_directories()
            active_files, disabled_files = await asyncio.gather(
                self.ssh.list_zip_files(self.active_root),
                self.ssh.list_zip_files(self.disabled_root),
            )
            cache = self._load_cache().get("entries", {})
            next_cache: dict[str, Any] = {}
            work = [(True, item) for item in active_files] + [
                (False, item) for item in disabled_files
            ]

            def inspect_all(sftp: Any) -> list[ModItem]:
                inventory: list[ModItem] = []
                for active, remote_file in work:
                    mod_id = self._mod_id(active, remote_file.name)
                    signature = f"{remote_file.size}:{remote_file.modified_at}"
                    cached = cache.get(mod_id)
                    if cached and cached.get("signature") == signature:
                        try:
                            item = ModItem.model_validate(cached["item"])
                            if item.thumbnail_url:
                                thumbnail = self.thumbnail_dir / f"{mod_id}.webp"
                                if not thumbnail.is_file():
                                    raise ValueError("miniature absente")
                            inventory.append(item)
                            next_cache[mod_id] = cached
                            continue
                        except (KeyError, ValueError):
                            pass
                    root = self.active_root if active else self.disabled_root
                    remote_path = _safe_join(root, remote_file.name)
                    item = self._inspect_zip(sftp, remote_path, remote_file, active)
                    inventory.append(item)
                    next_cache[mod_id] = {
                        "signature": signature,
                        "item": item.model_dump(mode="json"),
                    }
                return inventory

            inventory = await self.ssh.run_sftp(inspect_all) if work else []
            self._save_cache(next_cache)
            live_ids = set(next_cache)
            if self.thumbnail_dir.is_dir():
                for thumbnail in self.thumbnail_dir.glob("*.webp"):
                    if thumbnail.stem not in live_ids:
                        thumbnail.unlink(missing_ok=True)
            return sorted(inventory, key=lambda item: item.display_name.casefold())

    async def set_enabled(self, mod_id: str, enabled: bool) -> ModItem:
        inventory = await self.scan()
        item = next((candidate for candidate in inventory if candidate.id == mod_id), None)
        if item is None:
            raise SSHError("Mod introuvable; actualisez l'inventaire")
        if item.active == enabled:
            return item

        source_root = self.active_root if item.active else self.disabled_root
        destination_root = self.active_root if enabled else self.disabled_root
        source = _safe_join(source_root, item.file_name)
        destination = _safe_join(destination_root, item.file_name)
        if source != item.remote_path:
            raise SSHError("L'inventaire du mod n'est plus coherent")
        await self.ssh.move_file(source, destination)
        refreshed = await self.scan()
        moved = next(
            (
                candidate
                for candidate in refreshed
                if candidate.file_name == item.file_name and candidate.active == enabled
            ),
            None,
        )
        if moved is None:
            raise SSHError("Le deplacement du mod n'a pas pu etre confirme")
        return moved

    async def find(self, mod_id: str) -> ModItem:
        item = next((candidate for candidate in await self.scan() if candidate.id == mod_id), None)
        if item is None:
            raise SSHError("Mod introuvable; actualisez l'inventaire")
        return item

    async def set_enabled_batch(self, mod_ids: list[str], enabled: bool) -> list[ModItem]:
        inventory = await self.scan()
        by_id = {item.id: item for item in inventory}
        if missing := [mod_id for mod_id in mod_ids if mod_id not in by_id]:
            raise SSHError(f"{len(missing)} mod(s) introuvable(s); actualisez l'inventaire")
        targets = [by_id[mod_id] for mod_id in mod_ids if by_id[mod_id].active != enabled]
        moved: list[tuple[str, str]] = []
        try:
            for item in targets:
                source_root = self.active_root if item.active else self.disabled_root
                destination_root = self.active_root if enabled else self.disabled_root
                source = _safe_join(source_root, item.file_name)
                destination = _safe_join(destination_root, item.file_name)
                await self.ssh.move_file(source, destination)
                moved.append((destination, source))
        except Exception:
            for source, destination in reversed(moved):
                try:
                    await self.ssh.move_file(source, destination)
                except Exception:
                    pass
            raise
        refreshed = await self.scan()
        names = {item.file_name for item in targets}
        return [item for item in refreshed if item.file_name in names and item.active == enabled]

    async def delete(self, mod_id: str) -> ModItem:
        item = await self.find(mod_id)
        root = self.active_root if item.active else self.disabled_root
        expected = _safe_join(root, item.file_name)
        if expected != item.remote_path:
            raise SSHError("L'inventaire du mod n'est plus coherent")

        def remove_sync(sftp: Any) -> None:
            attributes = sftp.stat(expected)
            if not stat.S_ISREG(attributes.st_mode):
                raise SSHError("La cible n'est pas un fichier")
            sftp.remove(expected)
            try:
                sftp.stat(expected)
            except FileNotFoundError:
                return
            raise SSHError("La suppression du mod n'a pas pu etre confirmee")

        await self.ssh.run_sftp(remove_sync)
        await self.scan()
        return item

    def thumbnail_path(self, mod_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{20}", mod_id):
            return None
        candidate = (self.thumbnail_dir / f"{mod_id}.webp").resolve()
        if candidate.parent != self.thumbnail_dir.resolve() or not candidate.is_file():
            return None
        return candidate
