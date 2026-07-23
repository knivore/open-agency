"""Owner-scoped runtime artifacts with opaque identifiers and bounded size."""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from .contracts import OwnerClaims


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    owner: OwnerClaims
    session_id: str
    media_type: str
    path: Path
    created_at: float
    expires_at: float


class ArtifactStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.maximum_bytes = int(os.getenv("BROWSER_ARTIFACT_MAX_BYTES", str(10 * 1024 * 1024)))
        self.retention_seconds = max(60, int(os.getenv("BROWSER_ARTIFACT_RETENTION_SECONDS", "86400")))
        self._records: dict[str, ArtifactRecord] = {}

    def put(
            self,
            data: bytes,
            *,
            owner: OwnerClaims,
            session_id: str,
            suffix: str,
            media_type: str,
            retention_seconds: int | None = None,
    ) -> ArtifactRecord:
        self.prune()
        if len(data) > self.maximum_bytes:
            raise ValueError("Browser artifact exceeds the configured size limit")
        artifact_id = f"bra_{secrets.token_urlsafe(20)}"
        session_dir = self.root / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"{artifact_id}{suffix}"
        path.write_bytes(data)
        created_at = time.time()
        effective_retention = min(retention_seconds or self.retention_seconds, self.retention_seconds)
        record = ArtifactRecord(
            artifact_id,
            owner,
            session_id,
            media_type,
            path,
            created_at,
            created_at + max(60, effective_retention),
        )
        self._records[artifact_id] = record
        return record

    def get(self, artifact_id: str, *, owner: OwnerClaims) -> ArtifactRecord:
        self.prune()
        record = self._records.get(artifact_id)
        if record is None or not owner.owns(record.owner):
            raise FileNotFoundError("Browser artifact was not found")
        return record

    def prune(self) -> int:
        now = time.time()
        expired = [record for record in self._records.values() if record.expires_at <= now]
        for record in expired:
            self._records.pop(record.artifact_id, None)
            try:
                record.path.unlink(missing_ok=True)
            except OSError:
                pass
        return len(expired)

