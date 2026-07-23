"""Versioned transport contracts for the private browser runtime."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


ExtractMode = Literal["auto", "text", "markdown", "article", "html", "none"]
BrowserAction = Literal["screenshot", "scroll", "click", "select", "type", "verify", "mouse_click", "key_press"]


class OwnerClaims(BaseModel):
    execution_id: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    user_id: str | None = None
    actor: str | None = None

    @property
    def identity_key(self) -> tuple[str | None, ...]:
        return self.execution_id, self.workspace_id, self.user_id, self.actor

    @property
    def is_identified(self) -> bool:
        return any(self.identity_key)

    def owns(self, recorded: "OwnerClaims") -> bool:
        if not self.is_identified or not recorded.is_identified:
            return self == recorded
        return all(expected is None or expected == actual
                   for expected, actual in zip(recorded.identity_key, self.identity_key, strict=True))


class BrowserOptions(BaseModel):
    headless: bool = True
    mobile: bool = False
    locale: str | None = None
    timezone_id: str | None = None
    user_agent: str | None = None
    viewport_width: int = Field(default=1440, ge=320, le=3840)
    viewport_height: int = Field(default=900, ge=240, le=2160)
    device_scale_factor: float = Field(default=1.0, ge=0.5, le=3.0)
    extra_http_headers: dict[str, str] = Field(default_factory=dict)
    http_credentials: dict[str, str] | None = None
    storage_state: dict[str, Any] | None = None
    trace_mode: Literal["off", "on", "retain-on-failure"] = "off"
    record_video: bool = False
    proxy_binding: str | None = None

    @model_validator(mode="after")
    def consistent_fingerprint(self) -> Self:
        mobile_ua = bool(self.user_agent and "mobile" in self.user_agent.lower())
        if mobile_ua and not self.mobile:
            raise ValueError("A mobile user agent requires mobile=true to keep the browser fingerprint consistent")
        if self.mobile and self.viewport_width == 1440 and self.viewport_height == 900:
            self.viewport_width, self.viewport_height = 390, 844
        if self.mobile and self.viewport_width > 768:
            raise ValueError("Mobile browser profiles require a viewport width of 768 pixels or less")
        return self


class BrowserRuntimePolicy(BaseModel):
    """Per-open resource preferences bounded again by runtime operator policy."""

    session_idle_ttl_seconds: int | None = Field(default=None, ge=30, le=86_400)
    session_maximum_ttl_seconds: int | None = Field(default=None, ge=60, le=604_800)
    max_sessions_per_owner: int | None = Field(default=None, ge=1, le=32)
    max_sessions_total: int | None = Field(default=None, ge=1, le=128)
    navigation_timeout_ms: int | None = Field(default=None, ge=5_000, le=300_000)
    retry_attempts: int | None = Field(default=None, ge=1, le=3)
    domain_max_concurrency: int | None = Field(default=None, ge=1, le=16)
    domain_min_interval_seconds: float | None = Field(default=None, ge=0.0, le=60.0)
    artifact_retention_seconds: int | None = Field(default=None, ge=60, le=604_800)

    @model_validator(mode="after")
    def consistent_limits(self) -> Self:
        if (
                self.session_idle_ttl_seconds is not None
                and self.session_maximum_ttl_seconds is not None
                and self.session_idle_ttl_seconds > self.session_maximum_ttl_seconds
        ):
            raise ValueError("Session idle TTL cannot exceed the maximum session TTL")
        if (
                self.max_sessions_per_owner is not None
                and self.max_sessions_total is not None
                and self.max_sessions_per_owner > self.max_sessions_total
        ):
            raise ValueError("Per-owner session limit cannot exceed the total session limit")
        return self


class OpenRequest(BaseModel):
    url: str
    goal: str | None = None
    extract_mode: ExtractMode = "auto"
    keep_open: bool = True
    session_id: str | None = None
    allowed_hosts: list[str] = Field(default_factory=list)
    options: BrowserOptions = Field(default_factory=BrowserOptions)
    runtime_policy: BrowserRuntimePolicy = Field(default_factory=BrowserRuntimePolicy)
    correlation_id: str | None = None

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(_cls, value: list[str]) -> list[str]:
        return sorted({host.strip().lower().rstrip(".") for host in value if host.strip()})


class ExtractRequest(BaseModel):
    extract_mode: ExtractMode = "auto"
    goal: str | None = None
    max_chars: int = Field(default=100_000, ge=1_000, le=1_000_000)
    correlation_id: str | None = None


class ActionRequest(BaseModel):
    action: BrowserAction
    instruction: str | None = None
    sequence_number: int | None = None
    scroll_direction: str | None = None
    value: str | None = None
    full_page: bool = True
    x: float | None = Field(default=None, ge=0)
    y: float | None = Field(default=None, ge=0)
    key: str | None = None
    correlation_id: str | None = None


class ChallengeResult(BaseModel):
    kind: str = "none"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    indicators: list[str] = Field(default_factory=list)
    http_status: int | None = None
    final_url: str | None = None
    engine: str | None = None
    retryable: bool = False
    terminal: bool = False
    human_action_required: bool = False
    instructions: str | None = None


class LinkResult(BaseModel):
    text: str = ""
    url: str


class ArticleMetadata(BaseModel):
    author: str | None = None
    published_at: str | None = None
    description: str | None = None
    site_name: str | None = None


class ExtractionResult(BaseModel):
    mode: ExtractMode
    title: str | None = None
    canonical_url: str | None = None
    text: str | None = None
    markdown: str | None = None
    html: str | None = None
    article: ArticleMetadata | None = None
    links: list[LinkResult] = Field(default_factory=list)
    truncated: bool = False


class BrowserTimings(BaseModel):
    total_ms: float = 0.0
    navigation_ms: float = 0.0
    extraction_ms: float = 0.0
    challenge_ms: float = 0.0


class HumanHandoff(BaseModel):
    required: bool = True
    session_id: str
    screenshot_artifact_id: str | None = None
    instructions: str
    ask_tool: Literal["agency.human.ask"] = "agency.human.ask"
    resume_tool: Literal["agency.browser.open"] = "agency.browser.open"
    expires_at: float


class BrowserResponse(BaseModel):
    version: Literal["agency.browser.v1"] = "agency.browser.v1"
    status: Literal["ok", "error", "human_action_required"] = "ok"
    requested_url: str | None = None
    final_url: str | None = None
    title: str | None = None
    session_id: str | None = None
    interactive: bool = False
    engine: str
    extraction: ExtractionResult | None = None
    challenge: ChallengeResult = Field(default_factory=ChallengeResult)
    timings: BrowserTimings = Field(default_factory=BrowserTimings)
    artifacts: dict[str, str] = Field(default_factory=dict)
    human_handoff: HumanHandoff | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    correlation_id: str | None = None


class SessionStatus(BaseModel):
    session_id: str
    engine: str
    status: str
    current_url: str | None = None
    created_at: float
    last_used_at: float
    idle_expires_at: float
    maximum_expires_at: float
    challenge: ChallengeResult = Field(default_factory=ChallengeResult)


class HealthResult(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    engines: dict[str, dict[str, Any]]
    active_sessions: int
    runtime_root: str
    free_bytes: int | None = None
    release: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)

