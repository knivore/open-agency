from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.core.config import get_settings
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
        prompt_parts: list[str] = []
        if get_settings().memory_context_pack_enabled and self._context_packs_enabled(config):
            context_packs = []
            selected_context_pack_id = self._selected_context_pack_id(execution)
            if selected_context_pack_id:
                selected_context_pack = await service.get_context_pack_by_id(
                    selected_context_pack_id,
                    current_user=current_user,
                )
                if selected_context_pack is not None:
                    context_packs.append(selected_context_pack)
            scoped_context_packs = await service.list_context_packs_for_agent_scope(
                agent_id=agent.id,
                workflow_id=workflow.id,
                workspace_id=workspace_id,
                user_id=user_id,
                mode=self._context_pack_mode(config),
                query=query,
                limit=self._context_pack_limit(config),
                current_user=current_user,
            )
            seen_context_pack_ids = {item.id for item in context_packs}
            context_packs.extend(item for item in scoped_context_packs if item.id not in seen_context_pack_ids)
            context_pack_prompt = service.format_context_packs_for_prompt(context_packs)
            if context_pack_prompt:
                prompt_parts.append(context_pack_prompt)
        operational_prompt = service.format_operational_context_for_prompt(operational_context)
        if operational_prompt:
            prompt_parts.append(operational_prompt)
        return "\n\n".join(prompt_parts)

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

    @staticmethod
    def _context_packs_enabled(config: dict[str, Any]) -> bool:
        if "context_packs_enabled" in config:
            return bool(config.get("context_packs_enabled"))
        return True

    @staticmethod
    def _context_pack_mode(config: dict[str, Any]) -> str | None:
        raw_mode = config.get("context_pack_mode")
        if not isinstance(raw_mode, str) or not raw_mode.strip():
            return None
        return raw_mode.strip().lower()

    @staticmethod
    def _context_pack_limit(config: dict[str, Any]) -> int:
        raw_limit = config.get("context_pack_limit", 2)
        if not isinstance(raw_limit, int):
            return 2
        return max(min(raw_limit, 10), 0)

    @staticmethod
    def _selected_context_pack_id(execution: Execution) -> str | None:
        for container in (execution.trigger_payload, execution.input_payload):
            if not isinstance(container, dict):
                continue
            for key in ("context_pack_id", "contextPackId"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None
