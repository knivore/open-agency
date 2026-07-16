"""Domain contracts for tool definitions, implementations, and security policy."""

from __future__ import annotations

import re
from enum import Enum
from pydantic import AliasChoices, Field, model_validator
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from .agents import FrameworkHints
from .credentials import ConnectorBindingDefinition, CredentialReference, DomainModel
from .intent_routing import ToolRoutingMetadata


class ToolType(str, Enum):
    PYTHON_FUNCTION = "python_function"
    HTTP_REQUEST = "http_request"
    SQL_QUERY = "sql_query"
    SHELL_COMMAND = "shell_command"
    MCP_TOOL = "mcp_tool"
    A2A_REMOTE_AGENT = "a2a_remote_agent"
    WORKFLOW_TOOL = "workflow_tool"
    HUMAN_APPROVAL = "human_approval"


class SecuritySettings(DomainModel):
    requires_approval: bool = Field(
        default=False,
        validation_alias=AliasChoices("requires_approval", "approval_required"),
        serialization_alias="requires_approval",
    )
    sandbox_required: bool = Field(
        default=False,
        validation_alias=AliasChoices("sandbox_required", "sandbox"),
        serialization_alias="sandbox_required",
    )
    allow_shell: bool = False
    allow_browser: bool = False
    allow_filesystem: bool = False
    allow_network: bool = False
    allowlisted_domains: List[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("allowlisted_domains", "allowed_domains"),
        serialization_alias="allowlisted_domains",
    )
    allowed_paths: List[str] = Field(default_factory=list)
    allowlisted_mcp_servers: List[str] = Field(default_factory=list)
    module_allowlist: List[str] = Field(default_factory=list)
    function_allowlist: List[str] = Field(default_factory=list)
    read_only_sql: bool = True
    read_only: bool = False
    dangerous: bool = False
    approval_on_rejection: Literal["fail", "skip"] = "fail"
    credential_references: List[CredentialReference] = Field(
        default_factory=list,
        validation_alias=AliasChoices("credential_references", "secret_references"),
        serialization_alias="credential_references",
    )
    connector_bindings: List[ConnectorBindingDefinition] = Field(default_factory=list)
    redaction_enabled: bool = False
    redaction_rules: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_sandbox_for_privileged_capabilities(self) -> "SecuritySettings":
        if self.has_privileged_capabilities and not self.sandbox_required:
            # Privileged adapters are executable boundaries. Normalize legacy and
            # user-authored definitions before they can reach the native executor.
            self.sandbox_required = True
        return self

    @property
    def has_privileged_capabilities(self) -> bool:
        return any((self.allow_shell, self.allow_browser, self.allow_filesystem, self.allow_network))

    @property
    def approval_required(self) -> bool:
        return self.requires_approval

    @property
    def sandbox(self) -> bool:
        return self.sandbox_required

    @property
    def allowed_domains(self) -> List[str]:
        return self.allowlisted_domains

    @property
    def secret_references(self) -> List[CredentialReference]:
        return self.credential_references


class MCPExposureSettings(DomainModel):
    expose_as_mcp_tool: bool = False
    expose_as_mcp_resource: bool = False
    expose_as_mcp_prompt: bool = False
    name_override: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class ToolImplementationReference(DomainModel):
    implementation_type: Literal[
        "python",
        "http",
        "mcp",
        "a2a",
        "shell",
        "other",
        "python_function",
        "http_request",
        "sql_query",
        "shell_command",
        "mcp_tool",
        "a2a_remote_agent",
        "workflow_tool",
        "human_approval",
    ] = "python"
    target: str = Field(
        validation_alias=AliasChoices("target", "module"),
        serialization_alias="target",
    )
    entrypoint: Optional[str] = None
    callable_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("callable_name", "function"),
        serialization_alias="callable_name",
    )
    config: Dict[str, Any] = Field(default_factory=dict)

    @property
    def module(self) -> str:
        return self.target

    @property
    def function(self) -> Optional[str]:
        return self.callable_name or self.entrypoint


class ToolDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    display_name: Optional[str] = None
    description: str
    tool_type: ToolType = ToolType.PYTHON_FUNCTION
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    implementation: ToolImplementationReference
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    mcp_exposure: MCPExposureSettings = Field(default_factory=MCPExposureSettings)
    tags: List[str] = Field(default_factory=list)
    routing: ToolRoutingMetadata | None = None
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)

    @model_validator(mode="after")
    def ensure_tool_is_safe_and_typed(self) -> "ToolDefinition":
        if not self.display_name:
            self.display_name = _default_tool_display_name(self.name)

        if not self.input_schema:
            raise ValueError("ToolDefinition.input_schema is required for all tools")

        implementation_map = {
            "python": ToolType.PYTHON_FUNCTION,
            "python_function": ToolType.PYTHON_FUNCTION,
            "http": ToolType.HTTP_REQUEST,
            "http_request": ToolType.HTTP_REQUEST,
            "shell": ToolType.SHELL_COMMAND,
            "shell_command": ToolType.SHELL_COMMAND,
            "mcp": ToolType.MCP_TOOL,
            "mcp_tool": ToolType.MCP_TOOL,
            "a2a": ToolType.A2A_REMOTE_AGENT,
            "a2a_remote_agent": ToolType.A2A_REMOTE_AGENT,
            "workflow_tool": ToolType.WORKFLOW_TOOL,
            "human_approval": ToolType.HUMAN_APPROVAL,
            "sql_query": ToolType.SQL_QUERY,
        }
        inferred_type = implementation_map.get(self.implementation.implementation_type)
        if inferred_type is not None and self.tool_type != inferred_type:
            self.tool_type = inferred_type

        if self.tool_type == ToolType.SHELL_COMMAND:
            if not self.security.allow_shell:
                raise ValueError("Shell command tools are disabled by default and require allow_shell=True")
            if not self.security.requires_approval:
                raise ValueError("Shell command tools require requires_approval=True")
            if not self.security.sandbox_required:
                raise ValueError("Shell command tools require sandbox_required=True")

        return self


_TOOL_DISPLAY_ACRONYMS = {
    "a2a",
    "api",
    "cli",
    "csv",
    "docx",
    "html",
    "http",
    "json",
    "llm",
    "mcp",
    "pdf",
    "sql",
    "txt",
    "ui",
    "url",
    "xml",
    "yaml",
}
_TOOL_DISPLAY_LOWERCASE_WORDS = {"a", "an", "and", "as", "by", "for", "from", "in", "of", "on", "or", "the", "to",
                                 "with"}


def _default_tool_display_name(value: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value.strip())
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", spaced)
    parts = [part for part in re.split(r"[^A-Za-z0-9]+", spaced) if part]
    if not parts:
        return "Tool"
    formatted: list[str] = []
    for index, part in enumerate(parts):
        lower = part.lower()
        if lower in _TOOL_DISPLAY_ACRONYMS:
            formatted.append(part.upper())
        elif index > 0 and lower in _TOOL_DISPLAY_LOWERCASE_WORDS:
            formatted.append(lower)
        else:
            formatted.append(f"{part[:1].upper()}{part[1:]}")
    return " ".join(formatted)
