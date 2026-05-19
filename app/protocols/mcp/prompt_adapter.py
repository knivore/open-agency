from __future__ import annotations

from typing import Any

from app.domain import MCPServerDefinition
from .schemas import MCPPromptDescriptor


def mcp_prompt_to_dict(server: MCPServerDefinition, prompt: MCPPromptDescriptor) -> dict[str, Any]:
    return {
        "id": f"mcp-prompt:{server.id}:{prompt.name}",
        "server_id": server.id,
        "name": prompt.name,
        "description": prompt.description,
        "arguments": prompt.arguments,
        "metadata": prompt.metadata,
    }
