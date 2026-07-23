"""Patchright primary engine with one isolated context per retained session."""

from __future__ import annotations

import asyncio
import base64
import os
import random
import re
import shutil
import tempfile
from importlib.metadata import PackageNotFoundError, version
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from ..challenges import classify_challenge
from ..contracts import ActionRequest, BrowserOptions, ChallengeResult, ExtractMode, ExtractionResult
from ..extraction import extract_document
from ..proxy import ResolvedProxy
from ..security import validate_public_url
from .base import EngineNavigationError, EngineUnavailableError


_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_PATCHRIGHT_INTERNAL_INIT_HOST = "patchright-init-script-inject.internal"


@dataclass(slots=True)
class PatchrightSession:
    engine: str
    playwright: Any
    browser: Any
    context: Any
    page: Any
    runtime_dir: Path
    trace_path: Path | None = None
    trace_mode: str = "off"
    last_status: int | None = None
    closed: bool = False
    crashed: bool = False
    on_close: Callable[[], None] | None = None
    on_crash: Callable[[], None] | None = None
    artifact_sink: Callable[[dict[str, bytes]], None] | None = None

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        cleanup_timeout = max(0.1, float(os.getenv("BROWSER_RESOURCE_CLEANUP_TIMEOUT_SECONDS", "5")))
        if self.trace_path and self.trace_mode == "on":
            try:
                trace_cleanup = self.context.tracing.stop(path=str(self.trace_path))
            except Exception:
                trace_cleanup = None
            if trace_cleanup is not None:
                await _bounded_result(trace_cleanup, timeout=cleanup_timeout)
        for resource in (self.context, self.browser, self.playwright):
            try:
                cleanup = resource.close() if resource is not self.playwright else resource.stop()
            except Exception:
                continue
            await _bounded_result(cleanup, timeout=cleanup_timeout)
        payloads: dict[str, bytes] = {}
        candidates = ([self.trace_path] if self.trace_path else []) + list((self.runtime_dir / "video").glob("**/*"))
        for path in candidates:
            if path and path.is_file() and path.stat().st_size <= 10 * 1024 * 1024:
                try:
                    payloads[path.name] = path.read_bytes()
                except OSError:
                    pass
        if payloads and self.artifact_sink:
            try:
                self.artifact_sink(payloads)
            except Exception:
                pass
        shutil.rmtree(self.runtime_dir, ignore_errors=True)
        if self.on_close:
            self.on_close()


