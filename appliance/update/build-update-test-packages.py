from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from beam_manager.appliance_update_package import PRODUCT_NAME, validate_update_package
from beam_manager.appliance_update_payload import stage_update_payload


HEALTHY_VERSION = "0.10.1.dev1"
ROLLBACK_VERSION = "0.10.1.dev2"
SOURCE_VERSION = "0.10.0"
INCLUDED_ROOTS = ("beam_manager", "appliance", "beammp_plugin")
INCLUDED_FILES = ("pyproject.toml", "README.md", "CHANGELOG.md")
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".pytest-tmp"}


def _patched_pyproject(raw: bytes, target_version: str) -> bytes:
    text = raw.decode("utf-8")
    pattern = re.compile(r'(?ms)(^\[project\]\s*.*?^version\s*=\s*")[^"]+("\s*$)')
    patched, count = pattern.subn(rf"\g<1>{target_version}\g<2>", text, count=1)
    if count != 1:
        raise RuntimeError("Could not patch [project] version in pyproject.toml")
    return patched.encode("utf-8")


def _patched_init(raw: bytes, target_version: str) -> bytes:
    text = raw.decode("utf-8")
    patched, count = re.subn(
        r'(?m)^__version__\s*=\s*"[^"]+"\s*$',
        f'__version__ = "{target_version}"',
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("Could not patch beam_manager.__version__")
    return patched.encode("utf-8")


def _forced_failure_cli(raw: bytes) -> bytes:
    text = raw.decode("utf-8")
    marker = 'raise RuntimeError("FORCED_APPLIANCE_UPDATE_ROLLBACK_TEST")\n'
    lines = text.splitlines(keepends=True)
    insert_at = 0
    if lines and lines[0].startswith("from __future__ import "):
        insert_at = 1
        while insert_at < len(lines) and lines[insert_at].startswith("from __future__ import "):
            insert_at += 1
    lines.insert(insert_at, marker)
    return "".join(lines).encode("utf-8")


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
            if path.suffix in {".pyc", ".pyo"}:
                continue
            files.append(path)
    return sorted(set(files), key=lambda item: item.as_posix())


def _payload_bytes(target_version: str, *, force_runtime_failure: bool) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
        for source in _iter_source_files():
            relative = source.relative_to(ROOT).as_posix()
            raw = source.read_bytes()
            if relative == "pyproject.toml":
                raw = _patched_pyproject(raw, target_version)
            elif relative == "beam_manager/__init__.py":
                raw = _patched_init(raw, target_version)
            elif force_runtime_failure and relative == "beam_manager/cli.py":
                raw = _forced_failure_cli(raw)

            info = tarfile.TarInfo(relative)
            info.size = len(raw)
            info.mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            archive.addfile(info, io.BytesIO(raw))
    return buffer.getvalue()


def _write_package(output: Path, target_version: str, *, force_runtime_failure: bool) -> Path:
    payload = _payload_bytes(target_version, force_runtime_failure=force_runtime_failure)
    manifest = {
        "schema_version": 1,
        "product": PRODUCT_NAME,
        "version": target_version,
        "min_version": SOURCE_VERSION,
        "payload": "payload.tar.gz",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_size": len(payload),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, separators=(",", ":")))
        archive.writestr("payload.tar.gz", payload)

    validated = validate_update_package(output)
    validated.assert_installable_from(SOURCE_VERSION)
    with tempfile.TemporaryDirectory(prefix="beam-update-stage-") as temp_dir:
        staged = stage_update_payload(validated, Path(temp_dir) / "staged", current_version=SOURCE_VERSION)
        if staged.version != target_version:
            raise RuntimeError("Synthetic package staging returned the wrong version")
    return output


def build_test_packages(output_dir: Path) -> tuple[Path, Path]:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if f'version = "{SOURCE_VERSION}"' not in pyproject:
        raise RuntimeError(
            f"Synthetic updater test packages are locked to source version {SOURCE_VERSION}; "
            "do not use this builder after the real version bump"
        )
    healthy = _write_package(
        output_dir / f"Beam-MP-Server-Manager-{HEALTHY_VERSION}-TEST.update.zip",
        HEALTHY_VERSION,
        force_runtime_failure=False,
    )
    rollback = _write_package(
        output_dir / f"Beam-MP-Server-Manager-{ROLLBACK_VERSION}-FORCED-ROLLBACK-TEST.update.zip",
        ROLLBACK_VERSION,
        force_runtime_failure=True,
    )
    return healthy, rollback


def main() -> int:
    parser = argparse.ArgumentParser(description="Build disposable updater runtime-test packages")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "release" / "update-test",
    )
    args = parser.parse_args()
    healthy, rollback = build_test_packages(args.output_dir.resolve())
    print(f"HEALTHY={healthy}")
    print(f"ROLLBACK={rollback}")
    print("TEST PACKAGES ONLY - DO NOT PUBLISH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
