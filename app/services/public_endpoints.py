"""Service helpers for persisting launcher-discovered public endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.api.context import ApiContext
from app.domain import PublicEndpointRecord


@dataclass(slots=True)
class PublicEndpointService:
    context: ApiContext

    async def get_current_webhook_base_url(self) -> str | None:
        if not hasattr(self.context.public_endpoint_repo, "get_latest_active"):
            return None
        record = await self.context.public_endpoint_repo.get_latest_active("webhook_base_url")
        if record is None:
            return None
        return record.url

    async def record_webhook_base_url(
            self,
            *,
            url: str,
            provider: str,
            source: str = "launcher",
            metadata: dict | None = None,
    ) -> PublicEndpointRecord:
        normalized_url = url.strip().rstrip("/")
        existing = None
        if hasattr(self.context.public_endpoint_repo, "get_latest_active"):
            existing = await self.context.public_endpoint_repo.get_latest_active("webhook_base_url")

        # Keep only one current public webhook base URL active so channel setup
        # and auto-registration use the same launcher-discovered origin.
        if existing is not None and existing.url == normalized_url and existing.provider == provider:
            updated = await self.context.public_endpoint_repo.update(
                existing.id,
                {
                    "status": "active",
                    "source": source,
                    "metadata": {**existing.metadata, **(metadata or {})},
                    "last_seen_at": datetime.now(timezone.utc),
                },
            )
            if updated is None:
                raise RuntimeError("Could not update the current public endpoint record.")
            return updated

        if hasattr(self.context.public_endpoint_repo, "deactivate_active"):
            await self.context.public_endpoint_repo.deactivate_active("webhook_base_url")

        record = PublicEndpointRecord(
            endpoint_type="webhook_base_url",
            provider=provider,
            url=normalized_url,
            status="active",
            source=source,
            metadata=metadata or {},
            last_seen_at=datetime.now(timezone.utc),
        )
        return await self.context.public_endpoint_repo.create(record)
