from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterator, Protocol, TypeVar

from beam_manager.ssh import CommandResult, RemoteFile, SSHClient, SSHError


T = TypeVar("T")


class ServerBackend(Protocol):
    """Transport contract shared by local Linux and remote SSH servers."""

    async def execute_service_command(self, action: str) -> CommandResult: ...
    async def list_remote_directory(self, remote_path: str) -> list[str]: ...
    async def disk_free_bytes(self, remote_path: str) -> int: ...
    async def beammp_version(self, beam_root: str) -> str | None: ...
    async def read_file(self, remote_path: str, max_bytes: int = 2_000_000) -> bytes: ...
    async def write_file_atomic(self, remote_path: str, payload: bytes) -> None: ...
    async def ensure_directory(self, remote_path: str, mode: int = 0o775) -> bool: ...
    async def list_zip_files(self, remote_path: str) -> list[RemoteFile]: ...
    async def move_file(self, source_path: str, destination_path: str) -> None: ...
    async def run_sftp(self, operation: Callable[[object], T]) -> T: ...


@dataclass(frozen=True, slots=True)
class LocalFileAttributes:
    filename: str
    st_mode: int
    st_size: int
    st_mtime: int


class LocalSFTP:
    """Small Paramiko-SFTP-compatible adapter over the local POSIX filesystem."""

    @staticmethod
    def _path(value: str) -> Path:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise SSHError("Chemin Linux local invalide")
        return path

    def listdir(self, path: str) -> list[str]:
        return [entry.name for entry in self._path(path).iterdir()]

    def listdir_attr(self, path: str) -> list[LocalFileAttributes]:
        values: list[LocalFileAttributes] = []
        for entry in self._path(path).iterdir():
            info = entry.stat()
            values.append(LocalFileAttributes(entry.name, info.st_mode, info.st_size, int(info.st_mtime)))
        return values

    def open(self, path: str, mode: str = "r") -> BinaryIO:
        return self._path(path).open(mode)  # type: ignore[return-value]

    def stat(self, path: str) -> os.stat_result:
        return self._path(path).stat()

    def statvfs(self, path: str) -> os.statvfs_result:
        return os.statvfs(self._path(path))

    def chmod(self, path: str, mode: int) -> None:
        self._path(path).chmod(mode)

    def mkdir(self, path: str, mode: int = 0o777) -> None:
        self._path(path).mkdir(mode=mode)

    def remove(self, path: str) -> None:
        self._path(path).unlink()

    def rmdir(self, path: str) -> None:
        self._path(path).rmdir()

    def rename(self, source: str, destination: str) -> None:
        self._path(source).rename(self._path(destination))

    def posix_rename(self, source: str, destination: str) -> None:
        os.replace(self._path(source), self._path(destination))


