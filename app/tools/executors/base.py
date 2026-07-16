from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain import ToolDefinition, ToolType
from app.protocols.mcp.registry import MCPClientRegistry
from app.runtime.native.approvals import ApprovalManager


@dataclass(slots=True)
class ToolExecutionContext:
    execution_id: str
    workflow_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    tool_call_id: str | None = None
    runtime_registry: Any | None = None
    approval_manager: ApprovalManager | None = None
    mcp_registry: MCPClientRegistry | None = None
    execution_store: Any | None = None
    # System workflow tools such as Agency Graph need the application composition
    # root; their target is a routing namespace, not a persisted workflow id.
    api_tool_runtime_executor: Any | None = None
    # Connector-backed adapters use this binding to avoid ambiguous credential
    # selection when several instances of the same provider are configured.
    connector_binding: dict[str, Any] | None = None

    def safe_metadata(self) -> dict[str, str | None]:
        return {
            "execution_id": self.execution_id,
            "workflow_id": self.workflow_id,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "tool_call_id": self.tool_call_id,
        }


class BaseTypedToolExecutor(Protocol):
    tool_type: ToolType

    def execute(self, tool: ToolDefinition, arguments: dict[str, Any], context: ToolExecutionContext) -> dict[
        str, Any]: ...
