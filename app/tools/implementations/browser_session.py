from __future__ import annotations

import yaml
from pathlib import Path
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from threading import Lock
from typing import Any

from .browser_runtime import configure_browser_runtime_env, ensure_browser_runtime_dir
from .browser_session_state import BrowserSessionState

_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };

const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
  window.navigator.permissions.query = (parameters) => (
    parameters && parameters.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(parameters)
  );
}
"""


class BrowserSessionManager:
    _instance: BrowserSessionManager | None = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.playwright = None
                cls._instance.browser = None
                cls._instance.context = None
                cls._instance.page = None
                cls._instance.runtime_root = None
                cls._instance.trace_path = None
                cls._instance.session_state = BrowserSessionState()
                cls._instance.config = cls._instance._load_config()
        return cls._instance

    playwright: Playwright | None
    browser: Browser | None
    context: BrowserContext | None
    page: Page | None
    runtime_root: str | None
    trace_path: str | None
    session_state: BrowserSessionState
    config: dict[str, Any]

    def _load_config(self) -> dict[str, Any]:
        config_path = Path(__file__).resolve().parents[1] / "config" / "playwright_config.yaml"
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
        return {
            "browser": "chromium",
            "timeout": 30000,
            "viewport": {"width": 1920, "height": 1080},
            "headless": False,
            "ignore_https_errors": True,
            "trace": "off",
            "record_video": False,
            "accept_downloads": True,
            "bypass_csp": True,
            "java_script_enabled": True,
            "locale": "en-US",
            "timezone_id": None,
            "launch_args": [],
            "slow_mo": 0,
        }

    def _close_context(self) -> None:
        if self.context is None:
            return
        if self.trace_path is not None:
            self.context.tracing.stop(path=self.trace_path)
            self.trace_path = None
        self.context.close()
        self.context = None
        self.page = None

    def start_browser(
            self,
            *,
            headless_mode: bool | None = None,
            browser_type: str | None = None,
            context_options: dict[str, Any] | None = None,
            session_options: dict[str, Any] | None = None,
    ) -> Page:
        self.runtime_root = configure_browser_runtime_env("agency-browser-tool")
        self.session_state = BrowserSessionState()
        browser_name = browser_type or self.config.get("browser", "chromium")
        if self.playwright is None:
            self.playwright = sync_playwright().start()
        if self.browser is None:
            launcher = getattr(self.playwright, browser_name)
            launch_options: dict[str, Any] = {
                "headless": self.config.get("headless", False) if headless_mode is None else headless_mode,
            }
            if self.config.get("slow_mo", 0):
                launch_options["slow_mo"] = self.config["slow_mo"]
            if self.config.get("channel"):
                launch_options["channel"] = self.config["channel"]
            launch_args = [arg for arg in self.config.get("launch_args", []) if arg]
            if launch_args:
                launch_options["args"] = launch_args
            self.browser = launcher.launch(**launch_options)
        if self.context is not None:
            self._close_context()

        options = {
            "viewport": self.config.get("viewport", {"width": 1920, "height": 1080}),
            "ignore_https_errors": self.config.get("ignore_https_errors", True),
            "bypass_csp": self.config.get("bypass_csp", True),
            "accept_downloads": self.config.get("accept_downloads", True),
            "java_script_enabled": self.config.get("java_script_enabled", True),
        }
        locale = self.config.get("locale")
        if locale:
            options["locale"] = locale
        timezone_id = self.config.get("timezone_id")
        if timezone_id:
            options["timezone_id"] = timezone_id
        color_scheme = self.config.get("color_scheme")
        if color_scheme:
            options["color_scheme"] = color_scheme
        if context_options:
            options.update(context_options)
        session_options = session_options or {}
        storage_state_path = session_options.get("storage_state_path")
        if storage_state_path:
            options["storage_state"] = storage_state_path
        if session_options.get("record_video", self.config.get("record_video", False)):
            options["record_video_dir"] = ensure_browser_runtime_dir("agency-browser-tool", "artifacts", "video")
        self.context = self.browser.new_context(**options)
        self.context.add_init_script(_STEALTH_INIT_SCRIPT)
        self.page = self.context.new_page()
        self.page.set_default_timeout(self.config.get("timeout", 30000))
        trace_mode = session_options.get("trace_mode", self.config.get("trace", "off"))
        if trace_mode and str(trace_mode).lower() != "off":
            trace_name = session_options.get("trace_name", "browser-session")
            trace_dir = ensure_browser_runtime_dir("agency-browser-tool", "artifacts", "traces")
            self.trace_path = str(Path(trace_dir) / f"{trace_name}.zip")
            self.context.tracing.start(
                screenshots=True,
                snapshots=True,
                sources=str(trace_mode).lower() == "retain-on-failure",
            )
        return self.page

    def get_page(self) -> Page:
        if self.page is None:
            raise RuntimeError("Browser is not started. Call open_browser first.")
        return self.page

    def set_page(self, page: Page) -> None:
        self.page = page

    def stop_browser(self) -> None:
        if self.context is not None:
            self._close_context()
        if self.browser is not None:
            self.browser.close()
        if self.playwright is not None:
            self.playwright.stop()
        self.context = None
        self.browser = None
        self.playwright = None
        self.page = None
        self.trace_path = None
        self.session_state = BrowserSessionState()

    def record_signal(self, name: str, value: Any) -> None:
        self.session_state.record_signal(name, value)

    def record_artifact(self, name: str, value: Any) -> None:
        self.session_state.record_artifact(name, value)

    def merge_result(self, result: Any) -> None:
        self.session_state.merge_result(result)

    def get_session_details(self) -> dict[str, Any]:
        return {
            "runtime_root": self.runtime_root,
            "trace_path": self.trace_path,
            "browser_active": self.browser is not None,
            "context_active": self.context is not None,
            "page_active": self.page is not None,
            "session_state": self.session_state.snapshot(),
        }


__all__ = ["BrowserSessionManager"]
