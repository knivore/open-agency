from __future__ import annotations

import time
from jsonschema import validate
from typing import Any, Dict, Optional
from uuid import uuid4

from app.domain import ExecutionEventType, ToolDefinition, WorkflowDefinition
from app.integrations.connectors import normalize_connector_provider_key
from app.runtime.native.approvals import ApprovalManager
from app.runtime.native.errors import ApprovalRequiredError, ToolExecutionError
from app.runtime.native.events import ExecutionEventEmitter
from app.runtime.native.state import NativeExecutionState
from app.tools.cli_discovery import list_builtin_tool_definitions
from app.tools.names import tool_matches_call_name
from app.tools.registry import ToolRegistry
from app.tools.risk import risk_labels_for_tool_definition


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

    def _connector_binding_payload(self, binding: Any) -> dict[str, Any]:
        if hasattr(binding, "model_dump"):
            payload = binding.model_dump(mode="json")
        elif isinstance(binding, dict):
            payload = dict(binding)
        else:
            payload = {}
        legacy_ref = str(payload.pop("ref", "") or "").strip()
        if legacy_ref and not str(payload.get("credential_id") or "").strip():
            payload["credential_id"] = legacy_ref
        provider = normalize_connector_provider_key(str(payload.get("provider") or ""))
        if not provider:
            purpose = str(payload.get("purpose") or "").strip()
            if purpose:
                provider = normalize_connector_provider_key(purpose.split("_", 1)[0].split("-", 1)[0])
        return {**payload, "provider": provider} if provider else payload

    def _workflow_connector_bindings(self, workflow: WorkflowDefinition) -> list[dict[str, Any]]:
        bindings = workflow.metadata.get("connector_bindings") if isinstance(workflow.metadata, dict) else None
        if not isinstance(bindings, list):
            return []
        return [
            self._connector_binding_payload(binding)
            for binding in bindings
            if isinstance(binding, dict) or hasattr(binding, "model_dump")
        ]

    def _tool_connector_bindings(self, tool: ToolDefinition) -> list[dict[str, Any]]:
        return [
            self._connector_binding_payload(binding)
            for binding in tool.security.connector_bindings
        ]

    def _connector_provider_hint(self, tool: ToolDefinition, arguments: dict[str, Any]) -> str | None:
        provider = arguments.get("provider")
        if isinstance(provider, str) and provider.strip():
            return normalize_connector_provider_key(provider.strip())
        config = tool.implementation.config or {}
        for key in ("provider", "provider_key", "connector", "connector_provider"):
            value = config.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_connector_provider_key(value.strip())
        return None

    def _is_connector_like_tool(self, tool: ToolDefinition, arguments: dict[str, Any]) -> bool:
        tags = {tag.strip().lower() for tag in tool.tags if isinstance(tag, str)}
        if {"connector", "integration"} & tags:
            return True
        if any(key in arguments for key in ("credential_id", "connector_credential_id", "provider")):
            return True
        config = tool.implementation.config or {}
        return any(key in config for key in ("provider", "provider_key", "connector", "connector_provider"))

    def _resolve_connector_binding(
            self,
            *,
            workflow: WorkflowDefinition,
            tool: ToolDefinition,
            arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        provider_hint = self._connector_provider_hint(tool, arguments)
        explicit_credential_id = arguments.get("credential_id") or arguments.get("connector_credential_id")
        explicit_credential_id = (
            explicit_credential_id.strip()
            if isinstance(explicit_credential_id, str) and explicit_credential_id.strip()
            else None
        )
        bindings = [*self._tool_connector_bindings(tool), *self._workflow_connector_bindings(workflow)]
        if provider_hint:
            bindings = [
                binding
                for binding in bindings
                if str(binding.get("provider") or "").strip().lower() == provider_hint.lower()
            ]
        if explicit_credential_id:
            matches = [
                binding
                for binding in bindings
                if str(binding.get("credential_id") or "").strip() == explicit_credential_id
            ]
            if not matches:
                raise ToolExecutionError(
                    f"Tool '{tool.id}' requested credential_id '{explicit_credential_id}' "
                    "but it is not in the tool or workflow connector bindings."
                )
            return matches[0]

        if len(bindings) == 1:
            return bindings[0]
        if len(bindings) > 1:
            raise ToolExecutionError(
                f"Tool '{tool.id}' has multiple connector bindings; pass credential_id or narrow provider scope."
            )
        if self._is_connector_like_tool(tool, arguments):
            raise ToolExecutionError(
                f"Tool '{tool.id}' is connector-backed but has no connector binding."
            )
        return None

    def resolve_tool(self, workflow: WorkflowDefinition, tool_id: str, *,
                     tool_name: Optional[str] = None) -> ToolDefinition:
        # Built-in Agency tools may be referenced by ID from task/agent definitions without
        # being embedded in each workflow document. Keep workflow-local definitions first so
        # bespoke tools can still shadow catalog entries intentionally.
        tools = [*workflow.tool_definitions, *list_builtin_tool_definitions()]
        seen: set[str] = set()
        for tool in tools:
            if tool.id in seen:
                continue
            seen.add(tool.id)
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
        connector_binding = self._resolve_connector_binding(
            workflow=workflow,
            tool=tool,
            arguments=arguments,
        )
        connector_binding_payload = (
            {"connector_binding": self._redact_value(tool, connector_binding)}
            if connector_binding
            else {}
        )
        self._check_security(tool)
        risk_labels = risk_labels_for_tool_definition(tool)
        risk_payload = {
            "risk_labels": risk_labels,
            "local_privileged_execution": "local_privileged_execution" in risk_labels,
        }
        approval_metadata = {
            "tool_id": tool.id,
            "tool_name": tool.name,
            "tool_type": tool.tool_type.value,
            "agent_id": state.current_agent_id,
            "task_id": state.current_task_id,
            **connector_binding_payload,
            **risk_payload,
        }

        if tool.security.approval_required:
            approval_event = await emitter.emit(
                state,
                ExecutionEventType.APPROVAL_REQUESTED,
                payload={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "arguments": self._redact_value(tool, arguments),
                    "tool_type": tool.tool_type.value,
                    **connector_binding_payload,
                    **risk_payload,
                },
                agent_id=state.current_agent_id,
                task_id=state.current_task_id,
            )
            decision = await self.approval_manager.request_approval(
                execution_id=state.execution_id,
                tool_id=tool.id,
                payload=arguments,
                event_id=approval_event.id,
                approval_metadata={**approval_metadata, "approval_event_id": approval_event.id},
            )
            decision_metadata = decision.metadata or {}
            if not decision.granted:
                await emitter.emit(
                    state,
                    ExecutionEventType.APPROVAL_REJECTED,
                    payload={
                        "tool_id": tool.id,
                        "tool_name": tool.name,
                        "reason": decision.reason,
                        "decision_metadata": decision_metadata,
                    },
                    agent_id=state.current_agent_id,
                    task_id=state.current_task_id,
                )
                if tool.security.approval_on_rejection == "skip":
                    skipped_result = {"skipped": True, "reason": decision.reason or "Approval rejected"}
                    await emitter.emit(
                        state,
                        ExecutionEventType.TOOL_CALL_COMPLETED,
                        payload={
                            "tool_id": tool.id,
                            "tool_name": tool.name,
                            "output": skipped_result,
                            "skipped": True,
                            **connector_binding_payload,
                            **risk_payload,
                        },
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
                    payload={
                        "tool_id": tool.id,
                        "tool_name": tool.name,
                        "error": f"Approval rejected{reason}",
                        **connector_binding_payload,
                        **risk_payload,
                    },
                    metrics={"tool_success": False, "latency_ms": 0.0},
                    agent_id=state.current_agent_id,
                    task_id=state.current_task_id,
                    tool_call_id=tool_id,
                )
                raise ApprovalRequiredError(f"Approval rejected{reason}")
            await emitter.emit(
                state,
                ExecutionEventType.APPROVAL_GRANTED,
                payload={
                    "tool_id": tool.id,
                    "tool_name": tool.name,
                    "reason": decision.reason,
                    "decision_metadata": decision_metadata,
                    **connector_binding_payload,
                },
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
                **connector_binding_payload,
                **risk_payload,
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
                task_id=state.current_task_id,
                agent_id=state.current_agent_id,
                tool_call_id=tool_id,
                connector_binding=connector_binding,
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
                    **connector_binding_payload,
                    **risk_payload,
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
                         "tool_type": tool.tool_type.value, "audit": True,
                         **connector_binding_payload, **risk_payload},
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
