from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class SQLAlchemyRepository(Generic[T]):
    model_cls: type[T]

    def __init__(self, session: AsyncSession, model_cls: type[T]):
        self.session = session
        self.model_cls = model_cls

    async def create(self, item: T) -> T:
        self.session.add(item)
        await self.session.flush()
        await self.session.refresh(item)
        return item

    async def get(self, item_id: Any) -> T | None:
        return await self.session.get(self.model_cls, item_id)

    async def list(self) -> list[T]:
        result = await self.session.execute(select(self.model_cls))
        return list(result.scalars().all())

    async def update(self, item_id: Any, patch: dict[str, Any]) -> T | None:
        item = await self.get(item_id)
        if item is None:
            return None
        for key, value in patch.items():
            setattr(item, key, value)
        await self.session.flush()
        await self.session.refresh(item)
        return item
