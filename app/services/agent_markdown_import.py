"""Import Markdown-authored agent definitions into Agency agents."""

from __future__ import annotations

import hashlib
import httpx
import ipaddress
import re
import socket
import yaml
from datetime import datetime, timezone
from pydantic import Field, ValidationError, field_validator, model_validator
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from app.api.context import ApiContext
from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    DomainModel,
    Execution,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    FrameworkHints,
    NodeType,
    ToolDefinition,
    ToolType,
    UserDefinition,
    VersionDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.observability.event_bus import get_default_event_bus

MAX_MARKDOWN_BYTES = 512 * 1024
MAX_INSTRUCTION_CHARACTERS = 120_000
REMOTE_FETCH_TIMEOUT_SECONDS = 8.0
AGENT_IMPORT_AUDIT_WORKFLOW_ID = "agent-markdown-import"
AGENT_IMPORT_AUDIT_RUNTIME_ADAPTER_ID = "agent-import"
FRONTMATTER_KEYS = (
    "id",
    "name",
    "display_name",
    "description",
    "role",
    "backstory",
    "model_profile_id",
    "tool_ids",
    "handoff_agent_ids",
    "agent_kind",
    "color",
    "emoji",
    "vibe",
    "tags",
    "tools",
    "applyTo",
    "apply_to",
    "provider",
    "platform",
    "source_agent_format",
    "agent_format",
)
HIGH_RISK_TOOL_TYPES = {
    ToolType.SHELL_COMMAND,
    ToolType.HTTP_REQUEST,
    ToolType.MCP_TOOL,
    ToolType.A2A_REMOTE_AGENT,
    ToolType.WORKFLOW_TOOL,
}
PROMPT_INJECTION_PATTERNS = (
    re.compile(
        r"\bignore\s+(?:all\s+)?(?:previous|prior|above|system|developer)\s+instructions\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:bypass|disable|override)\s+(?:approval|approvals|policy|policies|safety|permissions?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:reveal|print|dump|exfiltrate)\s+(?:secrets?|credentials?|api\s*keys?|tokens?)\b",
        re.IGNORECASE,
    ),
)
TOOL_GRANT_PATTERNS = (
    re.compile(r"\bauto[-\s]?grant\s+(?:all\s+)?(?:tools?|permissions?|capabilities)\b", re.IGNORECASE),
    re.compile(
        r"\bgrant\s+(?:me|this agent|the agent)\s+(?:all\s+)?(?:tools?|permissions?|capabilities)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:do not|don't)\s+(?:ask for|require)\s+(?:tool\s+)?approval\b", re.IGNORECASE),
)
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(
        r"\b(?:api[_-]?key|secret|token|password|credential)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{16,}",
        re.IGNORECASE,
    ),
)
SHELL_SNIPPET_PATTERNS = (
    re.compile(r"```(?:bash|sh|zsh|shell|powershell|pwsh|cmd)\b", re.IGNORECASE),
    re.compile(
        r"^\s*(?:sudo\s+)?(?:rm\s+-rf|curl\b.*\|\s*(?:bash|sh)|wget\b.*\|\s*(?:bash|sh)|chmod\s+\+x)\b",
        re.IGNORECASE | re.MULTILINE,
    ),
)


class AgentImportError(ValueError):
    """Raised when an import source cannot be parsed or committed."""

    def __init__(self, message: str, *, code: str = "agent_import_error") -> None:
        super().__init__(message)
        self.code = code


class AgentImportSource(DomainModel):
    source_type: Literal["upload", "text", "url"] = "text"
    filename: str | None = None
    url: str | None = None
    sha256: str


