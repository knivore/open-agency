from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.core.time import ensure_utc, utc_now
from app.domain import ScheduleDefinition
from app.scheduler.jobs import ScheduledJobResult
from app.scheduler.triggers import compute_next_fire


logger = logging.getLogger(__name__)


class ScheduleConcurrencyError(RuntimeError):
    """Raised when a schedule hits its concurrency limit."""


class WorkflowScheduler:
    def __init__(
            self,
            *,
            schedule_repo,
            execution_store,
            runtime_registry,
            execution_starter: Callable[[str], Awaitable[Any]] | None = None,
            scheduler_id: str | None = None,
            claim_lease_seconds: int = 300,
            runtime_operations=None,
    ):
        self.schedule_repo = schedule_repo
        self.execution_store = execution_store
        self.runtime_registry = runtime_registry
        self.execution_starter = execution_starter
        self.scheduler_id = scheduler_id or f"workflow-scheduler:{uuid4()}"
        self.claim_lease_seconds = claim_lease_seconds
        self.runtime_operations = runtime_operations

    async def create_schedule(self, schedule: ScheduleDefinition) -> ScheduleDefinition:
        now = utc_now()
        computation = compute_next_fire(schedule, now=now)
        patch = schedule.model_dump(mode="json")
        patch["next_fire_at"] = computation.next_fire_at.isoformat() if computation.next_fire_at else None
        schedule = ScheduleDefinition.model_validate(patch)
        return await self.schedule_repo.create(schedule)

    async def patch_schedule(self, schedule_id: str, patch: dict[str, Any]) -> ScheduleDefinition | None:
        current = await self.schedule_repo.get(schedule_id)
        if current is None:
            return None
        if "next_fire_at" not in patch:
            candidate = current.__class__.model_validate({**current.model_dump(mode="json"), **patch})
            reschedules = any(
                key in patch
                for key in (
                    "enabled",
                    "trigger_type",
                    "schedule_type",
                    "trigger_config",
                    "cron",
                    "interval_seconds",
                    "timezone",
                )
            )
            if candidate.enabled is False:
                patch["next_fire_at"] = None
            elif reschedules:
                computation = compute_next_fire(candidate, now=utc_now())
                patch["next_fire_at"] = computation.next_fire_at.isoformat() if computation.next_fire_at else None
        return await self.schedule_repo.update(schedule_id, patch)

    async def enable_schedule(self, schedule_id: str) -> ScheduleDefinition | None:
        schedule = await self.schedule_repo.get(schedule_id)
        if schedule is None:
            return None
        computation = compute_next_fire(
            schedule.__class__.model_validate({**schedule.model_dump(mode="json"), "enabled": True}), now=utc_now())
        return await self.schedule_repo.update(
            schedule_id,
            {
                "enabled": True,
                "next_fire_at": computation.next_fire_at.isoformat() if computation.next_fire_at else None,
            },
        )

    async def disable_schedule(self, schedule_id: str) -> ScheduleDefinition | None:
        return await self.schedule_repo.update(schedule_id, {"enabled": False, "next_fire_at": None})

    async def trigger_now(
            self,
            schedule_id: str,
            *,
            source: str = "manual",
            scheduled_fire_at: datetime | None = None,
    ) -> ScheduledJobResult:
        schedule = await self.schedule_repo.get(schedule_id)
        if schedule is None:
            raise ValueError(f"Schedule '{schedule_id}' not found")
        now = utc_now()
        trigger = {
            "type": "schedule",
            "created_by": "scheduler",
            "source": source,
            "schedule_id": schedule.id,
            "schedule_trigger_type": schedule.trigger_type.value,
        }
        if scheduled_fire_at is not None:
            trigger["scheduled_fire_at"] = ensure_utc(scheduled_fire_at).isoformat()
        try:
            await self._assert_concurrency(schedule)
            execution = await self.runtime_registry.create_execution(
                schedule.workflow_id,
                schedule.input_template,
                trigger,
                runtime_adapter_id=schedule.runtime_adapter_override,
            )
            if self.execution_starter is not None:
                await self.execution_starter(execution.id)
        except Exception:
            if scheduled_fire_at is not None:
                await self._mark_fire_claim_failed(schedule.id, scheduled_fire_at)
            raise
        if scheduled_fire_at is not None:
            await self._mark_fire_claim_fired(schedule.id, scheduled_fire_at, execution.id)
        patch = {
            "last_fire_at": now.isoformat(),
            "metadata": {**schedule.metadata, "last_execution_id": execution.id},
        }
        fired_schedule = schedule.__class__.model_validate({**schedule.model_dump(mode="json"), **patch})
        next_computation = compute_next_fire(fired_schedule, now=now)
        patch["next_fire_at"] = next_computation.next_fire_at.isoformat() if next_computation.next_fire_at else None
        await self.schedule_repo.update(schedule_id, patch)
        return ScheduledJobResult(
            schedule=(await self.schedule_repo.get(schedule_id)) or schedule,
            execution_id=execution.id,
            triggered_at=now,
            metadata={"source": source},
        )

    async def run_due_schedules(self) -> list[ScheduledJobResult]:
        now = utc_now()
        results: list[ScheduledJobResult] = []
        for schedule in await self.schedule_repo.list():
            computation = compute_next_fire(schedule, now=now)
            if not computation.ready:
                continue
            scheduled_fire_at = schedule.next_fire_at or now
            if not await self._acquire_fire_claim(schedule.id, scheduled_fire_at):
                continue
            try:
                results.append(
                    await self.trigger_now(
                        schedule.id,
                        source="scheduler",
                        scheduled_fire_at=scheduled_fire_at,
                    )
                )
            except Exception as exc:
                logger.exception(
                    "Scheduled workflow fire failed for schedule '%s' at %s",
                    schedule.id,
                    scheduled_fire_at.isoformat(),
                )
                self._record_operation(
                    "scheduler.due_fire_failed",
                    schedule_id=schedule.id,
                    scheduled_fire_at=scheduled_fire_at.isoformat(),
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                continue
        return results

    def fire_claim_support_available(self) -> bool:
        return all(
            callable(getattr(self.schedule_repo, method_name, None))
            for method_name in (
                "acquire_schedule_fire_claim",
                "mark_schedule_fire_claim_fired",
                "mark_schedule_fire_claim_failed",
            )
        )

    async def _acquire_fire_claim(self, schedule_id: str, scheduled_fire_at: datetime) -> bool:
        acquire = getattr(self.schedule_repo, "acquire_schedule_fire_claim", None)
        if acquire is None:
            self._record_operation(
                "scheduler.fire_claim_support_missing",
                schedule_id=schedule_id,
                scheduled_fire_at=ensure_utc(scheduled_fire_at).isoformat(),
            )
            return True
        return await acquire(
            schedule_id=schedule_id,
            scheduled_fire_at=scheduled_fire_at,
            claimed_by=self.scheduler_id,
            lease_seconds=self.claim_lease_seconds,
        )

    async def _mark_fire_claim_fired(self, schedule_id: str, scheduled_fire_at: datetime, execution_id: str) -> None:
        mark_fired = getattr(self.schedule_repo, "mark_schedule_fire_claim_fired", None)
        if mark_fired is None:
            return
        await mark_fired(
            schedule_id=schedule_id,
            scheduled_fire_at=scheduled_fire_at,
            execution_id=execution_id,
            claimed_by=self.scheduler_id,
        )

    async def _mark_fire_claim_failed(self, schedule_id: str, scheduled_fire_at: datetime) -> None:
        mark_failed = getattr(self.schedule_repo, "mark_schedule_fire_claim_failed", None)
        if mark_failed is None:
            return
        await mark_failed(
            schedule_id=schedule_id,
            scheduled_fire_at=scheduled_fire_at,
            claimed_by=self.scheduler_id,
        )

    async def _assert_concurrency(self, schedule: ScheduleDefinition) -> None:
        active = await self.execution_store.list_active_executions()
        matching = [
            execution
            for execution in active
            if execution.metadata.get("trigger", {}).get("schedule_id") == schedule.id
               or execution.metadata.get("schedule_id") == schedule.id
        ]
        if len(matching) >= schedule.max_concurrent_executions:
            raise ScheduleConcurrencyError(
                f"Schedule '{schedule.id}' has reached max_concurrent_executions={schedule.max_concurrent_executions}"
            )

    def _record_operation(self, action: str, **payload: Any) -> None:
        operations = self.runtime_operations
        if operations is None:
            return
        operations.increment(action)
        error_type = payload.get("error_type")
        if isinstance(error_type, str) and error_type:
            operations.increment(f"{action}.{error_type}")
        operations.record_action(action, **payload)
