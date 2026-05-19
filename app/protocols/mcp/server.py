from __future__ import annotations

from typing import Any

from app.domain import ToolDefinition


class InternalMCPServer:
    def __init__(self, tools: list[ToolDefinition], tool_executor):
        self.tools = [tool for tool in tools if tool.mcp_exposure.expose_as_mcp_tool]
        self.tool_executor = tool_executor

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.mcp_exposure.name_override or tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any], *, execution_id: str = "mcp-server") -> dict[
        str, Any]:
        for tool in self.tools:
            exposed_name = tool.mcp_exposure.name_override or tool.name
            if exposed_name == name:
                return await self.tool_executor.execute(
                    tool,
                    arguments,
                    execution_id=execution_id,
                    workflow_id="mcp-server",
                )
        raise KeyError(f"Internal MCP tool '{name}' was not found")
