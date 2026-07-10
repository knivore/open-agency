from __future__ import annotations

from datetime import datetime, timezone

from app.domain import ApiTokenDefinition
from .catalog import InMemoryCatalogRepository


class InMemoryApiTokenRepository(InMemoryCatalogRepository[ApiTokenDefinition]):
    def __init__(self):
        super().__init__(ApiTokenDefinition)

    async def list_by_owner(self, owner_user_id: str) -> list[ApiTokenDefinition]:
        return [token for token in await self.list() if token.owner_user_id == owner_user_id]

    async def find_by_hash(self, token_hash: str) -> ApiTokenDefinition | None:
        for token in await self.list(include_deleted=True):
            if token.token_hash == token_hash:
                return token
        return None

    async def soft_delete(self, item_id: str) -> bool:
        current = await self.get(item_id)
        if current is None:
            return False
        await self.update(item_id, {"revoked_at": datetime.now(timezone.utc).isoformat()})
        return True
