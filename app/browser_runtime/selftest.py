"""Deterministic Patchright integration scenarios for the browser-runtime image.

Run with ``python -m app.browser_runtime.selftest`` inside the dedicated image.
The temporary loopback server is reachable only by this process; production URL
validation is replaced for the duration of the test and restored in ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .adapters.base import EngineNavigationError
from .adapters.patchright import PatchrightAdapter
from .contracts import ActionRequest, BrowserOptions


class _FixtureServer:
    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[str] = []

    async def __aenter__(self) -> "_FixtureServer":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("latin-1", errors="replace")
            path = request_line.split(" ", 2)[1] if " " in request_line else "/"
            self.requests.append(path)
            while await reader.readline() not in {b"\r\n", b"\n", b""}:
                pass
            if path == "/redirect":
                await self._respond(writer, b"", status="302 Found", headers={"Location": "/normal"})
            elif path == "/rendered":
                await self._respond(writer, b"""
                    <html><head><title>Rendered</title></head><body>
                    <main id="content">Waiting</main>
                    <script>setTimeout(() => content.textContent = 'Rendered by JavaScript', 40)</script>
                    </body></html>
                """)
            elif path == "/popup":
                await self._respond(
                    writer,
                    b'<html><body><a href="/second" target="_blank">Open second</a></body></html>',
                )
            elif path == "/download":
                await self._respond(
                    writer,
                    b'<html><body><a href="/file" download="payload.txt">Download payload</a></body></html>',
                )
            elif path == "/checkbox-challenge":
                await self._respond(
                    writer,
                    b"""
                    <html><head><title>Verify you are human</title></head><body>
                    <div class="cf-turnstile">
                      <label><input type="checkbox" aria-label="Verify you are human"
                        onchange="document.title='Recovered'; document.body.innerHTML='<main>Checkbox Recovered</main>'">
                        Verify you are human
                      </label>
                    </div>
                    </body></html>
                    """,
                )
            elif path == "/visual-challenge":
                await self._respond(
                    writer,
                    b"""
                    <html><head><title>Verify you are human</title></head><body>
                    <div class="captcha-container">Human verification is required.</div>
                    <button onclick="document.title='Recovered'; document.body.innerHTML='<main>Human Recovered</main>'">
                      Complete verification
                    </button>
                    </body></html>
                    """,
                )
            elif path == "/file":
                await self._respond(
                    writer,
                    b"untrusted download payload",
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Disposition": 'attachment; filename="payload.txt"',
                    },
                )
            elif path == "/infinite":
                # A delayed first byte deterministically models a target that
                # never becomes ready within the Agency navigation budget.
                await asyncio.sleep(2)
                await self._respond(
                    writer,
                    b"<html><head><title>Too Late</title></head><body>late response</body></html>",
                )
            else:
                label = "Second Page" if path == "/second" else "Normal Page"
                await self._respond(
                    writer,
                    f"<html><head><title>{label}</title></head><body><main>{label}</main></body></html>".encode(),
                )
        except (ConnectionError, asyncio.CancelledError):
            writer.close()

    @staticmethod
    async def _respond(
            writer: asyncio.StreamWriter,
            body: bytes,
            *,
            status: str = "200 OK",
            headers: dict[str, str] | None = None,
    ) -> None:
        response_headers = {
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(body)),
            "Connection": "close",
            **(headers or {}),
        }
        head = f"HTTP/1.1 {status}\r\n" + "".join(f"{key}: {value}\r\n" for key, value in response_headers.items())
        writer.write(head.encode("latin-1") + b"\r\n" + body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()


class _FixturePatchrightAdapter(PatchrightAdapter):
    """Permit only this process's ephemeral fixture listener."""

    def __init__(self, *, runtime_root: Path, fixture_port: int) -> None:
        super().__init__(runtime_root=runtime_root)
        self.fixture_port = fixture_port

    async def _enforce_route(self, route: Any, request: Any, *, allowed_hosts: list[str]) -> None:
        parsed = urlsplit(request.url)
        if parsed.hostname == "127.0.0.1" and parsed.port == self.fixture_port:
            await route.continue_()
            return
        await super()._enforce_route(route, request, allowed_hosts=allowed_hosts)


async def run_selftest() -> dict[str, Any]:
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as temp_dir:
        async with _FixtureServer() as fixtures:
            return await _run_scenarios(fixtures, Path(temp_dir), results)


