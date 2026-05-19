from __future__ import annotations

from enum import Enum
from pydantic import Field
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
