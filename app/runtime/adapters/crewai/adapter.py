from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime
from queue import Queue
from typing import Any, Awaitable, Callable, Optional

from app.core.time import utc_now
from app.domain import AgentDefinition, Execution, ExecutionArtifact, ExecutionEventType, ExecutionStatus, \
    ModelProfileDefinition, WorkflowDefinition
from app.llm.registry import ModelProviderRegistry
from app.runtime.adapters.base import BaseRuntimeAdapter
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import ExecutionStore, ModelProfileRepository, NativeExecutionState, WorkflowRepository
from .availability import get_crewai_status
from .errors import CrewAIUnsupportedOperationError, CrewAIUnavailableError
from .events import replay_callback_events
from .mapper import run_workflow


@dataclass
class CrewAIExecutionSnapshot:
    execution: Execution
    state: Optional[NativeExecutionState]


def execute_crewai_workflow(
        workflow: WorkflowDefinition,
        inputs: dict[str, Any],
        queue,
        process_id: str,
        run_by: str,
        *,
        default_model: str,
        model_profiles: dict[str, ModelProfileDefinition] | None = None,
        model_provider_registry: ModelProviderRegistry | None = None,
        model_event_loop: asyncio.AbstractEventLoop | None = None,
):
    return run_workflow(
        workflow,
        inputs,
        queue,
        process_id,
        run_by,
        default_model=default_model,
        model_profiles=model_profiles,
        model_provider_registry=model_provider_registry,
        model_event_loop=model_event_loop,
    )


