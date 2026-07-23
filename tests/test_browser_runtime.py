from __future__ import annotations

import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.browser_runtime.challenges import classify_challenge
from app.browser_runtime.artifacts import ArtifactStore
from app.browser_runtime.adapters.patchright import PatchrightAdapter, PatchrightSession, _safe_headers
from app.browser_runtime.adapters.base import EngineNavigationError, EngineUnavailableError
from app.browser_runtime.client import BrowserRuntimeClient
from app.browser_runtime.contracts import (
    ActionRequest,
    BrowserOptions,
    BrowserResponse,
    BrowserRuntimePolicy,
    ChallengeResult,
    ExtractRequest,
    ExtractionResult,
    HealthResult,
    OpenRequest,
    OwnerClaims,
)
from app.browser_runtime.extraction import extract_document
from app.browser_runtime.domain_policy import DomainPolicyStore
from app.browser_runtime.events import process_tree_resources, redact
from app.browser_runtime.proxy import ProxyConfigurationError, ProxyResolver
from app.browser_runtime.security import (
    BrowserCapabilityError,
    derive_execution_secret,
    issue_capability,
    validate_public_url,
    verify_capability,
)
from app.browser_runtime.server import create_app
from app.browser_runtime.service import BrowserRuntimeService
from app.browser_runtime.session_registry import AsyncSessionRegistry, SessionAccessError, SessionNotFoundError
from scripts.browser_live_check import run as run_browser_live_check
from scripts.browser_rollout_check import SCHEMA as ROLLOUT_SCHEMA
from scripts.browser_rollout_check import capture as capture_browser_rollout
from scripts.browser_rollout_check import compare_records as compare_browser_rollouts
from scripts.browser_rollout_check import merge_records as merge_browser_rollouts


