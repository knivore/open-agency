from __future__ import annotations

import base64
import os
import random
import re
from datetime import datetime
from openai import AzureOpenAI
from playwright.sync_api import Page
from pydantic import BaseModel, Field
from typing import Any
from urllib.parse import urlparse

from app.core.storage import upload_to_s3
from app.core.outbound_http import validate_outbound_http_url
from .browser_interaction import dismiss_common_overlays, humanize_page, sleep
from .browser_navigation import goto_with_readiness
from .browser_session import BrowserSessionManager
from .browser_session_state import BrowserActionResult
from .browser_signals import detect_page_challenge


class BrowserOpenInput(BaseModel):
    url: str | None = Field(default=None, description="The URL of the webpage to navigate to.")
    http_credentials_username: str | None = Field(default=None, description="Optional HTTP auth username.")
    http_credentials_password: str | None = Field(default=None, description="Optional HTTP auth password.")
    user_agent: str | None = Field(default=None, description="Optional specific user agent to use.")
    browser_type: str | None = Field(default=None, description="Optional browser type to emulate.")
    mobile: bool = Field(default=False, description="Whether to use a mobile user agent.")
    locale: str | None = Field(default=None, description="Optional locale override, for example en-US.")
    timezone_id: str | None = Field(default=None,
                                    description="Optional browser timezone ID, for example Asia/Singapore.")
    storage_state_path: str | None = Field(default=None,
                                           description="Optional Playwright storage state JSON file to preload cookies and local storage.")
    record_video: bool = Field(default=False, description="Whether to record browser video artifacts for the session.")
    trace_mode: str | None = Field(default=None, description="Tracing mode: off, on, or retain-on-failure.")
    extra_http_headers: dict[str, str] | None = Field(default=None,
                                                      description="Optional extra HTTP headers to send with browser requests.")


class BrowserAnalyseInput(BaseModel):
    text: str = Field(..., description="Instruction or question relating to the current screenshot.")


class BrowserExtractInput(BaseModel):
    text: str = Field(..., description="Instruction or query for content extraction.")


class BrowserScrollInput(BaseModel):
    instruction: str | None = Field(default=None, description="Optional element-finding instruction.")
    scroll_direction: str = Field(..., description="Direction to scroll, such as 'scroll down 2 times'.")


class BrowserClickInput(BaseModel):
    instruction: str = Field(..., description="Description of the element to click.")
    sequence_number: int | None = Field(default=None, description="Optional sequence number of the target element.")


class BrowserSelectInput(BaseModel):
    instruction: str = Field(..., description="Description of the dropdown and option to select.")
    sequence_number: int | None = Field(default=None, description="Optional sequence number of the dropdown element.")


class BrowserInputText(BaseModel):
    instruction: str = Field(..., description="Description of the input element and what to type.")
    sequence_number: int | None = Field(default=None, description="Optional sequence number of the input element.")


class BrowserVerifyInput(BaseModel):
    text: str = Field(..., description="Text to compare against the current page content.")


class _UserAgentGenerator:
    CHROME_VERSIONS = ["121.0.6167.160", "122.0.6261.94", "123.0.6312.86", "124.0.6367.102"]
    FIREFOX_VERSIONS = ["122.0", "123.0", "124.0"]
    SAFARI_VERSIONS = ["17.3", "17.4"]
    EDGE_VERSIONS = ["121.0.2277.112", "122.0.2365.66", "123.0.2420.53"]
    PLATFORMS = {
        "windows": ["Windows NT 10.0; Win64; x64", "Windows NT 6.1; Win64; x64"],
        "mac": ["Macintosh; Intel Mac OS X 10_15_7", "Macintosh; Intel Mac OS X 13_2_1"],
        "linux": ["X11; Linux x86_64", "X11; Ubuntu; Linux x86_64"],
    }
    MOBILE_DEVICES = {
        "iphone": ["iPhone; CPU iPhone OS 16_0 like Mac OS X", "iPhone; CPU iPhone OS 17_1 like Mac OS X"],
        "android": ["Linux; Android 12; Pixel 6", "Linux; Android 14; Pixel 8 Pro"],
    }

    @classmethod
    def generate(cls, browser_type: str | None = None, mobile: bool = False) -> str:
        browser = (browser_type or "chrome").lower()
        if browser == "firefox":
            version = random.choice(cls.FIREFOX_VERSIONS)
            platform = random.choice(cls.MOBILE_DEVICES["android"] if mobile else cls.PLATFORMS["linux"])
            return f"Mozilla/5.0 ({platform}; rv:{version}) Gecko/20100101 Firefox/{version}"
        if browser in {"safari", "webkit"}:
            version = random.choice(cls.SAFARI_VERSIONS)
            if mobile:
                platform = random.choice(cls.MOBILE_DEVICES["iphone"])
                return f"Mozilla/5.0 ({platform}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version} Mobile/15E148 Safari/604.1"
            platform = random.choice(cls.PLATFORMS["mac"])
            return f"Mozilla/5.0 ({platform}) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{version} Safari/605.1.15"
        if browser == "edge":
            version = random.choice(cls.EDGE_VERSIONS)
            platform = random.choice(cls.PLATFORMS["windows"])
            return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/{version}"
        version = random.choice(cls.CHROME_VERSIONS)
        platform = random.choice(cls.MOBILE_DEVICES["android"] if mobile else cls.PLATFORMS["windows"])
        mobile_suffix = " Mobile" if mobile else ""
        return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version}{mobile_suffix} Safari/537.36"