class AgentImportWarning(DomainModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    field: str | None = None


class AgentImportConflict(DomainModel):
    conflict_type: Literal["id", "name"]
    existing_agent_id: str
    existing_agent_name: str
    message: str


class AgentImportToolSuggestion(DomainModel):
    tool_id: str
    exists: bool
    requires_review: bool = True
    high_risk: bool = False
    reason: str


class AgentImportHandoffSuggestion(DomainModel):
    agent_id: str
    exists: bool
    matched_agent_id: str | None = None
    requires_review: bool = True
    reason: str


class AgentImportLLMSuggestedTool(DomainModel):
    tool_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class AgentImportLLMSuggestedHandoff(DomainModel):
    agent_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str


class AgentImportLLMNormalizedOutput(DomainModel):
    """Strict contract for future LLM normalization output."""

    agent: AgentDefinition
    suggested_tool_mappings: list[AgentImportLLMSuggestedTool] = Field(default_factory=list)
    suggested_handoff_mappings: list[AgentImportLLMSuggestedHandoff] = Field(default_factory=list)
    warnings: list[AgentImportWarning] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class AgentImportProposal(DomainModel):
    source: AgentImportSource
    detected_format: str
    agent: AgentDefinition
    suggested_tool_ids: list[AgentImportToolSuggestion] = Field(default_factory=list)
    suggested_handoff_agent_ids: list[AgentImportHandoffSuggestion] = Field(default_factory=list)
    warnings: list[AgentImportWarning] = Field(default_factory=list)
    conflicts: list[AgentImportConflict] = Field(default_factory=list)
    requires_review: bool = True


class AgentImportPreviewRequest(DomainModel):
    markdown_text: str | None = None
    source_url: str | None = None
    source_filename: str | None = None
    use_llm_normalization: bool = False
    llm_normalization_model_profile_id: str | None = None

    @model_validator(mode="after")
    def require_one_source(self) -> "AgentImportPreviewRequest":
        if bool(self.markdown_text) == bool(self.source_url):
            raise ValueError("Provide exactly one of markdown_text or source_url.")
        return self


class AgentImportCommitRequest(DomainModel):
    proposal: AgentImportProposal | None = None
    markdown_text: str | None = None
    source_url: str | None = None
    source_filename: str | None = None
    conflict_strategy: Literal["create_only", "update_existing", "duplicate_as_new"] = "create_only"
    approved_tool_ids: list[str] = Field(default_factory=list)
    approved_handoff_agent_ids: list[str] = Field(default_factory=list)
    model_profile_id: str | None = None
    enabled: bool | None = None
    use_llm_normalization: bool = False
    llm_normalization_model_profile_id: str | None = None

    @field_validator("approved_tool_ids", "approved_handoff_agent_ids")
    @classmethod
    def unique_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def require_proposal_or_source(self) -> "AgentImportCommitRequest":
        source_count = sum(bool(item) for item in (self.proposal, self.markdown_text, self.source_url))
        if source_count != 1:
            raise ValueError("Provide exactly one of proposal, markdown_text, or source_url.")
        return self


class AgentImportCommitResult(DomainModel):
    status: Literal["created", "updated"]
    agent: AgentDefinition
    warnings: list[AgentImportWarning] = Field(default_factory=list)


class AgentImportBatchError(DomainModel):
    index: int
    source_filename: str | None = None
    source_url: str | None = None
    code: str
    message: str


class AgentImportBatchPreviewRequest(DomainModel):
    items: list[AgentImportPreviewRequest]


class AgentImportBatchPreviewResult(DomainModel):
    proposals: list[AgentImportProposal] = Field(default_factory=list)
    errors: list[AgentImportBatchError] = Field(default_factory=list)


class AgentImportBatchCommitRequest(DomainModel):
    items: list[AgentImportCommitRequest]


class AgentImportBatchCommitResult(DomainModel):
    results: list[AgentImportCommitResult] = Field(default_factory=list)
    errors: list[AgentImportBatchError] = Field(default_factory=list)


class ParsedAgentMarkdown(DomainModel):
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    body: str
    detected_format: str
    source: AgentImportSource


class AgentMarkdownImportService:
    """Parse, preview, and commit Markdown agent definitions."""

    def __init__(self, context: ApiContext):
        self.context = context

    async def preview_from_request(
            self,
            payload: AgentImportPreviewRequest,
            *,
            current_user: UserDefinition | None = None,
    ) -> AgentImportProposal:
        if payload.use_llm_normalization:
            await self._reject_llm_normalization_request(
                payload.llm_normalization_model_profile_id,
                current_user=current_user,
            )
        markdown_text, source = await self._resolve_source(
            markdown_text=payload.markdown_text,
            source_url=payload.source_url,
            source_filename=payload.source_filename,
        )
        return await self.preview_markdown(markdown_text, source=source, current_user=current_user)

    async def preview_markdown(
            self,
            markdown_text: str,
            *,
            source: AgentImportSource,
            current_user: UserDefinition | None = None,
    ) -> AgentImportProposal:
        parsed = parse_agent_markdown(markdown_text, source=source)
        warnings: list[AgentImportWarning] = []
        warnings.extend(scan_agent_import_safety(parsed))
        agent = await self._agent_from_parsed(parsed, current_user=current_user, warnings=warnings)
        tool_suggestions = await self._tool_suggestions(
            parsed.frontmatter.get("tool_ids"),
            body=parsed.body,
            warnings=warnings,
        )
        handoff_suggestions = await self._handoff_suggestions(
            parsed.frontmatter.get("handoff_agent_ids"),
            body=parsed.body,
            warnings=warnings,
        )
        conflicts = await self._conflicts(agent)
        if conflicts:
            warnings.append(
                AgentImportWarning(
                    code="agent_conflict",
                    message="An existing agent matches this import. Choose update_existing or duplicate_as_new to commit.",
                )
            )
        proposal = AgentImportProposal(
            source=parsed.source,
            detected_format=parsed.detected_format,
            agent=agent,
            suggested_tool_ids=tool_suggestions,
            suggested_handoff_agent_ids=handoff_suggestions,
            warnings=warnings,
            conflicts=conflicts,
            requires_review=True,
        )
        await self._audit_preview(proposal, current_user=current_user)
        return proposal

    async def batch_preview_from_request(
            self,
            payload: AgentImportBatchPreviewRequest,
            *,
            current_user: UserDefinition | None = None,
    ) -> AgentImportBatchPreviewResult:
        proposals: list[AgentImportProposal] = []
        errors: list[AgentImportBatchError] = []
        for index, item in enumerate(payload.items):
            try:
                proposals.append(await self.preview_from_request(item, current_user=current_user))
            except AgentImportError as exc:
                errors.append(
                    AgentImportBatchError(
                        index=index,
                        source_filename=item.source_filename,
                        source_url=item.source_url,
                        code=exc.code,
                        message=str(exc),
                    )
                )
        return AgentImportBatchPreviewResult(proposals=proposals, errors=errors)

    async def commit_from_request(
            self,
            payload: AgentImportCommitRequest,
            *,
            current_user: UserDefinition | None = None,
    ) -> AgentImportCommitResult:
        if payload.proposal is not None:
            proposal = payload.proposal
        else:
            preview = AgentImportPreviewRequest(
                markdown_text=payload.markdown_text,
                source_url=payload.source_url,
                source_filename=payload.source_filename,
                use_llm_normalization=payload.use_llm_normalization,
                llm_normalization_model_profile_id=payload.llm_normalization_model_profile_id,
            )
            proposal = await self.preview_from_request(preview, current_user=current_user)

        warnings = list(proposal.warnings)
        existing_by_id = await self.context.agent_repo.get(proposal.agent.id, include_deleted=True)
        existing_by_name = await self._find_agent_by_name(proposal.agent.name)
        existing = existing_by_id or existing_by_name

        if existing is not None and payload.conflict_strategy == "create_only":
            raise AgentImportError(
                "An agent with the same id or name already exists. Use update_existing or duplicate_as_new.",
                code="agent_import_conflict",
            )

        agent = proposal.agent.model_copy(deep=True)
        if payload.conflict_strategy == "duplicate_as_new":
            agent.id = str(uuid4())
            if existing is not None:
                agent.name = _dedupe_name(agent.name, await self.context.agent_repo.list(include_deleted=True))
                agent.display_name = agent.name
        elif payload.conflict_strategy == "update_existing" and existing is not None:
            agent.id = existing.id
            if payload.model_profile_id is None:
                agent.model_profile_id = existing.model_profile_id
            agent.tool_ids = list(existing.tool_ids)
            agent.handoff_agent_ids = list(existing.handoff_agent_ids)
            agent.metadata = {**existing.metadata, **agent.metadata}
            agent.framework_hints = agent.framework_hints.model_copy(
                update={
                    "metadata": {
                        **existing.framework_hints.metadata,
                        **agent.framework_hints.metadata,
                    }
                }
            )

        if payload.model_profile_id is not None:
            profile = await self.context.model_profile_repo.get(payload.model_profile_id)
            if profile is None:
                raise AgentImportError(
                    f"Model profile '{payload.model_profile_id}' was not found.",
                    code="model_profile_not_found",
                )
            agent.model_profile_id = payload.model_profile_id

        approved_tools = await self._approved_tool_ids(payload.approved_tool_ids)
        blocked_tools = [item.tool_id for item in proposal.suggested_tool_ids if item.tool_id not in approved_tools]
        if blocked_tools:
            warnings.append(
                AgentImportWarning(
                    code="tool_suggestions_not_granted",
                    message="Some imported tool suggestions were not granted: " + ", ".join(sorted(blocked_tools)),
                    field="tool_ids",
                )
            )
        agent.tool_ids = _merge_unique(agent.tool_ids, approved_tools)

        approved_handoffs = await self._approved_handoff_ids(payload.approved_handoff_agent_ids)
        blocked_handoffs = [
            item.agent_id
            for item in proposal.suggested_handoff_agent_ids
            if (item.matched_agent_id or item.agent_id) not in approved_handoffs
        ]
        if blocked_handoffs:
            warnings.append(
                AgentImportWarning(
                    code="handoff_suggestions_not_granted",
                    message="Some imported handoff suggestions were not granted: " + ", ".join(
                        sorted(blocked_handoffs)),
                    field="handoff_agent_ids",
                )
            )
        agent.handoff_agent_ids = _merge_unique(agent.handoff_agent_ids, approved_handoffs)

        enabled = payload.enabled if payload.enabled is not None else False
        import_metadata = dict(agent.metadata.get("import") or {})
        import_metadata.update(
            {
                "committed_at": datetime.now(timezone.utc).isoformat(),
                "committed_by": current_user.id if current_user is not None else None,
                "review_status": "committed",
            }
        )
        metadata = dict(agent.metadata)
        metadata["import"] = import_metadata
        metadata["enabled"] = enabled
        agent.metadata = metadata
        audit_execution_id = _ensure_import_audit_execution_id(agent)
        import_metadata = dict(agent.metadata.get("import") or {})
        import_metadata["commit_audit_execution_id"] = audit_execution_id
        metadata = dict(agent.metadata)
        metadata["import"] = import_metadata
        agent.metadata = metadata

        saved = await self.context.agent_repo.save(agent)
        status_value: Literal["created", "updated"] = (
            "updated"
            if payload.conflict_strategy == "update_existing" and existing is not None
            else "created"
        )
        result = AgentImportCommitResult(status=status_value, agent=saved, warnings=warnings)
        await self._audit_commit(
            result,
            proposal=proposal,
            conflict_strategy=payload.conflict_strategy,
            approved_tool_ids=approved_tools,
            approved_handoff_agent_ids=approved_handoffs,
            enabled=enabled,
            current_user=current_user,
        )
        return result

    async def batch_commit_from_request(
            self,
            payload: AgentImportBatchCommitRequest,
            *,
            current_user: UserDefinition | None = None,
    ) -> AgentImportBatchCommitResult:
        results: list[AgentImportCommitResult] = []
        errors: list[AgentImportBatchError] = []
        for index, item in enumerate(payload.items):
            try:
                results.append(await self.commit_from_request(item, current_user=current_user))
            except AgentImportError as exc:
                proposal = item.proposal
                errors.append(
                    AgentImportBatchError(
                        index=index,
                        source_filename=proposal.source.filename if proposal is not None else item.source_filename,
                        source_url=proposal.source.url if proposal is not None else item.source_url,
                        code=exc.code,
                        message=str(exc),
                    )
                )
        return AgentImportBatchCommitResult(results=results, errors=errors)

    async def formats(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "agency_agents_markdown",
                    "label": "Agency agents Markdown",
                    "description": "YAML frontmatter with name/description/style metadata plus a Markdown instruction body.",
                },
                {
                    "id": "skill_md",
                    "label": "SKILL.md",
                    "description": "Skill-style Markdown imported as a reusable specialist instruction bundle.",
                },
                {
                    "id": "claude",
                    "label": "Claude Code agent Markdown",
                    "description": "Claude-style subagent Markdown with stable frontmatter markers such as tools.",
                },
                {
                    "id": "opencode",
                    "label": "OpenCode agent Markdown",
                    "description": "OpenCode agent Markdown when explicit provider metadata is present.",
                },
                {
                    "id": "copilot",
                    "label": "GitHub Copilot instruction Markdown",
                    "description": "Copilot instruction files with stable markers such as applyTo or .instructions.md filenames.",
                },
                {
                    "id": "antigravity",
                    "label": "Antigravity agent Markdown",
                    "description": "Antigravity agent Markdown when explicit provider metadata is present.",
                },
                {
                    "id": "generic_markdown",
                    "label": "Generic Markdown",
                    "description": "Plain Markdown converted into an Agency agent using title/body heuristics.",
                },
            ],
            "import_modes": ["preview", "commit", "batch_preview", "batch_commit"],
            "commit_strategies": ["create_only", "update_existing", "duplicate_as_new"],
            "llm_normalization_available": False,
            "llm_normalization_requires_model_profile": True,
        }

    async def _reject_llm_normalization_request(
            self,
            model_profile_id: str | None,
            *,
            current_user: UserDefinition | None,
    ) -> None:
        if not model_profile_id:
            raise AgentImportError(
                "LLM normalization requires llm_normalization_model_profile_id.",
                code="llm_normalization_model_profile_required",
            )
        profile = await self.context.model_profile_repo.get(model_profile_id)
        if profile is None:
            raise AgentImportError(
                f"LLM normalization model profile '{model_profile_id}' was not found.",
                code="llm_normalization_model_profile_not_found",
            )
        self.context.runtime_operations.record_action(
            "agent.import.llm_normalization.requested",
            actor_user_id=current_user.id if current_user is not None else None,
            model_profile_id=model_profile_id,
            available=False,
        )
        raise AgentImportError(
            "LLM normalization is not available until model-output validation and review controls are enabled.",
            code="llm_normalization_unavailable",
        )

    async def _resolve_source(
            self,
            *,
            markdown_text: str | None,
            source_url: str | None,
            source_filename: str | None,
    ) -> tuple[str, AgentImportSource]:
        if markdown_text is not None:
            _validate_markdown_size(markdown_text.encode("utf-8"))
            return markdown_text, AgentImportSource(
                source_type="text",
                filename=source_filename,
                sha256=_sha256(markdown_text.encode("utf-8")),
            )
        if source_url is None:
            raise AgentImportError("No import source was provided.", code="missing_source")
        markdown = await fetch_remote_markdown(source_url)
        return markdown, AgentImportSource(
            source_type="url",
            filename=source_filename or _filename_from_url(source_url),
            url=source_url,
            sha256=_sha256(markdown.encode("utf-8")),
        )

    async def _agent_from_parsed(
            self,
            parsed: ParsedAgentMarkdown,
            *,
            current_user: UserDefinition | None,
            warnings: list[AgentImportWarning],
    ) -> AgentDefinition:
        frontmatter = parsed.frontmatter
        body = parsed.body.strip()
        if not body:
            raise AgentImportError("Markdown body is empty after frontmatter.", code="empty_markdown_body")
        if len(body) > MAX_INSTRUCTION_CHARACTERS:
            raise AgentImportError("Markdown instructions are too large.", code="instructions_too_large")

        title = _first_heading(body)
        name = _string_value(frontmatter.get("name")) or title or "Imported Agent"
        role = _string_value(frontmatter.get("role")) or _extract_role(body)
        agent_id = _string_value(frontmatter.get("id")) or _slugify(name)
        display_name = _string_value(frontmatter.get("display_name")) or name
        description = _string_value(frontmatter.get("description"))
        backstory = _string_value(frontmatter.get("backstory"))
        agent_kind = _agent_kind(frontmatter, parsed.detected_format, body)
        unknown_frontmatter = {
            key: value
            for key, value in frontmatter.items()
            if key not in {
                "id",
                "name",
                "display_name",
                "description",
                "role",
                "backstory",
                "model_profile_id",
                "tool_ids",
                "handoff_agent_ids",
            }
        }
        import_metadata = {
            "source_type": parsed.source.source_type,
            "source_url": parsed.source.url,
            "source_filename": parsed.source.filename,
            "source_sha256": parsed.source.sha256,
            "detected_format": parsed.detected_format,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "imported_by": current_user.id if current_user is not None else None,
            "review_status": "preview",
            "frontmatter": unknown_frontmatter,
            "llm_normalization_requested": False,
            "llm_normalization_used": False,
            "llm_normalization_model_profile_id": None,
        }
        model_profile_id = _string_value(frontmatter.get("model_profile_id"))
        if model_profile_id is not None and await self.context.model_profile_repo.get(model_profile_id) is None:
            warnings.append(
                AgentImportWarning(
                    code="model_profile_not_found",
                    message=f"Model profile '{model_profile_id}' was not found and was not assigned.",
                    field="model_profile_id",
                )
            )
            model_profile_id = None

        return AgentDefinition(
            id=agent_id,
            name=name,
            display_name=display_name,
            description=description,
            instructions=body,
            system_prompt=body,
            role=role,
            backstory=backstory,
            model_profile_id=model_profile_id,
            tool_ids=[],
            handoff_agent_ids=[],
            framework_hints=FrameworkHints(
                metadata={
                    "agent_kind": agent_kind,
                    "source_agent_format": parsed.detected_format,
                    **{
                        key: value
                        for key, value in unknown_frontmatter.items()
                        if key in {"color", "emoji", "vibe", "tags"}
                    },
                }
            ),
            metadata={"enabled": False, "agent_kind": agent_kind, "import": import_metadata},
        )

    async def _tool_suggestions(
            self,
            raw_tool_ids: Any,
            *,
            body: str,
            warnings: list[AgentImportWarning],
    ) -> list[AgentImportToolSuggestion]:
        ids = _merge_unique(_string_list(raw_tool_ids), await self._tool_references_from_body(body))
        suggestions: list[AgentImportToolSuggestion] = []
        for tool_id in ids:
            tool = await self.context.tool_repo.get(tool_id)
            if tool is None:
                warnings.append(
                    AgentImportWarning(
                        code="unknown_tool",
                        message=f"Tool '{tool_id}' does not exist and cannot be assigned during import.",
                        field="tool_ids",
                    )
                )
                suggestions.append(
                    AgentImportToolSuggestion(
                        tool_id=tool_id,
                        exists=False,
                        requires_review=True,
                        high_risk=True,
                        reason="Tool id was present in Markdown but does not exist in Agency.",
                    )
                )
                continue
            high_risk = _is_high_risk_tool(tool)
            suggestions.append(
                AgentImportToolSuggestion(
                    tool_id=tool_id,
                    exists=True,
                    requires_review=True,
                    high_risk=high_risk,
                    reason=(
                        "Existing Agency tool. Explicit approval is required before assignment."
                        if not high_risk
                        else "High-risk tool. Explicit approval is required before assignment."
                    ),
                )
            )
        return suggestions

    async def _handoff_suggestions(
            self,
            raw_handoff_ids: Any,
            *,
            body: str,
            warnings: list[AgentImportWarning],
    ) -> list[AgentImportHandoffSuggestion]:
        ids = _merge_unique(_string_list(raw_handoff_ids), self._handoff_references_from_body(body))
        suggestions: list[AgentImportHandoffSuggestion] = []
        for item in ids:
            direct = await self.context.agent_repo.get(item, include_deleted=True)
            matched = direct or await self._find_agent_by_name(item)
            if matched is None:
                warnings.append(
                    AgentImportWarning(
                        code="unknown_handoff_agent",
                        message=f"Handoff target '{item}' does not exist and was not assigned.",
                        field="handoff_agent_ids",
                    )
                )
                suggestions.append(
                    AgentImportHandoffSuggestion(
                        agent_id=item,
                        exists=False,
                        requires_review=True,
                        reason="Handoff target was present in Markdown but no matching Agency agent exists.",
                    )
                )
                continue
            suggestions.append(
                AgentImportHandoffSuggestion(
                    agent_id=item,
                    exists=True,
                    matched_agent_id=matched.id,
                    requires_review=True,
                    reason="Matching Agency agent exists. Explicit approval is required before handoff assignment.",
                )
            )
        return suggestions

    async def _tool_references_from_body(self, body: str) -> list[str]:
        references = _section_references(body, {"tool", "tools", "capability", "capabilities", "permissions"})
        if not references:
            return []
        tools = await self.context.tool_repo.list(include_deleted=True)
        resolved: list[str] = []
        for reference in references:
            match = _match_tool_reference(reference, tools)
            if match is not None:
                resolved.append(match.id)
        return list(dict.fromkeys(resolved))

    def _handoff_references_from_body(self, body: str) -> list[str]:
        return _section_references(
            body,
            {"handoff", "handoffs", "specialist", "specialists", "collaborator", "collaborators", "agents"},
        )

    async def _approved_tool_ids(self, requested_tool_ids: list[str]) -> list[str]:
        approved: list[str] = []
        for tool_id in requested_tool_ids:
            tool = await self.context.tool_repo.get(tool_id)
            if tool is None:
                raise AgentImportError(f"Tool '{tool_id}' was not found.", code="tool_not_found")
            approved.append(tool_id)
        return approved

    async def _approved_handoff_ids(self, requested_agent_ids: list[str]) -> list[str]:
        approved: list[str] = []
        for agent_id in requested_agent_ids:
            agent = await self.context.agent_repo.get(agent_id, include_deleted=True) or await self._find_agent_by_name(
                agent_id)
            if agent is None:
                raise AgentImportError(f"Handoff agent '{agent_id}' was not found.", code="handoff_agent_not_found")
            approved.append(agent.id)
        return approved

    async def _find_agent_by_name(self, name: str) -> AgentDefinition | None:
        normalized = _normalize_name(name)
        for agent in await self.context.agent_repo.list(include_deleted=True):
            if _normalize_name(agent.name) == normalized:
                return agent
        return None

    async def _conflicts(self, agent: AgentDefinition) -> list[AgentImportConflict]:
        conflicts: list[AgentImportConflict] = []
        existing_by_id = await self.context.agent_repo.get(agent.id, include_deleted=True)
        if existing_by_id is not None:
            conflicts.append(
                AgentImportConflict(
                    conflict_type="id",
                    existing_agent_id=existing_by_id.id,
                    existing_agent_name=existing_by_id.name,
                    message=f"Agent id '{agent.id}' already exists.",
                )
            )
        existing_by_name = await self._find_agent_by_name(agent.name)
        if existing_by_name is not None and existing_by_name.id != getattr(existing_by_id, "id", None):
            conflicts.append(
                AgentImportConflict(
                    conflict_type="name",
                    existing_agent_id=existing_by_name.id,
                    existing_agent_name=existing_by_name.name,
                    message=f"Agent name '{agent.name}' already exists.",
                )
            )
        return conflicts

    async def _audit_preview(
            self,
            proposal: AgentImportProposal,
            *,
            current_user: UserDefinition | None,
    ) -> None:
        execution_id = _ensure_import_audit_execution_id(proposal.agent)
        await self._emit_import_audit_event(
            execution_id=execution_id,
            event_type=ExecutionEventType.AGENT_IMPORT_PREVIEWED,
            current_user=current_user,
            payload={
                "operation": "preview",
                **_proposal_audit_payload(proposal),
            },
            output_payload={
                "operation": "preview",
                "agent_id": proposal.agent.id,
                "agent_name": proposal.agent.name,
                "warning_codes": [warning.code for warning in proposal.warnings],
                "conflict_count": len(proposal.conflicts),
            },
        )

    async def _audit_commit(
            self,
            result: AgentImportCommitResult,
            *,
            proposal: AgentImportProposal,
            conflict_strategy: str,
            approved_tool_ids: list[str],
            approved_handoff_agent_ids: list[str],
            enabled: bool,
            current_user: UserDefinition | None,
    ) -> None:
        execution_id = _ensure_import_audit_execution_id(result.agent)
        await self._emit_import_audit_event(
            execution_id=execution_id,
            event_type=ExecutionEventType.AGENT_IMPORT_COMMITTED,
            current_user=current_user,
            payload={
                "operation": "commit",
                "status": result.status,
                "conflict_strategy": conflict_strategy,
                "approved_tool_ids": approved_tool_ids,
                "approved_handoff_agent_ids": approved_handoff_agent_ids,
                "enabled": enabled,
                **_proposal_audit_payload(proposal),
                "saved_agent_id": result.agent.id,
                "saved_agent_name": result.agent.name,
            },
            output_payload={
                "operation": "commit",
                "status": result.status,
                "agent_id": result.agent.id,
                "agent_name": result.agent.name,
                "approved_tool_ids": approved_tool_ids,
                "approved_handoff_agent_ids": approved_handoff_agent_ids,
                "warning_codes": [warning.code for warning in result.warnings],
            },
        )

    async def _emit_import_audit_event(
            self,
            *,
            execution_id: str,
            event_type: ExecutionEventType,
            current_user: UserDefinition | None,
            payload: dict[str, Any],
            output_payload: dict[str, Any],
    ) -> ExecutionEvent:
        await self._ensure_import_audit_execution(
            execution_id=execution_id,
            current_user=current_user,
            payload=payload,
            output_payload=output_payload,
        )
        existing_events = await self.context.execution_store.list_events(execution_id)
        event = ExecutionEvent(
            execution_id=execution_id,
            workflow_id=AGENT_IMPORT_AUDIT_WORKFLOW_ID,
            parent_event_id=existing_events[-1].id if existing_events else None,
            trace_id=f"agent-import:{execution_id}",
            event_type=event_type,
            actor=current_user.id if current_user is not None else None,
            actor_type="user" if current_user is not None else "system",
            source="agent_markdown_import",
            status="completed",
            payload={**payload, "audit": True},
            metadata={"category": "agent_import", "audit": True},
        )
        saved = await self.context.execution_store.save_event(get_default_event_bus().publish(event))
        self.context.runtime_operations.record_action(
            event_type.value,
            execution_id=execution_id,
            actor_user_id=current_user.id if current_user is not None else None,
            source_sha256=payload.get("source", {}).get("sha256"),
            agent_id=payload.get("agent", {}).get("id") or payload.get("saved_agent_id"),
        )
        return saved

    async def _ensure_import_audit_execution(
            self,
            *,
            execution_id: str,
            current_user: UserDefinition | None,
            payload: dict[str, Any],
            output_payload: dict[str, Any],
    ) -> None:
        existing = await self.context.execution_store.get_execution(execution_id)
        now = utc_now()
        if existing is None:
            await self._ensure_import_audit_workflow()
            source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
            await self.context.execution_store.save_execution(
                Execution(
                    id=execution_id,
                    workflow_id=AGENT_IMPORT_AUDIT_WORKFLOW_ID,
                    runtime_adapter_id=AGENT_IMPORT_AUDIT_RUNTIME_ADAPTER_ID,
                    status=ExecutionStatus.COMPLETED,
                    trigger_type="agent_import",
                    trigger_payload={
                        "source_type": source.get("source_type"),
                        "source_filename": source.get("filename"),
                        "source_url": source.get("url"),
                        "source_sha256": source.get("sha256"),
                    },
                    input_payload={},
                    output_payload=output_payload,
                    metadata={
                        "mode": "agent_markdown_import_audit",
                        "source_sha256": source.get("sha256"),
                        "agent_ids": [payload.get("agent", {}).get("id")] if payload.get("agent") else [],
                    },
                    started_at=now,
                    completed_at=now,
                    created_by=current_user.id if current_user is not None else None,
                )
            )
            return
        existing.status = ExecutionStatus.COMPLETED
        existing.output_payload = output_payload
        existing.completed_at = now
        existing.updated_at = now
        await self.context.execution_store.update_execution(existing)

    async def _ensure_import_audit_workflow(self) -> None:
        existing = await self.context.workflow_repo.get(AGENT_IMPORT_AUDIT_WORKFLOW_ID)
        if existing is not None:
            return
        workflow = WorkflowDefinition(
            id=AGENT_IMPORT_AUDIT_WORKFLOW_ID,
            name="Agent Markdown Import Audit",
            description="Internal workflow used to anchor agent Markdown import audit executions.",
            entrypoint="agent-import-audit",
            nodes=[
                WorkflowNodeDefinition(
                    id="agent-import-audit",
                    name="Agent Import Audit",
                    node_type=NodeType.TASK,
                    metadata={"system": True, "audit": True},
                )
            ],
            allowed_runtime_adapter_ids=[AGENT_IMPORT_AUDIT_RUNTIME_ADAPTER_ID],
            default_runtime_adapter_id=AGENT_IMPORT_AUDIT_RUNTIME_ADAPTER_ID,
            versioning=VersionDefinition(
                version="1.0.0",
                revision=1,
                is_published=True,
                labels=["system", "agent-import", "audit"],
            ),
            metadata={
                "system": True,
                "hidden": True,
                "category": "agent_import",
                "managed_by": "agent_markdown_import",
            },
        )
        await self.context.workflow_repo.save(workflow)