class BrowserCapabilityTests(unittest.TestCase):
    def test_execution_derived_capability_is_scoped_and_tamper_evident(self):
        master = "master-secret-with-at-least-32-characters"
        execution_secret = derive_execution_secret(master, "execution-1")
        token = issue_capability(
            execution_secret,
            owner=OwnerClaims(execution_id="execution-1"),
            operations=["open"],
            allowed_hosts=["example.com"],
            now=100,
        )

        claims = verify_capability(execution_secret, token, operation="open", now=100)

        self.assertEqual(claims.owner.execution_id, "execution-1")
        self.assertEqual(claims.allowed_hosts, ("example.com",))
        with self.assertRaises(BrowserCapabilityError):
            verify_capability(derive_execution_secret(master, "execution-2"), token, operation="open", now=100)

    @patch("app.browser_runtime.security.socket.getaddrinfo")
    def test_public_url_policy_rejects_private_dns_answers(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("169.254.169.254", 443))]
        with self.assertRaisesRegex(ValueError, "non-public"):
            validate_public_url("https://example.com/path", ["example.com"])

    @patch("app.browser_runtime.security.socket.getaddrinfo")
    def test_public_url_policy_requires_request_scoped_host_grant(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with self.assertRaisesRegex(ValueError, "not approved"):
            validate_public_url("https://example.com/path", ["other.example"])

    def test_mobile_fingerprint_requires_consistent_viewport_and_user_agent(self):
        options = BrowserOptions(mobile=True)
        self.assertEqual((options.viewport_width, options.viewport_height), (390, 844))
        with self.assertRaisesRegex(ValueError, "mobile user agent"):
            BrowserOptions(user_agent="Example Mobile Browser", mobile=False)

    def test_sensitive_headers_and_values_are_not_observable(self):
        self.assertEqual(
            _safe_headers({"x-safe": "yes", "Authorization": "Bearer secret", "Cookie": "session=secret"}),
            {"x-safe": "yes"},
        )
        result = redact({"proxy_url": "http://user:pass@proxy.example:8080", "message": "Bearer token-value"})
        self.assertEqual(result["proxy_url"], "[REDACTED]")
        self.assertNotIn("token-value", result["message"])

    def test_browser_runtime_policy_rejects_inconsistent_limits(self):
        with self.assertRaisesRegex(ValueError, "idle TTL"):
            BrowserRuntimePolicy(
                session_idle_ttl_seconds=120,
                session_maximum_ttl_seconds=60,
            )
        with self.assertRaisesRegex(ValueError, "Per-owner"):
            BrowserRuntimePolicy(max_sessions_per_owner=3, max_sessions_total=2)


class BrowserRoutePolicyTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.browser_runtime.adapters.patchright.validate_public_url")
    async def test_route_revalidates_redirect_frame_popup_and_subresource_destinations(self, validate):
        route = unittest.mock.AsyncMock()
        request = type("Request", (), {"url": "https://blocked.example/resource"})()
        validate.side_effect = ValueError("not approved")
        await PatchrightAdapter()._enforce_route(route, request, allowed_hosts=["example.com"])
        route.abort.assert_awaited_once_with("blockedbyclient")
        route.continue_.assert_not_awaited()

    @patch("app.browser_runtime.adapters.patchright.validate_public_url")
    async def test_route_allows_patchright_synthetic_init_request_for_http_and_https(self, validate):
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                route = unittest.mock.AsyncMock()
                request = type(
                    "Request",
                    (),
                    {"url": f"{scheme}://patchright-init-script-inject.internal/"},
                )()

                await PatchrightAdapter()._enforce_route(route, request, allowed_hosts=["example.com"])

                route.continue_.assert_awaited_once()
        validate.assert_not_called()

    async def test_navigation_has_agency_owned_wall_clock_timeout(self):
        async def never_finishes(*_args, **_kwargs):
            await asyncio.Future()

        page = unittest.mock.MagicMock()
        page.url = "https://example.com/infinite"
        page.goto = unittest.mock.AsyncMock(side_effect=never_finishes)
        page.content = unittest.mock.AsyncMock(return_value="<html><body>partial</body></html>")
        with tempfile.TemporaryDirectory() as temp_dir:
            session = PatchrightSession(
                engine="patchright",
                playwright=unittest.mock.MagicMock(),
                browser=unittest.mock.MagicMock(),
                context=unittest.mock.MagicMock(),
                page=page,
                runtime_dir=Path(temp_dir),
            )

            with self.assertRaisesRegex(EngineNavigationError, "exceeded 50 ms"):
                await asyncio.wait_for(
                    PatchrightAdapter().navigate(session, "https://example.com/infinite", timeout_ms=50),
                    timeout=0.5,
                )


class BrowserArtifactTests(unittest.TestCase):
    def test_artifact_access_is_owner_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ArtifactStore(tmp)
            owner = OwnerClaims(execution_id="execution-1")
            record = store.put(b"png", owner=owner, session_id="brs_test", suffix=".png", media_type="image/png")
            self.assertEqual(store.get(record.artifact_id, owner=owner).path.read_bytes(), b"png")
            with self.assertRaises(FileNotFoundError):
                store.get(record.artifact_id, owner=OwnerClaims(execution_id="execution-2"))

    def test_artifact_can_request_shorter_retention_than_operator_limit(self):
        with tempfile.TemporaryDirectory() as tmp, patch("app.browser_runtime.artifacts.time.time") as now:
            now.return_value = 100.0
            store = ArtifactStore(tmp)
            owner = OwnerClaims(execution_id="execution-1")
            record = store.put(
                b"png",
                owner=owner,
                session_id="brs_test",
                suffix=".png",
                media_type="image/png",
                retention_seconds=60,
            )
            now.return_value = 161.0

            self.assertEqual(store.prune(), 1)
            self.assertFalse(record.path.exists())


class BrowserChallengeTests(unittest.TestCase):
    def test_article_with_incidental_challenge_words_is_not_false_positive(self):
        result = classify_challenge(
            title="How Cloudflare handles CAPTCHA accessibility",
            body_text="This article discusses Cloudflare and captcha design in ordinary prose.",
            html="<article><p>Cloudflare and captcha are discussed here.</p></article>",
            http_status=200,
        )
        self.assertEqual(result.kind, "none")

    def test_machine_marker_and_challenge_title_classify_turnstile(self):
        result = classify_challenge(
            title="Verify you are human",
            html='<div class="cf-turnstile"></div>',
            http_status=403,
            engine="patchright",
        )
        self.assertEqual(result.kind, "turnstile")
        self.assertTrue(result.human_action_required)
        self.assertGreater(result.confidence, 0.8)


class BrowserExtractionTests(unittest.TestCase):
    def test_article_extraction_returns_metadata_markdown_and_absolute_links(self):
        html = """
        <html><head><title>Example Story</title>
        <link rel="canonical" href="/story"><meta name="author" content="A. Writer">
        <meta property="article:published_time" content="2026-07-22"></head>
        <body><article><h1>Example Story</h1><p>Hello <a href="/more">world</a>.</p></article></body></html>
        """
        result = extract_document(html, final_url="https://example.com/input", mode="auto")
        self.assertEqual(result.mode, "article")
        self.assertEqual(result.canonical_url, "https://example.com/story")
        self.assertEqual(result.article.author, "A. Writer")
        self.assertIn("# Example Story", result.markdown)
        self.assertEqual(result.links[0].url, "https://example.com/more")


class BrowserProxyTests(unittest.TestCase):
    def test_proxy_bindings_are_sticky_and_do_not_expose_credentials(self):
        resolver = ProxyResolver({"research": "http://user:password@proxy.example:8080"}, clock=lambda: 100.0)
        first = resolver.resolve("research", domain="example.com")
        second = resolver.resolve(None, domain="example.com")
        self.assertEqual(first, second)
        self.assertEqual(first.server, "http://proxy.example:8080")
        self.assertNotIn("user", repr(first))
        self.assertNotIn("password", repr(first))

    def test_managed_pool_rotation_changes_endpoint_only_after_invalidation(self):
        resolver = ProxyResolver({
            "pool": ["http://first.example:8080", "http://second.example:8080"],
        }, clock=lambda: 100.0)
        first = resolver.resolve("pool", domain="example.com")
        self.assertEqual(resolver.resolve(None, domain="example.com"), first)
        resolver.invalidate("example.com")
        rotated = resolver.resolve("pool", domain="example.com")
        self.assertNotEqual(rotated.server, first.server)

    def test_unknown_proxy_binding_fails_without_disclosing_configured_endpoints(self):
        resolver = ProxyResolver({"known": "http://user:password@proxy.example:8080"})
        with self.assertRaisesRegex(ProxyConfigurationError, "Unknown browser proxy binding 'missing'") as raised:
            resolver.resolve("missing", domain="example.com")
        self.assertNotIn("proxy.example", str(raised.exception))
        self.assertNotIn("password", str(raised.exception))


class ScraplingCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_storage_state_compatibility_is_confined_and_awaited(self):
        from app.browser_runtime.adapters.scrapling import ScraplingAdapter

        context = unittest.mock.MagicMock()
        context.storage_state = unittest.mock.AsyncMock(return_value={"cookies": [{"name": "clearance"}], "origins": []})
        client = type("Client", (), {"context": context, "_context": None, "browser": None})()
        state = await ScraplingAdapter._storage_state(client, object())
        self.assertEqual(state["cookies"][0]["name"], "clearance")

    async def test_scrapling_kill_switch_does_not_disable_patchright_contract(self):
        from app.browser_runtime.adapters.scrapling import ScraplingAdapter

        health = await ScraplingAdapter(enabled=False).health()

        self.assertFalse(health["available"])
        self.assertFalse(health["enabled"])
        self.assertEqual(health["reason"], "kill switch disabled")


class BrowserDomainPolicyTests(unittest.TestCase):
    def test_history_persists_and_expires_without_stale_lock_in(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"BROWSER_DOMAIN_HISTORY_TTL_SECONDS": "300"}, clear=False
        ):
            path = Path(tmp) / "domains.json"
            store = DomainPolicyStore(path, clock=lambda: now[0])
            store.record("example.com", engine="patchright", success=True, challenge="none")
            reloaded = DomainPolicyStore(path, clock=lambda: now[0])
            self.assertEqual(reloaded.get("example.com").successes, 1)
            self.assertEqual(reloaded.get("example.com").engine_successes, {"patchright": 1})
            self.assertEqual(reloaded.get("example.com").strategy_successes, {"primary": 1})
            now[0] = 401.0
            self.assertIsNone(reloaded.get("example.com"))

    def test_fallback_results_are_counted_by_engine_and_strategy(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DomainPolicyStore(Path(tmp) / "domains.json")
            store.record("example.com", engine="scrapling", success=True, fallback=True)
            history = store.get("example.com")
            self.assertEqual(history.engine_successes, {"scrapling": 1})
            self.assertEqual(history.strategy_successes, {"fallback": 1})


class BrowserDomainPolicyAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_can_reduce_domain_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ", {"BROWSER_DOMAIN_MAX_CONCURRENCY": "3"}, clear=False
        ):
            store = DomainPolicyStore(Path(tmp) / "domains.json")
            first = await store.acquire("example.com", max_concurrency=1)
            blocked = asyncio.create_task(store.acquire("example.com", max_concurrency=3))
            await asyncio.sleep(0)

            self.assertFalse(blocked.done())
            await first.release()
            second = await asyncio.wait_for(blocked, timeout=0.5)
            await second.release()


