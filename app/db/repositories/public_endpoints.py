"""Repository helpers for public endpoint records used by launcher automation."""

from __future__ import annotations

from app.db.repositories.catalog import InMemoryCatalogRepository
from app.domain import PublicEndpointRecord


class InMemoryPublicEndpointRepository(InMemoryCatalogRepository[PublicEndpointRecord]):
    def __init__(self) -> None:
        super().__init__(PublicEndpointRecord)

    async def get_latest_active(self, endpoint_type: str) -> PublicEndpointRecord | None:
        matches = [
            item
            for item in await self.list()
            if item.endpoint_type == endpoint_type and item.status == "active"
        ]
        return matches[-1] if matches else None

    async def deactivate_active(self, endpoint_type: str) -> None:
        for item in await self.list():
            if item.endpoint_type == endpoint_type and item.status == "active":
                await self.update(item.id, {"status": "inactive"})
