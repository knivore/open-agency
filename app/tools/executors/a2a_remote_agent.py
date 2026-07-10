"""Executor for tools that call a remote Agent2Agent endpoint."""

from __future__ import annotations

from app.domain import ToolDefinition, ToolType
from app.protocols.a2a.adapter import A2AAdapter
from app.runtime.native.errors import ToolExecutionError
from .base import ToolExecutionContext


class A2ARemoteAgentToolExecutor:
    """Dispatch an Agency tool invocation through the A2A adapter layer."""

    tool_type = ToolType.A2A_REMOTE_AGENT
    async_execution = True

    async def aexecute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        if context.execution_store is None:
            raise ToolExecutionError(f"A2A tool '{tool.id}' requires an execution store")
        adapter = A2AAdapter(execution_store=context.execution_store)
        return await adapter.call_remote_agent(tool, arguments)