class CrewAIRuntimeAdapter(BaseRuntimeAdapter):
    adapter_name = "crewai"

    def __init__(
            self,
            *,
            workflow_repository: WorkflowRepository,
            model_profile_repository: ModelProfileRepository,
            execution_store: ExecutionStore,
            model_provider_registry: ModelProviderRegistry | None = None,
            execution_completion_handler: Optional[
                Callable[[Execution, WorkflowDefinition], Awaitable[None]]
            ] = None,
    ):
        self.workflow_repository = workflow_repository
        self.model_profile_repository = model_profile_repository
        self.execution_store = execution_store
        self.model_provider_registry = model_provider_registry
        self.execution_completion_handler = execution_completion_handler
        self.emitter = ExecutionEventEmitter(execution_store)
        self._states: dict[str, NativeExecutionState] = {}

    def get_status(self):
        return get_crewai_status()

    async def supports(self, workflow_definition: WorkflowDefinition) -> bool:
        if not self.get_status().available:
            return False
        return bool(workflow_definition.task_definitions and workflow_definition.agent_definitions)

    async def prepare_execution(self, execution: Execution) -> Execution:
        state = self._states.setdefault(
            execution.id,
            NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id),
        )
        await self.execution_store.save_execution(execution)
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_CREATED,
            payload={"workflow_id": execution.workflow_id, "trigger": execution.metadata.get("trigger", {})},
        )
        return execution

    async def start_execution(self, execution_id: str) -> Execution:
        status = self.get_status()
        if not status.available:
            raise CrewAIUnavailableError(status.detail or "CrewAI is unavailable")

        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ValueError(f"Execution '{execution_id}' not found")
        workflow = await self.workflow_repository.get_workflow(execution.workflow_id)
        if workflow is None:
            raise ValueError(f"Workflow '{execution.workflow_id}' not found")
        state = self._states.setdefault(
            execution.id,
            NativeExecutionState(execution_id=execution.id, workflow_id=execution.workflow_id),
        )

        execution.status = ExecutionStatus.RUNNING
        execution.started_at = execution.started_at or utc_now()
        await self.execution_store.update_execution(execution)
        await self.emitter.emit(state, ExecutionEventType.EXECUTION_STARTED, payload={"workflow_id": workflow.id})

        log_path = self._initialize_log_file(execution.id)
        queue: Queue[str] = Queue()
        model_profiles = await self._resolve_model_profiles(workflow)
        default_model = next(iter(model_profiles.values())).model if model_profiles else "gpt-4o-mini"
        run_by = execution.created_by or "system"
        model_event_loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()
        callback_stream_task = asyncio.create_task(
            self._stream_callback_events(log_path, state, stop_event),
            name=f"crewai-callback-stream-{execution.id}",
        )
        result = None
        try:
            result = await asyncio.to_thread(
                execute_crewai_workflow,
                workflow,
                execution.input_payload,
                queue,
                execution.id,
                run_by,
                default_model=default_model,
                model_profiles=model_profiles,
                model_provider_registry=self.model_provider_registry,
                model_event_loop=model_event_loop,
            )
            if result is None and not queue.empty():
                result = queue.get_nowait()
        finally:
            stop_event.set()
            await callback_stream_task

        await self._record_final_output(execution, state, workflow, result)
        return execution

    async def pause_execution(self, execution_id: str) -> Execution:
        raise CrewAIUnsupportedOperationError(f"Runtime adapter '{self.adapter_name}' does not support pause_execution")

    async def resume_execution(self, execution_id: str) -> Execution:
        raise CrewAIUnsupportedOperationError(
            f"Runtime adapter '{self.adapter_name}' does not support resume_execution")

    async def cancel_execution(self, execution_id: str) -> Execution:
        raise CrewAIUnsupportedOperationError(
            f"Runtime adapter '{self.adapter_name}' does not support cancel_execution")

    async def get_execution_state(self, execution_id: str):
        execution = await self.execution_store.get_execution(execution_id)
        if execution is None:
            raise ValueError(f"Execution '{execution_id}' not found")
        return CrewAIExecutionSnapshot(execution=execution, state=self._states.get(execution_id))

    def _initialize_log_file(self, execution_id: str) -> str:
        os.makedirs("logs", exist_ok=True)
        path = f"logs/{execution_id}.json"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([], handle)
        return path

    def _agent_runtime_config(self, agent: AgentDefinition) -> dict[str, Any]:
        runtime_config = agent.metadata.get("runtime_config")
        return runtime_config if isinstance(runtime_config, dict) else {}

    def _build_runtime_override_profile(
            self,
            agent: AgentDefinition,
            llm_override: dict[str, Any],
            base_profile: ModelProfileDefinition | None = None,
    ) -> ModelProfileDefinition | None:
        provider = llm_override.get("provider")
        model = llm_override.get("model")
        if not isinstance(provider, str) or not isinstance(model, str) or not model.strip():
            return None

        return ModelProfileDefinition(
            id=f"runtime-override-{agent.id}",
            name=f"Runtime Override {agent.name}",
            provider=provider,
            model=model.strip(),
            description=f"Per-run runtime override for agent {agent.id}",
            base_url=(
                llm_override.get("base_url")
                if isinstance(llm_override.get("base_url"), str) and llm_override.get("base_url").strip()
                else (base_profile.base_url if base_profile is not None else None)
            ),
            api_key_ref=(
                llm_override.get("api_key")
                if isinstance(llm_override.get("api_key"), str) and llm_override.get("api_key").strip()
                else (base_profile.api_key_ref if base_profile is not None else None)
            ),
            temperature=base_profile.temperature if base_profile is not None else None,
            max_tokens=base_profile.max_tokens if base_profile is not None else None,
            context_window=base_profile.context_window if base_profile is not None else None,
            top_p=base_profile.top_p if base_profile is not None else None,
            supports_tools=base_profile.supports_tools if base_profile is not None else True,
            supports_structured_output=(
                base_profile.supports_structured_output if base_profile is not None else False
            ),
            supports_vision=base_profile.supports_vision if base_profile is not None else False,
            supports_streaming=base_profile.supports_streaming if base_profile is not None else True,
            parameters=dict(base_profile.parameters) if base_profile is not None else {},
            framework_hints=base_profile.framework_hints if base_profile is not None else agent.framework_hints,
        )

    async def _resolve_model_profiles(self, workflow: WorkflowDefinition) -> dict[str, ModelProfileDefinition]:
        profiles: dict[str, ModelProfileDefinition] = {}
        for agent in workflow.agent_definitions:
            runtime_config = self._agent_runtime_config(agent)
            runtime_profile_id = runtime_config.get("model_profile_id")
            requested_profile_id = runtime_profile_id if isinstance(runtime_profile_id, str) and runtime_profile_id else agent.model_profile_id
            base_profile: ModelProfileDefinition | None = None
            if requested_profile_id:
                base_profile = await self.model_profile_repository.get_profile(requested_profile_id)

            llm_override = runtime_config.get("llm_override")
            if isinstance(llm_override, dict):
                runtime_profile = self._build_runtime_override_profile(agent, llm_override, base_profile=base_profile)
                if runtime_profile is not None:
                    profiles[agent.id] = runtime_profile
                    continue

            if base_profile is not None:
                profiles[agent.id] = base_profile
        return profiles

    async def _stream_callback_events(
            self,
            log_path: str,
            state: NativeExecutionState,
            stop_event: asyncio.Event,
            *,
            poll_interval_seconds: float = 0.1,
    ) -> None:
        callback_index = 0
        while True:
            callback_index = await replay_callback_events(
                self.emitter,
                state,
                log_path,
                start_index=callback_index,
            )
            if stop_event.is_set():
                callback_index = await replay_callback_events(
                    self.emitter,
                    state,
                    log_path,
                    start_index=callback_index,
                )
                return
            await asyncio.sleep(poll_interval_seconds)

    async def _record_final_output(
            self,
            execution: Execution,
            state: NativeExecutionState,
            workflow: WorkflowDefinition,
            result: Any,
    ) -> None:
        execution.output_payload = {"final_output": result}
        execution.completed_at = utc_now()
        is_error = isinstance(result, str) and result.startswith("Error Encountered:")
        execution.status = ExecutionStatus.FAILED if is_error else ExecutionStatus.COMPLETED
        execution.error = result if is_error else None
        await self.execution_store.update_execution(execution)
        final_artifact = ExecutionArtifact(
            execution_id=execution.id,
            name="final_output.txt",
            artifact_type="text",
            content_text=str(result),
            uri=f"execution://{execution.id}/final_output",
            media_type="text/plain",
            size_bytes=len(str(result).encode("utf-8")),
            metadata={"adapter": self.adapter_name},
        )
        await self.execution_store.save_artifact(final_artifact)
        await self.emitter.emit(
            state,
            ExecutionEventType.ARTIFACT_CREATED,
            payload={"artifact_id": final_artifact.id, "name": final_artifact.name, "uri": final_artifact.uri},
        )
        await self.emitter.emit(
            state,
            ExecutionEventType.EXECUTION_FAILED if is_error else ExecutionEventType.EXECUTION_COMPLETED,
            payload={"output": execution.output_payload, "error": execution.error},
        )
        if self.execution_completion_handler is not None:
            try:
                await self.execution_completion_handler(execution, workflow)
            except Exception:
                return