def _ensure_import_audit_execution_id(agent: AgentDefinition) -> str:
    metadata = dict(agent.metadata)
    import_metadata = dict(metadata.get("import") or {})
    audit_execution_id = _string_value(import_metadata.get("preview_audit_execution_id"))
    if audit_execution_id is None:
        audit_execution_id = f"agent-import-{uuid4()}"
        import_metadata["preview_audit_execution_id"] = audit_execution_id
    metadata["import"] = import_metadata
    agent.metadata = metadata
    return audit_execution_id


def _proposal_audit_payload(proposal: AgentImportProposal) -> dict[str, Any]:
    return {
        "source": _source_audit_payload(proposal.source),
        "detected_format": proposal.detected_format,
        "requires_review": proposal.requires_review,
        "agent": {
            "id": proposal.agent.id,
            "name": proposal.agent.name,
            "model_profile_id": proposal.agent.model_profile_id,
            "agent_kind": proposal.agent.metadata.get("agent_kind") if proposal.agent.metadata else None,
        },
        "suggested_tool_ids": [
            {
                "tool_id": suggestion.tool_id,
                "exists": suggestion.exists,
                "requires_review": suggestion.requires_review,
                "high_risk": suggestion.high_risk,
            }
            for suggestion in proposal.suggested_tool_ids
        ],
        "suggested_handoff_agent_ids": [
            {
                "agent_id": suggestion.agent_id,
                "exists": suggestion.exists,
                "matched_agent_id": suggestion.matched_agent_id,
                "requires_review": suggestion.requires_review,
            }
            for suggestion in proposal.suggested_handoff_agent_ids
        ],
        "warnings": [
            {
                "code": warning.code,
                "severity": warning.severity,
                "field": warning.field,
            }
            for warning in proposal.warnings
        ],
        "conflicts": [
            {
                "conflict_type": conflict.conflict_type,
                "existing_agent_id": conflict.existing_agent_id,
                "existing_agent_name": conflict.existing_agent_name,
            }
            for conflict in proposal.conflicts
        ],
    }


