from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from typing import Any, Dict, List, Optional, Protocol
from uuid import uuid4

from app.core.time import utc_now
from app.db.models import ApprovalRequestORM, ExecutionArtifactORM, ExecutionEventORM, ExecutionORM, ToolInvocationORM
from app.db.repositories import (
    InMemoryExecutionArtifactRepository,
    InMemoryExecutionEventRepository,
    InMemoryExecutionRepository,
    MongoExecutionArtifactRepository,
    MongoExecutionEventRepository,
    MongoExecutionRepository,
)
from app.domain import Execution, ExecutionArtifact, ExecutionEvent, ModelProfileDefinition, WorkflowDefinition


@dataclass
class NativeExecutionState:
    execution_id: str
    workflow_id: str
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    paused: bool = False
    cancelled: bool = False
    sequence: int = 0
    node_outputs: Dict[str, Any] = field(default_factory=dict)
    memory_entries: List[Dict[str, Any]] = field(default_factory=list)
    current_node_id: Optional[str] = None
    current_agent_id: Optional[str] = None
    current_task_id: Optional[str] = None
    last_event_id: Optional[str] = None
    created_at: datetime = field(default_factory=utc_now)

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence


class WorkflowRepository(Protocol):
    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]: ...

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition: ...


class ModelProfileRepository(Protocol):
    async def get_profile(self, profile_id: str) -> Optional[ModelProfileDefinition]: ...

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition: ...


class ExecutionStore(Protocol):
    async def save_execution(self, execution: Execution) -> Execution: ...

    async def update_execution(self, execution: Execution) -> Execution: ...

    async def get_execution(self, execution_id: str) -> Optional[Execution]: ...

    async def list_executions(self) -> List[Execution]: ...

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent: ...

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]: ...

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]: ...

    async def delete_event(self, event_id: str) -> bool: ...

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact: ...

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]: ...

    async def list_active_executions(self) -> List[Execution]: ...

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]: ...

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]: ...

    async def delete_execution(self, execution_id: str) -> bool: ...

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool: ...

    async def release_lock(self, execution_id: str, worker_id: str) -> bool: ...

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]: ...


def _serialize_execution_trigger(trigger_payload: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    return {"payload": trigger_payload, "metadata": metadata}


def _deserialize_execution_payload(payload: Dict[str, Any] | None) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if isinstance(payload, dict) and "payload" in payload and "metadata" in payload:
        return dict(payload.get("payload") or {}), dict(payload.get("metadata") or {})
    return dict(payload or {}), {}


def _execution_to_orm(execution: Execution) -> ExecutionORM:
    return ExecutionORM(
        id=execution.id,
        workflow_id=execution.workflow_id,
        workflow_version_id=execution.workflow_version_id,
        status=execution.status.value,
        runtime_adapter=execution.runtime_adapter_id,
        runtime_revision_id=execution.runtime_revision_id,
        runtime_fingerprint=execution.runtime_fingerprint,
        trigger_type=execution.trigger_type,
        trigger_payload_json=_serialize_execution_trigger(execution.trigger_payload, execution.metadata),
        input_json=execution.input_payload,
        output_json=execution.output_payload,
        error_json=execution.error_json,
        created_by=execution.created_by,
        worker_id=execution.worker_id,
        created_at=execution.created_at,
        started_at=execution.started_at,
        ended_at=execution.completed_at,
        updated_at=execution.updated_at,
        last_heartbeat_at=execution.last_heartbeat_at,
        container_id=execution.container_id,
        container_name=execution.container_name,
        container_image=execution.container_image,
        container_status=execution.container_status,
        container_started_at=execution.container_started_at,
        container_ended_at=execution.container_ended_at,
        container_exit_code=execution.container_exit_code,
        replacement_of_execution_id=execution.replacement_of_execution_id,
        restart_reason=execution.restart_reason,
    )


def _execution_from_orm(orm: ExecutionORM) -> Execution:
    trigger_payload, metadata = _deserialize_execution_payload(orm.trigger_payload_json)
    return Execution.model_validate(
        {
            "id": orm.id,
            "workflow_id": orm.workflow_id,
            "workflow_version_id": orm.workflow_version_id,
            "runtime_adapter": orm.runtime_adapter,
            "runtime_revision_id": orm.runtime_revision_id,
            "runtime_fingerprint": orm.runtime_fingerprint,
            "status": orm.status,
            "trigger_type": orm.trigger_type,
            "trigger_payload": trigger_payload,
            "input_json": orm.input_json,
            "output_json": orm.output_json,
            "error": (
                orm.error_json.get("message") if isinstance(orm.error_json, dict) else orm.error_json
            ),
            "created_by": orm.created_by,
            "worker_id": orm.worker_id,
            "created_at": orm.created_at,
            "started_at": orm.started_at,
            "ended_at": orm.ended_at,
            "updated_at": orm.updated_at,
            "last_heartbeat_at": orm.last_heartbeat_at,
            "container_id": orm.container_id,
            "container_name": orm.container_name,
            "container_image": orm.container_image,
            "container_status": orm.container_status,
            "container_started_at": orm.container_started_at,
            "container_ended_at": orm.container_ended_at,
            "container_exit_code": orm.container_exit_code,
            "replacement_of_execution_id": orm.replacement_of_execution_id,
            "restart_reason": orm.restart_reason,
            "metadata": metadata,
        }
    )


def _event_to_orm(event: ExecutionEvent) -> ExecutionEventORM:
    payload_json = {
        "payload": event.payload,
        "metrics": event.metrics,
        "metadata": event.metadata,
        "workflow_id": event.workflow_id,
        "agent_id": event.agent_id,
        "task_id": event.task_id,
        "tool_call_id": event.tool_call_id,
        "model_request_id": event.model_request_id,
        "redacted_fields": event.redacted_fields,
    }
    return ExecutionEventORM(
        id=event.id,
        execution_id=event.execution_id,
        sequence=event.sequence,
        event_type=event.event_type.value,
        timestamp=event.timestamp,
        actor_type=event.actor_type,
        actor_id=event.actor,
        payload_json=payload_json,
        parent_event_id=event.parent_event_id,
        trace_id=event.trace_id,
        span_id=event.span_id,
    )


def _event_from_orm(orm: ExecutionEventORM) -> ExecutionEvent:
    payload = dict(orm.payload_json or {})
    return ExecutionEvent.model_validate(
        {
            "id": orm.id,
            "execution_id": orm.execution_id,
            "workflow_id": payload.get("workflow_id"),
            "agent_id": payload.get("agent_id"),
            "task_id": payload.get("task_id"),
            "tool_call_id": payload.get("tool_call_id"),
            "model_request_id": payload.get("model_request_id"),
            "parent_event_id": orm.parent_event_id,
            "trace_id": orm.trace_id,
            "span_id": orm.span_id,
            "event_type": orm.event_type,
            "timestamp": orm.timestamp,
            "sequence": orm.sequence,
            "actor_type": orm.actor_type,
            "actor_id": orm.actor_id,
            "payload_json": payload.get("payload", {}),
            "metrics": payload.get("metrics", {}),
            "metadata": payload.get("metadata", {}),
            "redacted_fields": payload.get("redacted_fields", []),
        }
    )


def _artifact_to_orm(artifact: ExecutionArtifact) -> ExecutionArtifactORM:
    metadata = dict(artifact.metadata)
    if artifact.size_bytes is not None:
        metadata["size_bytes"] = artifact.size_bytes
    return ExecutionArtifactORM(
        id=artifact.id,
        execution_id=artifact.execution_id,
        event_id=artifact.event_id,
        artifact_type=artifact.artifact_type,
        name=artifact.name,
        content_json=artifact.content_json,
        content_text=artifact.content_text,
        file_path=artifact.uri,
        mime_type=artifact.media_type,
        metadata_json=metadata,
        created_at=artifact.created_at,
    )


def _artifact_from_orm(orm: ExecutionArtifactORM) -> ExecutionArtifact:
    metadata = dict(orm.metadata_json or {})
    return ExecutionArtifact.model_validate(
        {
            "id": orm.id,
            "execution_id": orm.execution_id,
            "event_id": orm.event_id,
            "artifact_type": orm.artifact_type,
            "name": orm.name,
            "content_json": orm.content_json,
            "content_text": orm.content_text,
            "file_path": orm.file_path,
            "mime_type": orm.mime_type,
            "created_at": orm.created_at,
            "metadata_json": {k: v for k, v in metadata.items() if k != "size_bytes"},
            "size_bytes": metadata.get("size_bytes"),
        }
    )


class InMemoryWorkflowRepository:
    def __init__(self):
        self._workflows: Dict[str, WorkflowDefinition] = {}

    async def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        return self._workflows.get(workflow_id)

    async def save_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        self._workflows[workflow.id] = workflow
        return workflow


class InMemoryModelProfileRepository:
    def __init__(self):
        self._profiles: Dict[str, ModelProfileDefinition] = {}

    async def get_profile(self, profile_id: str) -> Optional[ModelProfileDefinition]:
        return self._profiles.get(profile_id)

    async def save_profile(self, profile: ModelProfileDefinition) -> ModelProfileDefinition:
        self._profiles[profile.id] = profile
        return profile


class InMemoryExecutionStore:
    def __init__(self):
        self.execution_repository = InMemoryExecutionRepository()
        self.event_repository = InMemoryExecutionEventRepository()
        self.artifact_repository = InMemoryExecutionArtifactRepository()
        self._executions = self.execution_repository._items
        self._events = self.event_repository._items
        self._artifacts = self.artifact_repository._items

    async def save_execution(self, execution: Execution) -> Execution:
        existing = await self.execution_repository.get_execution(execution.id)
        if existing is None:
            return await self.execution_repository.create_execution(execution)
        return await self.execution_repository.save_execution(execution)

    async def update_execution(self, execution: Execution) -> Execution:
        return await self.execution_repository.save_execution(execution)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        return await self.execution_repository.get_execution(execution_id)

    async def list_executions(self) -> List[Execution]:
        return await self.execution_repository.list_executions()

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent:
        return await self.event_repository.append_event(event)

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        return await self.event_repository.list_events(execution_id)

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        return await self.event_repository.list_events_after_sequence(execution_id, after_sequence)

    async def delete_event(self, event_id: str) -> bool:
        return await self.event_repository.delete_event(event_id)

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        return await self.artifact_repository.create_artifact(artifact)

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        return await self.artifact_repository.list_artifacts(execution_id)

    async def list_active_executions(self) -> List[Execution]:
        active = {"queued", "running", "waiting_for_approval", "paused", "cancelling"}
        return await self.execution_repository.list_executions(filters={"status_in": active})

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]:
        return await self.execution_repository.list_executions(filters={"workflow_id": workflow_id})

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]:
        return [
            execution
            for execution in await self.execution_repository.list_executions()
            if agent_id in (execution.metadata.get("agent_ids") or [])
        ]

    async def delete_execution(self, execution_id: str) -> bool:
        deleted = await self.execution_repository.delete_execution(execution_id)
        self._events.pop(execution_id, None)
        self._artifacts.pop(execution_id, None)
        return deleted

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool:
        execution = self._executions.get(execution_id)
        if execution is None:
            return False
        now = utc_now()
        last_heartbeat = execution.last_heartbeat_at
        is_stale = last_heartbeat is None or (now - last_heartbeat).total_seconds() > stale_after_seconds
        if execution.worker_id is None or execution.worker_id == worker_id or is_stale:
            execution.worker_id = worker_id
            execution.last_heartbeat_at = now
            self._executions[execution_id] = execution
            return True
        return False

    async def release_lock(self, execution_id: str, worker_id: str) -> bool:
        execution = self._executions.get(execution_id)
        if execution is None or execution.worker_id != worker_id:
            return False
        execution.worker_id = None
        self._executions[execution_id] = execution
        return True

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]:
        execution = self._executions.get(execution_id)
        if execution is None:
            return None
        if execution.worker_id not in {None, worker_id}:
            return execution
        execution.worker_id = worker_id
        execution.last_heartbeat_at = utc_now()
        self._executions[execution_id] = execution
        return execution


