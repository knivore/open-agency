from __future__ import annotations

from playwright.sync_api import Page

_CHALLENGE_PATTERNS: tuple[str, ...] = (
    "captcha",
    "verify you are human",
    "verify you're human",
    "cf-challenge",
    "cloudflare",
    "attention required",
    "access denied",
    "request blocked",
    "datadome",
    "challenge-running",
    "turnstile",
    "recaptcha",
    "hcaptcha",
)

_OVERLAY_SELECTORS: tuple[str, ...] = (
    "#onetrust-accept-btn-handler",
    "button:has-text('Accept')",
    "button:has-text('I agree')",
    "button:has-text('Agree')",
    "button:has-text('Allow all')",
    "button:has-text('Continue')",
    "button:has-text('OK')",
)


def detect_page_challenge(page: Page) -> str | None:
    try:
        content = " ".join(
            [
                page.url or "",
                page.title() or "",
                page.locator("body").inner_text(timeout=3000)[:2500],
            ]
        ).lower()
    except Exception:
        content = f"{page.url} {(page.title() if page else '')}".lower()
    for pattern in _CHALLENGE_PATTERNS:
        if pattern in content:
            return pattern
    return None


def overlay_selectors() -> tuple[str, ...]:
    return _OVERLAY_SELECTORS


__all__ = ["detect_page_challenge", "overlay_selectors"]
