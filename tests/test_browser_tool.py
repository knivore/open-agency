from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.tools.implementations.browser import open_browser, page_url_host
from app.core.outbound_http import validate_outbound_http_url
from app.tools.implementations.browser_navigation import goto_with_readiness
from app.tools.implementations.browser_runtime import configure_browser_runtime_env, ensure_browser_runtime_dir
from app.tools.implementations.browser_session import BrowserSessionManager
from app.tools.implementations.browser_session_state import BrowserActionResult, BrowserSessionState
from app.tools.implementations.browser_signals import detect_page_challenge, overlay_selectors


class BrowserRuntimeTests(unittest.TestCase):
    def test_runtime_dirs_are_created_under_configured_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict("os.environ", {"BROWSER_RUNTIME_ROOT": temp_dir}, clear=False):
                root = Path(configure_browser_runtime_env("agency-browser-tool"))
                artifact_dir = Path(ensure_browser_runtime_dir("agency-browser-tool", "artifacts", "traces"))
                self.assertTrue(root.exists())
                self.assertTrue((root / "tmp").exists())
                self.assertTrue((root / "runtime").exists())
                self.assertTrue(artifact_dir.exists())


class BrowserSessionManagerTests(unittest.TestCase):
    def tearDown(self) -> None:
        BrowserSessionManager().stop_browser()

    def test_get_session_details_without_browser(self):
        details = BrowserSessionManager().get_session_details()
        self.assertFalse(details["browser_active"])
        self.assertFalse(details["context_active"])
        self.assertFalse(details["page_active"])

    @patch("app.tools.implementations.browser_session.configure_browser_runtime_env")
    @patch("app.tools.implementations.browser_session.ensure_browser_runtime_dir")
    @patch("app.tools.implementations.browser_session.sync_playwright")
    def test_start_browser_applies_trace_and_video_settings(self, mock_sync_playwright, mock_ensure_runtime_dir,
                                                            mock_runtime_env):
        mock_runtime_env.return_value = "/tmp/browser-runtime/agency-browser-tool"
        mock_ensure_runtime_dir.side_effect = [
            "/tmp/browser-runtime/agency-browser-tool/artifacts/video",
            "/tmp/browser-runtime/agency-browser-tool/artifacts/traces",
        ]
        mock_page = MagicMock()
        mock_context = MagicMock()
        mock_context.new_page.return_value = mock_page
        mock_browser = MagicMock()
        mock_browser.new_context.return_value = mock_context
        mock_launcher = MagicMock()
        mock_launcher.launch.return_value = mock_browser
        mock_playwright = MagicMock()
        mock_playwright.chromium = mock_launcher
        mock_sync_playwright.return_value.start.return_value = mock_playwright

        manager = BrowserSessionManager()
        manager.browser = None
        manager.context = None
        manager.page = None
        manager.playwright = None

        manager.start_browser(
            headless_mode=True,
            browser_type="chromium",
            context_options={"user_agent": "ua"},
            session_options={"record_video": True, "trace_mode": "retain-on-failure", "trace_name": "example"},
        )

        mock_launcher.launch.assert_called_once()
        _, launch_kwargs = mock_launcher.launch.call_args
        self.assertTrue(launch_kwargs["headless"])

        _, context_kwargs = mock_browser.new_context.call_args
        self.assertEqual(context_kwargs["record_video_dir"], "/tmp/browser-runtime/agency-browser-tool/artifacts/video")
        self.assertEqual(context_kwargs["user_agent"], "ua")
        mock_context.tracing.start.assert_called_once()
        self.assertEqual(manager.trace_path, "/tmp/browser-runtime/agency-browser-tool/artifacts/traces/example.zip")

    def test_session_state_records_signals_and_artifacts(self):
        manager = BrowserSessionManager()
        manager.record_signal("challenge_detected", "cloudflare")
        manager.record_artifact("screenshot_file", "capture.png")

        details = manager.get_session_details()

        self.assertEqual(details["session_state"]["signals"]["challenge_detected"], "cloudflare")
        self.assertEqual(details["session_state"]["artifacts"]["screenshot_file"], "capture.png")


class BrowserSessionStateTests(unittest.TestCase):
    def test_merge_result_tracks_events(self):
        state = BrowserSessionState()
        state.merge_result(
            BrowserActionResult(
                signals={"overlay_dismissed": "button:has-text('Accept')"},
                artifacts={"screenshot_file": "capture.png"},
            )
        )

        self.assertEqual(state.signals["overlay_dismissed"], "button:has-text('Accept')")
        self.assertEqual(state.artifacts["screenshot_file"], "capture.png")
        self.assertEqual(len(state.events), 2)


