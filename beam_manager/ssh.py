from __future__ import annotations

import asyncio
import io
import stat
import re
import shlex
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, TypeVar

import paramiko


class SSHError(RuntimeError):
    """Safe application-level SSH error."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


@dataclass(frozen=True, slots=True)
class RemoteFile:
    name: str
    size: int
    modified_at: int


T = TypeVar("T")


class SSHClient:
    """Small async facade around Paramiko for SSH and SFTP operations."""

    _SERVICE_ACTIONS = {"status": "is-active", "start": "start", "stop": "stop", "restart": "restart"}

    def __init__(
        self,
        host: str,
        username: str,
        key_path: Path,
        timeout: float = 8.0,
        client_factory: Callable[[], paramiko.SSHClient] = paramiko.SSHClient,
        port: int = 22,
        service: str = "beammp",
    ) -> None:
        service = service.removesuffix(".service")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,80}", service):
            raise ValueError("Nom de service systemd invalide")
        if not 1 <= port <= 65535:
            raise ValueError("Port SSH invalide")
        self.host = host
        self.username = username
        self.key_path = key_path
        self.timeout = timeout
        self.port = port
        self.service = service
        self._client_factory = client_factory

    def _connect(self) -> paramiko.SSHClient:
        if not self.key_path.is_file():
            raise SSHError("Cle SSH introuvable sur le PC maitre")

        client = self._client_factory()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                key_filename=str(self.key_path),
                timeout=self.timeout,
                banner_timeout=self.timeout,
                auth_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:
            client.close()
            raise SSHError("Connexion SSH au serveur BeamMP impossible") from exc
        return client

    def _execute_sync(self, command: str) -> CommandResult:
        client = self._connect()
        try:
            _, stdout, stderr = client.exec_command(command, timeout=self.timeout)
            exit_code = stdout.channel.recv_exit_status()
            return CommandResult(
                stdout=stdout.read().decode("utf-8", errors="replace").strip(),
                stderr=stderr.read().decode("utf-8", errors="replace").strip(),
                exit_code=exit_code,
            )
        except Exception as exc:
            raise SSHError("La commande distante n'a pas pu etre executee") from exc
        finally:
            client.close()

    async def execute_service_command(self, action: str) -> CommandResult:
        systemctl_action = self._SERVICE_ACTIONS.get(action)
        if systemctl_action is None:
            raise ValueError("Action de service interdite")
        command = f"sudo -n systemctl {systemctl_action} {self.service}"
        return await asyncio.to_thread(self._execute_sync, command)

    async def list_remote_directory(self, remote_path: str) -> list[str]:
        """Read-only SFTP primitive used by later phases."""

        def list_sync() -> list[str]:
            client = self._connect()
            try:
                with client.open_sftp() as sftp:
                    return sorted(sftp.listdir(remote_path))
            except Exception as exc:
                raise SSHError("Lecture SFTP impossible") from exc
            finally:
                client.close()

        return await asyncio.to_thread(list_sync)

    async def disk_free_bytes(self, remote_path: str) -> int:
        """Read capacity through SFTP, with one fixed validated `df` fallback."""

        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", remote_path) or ".." in remote_path.split("/"):
            raise SSHError("Chemin distant invalide pour le diagnostic disque")

        def stat_sync() -> int:
            with self.sftp_session() as sftp:
                values = sftp.statvfs(remote_path)
                return int(values.f_bavail * values.f_frsize)

        try:
            return await asyncio.to_thread(stat_sync)
        except (AttributeError, OSError, SSHError):
            result = await asyncio.to_thread(
                self._execute_sync, f"df -Pk -- {shlex.quote(remote_path)}"
            )
            if result.exit_code != 0:
                raise SSHError("Espace disque serveur indisponible")
            lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
            try:
                return int(lines[-1][3]) * 1024
            except (IndexError, ValueError) as exc:
                raise SSHError("Reponse disque serveur invalide") from exc

    async def beammp_version(self, beam_root: str) -> str | None:
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", beam_root) or ".." in beam_root.split("/"):
            raise SSHError("Chemin BeamMP invalide")
        result = await asyncio.to_thread(
            self._execute_sync, f"{shlex.quote(beam_root.rstrip('/') + '/BeamMP-Server')} --version"
        )
        if result.exit_code != 0:
            return None
        match = re.search(r"\b(\d+\.\d+\.\d+)\b", result.stdout)
        return match.group(1) if match else None

    @contextmanager
    def sftp_session(self) -> Iterator[paramiko.SFTPClient]:
        """Open one authenticated SFTP session for a bounded operation."""

        client = self._connect()
        try:
            with client.open_sftp() as sftp:
                yield sftp
        except SSHError:
            raise
        except Exception as exc:
            raise SSHError("Operation SFTP impossible") from exc
        finally:
            client.close()

    async def read_file(self, remote_path: str, max_bytes: int = 2_000_000) -> bytes:
        def read_sync() -> bytes:
            with self.sftp_session() as sftp:
                with sftp.open(remote_path, "rb") as handle:
                    payload = handle.read(max_bytes + 1)
                    if len(payload) > max_bytes:
                        raise SSHError("Le fichier distant depasse la taille autorisee")
                    return payload

        return await asyncio.to_thread(read_sync)

    async def write_file_atomic(self, remote_path: str, payload: bytes) -> None:
        def write_sync() -> None:
            temporary_path = f"{remote_path}.beam-manager-tmp"
            backup_path = f"{remote_path}.beam-manager-backup"
            with self.sftp_session() as sftp:
                try:
                    original = sftp.stat(remote_path)
                except FileNotFoundError:
                    original = None
                try:
                    with sftp.open(temporary_path, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                    sftp.chmod(
                        temporary_path,
                        stat.S_IMODE(original.st_mode) if original is not None else 0o640,
                    )
                    try:
                        sftp.posix_rename(temporary_path, remote_path)
                    except OSError:
                        if original is None:
                            sftp.rename(temporary_path, remote_path)
                            return
                        try:
                            sftp.remove(backup_path)
                        except FileNotFoundError:
                            pass
                        sftp.rename(remote_path, backup_path)
                        try:
                            sftp.rename(temporary_path, remote_path)
                        except Exception:
                            sftp.rename(backup_path, remote_path)
                            raise
                        sftp.remove(backup_path)
                except Exception:
                    try:
                        sftp.remove(temporary_path)
                    except OSError:
                        pass
                    raise

        await asyncio.to_thread(write_sync)

    async def ensure_directory(self, remote_path: str, mode: int = 0o775) -> bool:
        """Create one configured directory if absent. Returns True when created."""

        def ensure_sync() -> bool:
            with self.sftp_session() as sftp:
                try:
                    attributes = sftp.stat(remote_path)
                    if not stat.S_ISDIR(attributes.st_mode):
                        raise SSHError("Le chemin distant existe mais n'est pas un dossier")
                    return False
                except FileNotFoundError:
                    sftp.mkdir(remote_path, mode=mode)
                    sftp.chmod(remote_path, mode)
                    return True

        return await asyncio.to_thread(ensure_sync)

    async def list_zip_files(self, remote_path: str) -> list[RemoteFile]:
        def list_sync() -> list[RemoteFile]:
            with self.sftp_session() as sftp:
                files: list[RemoteFile] = []
                for item in sftp.listdir_attr(remote_path):
                    if stat.S_ISREG(item.st_mode) and item.filename.lower().endswith(".zip"):
                        files.append(
                            RemoteFile(
                                name=item.filename,
                                size=item.st_size,
                                modified_at=item.st_mtime,
                            )
                        )
                return sorted(files, key=lambda item: item.name.casefold())

        return await asyncio.to_thread(list_sync)

    async def move_file(self, source_path: str, destination_path: str) -> None:
        def move_sync() -> None:
            with self.sftp_session() as sftp:
                source = sftp.stat(source_path)
                if not stat.S_ISREG(source.st_mode):
                    raise SSHError("La source distante n'est pas un fichier")
                try:
                    sftp.stat(destination_path)
                except FileNotFoundError:
                    pass
                else:
                    raise SSHError("Un fichier du meme nom existe deja a destination")
                sftp.rename(source_path, destination_path)

        await asyncio.to_thread(move_sync)

    async def run_sftp(self, operation: Callable[[paramiko.SFTPClient], T]) -> T:
        def run_sync() -> T:
            with self.sftp_session() as sftp:
                return operation(sftp)

        return await asyncio.to_thread(run_sync)
