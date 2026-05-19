from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from app.api.context import ApiContext
from app.core.config import get_settings
from app.core.storage import return_file_from_s3
from app.domain import ExecutionEvent, ExecutionEventType, ModelProfileDefinition, WorkflowDefinition
from app.runtime.channels import agent_output_channel, human_reply_channel
from app.runtime.native.errors import ExecutionNotFoundError
from app.runtime.process_supervisor import execution_process_manager
from app.services.execution_classification import classify_execution_staleness

AGENT_EVENT_TYPES = {
    ExecutionEventType.AGENT_MESSAGE_CREATED,
    ExecutionEventType.LLM_REQUEST_CREATED,
    ExecutionEventType.LLM_RESPONSE_CREATED,
    ExecutionEventType.TASK_STARTED,
    ExecutionEventType.TOOL_CALL_STARTED,
    ExecutionEventType.TOOL_CALL_COMPLETED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.APPROVAL_REQUESTED,
    ExecutionEventType.APPROVAL_GRANTED,
    ExecutionEventType.APPROVAL_REJECTED,
}

ERROR_EVENT_TYPES = {
    ExecutionEventType.EXECUTION_FAILED,
    ExecutionEventType.TOOL_CALL_FAILED,
    ExecutionEventType.CONTAINER_FAILED,
    ExecutionEventType.RUNTIME_BUILD_FAILED,
    ExecutionEventType.APPROVAL_REJECTED,
}

WARN_EVENT_TYPES = {
    ExecutionEventType.APPROVAL_REQUESTED,
    ExecutionEventType.EXECUTION_PAUSED,
    ExecutionEventType.EXECUTION_CANCELLED,
    ExecutionEventType.CONTAINER_REPLACED,
    ExecutionEventType.MONITOR_FINDING_CREATED,
    ExecutionEventType.MONITOR_IMPROVEMENT_PROPOSED,
}


@dataclass(slots=True)
class ExecutionService:
    context: ApiContext
    return_file_from_s3_func: Any = return_file_from_s3
    redis_client: Any = execution_process_manager.redis_client

    async def list_executions(self) -> dict[str, list[dict[str, Any]]]:
        executions = await self.context.execution_store.list_executions()
        return {"items": [self._execution_payload(execution) for execution in executions]}

    async def list_active_executions(self) -> dict[str, list[dict[str, Any]]]:
        items = await self.context.execution_store.list_active_executions()
        return {"items": [self._execution_payload(item) for item in items]}

    async def create_execution(
            self,
            workflow_id: str,
            input_payload: dict[str, Any],
            trigger: dict[str, Any],
            runtime_adapter_id: str | None = None,
            execution_host: str | None = None,
            workflow_definition: WorkflowDefinition | None = None,
            model_profiles: list[ModelProfileDefinition] | None = None,
    ) -> dict[str, Any]:
        await self.context.ensure_runtime_adapter_seed_data()
        if workflow_definition is not None:
            await self.context.runtime_registry.register_workflow(workflow_definition)
            workflow_id = workflow_definition.id
        for profile in model_profiles or []:
            await self.context.runtime_registry.register_model_profile(profile)

        if execution_host:
            trigger = {**trigger, "execution_host": execution_host}

        execution = await self.context.runtime_registry.create_execution(
            workflow_id,
            input_payload,
            trigger,
            runtime_adapter_id=runtime_adapter_id,
        )
        return execution.model_dump(mode="json")

    async def queue_start(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.queue_start(execution_id)).model_dump(mode="json")

    async def pause(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.pause(execution_id)).model_dump(mode="json")

    async def resume(self, execution_id: str) -> dict[str, Any]:
        return (await self.context.control_plane.resume(execution_id)).model_dump(mode="json")

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
            },
            "runtime": await self._runtime_details_for(snapshot.execution),
            "replacement": replacement_chain,
        }

    def _execution_payload(self, execution) -> dict[str, Any]:
        payload = execution.model_dump(mode="json")
        payload["stale_classification"] = classify_execution_staleness(
            execution,
            stale_after_seconds=get_settings().main_agent_workflow_monitor_stale_after_seconds,
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
        if event_type == ExecutionEventType.LLM_REQUEST_CREATED:
            model = payload.get("model_name") or event.metrics.get("model_name") or payload.get("model_profile_id")
            return f"Model request created{f' for {model}' if model else ''}."
        if event_type == ExecutionEventType.LLM_RESPONSE_CREATED:
            return f"Model response: {self._preview(payload.get('content') or payload.get('output') or payload.get('text') or payload)}"
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
        if event_type == ExecutionEventType.EXECUTION_REPAIRED:
            action = payload.get("repair_action") or "stale execution repaired"
            return f"Execution repaired: {self._preview(action)}".strip()
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
        return asdict(report)

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

    async def list_execution_events(self, execution_id: str, after_sequence: int = 0) -> dict[
        str, list[dict[str, Any]]]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        events = await self.context.execution_store.list_events_after(execution_id, after_sequence)
        return {"items": [event.model_dump(mode="json") for event in events]}

    async def list_execution_artifacts(self, execution_id: str) -> dict[str, list[dict[str, Any]]]:
        execution = await self.context.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        artifacts = await self.context.execution_store.list_artifacts(execution_id)
        return {"items": [artifact.model_dump(mode="json") for artifact in artifacts]}

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
