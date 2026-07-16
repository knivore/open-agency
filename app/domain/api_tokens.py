"""Domain contracts for API tokens and supported token scopes."""

from __future__ import annotations

from datetime import datetime
from pydantic import Field
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


class ApiTokenScopeDefinition(DomainModel):
    id: str
    label: str
    description: str
    category: str


API_TOKEN_SCOPE_DEFINITIONS: tuple[ApiTokenScopeDefinition, ...] = (
    ApiTokenScopeDefinition(
        id="agents:read",
        label="Agents read",
        description="Read agent definitions and related agent metadata.",
        category="agents",
    ),
    ApiTokenScopeDefinition(
        id="agents:write",
        label="Agents write",
        description="Create, update, or delete agent definitions.",
        category="agents",
    ),
    ApiTokenScopeDefinition(
        id="conversations:read",
        label="Conversations read",
        description="Read conversations, messages, context usage, and the active main-agent profile.",
        category="conversations",
    ),
    ApiTokenScopeDefinition(
        id="conversations:write",
        label="Conversations write",
        description="Create or update conversations, messages, approvals, and main-agent settings.",
        category="conversations",
    ),
    ApiTokenScopeDefinition(
        id="workflows:read",
        label="Workflows read",
        description="Read workflow definitions and workflow metadata.",
        category="workflows",
    ),
    ApiTokenScopeDefinition(
        id="workflows:write",
        label="Workflows write",
        description="Create, update, publish, clone, or delete workflows.",
        category="workflows",
    ),
    ApiTokenScopeDefinition(
        id="workflows:run",
        label="Workflows run",
        description="Start workflow executions.",
        category="workflows",
    ),
    ApiTokenScopeDefinition(
        id="executions:read",
        label="Executions read",
        description="Read execution state, events, artifacts, and run history.",
        category="executions",
    ),
    ApiTokenScopeDefinition(
        id="executions:write",
        label="Executions write",
        description="Create executions and control execution lifecycle actions such as start, pause, resume, cancel, and approvals.",
        category="executions",
    ),
    ApiTokenScopeDefinition(
        id="goals:read",
        label="Goals read",
        description="Read durable goal records, goal links, success criteria, evidence, and supervision metadata.",
        category="goals",
    ),
    ApiTokenScopeDefinition(
        id="goals:write",
        label="Goals write",
        description="Create, update, pause, resume, cancel, and complete durable goal records.",
        category="goals",
    ),
    ApiTokenScopeDefinition(
        id="integrations:read",
        label="Integrations read",
        description="Read integration catalog, connector configuration, and integration health metadata.",
        category="integrations",
    ),
    ApiTokenScopeDefinition(
        id="integrations:write",
        label="Integrations write",
        description="Create or update integration, connector, and credential-backed configuration.",
        category="integrations",
    ),
    ApiTokenScopeDefinition(
        id="models:read",
        label="Models read",
        description="Read model provider and model profile definitions.",
        category="models",
    ),
    ApiTokenScopeDefinition(
        id="models:write",
        label="Models write",
        description="Create or update model providers and model profiles.",
        category="models",
    ),
    ApiTokenScopeDefinition(
        id="tools:read",
        label="Tools read",
        description="Read backend tool definitions and tool catalog metadata.",
        category="tools",
    ),
    ApiTokenScopeDefinition(
        id="tools:write",
        label="Tools write",
        description="Create, update, validate, or test backend tool definitions.",
        category="tools",
    ),
    ApiTokenScopeDefinition(
        id="memory:read",
        label="Memory read",
        description="Read durable user, workspace, conversation, workflow, and global memory records.",
        category="memory",
    ),
    ApiTokenScopeDefinition(
        id="memory:write",
        label="Memory write",
        description="Create, update, or delete durable memory records.",
        category="memory",
    ),
    ApiTokenScopeDefinition(
        id="personas:read",
        label="Personas read",
        description="Read persona definitions, package versions, sources, and Persona Factory review state.",
        category="personas",
    ),
    ApiTokenScopeDefinition(
        id="personas:write",
        label="Personas write",
        description="Create, update, approve, publish, or archive personas and persona packages.",
        category="personas",
    ),
    ApiTokenScopeDefinition(
        id="schedules:read",
        label="Schedules read",
        description="Read schedule definitions and scheduler metadata.",
        category="schedules",
    ),
    ApiTokenScopeDefinition(
        id="schedules:write",
        label="Schedules write",
        description="Create, update, enable, disable, or trigger schedules.",
        category="schedules",
    ),
)


class ApiTokenDefinition(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: str
    name: str
    token_hash: str
    prefix: str
    last4: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApiTokenPublicDefinition(DomainModel):
    id: str
    owner_user_id: str
    name: str
    prefix: str
    last4: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
