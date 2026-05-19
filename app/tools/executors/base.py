from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain import ToolDefinition, ToolType
from app.protocols.mcp import MCPClientRegistry
from app.runtime.native.approvals import ApprovalManager


@dataclass(slots=True)
class ToolExecutionContext:
    execution_id: str
    workflow_id: str | None = None
    runtime_registry: Any | None = None
    approval_manager: ApprovalManager | None = None
    mcp_registry: MCPClientRegistry | None = None
    execution_store: Any | None = None


class BaseTypedToolExecutor(Protocol):
    tool_type: ToolType

    def execute(self, tool: ToolDefinition, arguments: dict[str, Any], context: ToolExecutionContext) -> dict[
        str, Any]: ...
