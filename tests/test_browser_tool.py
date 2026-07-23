from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.core.outbound_http import validate_outbound_http_url
from app.tools.implementations.browser import open_browser


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

    @patch("app.tools.implementations.browser.get_browser_runtime_client")
    def test_open_browser_passes_session_options(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.open.return_value = {
            "version": "agency.browser.v1",
            "status": "ok",
            "session_id": "brs_test",
            "interactive": True,
            "engine": "patchright",
            "requested_url": "https://example.com/path",
            "final_url": "https://example.com",
            "title": "Example",
            "challenge": {"kind": "none"},
            "extraction": {"mode": "text", "text": "Example content"},
        }
        mock_get_client.return_value = mock_client

        result = open_browser(
            url="https://example.com/path",
            trace_mode="on",
            record_video=True,
            locale="en-US",
            timezone_id="Asia/Singapore",
            extra_http_headers={"x-test": "1"},
            runtime_policy={
                "session_idle_ttl_seconds": 120,
                "navigation_timeout_ms": 10_000,
                "retry_attempts": 1,
            },
            _allowed_hosts=["example.com"],
        )

        self.assertEqual(result["session_id"], "brs_test")
        self.assertTrue(result["interactive"])
        self.assertEqual(result["engine"], "patchright")
        self.assertEqual(result["url"], "https://example.com")
        self.assertIsNone(result["challenge_detected"])
        _, kwargs = mock_client.open.call_args
        self.assertTrue(kwargs["keep_open"])
        self.assertEqual(kwargs["extract_mode"], "auto")
        self.assertEqual(kwargs["owner"].actor, "direct-local-browser")
        self.assertEqual(kwargs["options"].trace_mode, "on")
        self.assertTrue(kwargs["options"].record_video)
        self.assertEqual(kwargs["options"].locale, "en-US")
        self.assertEqual(kwargs["options"].timezone_id, "Asia/Singapore")
        self.assertEqual(kwargs["options"].extra_http_headers, {"x-test": "1"})
        self.assertEqual(kwargs["runtime_policy"].session_idle_ttl_seconds, 120)
        self.assertEqual(kwargs["runtime_policy"].navigation_timeout_ms, 10_000)
        self.assertEqual(kwargs["runtime_policy"].retry_attempts, 1)

    @patch("app.tools.implementations.browser.get_browser_runtime_client")
    def test_unified_browser_kill_switch_fails_before_runtime_access(self, runtime_client):
        with patch.dict("os.environ", {"BROWSER_UNIFIED_ENABLED": "false"}, clear=False):
            result = open_browser(url="https://example.com")

        self.assertIn("disabled by operator policy", result["Error Message"])
        runtime_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