class MongoExecutionStore:
    def __init__(self):
        self.execution_repository = MongoExecutionRepository()
        self.event_repository = MongoExecutionEventRepository(self.execution_repository)
        self.artifact_repository = MongoExecutionArtifactRepository()
        self._db = self.execution_repository.collection.database
        self._executions = self.execution_repository.collection
        self._events = self.event_repository.collection
        self._artifacts = self.artifact_repository.collection

    async def save_execution(self, execution: Execution) -> Execution:
        existing = await self.execution_repository.get_execution(execution.id)
        if existing is None:
            return await self.execution_repository.create_execution(execution)
        return await self.execution_repository.save_execution(execution)

    async def update_execution(self, execution: Execution) -> Execution:
        return await self.execution_repository.save_execution(execution)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        return await self.execution_repository.get_execution(execution_id)

    async def list_executions(self) -> List[Execution]:
        return await self.execution_repository.list_executions()

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent:
        return await self.event_repository.append_event(event)

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        return await self.event_repository.list_events(execution_id)

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        return await self.event_repository.list_events_after_sequence(execution_id, after_sequence)

    async def delete_event(self, event_id: str) -> bool:
        return await self.event_repository.delete_event(event_id)

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        return await self.artifact_repository.create_artifact(artifact)

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        return await self.artifact_repository.list_artifacts(execution_id)

    async def list_active_executions(self) -> List[Execution]:
        active = ["queued", "running", "waiting_for_approval", "paused", "cancelling"]
        return await self.execution_repository.list_executions(filters={"status_in": active})

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]:
        return await self.execution_repository.list_executions(filters={"workflow_id": workflow_id})

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]:
        cursor = self._executions.find({"metadata.agent_ids": agent_id})
        items: List[Execution] = []
        async for record in cursor:
            record.pop("_id", None)
            record.pop("_next_event_sequence", None)
            items.append(Execution.model_validate(record))
        return items

    async def delete_execution(self, execution_id: str) -> bool:
        deleted = await self.execution_repository.delete_execution(execution_id)
        await self._events.delete_many({"execution_id": execution_id})
        await self._artifacts.delete_many({"execution_id": execution_id})
        return deleted

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool:
        now = utc_now()
        current = await self.get_execution(execution_id)
        if current is None:
            return False
        is_stale = current.last_heartbeat_at is None or (
                    now - current.last_heartbeat_at).total_seconds() > stale_after_seconds
        if current.worker_id not in {None, worker_id} and not is_stale:
            return False
        current.worker_id = worker_id
        current.last_heartbeat_at = now
        await self.update_execution(current)
        return True

    async def release_lock(self, execution_id: str, worker_id: str) -> bool:
        current = await self.get_execution(execution_id)
        if current is None or current.worker_id != worker_id:
            return False
        current.worker_id = None
        await self.update_execution(current)
        return True

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]:
        current = await self.get_execution(execution_id)
        if current is None:
            return None
        if current.worker_id not in {None, worker_id}:
            return current
        current.worker_id = worker_id
        current.last_heartbeat_at = utc_now()
        await self.update_execution(current)
        return current


class SQLExecutionStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def save_execution(self, execution: Execution) -> Execution:
        async with self.session_factory() as session:
            existing = await session.get(ExecutionORM, execution.id)
            if existing is None:
                entity = _execution_to_orm(execution)
                session.add(entity)
            else:
                entity = existing
                source = _execution_to_orm(execution)
                for field in (
                        "workflow_id",
                        "workflow_version_id",
                        "status",
                        "runtime_adapter",
                        "runtime_revision_id",
                        "runtime_fingerprint",
                        "trigger_type",
                        "trigger_payload_json",
                        "input_json",
                        "output_json",
                        "error_json",
                        "created_by",
                        "worker_id",
                        "created_at",
                        "started_at",
                        "ended_at",
                        "updated_at",
                        "last_heartbeat_at",
                        "container_id",
                        "container_name",
                        "container_image",
                        "container_status",
                        "container_started_at",
                        "container_ended_at",
                        "container_exit_code",
                        "replacement_of_execution_id",
                        "restart_reason",
                ):
                    setattr(entity, field, getattr(source, field))
            await session.commit()
            return _execution_from_orm(entity)

    async def update_execution(self, execution: Execution) -> Execution:
        return await self.save_execution(execution)

    async def get_execution(self, execution_id: str) -> Optional[Execution]:
        async with self.session_factory() as session:
            entity = await session.get(ExecutionORM, execution_id)
            return None if entity is None else _execution_from_orm(entity)

    async def list_executions(self) -> List[Execution]:
        async with self.session_factory() as session:
            result = await session.execute(select(ExecutionORM).order_by(ExecutionORM.created_at.desc()))
            return [_execution_from_orm(item) for item in result.scalars().all()]

    async def save_event(self, event: ExecutionEvent) -> ExecutionEvent:
        async with self.session_factory() as session:
            async with session.begin():
                execution = await session.get(ExecutionORM, event.execution_id, with_for_update=True)
                if execution is None:
                    raise ValueError(f"Execution '{event.execution_id}' was not found for event append")

                result = await session.execute(
                    select(func.max(ExecutionEventORM.sequence)).where(
                        ExecutionEventORM.execution_id == event.execution_id
                    )
                )
                event.sequence = int(result.scalar_one_or_none() or 0) + 1

                entity = _event_to_orm(event)
                session.add(entity)
                await session.flush()
                return _event_from_orm(entity)

    async def list_events(self, execution_id: str) -> List[ExecutionEvent]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionEventORM)
                .where(ExecutionEventORM.execution_id == execution_id)
                .order_by(ExecutionEventORM.sequence.asc())
            )
            return [_event_from_orm(item) for item in result.scalars().all()]

    async def list_events_after(self, execution_id: str, after_sequence: int) -> List[ExecutionEvent]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionEventORM)
                .where(
                    ExecutionEventORM.execution_id == execution_id,
                    ExecutionEventORM.sequence > after_sequence,
                )
                .order_by(ExecutionEventORM.sequence.asc())
            )
            return [_event_from_orm(item) for item in result.scalars().all()]

    async def delete_event(self, event_id: str) -> bool:
        async with self.session_factory() as session:
            entity = await session.get(ExecutionEventORM, event_id)
            if entity is None:
                return False
            await session.delete(entity)
            await session.commit()
            return True

    async def save_artifact(self, artifact: ExecutionArtifact) -> ExecutionArtifact:
        async with self.session_factory() as session:
            entity = _artifact_to_orm(artifact)
            session.add(entity)
            await session.commit()
            return _artifact_from_orm(entity)

    async def list_artifacts(self, execution_id: str) -> List[ExecutionArtifact]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionArtifactORM)
                .where(ExecutionArtifactORM.execution_id == execution_id)
                .order_by(ExecutionArtifactORM.created_at.asc())
            )
            return [_artifact_from_orm(item) for item in result.scalars().all()]

    async def list_active_executions(self) -> List[Execution]:
        active = {"queued", "running", "waiting_for_approval", "paused", "cancelling"}
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionORM).where(ExecutionORM.status.in_(active)).order_by(ExecutionORM.created_at.desc())
            )
            return [_execution_from_orm(item) for item in result.scalars().all()]

    async def list_executions_by_workflow(self, workflow_id: str) -> List[Execution]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ExecutionORM).where(ExecutionORM.workflow_id == workflow_id).order_by(
                    ExecutionORM.created_at.desc())
            )
            return [_execution_from_orm(item) for item in result.scalars().all()]

    async def list_executions_by_agent(self, agent_id: str) -> List[Execution]:
        executions = await self.list_executions()
        return [execution for execution in executions if agent_id in (execution.metadata.get("agent_ids") or [])]

    async def delete_execution(self, execution_id: str) -> bool:
        async with self.session_factory() as session:
            entity = await session.get(ExecutionORM, execution_id)
            if entity is None:
                return False
            await session.delete(entity)
            await session.commit()
            return True

    async def acquire_lock(self, execution_id: str, worker_id: str, stale_after_seconds: int = 30) -> bool:
        current = await self.get_execution(execution_id)
        if current is None:
            return False
        now = utc_now()
        is_stale = current.last_heartbeat_at is None or (
                    now - current.last_heartbeat_at).total_seconds() > stale_after_seconds
        if current.worker_id not in {None, worker_id} and not is_stale:
            return False
        current.worker_id = worker_id
        current.last_heartbeat_at = now
        await self.update_execution(current)
        return True

    async def release_lock(self, execution_id: str, worker_id: str) -> bool:
        current = await self.get_execution(execution_id)
        if current is None or current.worker_id != worker_id:
            return False
        current.worker_id = None
        await self.update_execution(current)
        return True

    async def heartbeat(self, execution_id: str, worker_id: str) -> Optional[Execution]:
        current = await self.get_execution(execution_id)
        if current is None:
            return None
        if current.worker_id not in {None, worker_id}:
            return current
        current.worker_id = worker_id
        current.last_heartbeat_at = utc_now()
        await self.update_execution(current)
        return current

    async def create_approval_request(self, *, execution_id: str, event_id: str | None, tool_id: str, status: str,
                                      payload: dict[str, Any]) -> str:
        request_id = f"{execution_id}:{tool_id}"
        async with self.session_factory() as session:
            entity = ApprovalRequestORM(
                id=request_id[:64],
                execution_id=execution_id,
                event_id=event_id,
                tool_id=tool_id,
                status=status,
                request_payload_json=payload,
            )
            session.merge(entity)
            await session.commit()
        return request_id[:64]

    async def update_approval_request(self, request_id: str, *, status: str, response_payload: dict[str, Any],
                                      responded_by: str | None = None) -> None:
        async with self.session_factory() as session:
            entity = await session.get(ApprovalRequestORM, request_id)
            if entity is None:
                return
            entity.status = status
            entity.response_payload_json = response_payload
            entity.responded_by = responded_by
            entity.responded_at = utc_now()
            await session.commit()

    async def create_tool_invocation(self, *, invocation_id: str, execution_id: str, tool_id: str, event_id: str | None,
                                     input_json: dict[str, Any]) -> None:
        async with self.session_factory() as session:
            entity = ToolInvocationORM(
                id=invocation_id[:64],
                execution_id=execution_id,
                tool_id=tool_id,
                event_id=event_id,
                status="running",
                input_json=input_json,
                started_at=utc_now(),
            )
            session.add(entity)
            await session.commit()

    async def update_tool_invocation(
            self,
            invocation_id: str,
            *,
            status: str,
            output_json: dict[str, Any] | None = None,
            error_json: dict[str, Any] | None = None,
            latency_ms: int | None = None,
    ) -> None:
        async with self.session_factory() as session:
            entity = await session.get(ToolInvocationORM, invocation_id[:64])
            if entity is None:
                return
            entity.status = status
            entity.output_json = output_json
            entity.error_json = error_json
            entity.ended_at = utc_now()
            entity.latency_ms = latency_ms
            await session.commit()

    async def list_all_executions(self) -> List[Execution]:
        return await self.list_executions()

    async def list_all_events(self) -> List[ExecutionEvent]:
        async with self.session_factory() as session:
            result = await session.execute(select(ExecutionEventORM).order_by(ExecutionEventORM.timestamp.asc()))
            return [_event_from_orm(item) for item in result.scalars().all()]