class _FakeHandle:
    def __init__(self, engine: str) -> None:
        self.engine = engine
        self.page = type("Page", (), {"url": "https://example.com/final"})()
        self.closed = False
        self.crashed = False

    async def close(self) -> None:
        self.closed = True


class _FailingCloseHandle(_FakeHandle):
    async def close(self) -> None:
        raise RuntimeError("cleanup failed")


class _FakeAdapter:
    interactive = True

    def __init__(self, name: str, *, navigation_errors: int = 0, challenge: ChallengeResult | None = None) -> None:
        self.name = name
        self.navigation_errors = navigation_errors
        self.challenge_result = challenge or ChallengeResult(engine=name)
        self.started = 0
        self.navigated = 0
        self.navigation_timeouts: list[int] = []
        self.handles: list[_FakeHandle] = []

    async def start(self, **_) -> _FakeHandle:
        self.started += 1
        handle = _FakeHandle(self.name)
        self.handles.append(handle)
        return handle

    async def navigate(self, session, url: str, *, timeout_ms: int):
        self.navigated += 1
        self.navigation_timeouts.append(timeout_ms)
        if self.navigated <= self.navigation_errors:
            raise RuntimeError("navigation failed")
        session.page.url = "https://example.com/final"
        return {"requested_url": url, "final_url": session.page.url, "title": "Example", "http_status": 200}

    async def extract(self, session, *, mode: str, max_chars: int):
        return ExtractionResult(mode="text", title="Example", text="content")

    async def challenge(self, session, *, http_status=None):
        return self.challenge_result.model_copy(update={"engine": self.name, "http_status": http_status})

    async def action(self, session, request: ActionRequest):
        if request.action == "screenshot":
            return {"screenshot_base64": "cG5n", "final_url": session.page.url}
        return {"message": request.action, "final_url": session.page.url}

    async def health(self):
        return {"available": True}

    async def shutdown(self):
        return None


class BrowserAsyncRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry_isolates_owners_and_closes_expired_handles(self):
        now = [100.0]
        registry = AsyncSessionRegistry(idle_ttl_seconds=5, maximum_ttl_seconds=10, clock=lambda: now[0])
        owner = OwnerClaims(execution_id="execution-1")
        handle = _FakeHandle("patchright")
        record = await registry.create(owner=owner, handle=handle, allowed_hosts=["example.com"])
        with self.assertRaises(SessionAccessError):
            await registry.resolve(owner=OwnerClaims(execution_id="execution-2"), session_id=record.session_id)
        now[0] = 106.0
        self.assertEqual(await registry.expire(), 1)
        self.assertTrue(handle.closed)

    async def test_registry_runs_normal_cleanup_for_crashed_handles(self):
        registry = AsyncSessionRegistry()
        owner = OwnerClaims(execution_id="execution-1")
        handle = _FakeHandle("patchright")
        record = await registry.create(owner=owner, handle=handle, allowed_hosts=["example.com"])
        handle.crashed = True

        with self.assertRaisesRegex(SessionNotFoundError, "not found or expired"):
            await registry.resolve(owner=owner, session_id=record.session_id)

        self.assertTrue(handle.closed)
        self.assertEqual(registry.active_count, 0)

    async def test_registry_counts_cleanup_failures(self):
        registry = AsyncSessionRegistry()
        owner = OwnerClaims(execution_id="execution-1")
        record = await registry.create(
            owner=owner,
            handle=_FailingCloseHandle("patchright"),
            allowed_hosts=["example.com"],
        )

        self.assertTrue(await registry.close(owner=owner, session_id=record.session_id))
        self.assertEqual(registry.cleanup_failures, 1)

    async def test_concurrent_users_agents_workflows_and_executions_remain_isolated(self):
        registry = AsyncSessionRegistry(max_sessions_per_owner=2, max_sessions_total=8)
        owners = [
            OwnerClaims(execution_id=f"execution-{index}", workflow_id=f"workflow-{index}",
                        agent_id=f"agent-{index}", user_id=f"user-{index}")
            for index in range(4)
        ]
        records = await asyncio.gather(*(
            registry.create(owner=owner, handle=_FakeHandle("patchright"), allowed_hosts=["example.com"])
            for owner in owners
        ))

        resolved = await asyncio.gather(*(
            registry.resolve(owner=owner, session_id=record.session_id)
            for owner, record in zip(owners, records, strict=True)
        ))

        self.assertEqual([record.session_id for record in resolved], [record.session_id for record in records])
        with self.assertRaises(SessionAccessError):
            await registry.resolve(owner=owners[1], session_id=records[0].session_id)
        self.assertEqual(await registry.close_all(), 4)


class PatchrightResourceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_disk_pressure_rejects_launch_before_browser_resources_are_created(self):
        patchright_package = types.ModuleType("patchright")
        patchright_api = types.ModuleType("patchright.async_api")
        patchright_api.async_playwright = unittest.mock.MagicMock()
        adapter = PatchrightAdapter()
        adapter.minimum_free_bytes = 1024

        with patch.dict(sys.modules, {"patchright": patchright_package, "patchright.async_api": patchright_api}), \
                patch("app.browser_runtime.adapters.patchright.shutil.disk_usage",
                      return_value=SimpleNamespace(free=512)):
            with self.assertRaisesRegex(EngineUnavailableError, "insufficient free disk space"):
                await adapter.start(options=BrowserOptions(), proxy=None, allowed_hosts=["example.com"])

    async def test_hung_engine_cleanup_is_bounded_and_removes_runtime_profile(self):
        async def never_finishes():
            await asyncio.Future()

        context = unittest.mock.MagicMock()
        context.close = unittest.mock.AsyncMock(side_effect=never_finishes)
        context.tracing.stop = unittest.mock.AsyncMock(side_effect=never_finishes)
        browser = unittest.mock.MagicMock()
        browser.close = unittest.mock.AsyncMock(side_effect=never_finishes)
        playwright = unittest.mock.MagicMock()
        playwright.stop = unittest.mock.AsyncMock(side_effect=never_finishes)
        with tempfile.TemporaryDirectory() as parent, patch.dict(
                "os.environ",
                {"BROWSER_RESOURCE_CLEANUP_TIMEOUT_SECONDS": "0.1"},
                clear=False,
        ):
            runtime_dir = Path(parent) / "profile"
            runtime_dir.mkdir()
            session = PatchrightSession(
                engine="patchright",
                playwright=playwright,
                browser=browser,
                context=context,
                page=unittest.mock.MagicMock(),
                runtime_dir=runtime_dir,
                trace_path=runtime_dir / "trace.zip",
                trace_mode="on",
            )

            await asyncio.wait_for(session.close(), timeout=0.8)

            self.assertFalse(runtime_dir.exists())


class BrowserRuntimeServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_exposes_release_identity_and_process_tree_resources(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            "os.environ",
            {
                "BROWSER_RUNTIME_RELEASE": "candidate-abc123",
                "BROWSER_RUNTIME_IMAGE_REF": "registry.example/runtime@sha256:abc123",
            },
            clear=False,
        ):
            service = BrowserRuntimeService(
                primary=_FakeAdapter("patchright"),
                fallback=_FakeAdapter("scrapling"),
                runtime_root=tmp,
            )

            health = await service.health()

            self.assertEqual(health.release["id"], "candidate-abc123")
            self.assertEqual(health.release["image"], "registry.example/runtime@sha256:abc123")
            self.assertGreaterEqual(health.metrics["gauges"]["process_tree_pids"], 1)
            self.assertIn("cgroup_memory_current_bytes", health.metrics["gauges"])
            self.assertIn("cgroup_pids_current", health.metrics["gauges"])

    def test_handoff_storage_verification_includes_local_storage_values(self):
        state = {
            "origins": [{
                "origin": "https://example.com",
                "localStorage": [{"name": "challenge", "value": "cleared"}],
            }]
        }
        self.assertEqual(
            BrowserRuntimeService._storage_entries(state),
            {("https://example.com", "challenge", "cleared")},
        )

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_keep_open_returns_controllable_owner_scoped_session(self, _):
        primary = _FakeAdapter("patchright")
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            owner = OwnerClaims(execution_id="execution-1")
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=owner,
            )
            self.assertTrue(response.interactive)
            self.assertIsNotNone(response.session_id)
            action = await service.action(response.session_id, ActionRequest(action="scroll"), owner=owner)
            self.assertEqual(action["message"], "scroll")
            self.assertEqual(fallback.started, 0)
            await service.shutdown()

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_agent_runtime_policy_is_applied_within_operator_limits(self, _):
        primary = _FakeAdapter("patchright")
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        now = [100.0]
        registry = AsyncSessionRegistry(
            idle_ttl_seconds=300,
            maximum_ttl_seconds=900,
            max_sessions_per_owner=3,
            max_sessions_total=8,
            clock=lambda: now[0],
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ",
                {
                    "BROWSER_NAVIGATION_TIMEOUT_MS": "45000",
                    "BROWSER_PATCHRIGHT_ATTEMPTS": "3",
                    "BROWSER_DOMAIN_MAX_CONCURRENCY": "2",
                    "BROWSER_DOMAIN_MIN_INTERVAL_SECONDS": "0",
                    "BROWSER_ARTIFACT_RETENTION_SECONDS": "3600",
                },
                clear=False,
        ):
            service = BrowserRuntimeService(
                primary=primary,
                fallback=fallback,
                registry=registry,
                runtime_root=tmp,
            )
            response = await service.open(
                OpenRequest(
                    url="https://example.com",
                    allowed_hosts=["example.com"],
                    keep_open=True,
                    runtime_policy=BrowserRuntimePolicy(
                        session_idle_ttl_seconds=120,
                        session_maximum_ttl_seconds=600,
                        max_sessions_per_owner=1,
                        max_sessions_total=4,
                        navigation_timeout_ms=10_000,
                        retry_attempts=1,
                        domain_max_concurrency=1,
                        domain_min_interval_seconds=0,
                        artifact_retention_seconds=600,
                    ),
                ),
                owner=OwnerClaims(execution_id="execution-1"),
            )

            record = await registry.resolve(
                owner=OwnerClaims(execution_id="execution-1"),
                session_id=response.session_id,
                touch=False,
            )
            self.assertEqual(primary.navigation_timeouts, [10_000])
            self.assertEqual(record.idle_expires_at, 220.0)
            self.assertEqual(record.maximum_expires_at, 700.0)
            self.assertEqual(record.artifact_retention_seconds, 600)
            self.assertEqual(response.diagnostics["runtime_policy"]["max_sessions_per_owner"], 1)
            self.assertEqual(response.diagnostics["runtime_policy"]["domain_max_concurrency"], 1)
            await service.shutdown()

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_agent_runtime_policy_cannot_exceed_operator_limits(self, _):
        primary = _FakeAdapter("patchright", navigation_errors=2)
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        registry = AsyncSessionRegistry(
            idle_ttl_seconds=120,
            maximum_ttl_seconds=300,
            max_sessions_per_owner=2,
            max_sessions_total=4,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
                "os.environ",
                {
                    "BROWSER_NAVIGATION_TIMEOUT_MS": "20000",
                    "BROWSER_PATCHRIGHT_ATTEMPTS": "2",
                    "BROWSER_DOMAIN_MAX_CONCURRENCY": "2",
                    "BROWSER_DOMAIN_MIN_INTERVAL_SECONDS": "0.25",
                    "BROWSER_ARTIFACT_RETENTION_SECONDS": "1000",
                },
                clear=False,
        ):
            service = BrowserRuntimeService(
                primary=primary,
                fallback=fallback,
                registry=registry,
                runtime_root=tmp,
            )
            response = await service.open(
                OpenRequest(
                    url="https://example.com",
                    allowed_hosts=["example.com"],
                    keep_open=False,
                    runtime_policy=BrowserRuntimePolicy(
                        session_idle_ttl_seconds=500,
                        session_maximum_ttl_seconds=600,
                        max_sessions_per_owner=5,
                        max_sessions_total=10,
                        navigation_timeout_ms=100_000,
                        retry_attempts=3,
                        domain_max_concurrency=4,
                        domain_min_interval_seconds=0,
                        artifact_retention_seconds=2000,
                    ),
                ),
                owner=OwnerClaims(execution_id="execution-1"),
            )

            policy = response.diagnostics["runtime_policy"]
            self.assertEqual(primary.navigation_timeouts, [20_000, 20_000])
            self.assertEqual(primary.started, 2)
            self.assertEqual(policy["session_idle_ttl_seconds"], 300)
            self.assertEqual(policy["session_maximum_ttl_seconds"], 300)
            self.assertEqual(policy["max_sessions_per_owner"], 2)
            self.assertEqual(policy["max_sessions_total"], 4)
            self.assertEqual(policy["navigation_timeout_ms"], 20_000)
            self.assertEqual(policy["retry_attempts"], 2)
            self.assertEqual(policy["domain_max_concurrency"], 2)
            self.assertEqual(policy["domain_min_interval_seconds"], 0.25)
            self.assertEqual(policy["artifact_retention_seconds"], 1000)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_sessionless_retrieval_closes_primary_resources(self, _):
        primary = _FakeAdapter("patchright")
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=False),
                owner=OwnerClaims(actor="operator"),
            )
            self.assertFalse(response.interactive)
            self.assertTrue(primary.handles[0].closed)
            self.assertEqual(service.registry.active_count, 0)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_scrapling_runs_only_after_bounded_primary_failures(self, _):
        primary = _FakeAdapter("patchright", navigation_errors=3)
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=False),
                owner=OwnerClaims(execution_id="execution-1"),
            )
            self.assertEqual(primary.started, 3)
            self.assertEqual(fallback.started, 1)
            self.assertEqual(response.engine, "scrapling")
            self.assertFalse(response.interactive)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_human_challenge_returns_owned_screenshot_handoff(self, _):
        challenge = ChallengeResult(
            kind="visual_captcha",
            confidence=0.9,
            human_action_required=True,
            instructions="Complete the visual verification.",
        )
        primary = _FakeAdapter("patchright", challenge=challenge)
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=OwnerClaims(execution_id="execution-1"),
            )
            self.assertEqual(response.status, "human_action_required")
            self.assertTrue(response.interactive)
            self.assertEqual(response.human_handoff.ask_tool, "agency.human.ask")
            self.assertEqual(response.human_handoff.session_id, response.session_id)
            self.assertIn("challenge_screenshot_id", response.artifacts)
            await service.shutdown()

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_human_handoff_resumes_same_session_after_operator_completion(self, _):
        primary = _FakeAdapter(
            "patchright",
            challenge=ChallengeResult(kind="visual_captcha", confidence=0.9, human_action_required=True),
        )
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            owner = OwnerClaims(execution_id="execution-1")
            handoff = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=owner,
            )
            primary.challenge_result = ChallengeResult()

            resumed = await service.extract(
                handoff.session_id,
                ExtractRequest(extract_mode="text"),
                owner=owner,
            )

            self.assertEqual(resumed.status, "ok")
            self.assertEqual(resumed.session_id, handoff.session_id)
            self.assertTrue(resumed.interactive)
            await service.shutdown()

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_handoff_timeout_and_execution_cancellation_release_session(self, _):
        now = [100.0]
        registry = AsyncSessionRegistry(idle_ttl_seconds=5, maximum_ttl_seconds=10, clock=lambda: now[0])
        primary = _FakeAdapter(
            "patchright",
            challenge=ChallengeResult(kind="visual_captcha", confidence=0.9, human_action_required=True),
        )
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(
                primary=primary,
                fallback=fallback,
                registry=registry,
                runtime_root=tmp,
            )
            owner = OwnerClaims(execution_id="execution-1")
            first = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=owner,
            )
            now[0] = 106.0
            self.assertEqual(await service.expire(), 1)
            self.assertTrue(primary.handles[0].closed)

            second = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=owner,
            )
            self.assertNotEqual(second.session_id, first.session_id)
            self.assertEqual(await service.close_all_for_execution("execution-1"), 1)
            self.assertTrue(primary.handles[1].closed)
            self.assertEqual(service.registry.active_count, 0)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_runtime_shutdown_closes_all_retained_sessions(self, _):
        primary = _FakeAdapter("patchright")
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            for index in range(2):
                await service.open(
                    OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                    owner=OwnerClaims(execution_id=f"execution-{index}"),
                )

            await service.shutdown()

            self.assertTrue(all(handle.closed for handle in primary.handles))
            self.assertEqual(service.registry.active_count, 0)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_terminal_block_never_reports_success(self, _):
        challenge = ChallengeResult(
            kind="regional_block",
            confidence=0.9,
            retryable=False,
            terminal=True,
        )
        primary = _FakeAdapter("patchright", challenge=challenge)
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=OwnerClaims(execution_id="execution-1"),
            )

            self.assertEqual(response.status, "error")
            self.assertEqual(response.challenge.kind, "regional_block")
            self.assertFalse(response.interactive)
            self.assertIsNone(response.session_id)
            self.assertEqual(service.registry.active_count, 0)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_sessionless_human_challenge_requires_retained_retry(self, _):
        challenge = ChallengeResult(kind="visual_captcha", confidence=0.9, human_action_required=True)
        primary = _FakeAdapter("patchright", challenge=challenge)
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=False),
                owner=OwnerClaims(execution_id="execution-1"),
            )

            self.assertEqual(response.status, "error")
            self.assertFalse(response.interactive)
            self.assertIn("keep_open=true", response.message)

    @patch("app.browser_runtime.service.validate_public_url", return_value="https://example.com")
    async def test_retained_scrapling_fallback_reports_only_controllable_patchright_handoff(self, _):
        primary = _FakeAdapter("patchright", navigation_errors=3)
        fallback = _FakeAdapter("scrapling")
        fallback.interactive = False
        with tempfile.TemporaryDirectory() as tmp:
            service = BrowserRuntimeService(primary=primary, fallback=fallback, runtime_root=tmp)
            response = await service.open(
                OpenRequest(url="https://example.com", allowed_hosts=["example.com"], keep_open=True),
                owner=OwnerClaims(execution_id="execution-1"),
            )
            self.assertEqual(response.engine, "patchright")
            self.assertTrue(response.interactive)
            self.assertIsNotNone(response.session_id)
            self.assertTrue(response.diagnostics["handoff_state"]["storage_state_verified"])
            await service.shutdown()