class LocalLinuxBackend:
    """Operate the BeamMP host directly; no SSH key or loopback SSH is involved."""

    _SERVICE_ACTIONS = {"status": "is-active", "start": "start", "stop": "stop", "restart": "restart"}

    def __init__(self, service: str = "beammp") -> None:
        service = service.removesuffix(".service")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", service):
            raise ValueError("Nom de service systemd invalide")
        self.service = service
        self.host = "localhost"
        self.username = "beammanager"

    async def execute_service_command(self, action: str) -> CommandResult:
        command = self._SERVICE_ACTIONS.get(action)
        if command is None:
            raise ValueError("Action de service interdite")

        def run() -> CommandResult:
            try:
                process = subprocess.run(
                    ["sudo", "-n", "/usr/bin/systemctl", command, f"{self.service}.service"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise SSHError("Commande systemd locale impossible") from exc
            return CommandResult(process.stdout.strip(), process.stderr.strip(), process.returncode)

        return await asyncio.to_thread(run)

    @contextmanager
    def sftp_session(self) -> Iterator[LocalSFTP]:
        yield LocalSFTP()

    async def run_sftp(self, operation: Callable[[LocalSFTP], T]) -> T:
        return await asyncio.to_thread(operation, LocalSFTP())

    async def list_remote_directory(self, remote_path: str) -> list[str]:
        return await asyncio.to_thread(lambda: sorted(LocalSFTP().listdir(remote_path)))

    async def disk_free_bytes(self, remote_path: str) -> int:
        return await asyncio.to_thread(lambda: shutil.disk_usage(LocalSFTP._path(remote_path)).free)

    async def beammp_version(self, beam_root: str) -> str | None:
        executable = LocalSFTP._path(f"{beam_root.rstrip('/')}/BeamMP-Server")

        def read_version() -> str | None:
            try:
                result = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                return None
            match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
            return match.group(1) if result.returncode == 0 and match else None

        return await asyncio.to_thread(read_version)

    async def read_file(self, remote_path: str, max_bytes: int = 2_000_000) -> bytes:
        def read() -> bytes:
            with LocalSFTP._path(remote_path).open("rb") as handle:
                payload = handle.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise SSHError("Le fichier local depasse la taille autorisee")
            return payload

        try:
            return await asyncio.to_thread(read)
        except SSHError:
            raise
        except OSError as exc:
            raise SSHError("Lecture du fichier BeamMP local impossible") from exc

    async def write_file_atomic(self, remote_path: str, payload: bytes) -> None:
        def write() -> None:
            target = LocalSFTP._path(remote_path)
            temporary = target.with_name(f"{target.name}.beam-manager-tmp")
            try:
                original = target.stat()
                mode = stat.S_IMODE(original.st_mode)
                group_id = original.st_gid
            except FileNotFoundError:
                mode = 0o640
                group_id = target.parent.stat().st_gid
            try:
                with temporary.open("wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.chmod(mode)
                # Atomic replacement changes ownership to the Manager account.
                # Preserve the original shared group so beammpserver can still
                # read ServerConfig.toml after any web-side config/map update.
                if os.name == "posix":
                    os.chown(temporary, -1, group_id)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

        try:
            await asyncio.to_thread(write)
        except OSError as exc:
            raise SSHError("Ecriture atomique du fichier BeamMP local impossible") from exc

    async def ensure_directory(self, remote_path: str, mode: int = 0o775) -> bool:
        def ensure() -> bool:
            path = LocalSFTP._path(remote_path)
            if path.exists():
                if not path.is_dir():
                    raise SSHError("Le chemin local existe mais n'est pas un dossier")
                return False
            path.mkdir(mode=mode)
            path.chmod(mode)
            return True

        return await asyncio.to_thread(ensure)

    async def list_zip_files(self, remote_path: str) -> list[RemoteFile]:
        def list_files() -> list[RemoteFile]:
            values = []
            for entry in LocalSFTP._path(remote_path).iterdir():
                if entry.is_file() and entry.suffix.casefold() == ".zip":
                    info = entry.stat()
                    values.append(RemoteFile(entry.name, info.st_size, int(info.st_mtime)))
            return sorted(values, key=lambda item: item.name.casefold())

        return await asyncio.to_thread(list_files)

    async def move_file(self, source_path: str, destination_path: str) -> None:
        def move() -> None:
            source = LocalSFTP._path(source_path)
            destination = LocalSFTP._path(destination_path)
            if not source.is_file():
                raise SSHError("La source locale n'est pas un fichier")
            if destination.exists():
                raise SSHError("Un fichier du meme nom existe deja a destination")
            source.rename(destination)

        await asyncio.to_thread(move)


def build_server_backend(settings: object, profile: dict[str, object]) -> ServerBackend:
    backend = str(profile.get("backend") or getattr(settings, "server_backend", "ssh"))
    if backend == "local":
        return LocalLinuxBackend(service=str(getattr(settings, "systemd_service")))
    return SSHClient(
        host=str(getattr(settings, "server_host")),
        username=str(getattr(settings, "ssh_user")),
        key_path=Path(getattr(settings, "ssh_key_path")),
        timeout=float(getattr(settings, "ssh_timeout")),
        port=int(profile["ssh_port"]),
        service=str(getattr(settings, "systemd_service")),
    )
