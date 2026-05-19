from __future__ import annotations

from typing import Any

from app.domain import MCPServerDefinition
from .schemas import MCPResourceDescriptor


def mcp_resource_to_dict(server: MCPServerDefinition, resource: MCPResourceDescriptor) -> dict[str, Any]:
    return {
        "id": f"mcp-resource:{server.id}:{resource.uri}",
        "server_id": server.id,
        "uri": resource.uri,
        "name": resource.name,
        "description": resource.description,
        "mime_type": resource.mime_type,
        "metadata": resource.metadata,
    }
