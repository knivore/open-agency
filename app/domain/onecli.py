from __future__ import annotations

from enum import Enum
from pydantic import Field
from typing import Any
from uuid import uuid4

from .credentials import DomainModel


class OneCLIIdentityMappingStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OneCLIRuleAction(str, Enum):
    BLOCK = "block"
    RATE_LIMIT = "rate_limit"
    MANUAL_APPROVAL = "manual_approval"


class OneCLIRuleScope(str, Enum):
    ALL_AGENTS = "all_agents"
    SPECIFIC_AGENT = "specific_agent"


class OneCLIRateLimitWindow(str, Enum):
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"


class OneCLIRuleTemplate(DomainModel):
    id: str
    name: str
    description: str
    host_pattern: str
    path_pattern: str | None = None
    method: str = "ANY"
    scope: OneCLIRuleScope = OneCLIRuleScope.ALL_AGENTS
    action: OneCLIRuleAction
    rate_limit_count: int | None = None
    rate_limit_window: OneCLIRateLimitWindow | None = None
    default_enabled: bool = True
    category: str
    notes: str | None = None


class OneCLIRuleProfile(DomainModel):
    id: str
    version: int
    name: str
    description: str
    rules: list[OneCLIRuleTemplate]


class OneCLIIdentityMapping(DomainModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    owner_user_id: str
    name: str
    onecli_agent_id: str
    agent_token_secret_ref: str
    status: OneCLIIdentityMappingStatus = OneCLIIdentityMappingStatus.ACTIVE
    workflow_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
