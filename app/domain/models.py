from __future__ import annotations

from enum import Enum
from pydantic import AliasChoices, Field
from typing import Any, Dict
from uuid import uuid4

from .agents import FrameworkHints, MemorySettings
from .credentials import CredentialReference, DomainModel, ProviderEndpointDefinition, SecretReference
from .schedules import ScheduleType
from .tools import MCPExposureSettings, SecuritySettings, ToolImplementationReference, ToolType
from .workflows import EdgeType, RuntimeAdapterType, VersionDefinition


class ModelProviderType(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    AWS_BEDROCK = "aws_bedrock"
    OLLAMA = "ollama"
    LM_STUDIO = "lm_studio"
    VLLM = "vllm"
    OPENAI_COMPATIBLE = "openai_compatible"
    AZURE_OPENAI = "azure_openai"
    OPENAI_CODEX = "openai_codex"
    OTHER = "other"


class ModelProviderDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    provider_type: ModelProviderType
    description: str | None = None
    endpoint: ProviderEndpointDefinition | None = None
    capabilities: list[str] = Field(default_factory=list)
    default_headers: Dict[str, str] = Field(default_factory=dict)
    secret_references: list[SecretReference] = Field(default_factory=list)
    config: Dict[str, Any] = Field(default_factory=dict)
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)


class ModelProfileDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    provider: str = Field(validation_alias=AliasChoices("provider", "provider_id"))
    model: str = Field(validation_alias=AliasChoices("model", "model_name"))
    description: str | None = None
    base_url: str | None = None
    api_key_ref: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
    top_p: float | None = None
    supports_tools: bool = True
    supports_structured_output: bool = False
    supports_vision: bool = False
    supports_streaming: bool = True
    parameters: Dict[str, Any] = Field(default_factory=dict)
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)

    @property
    def provider_id(self) -> str:
        return self.provider

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def supports_structured_outputs(self) -> bool:
        return self.supports_structured_output


__all__ = [
    "CredentialReference",
    "DomainModel",
    "EdgeType",
    "FrameworkHints",
    "MCPExposureSettings",
    "MemorySettings",
    "ModelProfileDefinition",
    "ModelProviderDefinition",
    "ModelProviderType",
    "ProviderEndpointDefinition",
    "RuntimeAdapterType",
    "ScheduleType",
    "SecretReference",
    "SecuritySettings",
    "ToolImplementationReference",
    "ToolType",
    "VersionDefinition",
]
