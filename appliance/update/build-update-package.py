from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beam_manager.appliance_update_package import PRODUCT_NAME, validate_update_package
from beam_manager.appliance_update_payload import stage_update_payload


INCLUDED_ROOTS = ("beam_manager", "appliance", "beammp_plugin", "docs")
INCLUDED_FILES = ("pyproject.toml", "README.md", "CHANGELOG.md", "LICENSE")
EXCLUDED_PARTS = {
    ".git",
    ".github",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".pytest-tmp",
    "release",
    "build",
    "dist",
    "data",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def project_version() -> str:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise RuntimeError("pyproject.toml has no valid project version")
    return version.strip()


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for name in INCLUDED_FILES:
        path = ROOT / name
        if path.is_file():
            files.append(path)
    for root_name in INCLUDED_ROOTS:
        base = ROOT / root_name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED_PARTS for part in relative.parts):
                continue
            if path.suffix.casefold() in EXCLUDED_SUFFIXES:
                continue
            files.append(path)
    return sorted(set(files), key=lambda item: item.relative_to(ROOT).as_posix())


def _payload_bytes() -> bytes:
    raw_tar = io.BytesIO()
    with tarfile.open(fileobj=raw_tar, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for source in _iter_source_files():
            relative = source.relative_to(ROOT).as_posix()
            payload = source.read_bytes()
            info = tarfile.TarInfo(relative)
            info.size = len(payload)
            info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    compressed = io.BytesIO()
    with gzip.GzipFile(fileobj=compressed, mode="wb", filename="", mtime=0) as output:
        output.write(raw_tar.getvalue())
    return compressed.getvalue()


def build_update_package(output_dir: Path, *, min_version: str) -> Path:
    target_version = project_version()
    payload = _payload_bytes()
    manifest = {
        "schema_version": 1,
        "product": PRODUCT_NAME,
        "version": target_version,
        "min_version": min_version,
        "payload": "payload.tar.gz",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size": len(payload),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"Beam-MP-Server-Manager-v{target_version}.update.zip"
    if output.exists():
        output.unlink()

    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fixed_time = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        manifest_info = zipfile.ZipInfo("manifest.json", date_time=fixed_time)
        manifest_info.compress_type = zipfile.ZIP_DEFLATED
        manifest_info.external_attr = 0o644 << 16
        archive.writestr(manifest_info, manifest_bytes)
        payload_info = zipfile.ZipInfo("payload.tar.gz", date_time=fixed_time)
        payload_info.compress_type = zipfile.ZIP_DEFLATED
        payload_info.external_attr = 0o644 << 16
        archive.writestr(payload_info, payload)

    validated = validate_update_package(output)
    validated.assert_installable_from(min_version)
    with tempfile.TemporaryDirectory(prefix="beam-release-update-stage-") as temp:
        staged = stage_update_payload(
            validated,
            Path(temp) / "staged",
            current_version=min_version,
        )
        if staged.version != target_version:
            raise RuntimeError("Built package staged with an unexpected version")
    return output


def sha256_line(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return f"{digest.hexdigest()}  {path.name}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a publishable Beam-MP-Server-Manager appliance update package")
    parser.add_argument("--min-version", required=True, help="Oldest installed Manager version accepted by this package")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "release")
    args = parser.parse_args()
    output = build_update_package(args.output_dir.resolve(), min_version=args.min_version)
    checksum = output.with_suffix(output.suffix + ".sha256.txt")
    checksum.write_text(sha256_line(output) + "\n", encoding="ascii")
    print(f"UPDATE_PACKAGE={output}")
    print(f"SHA256={checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
