from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from app.domain import DomainModel


class ToolContract(DomainModel):
    context: str | None = Field(default=None, alias="@context")
    type: str | None = Field(default=None, alias="@type")
    name: str
    version: str
    description: str | None = None
    inputs: dict[str, Any]
    outputs: dict[str, Any]


class ToolRunRequest(DomainModel):
    tool: str
    input: dict[str, Any]
    actor: str | None = None
    dryRun: bool = True


class PolicyRuleResult(DomainModel):
    id: str
    outcome: Literal["ok", "warn", "deny"]
    reason: str | None = None


class PolicyVerdict(DomainModel):
    score: int = 0
    rules: list[PolicyRuleResult] = Field(default_factory=list)

    @property
    def outcome(self) -> Literal["ok", "warn", "deny"]:
        if any(rule.outcome == "deny" for rule in self.rules):
            return "deny"
        if any(rule.outcome == "warn" for rule in self.rules):
            return "warn"
        return "ok"


class FileChanged(DomainModel):
    path: str
    op: Literal["create", "modify", "delete", "rename"] = "modify"
    hunks: list[dict[str, Any]] = Field(default_factory=list)


class ToolRunResponse(DomainModel):
    verdict: Literal["ok", "warn", "deny"]
    policyVerdict: PolicyVerdict | None = None
    result: dict[str, Any] | None = None
    patch: str | None = None
    filesChanged: list[FileChanged] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    dryRun: bool = True
    timestamp: str
    actor: str | None = None
    signature: str | None = None
