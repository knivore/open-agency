from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from openai import AzureOpenAI
from pydantic import BaseModel, Field
from typing import Any, Literal

from app.core.storage import upload_to_s3
from app.browser_runtime.client import BrowserRuntimeClient, BrowserRuntimeClientError
from app.browser_runtime.contracts import BrowserOptions, BrowserRuntimePolicy, OwnerClaims


class BrowserOpenInput(BaseModel):
    url: str | None = Field(default=None, description="The URL of the webpage to navigate to.")
    session_id: str | None = Field(default=None, description="Optional owned browser session to navigate.")
    goal: str | None = Field(default=None, description="Optional extraction goal or research question.")
    extract_mode: Literal["auto", "text", "markdown", "article", "html", "none"] = Field(
        default="auto", description="Extraction mode: auto, text, markdown, article, html, or none."
    )
    keep_open: bool = Field(
        default=False,
        description=(
            "Keep a live interactive session after retrieval. Set true only when a later browser action "
            "must reuse this page; extraction-only opens are transient by default."
        ),
    )
    http_credentials_username: str | None = Field(default=None, description="Optional HTTP auth username.")
    http_credentials_password: str | None = Field(default=None, description="Optional HTTP auth password.")
    user_agent: str | None = Field(default=None, description="Optional specific user agent to use.")
    browser_type: str | None = Field(
        default=None,
        description="Optional compatibility marker; only chrome/chromium is supported by the unified runtime.",
    )
    mobile: bool = Field(default=False, description="Whether to use a mobile user agent.")
    viewport_width: int = Field(default=1440, ge=320, le=3840, description="Browser viewport width.")
    viewport_height: int = Field(default=900, ge=240, le=2160, description="Browser viewport height.")
    device_scale_factor: float = Field(default=1.0, ge=0.5, le=3.0, description="Browser device scale factor.")
    locale: str | None = Field(default=None, description="Optional locale override, for example en-US.")
    timezone_id: str | None = Field(default=None,
                                    description="Optional browser timezone ID, for example Asia/Singapore.")
    storage_state_path: str | None = Field(default=None,
                                           description="Optional Playwright storage state JSON file to preload cookies and local storage.")
    record_video: bool = Field(default=False, description="Whether to record browser video artifacts for the session.")
    trace_mode: Literal["off", "on", "retain-on-failure"] | None = Field(
        default=None, description="Tracing mode: off, on, or retain-on-failure."
    )
    extra_http_headers: dict[str, str] | None = Field(default=None,
                                                      description="Optional extra HTTP headers to send with browser requests.")
    proxy_binding: str | None = Field(default=None, description="Opaque Agency proxy credential binding.")
    runtime_policy: BrowserRuntimePolicy = Field(
        default_factory=BrowserRuntimePolicy,
        description="Optional per-open resource preferences bounded by Agency operator limits.",
    )


