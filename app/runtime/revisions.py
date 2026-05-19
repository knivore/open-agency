from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.time import utc_now
from app.domain import RuntimeRevision, RuntimeRevisionStatus
from app.tools.discovery import discover_integrations, integrations_root

FINGERPRINT_SCHEMA_VERSION = "integrations-fingerprint:v1"
IGNORED_DIRECTORY_NAMES = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _iter_integration_files(integration_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in integration_root.rglob("*"):
        if path.is_dir():
            continue
        if any(part in IGNORED_DIRECTORY_NAMES for part in path.parts):
            continue
        if path.suffix in IGNORED_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def fingerprint_integrations(
        *,
        root: Path | None = None,
        base_image: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
        strict: bool = True,
) -> str:
    root = root or integrations_root()
    discovered = discover_integrations(root=root, strict=strict)
    payload: dict[str, Any] = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "source_path": "integrations/",
        "base_image": base_image,
        "extra_metadata": extra_metadata or {},
        "integrations": [],
    }
    for integration in discovered:
        integration_root = Path(integration.root_path)
        files_payload = []
        for file_path in _iter_integration_files(integration_root):
            relative_path = file_path.relative_to(root).as_posix()
            files_payload.append(
                {
                    "path": relative_path,
                    "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
                }
            )
        payload["integrations"].append(
            {
                "id": integration.manifest.id,
                "name": integration.manifest.name,
                "version": integration.manifest.version,
                "module_root": integration.manifest.module_root,
                "tool_modules": integration.manifest.tool_modules,
                "requirements_file": integration.manifest.requirements_file,
                "env": integration.manifest.env,
                "capabilities": integration.manifest.capabilities,
                "metadata": integration.manifest.metadata,
                "files": files_payload,
            }
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RuntimeRevisionService:
    runtime_revision_repo: Any
    root_path: Path | None = None
    base_image: str | None = None

    def current_source_path(self) -> str:
        root = self.root_path or integrations_root()
        return f"{root.name}/"

    def compute_current_fingerprint(
            self,
            *,
            extra_metadata: dict[str, Any] | None = None,
            strict: bool = True,
    ) -> str:
        return fingerprint_integrations(
            root=self.root_path,
            base_image=self.base_image,
            extra_metadata=extra_metadata,
            strict=strict,
        )

    async def resolve_current_revision(
            self,
            *,
            metadata: dict[str, Any] | None = None,
            mark_ready: bool = True,
            strict: bool = True,
    ) -> RuntimeRevision:
        fingerprint = self.compute_current_fingerprint(extra_metadata=metadata, strict=strict)
        existing = await self.runtime_revision_repo.get_by_fingerprint(fingerprint)
        if existing is not None:
            if existing.build_status == RuntimeRevisionStatus.INVALIDATED:
                revived = await self.runtime_revision_repo.update(
                    existing.id,
                    {
                        "build_status": RuntimeRevisionStatus.READY.value if mark_ready else RuntimeRevisionStatus.PENDING.value,
                        "ready_at": utc_now().isoformat() if mark_ready else None,
                        "invalidated_at": None,
                        "invalidation_reason": None,
                        "metadata": {**existing.metadata, **(metadata or {})},
                    },
                )
                if revived is not None:
                    return revived
            return existing

        now = utc_now()
        return await self.runtime_revision_repo.create(
            RuntimeRevision(
                fingerprint=fingerprint,
                source_path=self.current_source_path(),
                build_status=RuntimeRevisionStatus.READY if mark_ready else RuntimeRevisionStatus.PENDING,
                base_image=self.base_image,
                ready_at=now if mark_ready else None,
                metadata_json=metadata or {},
            )
        )

    async def mark_revision_building(self, revision_id: str) -> RuntimeRevision | None:
        return await self.runtime_revision_repo.update(
            revision_id,
            {
                "build_status": RuntimeRevisionStatus.BUILDING.value,
            },
        )

    async def mark_revision_ready(
            self,
            revision_id: str,
            *,
            image_name: str | None = None,
            image_tag: str | None = None,
            metadata: dict[str, Any] | None = None,
    ) -> RuntimeRevision | None:
        patch: dict[str, Any] = {
            "build_status": RuntimeRevisionStatus.READY.value,
            "ready_at": utc_now().isoformat(),
        }
        if image_name is not None:
            patch["image_name"] = image_name
        if image_tag is not None:
            patch["image_tag"] = image_tag
        if metadata is not None:
            patch["metadata"] = metadata
        return await self.runtime_revision_repo.update(revision_id, patch)

    async def mark_revision_failed(
            self,
            revision_id: str,
            *,
            reason: str,
            metadata: dict[str, Any] | None = None,
    ) -> RuntimeRevision | None:
        patch: dict[str, Any] = {
            "build_status": RuntimeRevisionStatus.FAILED.value,
            "invalidation_reason": reason,
        }
        if metadata is not None:
            patch["metadata"] = metadata
        return await self.runtime_revision_repo.update(revision_id, patch)

    async def invalidate_superseded_revisions(
            self,
            active_revision_id: str,
            *,
            reason: str = "superseded",
    ) -> list[RuntimeRevision]:
        revisions = await self.runtime_revision_repo.list(include_deleted=True)
        invalidated: list[RuntimeRevision] = []
        for revision in revisions:
            if revision.id == active_revision_id:
                continue
            if revision.build_status not in {
                RuntimeRevisionStatus.READY,
                RuntimeRevisionStatus.PENDING,
                RuntimeRevisionStatus.BUILDING,
            }:
                continue
            updated = await self.runtime_revision_repo.invalidate_revision(revision.id, reason=reason)
            if updated is not None:
                invalidated.append(updated)
        return invalidated
