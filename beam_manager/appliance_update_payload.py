from __future__ import annotations

import argparse
import json
import shutil
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from beam_manager.appliance_update_package import PAYLOAD_NAME, ValidatedUpdatePackage, validate_update_package


MAX_PAYLOAD_FILES = 10_000
MAX_EXPANDED_BYTES = 512 * 1024 * 1024


class UpdatePayloadError(ValueError):
    """Raised when an update payload is unsafe or does not match its manifest."""


@dataclass(frozen=True)
class StagedUpdatePayload:
    source_root: Path
    version: str
    file_count: int
    expanded_bytes: int


def _safe_member_path(name: str) -> PurePosixPath:
    normalized = name
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    first = path.parts[0] if path.parts else ""
    if (
        not normalized
        or "\\" in normalized
        or path.is_absolute()
        or ":" in first
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise UpdatePayloadError("Update payload contains an unsafe path")
    return path


def _payload_version(source_root: Path) -> str:
    pyproject = source_root / "pyproject.toml"
    manager_dir = source_root / "beam_manager"
    appliance_dir = source_root / "appliance"
    if not pyproject.is_file() or not manager_dir.is_dir() or not appliance_dir.is_dir():
        raise UpdatePayloadError("Update payload does not contain the canonical application layout")
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        version = data["project"]["version"]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise UpdatePayloadError("Update payload pyproject.toml is invalid") from exc
    if not isinstance(version, str) or not version.strip():
        raise UpdatePayloadError("Update payload version is invalid")
    return version.strip()


def stage_update_payload(
    package: ValidatedUpdatePackage,
    destination: Path,
    *,
    current_version: str,
) -> StagedUpdatePayload:
    package.assert_installable_from(current_version)
    destination = Path(destination)
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise UpdatePayloadError("Update staging destination must be an empty directory")
    else:
        destination.mkdir(parents=True, mode=0o700)

    seen: set[PurePosixPath] = set()
    file_count = 0
    expanded_bytes = 0

    try:
        with zipfile.ZipFile(package.path, "r") as container:
            with container.open(PAYLOAD_NAME, "r") as compressed_payload:
                with tarfile.open(fileobj=compressed_payload, mode="r:gz") as archive:
                    members = archive.getmembers()
                    if len(members) > MAX_PAYLOAD_FILES:
                        raise UpdatePayloadError("Update payload contains too many entries")

                    for member in members:
                        relative = _safe_member_path(member.name)
                        if relative in seen:
                            raise UpdatePayloadError("Update payload contains duplicate paths")
                        seen.add(relative)

                        if member.isdir():
                            (destination / Path(*relative.parts)).mkdir(parents=True, exist_ok=True, mode=0o755)
                            continue
                        if not member.isreg():
                            raise UpdatePayloadError("Update payload may contain only regular files and directories")
                        if member.size < 0:
                            raise UpdatePayloadError("Update payload contains an invalid file size")

                        file_count += 1
                        expanded_bytes += member.size
                        if file_count > MAX_PAYLOAD_FILES or expanded_bytes > MAX_EXPANDED_BYTES:
                            raise UpdatePayloadError("Update payload exceeds staging limits")

                        target = destination / Path(*relative.parts)
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                        source = archive.extractfile(member)
                        if source is None:
                            raise UpdatePayloadError("Update payload file could not be read")
                        with source, target.open("wb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        target.chmod(0o755 if member.mode & 0o111 else 0o644)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise UpdatePayloadError("Update payload could not be staged safely") from exc

    version = _payload_version(destination)
    if version != package.manifest.version:
        raise UpdatePayloadError("Update payload version does not match manifest")

    return StagedUpdatePayload(
        source_root=destination,
        version=version,
        file_count=file_count,
        expanded_bytes=expanded_bytes,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and safely stage a Beam-MP-Server-Manager update payload")
    parser.add_argument("package", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("current_version")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    package = validate_update_package(args.package)
    staged = stage_update_payload(package, args.destination, current_version=args.current_version)
    print(
        json.dumps(
            {
                "source_root": str(staged.source_root),
                "version": staged.version,
                "file_count": staged.file_count,
                "expanded_bytes": staged.expanded_bytes,
            },
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