class _ServerService:
    def __init__(self) -> None:
        self.open_owner = None

    async def open(self, request, *, owner):
        self.open_owner = owner
        return BrowserResponse(engine="patchright", requested_url=request.url, final_url=request.url)

    async def health(self):
        return HealthResult(status="ok", engines={}, active_sessions=0, runtime_root="/tmp")

    async def expire(self):
        return 0

    async def shutdown(self):
        return None


class BrowserRuntimeServerTests(unittest.TestCase):
    def test_server_authenticates_owner_host_grant_and_rejects_replay(self):
        master = "master-secret-with-at-least-32-characters"
        service = _ServerService()
        app = create_app(service=service, signing_secret=master)
        owner = OwnerClaims(execution_id="execution-1")
        execution_secret = derive_execution_secret(master, "execution-1")
        token = issue_capability(
            execution_secret,
            owner=owner,
            operations=["open"],
            allowed_hosts=["example.com"],
        )
        body = OpenRequest(url="https://example.com", allowed_hosts=["example.com"]).model_dump(mode="json")
        with TestClient(app) as client:
            response = client.post("/v1/open", json=body, headers={"Authorization": f"Bearer {token}"})
            replay = client.post("/v1/open", json=body, headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(service.open_owner.execution_id, "execution-1")
        self.assertEqual(replay.status_code, 409)


class BrowserRuntimeClientTests(unittest.TestCase):
    def test_client_grants_only_explicit_navigation_origin(self):
        secret = "master-secret-with-at-least-32-characters"

        def handler(request):
            from app.browser_runtime.security import peek_capability_owner
            import httpx

            token = request.headers["Authorization"][7:]
            owner = peek_capability_owner(token)
            claims = verify_capability(
                derive_execution_secret(secret, owner.execution_id), token, operation="open"
            )
            self.assertEqual(claims.allowed_hosts, ("example.com",))
            return httpx.Response(200, json={"status": "ok", "engine": "patchright"})

        import httpx
        client = BrowserRuntimeClient(
            base_url="http://browser-runtime",
            signing_secret=secret,
            transport=httpx.MockTransport(handler),
        )
        result = client.open(url="https://example.com/path", owner={"execution_id": "execution-1"})
        self.assertEqual(result["engine"], "patchright")
        client.close_client()


class BrowserLiveCheckTests(unittest.TestCase):
    @patch("scripts.browser_live_check.BrowserRuntimeClient")
    def test_opt_in_live_check_covers_sessionless_and_retained_lifecycles(self, client_type):
        client = client_type.return_value
        client.health.return_value = {
            "status": "ok",
            "active_sessions": 0,
            "metrics": {"gauges": {"cleanup_failures": 0}},
        }
        client.status.return_value = {"sessions": []}
        client.close.return_value = {"closed": True}
        client.open.side_effect = [
            {
                "status": "ok",
                "engine": "patchright",
                "interactive": False,
                "extraction": {"text": "Example"},
                "challenge": {"kind": "none"},
                "artifacts": {"engine_trace.zip": "artifact-1"},
            },
            {
                "status": "ok",
                "engine": "patchright",
                "interactive": True,
                "session_id": "brs_live",
                "challenge": {"kind": "none"},
            },
        ]
        client.action.return_value = {"session_id": "brs_live"}
        client.extract.return_value = {"session_id": "brs_live", "extraction": {"text": "Example"}}

        result = run_browser_live_check(
            "https://example.com",
            expect_challenge=False,
            challenge_kind=None,
        )

        self.assertEqual(result["sessionless"]["engine"], "patchright")
        self.assertTrue(result["retained"]["refreshed"])
        self.assertEqual(result["cleanup"]["owner_sessions"], 0)
        client.close.assert_called_once()
        client.close_client.assert_called_once_with()

    @patch("scripts.browser_live_check.BrowserRuntimeClient")
    def test_live_check_proves_human_challenge_recovery_on_same_session(self, client_type):
        client = client_type.return_value
        client.health.return_value = {
            "status": "ok",
            "active_sessions": 0,
            "metrics": {"gauges": {"cleanup_failures": 0}},
        }
        client.status.return_value = {"sessions": []}
        client.close.return_value = {"closed": True}
        client.open.side_effect = [
            {
                "status": "error",
                "engine": "patchright",
                "interactive": False,
                "challenge": {"kind": "turnstile"},
                "artifacts": {},
            },
            {
                "status": "human_action_required",
                "engine": "patchright",
                "interactive": True,
                "session_id": "brs_handoff",
                "challenge": {"kind": "turnstile"},
                "human_handoff": {
                    "instructions": "Complete the visible verification.",
                    "expires_at": 999,
                },
            },
        ]
        client.extract.return_value = {
            "status": "ok",
            "session_id": "brs_handoff",
            "challenge": {"kind": "none"},
            "extraction": {"text": "Recovered content"},
        }

        result = run_browser_live_check(
            "https://challenge.example.com",
            expect_challenge=True,
            challenge_kind="turnstile",
            human_wait_seconds=1,
            human_poll_seconds=0.01,
        )

        self.assertTrue(result["retained"]["challenge_recovered"])
        self.assertEqual(result["retained"]["initial_challenge"], "turnstile")
        client.extract.assert_called_once_with(
            "brs_handoff",
            owner=unittest.mock.ANY,
            extract_mode="auto",
        )
        client.close.assert_called_once_with("brs_handoff", owner=unittest.mock.ANY)

    @patch("scripts.browser_live_check.BrowserRuntimeClient")
    def test_live_check_records_challenge_cleared_on_fresh_retained_navigation(self, client_type):
        client = client_type.return_value
        client.health.return_value = {"status": "ok", "metrics": {"gauges": {}}, "active_sessions": 0}
        client.status.return_value = {"sessions": []}
        client.close.return_value = {"closed": True}
        client.open.side_effect = [
            {
                "status": "error",
                "engine": "patchright",
                "interactive": False,
                "challenge": {"kind": "turnstile"},
                "artifacts": {},
            },
            {
                "status": "ok",
                "engine": "patchright",
                "interactive": True,
                "session_id": "brs_recovered",
                "challenge": {"kind": "none"},
            },
        ]
        client.extract.return_value = {
            "status": "ok",
            "session_id": "brs_recovered",
            "extraction": {"text": "Recovered content"},
        }

        result = run_browser_live_check(
            "https://challenge.example.com",
            expect_challenge=True,
            challenge_kind="turnstile",
        )

        self.assertTrue(result["retained"]["challenge_recovered"])
        self.assertEqual(result["retained"]["recovery_mode"], "fresh_retained_navigation")


class BrowserRolloutCheckTests(unittest.TestCase):
    @staticmethod
    def _live_result(*, challenge: bool = False, release: str = "candidate"):
        return {
            "url": "https://example.com",
            "health": {"before": "ok", "after": "ok", "release": {"id": release, "image": release}},
            "sessionless": {
                "status": "error" if challenge else "ok",
                "engine": "patchright",
                "challenge": "turnstile" if challenge else "none",
            },
            "retained": {
                "status": "ok",
                "engine": "patchright",
                "challenge_recovered": challenge,
            },
            "cleanup": {"closed": True, "owner_sessions": 0, "cleanup_failures": 0},
            "resources": {"process_tree_rss_bytes": 1000, "process_tree_pids": 5},
        }

    @patch("scripts.browser_rollout_check.run_live_check")
    def test_capture_records_normal_and_recovered_challenge_evidence(self, live_check):
        live_check.side_effect = [self._live_result(), self._live_result(challenge=True)]

        record = capture_browser_rollout(
            label="candidate",
            urls=["https://example.com"],
            challenge_url="https://challenge.example.com",
            challenge_kind="turnstile",
            human_wait_seconds=30,
        )

        self.assertEqual(record["schema"], ROLLOUT_SCHEMA)
        self.assertEqual(record["summary"]["success_rate"], 1.0)
        self.assertEqual(record["summary"]["challenge_recovery_rate"], 1.0)
        self.assertEqual(record["summary"]["cleanup_failure_count"], 0)

    def test_capture_rejects_challenge_without_human_resume_window(self):
        with self.assertRaisesRegex(ValueError, "human-wait-seconds"):
            capture_browser_rollout(
                label="candidate",
                urls=["https://example.com"],
                challenge_url="https://challenge.example.com",
            )

    def test_comparison_gates_release_behavior_latency_resources_and_cleanup(self):
        baseline = {
            "schema": ROLLOUT_SCHEMA,
            "label": "baseline",
            "scenarios": [{"kind": "normal", "url": "https://example.com"}],
            "summary": {
                "success_rate": 1.0,
                "latency_ms": {"mean": 100.0},
                "fallback_rate": 0.0,
                "challenge_rate": 0.0,
                "challenge_recovery_rate": 1.0,
                "cleanup_failure_count": 0,
                "max_memory_bytes": 1000,
                "max_pids": 10,
                "resource_sources": ["cgroup"],
                "releases": [{"id": "baseline", "image": "runtime:baseline"}],
            },
        }
        candidate = {
            **baseline,
            "label": "candidate",
            "summary": {
                **baseline["summary"],
                "latency_ms": {"mean": 120.0},
                "max_memory_bytes": 1200,
                "max_pids": 11,
                "releases": [{"id": "candidate", "image": "runtime:candidate"}],
            },
        }

        passed = compare_browser_rollouts(baseline, candidate, expected_release="candidate")
        self.assertEqual(passed["status"], "passed")

        regressed = {
            **candidate,
            "summary": {
                **candidate["summary"],
                "success_rate": 0.5,
                "cleanup_failure_count": 1,
            },
        }
        failed = compare_browser_rollouts(baseline, regressed, expected_release="candidate")
        self.assertEqual(failed["status"], "failed")
        failed_names = {item["name"] for item in failed["checks"] if not item["passed"]}
        self.assertEqual(failed_names, {"success_rate", "cleanup_failures"})

    def test_merge_combines_repeated_rollout_samples(self):
        scenario = {
            "kind": "normal",
            "url": "https://example.com",
            "status": "passed",
            "wall_time_ms": 100,
            "result": self._live_result(),
        }
        record = {
            "schema": ROLLOUT_SCHEMA,
            "label": "sample",
            "runtime_url": "local",
            "scenarios": [scenario],
            "summary": {"success_rate": 1.0},
        }

        merged = merge_browser_rollouts([record, record, record], label="three-samples")

        self.assertEqual(merged["summary"]["sample_count"], 3)
        self.assertEqual(merged["summary"]["success_rate"], 1.0)
        self.assertEqual(merged["summary"]["latency_ms"]["mean"], 100.0)

    def test_process_tree_resource_snapshot_is_safe_on_current_platform(self):
        snapshot = process_tree_resources()
        self.assertGreaterEqual(snapshot["process_tree_pids"], 1)
        if snapshot["process_tree_rss_bytes"] is not None:
            self.assertGreater(snapshot["process_tree_rss_bytes"], 0)
