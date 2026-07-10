from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.core.time import ensure_utc, utc_now
from app.domain import GoalDefinition, GoalStatus, ScheduleDefinition, ScheduleType
from app.runtime.native.errors import WorkflowNotFoundError
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
            goal_supervisor_waker: Callable[[str], Awaitable[Any]] | None = None,
            goal_repo=None,
            scheduler_id: str | None = None,
            claim_lease_seconds: int = 300,
            runtime_operations=None,
    ):
        self.schedule_repo = schedule_repo
        self.execution_store = execution_store
        self.runtime_registry = runtime_registry
        self.execution_starter = execution_starter
        self.goal_supervisor_waker = goal_supervisor_waker
        self.goal_repo = goal_repo
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
            event_payload: dict[str, Any] | None = None,
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
        physical_action = schedule.metadata.get("physical_action") if isinstance(schedule.metadata, dict) else None
        if isinstance(physical_action, dict):
            trigger["physical_action"] = {
                "device_id": physical_action.get("device_id"),
                "command_type": physical_action.get("command_type"),
                "status_tracking": True,
            }
        if scheduled_fire_at is not None:
            trigger["scheduled_fire_at"] = ensure_utc(scheduled_fire_at).isoformat()
        if event_payload is not None:
            trigger["event"] = event_payload
        goal = await self._resolve_schedule_goal(schedule, scheduled_fire_at=scheduled_fire_at, now=now)
        goal_id = goal.id if goal is not None else None
        input_payload = dict(schedule.input_template)
        if goal_id:
            trigger["goal_id"] = goal_id
            input_payload.setdefault("goal_id", goal_id)
        try:
            await self._assert_concurrency(schedule)
            execution = await self.runtime_registry.create_execution(
                schedule.workflow_id,
                input_payload,
                trigger,
                runtime_adapter_id=schedule.runtime_adapter_override,
                goal_id=goal_id,
            )
            if goal is not None:
                goal = await self._link_execution_to_goal(goal, execution.id)
            if self.execution_starter is not None:
                await self.execution_starter(execution.id)
        except WorkflowNotFoundError:
            if scheduled_fire_at is not None:
                await self._mark_fire_claim_failed(schedule.id, scheduled_fire_at)
            # Disable orphaned schedules so the scheduler does not retry a workflow that has already been deleted.
            await self.disable_schedule(schedule.id)
            self._record_operation(
                "scheduler.schedule_disabled_missing_workflow",
                schedule_id=schedule.id,
                workflow_id=schedule.workflow_id,
                source=source,
            )
            raise
        except Exception:
            if scheduled_fire_at is not None:
                await self._mark_fire_claim_failed(schedule.id, scheduled_fire_at)
            raise
        if scheduled_fire_at is not None:
            await self._mark_fire_claim_fired(schedule.id, scheduled_fire_at, execution.id)
        patch = {
            "last_fire_at": now.isoformat(),
            "metadata": self._schedule_metadata_after_fire(schedule, execution.id, goal_id),
        }
        fired_schedule = schedule.__class__.model_validate({**schedule.model_dump(mode="json"), **patch})
        next_computation = compute_next_fire(fired_schedule, now=now)
        patch["next_fire_at"] = next_computation.next_fire_at.isoformat() if next_computation.next_fire_at else None
        await self.schedule_repo.update(schedule_id, patch)
        await self._maybe_wake_goal_supervisor(schedule, goal_id=goal_id, source=source)
        return ScheduledJobResult(
            schedule=(await self.schedule_repo.get(schedule_id)) or schedule,
            execution_id=execution.id,
            triggered_at=now,
            metadata={"source": source, "goal_id": goal_id},
        )

    async def dispatch_event(
            self,
            *,
            event_type: str,
            payload: dict[str, Any] | None = None,
            source: str = "event",
    ) -> list[ScheduledJobResult]:
        normalized_event_type = event_type.strip()
        if not normalized_event_type:
            raise ValueError("event_type is required")
        event_payload = {
            "event_type": normalized_event_type,
            "payload": dict(payload or {}),
            "received_at": utc_now().isoformat(),
        }
        results: list[ScheduledJobResult] = []
        for schedule in await self.schedule_repo.list():
            if not schedule.enabled or schedule.trigger_type != ScheduleType.EVENT_MATCH:
                continue
            if not self._schedule_matches_event(schedule, normalized_event_type, event_payload["payload"]):
                continue
            try:
                results.append(
                    await self.trigger_now(
                        schedule.id,
                        source=source,
                        event_payload=event_payload,
                    )
                )
            except WorkflowNotFoundError as exc:
                logger.warning(
                    "Disabled event schedule '%s' because workflow '%s' was not found",
                    schedule.id,
                    schedule.workflow_id,
                )
                self._record_operation(
                    "scheduler.event_fire_missing_workflow",
                    schedule_id=schedule.id,
                    workflow_id=schedule.workflow_id,
                    event_type=normalized_event_type,
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                continue
            except Exception as exc:
                logger.exception("Event schedule '%s' failed for event '%s'", schedule.id, normalized_event_type)
                self._record_operation(
                    "scheduler.event_fire_failed",
                    schedule_id=schedule.id,
                    event_type=normalized_event_type,
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                continue
        self._record_operation(
            "scheduler.event_dispatched",
            event_type=normalized_event_type,
            matched_schedule_count=len(results),
        )
        return results

    async def _resolve_schedule_goal(
            self,
            schedule: ScheduleDefinition,
            *,
            scheduled_fire_at: datetime | None,
            now: datetime,
    ) -> GoalDefinition | None:
        goal_config = schedule.metadata.get("goal") if isinstance(schedule.metadata, dict) else None
        if not isinstance(goal_config, dict):
            return None
        mode = str(goal_config.get("mode") or "").strip().lower()
        if mode not in {"create", "continue"}:
            return None
        if self.goal_repo is None:
            raise RuntimeError("Schedule goal metadata requires a configured goal repository")

        if mode == "continue":
            goal_id = str(goal_config.get("goal_id") or schedule.metadata.get("last_goal_id") or "").strip()
            if not goal_id:
                raise ValueError(f"Schedule '{schedule.id}' is configured to continue a goal but no goal_id is set")
            goal = await self.goal_repo.get(goal_id)
            if goal is None:
                raise ValueError(f"Goal '{goal_id}' configured for schedule '{schedule.id}' was not found")
            return goal

        objective = str(goal_config.get("objective") or schedule.name).strip()
        success_criteria = goal_config.get("success_criteria")
        if not isinstance(success_criteria, list) or not success_criteria:
            success_criteria = [
                {
                    "kind": "scheduled_workflow_execution",
                    "description": f"Scheduled workflow '{schedule.workflow_id}' produces completion evidence.",
                }
            ]
        metadata = {
            "created_by_schedule": {
                "schedule_id": schedule.id,
                "workflow_id": schedule.workflow_id,
                "scheduled_fire_at": ensure_utc(scheduled_fire_at).isoformat() if scheduled_fire_at else None,
                "triggered_at": now.isoformat(),
            }
        }
        if isinstance(goal_config.get("metadata"), dict):
            metadata.update(goal_config["metadata"])
        return GoalDefinition(
            objective=objective,
            status=GoalStatus.ACTIVE,
            priority=str(goal_config.get("priority") or "normal"),
            owner_actor=goal_config.get("owner_actor"),
            parent_goal_id=goal_config.get("parent_goal_id"),
            success_criteria=success_criteria,
            constraints=goal_config.get("constraints") if isinstance(goal_config.get("constraints"), dict) else {},
            deadline_at=goal_config.get("deadline_at"),
            metadata=metadata,
        )

    async def _link_execution_to_goal(self, goal: GoalDefinition, execution_id: str) -> GoalDefinition:
        if execution_id in goal.execution_ids:
            return goal
        return await self.goal_repo.save(
            goal.model_copy(
                update={
                    "execution_ids": [*goal.execution_ids, execution_id],
                    "updated_at": utc_now(),
                }
            )
        )

    def _schedule_metadata_after_fire(
            self,
            schedule: ScheduleDefinition,
            execution_id: str,
            goal_id: str | None,
    ) -> dict[str, Any]:
        metadata = {**schedule.metadata, "last_execution_id": execution_id}
        if goal_id:
            metadata["last_goal_id"] = goal_id
        return metadata

    async def _maybe_wake_goal_supervisor(
            self,
            schedule: ScheduleDefinition,
            *,
            goal_id: str | None,
            source: str,
    ) -> None:
        if not goal_id or self.goal_supervisor_waker is None:
            return
        supervisor = schedule.metadata.get("goal_supervisor") if isinstance(schedule.metadata, dict) else None
        supervisor = supervisor if isinstance(supervisor, dict) else {}
        if supervisor.get("wake_on_event") is not True and supervisor.get("wake_on_fire") is not True:
            return
        await self.goal_supervisor_waker(goal_id)
        self._record_operation(
            "scheduler.goal_supervisor_woken",
            schedule_id=schedule.id,
            goal_id=goal_id,
            source=source,
        )

    def _schedule_matches_event(self, schedule: ScheduleDefinition, event_type: str, payload: dict[str, Any]) -> bool:
        config = schedule.trigger_config if isinstance(schedule.trigger_config, dict) else {}
        configured_types = config.get("event_types")
        if configured_types is None:
            configured_types = config.get("event_type")
        allowed = self._normalized_event_types(configured_types)
        if allowed and event_type not in allowed:
            return False
        payload_matches = config.get("payload_matches")
        if isinstance(payload_matches, dict):
            for key, expected in payload_matches.items():
                if self._value_at_path(payload, str(key)) != expected:
                    return False
        return True

    @staticmethod
    def _normalized_event_types(value: Any) -> set[str]:
        if isinstance(value, str):
            return {value.strip()} if value.strip() else set()
        if isinstance(value, list):
            return {str(item).strip() for item in value if str(item).strip()}
        return set()

    @staticmethod
    def _value_at_path(payload: dict[str, Any], path: str) -> Any:
        current: Any = payload
        for part in path.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

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
            except WorkflowNotFoundError as exc:
                logger.warning(
                    "Disabled schedule '%s' because workflow '%s' was not found while firing %s",
                    schedule.id,
                    schedule.workflow_id,
                    scheduled_fire_at.isoformat(),
                )
                self._record_operation(
                    "scheduler.due_fire_missing_workflow",
                    schedule_id=schedule.id,
                    workflow_id=schedule.workflow_id,
                    scheduled_fire_at=scheduled_fire_at.isoformat(),
                    error_type=exc.__class__.__name__,
                    error=str(exc),
                )
                continue
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
