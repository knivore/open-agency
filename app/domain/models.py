from __future__ import annotations

from enum import Enum
from pydantic import AliasChoices, Field
from typing import Any, Dict, Literal
from uuid import uuid4

from .agents import FrameworkHints, GraphContextSettings, MemorySettings
from .credentials import (
    ConnectorBindingDefinition,
    CredentialReference,
    DomainModel,
    ProviderEndpointDefinition,
    SecretReference,
)
from .schedules import ScheduleType
from .tools import MCPExposureSettings, SecuritySettings, ToolImplementationReference, ToolType
from .workflows import EdgeType, RuntimeAdapterType, VersionDefinition


class ModelProviderType(str, Enum):
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    AWS_BEDROCK = "aws_bedrock"
    OLLAMA = "ollama"
    LM_STUDIO = "lm_studio"
    VLLM = "vllm"
    OPENAI_COMPATIBLE = "openai_compatible"
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
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


class ModelFallbackTarget(DomainModel):
    provider: str | None = None
    model: str
    name: str | None = None
    description: str | None = None
    base_url: str | None = None
    api_key_ref: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    context_window: int | None = None
    top_p: float | None = None
    supports_tools: bool | None = None
    supports_structured_output: bool | None = None
    supports_vision: bool | None = None
    supports_streaming: bool | None = None
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ModelFallbackPolicy(DomainModel):
    retry_on: list[Literal["rate_limit", "timeout", "service_unavailable", "network", "auth"]] = Field(
        default_factory=lambda: ["rate_limit", "timeout", "service_unavailable", "network", "auth"]
    )
    same_provider_only: bool = False
    require_capability_match: bool = True


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
    fallback_strategy: Literal["auto", "manual", "disabled"] = "auto"
    fallback_models: list[ModelFallbackTarget] = Field(default_factory=list, max_length=5)
    fallback_policy: ModelFallbackPolicy = Field(default_factory=ModelFallbackPolicy)
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
    "ConnectorBindingDefinition",
    "DomainModel",
    "EdgeType",
    "FrameworkHints",
    "GraphContextSettings",
    "MCPExposureSettings",
    "MemorySettings",
    "ModelFallbackPolicy",
    "ModelFallbackTarget",
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