def _source_audit_payload(source: AgentImportSource) -> dict[str, Any]:
    return {
        "source_type": source.source_type,
        "filename": source.filename,
        "url": source.url,
        "sha256": source.sha256,
    }


def parse_agent_markdown(markdown_text: str, *, source: AgentImportSource) -> ParsedAgentMarkdown:
    if not markdown_text or not markdown_text.strip():
        raise AgentImportError("Markdown content is empty.", code="empty_markdown")
    _validate_markdown_size(markdown_text.encode("utf-8"))
    frontmatter, body = _split_frontmatter(markdown_text)
    detected_format = _detect_format(frontmatter, source.filename, body)
    return ParsedAgentMarkdown(
        frontmatter=frontmatter,
        body=body.strip(),
        detected_format=detected_format,
        source=source,
    )


def scan_agent_import_safety(parsed: ParsedAgentMarkdown) -> list[AgentImportWarning]:
    warnings: list[AgentImportWarning] = []
    body = parsed.body
    frontmatter_text = yaml.safe_dump(parsed.frontmatter, sort_keys=True) if parsed.frontmatter else ""
    searchable = f"{frontmatter_text}\n{body}"
    if _matches_any(PROMPT_INJECTION_PATTERNS, searchable):
        warnings.append(
            AgentImportWarning(
                code="prompt_injection_detected",
                message=(
                    "Imported Markdown contains instructions that appear to override policies, approvals, "
                    "or higher-priority instructions. Review before committing."
                ),
                severity="error",
                field="instructions",
            )
        )
    if _matches_any(TOOL_GRANT_PATTERNS, searchable):
        warnings.append(
            AgentImportWarning(
                code="tool_grant_instruction_detected",
                message=(
                    "Imported Markdown appears to request automatic tool or permission grants. "
                    "Agency will keep these as review-only suggestions."
                ),
                severity="warning",
                field="instructions",
            )
        )
    if _matches_any(SECRET_PATTERNS, searchable):
        warnings.append(
            AgentImportWarning(
                code="secret_like_value_detected",
                message=(
                    "Imported Markdown contains secret-like values. Remove secrets from the source before committing "
                    "or confirm they are placeholders."
                ),
                severity="error",
                field="instructions",
            )
        )
    if _matches_any(SHELL_SNIPPET_PATTERNS, body):
        warnings.append(
            AgentImportWarning(
                code="shell_snippet_detected",
                message=(
                    "Imported Markdown contains shell command snippets. Import preview and commit never execute them, "
                    "but the instructions should be reviewed."
                ),
                severity="warning",
                field="instructions",
            )
        )
    return warnings


