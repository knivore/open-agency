from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, Awaitable, Callable, Dict, List, Tuple
from uuid import uuid4

from app.domain import AgentDefinition, Execution, ExecutionEventType, ModelProfileDefinition, TaskDefinition, WorkflowDefinition
from app.llm.base import BaseModelClient, ModelMessage
from app.runtime.native.errors import ExecutionCancelledError, ExecutionPausedError, MaxIterationsReachedError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.runtime.native.tool_executor import ToolExecutor
from app.tools.names import tool_call_name


MemoryPromptBuilder = Callable[
    [WorkflowDefinition, TaskDefinition, AgentDefinition, Execution, Dict[str, Any], NativeExecutionState],
    Awaitable[str],
]


class AgentExecutor:
    def __init__(self, tool_executor: ToolExecutor, *, memory_prompt_builder: MemoryPromptBuilder | None = None):
        self.tool_executor = tool_executor
        self.memory_prompt_builder = memory_prompt_builder

    async def _build_messages(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            execution: Execution,
            execution_input: Dict[str, Any],
            state: NativeExecutionState,
    ) -> List[ModelMessage]:
        system_parts = [
            agent.system_prompt or "",
            f"Role: {agent.role}" if agent.role else "",
            f"Instructions: {agent.instructions}" if agent.instructions else "",
            f"Backstory: {agent.backstory}" if agent.backstory else "",
            task.instructions or "",
            f"Expected output: {task.expected_output}" if task.expected_output else "",
        ]
        if self.memory_prompt_builder is not None:
            try:
                memory_prompt = await self.memory_prompt_builder(
                    workflow,
                    task,
                    agent,
                    execution,
                    execution_input,
                    state,
                )
            except Exception:
                memory_prompt = ""
            if memory_prompt:
                system_parts.append(memory_prompt)
        if state.memory_entries:
            system_parts.append(f"Memory: {json.dumps(state.memory_entries)}")
        if state.node_outputs:
            system_parts.append(f"Previous node outputs: {json.dumps(state.node_outputs, default=str)}")

        user_content = {
            "workflow": workflow.name,
            "task": task.description,
            "input": execution_input,
        }
        return [
            ModelMessage(role="system", content="\n".join(part for part in system_parts if part)),
            ModelMessage(role="user", content=json.dumps(user_content, default=str)),
        ]

    def _tool_payload(self, workflow: WorkflowDefinition, agent: AgentDefinition, task: TaskDefinition) -> List[
        Dict[str, Any]]:
        allowed_tool_ids = task.tool_ids or agent.tool_ids
        tools = []
        for tool in workflow.tool_definitions:
            if tool.id not in allowed_tool_ids:
                continue
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_call_name(tool),
                        "description": tool.description,
                        "parameters": tool.input_schema or {"type": "object"},
                    },
                }
            )
        return tools

    def _assert_not_interrupted(self, state: NativeExecutionState) -> None:
        if state.cancelled:
            raise ExecutionCancelledError("Execution was cancelled")
        if state.paused:
            raise ExecutionPausedError("Execution is paused")

    async def execute_task(
            self,
            workflow: WorkflowDefinition,
            task: TaskDefinition,
            agent: AgentDefinition,
            profile: ModelProfileDefinition,
            model_client: BaseModelClient,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            execution: Execution,
            execution_input: Dict[str, Any],
    ) -> Tuple[Any, List[ModelMessage]]:
        messages = await self._build_messages(workflow, task, agent, execution, execution_input, state)
        max_iterations = int(agent.framework_hints.adapter_config.get("max_iterations", 5))
        tool_payload = self._tool_payload(workflow, agent, task)

        for iteration in range(1, max_iterations + 1):
            self._assert_not_interrupted(state)
            model_request_id = str(uuid4())
            log_prompts = bool(workflow.metadata.get("log_prompts", True))
            await emitter.emit(
                state,
                ExecutionEventType.LLM_REQUEST_CREATED,
                actor=agent.name,
                payload={
                    "iteration": iteration,
                    "model_profile_id": profile.id,
                    "message_count": len(messages),
                    "messages": [asdict(message) for message in
                                 messages] if log_prompts else "[PROMPT_LOGGING_DISABLED]",
                },
                metrics={
                    "model_provider": profile.provider,
                    "model_name": profile.model,
                    "input_tokens": model_client.count_tokens(messages) or 0,
                },
                agent_id=agent.id,
                task_id=task.id,
                model_request_id=model_request_id,
            )

            if hasattr(model_client, "agenerate_text"):
                response = await model_client.agenerate_text(
                    messages,
                    temperature=profile.temperature,
                    max_tokens=profile.max_tokens,
                    tools=tool_payload if tool_payload else None,
                )
            else:
                response = await asyncio.to_thread(
                    model_client.generate_text,
                    messages,
                    temperature=profile.temperature,
                    max_tokens=profile.max_tokens,
                    tools=tool_payload if tool_payload else None,
                )

            await emitter.emit(
                state,
                ExecutionEventType.LLM_RESPONSE_CREATED,
                actor=agent.name,
                payload={
                    "iteration": iteration,
                    "content": response.content,
                    "tool_calls": [tool_call.name for tool_call in response.tool_calls],
                    "usage": response.usage,
                    "latency_ms": response.latency_ms,
                    "model_provider": response.provider or profile.provider,
                    "model_name": response.model or profile.model,
                },
                metrics={
                    "model_provider": response.provider or profile.provider,
                    "model_name": response.model or profile.model,
                    "latency_ms": response.latency_ms,
                    "input_tokens": response.usage.get("input_tokens") or response.usage.get("prompt_tokens") or 0,
                    "output_tokens": response.usage.get("output_tokens") or response.usage.get(
                        "completion_tokens") or 0,
                    "total_tokens": response.usage.get("total_tokens") or (
                            (response.usage.get("input_tokens") or response.usage.get("prompt_tokens") or 0)
                            + (response.usage.get("output_tokens") or response.usage.get("completion_tokens") or 0)
                    ),
                    "estimated_cost": float(response.usage.get("estimated_cost", 0.0) or 0.0),
                },
                agent_id=agent.id,
                task_id=task.id,
                model_request_id=model_request_id,
            )

            if response.content:
                messages.append(ModelMessage(role="assistant", content=response.content, name=agent.name))
                await emitter.emit(
                    state,
                    ExecutionEventType.AGENT_MESSAGE_CREATED,
                    actor=agent.name,
                    payload={"iteration": iteration, "content": response.content},
                    agent_id=agent.id,
                    task_id=task.id,
                )

            if response.tool_calls:
                for tool_call in response.tool_calls:
                    tool_result = await self.tool_executor.execute(
                        workflow,
                        state,
                        emitter,
                        tool_id=tool_call.id or "",
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                    )
                    state.memory_entries.append(
                        {"tool_name": tool_call.name, "arguments": tool_call.arguments, "output": tool_result}
                    )
                    messages.append(
                        ModelMessage(
                            role="tool",
                            content=json.dumps(tool_result, default=str),
                            name=tool_call.name,
                            tool_call_id=tool_call.id,
                        )
                    )
                continue

            return response.content, messages

        raise MaxIterationsReachedError(f"Max iterations reached for task '{task.id}'")
