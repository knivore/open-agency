"""Pinned Scrapling 0.4 fallback isolated from its compatibility-sensitive API."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..challenges import classify_challenge
from ..contracts import ActionRequest, BrowserOptions, ChallengeResult, ExtractMode, ExtractionResult
from ..extraction import extract_document
from ..proxy import ProxyResolver, ResolvedProxy
from .base import EngineNavigationError, EngineUnavailableError


@dataclass(slots=True)
class ScraplingSession:
    engine: str
    context_manager: Any
    client: Any
    runtime_dir: Path
    page: Any = None
    html: str = ""
    final_url: str = ""
    status: int | None = None
    headers: dict[str, str] | None = None
    storage_state: dict[str, Any] | None = None
    closed: bool = False

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.context_manager.__aexit__(None, None, None)
        finally:
            shutil.rmtree(self.runtime_dir, ignore_errors=True)


class ScraplingAdapter:
    name = "scrapling"
    interactive = False

    def __init__(self, *, runtime_root: str | Path | None = None, enabled: bool | None = None) -> None:
        self.runtime_root = Path(runtime_root or os.getenv("BROWSER_RUNTIME_ROOT", "/tmp/agency-browser-runtime"))
        self.enabled = (
            os.getenv("BROWSER_SCRAPLING_ENABLED", "true").lower() in {"1", "true", "yes"}
            if enabled is None else enabled
        )

    async def start(
            self,
            *,
            options: BrowserOptions,
            proxy: ResolvedProxy | None,
            allowed_hosts: list[str],
    ) -> ScraplingSession:
        if not self.enabled:
            raise EngineUnavailableError("Scrapling fallback is disabled")
        try:
            from scrapling.fetchers import AsyncStealthySession
        except ImportError as exc:
            raise EngineUnavailableError("Scrapling 0.4 is not installed in the browser runtime image") from exc
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        runtime_dir = Path(tempfile.mkdtemp(prefix="scrapling-", dir=self.runtime_root))
        kwargs: dict[str, Any] = {
            "headless": options.headless,
            "block_webrtc": True,
            "hide_canvas": True,
            "solve_cloudflare": True,
            "allow_webgl": True,
            "disable_resources": False,
            "timeout": int(os.getenv("BROWSER_SCRAPLING_TIMEOUT_MS", "90000")),
            "max_pages": 1,
            "retries": 1,
            "retry_delay": 1.0,
            "user_data_dir": str(runtime_dir / "profile"),
            "locale": options.locale or "en-US",
            "timezone_id": options.timezone_id or "UTC",
            "proxy": ProxyResolver.authenticated_url(proxy) if proxy else None,
        }
        manager = AsyncStealthySession(**kwargs)
        try:
            client = await manager.__aenter__()
        except Exception:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise
        return ScraplingSession(engine=self.name, context_manager=manager, client=client, runtime_dir=runtime_dir)

    async def navigate(self, session: ScraplingSession, url: str, *, timeout_ms: int) -> dict[str, Any]:
        fetch_task = asyncio.create_task(session.client.fetch(
            url,
            solve_cloudflare=True,
            disable_resources=False,
            timeout=timeout_ms,
            wait=1_500,
            network_idle=False,
            retries=1,
            retry_delay=1.0,
        ))
        done, _ = await asyncio.wait({fetch_task}, timeout=max(0.1, timeout_ms / 1_000))
        if not done:
            fetch_task.cancel()
            # A compatibility-layer solver can ignore cancellation briefly;
            # consume its eventual result without extending Agency's budget.
            fetch_task.add_done_callback(_consume_task_result)
            raise EngineNavigationError(f"Scrapling navigation exceeded {timeout_ms} ms")
        response = fetch_task.result()
        body = getattr(response, "body", b"")
        if isinstance(body, bytes):
            session.html = body.decode(getattr(response, "encoding", None) or "utf-8", errors="replace")
        else:
            session.html = str(body or getattr(response, "html_content", "") or "")
        session.final_url = str(getattr(response, "url", None) or url)
        session.status = getattr(response, "status", None) or getattr(response, "status_code", None)
        raw_headers = getattr(response, "headers", None) or {}
        try:
            session.headers = {str(key): str(value) for key, value in raw_headers.items()}
        except AttributeError:
            session.headers = {}
        session.storage_state = await self._storage_state(session.client, response)
        extracted = extract_document(session.html, final_url=session.final_url, mode="text", max_chars=2_000)
        return {
            "requested_url": url,
            "final_url": session.final_url,
            "title": extracted.title,
            "http_status": session.status,
        }

    async def extract(
            self,
            session: ScraplingSession,
            *,
            mode: ExtractMode,
            max_chars: int,
    ) -> ExtractionResult:
        return extract_document(session.html, final_url=session.final_url, mode=mode, max_chars=max_chars)

    async def challenge(self, session: ScraplingSession, *, http_status: int | None = None) -> ChallengeResult:
        extracted = extract_document(session.html, final_url=session.final_url, mode="text", max_chars=20_000)
        return classify_challenge(
            title=extracted.title or "",
            body_text=extracted.text or "",
            html=session.html,
            final_url=session.final_url,
            http_status=http_status if http_status is not None else session.status,
            engine=self.name,
            response_headers=session.headers,
        )

    async def action(self, session: ScraplingSession, request: ActionRequest) -> dict[str, Any]:
        raise EngineUnavailableError("Scrapling sessionless fallback does not expose interactive actions")

    async def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"available": False, "enabled": False, "version": "0.4", "reason": "kill switch disabled"}
        try:
            fetchers = import_module("scrapling.fetchers")
            if not hasattr(fetchers, "AsyncStealthySession"):
                raise ImportError("Scrapling AsyncStealthySession is unavailable")
            installed_version = version("scrapling")
            if installed_version != "0.4":
                raise RuntimeError(f"unsupported Scrapling version {installed_version}")
            available, reason = True, None
        except (ImportError, PackageNotFoundError, RuntimeError) as exc:
            available, reason = False, str(exc)
        return {"available": available, "enabled": True, "version": "0.4", "reason": reason}

    async def shutdown(self) -> None:
        return None

    @staticmethod
    async def _storage_state(client: Any, response: Any) -> dict[str, Any] | None:
        """Confine Scrapling 0.4 private-context compatibility to one method."""

        for candidate in (
            getattr(client, "context", None),
            getattr(client, "_context", None),
            getattr(getattr(client, "browser", None), "contexts", [None])[0]
            if getattr(getattr(client, "browser", None), "contexts", None) else None,
        ):
            storage_state = getattr(candidate, "storage_state", None)
            if callable(storage_state):
                try:
                    value = storage_state()
                    return await value if hasattr(value, "__await__") else value
                except Exception:
                    continue
        cookies = getattr(response, "cookies", None)
        if isinstance(cookies, dict):
            hostname = urlsplit(str(getattr(response, "url", "") or "")).hostname or ""
            return {
                "cookies": [
                    {"name": str(name), "value": str(value), "domain": hostname, "path": "/"}
                    for name, value in cookies.items()
                    if hostname
                ],
                "origins": [],
            }
        return None


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass
