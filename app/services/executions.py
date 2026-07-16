"""Service facade for execution lifecycle and runtime observability APIs.

Routes call `ExecutionService` to keep HTTP concerns separate from runtime
adapter selection, control-plane commands, event formatting, artifact retrieval,
and operational metrics.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.storage import return_file_from_s3
from app.domain import (
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    ModelProfileDefinition,
    RuntimeAdapterType,
    UserDefinition,
    WorkflowDefinition,
)
from app.runtime.channels import agent_output_channel, human_reply_channel
from app.runtime.native.errors import ExecutionNotFoundError, WorkflowNotFoundError
from app.runtime.native.planner import LinearWorkflowPlanner
from app.runtime.process_supervisor import execution_process_manager
from app.services.execution_classification import classify_execution_staleness
from app.services.execution_waits import ExecutionWaitService
from app.services.goals import GoalService
from app.services.memory import MemoryService

AGENT_EVENT_TYPES = {
    ExecutionEventType.AGENT_MESSAGE_CREATED,
    ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
    ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    ExecutionEventType.CONTEXT_COMPACTION_STARTED,
    ExecutionEventType.CONTEXT_HEALTH_RECORDED,
    ExecutionEventType.LLM_REQUEST_CREATED,
    ExecutionEventType.LLM_RESPONSE_CREATED,
    ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
    ExecutionEventType.SUBAGENT_NEEDS_INPUT,
    ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
    ExecutionEventType.SUBAGENT_STEP_COMPLETED,
    ExecutionEventType.SUBAGENT_STEP_FAILED,
    ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
    ExecutionEventType.SUPERVISOR_STEERING_APPLIED,
    ExecutionEventType.TASK_STARTED,
    ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
    ExecutionEventType.TOKEN_BUDGET_WARNING,
    ExecutionEventType.TOKEN_USAGE_RECORDED,
    ExecutionEventType.TOOL_CALL_STARTED,
    ExecutionEventType.TOOL_CALL_COMPLETED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.APPROVAL_REQUESTED,
    ExecutionEventType.APPROVAL_GRANTED,
    ExecutionEventType.APPROVAL_REJECTED,
}

ERROR_EVENT_TYPES = {
    ExecutionEventType.CONTEXT_COMPACTION_FAILED,
    ExecutionEventType.EXECUTION_FAILED,
    ExecutionEventType.EXECUTION_CYCLE_FAILED,
    ExecutionEventType.SUBAGENT_STEP_FAILED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.CONTAINER_FAILED,
    ExecutionEventType.RUNTIME_BUILD_FAILED,
    ExecutionEventType.APPROVAL_REJECTED,
}

WARN_EVENT_TYPES = {
    ExecutionEventType.APPROVAL_REQUESTED,
    ExecutionEventType.EXECUTION_PAUSED,
    ExecutionEventType.EXECUTION_CANCELLED,
    ExecutionEventType.EXECUTION_CYCLE_GUARD_TRIGGERED,
    ExecutionEventType.CONTAINER_REPLACED,
    ExecutionEventType.CONTEXT_COMPACTION_STARTED,
    ExecutionEventType.MONITOR_FINDING_CREATED,
    ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
    ExecutionEventType.SUPERVISOR_STEERING_REQUESTED,
    ExecutionEventType.SUBAGENT_NEEDS_APPROVAL,
    ExecutionEventType.SUBAGENT_NEEDS_INPUT,
    ExecutionEventType.TOKEN_BUDGET_WARNING,
    ExecutionEventType.TOKEN_BUDGET_EXCEEDED,
}


@dataclass(slots=True)
class ExecutionService:
    """Coordinate execution CRUD, control actions, logs, artifacts, and runtime status."""

    context: ApiContext
    return_file_from_s3_func: Any = return_file_from_s3
    redis_client: Any = execution_process_manager.redis_client

    async def list_executions(
            self,
            *,
            workflow_id: str | None = None,
            agent_id: str | None = None,
            statuses: list[str] | None = None,
            active_only: bool = False,
            limit: int | None = None,
    ) -> dict[str, Any]:
        # The main agent needs a bounded discovery surface before it can drill into
        # a specific run with execution.get/events/artifacts.
        if workflow_id:
            executions = await self.context.execution_store.list_executions_by_workflow(workflow_id)
        elif agent_id:
            executions = await self.context.execution_store.list_executions_by_agent(agent_id)
        elif active_only:
            executions = await self.context.execution_store.list_active_executions()
        else:
            executions = await self.context.execution_store.list_executions()

        normalized_statuses = {item.strip().lower() for item in statuses or [] if
                               isinstance(item, str) and item.strip()}
        if normalized_statuses:
            executions = [item for item in executions if item.status.value.lower() in normalized_statuses]

        executions = sorted(
            executions,
            key=lambda item: item.created_at,
            reverse=True,
        )
        if isinstance(limit, int) and limit > 0:
            executions = executions[:limit]

        return {
            "items": [self._execution_payload(execution) for execution in executions],
            "count": len(executions),
            "filters": {
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "status": sorted(normalized_statuses) if normalized_statuses else [],
                "active_only": active_only,
                "limit": limit,
            },
        }

    async def list_active_executions(self) -> dict[str, list[dict[str, Any]]]:
        items = await self.context.execution_store.list_active_executions()
        return {"items": [self._execution_payload(item) for item in items]}

    async def create_execution(
            self,
            workflow_id: str,
            input_payload: dict[str, Any],
            trigger: dict[str, Any],
            goal_id: str | None = None,
            context_pack_id: str | None = None,
            runtime_adapter_id: str | None = None,
            execution_host: str | None = None,
            workflow_definition: WorkflowDefinition | None = None,
            model_profiles: list[ModelProfileDefinition] | None = None,
            current_user: UserDefinition | None = None,
    ) -> dict[str, Any]:
        # Frontend BFF routes may pre-shape launch requests with authenticated user
        # context, but execution creation still converges here so adapter selection,
        # trigger metadata, and runtime persistence stay backend-owned.
        await self.context.ensure_runtime_adapter_seed_data()
        if workflow_definition is not None:
            await self.context.runtime_registry.register_workflow(workflow_definition)
            workflow_id = workflow_definition.id
        for profile in model_profiles or []:
            await self.context.runtime_registry.register_model_profile(profile)

        input_payload = dict(input_payload or {})
        trigger = dict(trigger or {})
        if current_user is not None:
            # Actor identity is derived from the authenticated principal. Trigger
            # metadata remains descriptive input and cannot impersonate another user.
            trigger.pop("run_by", None)
            trigger["created_by"] = current_user.id
        if goal_id:
            goal = await GoalService(self.context).get_goal(goal_id)
            trigger["goal_id"] = goal.id
            input_payload.setdefault("goal_id", goal.id)
        if execution_host:
            trigger = {**trigger, "execution_host": execution_host}
        selected_context_pack_id = self._selected_context_pack_id(
            context_pack_id=context_pack_id,
            input_payload=input_payload,
            trigger=trigger,
        )
        if selected_context_pack_id:
            context_pack = await MemoryService(self.context).get_context_pack_by_id(
                selected_context_pack_id,
                current_user=current_user,
            )
            if context_pack is None:
                raise ValueError(f"Context pack '{selected_context_pack_id}' was not found or is not readable.")
            input_payload.setdefault("context_pack_id", context_pack.id)
            trigger["context_pack_id"] = context_pack.id
            trigger["context_pack"] = {
                "id": context_pack.id,
                "scope": context_pack.scope.value,
                "mode": context_pack.metadata.get("mode"),
                "summary": context_pack.summary,
                "source_conversation_id": context_pack.source_conversation_id,
            }

        execution = await self.context.runtime_registry.create_execution(
            workflow_id,
            input_payload,
            trigger,
            runtime_adapter_id=runtime_adapter_id,
            goal_id=goal_id,
        )
        if goal_id:
            await GoalService(self.context).link_execution(goal_id, execution.id)
        return execution.model_dump(mode="json")

    @staticmethod
    def _selected_context_pack_id(
            *,
            context_pack_id: str | None,
            input_payload: dict[str, Any],
            trigger: dict[str, Any],
    ) -> str | None:
        for candidate in (
                context_pack_id,
                input_payload.get("context_pack_id"),
                input_payload.get("contextPackId"),
                trigger.get("context_pack_id"),
                trigger.get("contextPackId"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    async def queue_start(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.queue_start(execution_id)).model_dump(mode="json")

    async def pause(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.pause(execution_id)).model_dump(mode="json")

    async def resume(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.resume(execution_id)).model_dump(mode="json")

    async def retry_task(
            self,
            execution_id: str,
            task_id: str,
            *,
            reason: str | None = None,
            actor: str | None = None,
    ) -> dict[str, Any]:
        source_execution = await self.context.execution_store.get_execution(execution_id)
        if source_execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if source_execution.runtime_adapter_id != RuntimeAdapterType.NATIVE.value:
            raise ValueError("Task retry is currently supported only for native runtime executions.")
        if source_execution.status != ExecutionStatus.FAILED:
            raise ValueError("Task retry requires a failed source execution.")

        workflow = await self.context.workflow_repo.get(source_execution.workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{source_execution.workflow_id}' was not found")
        task = next((candidate for candidate in workflow.task_definitions if candidate.id == task_id), None)
        if task is None:
            raise WorkflowNotFoundError(f"Task '{task_id}' was not found in workflow '{workflow.id}'")
        retry_node = next(
            (
                node
                for node in workflow.nodes
                if node.node_type == "task" and (node.task_id == task_id or node.id == task_id)
            ),
            None,
        )
        if retry_node is None:
            raise WorkflowNotFoundError(f"Task '{task_id}' does not have a workflow graph node.")

        events = await self.context.execution_store.list_events(execution_id)
        has_failed_task_event = any(
            event.task_id == task_id and event.event_type == ExecutionEventType.AGENT_STEP_FAILED
            for event in events
        )
        snapshot = await self.context.runtime_registry.get_execution_state(execution_id)
        state = snapshot.state
        current_task_id = getattr(state, "current_task_id", None) if state is not None else None
        if not has_failed_task_event and current_task_id != task_id:
            raise ValueError(f"Task '{task_id}' does not have failed runtime evidence on execution '{execution_id}'.")

        source_node_outputs = {}
        if state is not None and isinstance(getattr(state, "node_outputs", None), dict):
            source_node_outputs = dict(state.node_outputs)
        elif isinstance(source_execution.output_payload, dict):
            node_outputs = source_execution.output_payload.get("node_outputs")
            if isinstance(node_outputs, dict):
                source_node_outputs = dict(node_outputs)
        prior_node_outputs = {
            node_id: output
            for node_id, output in source_node_outputs.items()
            if node_id != retry_node.id
        }
        trigger = {
            **(source_execution.trigger_payload or {}),
            "type": "task_retry",
            "source_execution_id": execution_id,
            "task_id": task_id,
            "node_id": retry_node.id,
            "reason": reason,
            "created_by": actor or source_execution.created_by,
        }
        replacement = await self.context.runtime_registry.create_execution(
            source_execution.workflow_id,
            dict(source_execution.input_payload or {}),
            trigger,
            runtime_adapter_id=RuntimeAdapterType.NATIVE.value,
            goal_id=source_execution.goal_id,
        )
        replacement.replacement_of_execution_id = source_execution.id
        replacement.restart_reason = reason or f"Retry task '{task.name or task.id}'"
        replacement.metadata = {
            **(replacement.metadata or {}),
            "task_retry": {
                "source_execution_id": execution_id,
                "task_id": task_id,
                "node_id": retry_node.id,
                "reason": reason,
                "prior_node_outputs": prior_node_outputs,
            },
        }
        await self.context.execution_store.update_execution(replacement)
        if replacement.goal_id:
            await GoalService(self.context).link_execution(replacement.goal_id, replacement.id)
        queued = await self.context.control_plane.queue_start(replacement.id)
        return {
            "status": "queued",
            "source_execution_id": execution_id,
            "replacement_execution_id": queued.id,
            "task_id": task_id,
            "node_id": retry_node.id,
            "execution": queued.model_dump(mode="json"),
        }

    async def resume_from_checkpoint(
            self,
            execution_id: str,
            *,
            reason: str | None = None,
            actor: str | None = None,
    ) -> dict[str, Any]:
        source_execution = await self.context.execution_store.get_execution(execution_id)
        if source_execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if source_execution.runtime_adapter_id != RuntimeAdapterType.NATIVE.value:
            raise ValueError("Checkpoint resume is currently supported only for native runtime executions.")
        if source_execution.status not in {ExecutionStatus.FAILED, ExecutionStatus.PAUSED, ExecutionStatus.CANCELLED}:
            raise ValueError("Checkpoint resume requires a failed, paused, or cancelled source execution.")

        workflow = await self.context.workflow_repo.get(source_execution.workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{source_execution.workflow_id}' was not found")

        snapshot = await self.context.runtime_registry.get_execution_state(execution_id)
        state = snapshot.state
        source_node_outputs = self._source_node_outputs(source_execution, state)
        if not source_node_outputs:
            raise ValueError(f"Execution '{execution_id}' has no completed node outputs to resume from.")

        ordered_task_nodes = [
            node
            for node in LinearWorkflowPlanner().order_nodes(workflow)
            if node.node_type == "task"
        ]
        completed_node_ids = {
            node_id
            for node_id in source_node_outputs
            if isinstance(node_id, str)
        }
        completed_indices = [
            index
            for index, node in enumerate(ordered_task_nodes)
            if node.id in completed_node_ids
        ]
        if not completed_indices:
            raise ValueError(f"Execution '{execution_id}' has no workflow checkpoint that matches current graph nodes.")

        last_completed_index = max(completed_indices)
        resume_node = next(
            (
                node
                for node in ordered_task_nodes[last_completed_index + 1:]
                if node.id not in completed_node_ids
            ),
            None,
        )
        if resume_node is None or not resume_node.task_id:
            raise ValueError(f"Execution '{execution_id}' has no remaining task after the latest checkpoint.")
        resume_task = next(
            (candidate for candidate in workflow.task_definitions if candidate.id == resume_node.task_id),
            None,
        )
        if resume_task is None:
            raise WorkflowNotFoundError(f"Task '{resume_node.task_id}' was not found in workflow '{workflow.id}'")

        prior_node_outputs = {
            node_id: output
            for node_id, output in source_node_outputs.items()
            if node_id in completed_node_ids
        }
        trigger = {
            **(source_execution.trigger_payload or {}),
            "type": "checkpoint_resume",
            "source_execution_id": execution_id,
            "resume_node_id": resume_node.id,
            "task_id": resume_task.id,
            "reason": reason,
            "created_by": actor or source_execution.created_by,
        }
        replacement = await self.context.runtime_registry.create_execution(
            source_execution.workflow_id,
            dict(source_execution.input_payload or {}),
            trigger,
            runtime_adapter_id=RuntimeAdapterType.NATIVE.value,
            goal_id=source_execution.goal_id,
        )
        replacement.replacement_of_execution_id = source_execution.id
        replacement.restart_reason = reason or f"Resume from checkpoint before task '{resume_task.name or resume_task.id}'"
        replacement.metadata = {
            **(replacement.metadata or {}),
            "checkpoint_resume": {
                "source_execution_id": execution_id,
                "last_completed_node_id": ordered_task_nodes[last_completed_index].id,
                "resume_node_id": resume_node.id,
                "task_id": resume_task.id,
                "reason": reason,
                "prior_node_outputs": prior_node_outputs,
            },
            # Native execution uses the same skip-to-task metadata path for both
            # failed-task retry and checkpoint resume so prior task outputs stay intact.
            "task_retry": {
                "source_execution_id": execution_id,
                "task_id": resume_task.id,
                "node_id": resume_node.id,
                "reason": reason,
                "prior_node_outputs": prior_node_outputs,
            },
        }
        await self.context.execution_store.update_execution(replacement)
        if replacement.goal_id:
            await GoalService(self.context).link_execution(replacement.goal_id, replacement.id)
        queued = await self.context.control_plane.queue_start(replacement.id)
        return {
            "status": "queued",
            "source_execution_id": execution_id,
            "replacement_execution_id": queued.id,
            "resume_node_id": resume_node.id,
            "task_id": resume_task.id,
            "execution": queued.model_dump(mode="json"),
        }

    @staticmethod
    def _source_node_outputs(execution, state: Any | None) -> dict[str, Any]:
        if state is not None and isinstance(getattr(state, "node_outputs", None), dict):
            return dict(state.node_outputs)
        if isinstance(execution.output_payload, dict):
            node_outputs = execution.output_payload.get("node_outputs")
            if isinstance(node_outputs, dict):
                return dict(node_outputs)
        return {}

    async def cancel(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.cancel(execution_id)).model_dump(mode="json")

    async def approve(self, execution_id: str, tool_id: str, reason: str | None) -> dict[str, Any]:
        approved = await self.context.control_plane.approve(execution_id, tool_id, reason)
        if not approved:
            raise ExecutionNotFoundError("Pending approval was not found")
        return {"approved": True, "execution_id": execution_id, "tool_id": tool_id}

    async def reject(self, execution_id: str, tool_id: str, reason: str | None) -> dict[str, Any]:
        rejected = await self.context.control_plane.reject(execution_id, tool_id, reason)
        if not rejected:
            raise ExecutionNotFoundError("Pending approval was not found")
        return {"rejected": True, "execution_id": execution_id, "tool_id": tool_id}

    async def get_execution(self, execution_id: str) -> dict[str, Any]:
        snapshot = await self.context.runtime_registry.get_execution_state(execution_id)
        replacement_chain = await self._build_replacement_chain(snapshot.execution.id)
        return {
            "execution": self._execution_payload(snapshot.execution),
            "state": {
                "paused": snapshot.state.paused if snapshot.state else False,
                "cancelled": snapshot.state.cancelled if snapshot.state else False,
                "current_node_id": snapshot.state.current_node_id if snapshot.state else None,
                "node_outputs": snapshot.state.node_outputs if snapshot.state else {},
                "runtime_governance": (
                    snapshot.execution.metadata.get("runtime_governance", {})
                    if isinstance(snapshot.execution.metadata, dict)
                    else {}
                ),
                "runtime_callbacks": (
                    snapshot.execution.metadata.get("runtime_callbacks", {})
                    if isinstance(snapshot.execution.metadata, dict)
                    else {}
                ),
            },
            "runtime": await self._runtime_details_for(snapshot.execution),
            "replacement": replacement_chain,
        }

    def _execution_payload(self, execution) -> dict[str, Any]:
        payload = execution.model_dump(mode="json")
        settings = get_settings()
        payload["stale_classification"] = classify_execution_staleness(
            execution,
            stale_after_seconds=settings.main_agent_workflow_monitor_stale_after_seconds,
            idle_timeout_seconds=settings.agent_activity_idle_timeout_seconds,
            run_timeout_seconds=settings.agent_run_timeout_seconds,
        )
        return payload

    async def list_runtime_revisions(self, *, include_invalidated: bool = False) -> dict[str, list[dict[str, Any]]]:
        revisions = await self.context.runtime_revision_repo.list(include_deleted=include_invalidated)
        return {"items": [revision.model_dump(mode="json") for revision in revisions]}

    async def get_runtime_revision(self, revision_id: str, *, include_invalidated: bool = False) -> dict[str, Any]:
        revision = await self.context.runtime_revision_repo.get(revision_id, include_deleted=include_invalidated)
        if revision is None:
            raise ExecutionNotFoundError(f"Runtime revision '{revision_id}' was not found")
        executions = await self.context.execution_store.list_executions()
        linked = [
            execution.model_dump(mode="json")
            for execution in executions
            if execution.runtime_revision_id == revision.id
        ]
        return {
            "runtime_revision": revision.model_dump(mode="json"),
            "executions": linked,
        }

    async def list_managed_containers(self) -> dict[str, list[dict[str, Any]]]:
        containers = self.context.runtime_container_manager.list_managed_containers(all_containers=True)
        items = [
            {
                "container_id": container.container_id,
                "name": container.name,
                "image": container.image,
                "status": container.status,
                "labels": container.labels,
                "started_at": container.started_at.isoformat() if container.started_at else None,
                "finished_at": container.finished_at.isoformat() if container.finished_at else None,
                "exit_code": container.exit_code,
            }
            for container in containers
        ]
        return {"items": items}

    async def get_runtime_metrics(self) -> dict[str, Any]:
        return self.context.runtime_operations.snapshot_dict()

    async def get_container_logs(
            self,
            *,
            execution_id: str | None = None,
            container_id: str | None = None,
            tail_lines: int = 200,
    ) -> dict[str, Any]:
        execution = None
        events: list[ExecutionEvent] = []
        if container_id is None:
            if execution_id is None:
                raise ExecutionNotFoundError("Either execution_id or container_id is required")
            execution = await self.context.execution_store.get_execution(execution_id)
            if execution is None:
                raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
            events = await self.context.execution_store.list_events(execution_id)
            container_id = execution.container_id

        payload = ""
        if container_id:
            payload = self.context.runtime_container_manager.read_container_logs(container_id)
            self.context.runtime_operations.increment("container_logs.reads")
        raw_lines = payload.splitlines()
        if tail_lines > 0:
            raw_lines = raw_lines[-tail_lines:]
        raw_container_logs = "\n".join(raw_lines)

        structured_logs = await self._structured_logs_for_execution_events(execution, events, tail_lines=tail_lines)
        flat_lines = [line["text"] for line in structured_logs["workflow_logs"]]
        for group in structured_logs["agent_logs"]:
            flat_lines.extend(line["text"] for line in group["logs"])
        if raw_container_logs:
            flat_lines.append("[container] stdout/stderr")
            flat_lines.extend(raw_lines)

        return {
            "container_id": container_id,
            "execution_id": execution_id,
            "logs": "\n".join(flat_lines),
            "workflow_logs": structured_logs["workflow_logs"],
            "agent_logs": structured_logs["agent_logs"],
            "raw_container_logs": raw_container_logs,
            **({} if container_id else {"message": f"Execution '{execution_id}' has no managed container logs."}),
        }

    async def _structured_logs_for_execution_events(
            self,
            execution,
            events: list[ExecutionEvent],
            *,
            tail_lines: int,
    ) -> dict[str, list[dict[str, Any]]]:
        if execution is None:
            return {"workflow_logs": [], "agent_logs": []}

        workflow = await self.context.workflow_repo.get(execution.workflow_id)
        agent_name_by_id = {
            agent.id: agent.name
            for agent in (workflow.agent_definitions if workflow else [])
            if agent.id
        }
        workflow_logs: list[dict[str, Any]] = []
        agent_logs_by_key: dict[str, dict[str, Any]] = {}

        for event in sorted(events, key=lambda item: item.sequence):
            line = self._event_log_line(event, agent_name_by_id)
            if event.event_type in AGENT_EVENT_TYPES and (event.agent_id or event.actor):
                group_key = event.agent_id or f"actor:{event.actor}"
                group = agent_logs_by_key.setdefault(
                    group_key,
                    {
                        "agent_id": event.agent_id,
                        "agent_name": agent_name_by_id.get(event.agent_id or "") or event.actor or event.agent_id,
                        "logs": [],
                    },
                )
                group["logs"].append(line)
            else:
                workflow_logs.append(line)

        if tail_lines > 0:
            workflow_logs = workflow_logs[-tail_lines:]
            for group in agent_logs_by_key.values():
                group["logs"] = group["logs"][-tail_lines:]

        return {
            "workflow_logs": workflow_logs,
            "agent_logs": list(agent_logs_by_key.values()),
        }

    def _event_log_line(self, event: ExecutionEvent, agent_name_by_id: dict[str, str]) -> dict[str, Any]:
        agent_name = agent_name_by_id.get(event.agent_id or "") or event.actor or event.agent_id
        prefix = f"[{event.sequence}]"
        if agent_name:
            prefix = f"{prefix} [{agent_name}]"
        else:
            prefix = f"{prefix} [workflow]"
        message = self._event_message(event)
        return {
            "timestamp": event.timestamp.isoformat(),
            "sequence": event.sequence,
            "event_type": event.event_type.value,
            "level": self._event_level(event),
            "agent_id": event.agent_id,
            "agent_name": agent_name,
            "task_id": event.task_id,
            "text": f"{prefix} {message}",
            "message": message,
        }

    def _event_level(self, event: ExecutionEvent) -> str:
        if event.event_type in ERROR_EVENT_TYPES:
            return "error"
        if event.event_type in WARN_EVENT_TYPES:
            return "warn"
        return "info"

    def _event_message(self, event: ExecutionEvent) -> str:
        payload = event.payload or {}
        event_type = event.event_type
        if event_type == ExecutionEventType.EXECUTION_CREATED:
            return "Workflow execution created."
        if event_type == ExecutionEventType.EXECUTION_STARTED:
            return "Workflow execution started."
        if event_type == ExecutionEventType.EXECUTION_COMPLETED:
            return f"Workflow execution completed. {self._preview(payload.get('output'))}".strip()
        if event_type == ExecutionEventType.EXECUTION_FAILED:
            return f"Workflow execution failed. {self._preview(payload.get('error') or payload)}".strip()
        if event_type == ExecutionEventType.TASK_STARTED:
            return f"Task started: {payload.get('task_name') or payload.get('task_id') or event.task_id or 'task'}."
        if event_type == ExecutionEventType.AGENT_MESSAGE_CREATED:
            return f"Agent output: {self._preview(payload.get('content') or payload.get('raw') or payload)}"
        if event_type == ExecutionEventType.SUBAGENT_PROGRESS_UPDATED:
            status = payload.get("status") or payload.get("subagent_status") or payload.get("phase") or "running"
            task = payload.get("current_task") or payload.get("message") or payload.get("completed_step")
            progress = payload.get("progress_percent") or payload.get("percent")
            progress_suffix = f" Progress: {progress}%." if progress is not None else ""
            if payload.get("blocker"):
                return f"Sub-agent blocked: {self._preview(payload.get('blocker'))}.{progress_suffix}"
            if payload.get("clarification_needed"):
                return f"Sub-agent needs clarification: {self._preview(payload.get('clarification_needed'))}.{progress_suffix}"
            return f"Sub-agent progress: {status}. {self._preview(task)}{progress_suffix}".strip()
        if event_type == ExecutionEventType.SUBAGENT_STEP_COMPLETED:
            return f"Sub-agent completed step: {self._preview(payload.get('completed_step') or payload.get('result') or payload)}"
        if event_type == ExecutionEventType.SUBAGENT_STEP_FAILED:
            return f"Sub-agent failed step: {self._preview(payload.get('blocker') or payload.get('error') or payload)}"
        if event_type == ExecutionEventType.SUBAGENT_NEEDS_INPUT:
            return f"Sub-agent needs input: {self._preview(payload.get('clarification_needed') or payload.get('question') or payload)}"
        if event_type == ExecutionEventType.SUBAGENT_NEEDS_APPROVAL:
            return f"Sub-agent needs approval: {self._preview(payload.get('approval_type') or payload.get('reason') or payload)}"
        if event_type == ExecutionEventType.LLM_REQUEST_CREATED:
            model = payload.get("model_name") or event.metrics.get("model_name") or payload.get("model_profile_id")
            context_status = event.metrics.get("context_status")
            context_suffix = f" Context: {context_status}." if context_status else ""
            return f"Model request created{f' for {model}' if model else ''}.{context_suffix}"
        if event_type == ExecutionEventType.LLM_RESPONSE_CREATED:
            total_tokens = event.metrics.get("total_tokens")
            token_suffix = f" Tokens: {total_tokens}." if total_tokens else ""
            return f"Model response: {self._preview(payload.get('content') or payload.get('output') or payload.get('text') or payload)}{token_suffix}"
        if event_type == ExecutionEventType.TOKEN_USAGE_RECORDED:
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            total_tokens = usage.get("total_tokens") or event.metrics.get("total_tokens")
            cost = usage.get("estimated_cost") or event.metrics.get("estimated_cost")
            cost_suffix = f" Estimated cost: {cost}." if cost else ""
            return f"Token usage recorded: {total_tokens or 0} total tokens.{cost_suffix}"
        if event_type in {ExecutionEventType.TOKEN_BUDGET_WARNING, ExecutionEventType.TOKEN_BUDGET_EXCEEDED}:
            budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
            status_label = "exceeded" if event_type == ExecutionEventType.TOKEN_BUDGET_EXCEEDED else "warning"
            scope = budget.get("scope") or "run"
            used = budget.get("used_tokens")
            budget_tokens = budget.get("budget_tokens")
            return f"Token budget {status_label}: {scope} used {used}/{budget_tokens} tokens."
        if event_type == ExecutionEventType.CONTEXT_HEALTH_RECORDED:
            status_value = payload.get("status") or event.metrics.get("context_status")
            total = payload.get("estimated_total_context_tokens") or event.metrics.get("estimated_total_context_tokens")
            window = payload.get("context_window") or event.metrics.get("context_window")
            return f"Context health: {status_value or 'unknown'} ({total or 0}/{window or 'unknown'} tokens)."
        if event_type == ExecutionEventType.CONTEXT_COMPACTION_STARTED:
            context_health = payload.get("context_health") if isinstance(payload.get("context_health"), dict) else {}
            status_value = context_health.get("status")
            return f"Context compaction started{f' after {status_value} context health' if status_value else ''}."
        if event_type == ExecutionEventType.CONTEXT_COMPACTION_COMPLETED:
            record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
            if record.get("compacted"):
                return f"Context compaction completed. Estimated tokens saved: {record.get('estimated_tokens_saved') or 0}."
            return f"Context compaction skipped: {record.get('reason') or 'not needed'}."
        if event_type == ExecutionEventType.CONTEXT_COMPACTION_FAILED:
            return f"Context compaction failed: {self._preview(payload.get('error') or payload)}"
        if event_type == ExecutionEventType.TOOL_CALL_STARTED:
            return f"Tool started: {payload.get('tool_name') or payload.get('tool_id') or payload.get('tool') or 'tool'}."
        if event_type == ExecutionEventType.TOOL_CALL_COMPLETED:
            return f"Tool completed: {payload.get('tool_name') or payload.get('tool_id') or payload.get('tool') or 'tool'}. {self._preview(payload.get('result') or payload.get('output'))}".strip()
        if event_type == ExecutionEventType.TOOL_CALL_FAILED:
            return f"Tool failed: {payload.get('tool_name') or payload.get('tool_id') or payload.get('tool') or 'tool'}. {self._preview(payload.get('error') or payload.get('result') or payload)}".strip()
        if event_type == ExecutionEventType.ARTIFACT_CREATED:
            return f"Artifact created: {payload.get('name') or payload.get('artifact_id') or 'artifact'}."
        if event_type == ExecutionEventType.MONITOR_FINDING_CREATED:
            category = payload.get("category") or event.metadata.get("category") or "finding"
            reason = payload.get("reason")
            return f"Main-agent monitor finding: {category}. {self._preview(reason)}".strip()
        if event_type == ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED:
            change = payload.get("proposed_change") if isinstance(payload.get("proposed_change"), dict) else {}
            summary = change.get("summary") or "Improvement proposed."
            return f"Main-agent monitor improvement proposal: {self._preview(summary)}".strip()
        if event_type == ExecutionEventType.SUPERVISOR_STEERING_REQUESTED:
            action = payload.get("recommended_action") or "review"
            reason = payload.get("reason")
            return f"Supervisor steering requested: {action}. {self._preview(reason)}".strip()
        if event_type == ExecutionEventType.SUPERVISOR_STEERING_APPLIED:
            action = payload.get("applied_action") or payload.get("recommended_action") or "steering"
            return f"Supervisor steering applied: {self._preview(action)}".strip()
        if event_type == ExecutionEventType.EXECUTION_REPAIRED:
            action = payload.get("repair_action") or "stale execution repaired"
            return f"Execution repaired: {self._preview(action)}".strip()
        if event_type == ExecutionEventType.EXECUTION_CYCLE_STARTED:
            return f"Monitor cycle {payload.get('cycle_number') or '?'} started."
        if event_type == ExecutionEventType.EXECUTION_CYCLE_COMPLETED:
            return f"Monitor cycle {payload.get('cycle_number') or '?'} completed."
        if event_type == ExecutionEventType.EXECUTION_CYCLE_FAILED:
            return f"Monitor cycle failed: {self._preview(payload.get('error'))}".strip()
        if event_type == ExecutionEventType.EXECUTION_CYCLE_GUARD_TRIGGERED:
            return f"Monitor cycle paused: {self._preview(payload.get('reason'))}".strip()
        if event_type == ExecutionEventType.EXECUTION_WAITING:
            return f"Execution waiting for {payload.get('kind') or 'wake condition'}."
        if event_type == ExecutionEventType.EXECUTION_WOKEN:
            return f"Execution wait resolved: {payload.get('status') or 'resolved'}."
        if event_type.value.startswith("container."):
            return f"Container event: {event_type.value}."
        if event_type.value.startswith("runtime."):
            return f"Runtime event: {event_type.value}."
        if event_type.value.startswith("approval."):
            return f"Approval event: {event_type.value}."
        return event_type.value

    def _preview(self, value: Any, max_length: int = 500) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            text = str(value)
        text = " ".join(text.split())
        return text if len(text) <= max_length else f"{text[:max_length - 1]}…"

    async def reconcile_runtime(self) -> dict[str, Any]:
        report = await self.context.runtime_reconciler.reconcile_once()
        return {
            **asdict(report),
            "execution_waits": await ExecutionWaitService(self.context).wake_due_waits(),
        }

    async def repair_stale_executions(self, *, workflow_id: str | None = None) -> dict[str, Any]:
        repaired = await self.context.control_plane.repair_stale_executions(workflow_id=workflow_id)
        return {
            "workflow_id": workflow_id,
            "items": repaired,
            "repaired_count": len(repaired),
        }

    async def update_execution(self, execution_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if execution.status.value != "created":
            raise ValueError("Only executions in created status can be updated")
        merged = execution.model_dump(mode="json")
        merged.update(patch)
        merged["id"] = execution_id
        updated = execution.__class__.model_validate(merged)
        await self.context.execution_store.update_execution(updated)
        return updated.model_dump(mode="json")

    async def list_execution_events(
            self,
            execution_id: str,
            after_sequence: int = 0,
            event_types: list[str] | None = None,
    ) -> dict[str, Any]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        normalized_event_types = self._normalize_event_type_filters(event_types)
        events = await self.context.execution_store.list_events_after(execution_id, after_sequence)
        if normalized_event_types:
            events = [event for event in events if event.event_type in normalized_event_types]
        return {
            "items": [event.model_dump(mode="json") for event in events],
            "filters": {
                "after_sequence": after_sequence,
                "event_types": (
                    [event_type.value for event_type in sorted(normalized_event_types, key=lambda item: item.value)]
                    if normalized_event_types
                    else []
                ),
            },
        }

    def _normalize_event_type_filters(self, event_types: list[str] | None) -> set[ExecutionEventType]:
        normalized: set[ExecutionEventType] = set()
        for raw_value in event_types or []:
            for candidate in str(raw_value).split(","):
                value = candidate.strip()
                if not value:
                    continue
                try:
                    normalized.add(ExecutionEventType(value))
                    continue
                except ValueError:
                    pass
                try:
                    normalized.add(ExecutionEventType[value.upper()])
                except KeyError as exc:
                    raise ValueError(f"Unsupported execution event type filter: {value}") from exc
        return normalized

    async def list_execution_artifacts(self, execution_id: str) -> dict[str, list[dict[str, Any]]]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        artifacts = await self.context.execution_store.list_artifacts(execution_id)
        return {"items": [artifact.model_dump(mode="json") for artifact in artifacts]}

    async def get_execution_usage(self, execution_id: str) -> dict[str, Any]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        governance = self._runtime_governance_for_execution(execution)
        token_usage = self._public_token_usage(governance.get("token_usage"))
        budget_warnings = self._public_budget_warnings(governance.get("budget_warnings_emitted"))
        source = "execution.metadata.runtime_governance"
        if not token_usage:
            token_usage = await self._token_usage_from_events(execution_id)
            if token_usage:
                source = "execution.events"
        if not budget_warnings:
            budget_warnings = await self._budget_warnings_from_events(execution_id)
        return {
            "execution_id": execution.id,
            "workflow_id": execution.workflow_id,
            "source": source,
            "token_usage": token_usage,
            "budget_warnings": budget_warnings,
            "updated_at": token_usage.get("updated_at") or execution.updated_at.isoformat(),
        }

    async def get_execution_context_usage(self, execution_id: str) -> dict[str, Any]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        governance = self._runtime_governance_for_execution(execution)
        context_health = self._public_dict(governance.get("context_health"))
        context_compaction = self._public_dict(governance.get("context_compaction"))
        latest_health = self._public_dict(context_health.get("last"))
        latest_compaction = self._public_dict(context_compaction.get("last"))
        source = "execution.metadata.runtime_governance"
        compaction_records = [
            self._public_dict(item)
            for item in context_compaction.get("records", [])
            if isinstance(item, dict)
        ]
        if not latest_health:
            latest_health = await self._latest_context_health_from_events(execution_id)
            if latest_health:
                source = "execution.events"
                context_health = {"last": latest_health}
        if not compaction_records:
            compaction_records = await self._context_compaction_records_from_events(execution_id)
            if compaction_records:
                source = "execution.events"
                latest_compaction = compaction_records[-1]
                context_compaction = {
                    "last": latest_compaction,
                    "records": compaction_records,
                    "count": len(compaction_records),
                    "compacted_count": sum(1 for item in compaction_records if item.get("compacted")),
                    "estimated_tokens_saved": sum(
                        int(item.get("estimated_tokens_saved") or 0) for item in compaction_records
                    ),
                }
        protected_context = self._public_protected_context(latest_compaction)
        return {
            "execution_id": execution.id,
            "workflow_id": execution.workflow_id,
            "source": source,
            "context_health": context_health,
            "latest_context_health": latest_health,
            "context_compaction": {
                **context_compaction,
                "records": compaction_records,
            },
            "latest_compaction": latest_compaction,
            "compaction_records": compaction_records,
            "protected_context": protected_context,
            "updated_at": (
                    latest_compaction.get("updated_at")
                    or latest_health.get("updated_at")
                    or execution.updated_at.isoformat()
            ),
        }

    def _runtime_governance_for_execution(self, execution) -> dict[str, Any]:
        metadata = execution.metadata if isinstance(execution.metadata, dict) else {}
        governance = metadata.get("runtime_governance")
        return dict(governance or {}) if isinstance(governance, dict) else {}

    def _public_dict(self, value: Any) -> dict[str, Any]:
        return dict(value or {}) if isinstance(value, dict) else {}

    def _public_protected_context(self, latest_compaction: dict[str, Any]) -> dict[str, Any]:
        metadata = self._public_dict(latest_compaction.get("metadata"))
        reasons = metadata.get("protected_message_reasons")
        if not isinstance(reasons, dict):
            reasons = {}
        roles = metadata.get("protected_message_roles")
        if not isinstance(roles, list):
            roles = []
        protected_count = int(metadata.get("protected_message_count") or 0)
        return {
            "retained": bool(metadata.get("protected_context_retained") or protected_count or reasons or roles),
            "protected_message_count": protected_count,
            "protected_message_roles": roles,
            "protected_message_reasons": reasons,
        }

    def _public_token_usage(self, value: Any) -> dict[str, Any]:
        token_usage = self._public_dict(value)
        token_usage.pop("processed_event_ids", None)
        return token_usage

    async def _token_usage_from_events(self, execution_id: str) -> dict[str, Any]:
        events = await self.context.execution_store.list_events(execution_id)
        usage_events = [
            event for event in events if event.event_type == ExecutionEventType.TOKEN_USAGE_RECORDED
        ]
        if not usage_events:
            return {}

        token_usage: dict[str, Any] = {
            "total": self._empty_usage_bucket(),
            "by_agent": {},
            "by_task": {},
            "by_model": {},
        }
        last_event: ExecutionEvent | None = None
        for event in usage_events:
            usage = event.payload.get("usage") if isinstance(event.payload.get("usage"), dict) else {}
            if not usage:
                continue
            last_event = event
            self._add_usage_to_bucket(token_usage["total"], usage)
            if event.agent_id:
                agent_bucket = token_usage["by_agent"].setdefault(event.agent_id, self._empty_usage_bucket())
                self._add_usage_to_bucket(agent_bucket, usage)
            if event.task_id:
                task_bucket = token_usage["by_task"].setdefault(event.task_id, self._empty_usage_bucket())
                self._add_usage_to_bucket(task_bucket, usage)
            model_key = ":".join(
                part for part in (usage.get("provider"), usage.get("model")) if isinstance(part, str) and part
            ) or "unknown"
            model_bucket = token_usage["by_model"].setdefault(model_key, self._empty_usage_bucket())
            self._add_usage_to_bucket(model_bucket, usage)

        if last_event is None:
            return {}
        token_usage["last_event_id"] = last_event.id
        token_usage["last_model_request_id"] = last_event.model_request_id
        token_usage["updated_at"] = self._event_timestamp(last_event)
        return token_usage

    async def _budget_warnings_from_events(self, execution_id: str) -> list[dict[str, Any]]:
        events = await self.context.execution_store.list_events(execution_id)
        warnings: list[dict[str, Any]] = []
        for event in events:
            if event.event_type not in {ExecutionEventType.TOKEN_BUDGET_WARNING,
                                        ExecutionEventType.TOKEN_BUDGET_EXCEEDED}:
                continue
            budget = event.payload.get("budget") if isinstance(event.payload.get("budget"), dict) else event.payload
            warning = self._public_dict(budget)
            warning.setdefault("status",
                               "exceeded" if event.event_type == ExecutionEventType.TOKEN_BUDGET_EXCEEDED else "warning")
            warning.setdefault("event_id", event.id)
            warning.setdefault("agent_id", event.agent_id)
            warning.setdefault("task_id", event.task_id)
            warning.setdefault("emitted_at", self._event_timestamp(event))
            warnings.append(warning)
        return sorted(warnings, key=lambda item: item.get("emitted_at") or "")

    async def _latest_context_health_from_events(self, execution_id: str) -> dict[str, Any]:
        events = await self.context.execution_store.list_events(execution_id)
        health_events = [
            event for event in events if event.event_type == ExecutionEventType.CONTEXT_HEALTH_RECORDED
        ]
        if not health_events:
            return {}
        event = health_events[-1]
        health = self._public_dict(event.payload)
        health["agent_id"] = event.agent_id
        health["task_id"] = event.task_id
        health["event_id"] = event.id
        health["updated_at"] = self._event_timestamp(event)
        return health

    async def _context_compaction_records_from_events(self, execution_id: str) -> list[dict[str, Any]]:
        events = await self.context.execution_store.list_events(execution_id)
        records: list[dict[str, Any]] = []
        for event in events:
            if event.event_type not in {
                ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
                ExecutionEventType.CONTEXT_COMPACTION_FAILED,
            }:
                continue
            record = event.payload.get("record") if isinstance(event.payload.get("record"), dict) else {}
            if not record:
                record = {
                    "compacted": event.event_type == ExecutionEventType.CONTEXT_COMPACTION_COMPLETED,
                    "reason": event.payload.get("reason") or event.payload.get("error"),
                    "estimated_tokens_saved": event.metrics.get("estimated_tokens_saved") or 0,
                }
            item = self._public_dict(record)
            item["agent_id"] = event.agent_id
            item["task_id"] = event.task_id
            item["event_id"] = event.id
            item["updated_at"] = self._event_timestamp(event)
            records.append(item)
        return records[-25:]

    def _empty_usage_bucket(self) -> dict[str, Any]:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost": 0.0,
        }

    def _add_usage_to_bucket(self, bucket: dict[str, Any], usage: dict[str, Any]) -> None:
        bucket["prompt_tokens"] = int(bucket.get("prompt_tokens") or 0) + int(usage.get("prompt_tokens") or 0)
        bucket["completion_tokens"] = int(bucket.get("completion_tokens") or 0) + int(
            usage.get("completion_tokens") or 0)
        bucket["total_tokens"] = int(bucket.get("total_tokens") or 0) + int(usage.get("total_tokens") or 0)
        bucket["estimated_cost"] = round(
            float(bucket.get("estimated_cost") or 0.0) + float(usage.get("estimated_cost") or 0.0),
            8,
        )
        if usage.get("currency"):
            bucket["currency"] = usage["currency"]

    def _event_timestamp(self, event: ExecutionEvent) -> str | None:
        return event.timestamp.isoformat() if event.timestamp is not None else None

    def _public_budget_warnings(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            return []
        warnings = [dict(item) for item in value.values() if isinstance(item, dict)]
        return sorted(warnings, key=lambda item: item.get("emitted_at") or "")

    async def list_execution_approvals(self, execution_id: str) -> dict[str, list[dict[str, Any]]]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        if not hasattr(self.context.execution_store, "list_approval_requests"):
            return {"items": []}
        items = await self.context.execution_store.list_approval_requests(execution_id)
        return {"items": items}

    async def stream_execution_images(
            self,
            execution_id: str,
            poll_interval: float,
            max_duration: int,
            boundary: str,
    ) -> AsyncGenerator[bytes, None]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")

        processed_ids: set[str] = set()
        start_time = time.time()
        last_image_time = time.time()

        yield f"--{boundary}\r\n".encode()

        while True:
            current_time = time.time()
            if current_time - start_time > max_duration:
                yield f"--{boundary}--\r\n".encode()
                break

            artifacts = await self.context.execution_store.list_artifacts(execution_id)
            image_artifacts = [
                artifact
                for artifact in artifacts
                if artifact.id not in processed_ids and self._is_streamable_image_artifact(artifact)
            ]
            image_artifacts.sort(key=lambda artifact: artifact.created_at)

            if not image_artifacts:
                await asyncio.sleep(1)
                if current_time - last_image_time > 3:
                    yield (
                        f"\r\n--{boundary}\r\nContent-Type: text/plain\r\n\r\nkeep-alive\r\n--{boundary}\r\n"
                    ).encode()
                    last_image_time = current_time
                continue

            for artifact in image_artifacts:
                processed_ids.add(artifact.id)
                if not artifact.uri:
                    continue
                try:
                    file_data = self.return_file_from_s3_func(artifact.uri)
                    image_data = file_data.read()
                    if hasattr(file_data, "close"):
                        file_data.close()
                    media_type = artifact.media_type or "image/jpeg"
                    yield f"Content-Type: {media_type}\r\n\r\n".encode()
                    yield image_data
                    yield f"\r\n--{boundary}\r\n".encode()
                    last_image_time = time.time()
                except Exception:
                    continue

            await asyncio.sleep(poll_interval)

    async def stream_execution_events(self, execution_id: str, request: Any, after_sequence: int = 0) -> AsyncGenerator[
        str, None]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")

        current_sequence = after_sequence
        while True:
            if await request.is_disconnected():
                break
            events = await self.context.execution_store.list_events_after(execution_id, current_sequence)
            for event in events:
                current_sequence = max(current_sequence, event.sequence)
                yield f"data: {event.model_dump_json()}\n\n"
            current = await self.context.execution_store.get_execution(execution_id)
            if current and current.status.value in {"completed", "failed", "cancelled"} and not events:
                break
            await asyncio.sleep(0.2)

    async def stream_execution_hitl_output(self, request: Any, execution_id: str) -> AsyncGenerator[str, None]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")

        pubsub = self.redis_client.pubsub()
        channel_name = agent_output_channel(execution_id)
        await pubsub.subscribe(channel_name)
        try:
            async for message in pubsub.listen():
                if await request.is_disconnected():
                    break
                if message and message["type"] == "message":
                    message_data = message["data"]
                    if isinstance(message_data, bytes):
                        message_data = message_data.decode("utf-8")
                    yield f"data: {message_data}\n\n"
        finally:
            await pubsub.unsubscribe(channel_name)

    async def publish_execution_hitl_reply(self, execution_id: str, reply: str) -> dict[str, str]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")

        await self.redis_client.publish(human_reply_channel(execution_id), reply)
        return {"message": "Human reply received"}

    async def _runtime_details_for(self, execution) -> dict[str, Any]:
        revision = None
        if execution.runtime_revision_id:
            revision = await self.context.runtime_revision_repo.get(execution.runtime_revision_id, include_deleted=True)
        return {
            "runtime_revision": None if revision is None else revision.model_dump(mode="json"),
            "container": {
                "container_id": execution.container_id,
                "container_name": execution.container_name,
                "image": execution.container_image,
                "status": execution.container_status,
                "started_at": execution.container_started_at.isoformat() if execution.container_started_at else None,
                "ended_at": execution.container_ended_at.isoformat() if execution.container_ended_at else None,
                "exit_code": execution.container_exit_code,
            },
            "diagnostics": execution.metadata.get("runtime_diagnostics", {}) if isinstance(execution.metadata,
                                                                                           dict) else {},
        }

    async def _build_replacement_chain(self, execution_id: str) -> dict[str, Any]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        replaced_execution = None
        if execution.replacement_of_execution_id:
            replaced_execution = await self.context.execution_store.get_execution(execution.replacement_of_execution_id)
        all_executions = await self.context.execution_store.list_executions()
        replaced_by = [
            item.model_dump(mode="json")
            for item in all_executions
            if item.replacement_of_execution_id == execution_id
        ]
        return {
            "restart_reason": execution.restart_reason,
            "replaces_execution": None if replaced_execution is None else replaced_execution.model_dump(mode="json"),
            "replaced_by_executions": replaced_by,
        }

    @staticmethod
    def _is_streamable_image_artifact(artifact: Any) -> bool:
        if not getattr(artifact, "uri", None):
            return False

        media_type = (getattr(artifact, "media_type", None) or "").lower()
        if media_type.startswith("image/"):
            return True

        uri = str(getattr(artifact, "uri", "")).lower()
        return uri.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"))
