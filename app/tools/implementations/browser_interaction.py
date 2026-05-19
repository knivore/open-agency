from __future__ import annotations

import random
from playwright.sync_api import Page

from .browser_signals import overlay_selectors


def sleep(page: Page, low_ms: int = 120, high_ms: int = 420) -> None:
    page.wait_for_timeout(random.randint(low_ms, high_ms))


def humanize_page(page: Page, *, include_scroll: bool = False) -> None:
    try:
        page.mouse.move(random.randint(40, 220), random.randint(80, 260), steps=random.randint(4, 10))
        sleep(page)
        if include_scroll:
            for _ in range(random.randint(1, 2)):
                page.mouse.wheel(0, random.randint(150, 350))
                sleep(page, 60, 180)
    except Exception:
        return


def dismiss_common_overlays(page: Page) -> str | None:
    for selector in overlay_selectors():
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible(timeout=250):
                locator.click(force=True, timeout=1500)
                sleep(page, 150, 350)
                return selector
        except Exception:
            continue
    return None


__all__ = ["dismiss_common_overlays", "humanize_page", "sleep"]
