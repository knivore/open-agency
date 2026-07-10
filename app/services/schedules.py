from __future__ import annotations

from dataclasses import dataclass
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
        current = await self.context.schedule_repo.get(schedule_id)
        if current is None:
            return None
        merged = {**current.model_dump(mode="json"), **patch}
        return await self.context.scheduler.patch_schedule(
            schedule_id,
            merged,
        )

    async def enable_schedule(self, schedule_id: str):
        return await self.context.scheduler.enable_schedule(schedule_id)

    async def disable_schedule(self, schedule_id: str):
        return await self.context.scheduler.disable_schedule(schedule_id)

    async def trigger_now(self, schedule_id: str):
        return await self.context.scheduler.trigger_now(schedule_id)

    async def dispatch_event(self, payload: dict[str, Any]):
        event_type = str(payload.get("event_type") or payload.get("type") or "").strip()
        event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        source = str(payload.get("source") or "event")
        return await self.context.scheduler.dispatch_event(
            event_type=event_type,
            payload=event_payload,
            source=source,
        )
