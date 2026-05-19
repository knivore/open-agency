from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .browser_interaction import dismiss_common_overlays, humanize_page
from .browser_session_state import BrowserActionResult
from .browser_signals import detect_page_challenge


def goto_with_readiness(page: Page, url: str) -> BrowserActionResult:
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightError:
        pass
    overlay_selector = dismiss_common_overlays(page)
    humanize_page(page, include_scroll=True)
    challenge = detect_page_challenge(page)
    return BrowserActionResult(
        signals={
            "overlay_dismissed": overlay_selector,
            "challenge_detected": challenge,
        }
    )


__all__ = ["goto_with_readiness"]
