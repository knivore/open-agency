from __future__ import annotations

from typing import Any

from app.domain import UserDefinition
from .catalog import InMemoryCatalogRepository


class InMemoryUserRepository(InMemoryCatalogRepository[UserDefinition]):
    def __init__(self):
        super().__init__(UserDefinition)

    async def create(self, item: UserDefinition) -> UserDefinition:
        return await super().create(self._normalize(item))

    async def save(self, item: UserDefinition) -> UserDefinition:
        return await super().save(self._normalize(item))

    def _normalize(self, item: UserDefinition) -> UserDefinition:
        payload = item.model_dump(mode="json")
        payload["email"] = item.email.lower()
        return UserDefinition.model_validate(payload)

    async def find_by_email(self, email: str) -> UserDefinition | None:
        normalized = email.lower()
        for user in await self.list():
            if user.email.lower() == normalized:
                return user
        return None

    async def find_by_external_identity(self, provider: str, provider_subject: str) -> UserDefinition | None:
        for user in await self.list():
            if user.provider == provider and user.provider_subject == provider_subject:
                return user
        return None

    async def search_by_email(self, email: str) -> list[UserDefinition]:
        normalized = email.lower()
        return [user for user in await self.list() if normalized in user.email.lower()]

    async def upsert_from_identity(self, item: UserDefinition) -> UserDefinition:
        existing = None
        if item.provider and item.provider_subject:
            existing = await self.find_by_external_identity(item.provider, item.provider_subject)
        if existing is None:
            existing = await self.find_by_email(item.email)
        if existing is None:
            return await self.create(item)
        merged: dict[str, Any] = existing.model_dump(mode="json")
        merged.update(item.model_dump(mode="json", exclude_none=True))
        merged["id"] = existing.id
        return await self.save(UserDefinition.model_validate(merged))
