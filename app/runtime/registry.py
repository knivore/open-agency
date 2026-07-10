"""Runtime adapter registry and execution-host selection.

The registry is the boundary between workflow definitions and concrete runtime
adapters. It persists prepared executions through the selected adapter while
keeping adapter lookup, host metadata, and lifecycle dispatch in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.core.config import get_settings
from app.core.time import utc_now
from app.domain import Execution, ExecutionStatus, RuntimeAdapterType, WorkflowDefinition
from app.runtime.adapters.base import BaseRuntimeAdapter
from app.runtime.execution_lifecycle import build_execution_lifecycle_metadata, resolve_execution_runtime_policy
from app.runtime.native.errors import ExecutionNotFoundError, WorkflowNotFoundError
from app.runtime.native.state import ExecutionStore, ModelProfileRepository, WorkflowRepository

EXECUTION_HOST_DOCKER = "docker"
EXECUTION_HOST_LOCAL = "local"
EXECUTION_HOSTS = {EXECUTION_HOST_DOCKER, EXECUTION_HOST_LOCAL}


def _normalized_execution_host(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if normalized in EXECUTION_HOSTS else None


def resolve_execution_host(trigger: dict, workflow_metadata: dict) -> str | None:
    """Resolve local-vs-Docker execution preference from trigger and workflow metadata."""
    workflow_runtime_metadata = workflow_metadata.get("runtime_execution")
    if not isinstance(workflow_runtime_metadata, dict):
        workflow_runtime_metadata = {}

    candidates = (
        trigger.get("execution_host"),
        trigger.get("executionHost"),
        workflow_runtime_metadata.get("execution_host"),
        workflow_runtime_metadata.get("executionHost"),
        workflow_metadata.get("execution_host"),
        workflow_metadata.get("executionHost"),
    )
    for candidate in candidates:
        normalized = _normalized_execution_host(candidate)
        if normalized:
            return normalized
    return None


def _normalized_runtime_adapter_id(runtime_adapter_id: Optional[str]) -> str | None:
    if runtime_adapter_id is None:
        return None
    normalized = runtime_adapter_id.strip()
    return normalized or None


@dataclass
class RuntimeAdapterRegistry:
    """Dispatch execution lifecycle operations to registered runtime adapters."""

    workflow_repository: WorkflowRepository
    model_profile_repository: ModelProfileRepository
    execution_store: ExecutionStore
    _adapters: dict[str, BaseRuntimeAdapter]

    def __init__(self, workflow_repository: WorkflowRepository, model_profile_repository: ModelProfileRepository,
                 execution_store: ExecutionStore):
        self.workflow_repository = workflow_repository
        self.model_profile_repository = model_profile_repository
        self.execution_store = execution_store
        self._adapters = {}

    def register(self, adapter: BaseRuntimeAdapter) -> None:
        self._adapters[adapter.adapter_name] = adapter

    def get(self, adapter_name: str) -> BaseRuntimeAdapter:
        if adapter_name not in self._adapters:
            raise KeyError(f"Runtime adapter '{adapter_name}' is not registered")
        return self._adapters[adapter_name]

    def registered_adapter_names(self) -> list[str]:
        return sorted(self._adapters.keys())

    async def register_workflow(self, workflow: WorkflowDefinition) -> WorkflowDefinition:
        return await self.workflow_repository.save_workflow(workflow)

    async def register_model_profile(self, profile):
        return await self.model_profile_repository.save_profile(profile)

    async def _resolve_adapter_for_workflow(
            self,
            workflow: WorkflowDefinition,
            runtime_adapter_id: Optional[str] = None,
    ) -> BaseRuntimeAdapter:
        adapter_name = _normalized_runtime_adapter_id(runtime_adapter_id) or RuntimeAdapterType.NATIVE.value
        adapter = self.get(adapter_name)
        if not await adapter.supports(workflow):
            raise WorkflowNotFoundError(f"Workflow '{workflow.id}' is not supported by adapter '{adapter_name}'")
        return adapter

    async def _resolve_adapter_for_execution(self, execution_id: str) -> tuple[Execution, BaseRuntimeAdapter]:
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ExecutionNotFoundError(f"Execution '{execution_id}' was not found")
        adapter = self.get(execution.runtime_adapter_id)
        return execution, adapter

    async def create_execution(
            self,
            workflow_id: str,
            input_payload: dict,
            trigger: dict,
            runtime_adapter_id: Optional[str] = None,
            goal_id: str | None = None,
    ) -> Execution:
        workflow = await self.workflow_repository.get_workflow(workflow_id)
        if workflow is None:
            raise WorkflowNotFoundError(f"Workflow '{workflow_id}' was not found")
        requested_adapter_id = _normalized_runtime_adapter_id(runtime_adapter_id)
        adapter = await self._resolve_adapter_for_workflow(workflow, requested_adapter_id)
        agent_ids = [agent.id for agent in workflow.agent_definitions]
        execution_lifecycle = build_execution_lifecycle_metadata(
            trigger=trigger,
            workflow_metadata=workflow.metadata,
        )
        execution = Execution(
            workflow_id=workflow_id,
            goal_id=goal_id,
            runtime_adapter_id=adapter.adapter_name,
            status=ExecutionStatus.CREATED,
            trigger_type=trigger.get("type", "manual"),
            trigger_payload=trigger,
            input_payload=input_payload,
            created_by=trigger.get("created_by") or trigger.get("run_by"),
            metadata={
                "trigger": trigger,
                "goal_id": goal_id,
                "requested_adapter": requested_adapter_id,
                "execution_host": resolve_execution_host(trigger, workflow.metadata),
                "agent_ids": agent_ids,
                "execution_lifecycle": execution_lifecycle,
            },
        )
        runtime_policy = resolve_execution_runtime_policy(
            settings=get_settings(),
            workflow=workflow,
            execution=execution,
            include_workflow_member_maxima=True,
        )
        execution.metadata["runtime_policy"] = runtime_policy.model_dump()
        await adapter.prepare_execution(execution)
        return execution

    async def start_execution(self, execution_id: str) -> Execution:
        _execution, adapter = await self._resolve_adapter_for_execution(execution_id)
        return await adapter.start_execution(execution_id)

    async def pause_execution(self, execution_id: str) -> Execution:
        _execution, adapter = await self._resolve_adapter_for_execution(execution_id)
        return await adapter.pause_execution(execution_id)

    async def resume_execution(self, execution_id: str) -> Execution:
        _execution, adapter = await self._resolve_adapter_for_execution(execution_id)
        return await adapter.resume_execution(execution_id)

    async def cancel_execution(self, execution_id: str) -> Execution:
        _execution, adapter = await self._resolve_adapter_for_execution(execution_id)
        return await adapter.cancel_execution(execution_id)

    async def get_execution_state(self, execution_id: str):
        _execution, adapter = await self._resolve_adapter_for_execution(execution_id)
        return await adapter.get_execution_state(execution_id)

    async def mark_execution_completed(self, execution: Execution, *, output_payload=None,
                                       error: Optional[str] = None) -> Execution:
        execution.completed_at = utc_now()
        execution.output_payload = output_payload
        execution.error = error
        execution.status = ExecutionStatus.COMPLETED if error is None else ExecutionStatus.FAILED
        await self.execution_store.update_execution(execution)
        return execution