class BrowserOpenToolTests(unittest.TestCase):
    @patch("app.core.outbound_http.socket.getaddrinfo")
    def test_outbound_url_rejects_allowlisted_hostname_resolving_private(self, getaddrinfo):
        getaddrinfo.return_value = [(2, 1, 6, "", ("10.0.0.5", 443))]
        with patch.dict("os.environ", {"APP_ENV": "development"}, clear=False):
            with self.assertRaisesRegex(ValueError, "non-public address"):
                validate_outbound_http_url(
                    "https://allowed.example/path",
                    allowed_hosts=["allowed.example"],
                )

    def test_page_url_host_extracts_netloc(self):
        self.assertEqual(page_url_host("https://example.com/path?q=1"), "example.com")
        self.assertEqual(page_url_host("http://sub.example.com:8443/test"), "sub.example.com:8443")

    @patch("app.tools.implementations.browser._upload_browser_screenshot")
    @patch("app.tools.implementations.browser.BrowserSessionManager")
    @patch("app.tools.implementations.browser.goto_with_readiness")
    def test_open_browser_passes_session_options(self, mock_goto_with_readiness, mock_manager_cls, mock_upload):
        mock_goto_with_readiness.return_value = BrowserActionResult(
            signals={"overlay_dismissed": "button:has-text('Accept')", "challenge_detected": None}
        )
        mock_page = MagicMock()
        mock_page.url = "https://example.com"
        mock_page.title.return_value = "Example"
        mock_page.screenshot.return_value = b"png"

        mock_manager = MagicMock()
        mock_manager.start_browser.return_value = mock_page
        mock_manager.get_session_details.return_value = {
            "runtime_root": "/tmp/runtime",
            "trace_path": "/tmp/runtime/artifacts/traces/example-com.zip",
            "browser_active": True,
            "context_active": True,
            "page_active": True,
            "session_state": {
                "signals": {
                    "overlay_dismissed": "button:has-text('Accept')",
                    "challenge_detected": None,
                },
                "artifacts": {"screenshot_file": "capture.png"},
                "events": [],
            },
        }
        mock_manager_cls.return_value = mock_manager

        result = open_browser(
            url="https://example.com/path",
            trace_mode="on",
            record_video=True,
            locale="en-US",
            timezone_id="Asia/Singapore",
            extra_http_headers={"x-test": "1"},
            _allowed_hosts=["example.com"],
        )

        self.assertEqual(result["trace_path"], "/tmp/runtime/artifacts/traces/example-com.zip")
        self.assertEqual(result["overlay_dismissed"], "button:has-text('Accept')")
        self.assertIsNone(result["challenge_detected"])
        _, kwargs = mock_manager.start_browser.call_args
        self.assertEqual(kwargs["session_options"]["trace_mode"], "on")
        self.assertTrue(kwargs["session_options"]["record_video"])
        self.assertEqual(kwargs["context_options"]["locale"], "en-US")
        self.assertEqual(kwargs["context_options"]["timezone_id"], "Asia/Singapore")
        self.assertEqual(kwargs["context_options"]["extra_http_headers"], {"x-test": "1"})
        mock_manager.context.route.assert_called_once()
        mock_upload.assert_called_once()


class BrowserSignalsTests(unittest.TestCase):
    def test_overlay_selectors_include_common_consent_button(self):
        self.assertIn("button:has-text('Accept')", overlay_selectors())

    def test_detect_page_challenge_matches_cloudflare_text(self):
        mock_page = MagicMock()
        mock_page.url = "https://example.com"
        mock_page.title.return_value = "Attention Required"
        mock_page.locator.return_value.inner_text.return_value = "Cloudflare challenge page"

        self.assertEqual(detect_page_challenge(mock_page), "cloudflare")


class BrowserNavigationTests(unittest.TestCase):
    @patch("app.tools.implementations.browser_navigation.detect_page_challenge")
    @patch("app.tools.implementations.browser_navigation.humanize_page")
    @patch("app.tools.implementations.browser_navigation.dismiss_common_overlays")
    def test_goto_with_readiness_returns_overlay_and_challenge(self, mock_dismiss, mock_humanize, mock_detect):
        mock_dismiss.return_value = "button:has-text('Accept')"
        mock_detect.return_value = "turnstile"
        mock_page = MagicMock()

        result = goto_with_readiness(mock_page, "https://example.com")

        mock_page.goto.assert_called_once_with("https://example.com", wait_until="domcontentloaded")
        mock_humanize.assert_called_once_with(mock_page, include_scroll=True)
        self.assertEqual(
            result.to_dict(),
            {
                "signals": {
                    "overlay_dismissed": "button:has-text('Accept')",
                    "challenge_detected": "turnstile",
                },
                "artifacts": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