class BrowserAnalyseInput(BaseModel):
    text: str = Field(..., description="Instruction or question relating to the current screenshot.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


class BrowserExtractInput(BaseModel):
    text: str = Field(..., description="Instruction or query for content extraction.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


class BrowserScrollInput(BaseModel):
    instruction: str | None = Field(default=None, description="Optional element-finding instruction.")
    scroll_direction: str = Field(..., description="Direction to scroll, such as 'scroll down 2 times'.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


class BrowserClickInput(BaseModel):
    instruction: str = Field(..., description="Description of the element to click.")
    sequence_number: int | None = Field(default=None, description="Optional sequence number of the target element.")
    x: float | None = Field(default=None, ge=0, description="Optional screenshot-relative X coordinate for manual clicking.")
    y: float | None = Field(default=None, ge=0, description="Optional screenshot-relative Y coordinate for manual clicking.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


class BrowserSelectInput(BaseModel):
    instruction: str = Field(..., description="Description of the dropdown and option to select.")
    sequence_number: int | None = Field(default=None, description="Optional sequence number of the dropdown element.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


class BrowserInputText(BaseModel):
    instruction: str = Field(..., description="Description of the input element and what to type.")
    sequence_number: int | None = Field(default=None, description="Optional sequence number of the input element.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


class BrowserVerifyInput(BaseModel):
    text: str = Field(..., description="Text to compare against the current page content.")
    session_id: str | None = Field(default=None, description="Optional owned browser session identifier.")


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


def _browser_session_owner(tool_context: Any = None, browser_owner: Any = None) -> OwnerClaims:
    if isinstance(browser_owner, OwnerClaims):
        return browser_owner
    if isinstance(browser_owner, dict):
        return OwnerClaims(
            execution_id=browser_owner.get("execution_id"),
            workflow_id=browser_owner.get("workflow_id"),
            task_id=browser_owner.get("task_id"),
            agent_id=browser_owner.get("agent_id"),
            workspace_id=browser_owner.get("workspace_id"),
            user_id=browser_owner.get("user_id"),
            actor=browser_owner.get("actor"),
        )
    if tool_context is not None and hasattr(tool_context, "safe_metadata"):
        metadata = tool_context.safe_metadata()
        return OwnerClaims(
            execution_id=metadata.get("execution_id"),
            workflow_id=metadata.get("workflow_id"),
            task_id=metadata.get("task_id"),
            agent_id=metadata.get("agent_id"),
        )
    # Direct local calls exist for the CLI and contract bridge. External
    # surfaces must inject an authenticated actor or execution owner.
    return OwnerClaims(actor="direct-local-browser")


def _runtime_owner(tool_context: Any = None, browser_owner: Any = None) -> OwnerClaims:
    return _browser_session_owner(tool_context, browser_owner)


@lru_cache(maxsize=1)
def get_browser_runtime_client() -> BrowserRuntimeClient:
    """Reuse HTTP connections while all durable browser state remains remote."""

    return BrowserRuntimeClient()


def _runtime_session_id(
        session_id: str | None,
        *,
        tool_context: Any = None,
        browser_owner: Any = None,
) -> tuple[str, OwnerClaims]:
    owner = _runtime_owner(tool_context, browser_owner)
    if session_id:
        return session_id, owner
    sessions = get_browser_runtime_client().status(owner=owner).get("sessions", [])
    if not sessions:
        raise BrowserRuntimeClientError("No active browser session exists for this execution or actor")
    if len(sessions) > 1:
        raise BrowserRuntimeClientError("Multiple browser sessions are active; provide session_id explicitly")
    return str(sessions[0]["session_id"]), owner


def _load_storage_state(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    # Storage state is transmitted only to the private runtime and is never
    # included in returned payloads, telemetry, or model-visible diagnostics.
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _extract_input_values(instruction: str) -> list[str]:
    quoted = re.findall(r"['\"]([^'\"]+)['\"]", instruction)
    return quoted
def open_browser(
        url: str | None = None,
        session_id: str | None = None,
        goal: str | None = None,
        extract_mode: str = "auto",
        keep_open: bool = False,
        http_credentials_username: str | None = None,
        http_credentials_password: str | None = None,
        user_agent: str | None = None,
        browser_type: str | None = None,
        mobile: bool = False,
        viewport_width: int = 1440,
        viewport_height: int = 900,
        device_scale_factor: float = 1.0,
        locale: str | None = None,
        timezone_id: str | None = None,
        storage_state_path: str | None = None,
        record_video: bool = False,
        trace_mode: str | None = None,
        extra_http_headers: dict[str, str] | None = None,
        proxy_binding: str | None = None,
        runtime_policy: BrowserRuntimePolicy | dict[str, Any] | None = None,
        img_directory: str | None = None,
        headless_mode: bool | None = None,
        _allowed_hosts: list[str] | None = None,
        tool_context: Any = None,
        _browser_owner: Any = None,
        **_: Any,
) -> Any:
    if not url:
        return {"Error Message": "No URL provided. Please provide a URL through tool initialization or as a parameter."}
    if browser_type and browser_type.lower() not in {"chrome", "chromium"}:
        return {"Error Message": "The unified Patchright runtime supports Chromium profiles only."}
    if os.getenv("BROWSER_UNIFIED_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return {"Error Message": "Unified browser capability is disabled by operator policy."}
    try:
        response = get_browser_runtime_client().open(
            url=url,
            owner=_runtime_owner(tool_context, _browser_owner),
            goal=goal,
            extract_mode=extract_mode,
            keep_open=keep_open,
            session_id=session_id,
            allowed_hosts=list(_allowed_hosts or []),
            options=BrowserOptions(
                headless=True if headless_mode is None else headless_mode,
                mobile=mobile,
                locale=locale,
                timezone_id=timezone_id,
                user_agent=user_agent,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                device_scale_factor=device_scale_factor,
                extra_http_headers=extra_http_headers or {},
                http_credentials=(
                    {"username": http_credentials_username, "password": http_credentials_password}
                    if http_credentials_username and http_credentials_password else None
                ),
                storage_state=_load_storage_state(storage_state_path),
                record_video=record_video,
                trace_mode=trace_mode or "off",
                proxy_binding=proxy_binding,
            ),
            runtime_policy=BrowserRuntimePolicy.model_validate(runtime_policy or {}),
        )
    except (BrowserRuntimeClientError, ValueError, OSError, json.JSONDecodeError) as exc:
        return {"Error Message": str(exc)}
    challenge = response.get("challenge") or {}
    # Compatibility aliases remain during the contract migration; the complete
    # versioned response is returned alongside them.
    response["url"] = response.get("final_url")
    response["challenge_detected"] = challenge.get("kind") if challenge.get("kind") != "none" else None
    response["user_agent"] = user_agent or response.get("user_agent")
    return response


def screenshot(img_directory: str | None = None, full_page_screenshot: bool = True,
               session_id: str | None = None, **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        result = get_browser_runtime_client().action(
            resolved, owner=owner, action="screenshot", full_page=full_page_screenshot
        )
        screenshot_bytes = base64.b64decode(result.pop("screenshot_base64"))
        file_name = _upload_browser_screenshot(
            img_directory,
            screenshot_bytes,
            process_id=kwargs.get("process_id"),
            run_by=kwargs.get("run_by"),
        )
        return {"Success Message": "Screenshot captured successfully.", "session_id": resolved,
                "screenshot_file": file_name}
    except (BrowserRuntimeClientError, ValueError, KeyError) as exc:
        return {"Error Message": str(exc)}


def screenshot_and_analyse(text: str, img_directory: str | None = None, full_page_screenshot: bool = True,
                           session_id: str | None = None, **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        result = get_browser_runtime_client().action(
            resolved, owner=owner, action="screenshot", full_page=full_page_screenshot
        )
        screenshot_bytes = base64.b64decode(result["screenshot_base64"])
    except (BrowserRuntimeClientError, ValueError, KeyError) as exc:
        return {"Error Message": str(exc)}
    _upload_browser_screenshot(img_directory, screenshot_bytes, process_id=kwargs.get("process_id"),
                               run_by=kwargs.get("run_by"))
    prompt = f"Determine whether this instruction is resolvable from the current page and explain why: {text}"
    analysis = _ask_vision(prompt, screenshot_bytes)
    return analysis or f"Instruction received for browser session {resolved}."


def screenshot_and_extract(text: str, img_directory: str | None = None, full_page_screenshot: bool = True,
                           session_id: str | None = None, **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        return get_browser_runtime_client().extract(
            resolved, owner=owner, extract_mode="auto", goal=text, max_chars=100_000
        )
    except (BrowserRuntimeClientError, ValueError) as exc:
        return {"Error Message": str(exc)}


def scroll_page(scroll_direction: str, instruction: str | None = None, session_id: str | None = None,
                **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        return get_browser_runtime_client().action(
            resolved, owner=owner, action="scroll", scroll_direction=scroll_direction, instruction=instruction
        )
    except (BrowserRuntimeClientError, ValueError) as exc:
        return {"Error Message": str(exc)}


def click_element(instruction: str, sequence_number: int | None = None, session_id: str | None = None,
                  x: float | None = None, y: float | None = None,
                  **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        return get_browser_runtime_client().action(
            resolved,
            owner=owner,
            action="mouse_click" if x is not None and y is not None else "click",
            instruction=instruction,
            sequence_number=sequence_number,
            x=x,
            y=y,
        )
    except (BrowserRuntimeClientError, ValueError) as exc:
        return {"Error Message": str(exc)}


def select_dropdown(instruction: str, sequence_number: int | None = None, session_id: str | None = None,
                    **kwargs: Any) -> Any:
    values = _extract_input_values(instruction)
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        return get_browser_runtime_client().action(
            resolved,
            owner=owner,
            action="select",
            instruction=instruction,
            sequence_number=sequence_number,
            value=values[-1] if values else None,
        )
    except (BrowserRuntimeClientError, ValueError) as exc:
        return {"Error Message": str(exc)}


def send_keys(instruction: str, sequence_number: int | None = None, session_id: str | None = None,
              **kwargs: Any) -> Any:
    values = _extract_input_values(instruction)
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        return get_browser_runtime_client().action(
            resolved,
            owner=owner,
            action="type",
            instruction=instruction,
            sequence_number=sequence_number,
            value=values[0] if values else instruction,
        )
    except (BrowserRuntimeClientError, ValueError) as exc:
        return {"Error Message": str(exc)}


def verify_content(text: str, session_id: str | None = None, **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        result = get_browser_runtime_client().action(
            resolved, owner=owner, action="verify", instruction=text
        )
        return {
            "Verification Reasoning": "Expected text was compared against the current page content.",
            "Verification Score": result.get("score", 0),
            "Challenge Detected": False,
            "session_id": resolved,
        }
    except (BrowserRuntimeClientError, ValueError) as exc:
        return {"Error Message": str(exc)}


def terminate_browser(session_id: str | None = None, **kwargs: Any) -> Any:
    try:
        resolved, owner = _runtime_session_id(
            session_id, tool_context=kwargs.get("tool_context"), browser_owner=kwargs.get("_browser_owner")
        )
        result = get_browser_runtime_client().close(resolved, owner=owner)
        return {"Success Message": "Driver terminated successfully.", "session_id": resolved, **result}
    except BrowserRuntimeClientError as exc:
        if "No active browser session" in str(exc):
            return {"Success Message": "Driver terminated successfully."}
        return {"Error Message": str(exc)}


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
