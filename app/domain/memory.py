from __future__ import annotations

from datetime import date
from datetime import datetime, timezone
from enum import Enum
from pydantic import Field, model_validator
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MemoryScope(str, Enum):
    USER = "user"
    WORKSPACE = "workspace"
    CONVERSATION = "conversation"
    WORKFLOW = "workflow"
    GLOBAL = "global"


class MemoryKind(str, Enum):
    FACT = "fact"
    PREFERENCE = "preference"
    DAILY_SUMMARY = "daily_summary"
    DECISION = "decision"
    TASK_COMMITMENT = "task_commitment"
    ARCHIVE = "archive"
    RUN_SUMMARY = "run_summary"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class MemoryRecord(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: MemoryScope
    content: str
    summary: str | None = None
    tags: list[str] = Field(default_factory=list)
    sensitive: bool = False
    created_by_user_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    workflow_id: str | None = None
    agent_id: str | None = None
    source: str | None = None
    memory_kind: MemoryKind | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    importance: int = 50
    summary_date: date | None = None
    archived_window_start: datetime | None = None
    archived_window_end: datetime | None = None
    source_conversation_id: str | None = None
    source_execution_id: str | None = None
    supersedes_memory_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None
    embedding_model_profile_id: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    embedded_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def validate_scope_binding(self) -> "MemoryRecord":
        if not self.content.strip():
            raise ValueError("Memory content cannot be empty.")
        if not 0 <= self.importance <= 100:
            raise ValueError("Memory importance must be between 0 and 100.")
        if self.scope == MemoryScope.USER and not self.created_by_user_id:
            raise ValueError("User-scoped memory requires created_by_user_id.")
        if self.scope == MemoryScope.WORKSPACE and not self.workspace_id:
            raise ValueError("Workspace-scoped memory requires workspace_id.")
        if self.scope == MemoryScope.CONVERSATION and not self.conversation_id:
            raise ValueError("Conversation-scoped memory requires conversation_id.")
        if self.scope == MemoryScope.WORKFLOW and not self.workflow_id:
            raise ValueError("Workflow-scoped memory requires workflow_id.")
        if self.memory_kind == MemoryKind.DAILY_SUMMARY:
            if self.summary_date is None:
                raise ValueError("Daily-summary memory requires summary_date.")
            if not self.source_conversation_id:
                raise ValueError("Daily-summary memory requires source_conversation_id.")
            if self.archived_window_start is None or self.archived_window_end is None:
                raise ValueError("Daily-summary memory requires archived window timestamps.")
        if (
                self.archived_window_start is not None
                and self.archived_window_end is not None
                and self.archived_window_end < self.archived_window_start
        ):
            raise ValueError("Memory archived_window_end must be greater than or equal to archived_window_start.")
        return self
