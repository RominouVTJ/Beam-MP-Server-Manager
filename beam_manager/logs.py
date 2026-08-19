from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

from beam_manager.models import LogEntry, LogsResponse
from beam_manager.ssh import SSHClient


LEVEL = re.compile(r"\b(ERROR|ERR|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE)\b", re.I)
TIMESTAMP = re.compile(
    r"(?:\[)?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)(?:\])?"
)
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(AuthKey|authorization|password|passwd|token|secret)\b(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
)


def redact(line: str) -> str:
    result = line
    result = SECRET_PATTERNS[0].sub(lambda match: f"{match.group(1)}{match.group(2)}[MASQUE]", result)
    result = SECRET_PATTERNS[1].sub("Bearer [MASQUE]", result)
    result = SECRET_PATTERNS[2].sub("[SECRET-MASQUE]", result)
    return result


class LogService:
    def __init__(self, ssh: SSHClient, current_path: str, old_path: str) -> None:
        self.ssh = ssh
        self.current_path = current_path
        self.old_path = old_path

    @staticmethod
    def _tail(sftp, path: str, max_bytes: int) -> str:
        attributes = sftp.stat(path)
        with sftp.open(path, "rb") as handle:
            handle.seek(max(0, attributes.st_size - max_bytes))
            payload = handle.read(max_bytes)
        text = payload.decode("utf-8", "replace")
        if attributes.st_size > max_bytes and "\n" in text:
            text = text.split("\n", 1)[1]
        return text

    async def read(self, limit: int = 300) -> LogsResponse:
        limit = min(max(limit, 1), 2000)

        def read_sync(sftp) -> tuple[str, str]:
            chunks: list[str] = []
            sources: list[str] = []
            for path, budget in ((self.old_path, 256_000), (self.current_path, 1_500_000)):
                try:
                    chunks.append(self._tail(sftp, path, budget))
                    sources.append(PurePosixPath(path).name)
                except OSError:
                    continue
            return "\n".join(chunks), " + ".join(sources)

        raw, source = await self.ssh.run_sftp(read_sync)
        entries: list[LogEntry] = []
        for index, raw_line in enumerate(raw.splitlines()):
            line = redact(raw_line.strip())
            if not line:
                continue
            match = LEVEL.search(line)
            token = match.group(1).upper() if match else "INFO"
            level = "ERROR" if token in {"ERROR", "ERR", "FATAL"} else "WARNING" if token.startswith("WARN") else "INFO"
            timestamp = TIMESTAMP.search(line)
            digest = hashlib.sha256(f"{index}\0{line}".encode("utf-8")).hexdigest()[:20]
            entries.append(
                LogEntry(
                    id=digest,
                    timestamp=timestamp.group(1) if timestamp else None,
                    level=level,
                    message=line,
                )
            )
        return LogsResponse(
            source=source or "indisponible",
            entries=entries[-limit:],
            message=(
                "Journal systemd non lisible; lecture securisee des fichiers BeamMP locaux."
                if source
                else "Aucune source de logs BeamMP lisible."
            ),
        )
