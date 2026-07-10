"""Domain contracts for conversations, messages, channels, and approvals."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pydantic import Field
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationStatus(str, Enum):
    OPEN = "open"
    ARCHIVED = "archived"
    BLOCKED = "blocked"


class ConversationChannelType(str, Enum):
    WEB = "web"
    API = "api"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WHATSAPP = "whatsapp"
    MICROSOFT_TEAMS = "microsoft-teams"
    SLACK = "slack"
    OTHER = "other"


class ConversationRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ConversationMessageType(str, Enum):
    USER_TEXT = "user_text"
    ASSISTANT_TEXT = "assistant_text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_PROGRESS = "execution_progress"
    EXECUTION_COMPLETED = "execution_completed"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESULT = "approval_result"
    WORKFLOW_PROPOSAL = "workflow_proposal"
    WORKFLOW_UPDATE_PROPOSAL = "workflow_update_proposal"
    SYSTEM_NOTE = "system_note"


class ApprovalType(str, Enum):
    WORKFLOW_EXECUTION = "workflow_execution"
    WORKFLOW_CREATE = "workflow_create"
    WORKFLOW_UPDATE = "workflow_update"
    TOOL_CREATE = "tool_create"
    TOOL_UPDATE = "tool_update"
    TOOL_EXECUTE = "tool_execute"
    OTHER = "other"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalTargetType(str, Enum):
    WORKFLOW = "workflow"
    TOOL = "tool"
    AGENT = "agent"
    OTHER = "other"


class Conversation(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str | None = None
    status: ConversationStatus = ConversationStatus.OPEN
    created_by_user_id: str | None = None
    main_agent_profile_id: str | None = None
    channel_type: ConversationChannelType = ConversationChannelType.API
    channel_thread_id: str | None = None
    channel_user_id: str | None = None
    channel_display_name: str | None = None
    workspace_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChannelIdentityMapping(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    channel_type: ConversationChannelType
    channel_user_id: str
    internal_user_id: str
    channel_display_name: str | None = None
    trusted: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ConversationMessage(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    conversation_id: str
    role: ConversationRole
    message_type: ConversationMessageType
    content: dict[str, Any] = Field(default_factory=dict)
    plain_text: str | None = None
    external_message_id: str | None = None
    execution_id: str | None = None
    approval_request_id: str | None = None
    tool_call_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class MainAgentProfile(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    description: str | None = None
    agent_id: str
    default_workflow_id: str
    default_model_profile_id: str | None = None
    enabled: bool = True
    policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ApprovalRequest(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    approval_type: ApprovalType
    status: ApprovalStatus = ApprovalStatus.PENDING
    target_type: ApprovalTargetType
    target_id: str | None = None
    requested_by_agent_id: str
    requested_by_profile_id: str | None = None
    conversation_id: str
    origin_message_id: str
    summary: str
    diff_summary: str | None = None
    proposed_payload: dict[str, Any] | None = None
    decision_reason: str | None = None
    approved_by_user_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
