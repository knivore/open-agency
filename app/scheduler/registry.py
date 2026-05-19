from __future__ import annotations

from typing import Protocol

from app.domain import ScheduleDefinition


class ScheduleRepository(Protocol):
    async def create(self, item: ScheduleDefinition) -> ScheduleDefinition: ...

    async def list(self, *, include_deleted: bool = False): ...

    async def get(self, item_id: str, *, include_deleted: bool = False): ...

    async def update(self, item_id: str, patch: dict): ...