async def fetch_remote_markdown(source_url: str) -> str:
    fetch_url = normalize_remote_markdown_url(source_url)
    parsed = urlparse(fetch_url)
    if parsed.scheme != "https":
        raise AgentImportError("Only https source URLs are allowed.", code="source_url_blocked")
    if not parsed.hostname:
        raise AgentImportError("Source URL must include a hostname.", code="source_url_invalid")
    _assert_public_hostname(parsed.hostname)
    async with httpx.AsyncClient(
            timeout=REMOTE_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
    ) as client:
        try:
            response = await client.get(fetch_url, headers={"accept": "text/markdown,text/plain,*/*"})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AgentImportError(f"Could not fetch source URL: {exc}", code="source_url_fetch_failed") from exc
    content = response.content
    _validate_markdown_size(content)
    content_type = response.headers.get("content-type", "").lower()
    if content_type and not any(item in content_type for item in ("text/", "markdown", "octet-stream")):
        raise AgentImportError("Source URL did not return a text or Markdown response.",
                               code="source_content_type_invalid")
    try:
        return content.decode(response.encoding or "utf-8")
    except UnicodeDecodeError as exc:
        raise AgentImportError("Source Markdown is not valid UTF-8 text.", code="source_encoding_invalid") from exc


def normalize_remote_markdown_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if parsed.scheme == "https" and parsed.hostname == "github.com":
        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 5 and path_parts[2] == "blob":
            owner, repo, _, branch = path_parts[:4]
            raw_path = "/".join(path_parts[4:])
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{raw_path}"
    return source_url


