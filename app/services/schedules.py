from __future__ import annotations

from dataclasses import dataclass
from pydantic import ValidationError
from typing import Any

from app.api.context import ApiContext
from app.domain import ScheduleDefinition


@dataclass(slots=True)
class ScheduleService:
    context: ApiContext

    async def create_schedule(self, payload: dict[str, Any]):
        schedule = ScheduleDefinition.model_validate(payload)
        return await self.context.scheduler.create_schedule(schedule)

    async def patch_schedule(self, schedule_id: str, patch: dict[str, Any]):
        return await self.context.scheduler.patch_schedule(schedule_id, patch)

    async def enable_schedule(self, schedule_id: str):
        return await self.context.scheduler.enable_schedule(schedule_id)

    async def disable_schedule(self, schedule_id: str):
        return await self.context.scheduler.disable_schedule(schedule_id)

    async def trigger_now(self, schedule_id: str):
        return await self.context.scheduler.trigger_now(schedule_id)