def _timestamped_name() -> str:
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"


def _upload_browser_screenshot(directory: str | None, screenshot: bytes, *, process_id: str | None = None,
                               run_by: str | None = None) -> str | None:
    image_directory = directory or os.getenv("IMAGE_ASSET_DIR", "output/images")
    if not image_directory:
        return None
    file_name = _timestamped_name()
    upload_to_s3(image_directory, process_id, run_by, [screenshot], [file_name])
    return file_name


def _capture_screenshot_artifact(
        page: Page,
        img_directory: str | None,
        *,
        process_id: str | None = None,
        run_by: str | None = None,
        full_page: bool = False,
) -> BrowserActionResult:
    screenshot_bytes = page.screenshot(type="png", full_page=full_page)
    file_name = _upload_browser_screenshot(img_directory, screenshot_bytes, process_id=process_id, run_by=run_by)
    return BrowserActionResult(artifacts={"screenshot_file": file_name} if file_name else {})


def _get_llm_client() -> AzureOpenAI | None:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    if not endpoint or not api_key:
        return None
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_key=api_key,
        api_version="2024-05-01-preview",
    )


def _ask_vision(prompt: str, screenshot: bytes) -> str:
    client = _get_llm_client()
    if client is None:
        return prompt
    screenshot_base64 = base64.b64encode(screenshot).decode("utf-8")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Answer based on the screenshot and current page state."},
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"}},
                    {"type": "text", "text": prompt},
                ],
            },
        ],
        max_tokens=512,
    )
    return response.choices[0].message.content or ""


def _resolve_page() -> Page:
    return BrowserSessionManager().get_page()


