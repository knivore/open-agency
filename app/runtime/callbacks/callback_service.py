"""Internal callback service for sub-agent checkpoint events."""

from __future__ import annotations

from typing import Any, Protocol

from app.core.time import utc_now
from app.domain import ExecutionEventType, ExecutionStatus, SubAgentStatusUpdate
from app.runtime.events.factory import RuntimeEventEnvelope, RuntimeEventStatus, \
    create_execution_event_from_runtime_event
from app.runtime.native.errors import ExecutionNotFoundError
from .callback_schemas import CallbackReceipt, SubAgentCallbackPayload


class RuntimeDispatcher(Protocol):
    async def pause(self, execution_id: str): ...

    async def resume(self, execution_id: str): ...


class OutboundWebhookNotifier(Protocol):
    async def send(
            self,
            *,
            target: str,
            event_type: str,
            payload: dict[str, Any],
            idempotency_key: str | None = None,
            run_id: str | None = None,
            workflow_id: str | None = None,
    ): ...


class SubAgentCallbackService:
    def __init__(
            self,
            *,
            execution_store: Any,
            dispatcher: RuntimeDispatcher | None = None,
            workflow_repository: Any | None = None,
            webhook_client: OutboundWebhookNotifier | None = None,
    ):
        self.execution_store = execution_store
        self.dispatcher = dispatcher
        self.workflow_repository = workflow_repository
        self.webhook_client = webhook_client

    async def record_subagent_progress(
            self,
            *,
            run_id: str,
            step_id: str,
            agent_id: str,
            workflow_id: str | None = None,
            payload: dict[str, Any] | None = None,
            source: str | None = None,
            idempotency_key: str | None = None,
    ) -> CallbackReceipt:
        return await self._record(
            callback=SubAgentCallbackPayload(
                run_id=run_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                step_id=step_id,
                source=source,
                payload=payload or {},
                idempotency_key=idempotency_key,
            ),
            event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
            event_status=RuntimeEventStatus.RUNNING,
            checkpoint_status="running",
        )

    async def record_subagent_status(
            self,
            *,
            run_id: str,
            step_id: str,
            agent_id: str,
            workflow_id: str | None = None,
            status: str,
            current_task: str | None = None,
            completed_step: str | None = None,
            blocker: str | None = None,
            clarification_needed: str | None = None,
            confidence: float | None = None,
            token_usage: dict[str, Any] | None = None,
            context_health: dict[str, Any] | None = None,
            tool_result_summary: str | None = None,
            next_action: str | None = None,
            progress_percent: float | None = None,
            payload: dict[str, Any] | None = None,
            source: str | None = None,
            idempotency_key: str | None = None,
    ) -> CallbackReceipt:
        status_payload = dict(payload or {})
        status_payload.update(
            {
                "status": status,
                "current_task": current_task,
                "completed_step": completed_step,
                "blocker": blocker,
                "clarification_needed": clarification_needed,
                "confidence": confidence,
                "token_usage": token_usage,
                "context_health": context_health,
                "tool_result_summary": tool_result_summary,
                "next_action": next_action,
                "progress_percent": progress_percent,
            }
        )
        status_payload = {key: value for key, value in status_payload.items() if value is not None}
        return await self.record_subagent_progress(
            run_id=run_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
            step_id=step_id,
            payload=status_payload,
            source=source,
            idempotency_key=idempotency_key,
        )

    async def record_subagent_completed(
            self,
            *,
            run_id: str,
            step_id: str,
            agent_id: str,
            workflow_id: str | None = None,
            payload: dict[str, Any] | None = None,
            source: str | None = None,
            idempotency_key: str | None = None,
    ) -> CallbackReceipt:
        return await self._record(
            callback=SubAgentCallbackPayload(
                run_id=run_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                step_id=step_id,
                source=source,
                payload=payload or {},
                idempotency_key=idempotency_key,
            ),
            event_type=ExecutionEventType.SUBAGENT_STEP_COMPLETED,
            event_status=RuntimeEventStatus.COMPLETED,
            checkpoint_status="completed",
            dispatcher_action="resume",
            dispatcher_condition="dependencies_satisfied",
        )

    async def record_subagent_failed(
            self,
            *,
            run_id: str,
            step_id: str,
            agent_id: str,
            workflow_id: str | None = None,
            payload: dict[str, Any] | None = None,
            source: str | None = None,
            idempotency_key: str | None = None,
    ) -> CallbackReceipt:
        return await self._record(
            callback=SubAgentCallbackPayload(
                run_id=run_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                step_id=step_id,
                source=source,
                payload=payload or {},
                idempotency_key=idempotency_key,
            ),
            event_type=ExecutionEventType.SUBAGENT_STEP_FAILED,
            event_status=RuntimeEventStatus.FAILED,
            checkpoint_status="failed",
            dispatcher_action="resume",
            dispatcher_condition="retry_available",
        )

    async def record_subagent_needs_input(
            self,
            *,
            run_id: str,
            step_id: str,
            agent_id: str,
            workflow_id: str | None = None,
            payload: dict[str, Any] | None = None,
            source: str | None = None,
            idempotency_key: str | None = None,
    ) -> CallbackReceipt:
        return await self._record(
            callback=SubAgentCallbackPayload(
                run_id=run_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                step_id=step_id,
                source=source,
                payload=payload or {},
                idempotency_key=idempotency_key,
            ),
            event_type=ExecutionEventType.SUBAGENT_NEEDS_INPUT,
            event_status=RuntimeEventStatus.QUEUED,
            checkpoint_status="needs_input",
            execution_status=ExecutionStatus.PAUSED,
            dispatcher_action="pause",
        )

    async def record_subagent_needs_approval(
            self,
            *,
            run_id: str,
            step_id: str,
            agent_id: str,
            workflow_id: str | None = None,
            payload: dict[str, Any] | None = None,
            source: str | None = None,
            idempotency_key: str | None = None,
    ) -> CallbackReceipt:
        return await self._record(
            callback=SubAgentCallbackPayload(
                run_id=run_id,
                workflow_id=workflow_id,
                agent_id=agent_id,
                step_id=step_id,
                source=source,
                payload=payload or {},
                idempotency_key=idempotency_key,
            ),
            event_type=ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
            event_status=RuntimeEventStatus.QUEUED,
            checkpoint_status="needs_approval",
            execution_status=ExecutionStatus.WAITING_FOR_APPROVAL,
            dispatcher_action="pause",
        )

    async def _record(
            self,
            *,
            callback: SubAgentCallbackPayload,
            event_type: ExecutionEventType,
            event_status: RuntimeEventStatus,
            checkpoint_status: str,
            execution_status: ExecutionStatus | None = None,
            dispatcher_action: str | None = None,
            dispatcher_condition: str | None = None,
    ) -> CallbackReceipt:
        execution = await self.execution_store.get_execution(callback.run_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{callback.run_id}' was not found")

        existing = self._existing_idempotent_receipt(execution.metadata, callback.idempotency_key)
        if existing is not None:
            return existing

        source = callback.source or f"subagent:{callback.agent_id}"
        previous_checkpoint = self._previous_checkpoint(execution.metadata, callback.step_id)
        checkpoint_status, retry_state = self._status_after_retry_policy(
            execution.metadata,
            callback=callback,
            requested_status=checkpoint_status,
        )
        event_payload = self._event_payload(callback)
        event_payload["supervisor_recovery"] = self._supervisor_recovery_context(
            callback=callback,
            execution=execution,
            event_type=event_type.value,
            source=source,
            status=checkpoint_status,
            previous_checkpoint=previous_checkpoint,
            retry_state=retry_state,
        )
        envelope = RuntimeEventEnvelope(
            event_type=event_type,
            run_id=callback.run_id,
            workflow_id=callback.workflow_id or execution.workflow_id,
            agent_id=callback.agent_id,
            step_id=callback.step_id,
            source=source,
            status=event_status,
            payload=event_payload,
        )
        event = create_execution_event_from_runtime_event(envelope)
        saved = await self.execution_store.save_event(event)

        ready_dependent_step_ids = await self._ready_dependent_step_ids(
            execution,
            completed_step_id=callback.step_id,
            checkpoint_status=checkpoint_status,
        )
        if execution_status is not None:
            execution.status = execution_status
        execution.metadata = self._checkpoint_metadata(
            execution.metadata,
            callback=callback,
            event_id=saved.id,
            event_type=saved.event_type.value,
            source=source,
            status=checkpoint_status,
            payload_sha256=saved.payload_sha256,
            idempotency_key=callback.idempotency_key,
            created_at=saved.timestamp.isoformat(),
            retry_state=retry_state,
            previous_checkpoint=previous_checkpoint,
            ready_dependent_step_ids=ready_dependent_step_ids,
        )
        await self.execution_store.update_execution(execution)

        await self._notify_dispatcher(
            dispatcher_action,
            callback.run_id,
            should_dispatch=self._should_dispatch(
                dispatcher_condition=dispatcher_condition,
                checkpoint_status=checkpoint_status,
                ready_dependent_step_ids=ready_dependent_step_ids,
            ),
        )
        await self._notify_outbound_webhook(
            execution=execution,
            callback=callback,
            event_type=saved.event_type.value,
            checkpoint_status=checkpoint_status,
            payload_sha256=saved.payload_sha256,
        )
        return CallbackReceipt(event_id=saved.id, run_id=callback.run_id, step_id=callback.step_id,
                               created_at=saved.timestamp)

    @staticmethod
    def _event_payload(callback: SubAgentCallbackPayload) -> dict[str, Any]:
        payload = dict(callback.payload or {})
        if callback.status_update is None:
            return payload
        normalized = callback.status_update.model_dump(mode="json", exclude_none=True)
        payload["status_update"] = normalized
        for key, value in normalized.items():
            payload[key] = value
        return payload

    def _existing_idempotent_receipt(
            self,
            metadata: dict[str, Any] | None,
            idempotency_key: str | None,
    ) -> CallbackReceipt | None:
        if not idempotency_key:
            return None
        runtime_callbacks = (metadata or {}).get("runtime_callbacks")
        if not isinstance(runtime_callbacks, dict):
            return None
        idempotency = runtime_callbacks.get("idempotency")
        if not isinstance(idempotency, dict):
            return None
        receipt = idempotency.get(idempotency_key)
        if not isinstance(receipt, dict):
            return None
        return CallbackReceipt.model_validate(receipt)

    def _checkpoint_metadata(
            self,
            metadata: dict[str, Any] | None,
            *,
            callback: SubAgentCallbackPayload,
            event_id: str,
            event_type: str,
            source: str,
            status: str,
            payload_sha256: str | None,
            idempotency_key: str | None,
            created_at: str,
            retry_state: dict[str, Any] | None = None,
            ready_dependent_step_ids: list[str] | None = None,
            previous_checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        updated = dict(metadata or {})
        runtime_callbacks = dict(updated.get("runtime_callbacks") or {})
        checkpoints = dict(runtime_callbacks.get("checkpoints") or {})
        checkpoint = {
            "agent_id": callback.agent_id,
            "step_id": callback.step_id,
            "status": status,
            "event_id": event_id,
            "event_type": event_type,
            "source": source,
            "payload_sha256": payload_sha256,
            "updated_at": created_at,
        }
        status_update = self._checkpoint_status_update(callback.status_update)
        if status_update:
            checkpoint.update(status_update)
        if retry_state:
            checkpoint["retry"] = retry_state
        checkpoint["supervisor_recovery"] = self._supervisor_recovery_context(
            callback=callback,
            execution=None,
            event_type=event_type,
            source=source,
            status=status,
            previous_checkpoint=previous_checkpoint,
            retry_state=retry_state,
            event_id=event_id,
            payload_sha256=payload_sha256,
            updated_at=created_at,
        )
        if ready_dependent_step_ids:
            checkpoint["ready_dependent_step_ids"] = ready_dependent_step_ids
        checkpoints[callback.step_id] = checkpoint
        runtime_callbacks["checkpoints"] = checkpoints
        runtime_callbacks["last_event_id"] = event_id
        runtime_callbacks["last_event_type"] = event_type
        runtime_callbacks["updated_at"] = utc_now().isoformat()
        if idempotency_key:
            idempotency = dict(runtime_callbacks.get("idempotency") or {})
            idempotency[idempotency_key] = CallbackReceipt(
                event_id=event_id,
                run_id=callback.run_id,
                step_id=callback.step_id,
                created_at=created_at,
            ).model_dump(mode="json")
            runtime_callbacks["idempotency"] = idempotency
        updated["runtime_callbacks"] = runtime_callbacks
        if status == "needs_input":
            updated["pending_subagent_input"] = checkpoint
        if status == "needs_approval":
            updated["pending_subagent_approval"] = checkpoint
        return updated

    @staticmethod
    def _previous_checkpoint(metadata: dict[str, Any] | None, step_id: str) -> dict[str, Any] | None:
        runtime_callbacks = (metadata or {}).get("runtime_callbacks")
        checkpoints = runtime_callbacks.get("checkpoints") if isinstance(runtime_callbacks, dict) else {}
        previous = checkpoints.get(step_id) if isinstance(checkpoints, dict) else None
        if not isinstance(previous, dict):
            return None
        return {
            key: previous.get(key)
            for key in (
                "agent_id",
                "step_id",
                "status",
                "event_id",
                "event_type",
                "updated_at",
                "subagent_status",
                "current_task",
                "blocker",
                "next_action",
                "progress_percent",
            )
            if previous.get(key) is not None
        }

    def _supervisor_recovery_context(
            self,
            *,
            callback: SubAgentCallbackPayload,
            execution,
            event_type: str,
            source: str,
            status: str,
            previous_checkpoint: dict[str, Any] | None,
            retry_state: dict[str, Any] | None,
            event_id: str | None = None,
            payload_sha256: str | None = None,
            updated_at: str | None = None,
    ) -> dict[str, Any]:
        status_update = self._checkpoint_status_update(callback.status_update)
        context = {
            "run_id": callback.run_id,
            "workflow_id": callback.workflow_id or getattr(execution, "workflow_id", None),
            "agent_id": callback.agent_id,
            "step_id": callback.step_id,
            "status": status,
            "event_type": event_type,
            "source": source,
            "payload_keys": sorted(str(key) for key in (callback.payload or {}).keys()),
            "previous_checkpoint": previous_checkpoint,
        }
        if event_id is not None:
            context["event_id"] = event_id
        if payload_sha256 is not None:
            context["payload_sha256"] = payload_sha256
        if updated_at is not None:
            context["updated_at"] = updated_at
        if status_update:
            context["status_update"] = status_update.get("status_update", {})
            for key in (
                    "subagent_status",
                    "current_task",
                    "completed_step",
                    "blocker",
                    "clarification_needed",
                    "confidence",
                    "token_usage",
                    "context_health",
                    "tool_result_summary",
                    "next_action",
                    "progress_percent",
            ):
                if key in status_update:
                    context[key] = status_update[key]
        if retry_state:
            context["retry"] = retry_state
        return {key: value for key, value in context.items() if value is not None}

    @staticmethod
    def _checkpoint_status_update(status_update: SubAgentStatusUpdate | None) -> dict[str, Any]:
        if status_update is None:
            return {}
        normalized = status_update.model_dump(mode="json", exclude_none=True)
        checkpoint_update: dict[str, Any] = {"status_update": normalized}
        if "status" in normalized:
            checkpoint_update["subagent_status"] = normalized["status"]
        for key in (
                "current_task",
                "completed_step",
                "blocker",
                "clarification_needed",
                "confidence",
                "token_usage",
                "context_health",
                "tool_result_summary",
                "next_action",
                "progress_percent",
        ):
            if key in normalized:
                checkpoint_update[key] = normalized[key]
        return checkpoint_update

    def _status_after_retry_policy(
            self,
            metadata: dict[str, Any] | None,
            *,
            callback: SubAgentCallbackPayload,
            requested_status: str,
    ) -> tuple[str, dict[str, Any] | None]:
        if requested_status != "failed":
            return requested_status, None
        policy = self._retry_policy_for(metadata, callback)
        max_retries = int(policy.get("max_retries") or 0) if policy else 0
        if max_retries <= 0:
            return requested_status, None
        runtime_callbacks = (metadata or {}).get("runtime_callbacks")
        checkpoints = runtime_callbacks.get("checkpoints") if isinstance(runtime_callbacks, dict) else {}
        previous = checkpoints.get(callback.step_id) if isinstance(checkpoints, dict) else {}
        previous_retry = previous.get("retry") if isinstance(previous, dict) else {}
        attempts = int(previous_retry.get("attempts") or 0) + 1 if isinstance(previous_retry, dict) else 1
        retry_state = {
            "attempts": attempts,
            "max_retries": max_retries,
            "retry_available": attempts <= max_retries,
            "reason": callback.payload.get("error") or callback.payload.get("reason"),
        }
        if attempts <= max_retries:
            return "retry_queued", retry_state
        return requested_status, retry_state

    def _retry_policy_for(
            self,
            metadata: dict[str, Any] | None,
            callback: SubAgentCallbackPayload,
    ) -> dict[str, Any]:
        payload_policy = callback.payload.get("retry_policy")
        if isinstance(payload_policy, dict):
            return payload_policy
        runtime_callbacks = (metadata or {}).get("runtime_callbacks")
        if isinstance(runtime_callbacks, dict):
            retry_policies = runtime_callbacks.get("retry_policies")
            if isinstance(retry_policies, dict):
                step_policy = retry_policies.get(callback.step_id)
                if isinstance(step_policy, dict):
                    return step_policy
            retry_policy = runtime_callbacks.get("retry_policy")
            if isinstance(retry_policy, dict):
                return retry_policy
        return {}

    async def _ready_dependent_step_ids(
            self,
            execution,
            *,
            completed_step_id: str,
            checkpoint_status: str,
    ) -> list[str]:
        if checkpoint_status != "completed":
            return []
        workflow = None
        if self.workflow_repository is not None:
            getter = getattr(self.workflow_repository, "get_workflow", None) or getattr(self.workflow_repository, "get",
                                                                                        None)
            if getter is not None:
                workflow = await getter(execution.workflow_id)
        if workflow is None:
            return []
        runtime_callbacks = execution.metadata.get("runtime_callbacks") if isinstance(execution.metadata, dict) else {}
        checkpoints = runtime_callbacks.get("checkpoints") if isinstance(runtime_callbacks, dict) else {}
        ready: list[str] = []
        for task in getattr(workflow, "task_definitions", []) or []:
            depends_on = list(getattr(task, "depends_on_task_ids", []) or [])
            if completed_step_id not in depends_on:
                continue
            if all(
                    dependency_id == completed_step_id
                    or (
                            isinstance(checkpoints, dict)
                            and checkpoints.get(dependency_id, {}).get("status") == "completed"
                    )
                    for dependency_id in depends_on
            ):
                ready.append(task.id)
        return ready

    def _should_dispatch(
            self,
            *,
            dispatcher_condition: str | None,
            checkpoint_status: str,
            ready_dependent_step_ids: list[str],
    ) -> bool:
        if dispatcher_condition is None:
            return True
        if dispatcher_condition == "retry_available":
            return checkpoint_status == "retry_queued"
        if dispatcher_condition == "dependencies_satisfied":
            return True
        return True

    async def _notify_dispatcher(self, action: str | None, run_id: str, *, should_dispatch: bool = True) -> None:
        if self.dispatcher is None or action is None or not should_dispatch:
            return
        handler = getattr(self.dispatcher, action, None)
        if handler is None:
            return
        await handler(run_id)

    async def _notify_outbound_webhook(
            self,
            *,
            execution,
            callback: SubAgentCallbackPayload,
            event_type: str,
            checkpoint_status: str,
            payload_sha256: str | None,
    ) -> None:
        if self.webhook_client is None:
            return
        target = self._webhook_target_for(execution.metadata, callback, event_type=event_type,
                                          status=checkpoint_status)
        if not target:
            return
        try:
            await self.webhook_client.send(
                target=target,
                event_type=event_type,
                payload={
                    "run_id": callback.run_id,
                    "workflow_id": callback.workflow_id or execution.workflow_id,
                    "agent_id": callback.agent_id,
                    "step_id": callback.step_id,
                    "status": checkpoint_status,
                    "payload": self._event_payload(callback),
                    "payload_sha256": payload_sha256,
                },
                idempotency_key=f"{callback.run_id}:{callback.step_id}:{event_type}",
                run_id=callback.run_id,
                workflow_id=callback.workflow_id or execution.workflow_id,
            )
        except Exception:
            return

    def _webhook_target_for(
            self,
            metadata: dict[str, Any] | None,
            callback: SubAgentCallbackPayload,
            *,
            event_type: str,
            status: str,
    ) -> str | None:
        payload_target = callback.payload.get("outbound_webhook_target")
        if isinstance(payload_target, str) and payload_target.strip():
            return payload_target.strip()
        payload_config = callback.payload.get("outbound_webhook")
        if isinstance(payload_config, dict):
            target = payload_config.get("target")
            if isinstance(target, str) and target.strip():
                return target.strip()
        runtime_callbacks = (metadata or {}).get("runtime_callbacks")
        if isinstance(runtime_callbacks, dict):
            outbound = runtime_callbacks.get("outbound_webhooks")
            if isinstance(outbound, dict):
                for key in (event_type, status, "default"):
                    target = outbound.get(key)
                    if isinstance(target, str) and target.strip():
                        return target.strip()
                    if isinstance(target, dict) and isinstance(target.get("target"), str):
                        return target["target"].strip()
        return None
