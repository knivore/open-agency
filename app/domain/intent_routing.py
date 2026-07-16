"""Typed contracts for main-agent intent routing and selective tool exposure."""

from __future__ import annotations

from enum import Enum
from typing import Self

from pydantic import Field, model_validator

from .credentials import DomainModel


class ExecutionMode(str, Enum):
    DIRECT_RESPONSE = "direct_response"
    SELECTED_TOOLS = "selected_tools"
    SPECIALIST_AGENT = "specialist_agent"
    CLARIFICATION = "clarification"
    FULL_AGENT = "full_agent"


class RequestComplexity(str, Enum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    COMPLEX = "complex"


class ContextScope(str, Enum):
    CURRENT_MESSAGE = "current_message"
    RECENT_TURNS = "recent_turns"
    CONVERSATION_SUMMARY = "conversation_summary"
    RELEVANT_RETRIEVAL = "relevant_retrieval"
    FULL_THREAD = "full_thread"


class ToolRoutingMetadata(DomainModel):
    """Compact routing facts kept separate from a tool's provider JSON schema."""

    group: str = Field(min_length=1, max_length=96)
    additional_groups: list[str] = Field(default_factory=list)
    short_description: str = Field(min_length=1, max_length=240)
    intents: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    read_only: bool
    risk_level: str = Field(min_length=1, max_length=32)
    requires_confirmation: bool = False
    enabled: bool = True


class ToolGroupDescriptor(DomainModel):
    """The compact, non-schema capability description passed to the router."""

    id: str = Field(min_length=1, max_length=96)
    description: str = Field(min_length=1, max_length=240)
    risk: str = Field(min_length=1, max_length=32)


class SpecialistAgentDescriptor(DomainModel):
    """Compact specialist metadata exposed to the router without full agent prompts."""

    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="Specialist agent", min_length=1, max_length=300)
    tool_groups: list[str] = Field(default_factory=list)


class RoutingDecision(DomainModel):
    """Validated routing output; it contains auditable categories, never private reasoning."""

    intent: str = Field(min_length=1, max_length=96)
    complexity: RequestComplexity
    execution_mode: ExecutionMode
    tool_groups: list[str] = Field(default_factory=list)
    specialist_agent: str | None = Field(default=None, max_length=128)
    context_scope: ContextScope = ContextScope.CURRENT_MESSAGE
    needs_memory: bool = False
    needs_user_context: bool = False
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9_./-]+$")
    max_tool_iterations: int | None = Field(default=None, ge=1, le=20)
    token_budget: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_execution_mode(self) -> Self:
        if self.execution_mode == ExecutionMode.DIRECT_RESPONSE and self.tool_groups:
            raise ValueError("direct_response decisions cannot select tool groups")
        if self.execution_mode == ExecutionMode.CLARIFICATION:
            if not self.needs_clarification or not self.clarification_question:
                raise ValueError("clarification decisions require a clarification question")
            if self.tool_groups:
                raise ValueError("clarification decisions cannot select tool groups")
        elif self.needs_clarification:
            raise ValueError("needs_clarification requires clarification execution mode")
        if self.execution_mode == ExecutionMode.SPECIALIST_AGENT and not self.specialist_agent:
            raise ValueError("specialist_agent decisions require a specialist agent")
        if self.execution_mode != ExecutionMode.SPECIALIST_AGENT and self.specialist_agent:
            raise ValueError("specialist agent is only valid for specialist_agent mode")
        if self.execution_mode == ExecutionMode.SELECTED_TOOLS and not self.tool_groups:
            raise ValueError("selected_tools decisions require at least one tool group")
        return self


class FastPathResult(DomainModel):
    """A conservative deterministic classification before any router model call."""

    matched: bool = False
    rule_code: str | None = Field(default=None, max_length=96)
    decision: RoutingDecision | None = None

    @model_validator(mode="after")
    def validate_match(self) -> Self:
        if self.matched and (not self.rule_code or self.decision is None):
            raise ValueError("matched fast paths require a rule code and routing decision")
        if not self.matched and (self.rule_code is not None or self.decision is not None):
            raise ValueError("unmatched fast paths cannot include a decision")
        return self
