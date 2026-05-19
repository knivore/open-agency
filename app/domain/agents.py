from __future__ import annotations

from pydantic import Field, model_validator
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from .credentials import DomainModel


class GuardrailDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: Optional[str] = None
    mode: Literal["input", "output", "tool", "policy", "other"] = "policy"
    config: Dict[str, Any] = Field(default_factory=dict)


class MemorySettings(DomainModel):
    enabled: bool = False
    strategy: Optional[str] = None
    scope: Optional[Literal["execution", "agent", "workflow", "user"]] = None
    backend_ref: Optional[str] = None
    max_entries: Optional[int] = None
    ttl_seconds: Optional[int] = None
    config: Dict[str, Any] = Field(default_factory=dict)


class FrameworkHints(DomainModel):
    preferred_adapter: Optional[str] = None
    adapter_config: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class AgentDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    system_prompt: Optional[str] = None
    role: Optional[str] = None
    backstory: Optional[str] = None
    model_profile_id: Optional[str] = None
    tool_ids: List[str] = Field(default_factory=list)
    handoff_agent_ids: List[str] = Field(default_factory=list)
    guardrails: List[GuardrailDefinition] = Field(default_factory=list)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    framework_hints: FrameworkHints = Field(default_factory=FrameworkHints)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_prompt_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        objective = payload.pop("objective", None)
        goal = payload.pop("goal", None)
        prompt = payload.get("instructions") or payload.get("system_prompt") or objective or goal
        if payload.get("instructions") is None and prompt is not None:
            payload["instructions"] = prompt
        if payload.get("system_prompt") is None and prompt is not None:
            payload["system_prompt"] = prompt
        return payload

    @model_validator(mode="after")
    def normalize_agent_prompts(self) -> "AgentDefinition":
        if self.instructions is None and self.system_prompt is not None:
            self.instructions = self.system_prompt
        if self.system_prompt is None and self.instructions is not None:
            self.system_prompt = self.instructions
        if self.display_name is None:
            self.display_name = self.name
        return self
