from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from app.domain import AgentDefinition, Execution, TaskDefinition, UserDefinition, WorkflowDefinition
from app.runtime.native.state import NativeExecutionState


@dataclass(slots=True)
class SharedMemoryPromptBuilder:
    context: Any

    async def __call__(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            execution_input: dict[str, Any],
            state: NativeExecutionState,
    ) -> str:
        config = self._shared_memory_config(workflow=workflow, agent=agent)
        if not self._enabled(agent=agent, config=config):
            return ""

        user_id = self._resolve_user_id(workflow=workflow, execution=execution)
        workspace_id = self._resolve_workspace_id(workflow=workflow, execution=execution)
        current_user = await self._resolve_current_user(user_id)
        query = self._build_query(
            task=task,
            execution_input=execution_input,
            node_outputs=state.node_outputs,
        )
        from app.services.memory import MemoryService

        service = MemoryService(self.context)
        operational_context = await service.retrieve_operational_context(
            agent_id=agent.id,
            workflow_id=workflow.id,
            workspace_id=workspace_id,
            user_id=user_id,
            query=query,
            current_user=current_user,
            limit_per_layer=self._limit_per_layer(config),
        )
        return service.format_operational_context_for_prompt(operational_context)

    @staticmethod
    def _shared_memory_config(*, workflow: WorkflowDefinition, agent: AgentDefinition) -> dict[str, Any]:
        config: dict[str, Any] = {}
        workflow_config = workflow.metadata.get("shared_memory")
        if isinstance(workflow_config, dict):
            config.update(workflow_config)
        agent_config = agent.memory.config.get("shared_memory")
        if isinstance(agent_config, dict):
            config.update(agent_config)
        return config

    @staticmethod
    def _enabled(*, agent: AgentDefinition, config: dict[str, Any]) -> bool:
        if "enabled" in config:
            return bool(config.get("enabled"))
        return bool(agent.memory.enabled and agent.memory.scope != "execution")

    @staticmethod
    def _resolve_user_id(*, workflow: WorkflowDefinition, execution: Execution) -> str | None:
        for candidate in (
                execution.created_by,
                execution.trigger_payload.get("created_by"),
                execution.trigger_payload.get("run_by"),
                workflow.metadata.get("created_by"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _resolve_workspace_id(*, workflow: WorkflowDefinition, execution: Execution) -> str | None:
        for candidate in (
                execution.metadata.get("workspace_id"),
                execution.trigger_payload.get("workspace_id"),
                workflow.metadata.get("workspace_id"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    async def _resolve_current_user(self, user_id: str | None) -> UserDefinition | None:
        if not user_id:
            return None
        get_user = getattr(self.context.user_repo, "get", None)
        if callable(get_user):
            existing = await get_user(user_id)
            if existing is not None:
                return existing
        return UserDefinition(id=user_id, email=f"{user_id}@memory.local")

    @staticmethod
    def _build_query(
            *,
            task: TaskDefinition,
            execution_input: dict[str, Any],
            node_outputs: dict[str, Any],
    ) -> str:
        parts = [
            task.name,
            task.description,
            task.instructions or "",
            task.expected_output or "",
            json.dumps(execution_input, sort_keys=True, default=str),
            json.dumps(node_outputs, sort_keys=True, default=str),
        ]
        query = "\n".join(part for part in parts if part.strip())
        return query[:4000]

    @staticmethod
    def _limit_per_layer(config: dict[str, Any]) -> dict[str, int] | None:
        raw_limits = config.get("limit_per_layer")
        if not isinstance(raw_limits, dict):
            return None
        limits: dict[str, int] = {}
        for key, value in raw_limits.items():
            if not isinstance(key, str):
                continue
            try:
                limits[key] = int(value)
            except (TypeError, ValueError):
                continue
        return limits or None
