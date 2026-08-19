from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version


PRODUCT_NAME = "Beam-MP-Server-Manager"
SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
PAYLOAD_NAME = "payload.tar.gz"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class UpdatePackageError(ValueError):
    """Raised when an appliance update package is malformed or untrusted."""


@dataclass(frozen=True)
class UpdateManifest:
    schema_version: int
    product: str
    version: str
    min_version: str
    payload: str
    payload_sha256: str
    payload_size: int

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> "UpdateManifest":
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UpdatePackageError("Update manifest is not valid UTF-8 JSON") from exc
        if not isinstance(data, dict):
            raise UpdatePackageError("Update manifest must be a JSON object")

        expected = {
            "schema_version",
            "product",
            "version",
            "min_version",
            "payload",
            "payload_sha256",
            "payload_size",
        }
        if set(data) != expected:
            raise UpdatePackageError("Update manifest fields do not match schema v1")

        manifest = cls(
            schema_version=data["schema_version"],
            product=data["product"],
            version=data["version"],
            min_version=data["min_version"],
            payload=data["payload"],
            payload_sha256=data["payload_sha256"],
            payload_size=data["payload_size"],
        )
        manifest.validate()
        return manifest

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise UpdatePackageError("Unsupported update manifest schema")
        if self.product != PRODUCT_NAME:
            raise UpdatePackageError("Update package targets another product")
        if self.payload != PAYLOAD_NAME:
            raise UpdatePackageError("Unexpected update payload name")
        if not isinstance(self.payload_size, int) or isinstance(self.payload_size, bool) or self.payload_size <= 0:
            raise UpdatePackageError("Update payload size must be a positive integer")
        if not isinstance(self.payload_sha256, str) or not _SHA256.fullmatch(self.payload_sha256):
            raise UpdatePackageError("Update payload SHA-256 is invalid")
        try:
            Version(self.version)
            Version(self.min_version)
        except InvalidVersion as exc:
            raise UpdatePackageError("Update package contains an invalid version") from exc


@dataclass(frozen=True)
class ValidatedUpdatePackage:
    path: Path
    manifest: UpdateManifest

    def assert_installable_from(self, current_version: str) -> None:
        try:
            current = Version(current_version)
        except InvalidVersion as exc:
            raise UpdatePackageError("Installed appliance version is invalid") from exc
        target = Version(self.manifest.version)
        minimum = Version(self.manifest.min_version)
        if current < minimum:
            raise UpdatePackageError(
                f"Update requires at least version {self.manifest.min_version}"
            )
        if target <= current:
            raise UpdatePackageError("Update target must be newer than installed version")


def validate_update_package(path: Path) -> ValidatedUpdatePackage:
    path = Path(path)
    if not path.is_file():
        raise UpdatePackageError("Update package does not exist")

    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise UpdatePackageError("Update package contains duplicate entries")
            if set(names) != {MANIFEST_NAME, PAYLOAD_NAME}:
                raise UpdatePackageError("Update package must contain only manifest.json and payload.tar.gz")
            for info in archive.infolist():
                if info.is_dir() or "/" in info.filename or "\\" in info.filename:
                    raise UpdatePackageError("Update package contains an unsafe entry name")

            manifest = UpdateManifest.from_json_bytes(archive.read(MANIFEST_NAME))
            payload_info = archive.getinfo(PAYLOAD_NAME)
            if payload_info.file_size != manifest.payload_size:
                raise UpdatePackageError("Update payload size does not match manifest")

            digest = hashlib.sha256()
            with archive.open(PAYLOAD_NAME, "r") as payload:
                while chunk := payload.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != manifest.payload_sha256:
                raise UpdatePackageError("Update payload SHA-256 does not match manifest")
    except zipfile.BadZipFile as exc:
        raise UpdatePackageError("Update package is not a valid ZIP archive") from exc

    return ValidatedUpdatePackage(path=path, manifest=manifest)