class PatchrightAdapter:
    name = "patchright"
    interactive = True

    def __init__(self, *, runtime_root: str | Path | None = None) -> None:
        self.runtime_root = Path(runtime_root or os.getenv("BROWSER_RUNTIME_ROOT", "/tmp/agency-browser-runtime"))
        self.minimum_free_bytes = int(os.getenv("BROWSER_RUNTIME_MIN_FREE_BYTES", str(256 * 1024 * 1024)))
        self._active: set[int] = set()
        self.crash_count = 0
        self.ignore_https_errors = os.getenv("BROWSER_IGNORE_HTTPS_ERRORS", "false").lower() in {
            "1", "true", "yes",
        }

    async def start(
            self,
            *,
            options: BrowserOptions,
            proxy: ResolvedProxy | None,
            allowed_hosts: list[str],
    ) -> PatchrightSession:
        try:
            from patchright.async_api import async_playwright
        except ImportError as exc:
            raise EngineUnavailableError("Patchright is not installed in the browser runtime image") from exc
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.runtime_root).free < self.minimum_free_bytes:
            raise EngineUnavailableError("Browser runtime has insufficient free disk space")
        runtime_dir = Path(tempfile.mkdtemp(prefix="patchright-", dir=self.runtime_root))
        playwright = await async_playwright().start()
        browser = None
        context = None
        try:
            launch_args = [
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-component-update",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            browser = await playwright.chromium.launch(
                headless=options.headless,
                args=launch_args,
                proxy=proxy.playwright_options() if proxy else None,
            )
            context_kwargs: dict[str, Any] = {
                "locale": options.locale or "en-US",
                "timezone_id": options.timezone_id or "UTC",
                "viewport": {"width": options.viewport_width, "height": options.viewport_height},
                "device_scale_factor": options.device_scale_factor,
                "is_mobile": options.mobile,
                "has_touch": options.mobile,
                "extra_http_headers": _safe_headers(options.extra_http_headers),
                "accept_downloads": False,
                "ignore_https_errors": self.ignore_https_errors,
            }
            if options.user_agent:
                context_kwargs["user_agent"] = options.user_agent
            if options.storage_state:
                context_kwargs["storage_state"] = options.storage_state
            if options.http_credentials:
                context_kwargs["http_credentials"] = options.http_credentials
            if options.record_video:
                video_dir = runtime_dir / "video"
                video_dir.mkdir(parents=True, exist_ok=True)
                context_kwargs["record_video_dir"] = str(video_dir)
            context = await browser.new_context(**context_kwargs)

            async def enforce_route(route: Any, request: Any) -> None:
                await self._enforce_route(route, request, allowed_hosts=allowed_hosts)

            await context.route("**/*", enforce_route)
            page = await context.new_page()
            session = PatchrightSession(
                engine=self.name,
                playwright=playwright,
                browser=browser,
                context=context,
                page=page,
                runtime_dir=runtime_dir,
                trace_mode=options.trace_mode,
                trace_path=(runtime_dir / "trace.zip") if options.trace_mode != "off" else None,
            )
            browser.on("disconnected", lambda: _mark_crashed(session))

            async def adopt_popup(new_page: Any) -> None:
                if new_page is session.page:
                    return
                previous = session.page
                session.page = new_page
                self._protect_downloads(new_page)
                try:
                    await previous.close()
                except Exception:
                    pass

            context.on("page", lambda new_page: asyncio.create_task(adopt_popup(new_page)))
            self._protect_downloads(page)
            if options.trace_mode != "off":
                await context.tracing.start(screenshots=True, snapshots=True, sources=False)
            self._active.add(id(session))
            session.on_close = lambda: self._active.discard(id(session))
            session.on_crash = self._record_crash
            return session
        except Exception:
            for resource in (context, browser, playwright):
                if resource is None:
                    continue
                try:
                    await resource.close() if resource is not playwright else await resource.stop()
                except Exception:
                    pass
            shutil.rmtree(runtime_dir, ignore_errors=True)
            raise

    async def navigate(self, session: PatchrightSession, url: str, *, timeout_ms: int) -> dict[str, Any]:
        try:
            goto_task = asyncio.create_task(
                session.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            )
            done, _ = await asyncio.wait({goto_task}, timeout=max(0.1, timeout_ms / 1_000))
            if not done:
                goto_task.cancel()
                # Patchright can delay cancellation while a socket remains
                # open. Consume the eventual result without extending Agency's
                # explicit wall-clock navigation budget.
                goto_task.add_done_callback(_consume_task_result)
                raise TimeoutError(f"Browser navigation exceeded {timeout_ms} ms")
            response = goto_task.result()
            session.last_status = response.status if response else None
            await self._bounded_human_dwell(session.page)
            return {
                "requested_url": url,
                "final_url": session.page.url,
                "title": await session.page.title(),
                "http_status": session.last_status,
            }
        except Exception as exc:
            diagnostics: dict[str, Any] = {"error_type": type(exc).__name__, "final_url": session.page.url}
            content = await _bounded_result(session.page.content(), timeout=1.0)
            if isinstance(content, str):
                diagnostics["html_excerpt"] = content[:2_000]
            if session.trace_path and session.trace_mode == "retain-on-failure":
                stopped = await _bounded_result(
                    session.context.tracing.stop(path=str(session.trace_path)),
                    timeout=2.0,
                )
                if stopped is not _BOUNDED_FAILURE:
                    diagnostics["trace"] = "trace.zip"
            raise EngineNavigationError(str(exc), diagnostics=diagnostics) from exc

    async def extract(
            self,
            session: PatchrightSession,
            *,
            mode: ExtractMode,
            max_chars: int,
    ) -> ExtractionResult:
        return extract_document(
            await session.page.content(),
            final_url=session.page.url,
            mode=mode,
            max_chars=max_chars,
        )

    async def challenge(self, session: PatchrightSession, *, http_status: int | None = None) -> ChallengeResult:
        title, body, html = await asyncio.gather(
            session.page.title(),
            session.page.locator("body").inner_text(timeout=5_000),
            session.page.content(),
            return_exceptions=True,
        )
        return classify_challenge(
            title=title if isinstance(title, str) else "",
            body_text=body if isinstance(body, str) else "",
            html=html if isinstance(html, str) else "",
            final_url=session.page.url,
            http_status=http_status if http_status is not None else session.last_status,
            engine=self.name,
        )

    async def recover_challenge(self, session: PatchrightSession, *, budget_ms: int = 8_000) -> bool:
        """Attempt only a visible checkbox; visual puzzles remain a human handoff."""

        deadline = asyncio.get_running_loop().time() + min(max(budget_ms, 1_000), 10_000) / 1_000
        candidates = [session.page.get_by_role("checkbox")]
        for frame in session.page.frames:
            candidates.append(frame.get_by_role("checkbox"))
        for candidate in candidates:
            if asyncio.get_running_loop().time() >= deadline:
                break
            try:
                if await candidate.count() and await candidate.first.is_visible():
                    await candidate.first.click(timeout=2_000)
                    remaining_ms = max(0, min(3_000, int((deadline - asyncio.get_running_loop().time()) * 1_000)))
                    if remaining_ms:
                        await session.page.wait_for_timeout(remaining_ms)
                    return True
            except Exception:
                continue
        return False

    async def action(self, session: PatchrightSession, request: ActionRequest) -> dict[str, Any]:
        page = session.page
        if request.action == "screenshot":
            data = await page.screenshot(type="png", full_page=request.full_page)
            return {"screenshot_base64": base64.b64encode(data).decode("ascii"), "final_url": page.url}
        if request.action == "scroll":
            match = re.search(r"(up|down)\s*(\d+)?", (request.scroll_direction or "down 1").lower())
            direction = match.group(1) if match else "down"
            times = min(20, int(match.group(2) or 1) if match else 1)
            for _ in range(times):
                await page.mouse.wheel(0, -800 if direction == "up" else 800)
                await page.wait_for_timeout(random.randint(160, 360))
            return {"message": f"Scrolled {direction} {times} time(s).", "final_url": page.url}
        if request.action == "verify":
            text = await page.locator("body").inner_text(timeout=5_000)
            expected = request.instruction or request.value or ""
            found = expected.lower() in text.lower()
            return {"found": found, "score": 100 if found else 0, "final_url": page.url}
        if request.action == "mouse_click":
            if request.x is None or request.y is None:
                raise ValueError("Manual mouse click requires both x and y coordinates")
            await page.mouse.click(request.x, request.y)
            await page.wait_for_timeout(random.randint(120, 300))
            return {"message": "Coordinates clicked.", "final_url": page.url}
        if request.action == "key_press":
            if not request.key:
                raise ValueError("Manual key press requires a key value")
            await page.keyboard.press(request.key)
            return {"message": "Key pressed.", "final_url": page.url}
        locator = self._locator(page, request)
        if await locator.count() == 0:
            raise ValueError("No visible element matched the browser action")
        locator = locator.first
        await locator.scroll_into_view_if_needed()
        if request.action == "click":
            target = (await locator.get_attribute("target") or "").lower()
            onclick = (await locator.get_attribute("onclick") or "").lower()
            may_open_popup = target == "_blank" or "window.open" in onclick
            await locator.click(timeout=5_000)
            if may_open_popup:
                # The context event adopts the popup asynchronously. Wait for
                # that ownership transfer so the next tool never receives the
                # just-closed opener page during the handoff race.
                deadline = asyncio.get_running_loop().time() + 2.0
                while session.page is page and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.025)
            return {"message": "Element clicked.", "final_url": session.page.url}
        if request.action == "select":
            await locator.select_option(label=request.value or _quoted_value(request.instruction or ""))
            return {"message": "Option selected.", "final_url": page.url}
        if request.action == "type":
            value = request.value if request.value is not None else _quoted_value(request.instruction or "")
            await locator.fill("")
            await locator.type(value, delay=random.randint(35, 95))
            return {"message": "Text entered.", "final_url": page.url}
        raise ValueError(f"Unsupported browser action '{request.action}'")

    async def health(self) -> dict[str, Any]:
        executable_path = None
        try:
            from patchright.async_api import async_playwright
            installed_version = version("patchright")
            if installed_version != "1.56.0":
                raise RuntimeError(f"unsupported Patchright version {installed_version}")
            playwright = await async_playwright().start()
            try:
                executable_path = playwright.chromium.executable_path
                if not executable_path or not Path(executable_path).exists():
                    raise RuntimeError("Patchright Chromium executable is missing")
            finally:
                await playwright.stop()
            available, reason = True, None
        except (ImportError, PackageNotFoundError, RuntimeError) as exc:
            available = False
            reason = str(exc)
        return {
            "available": available,
            "enabled": True,
            "version": "1.56.0",
            "reason": reason,
            "chromium_executable": bool(executable_path),
            "active": len(self._active),
            "crashes": self.crash_count,
            "ignore_https_errors": self.ignore_https_errors,
        }

    async def shutdown(self) -> None:
        self._active.clear()

    def _record_crash(self) -> None:
        self.crash_count += 1

    @staticmethod
    async def storage_state(session: PatchrightSession) -> dict[str, Any]:
        return await session.context.storage_state()

    @staticmethod
    async def _bounded_human_dwell(page: Any) -> None:
        await page.wait_for_timeout(random.randint(80, 260))
        try:
            await page.mouse.move(random.randint(100, 500), random.randint(100, 400), steps=random.randint(3, 8))
        except Exception:
            pass

    @staticmethod
    def _locator(page: Any, request: ActionRequest) -> Any:
        if request.sequence_number is not None:
            return page.locator(f'[data-element-id="{request.sequence_number}"]')
        hint = _quoted_value(request.instruction or "") or (request.instruction or "")
        if request.action == "select":
            return page.get_by_label(re.compile(re.escape(hint), re.IGNORECASE)).or_(page.locator("select"))
        if request.action == "type":
            return page.get_by_label(re.compile(re.escape(hint), re.IGNORECASE)).or_(
                page.locator("input, textarea, [contenteditable=true]")
            )
        return page.get_by_role("button", name=re.compile(re.escape(hint), re.IGNORECASE)).or_(
            page.get_by_role("link", name=re.compile(re.escape(hint), re.IGNORECASE))
        ).or_(page.get_by_text(re.compile(re.escape(hint), re.IGNORECASE)))

    @staticmethod
    def _protect_downloads(page: Any) -> None:
        # Downloads remain disabled until an Agency malware-scanning sink is
        # configured; silently writing attacker-controlled files is unsafe.
        page.on("download", lambda download: asyncio.create_task(download.cancel()))

    async def _enforce_route(self, route: Any, request: Any, *, allowed_hosts: list[str]) -> None:
        # Patchright uses this synthetic, non-network URL to inject its init
        # script when tracing is active. Blocking it aborts the real top-level
        # navigation; the explicit open URL still passes public-DNS validation.
        parsed = urlsplit(request.url)
        if (
            parsed.hostname == _PATCHRIGHT_INTERNAL_INIT_HOST
            and parsed.scheme in {"http", "https"}
            and parsed.path in {"", "/"}
            and not parsed.username
            and not parsed.password
        ) or request.url.startswith(("data:", "blob:", "about:")):
            await route.continue_()
            return
        try:
            # Context routing covers initial navigation, redirects, frames,
            # popups, and subresources; DNS is resolved again on every call.
            await asyncio.to_thread(validate_public_url, request.url, allowed_hosts)
        except ValueError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()


def _quoted_value(value: str) -> str:
    match = re.findall(r"['\"]([^'\"]+)['\"]", value)
    return match[-1] if match else value.strip()


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    forbidden = {"authorization", "cookie", "proxy-authorization", "host", "content-length"}
    return {key: value for key, value in headers.items() if key.lower() not in forbidden}


def _mark_crashed(session: PatchrightSession) -> None:
    if session.closed:
        return
    # Keep ``closed`` false so the registry's crash sweep still invokes the
    # normal close path and removes traces, profiles, and Playwright resources.
    session.crashed = True
    if session.on_crash:
        session.on_crash()
    if session.on_close:
        session.on_close()


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


_BOUNDED_FAILURE = object()


async def _bounded_result(awaitable: Any, *, timeout: float) -> Any:
    task = asyncio.create_task(awaitable)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        task.cancel()
        task.add_done_callback(_consume_task_result)
        return _BOUNDED_FAILURE
    try:
        return task.result()
    except (asyncio.CancelledError, Exception):
        return _BOUNDED_FAILURE

