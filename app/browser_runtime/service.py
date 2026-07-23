"""Unified orchestration across Patchright, extraction, recovery, and fallback."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag

from .adapters import EngineNavigationError, PatchrightAdapter, ScraplingAdapter
from .adapters.base import BrowserEngineAdapter, EngineSession, EngineUnavailableError
from .artifacts import ArtifactStore
from .contracts import (
    ActionRequest,
    BrowserResponse,
    BrowserTimings,
    ExtractRequest,
    HealthResult,
    HumanHandoff,
    OpenRequest,
    OwnerClaims,
)
from .events import BrowserTelemetry, cgroup_resources, process_tree_resources
from .domain_policy import DomainPolicyStore
from .proxy import ProxyResolver
from .security import validate_public_url
from .session_registry import AsyncSessionRegistry, SessionRecord


class BrowserRuntimeService:
    def __init__(
            self,
            *,
            primary: BrowserEngineAdapter | None = None,
            fallback: BrowserEngineAdapter | None = None,
            registry: AsyncSessionRegistry | None = None,
            proxy_resolver: ProxyResolver | None = None,
            telemetry: BrowserTelemetry | None = None,
            runtime_root: str | Path | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root or os.getenv("BROWSER_RUNTIME_ROOT", "/tmp/agency-browser-runtime"))
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.primary = primary or PatchrightAdapter(runtime_root=self.runtime_root / "engines")
        self.fallback = fallback or ScraplingAdapter(runtime_root=self.runtime_root / "engines")
        self.registry = registry or AsyncSessionRegistry()
        self.proxy_resolver = proxy_resolver or ProxyResolver()
        self.telemetry = telemetry or BrowserTelemetry()
        self.artifacts = ArtifactStore(self.runtime_root)
        self.domain_policy = DomainPolicyStore(self.runtime_root / "domain-policy.json")
        self.navigation_timeout_ms = max(5_000, int(os.getenv("BROWSER_NAVIGATION_TIMEOUT_MS", "45000")))
        self.retry_budget = max(1, min(3, int(os.getenv("BROWSER_PATCHRIGHT_ATTEMPTS", "3"))))
        self.challenge_handling_enabled = os.getenv("BROWSER_CHALLENGE_HANDLING_ENABLED", "true").lower() in {
            "1", "true", "yes",
        }
        self.extraction_enabled = os.getenv("BROWSER_UNIFIED_EXTRACTION_ENABLED", "true").lower() in {
            "1", "true", "yes",
        }

    async def open(self, request: OpenRequest, *, owner: OwnerClaims) -> BrowserResponse:
        domain = urlsplit(request.url).hostname or ""
        policy = self._effective_runtime_policy(request)
        lease = await self.domain_policy.acquire(
            domain,
            max_concurrency=int(policy["domain_max_concurrency"]),
            minimum_interval_seconds=float(policy["domain_min_interval_seconds"]),
        )
        try:
            response = await self._open_locked(request, owner=owner, policy=policy)
            response.diagnostics.setdefault("runtime_policy", policy)
            self.domain_policy.record(
                domain,
                engine=response.engine,
                success=response.status in {"ok", "human_action_required"},
                challenge=response.challenge.kind,
                fallback=response.engine == self.fallback.name,
            )
            return response
        except Exception:
            self.domain_policy.record(domain, engine=self.primary.name, success=False)
            raise
        finally:
            await lease.release()

    async def _open_locked(
            self,
            request: OpenRequest,
            *,
            owner: OwnerClaims,
            policy: dict[str, int | float],
    ) -> BrowserResponse:
        started = time.perf_counter()
        await asyncio.to_thread(validate_public_url, request.url, request.allowed_hosts)
        if request.session_id:
            return await self._navigate_existing(request, owner=owner, started=started, policy=policy)
        domain = urlsplit(request.url).hostname or ""
        history = self.domain_policy.get(domain)
        if history and history.last_challenge and history.cooldown_until > time.time():
            # A recent confirmed block is useful evidence for rotating a sticky
            # pool assignment, while Patchright remains the mandatory first engine.
            self.proxy_resolver.invalidate(domain)
            self.telemetry.emit(
                "domain_challenge_history_applied",
                domain=domain,
                last_challenge=history.last_challenge,
                last_engine=history.last_engine,
                correlation_id=request.correlation_id,
            )
        proxy = self.proxy_resolver.resolve(request.options.proxy_binding, domain=domain)
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None

        for attempt in range(1, int(policy["retry_attempts"]) + 1):
            session: EngineSession | None = None
            failure_engine_artifacts: dict[str, str] = {}
            try:
                session = await self.primary.start(
                    options=request.options,
                    proxy=proxy,
                    allowed_hosts=request.allowed_hosts,
                )
                # Install the sink before navigation so retain-on-failure
                # traces and videos survive failures raised during goto().
                self._attach_engine_artifact_sink(
                    session,
                    owner=owner,
                    session_id=f"failure-{request.correlation_id or time.time_ns()}",
                    destination=failure_engine_artifacts,
                    retention_seconds=int(policy["artifact_retention_seconds"]),
                )
                response = await self._navigate_and_build(
                    self.primary,
                    session,
                    request,
                    owner=owner,
                    started=started,
                    attempt=attempt,
                    policy=policy,
                )
                attempts.append({"engine": self.primary.name, "attempt": attempt, "outcome": response.status})
                response.diagnostics["attempts"] = attempts
                if response.challenge.kind == "none" or response.status == "human_action_required":
                    return response
                if not response.challenge.retryable:
                    if response.session_id:
                        await self.registry.close(
                            owner=owner,
                            session_id=response.session_id,
                            status="terminal_challenge",
                        )
                        response.session_id = None
                        response.interactive = False
                    return response
                if response.session_id:
                    await self.registry.close(owner=owner, session_id=response.session_id, status="retrying")
                else:
                    await session.close()
                if attempt == 1:
                    await asyncio.sleep(0.25)
                else:
                    self.proxy_resolver.invalidate(domain)
            except Exception as exc:
                last_error = exc
                failure_artifact = await self._capture_failure_artifact(
                    self.primary,
                    session,
                    owner=owner,
                    correlation_id=request.correlation_id,
                    retention_seconds=int(policy["artifact_retention_seconds"]),
                ) if session is not None else None
                navigation_diagnostics = self._safe_navigation_diagnostics(exc)
                if isinstance(exc, EngineNavigationError) and exc.diagnostics.get("html_excerpt"):
                    html_artifact = self._capture_failure_html(
                        str(exc.diagnostics["html_excerpt"]),
                        owner=owner,
                        correlation_id=request.correlation_id,
                        retention_seconds=int(policy["artifact_retention_seconds"]),
                    )
                    if html_artifact:
                        navigation_diagnostics["html_excerpt_artifact_id"] = html_artifact
                if session is not None:
                    await session.close()
                attempts.append({
                    "engine": self.primary.name,
                    "attempt": attempt,
                    "outcome": "error",
                    "type": type(exc).__name__,
                    **({"screenshot_artifact_id": failure_artifact} if failure_artifact else {}),
                    **({"engine_artifacts": failure_engine_artifacts} if failure_engine_artifacts else {}),
                    **({"navigation": navigation_diagnostics} if navigation_diagnostics else {}),
                })
                self.telemetry.emit(
                    "navigation_attempt_failed",
                    engine=self.primary.name,
                    attempt=attempt,
                    correlation_id=request.correlation_id,
                    error_type=type(exc).__name__,
                )
                if "timeout" in str(exc).lower():
                    self.telemetry.emit("navigation_timeout", engine=self.primary.name,
                                        correlation_id=request.correlation_id)

        fallback_session: EngineSession | None = None
        try:
            self.telemetry.emit("fallback_started", engine=self.fallback.name, correlation_id=request.correlation_id)
            fallback_session = await self.fallback.start(
                options=request.options,
                proxy=proxy,
                allowed_hosts=request.allowed_hosts,
            )
            fallback_response = await self._navigate_and_build(
                self.fallback,
                fallback_session,
                request.model_copy(update={"keep_open": False}),
                owner=owner,
                started=started,
                attempt=1,
                policy=policy,
            )
            attempts.append({"engine": self.fallback.name, "attempt": 1, "outcome": fallback_response.status})
            fallback_response.diagnostics["attempts"] = attempts
            if not request.keep_open:
                return fallback_response
            if fallback_response.challenge.kind != "none":
                fallback_response.status = "human_action_required" if fallback_response.challenge.human_action_required else "error"
                fallback_response.message = "Scrapling retrieved a challenge page but could not preserve a controllable session."
                return fallback_response
            # Retained fallback is only reported after a fresh Patchright page
            # successfully loads the resolved content without another challenge.
            handoff = await self._handoff_to_primary(
                request,
                fallback_response,
                fallback_session=fallback_session,
                owner=owner,
                started=started,
                policy=policy,
            )
            handoff.diagnostics["attempts"] = attempts + [{"engine": self.primary.name, "attempt": "handoff", "outcome": handoff.status}]
            return handoff
        except Exception as exc:
            last_error = exc
            attempts.append({"engine": self.fallback.name, "attempt": 1, "outcome": "error", "type": type(exc).__name__})
        finally:
            if fallback_session is not None:
                await fallback_session.close()

        return BrowserResponse(
            status="error",
            requested_url=request.url,
            engine=self.primary.name,
            interactive=False,
            timings=BrowserTimings(total_ms=(time.perf_counter() - started) * 1000),
            diagnostics={"attempts": attempts, "error_type": type(last_error).__name__ if last_error else None},
            message=str(last_error or "Browser navigation failed after all bounded attempts"),
            correlation_id=request.correlation_id,
        )

    async def extract(self, session_id: str, request: ExtractRequest, *, owner: OwnerClaims) -> BrowserResponse:
        started = time.perf_counter()
        record = await self.registry.resolve(owner=owner, session_id=session_id)
        adapter = self._adapter_for(record.engine)
        extraction_started = time.perf_counter()
        extraction = await adapter.extract(record.handle, mode=request.extract_mode, max_chars=request.max_chars)
        challenge = await adapter.challenge(record.handle)
        record.challenge = challenge
        record.current_url = record.handle.page.url if getattr(record.handle, "page", None) else record.current_url
        self.telemetry.emit("extraction_completed", engine=record.engine, session_id=session_id,
                            correlation_id=request.correlation_id, mode=request.extract_mode)
        self.telemetry.observe(
            "extraction_latency",
            (time.perf_counter() - extraction_started) * 1000,
            engine=record.engine,
            correlation_id=request.correlation_id,
        )
        return BrowserResponse(
            status="human_action_required" if challenge.human_action_required else "ok",
            requested_url=record.current_url,
            final_url=record.current_url,
            title=extraction.title,
            session_id=session_id,
            interactive=True,
            engine=record.engine,
            extraction=extraction,
            challenge=challenge,
            timings=BrowserTimings(
                total_ms=(time.perf_counter() - started) * 1000,
                extraction_ms=(time.perf_counter() - extraction_started) * 1000,
            ),
            correlation_id=request.correlation_id,
        )

    async def action(self, session_id: str, request: ActionRequest, *, owner: OwnerClaims) -> dict[str, Any]:
        record = await self.registry.resolve(owner=owner, session_id=session_id)
        adapter = self._adapter_for(record.engine)
        result = await adapter.action(record.handle, request)
        record.current_url = result.get("final_url") or record.current_url
        self.telemetry.emit(
            "interaction_completed",
            engine=record.engine,
            session_id=session_id,
            action=request.action,
            correlation_id=request.correlation_id,
        )
        # Form values and screenshots are intentionally omitted from telemetry.
        return {"session_id": session_id, "engine": record.engine, **result}

    async def close(self, session_id: str, *, owner: OwnerClaims) -> dict[str, Any]:
        record = await self.registry.resolve(owner=owner, session_id=session_id, touch=False)
        closed = await self.registry.close(owner=owner, session_id=session_id)
        if closed:
            self.telemetry.emit("session_closed", session_id=session_id)
        return {"closed": closed, "session_id": session_id, "artifacts": dict(record.artifacts)}

    async def close_all_for_execution(self, execution_id: str) -> int:
        count = await self.registry.close_all_for_execution(execution_id)
        self.telemetry.emit("execution_sessions_closed", execution_id=execution_id, count=count)
        return count

    async def status(self, *, owner: OwnerClaims) -> list[dict[str, Any]]:
        return [item.model_dump() for item in await self.registry.list_for_owner(owner)]

    async def health(self) -> HealthResult:
        primary, fallback = await asyncio.gather(self.primary.health(), self.fallback.health())
        free = shutil.disk_usage(self.runtime_root).free
        status = "ok" if primary.get("available") else "unhealthy"
        if status == "ok" and not fallback.get("available"):
            status = "degraded"
        metrics = self.telemetry.snapshot()
        metrics["gauges"] = {
            "active_sessions": self.registry.active_count,
            "active_pages": int(primary.get("active", 0)) + int(fallback.get("active", 0)),
            "browser_crashes": int(primary.get("crashes", 0)) + int(fallback.get("crashes", 0)),
            "cleanup_failures": self.registry.cleanup_failures,
            **cgroup_resources(),
            **process_tree_resources(),
        }
        return HealthResult(
            status=status,
            engines={self.primary.name: primary, self.fallback.name: fallback},
            active_sessions=self.registry.active_count,
            runtime_root=str(self.runtime_root),
            free_bytes=free,
            release={
                "id": os.getenv("BROWSER_RUNTIME_RELEASE", "local"),
                "image": os.getenv("BROWSER_RUNTIME_IMAGE_REF", "unknown"),
            },
            metrics=metrics,
        )

    async def expire(self) -> int:
        count = await self.registry.expire()
        self.artifacts.prune()
        if count:
            self.telemetry.emit("sessions_expired", count=count)
        return count

    async def shutdown(self) -> None:
        count = await self.registry.close_all(status="shutdown")
        await asyncio.gather(self.primary.shutdown(), self.fallback.shutdown(), return_exceptions=True)
        self.telemetry.emit("runtime_shutdown", closed_sessions=count)

    async def _navigate_existing(
            self,
            request: OpenRequest,
            *,
            owner: OwnerClaims,
            started: float,
            policy: dict[str, int | float],
    ) -> BrowserResponse:
        record = await self.registry.resolve(owner=owner, session_id=request.session_id or "")
        if set(request.allowed_hosts) - set(record.allowed_hosts):
            raise PermissionError("A retained session cannot expand its approved host set")
        adapter = self._adapter_for(record.engine)
        navigation_started = time.perf_counter()
        navigation = await adapter.navigate(
            record.handle,
            request.url,
            timeout_ms=int(policy["navigation_timeout_ms"]),
        )
        challenge = await adapter.challenge(record.handle, http_status=navigation.get("http_status"))
        extraction = None
        if request.extract_mode != "none" and self.extraction_enabled:
            extraction = await adapter.extract(record.handle, mode=request.extract_mode, max_chars=100_000)
        record.current_url = navigation.get("final_url")
        record.challenge = challenge
        artifacts: dict[str, str] = {}
        human_handoff = None
        if challenge.human_action_required:
            artifact = await self._capture_handoff(adapter, record)
            if artifact:
                artifacts["challenge_screenshot_id"] = artifact
            human_handoff = HumanHandoff(
                session_id=record.session_id,
                screenshot_artifact_id=artifact,
                instructions=challenge.instructions or "Complete the verification and resume this session.",
                expires_at=record.idle_expires_at,
            )
        return BrowserResponse(
            status="human_action_required" if challenge.human_action_required else "ok",
            requested_url=request.url,
            final_url=navigation.get("final_url"),
            title=navigation.get("title"),
            session_id=record.session_id,
            interactive=True,
            engine=record.engine,
            extraction=extraction,
            challenge=challenge,
            artifacts=artifacts,
            human_handoff=human_handoff,
            timings=BrowserTimings(
                total_ms=(time.perf_counter() - started) * 1000,
                navigation_ms=(time.perf_counter() - navigation_started) * 1000,
            ),
            correlation_id=request.correlation_id,
        )

    async def _navigate_and_build(
            self,
            adapter: BrowserEngineAdapter,
            session: EngineSession,
            request: OpenRequest,
            *,
            owner: OwnerClaims,
            started: float,
            attempt: int,
            policy: dict[str, int | float],
    ) -> BrowserResponse:
        navigation_started = time.perf_counter()
        navigation = await adapter.navigate(
            session,
            request.url,
            timeout_ms=int(policy["navigation_timeout_ms"]),
        )
        navigation_ms = (time.perf_counter() - navigation_started) * 1000
        challenge_started = time.perf_counter()
        challenge = await adapter.challenge(session, http_status=navigation.get("http_status"))
        recover = getattr(adapter, "recover_challenge", None)
        if self.challenge_handling_enabled and challenge.kind != "none" and recover is not None and not challenge.terminal:
            recovered = await recover(session, budget_ms=min(int(policy["navigation_timeout_ms"]), 8_000))
            if recovered:
                challenge = await adapter.challenge(session, http_status=navigation.get("http_status"))
                self.telemetry.emit(
                    "challenge_recovery_attempted",
                    engine=adapter.name,
                    cleared=challenge.kind == "none",
                    correlation_id=request.correlation_id,
                )
        challenge_ms = (time.perf_counter() - challenge_started) * 1000
        extraction = None
        extraction_ms = 0.0
        if request.extract_mode != "none" and self.extraction_enabled:
            extraction_started = time.perf_counter()
            extraction = await adapter.extract(session, mode=request.extract_mode, max_chars=100_000)
            extraction_ms = (time.perf_counter() - extraction_started) * 1000
        record: SessionRecord | None = None
        status = (
            "human_action_required"
            if challenge.human_action_required
            else "error"
            if challenge.kind != "none"
            else "ok"
        )
        artifacts: dict[str, str] = {}
        human_handoff = None
        if request.keep_open and adapter.interactive:
            record = await self.registry.create(
                owner=owner,
                handle=session,
                allowed_hosts=request.allowed_hosts,
                current_url=navigation.get("final_url"),
                correlation_id=request.correlation_id,
                idle_ttl_seconds=int(policy["session_idle_ttl_seconds"]),
                maximum_ttl_seconds=int(policy["session_maximum_ttl_seconds"]),
                max_sessions_per_owner=int(policy["max_sessions_per_owner"]),
                max_sessions_total=int(policy["max_sessions_total"]),
                artifact_retention_seconds=int(policy["artifact_retention_seconds"]),
            )
            record.challenge = challenge
            self._attach_engine_artifact_sink(session, owner=owner, session_id=record.session_id,
                                              destination=record.artifacts,
                                              retention_seconds=record.artifact_retention_seconds)
            self.telemetry.emit(
                "session_created",
                engine=adapter.name,
                session_id=record.session_id,
                correlation_id=request.correlation_id,
            )
            if challenge.human_action_required:
                artifact = await self._capture_handoff(adapter, record)
                if artifact:
                    artifacts["challenge_screenshot_id"] = artifact
                human_handoff = HumanHandoff(
                    session_id=record.session_id,
                    screenshot_artifact_id=artifact,
                    instructions=challenge.instructions or (
                        "Ask the operator to complete the visible verification, then navigate or extract again "
                        "with this same session_id."
                    ),
                    expires_at=record.idle_expires_at,
                )
        else:
            self._attach_engine_artifact_sink(
                session,
                owner=owner,
                session_id=f"request-{request.correlation_id or time.time_ns()}",
                destination=artifacts,
                retention_seconds=int(policy["artifact_retention_seconds"]),
            )
            await session.close()
        if challenge.human_action_required and record is None:
            status = "error"
        self.telemetry.emit(
            "navigation_completed",
            engine=adapter.name,
            attempt=attempt,
            challenge=challenge.kind,
            retained=bool(record),
            correlation_id=request.correlation_id,
        )
        if challenge.kind != "none":
            self.telemetry.emit(
                "challenge_detected",
                engine=adapter.name,
                challenge=challenge.kind,
                correlation_id=request.correlation_id,
            )
        self.telemetry.observe(
            "navigation_latency",
            navigation_ms,
            engine=adapter.name,
            correlation_id=request.correlation_id,
        )
        return BrowserResponse(
            status=status,
            requested_url=request.url,
            final_url=navigation.get("final_url"),
            title=navigation.get("title"),
            session_id=record.session_id if record else None,
            interactive=bool(record),
            engine=adapter.name,
            extraction=extraction,
            challenge=challenge,
            timings=BrowserTimings(
                total_ms=(time.perf_counter() - started) * 1000,
                navigation_ms=navigation_ms,
                extraction_ms=extraction_ms,
                challenge_ms=challenge_ms,
            ),
            artifacts=artifacts,
            human_handoff=human_handoff,
            correlation_id=request.correlation_id,
            message=(
                "Human verification requires keep_open=true so Agency can preserve a controllable session."
                if challenge.human_action_required and record is None else None
            ),
        )

    async def _capture_handoff(self, adapter: BrowserEngineAdapter, record: SessionRecord) -> str | None:
        try:
            result = await adapter.action(record.handle, ActionRequest(action="screenshot", full_page=False))
            data = base64.b64decode(result["screenshot_base64"])
            artifact = self.artifacts.put(
                data,
                owner=record.owner,
                session_id=record.session_id,
                suffix=".png",
                media_type="image/png",
                retention_seconds=record.artifact_retention_seconds,
            )
            record.artifacts["challenge_screenshot_id"] = artifact.artifact_id
            return artifact.artifact_id
        except Exception:
            return None

    async def _capture_failure_artifact(
            self,
            adapter: BrowserEngineAdapter,
            session: EngineSession,
            *,
            owner: OwnerClaims,
            correlation_id: str | None,
            retention_seconds: int,
    ) -> str | None:
        try:
            result = await adapter.action(session, ActionRequest(action="screenshot", full_page=False))
            data = base64.b64decode(result["screenshot_base64"])
            artifact = self.artifacts.put(
                data,
                owner=owner,
                session_id=f"failure-{correlation_id or time.time_ns()}",
                suffix=".png",
                media_type="image/png",
                retention_seconds=retention_seconds,
            )
            return artifact.artifact_id
        except Exception:
            return None

    def _capture_failure_html(
            self,
            html: str,
            *,
            owner: OwnerClaims,
            correlation_id: str | None,
            retention_seconds: int,
    ) -> str | None:
        try:
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup.find_all(["script", "style", "noscript"]):
                tag.decompose()
            for tag in soup.find_all(["input", "textarea", "select"]):
                if not isinstance(tag, Tag):
                    continue
                tag.attrs.pop("value", None)
                tag.string = "[REDACTED FORM VALUE]"
            sanitized = str(soup)[:20_000].encode("utf-8")
            artifact = self.artifacts.put(
                sanitized,
                owner=owner,
                session_id=f"failure-{correlation_id or time.time_ns()}",
                suffix=".html",
                media_type="text/html",
                retention_seconds=retention_seconds,
            )
            return artifact.artifact_id
        except Exception:
            return None

    def _attach_engine_artifact_sink(
            self,
            session: EngineSession,
            *,
            owner: OwnerClaims,
            session_id: str,
            destination: dict[str, str],
            retention_seconds: int,
    ) -> None:
        if not hasattr(session, "artifact_sink"):
            return

        def store_payloads(payloads: dict[str, bytes]) -> None:
            for name, data in payloads.items():
                suffix = Path(name).suffix or ".bin"
                media_type = "application/zip" if suffix == ".zip" else "video/webm" if suffix == ".webm" else "application/octet-stream"
                artifact = self.artifacts.put(
                    data,
                    owner=owner,
                    session_id=session_id,
                    suffix=suffix,
                    media_type=media_type,
                    retention_seconds=retention_seconds,
                )
                destination[f"engine_{name}"] = artifact.artifact_id

        setattr(session, "artifact_sink", store_payloads)

    @staticmethod
    def _safe_navigation_diagnostics(exc: Exception) -> dict[str, Any]:
        if not isinstance(exc, EngineNavigationError):
            return {}
        return {
            key: value
            for key, value in exc.diagnostics.items()
            if key in {"error_type", "final_url", "trace"}
        }

    async def _handoff_to_primary(
            self,
            request: OpenRequest,
            fallback_response: BrowserResponse,
            *,
            fallback_session: EngineSession,
            owner: OwnerClaims,
            started: float,
            policy: dict[str, int | float],
    ) -> BrowserResponse:
        fallback_state = getattr(fallback_session, "storage_state", None)
        handoff_options = request.options.model_copy(update={"storage_state": fallback_state or request.options.storage_state})
        session = await self.primary.start(
            options=handoff_options,
            proxy=self.proxy_resolver.resolve(
                request.options.proxy_binding,
                domain=urlsplit(fallback_response.final_url or request.url).hostname or "",
            ),
            allowed_hosts=request.allowed_hosts,
        )
        try:
            response = await self._navigate_and_build(
                self.primary,
                session,
                request.model_copy(update={"url": fallback_response.final_url or request.url, "keep_open": True}),
                owner=owner,
                started=started,
                attempt=1,
                policy=policy,
            )
            if response.challenge.kind != "none":
                if response.session_id:
                    await self.registry.close(owner=owner, session_id=response.session_id, status="handoff_failed")
                response.session_id = None
                response.interactive = False
                response.status = "error"
                response.message = "Scrapling content was retrieved, but browser state could not be handed off safely."
            else:
                storage_state_reader = getattr(self.primary, "storage_state", None)
                primary_state = await storage_state_reader(session) if storage_state_reader else None
                expected_cookies = {item.get("name") for item in (fallback_state or {}).get("cookies", [])}
                actual_cookies = {item.get("name") for item in (primary_state or {}).get("cookies", [])}
                expected_storage = self._storage_entries(fallback_state)
                actual_storage = self._storage_entries(primary_state)
                cookies_preserved = expected_cookies.issubset(actual_cookies)
                storage_preserved = expected_storage.issubset(actual_storage)
                state_preserved = cookies_preserved and storage_preserved
                response.diagnostics["handoff_state"] = {
                    "cookies_expected": len(expected_cookies),
                    "cookies_preserved": len(expected_cookies & actual_cookies),
                    "storage_entries_expected": len(expected_storage),
                    "storage_entries_preserved": len(expected_storage & actual_storage),
                    "storage_state_verified": state_preserved,
                    "proxy_binding_preserved": handoff_options.proxy_binding == request.options.proxy_binding,
                    "user_agent_preserved": handoff_options.user_agent == request.options.user_agent,
                }
                if not state_preserved:
                    if response.session_id:
                        await self.registry.close(owner=owner, session_id=response.session_id, status="handoff_state_mismatch")
                    response.session_id = None
                    response.interactive = False
                    response.status = "error"
                    response.message = "Scrapling state handoff failed cookie compatibility verification."
            return response
        except Exception:
            await session.close()
            raise

    def _effective_runtime_policy(self, request: OpenRequest) -> dict[str, int | float]:
        """Clamp agent preferences to the operator-owned resource envelope."""

        requested = request.runtime_policy
        maximum_ttl = min(
            requested.session_maximum_ttl_seconds or self.registry.maximum_ttl_seconds,
            self.registry.maximum_ttl_seconds,
        )
        idle_ttl = min(
            requested.session_idle_ttl_seconds or self.registry.idle_ttl_seconds,
            maximum_ttl,
        )
        return {
            "session_idle_ttl_seconds": idle_ttl,
            "session_maximum_ttl_seconds": maximum_ttl,
            "max_sessions_per_owner": min(
                requested.max_sessions_per_owner or self.registry.max_sessions_per_owner,
                self.registry.max_sessions_per_owner,
            ),
            "max_sessions_total": min(
                requested.max_sessions_total or self.registry.max_sessions_total,
                self.registry.max_sessions_total,
            ),
            "navigation_timeout_ms": min(
                requested.navigation_timeout_ms or self.navigation_timeout_ms,
                self.navigation_timeout_ms,
            ),
            "retry_attempts": min(requested.retry_attempts or self.retry_budget, self.retry_budget),
            "domain_max_concurrency": min(
                requested.domain_max_concurrency or self.domain_policy.max_concurrency,
                self.domain_policy.max_concurrency,
            ),
            "domain_min_interval_seconds": max(
                requested.domain_min_interval_seconds
                if requested.domain_min_interval_seconds is not None
                else self.domain_policy.minimum_interval_seconds,
                self.domain_policy.minimum_interval_seconds,
            ),
            "artifact_retention_seconds": min(
                requested.artifact_retention_seconds or self.artifacts.retention_seconds,
                self.artifacts.retention_seconds,
            ),
        }

    def _adapter_for(self, engine: str) -> BrowserEngineAdapter:
        if engine == self.primary.name:
            return self.primary
        if engine == self.fallback.name:
            return self.fallback
        raise EngineUnavailableError(f"Browser engine '{engine}' is unavailable")

    @staticmethod
    def _storage_entries(state: dict[str, Any] | None) -> set[tuple[str, str, str]]:
        entries: set[tuple[str, str, str]] = set()
        for origin in (state or {}).get("origins", []):
            origin_url = str(origin.get("origin") or "")
            for item in origin.get("localStorage", []):
                entries.add((origin_url, str(item.get("name") or ""), str(item.get("value") or "")))
        return entries

