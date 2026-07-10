"""Domain contracts for protocol integrations such as MCP servers."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from pydantic import Field, model_validator
from typing import Any, Dict, List, Optional
from uuid import uuid4

from .credentials import CredentialReference, DomainModel


class MCPTransportType(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"


class MCPServerDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    transport: MCPTransportType = MCPTransportType.STDIO
    command: str
    args: List[str] = Field(default_factory=list)
    url: Optional[str] = None
    env_refs: List[CredentialReference] = Field(default_factory=list)
    enabled: bool = False
    allowlisted_command: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def default_allowlisted_command_from_command(self) -> "MCPServerDefinition":
        if self.command and not self.allowlisted_command:
            # A persisted MCP server record represents user/admin intent to trust
            # this command without requiring a hidden backend env allowlist.
            self.allowlisted_command = Path(self.command).name
        return self