async def _run_scenarios(
        fixtures: _FixtureServer,
        temp_dir: Path,
        results: dict[str, Any],
) -> dict[str, Any]:
        # The subclass is test-only scaffolding. Shipping a production flag
        # that permits private destinations would weaken the SSRF boundary.
        adapter = _FixturePatchrightAdapter(
            runtime_root=temp_dir / "engines",
            fixture_port=fixtures.port,
        )
        session = await adapter.start(
            options=BrowserOptions(trace_mode="on"),
            proxy=None,
            allowed_hosts=["127.0.0.1"],
        )
        try:
            normal = await adapter.navigate(session, fixtures.base_url + "/normal", timeout_ms=3_000)
            extracted = await adapter.extract(session, mode="text", max_chars=10_000)
            assert normal["title"] == "Normal Page" and "Normal Page" in (extracted.text or "")
            _passed(results, "normal_html")

            rendered = await adapter.navigate(session, fixtures.base_url + "/rendered", timeout_ms=3_000)
            await session.page.wait_for_timeout(100)
            extracted = await adapter.extract(session, mode="text", max_chars=10_000)
            assert rendered["title"] == "Rendered" and "Rendered by JavaScript" in (extracted.text or "")
            _passed(results, "javascript_rendered")

            redirected = await adapter.navigate(session, fixtures.base_url + "/redirect", timeout_ms=3_000)
            assert redirected["final_url"].endswith("/normal")
            _passed(results, "redirect")

            await adapter.navigate(session, fixtures.base_url + "/popup", timeout_ms=3_000)
            await adapter.action(session, ActionRequest(action="click", instruction="Open second"))
            await session.page.wait_for_timeout(100)
            assert session.page.url.endswith("/second") and await session.page.title() == "Second Page"
            _passed(results, "popup_adoption")

            await adapter.navigate(session, fixtures.base_url + "/normal", timeout_ms=3_000)
            await adapter.navigate(session, fixtures.base_url + "/second", timeout_ms=3_000)
            assert session.page.url.endswith("/second")
            _passed(results, "multi_page_session")

            await adapter.navigate(session, fixtures.base_url + "/checkbox-challenge", timeout_ms=3_000)
            checkbox_challenge = await adapter.challenge(session)
            assert checkbox_challenge.kind == "turnstile" and checkbox_challenge.human_action_required
            assert await adapter.recover_challenge(session, budget_ms=1_000)
            cleared_checkbox = await adapter.challenge(session)
            checkbox_content = await adapter.extract(session, mode="text", max_chars=10_000)
            assert cleared_checkbox.kind == "none" and "Checkbox Recovered" in (checkbox_content.text or "")
            _passed(results, "automatic_checkbox_challenge")

            await adapter.navigate(session, fixtures.base_url + "/visual-challenge", timeout_ms=3_000)
            visual_challenge = await adapter.challenge(session)
            assert visual_challenge.kind == "visual_captcha" and visual_challenge.human_action_required
            original_page = session.page
            await adapter.action(session, ActionRequest(action="click", instruction="Complete verification"))
            cleared_visual = await adapter.challenge(session)
            human_content = await adapter.extract(session, mode="text", max_chars=10_000)
            assert session.page is original_page
            assert cleared_visual.kind == "none" and "Human Recovered" in (human_content.text or "")
            _passed(results, "same_session_human_challenge")

            await adapter.navigate(session, fixtures.base_url + "/download", timeout_ms=3_000)
            try:
                await adapter.action(session, ActionRequest(action="click", instruction="Download payload"))
            except Exception:
                # Chromium may surface the policy cancellation to the click;
                # either outcome is acceptable so long as no file is retained.
                pass
            await session.page.wait_for_timeout(100)
            retained_downloads = [
                path for path in session.runtime_dir.rglob("*")
                if path.is_file() and path.name == "payload.txt"
            ]
            assert not retained_downloads
            _passed(results, "download_blocked")

            try:
                await adapter.navigate(session, fixtures.base_url + "/infinite", timeout_ms=300)
            except EngineNavigationError as exc:
                assert exc.diagnostics.get("error_type") == "TimeoutError"
                assert "exceeded 300 ms" in str(exc)
                _passed(results, "infinite_timeout")
            else:
                raise AssertionError("Infinite response unexpectedly completed navigation")
        finally:
            await session.close()

        async def concurrent_session(index: int) -> str:
            handle = await adapter.start(
                options=BrowserOptions(),
                proxy=None,
                allowed_hosts=["127.0.0.1"],
            )
            try:
                navigation = await adapter.navigate(
                    handle,
                    fixtures.base_url + f"/normal?session={index}",
                    timeout_ms=3_000,
                )
                extraction = await adapter.extract(handle, mode="text", max_chars=10_000)
                assert navigation["title"] == "Normal Page" and "Normal Page" in (extraction.text or "")
                return navigation["final_url"]
            finally:
                await handle.close()

        concurrent_urls = await asyncio.gather(*(concurrent_session(index) for index in range(3)))
        assert len(set(concurrent_urls)) == 3
        _passed(results, "concurrent_sessions")
        assert not any(temp_dir.joinpath("engines").glob("patchright-*"))
        _passed(results, "profile_cleanup")
        results["requests"] = fixtures.requests
        return results


def _passed(results: dict[str, Any], scenario: str) -> None:
    results[scenario] = "ok"
    print(f"[browser-selftest] {scenario}: ok", file=sys.stderr, flush=True)


def main() -> int:
    print(json.dumps(asyncio.run(run_selftest()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

