from __future__ import annotations

from pydantic import BaseModel, Field
from typing import Any


class MCPToolDescriptor(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    annotations: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPResourceDescriptor(BaseModel):
    uri: str
    name: str
    description: str = ""
    mime_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPPromptDescriptor(BaseModel):
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MCPDiscoverySnapshot(BaseModel):
    server_id: str
    tools: list[MCPToolDescriptor] = Field(default_factory=list)
    resources: list[MCPResourceDescriptor] = Field(default_factory=list)
    prompts: list[MCPPromptDescriptor] = Field(default_factory=list)
