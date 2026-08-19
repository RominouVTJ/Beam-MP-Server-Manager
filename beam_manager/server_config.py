from __future__ import annotations

from typing import Any

import tomlkit
from tomlkit.container import Container

from beam_manager.backend import ServerBackend
from beam_manager.models import ServerConfigPatch, ServerConfigPublic
from beam_manager.ssh import SSHError


PUBLIC_KEYS = (
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
    "Debug",
    "IP",
    "InformationPacket",
    "ResourceFolder",
)


class ServerConfigService:
    """Read and safely update the public portion of ServerConfig.toml."""

    def __init__(self, ssh: ServerBackend, remote_path: str) -> None:
        self.ssh = ssh
        self.remote_path = remote_path

    async def _read_document(self) -> tomlkit.TOMLDocument:
        try:
            payload = await self.ssh.read_file(self.remote_path)
            return tomlkit.parse(payload.decode("utf-8-sig"))
        except SSHError:
            raise
        except Exception as exc:
            raise SSHError("ServerConfig.toml est illisible ou invalide") from exc

    @staticmethod
    def _general(document: tomlkit.TOMLDocument) -> Container:
        section = document.get("General")
        if section is not None and hasattr(section, "get"):
            return section
        return document

    @staticmethod
    def _value(section: Container, key: str, default: Any) -> Any:
        value = section.get(key, default)
        return value.unwrap() if hasattr(value, "unwrap") else value

    def _public(self, document: tomlkit.TOMLDocument) -> ServerConfigPublic:
        section = self._general(document)
        values = {
            "Name": str(self._value(section, "Name", "BeamMP Server")),
            "Description": str(self._value(section, "Description", "")),
            "Private": bool(self._value(section, "Private", False)),
            "MaxPlayers": int(self._value(section, "MaxPlayers", 8)),
            "MaxCars": int(self._value(section, "MaxCars", 1)),
            "Map": str(self._value(section, "Map", "")),
            "Tags": str(self._value(section, "Tags", "")),
            "LogChat": bool(self._value(section, "LogChat", True)),
            "AllowGuests": bool(self._value(section, "AllowGuests", True)),
            "Port": int(self._value(section, "Port", 30814)),
            "Debug": bool(self._value(section, "Debug", False)),
            "IP": str(self._value(section, "IP", "::")),
            "InformationPacket": bool(self._value(section, "InformationPacket", True)),
            "ResourceFolder": str(self._value(section, "ResourceFolder", "Resources")),
        }
        return ServerConfigPublic.model_validate(values)

    async def read_public(self) -> ServerConfigPublic:
        return self._public(await self._read_document())

    async def authkey_configured(self) -> bool:
        document = await self._read_document()
        value = self._value(self._general(document), "AuthKey", "")
        return bool(str(value).strip())

    async def set_authkey(self, authkey: str) -> None:
        secret = authkey.strip()
        if len(secret) < 8 or len(secret) > 512:
            raise ValueError("AuthKey BeamMP invalide")
        if any(ord(character) < 32 or ord(character) == 127 for character in secret):
            raise ValueError("AuthKey BeamMP invalide")
        document = await self._read_document()
        section = self._general(document)
        section["AuthKey"] = secret
        await self.ssh.write_file_atomic(self.remote_path, tomlkit.dumps(document).encode("utf-8"))

    async def configure_initial(
        self,
        *,
        server_name: str,
        authkey: str,
        max_players: int,
        max_cars: int,
        private: bool,
        map_path: str,
    ) -> ServerConfigPublic:
        secret = authkey.strip()
        if len(secret) < 8:
            raise ValueError("AuthKey BeamMP invalide")
        document = await self._read_document()
        section = self._general(document)
        section["Name"] = server_name
        section["AuthKey"] = secret
        section["MaxPlayers"] = max_players
        section["MaxCars"] = max_cars
        section["Private"] = private
        section["Map"] = map_path
        await self.ssh.write_file_atomic(
            self.remote_path,
            tomlkit.dumps(document).encode("utf-8"),
        )
        return self._public(document)

    async def patch_public(self, patch: ServerConfigPatch) -> ServerConfigPublic:
        document = await self._read_document()
        section = self._general(document)
        values = patch.model_dump(exclude_none=True, by_alias=True)
        for key, value in values.items():
            if key not in PUBLIC_KEYS:
                continue
            section[key] = value
        await self.ssh.write_file_atomic(
            self.remote_path,
            tomlkit.dumps(document).encode("utf-8"),
        )
        return self._public(document)

    async def update_public(self, patch: ServerConfigPatch) -> ServerConfigPublic:
        """Backward-compatible public update API used by the Web routes and tests."""
        return await self.patch_public(patch)

    async def select_map(self, map_path: str) -> ServerConfigPublic:
        """Change only the selected map while preserving private configuration values."""
        document = await self._read_document()
        section = self._general(document)
        section["Map"] = map_path
        await self.ssh.write_file_atomic(
            self.remote_path,
            tomlkit.dumps(document).encode("utf-8"),
        )
        return self._public(document)