def _matches_any(patterns: tuple[re.Pattern[str], ...], value: str) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def _split_frontmatter(markdown_text: str) -> tuple[dict[str, Any], str]:
    content = markdown_text.lstrip("\ufeff")
    if not content.startswith("---"):
        return {}, content
    if content.startswith("---\n") or content.startswith("---\r\n"):
        match = re.match(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)$", content, re.DOTALL)
        if match is None:
            return {}, content
        return _parse_frontmatter(match.group(1)), match.group(2)

    inline = re.match(r"^---\s+(.*?)\s+---\s*(.*)$", content, re.DOTALL)
    if inline is None:
        return {}, content
    return _parse_frontmatter(inline.group(1)), inline.group(2)


def _parse_frontmatter(raw_frontmatter: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        return {str(key): value for key, value in parsed.items()}
    inline = _parse_inline_frontmatter(raw_frontmatter)
    if inline:
        return inline
    raise AgentImportError("Frontmatter must be a YAML object.", code="invalid_frontmatter")


def _parse_inline_frontmatter(raw_frontmatter: str) -> dict[str, Any]:
    keys = "|".join(re.escape(key) for key in FRONTMATTER_KEYS)
    pattern = re.compile(rf"(?P<key>{keys})\s*:\s*(?P<value>.*?)(?=\s+(?:{keys})\s*:|$)", re.DOTALL)
    result: dict[str, Any] = {}
    for match in pattern.finditer(raw_frontmatter.strip()):
        key = match.group("key")
        value = match.group("value").strip()
        if value:
            result[key] = value
    return result


def _detect_format(frontmatter: dict[str, Any], filename: str | None, body: str) -> str:
    explicit = _explicit_provider_format(frontmatter)
    if explicit is not None:
        return explicit
    normalized_filename = filename.strip().lower() if filename else ""
    if normalized_filename == "skill.md":
        return "skill_md"
    if normalized_filename in {"copilot-instructions.md", "copilot.md"} or normalized_filename.endswith(
            ".instructions.md"):
        return "copilot"
    if "applyTo" in frontmatter or "apply_to" in frontmatter:
        return "copilot"
    if {"name", "description", "tools"}.issubset(frontmatter):
        return "claude"
    if {"name", "description"}.issubset(frontmatter) and any(key in frontmatter for key in ("color", "emoji", "vibe")):
        return "agency_agents_markdown"
    lowered = body.lower()
    if "skill" in lowered and "trigger" in lowered and normalized_filename.endswith(".md"):
        return "skill_md"
    return "generic_markdown"


def _explicit_provider_format(frontmatter: dict[str, Any]) -> str | None:
    for key in ("source_agent_format", "agent_format", "provider", "platform"):
        value = _string_value(frontmatter.get(key))
        if not value:
            continue
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in {"claude", "claude_code"}:
            return "claude"
        if normalized in {"opencode", "open_code"}:
            return "opencode"
        if normalized in {"copilot", "github_copilot"}:
            return "copilot"
        if normalized == "antigravity":
            return "antigravity"
    return None


def _agent_kind(frontmatter: dict[str, Any], detected_format: str, body: str) -> str:
    configured = _string_value(frontmatter.get("agent_kind"))
    if configured:
        return configured
    lowered = body.lower()
    if "orchestrator" in lowered or "handoff" in lowered or "specialist agents" in lowered:
        return "orchestrator"
    if detected_format == "skill_md":
        return "skill"
    return "specialist" if detected_format == "agency_agents_markdown" else "external"


def _first_heading(body: str) -> str | None:
    match = re.search(r"^\s*#\s+(.+?)\s*$", body, re.MULTILINE)
    return match.group(1).strip() if match else None


def _extract_role(body: str) -> str | None:
    match = re.search(r"\*\*Role\*\*\s*:\s*(.+?)(?:\n|$)", body)
    if match:
        return match.group(1).strip(" -*")
    match = re.search(r"^\s*[-*]\s*Role\s*:\s*(.+?)(?:\n|$)", body, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _section_references(body: str, heading_keywords: set[str]) -> list[str]:
    references: list[str] = []
    active_level: int | None = None
    for line in body.splitlines():
        heading = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", line)
        if heading is not None:
            level = len(heading.group(1))
            title_words = set(re.findall(r"[a-z0-9]+", heading.group(2).lower()))
            active_level = level if title_words & heading_keywords else None
            continue
        if active_level is None:
            continue
        next_heading = re.match(r"^\s{0,3}(#{1,6})\s+", line)
        if next_heading is not None and len(next_heading.group(1)) <= active_level:
            active_level = None
            continue
        references.extend(_references_from_section_line(line))
    return list(dict.fromkeys(references))


def _references_from_section_line(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped:
        return []
    backticked = [item.strip() for item in re.findall(r"`([^`]+)`", stripped) if item.strip()]
    if backticked:
        return backticked
    bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(?P<value>.+)$", stripped)
    if bullet is None:
        return []
    value = bullet.group("value").strip()
    value = re.split(r"\s+(?:-|--|:|=>)\s+", value, maxsplit=1)[0].strip()
    value = re.sub(r"\s*\([^)]*\)\s*$", "", value).strip()
    return [value] if value else []


def _match_tool_reference(reference: str, tools: list[ToolDefinition]) -> ToolDefinition | None:
    reference_clean = reference.strip()
    reference_normalized = _normalize_name(reference_clean)
    reference_slug = _slugish(reference_clean)
    for tool in tools:
        names = {
            tool.id,
            tool.name,
            tool.display_name or "",
        }
        for name in names:
            if not name:
                continue
            if reference_clean.lower() == name.lower():
                return tool
            if reference_normalized == _normalize_name(name):
                return tool
            if reference_slug == _slugish(name):
                return tool
    return None


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value).strip() or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if "," in value:
            return [item.strip() for item in value.split(",") if item.strip()]
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or f"imported-agent-{uuid4()}"


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _slugish(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")


def _merge_unique(existing: list[str], additions: list[str]) -> list[str]:
    return list(dict.fromkeys([*existing, *additions]))


def _dedupe_name(name: str, agents: list[AgentDefinition]) -> str:
    existing = {_normalize_name(agent.name) for agent in agents}
    if _normalize_name(name) not in existing:
        return name
    index = 2
    while _normalize_name(f"{name} {index}") in existing:
        index += 1
    return f"{name} {index}"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _validate_markdown_size(content: bytes) -> None:
    if len(content) > MAX_MARKDOWN_BYTES:
        raise AgentImportError("Markdown file is too large.", code="markdown_too_large")


def _filename_from_url(source_url: str) -> str | None:
    path = urlparse(source_url).path
    filename = path.rsplit("/", 1)[-1].strip()
    return filename or None


def _assert_public_hostname(hostname: str) -> None:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise AgentImportError("Source URL hostname could not be resolved.", code="source_url_blocked") from exc
    for info in infos:
        address = info[4][0]
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise AgentImportError("Source URL resolves to a blocked network address.", code="source_url_blocked")


def _is_high_risk_tool(tool: ToolDefinition) -> bool:
    security = tool.security
    return bool(
        tool.tool_type in HIGH_RISK_TOOL_TYPES
        or security.dangerous
        or security.allow_shell
        or security.allow_browser
        or security.allow_filesystem
        or security.allow_network
        or security.credential_references
    )


def validation_error_detail(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {
            **{key: value for key, value in item.items() if key != "ctx"},
            **({"ctx": {key: str(value) for key, value in item.get("ctx", {}).items()}} if item.get("ctx") else {}),
        }
        for item in exc.errors()
    ]
