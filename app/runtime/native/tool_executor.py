from __future__ import annotations

import time
from jsonschema import validate
from typing import Any, Dict, Optional
from uuid import uuid4

from app.domain import ExecutionEventType, ToolDefinition, WorkflowDefinition
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ApprovalRequiredError, ToolExecutionError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.tools.registry import ToolRegistry
from app.tools.names import tool_matches_call_name


class ToolExecutor:
    def __init__(self, approval_manager: ApprovalManager):
        self.approval_manager = approval_manager
        self.tool_registry = ToolRegistry(approval_manager=approval_manager)

    def _check_security(self, tool: ToolDefinition) -> None:
        privileged_flags = [
            tool.security.allow_shell,
            tool.security.allow_browser,
            tool.security.allow_filesystem,
            tool.security.allow_network,
        ]
        if any(privileged_flags) and not tool.security.sandbox_required:
            raise ToolExecutionError(
                f"Tool '{tool.id}' enables privileged capabilities but is missing sandbox_required=True"
            )

    def _redact_value(self, tool: ToolDefinition, value: Any) -> Any:
        if not tool.security.redaction_enabled:
            return value
        rules = {item.lower() for item in tool.security.redaction_rules}

        def redact(item: Any, *, key: str | None = None):
            if isinstance(item, dict):
                redacted = {}
                for nested_key, nested_value in item.items():
                    key_name = str(nested_key).lower()
                    if key_name in rules or any(rule in key_name for rule in rules):
                        redacted[nested_key] = "[REDACTED]"
                    else:
                        redacted[nested_key] = redact(nested_value, key=key_name)
                return redacted
            if isinstance(item, list):
                return [redact(entry, key=key) for entry in item]
            if key and any(rule in key for rule in rules):
                return "[REDACTED]"
            return item

        return redact(value)

    def resolve_tool(self, workflow: WorkflowDefinition, tool_id: str, *,
                     tool_name: Optional[str] = None) -> ToolDefinition:
        for tool in workflow.tool_definitions:
            if tool.id == tool_id or tool_matches_call_name(tool, tool_name):
                return tool
        raise ToolExecutionError(f"Tool '{tool_id or tool_name}' not found")

    async def execute(
            self,
            workflow: WorkflowDefinition,
            state: NativeExecutionState,
            emitter: ExecutionEventEmitter,
            *,
            tool_id: str,
            arguments: Dict[str, Any],
            tool_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        tool = self.resolve_tool(workflow, tool_id, tool_name=tool_name)
        validate(instance=arguments, schema=tool.input_schema or {"type": "object"})
        self._check_security(tool)

        if tool.security.approval_required:
            approval_event = await emitter.emit(
                state,
                ExecutionEventType.APPROVAL_REQUESTED,
                payload={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "arguments": self._redact_value(tool, arguments),
                    "tool_type": tool.tool_type.value,
                },
                agent_id=state.current_agent_id,
                task_id=state.current_task_id,
            )
            decision = await self.approval_manager.request_approval(
                execution_id=state.execution_id,
                tool_id=tool.id,
                payload=arguments,
                event_id=approval_event.id,
            )
            if not decision.granted:
                await emitter.emit(
                    state,
                    ExecutionEventType.APPROVAL_REJECTED,
                    payload={"tool_id": tool.id, "tool_name": tool.name, "reason": decision.reason},
                    agent_id=state.current_agent_id,
                    task_id=state.current_task_id,
                )
                if tool.security.approval_on_rejection == "skip":
                    skipped_result = {"skipped": True, "reason": decision.reason or "Approval rejected"}
                    await emitter.emit(
                        state,
                        ExecutionEventType.TOOL_CALL_COMPLETED,
                        payload={"tool_id": tool.id, "tool_name": tool.name, "output": skipped_result, "skipped": True},
                        metrics={"tool_success": True, "latency_ms": 0.0},
                        agent_id=state.current_agent_id,
                        task_id=state.current_task_id,
                        tool_call_id=tool_id,
                    )
                    return skipped_result
                reason = f": {decision.reason}" if decision.reason else f" for tool '{tool.id}'"
                await emitter.emit(
                    state,
                    ExecutionEventType.TOOL_CALL_FAILED,
                    payload={"tool_id": tool.id, "tool_name": tool.name, "error": f"Approval rejected{reason}"},
                    metrics={"tool_success": False, "latency_ms": 0.0},
                    agent_id=state.current_agent_id,
                    task_id=state.current_task_id,
                    tool_call_id=tool_id,
                )
                raise ApprovalRequiredError(f"Approval rejected{reason}")
            await emitter.emit(
                state,
                ExecutionEventType.APPROVAL_GRANTED,
                payload={"tool_id": tool.id, "tool_name": tool.name, "reason": decision.reason},
                agent_id=state.current_agent_id,
                task_id=state.current_task_id,
            )

        started_at = time.perf_counter()
        start_event = await emitter.emit(
            state,
            ExecutionEventType.TOOL_CALL_STARTED,
            payload={
                "tool_id": tool.id,
                "tool_name": tool.name,
                "arguments": self._redact_value(tool, arguments),
                "tool_type": tool.tool_type.value,
                "audit": True,
            },
            agent_id=state.current_agent_id,
            task_id=state.current_task_id,
            tool_call_id=tool_id,
        )
        invocation_id = str(uuid4())
        if hasattr(emitter.store, "create_tool_invocation"):
            await emitter.store.create_tool_invocation(
                invocation_id=invocation_id,
                execution_id=state.execution_id,
                tool_id=tool.id,
                event_id=start_event.id,
                input_json=arguments,
            )

        try:
            result = await self.tool_registry.execute(
                tool,
                arguments,
                execution_id=state.execution_id,
                workflow_id=state.workflow_id,
            )

            await emitter.emit(
                state,
                ExecutionEventType.TOOL_CALL_COMPLETED,
                payload={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "output": self._redact_value(tool, result),
                    "tool_type": tool.tool_type.value,
                    "audit": True,
                },
                metrics={
                    "latency_ms": (time.perf_counter() - started_at) * 1000,
                    "tool_success": True,
                },
                agent_id=state.current_agent_id,
                task_id=state.current_task_id,
                tool_call_id=tool_id,
            )
            if hasattr(emitter.store, "update_tool_invocation"):
                await emitter.store.update_tool_invocation(
                    invocation_id,
                    status="completed",
                    output_json=result,
                    latency_ms=int((time.perf_counter() - started_at) * 1000),
                )

            artifact_uri = result.get("artifact_uri")
            if artifact_uri:
                await emitter.record_artifact(
                    state,
                    name=result.get("artifact_name", tool.name),
                    artifact_type=result.get("artifact_type", "generic"),
                    uri=artifact_uri,
                    media_type=result.get("artifact_media_type"),
                    metadata={"tool_id": tool.id},
                )

            return result
        except Exception as exc:
            if hasattr(emitter.store, "update_tool_invocation"):
                await emitter.store.update_tool_invocation(
                    invocation_id,
                    status="failed",
                    error_json={"message": str(exc)},
                    latency_ms=int((time.perf_counter() - started_at) * 1000),
                )
            await emitter.emit(
                state,
                ExecutionEventType.TOOL_CALL_FAILED,
                payload={"tool_id": tool.id, "tool_name": tool.name, "error": str(exc),
                         "tool_type": tool.tool_type.value, "audit": True},
                metrics={
                    "latency_ms": (time.perf_counter() - started_at) * 1000,
                    "tool_success": False,
                },
                agent_id=state.current_agent_id,
                task_id=state.current_task_id,
                tool_call_id=tool_id,
            )
            if isinstance(exc, ToolExecutionError):
                raise
            raise ToolExecutionError(str(exc)) from exc