def _extract_text_hint(instruction: str) -> str:
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", instruction)
    if quoted:
        return quoted[0]
    cleaned = re.sub(r"\b(click|select|choose|pick|type|enter|fill|into|the|on|field|input|button|dropdown)\b", "",
                     instruction, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip() or instruction.strip()


def _extract_input_values(instruction: str) -> list[str]:
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", instruction)
    return quoted


def _find_element_by_sequence(page: Page, sequence_number: int):
    return page.locator(f'[data-element-id="{sequence_number}"]').first


def _find_clickable(page: Page, instruction: str):
    hint = _extract_text_hint(instruction)
    for locator in [
        page.get_by_role("button", name=re.compile(re.escape(hint), re.IGNORECASE)),
        page.get_by_role("link", name=re.compile(re.escape(hint), re.IGNORECASE)),
        page.get_by_text(re.compile(re.escape(hint), re.IGNORECASE)),
        page.locator(f'text="{hint}"').first,
        page.locator(f'[aria-label*="{hint}" i]').first,
    ]:
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def _find_input(page: Page, instruction: str):
    hint = _extract_text_hint(instruction)
    for locator in [
        page.get_by_label(re.compile(re.escape(hint), re.IGNORECASE)),
        page.get_by_placeholder(re.compile(re.escape(hint), re.IGNORECASE)),
        page.locator(f'input[aria-label*="{hint}" i], textarea[aria-label*="{hint}" i]').first,
        page.locator("input, textarea").first,
    ]:
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def _find_select(page: Page, instruction: str):
    hint = _extract_text_hint(instruction)
    for locator in [
        page.get_by_label(re.compile(re.escape(hint), re.IGNORECASE)),
        page.locator(f'select[aria-label*="{hint}" i]').first,
        page.locator("select").first,
    ]:
        try:
            if locator.count() > 0:
                return locator.first
        except Exception:
            continue
    return None


def open_browser(
        url: str | None = None,
        http_credentials_username: str | None = None,
        http_credentials_password: str | None = None,
        user_agent: str | None = None,
        browser_type: str | None = None,
        mobile: bool = False,
        locale: str | None = None,
        timezone_id: str | None = None,
        storage_state_path: str | None = None,
        record_video: bool = False,
        trace_mode: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        img_directory: str | None = None,
        headless_mode: bool | None = None,
        _allowed_hosts: list[str] | None = None,
        **_: Any,
) -> Any:
    if not url:
        return {"Error Message": "No URL provided. Please provide a URL through tool initialization or as a parameter."}
    manager = BrowserSessionManager()
    context_options: dict[str, Any] = {"user_agent": user_agent or _UserAgentGenerator.generate(browser_type, mobile)}
    if http_credentials_username and http_credentials_password:
        context_options["http_credentials"] = {"username": http_credentials_username,
                                               "password": http_credentials_password}
    if locale:
        context_options["locale"] = locale
    if timezone_id:
        context_options["timezone_id"] = timezone_id
    if extra_http_headers:
        context_options["extra_http_headers"] = extra_http_headers
    page = manager.start_browser(
        headless_mode=headless_mode,
        browser_type=browser_type or "chromium",
        context_options=context_options,
        session_options={
            "storage_state_path": storage_state_path,
            "record_video": record_video,
            "trace_mode": trace_mode,
            "trace_name": re.sub(r"[^a-zA-Z0-9_-]+", "-", page_url_host(url)),
        },
    )
    allowed_hosts = list(_allowed_hosts or [])
    if manager.context is None:
        raise RuntimeError("Browser context was not initialized")

    def enforce_request_destination(route: Any, request: Any) -> None:
        parsed = urlparse(request.url)
        if parsed.scheme in {"data", "blob", "about"}:
            route.continue_()
            return
        try:
            # Playwright applies this to redirects and subresources as well as
            # the initial navigation, closing policy gaps after page.goto().
            validate_outbound_http_url(request.url, allowed_hosts=allowed_hosts)
        except ValueError:
            route.abort("blockedbyclient")
            return
        route.continue_()

    manager.context.route("**/*", enforce_request_destination)
    navigation_state = goto_with_readiness(page, url)
    manager.merge_result(navigation_state)
    manager.merge_result(_capture_screenshot_artifact(page, img_directory))
    session_details = manager.get_session_details()
    return {
        "url": page.url,
        "title": page.title(),
        "user_agent": context_options["user_agent"],
        "runtime_root": session_details["runtime_root"],
        "trace_path": session_details["trace_path"],
        "overlay_dismissed": session_details["session_state"]["signals"].get("overlay_dismissed"),
        "challenge_detected": session_details["session_state"]["signals"].get("challenge_detected"),
        "session_state": session_details["session_state"],
        "message": "Browser started and URL loaded successfully.",
    }


def page_url_host(url: str) -> str:
    parsed = urlparse(url)
    sanitized = parsed.netloc or re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/", 1)[0]
    return sanitized or "browser-session"


def screenshot(img_directory: str | None = None, full_page_screenshot: bool = True, **kwargs: Any) -> Any:
    page = _resolve_page()
    BrowserSessionManager().merge_result(
        _capture_screenshot_artifact(
            page,
            img_directory,
            process_id=kwargs.get("process_id"),
            run_by=kwargs.get("run_by"),
            full_page=full_page_screenshot,
        )
    )
    return "{'Success Message': 'Screenshot captured and uploaded to storage directory successfully.'}"


def screenshot_and_analyse(text: str, img_directory: str | None = None, full_page_screenshot: bool = True,
                           **kwargs: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    screenshot_bytes = page.screenshot(type="png", full_page=full_page_screenshot)
    file_name = _upload_browser_screenshot(img_directory, screenshot_bytes, process_id=kwargs.get("process_id"),
                                           run_by=kwargs.get("run_by"))
    if file_name:
        manager.record_artifact("screenshot_file", file_name)
    prompt = f"Determine whether this instruction is resolvable from the current page and explain why: {text}"
    analysis = _ask_vision(prompt, screenshot_bytes)
    return analysis or f"Instruction received. Current page title: {page.title()}. URL: {page.url}"


def screenshot_and_extract(text: str, img_directory: str | None = None, full_page_screenshot: bool = True,
                           **kwargs: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    screenshot_bytes = page.screenshot(type="png", full_page=full_page_screenshot)
    file_name = _upload_browser_screenshot(img_directory, screenshot_bytes, process_id=kwargs.get("process_id"),
                                           run_by=kwargs.get("run_by"))
    if file_name:
        manager.record_artifact("screenshot_file", file_name)
    page_text = page.locator("body").inner_text(timeout=5000)
    summary = _ask_vision(f"Extract content relevant to this instruction from the page: {text}", screenshot_bytes)
    return {
        "page_type": "generic",
        "page_url": page.url,
        "content": {
            "summary": summary or "",
            "text": page_text[:4000],
        },
    }


def scroll_page(scroll_direction: str, instruction: str | None = None, **_: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    humanize_page(page)
    match = re.search(r"(up|down)\s*(\d+)?", scroll_direction.lower())
    direction = match.group(1) if match else "down"
    times = int(match.group(2) or 1) if match else 1
    delta = -800 if direction == "up" else 800
    for _ in range(times):
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(300)
    manager.record_signal("last_scroll_direction", direction)
    manager.record_signal("last_scroll_times", times)
    return f"Scrolled {direction} {times} time(s).{f' Instruction context: {instruction}' if instruction else ''}"


def click_element(instruction: str, sequence_number: int | None = None, **_: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    locator = _find_element_by_sequence(page, sequence_number) if sequence_number is not None else _find_clickable(page,
                                                                                                                   instruction)
    if locator is None:
        return "Error! No element found matches the description/instruction. Please try to scroll and find element"
    locator.scroll_into_view_if_needed()
    dismiss_common_overlays(page)
    humanize_page(page)
    locator.hover(timeout=2000)
    sleep(page, 80, 220)
    locator.click(force=True)
    sleep(page, 120, 300)
    manager.record_signal("last_clicked_instruction", instruction)
    manager.record_signal("last_clicked_sequence_number", sequence_number)
    return f"Clicked on element {sequence_number}" if sequence_number is not None else f"Clicked element matching instruction: {instruction}"


def select_dropdown(instruction: str, sequence_number: int | None = None, **_: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    locator = _find_element_by_sequence(page, sequence_number) if sequence_number is not None else _find_select(page,
                                                                                                                instruction)
    if locator is None:
        return "Error! No matching dropdown found. Please verify the element exists and is visible."
    option_candidates = _extract_input_values(instruction)
    option_text = option_candidates[-1] if option_candidates else _extract_text_hint(instruction)
    locator.scroll_into_view_if_needed()
    dismiss_common_overlays(page)
    humanize_page(page)
    try:
        locator.select_option(label=option_text)
    except Exception:
        try:
            locator.select_option(value=option_text)
        except Exception as exc:
            return f"Error! Could not select dropdown option. Error: {exc}"
    manager.record_signal("last_selected_option", option_text)
    manager.record_signal("last_selected_sequence_number", sequence_number)
    return f"Dropdown selection succeeded with option: {option_text}"


def send_keys(instruction: str, sequence_number: int | None = None, **_: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    locator = _find_element_by_sequence(page, sequence_number) if sequence_number is not None else _find_input(page,
                                                                                                               instruction)
    if locator is None:
        return "Error! No element found matches the description/instruction. Please try to scroll and find element"
    values = _extract_input_values(instruction)
    value = values[0] if values else instruction
    locator.scroll_into_view_if_needed()
    dismiss_common_overlays(page)
    humanize_page(page)
    locator.click(timeout=2000)
    sleep(page, 60, 180)
    locator.fill("")
    locator.type(value, delay=random.randint(40, 110))
    page.wait_for_timeout(300)
    manager.record_signal("last_input_instruction", instruction)
    manager.record_signal("last_input_sequence_number", sequence_number)
    return f"Text field is successfully inputted. Value entered: {value}"


def verify_content(text: str, **_: Any) -> Any:
    page = _resolve_page()
    manager = BrowserSessionManager()
    page_text = page.locator("body").inner_text(timeout=5000)
    normalized_page = page_text.lower()
    normalized_text = text.lower()
    challenge = detect_page_challenge(page)
    manager.record_signal("challenge_detected", challenge)
    if normalized_text in normalized_page:
        return {
            "Verification Reasoning": "Expected text found in page content.",
            "Verification Score": 100,
            "Challenge Detected": challenge,
        }
    score = 70 if all(token in normalized_page for token in normalized_text.split() if token) else 0
    return {
        "Verification Reasoning": "Expected text was compared against the current page content.",
        "Verification Score": score,
        "Challenge Detected": challenge,
    }


def terminate_browser(**_: Any) -> Any:
    BrowserSessionManager().stop_browser()
    return {"Success Message": "Driver terminated successfully."}


__all__ = [
    "BrowserAnalyseInput",
    "BrowserClickInput",
    "BrowserExtractInput",
    "BrowserInputText",
    "BrowserOpenInput",
    "BrowserScrollInput",
    "BrowserSelectInput",
    "BrowserVerifyInput",
    "click_element",
    "open_browser",
    "screenshot",
    "screenshot_and_analyse",
    "screenshot_and_extract",
    "scroll_page",
    "select_dropdown",
    "send_keys",
    "terminate_browser",
    "verify_content",
]
