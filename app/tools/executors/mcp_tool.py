from __future__ import annotations

from app.domain import ToolDefinition, ToolType
from app.protocols.mcp import MCPRegistryError
from app.protocols.mcp.computer_use_adapter import adapt_computer_use_arguments, normalize_computer_use_response
from app.runtime.native.errors import ToolExecutionError
from .base import ToolExecutionContext


class McpToolExecutor:
    tool_type = ToolType.MCP_TOOL
    async_execution = True

    async def aexecute(self, tool: ToolDefinition, arguments: dict[str, object], context: ToolExecutionContext) -> dict[
        str, object]:
        if context.mcp_registry is None:
            raise ToolExecutionError(f"MCP tool '{tool.id}' cannot execute without an MCP registry")
        server_id = tool.implementation.target
        tool_name = tool.implementation.config.get("mcp_tool_name") or tool.implementation.callable_name or tool.name
        adapted_arguments = (
            adapt_computer_use_arguments(tool, arguments)
            if tool.implementation.config.get("tool_family") == "computer_use"
            else arguments
        )
        if tool.security.allowlisted_mcp_servers and server_id not in tool.security.allowlisted_mcp_servers:
            raise ToolExecutionError(f"MCP server '{server_id}' is not allowlisted for tool '{tool.id}'")
        try:
            raw_result = context.mcp_registry.call_tool(server_id, tool_name, adapted_arguments)
            if tool.implementation.config.get("tool_family") == "computer_use":
                return normalize_computer_use_response(tool, arguments, adapted_arguments, raw_result)
            return raw_result
        except MCPRegistryError as exc:
            raise ToolExecutionError(str(exc)) from exc
