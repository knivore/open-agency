"""Provider-neutral document OCR tool with Baidu Unlimited-OCR as the first adapter.

The public tool contract deliberately returns normalized Markdown and structured-result
URLs instead of exposing a vendor response.  This keeps workflows stable when another
OCR engine is added behind the provider registry.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import time
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.core.outbound_http import validate_outbound_http_url
from app.runtime.native.errors import ToolExecutionError

BAIDU_OAUTH_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_TASK_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/unlimited-ocr-parser/task"
BAIDU_TASK_QUERY_URL = f"{BAIDU_TASK_URL}/query"
BAIDU_API_HOSTS = {"aip.baidubce.com"}
BAIDU_RESULT_HOSTS = {"*.bcebos.com", "*.baidubce.com"}
SUPPORTED_SUFFIXES = frozenset({".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".ofd", ".doc", ".docx", ".txt", ".wps", ".ppt", ".pptx"})
MAX_DIRECT_UPLOAD_BYTES = 100 * 1024 * 1024


class OCRDocumentInput(BaseModel):
    """Input for document parsing; exactly one document source is required."""

    file_path: str | None = Field(default=None, description="Local document path accessible to the backend.")
    file_base64: str | None = Field(
        default=None,
        description="Base64 document bytes. Data URLs are accepted.",
    )
    file_url: str | None = Field(
        default=None,
        description="Public document URL that Baidu can fetch. Do not use for private or access-controlled files.",
    )
    filename: str | None = Field(
        default=None,
        description="Source filename including extension. Required with file_base64 or file_url.",
    )
    provider: Literal["baidu_unlimited_ocr"] = Field(
        default="baidu_unlimited_ocr",
        description="OCR provider adapter. Additional adapters can be registered without changing this tool contract.",
    )
    model: str = Field(
        default="baidu/Unlimited-OCR",
        description="OCR model identifier recorded with the result. The hosted Baidu adapter currently supports baidu/Unlimited-OCR.",
    )
    wait_for_completion: bool = Field(
        default=True,
        description="Wait for the asynchronous parse job. Set false to receive a task id for later status retrieval.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=5,
        le=1200,
        description="Maximum time to wait for Baidu to complete the OCR job.",
    )
    poll_interval_seconds: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Delay between job-status checks while waiting for completion.",
    )
    include_markdown: bool = Field(
        default=True,
        description="Download the generated Markdown when the provider returns it.",
    )
    max_markdown_chars: int = Field(
        default=250_000,
        ge=1_000,
        le=1_000_000,
        description="Maximum Markdown characters returned inline; the result URL is always retained.",
    )

    @model_validator(mode="after")
    def _require_one_source_and_a_filename(self) -> "OCRDocumentInput":
        sources = [self.file_path, self.file_base64, self.file_url]
        if sum(bool(value) for value in sources) != 1:
            raise ValueError("Provide exactly one of file_path, file_base64, or file_url.")
        if (self.file_base64 or self.file_url) and not self.filename:
            raise ValueError("filename is required with file_base64 or file_url.")
        if self.file_url:
            parsed = urlparse(self.file_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("file_url must be an absolute HTTP(S) URL without credentials.")
        if self.provider == "baidu_unlimited_ocr" and self.model != "baidu/Unlimited-OCR":
            raise ValueError("The baidu_unlimited_ocr provider currently supports only baidu/Unlimited-OCR.")
        return self


class OCRProvider(Protocol):
    """Adapter boundary for hosted or self-hosted OCR implementations."""

    async def parse(self, request: OCRDocumentInput) -> dict[str, Any]: ...


class BaiduUnlimitedOCRProvider:
    """Official Baidu Cloud adapter for the Unlimited-OCR asynchronous API."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self._http_client = http_client

    async def parse(self, request: OCRDocumentInput) -> dict[str, Any]:
        if not self.api_key or not self.secret_key:
            raise ToolExecutionError("BAIDU_OCR_API_KEY and BAIDU_OCR_SECRET_KEY are required for Unlimited-OCR.")

        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=False)
        try:
            access_token = await self._access_token(client)
            task = await self._create_task(client, access_token, request)
            task_id = _task_id(task)
            if not request.wait_for_completion:
                return _result_payload(request, task_id=task_id, status="submitted")

            result = await self._wait_for_result(client, access_token, task_id, request)
            status = str(result.get("status") or "unknown").lower()
            payload = _result_payload(request, task_id=task_id, status=status, result=result)
            if status == "success" and request.include_markdown and payload.get("markdown_url"):
                markdown = await self._download_markdown(client, str(payload["markdown_url"]))
                payload["markdown"], payload["markdown_truncated"] = _truncate(markdown, request.max_markdown_chars)
            return payload
        finally:
            if owns_client:
                await client.aclose()

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        _validate_baidu_url(BAIDU_OAUTH_URL, BAIDU_API_HOSTS)
        response = await client.post(
            BAIDU_OAUTH_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.api_key,
                "client_secret": self.secret_key,
            },
        )
        payload = _response_json(response, "Baidu OAuth")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ToolExecutionError(_baidu_error("Baidu OAuth did not return an access token", payload))
        return token

    async def _create_task(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        request: OCRDocumentInput,
    ) -> dict[str, Any]:
        _validate_baidu_url(BAIDU_TASK_URL, BAIDU_API_HOSTS)
        filename, source_field, source_value = _document_source(request)
        response = await client.post(
            BAIDU_TASK_URL,
            params={"access_token": access_token},
            data={source_field: source_value, "file_name": filename},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return _response_json(response, "Baidu Unlimited-OCR task submission", require_success=True)

    async def _wait_for_result(
        self,
        client: httpx.AsyncClient,
        access_token: str,
        task_id: str,
        request: OCRDocumentInput,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + request.timeout_seconds
        while True:
            _validate_baidu_url(BAIDU_TASK_QUERY_URL, BAIDU_API_HOSTS)
            response = await client.post(
                BAIDU_TASK_QUERY_URL,
                params={"access_token": access_token},
                data={"task_id": task_id},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            payload = _response_json(response, "Baidu Unlimited-OCR task query", require_success=True)
            result = payload.get("result")
            if not isinstance(result, dict):
                raise ToolExecutionError("Baidu Unlimited-OCR task query did not return a result object.")
            status = str(result.get("status") or "").lower()
            if status in {"success", "failed"}:
                return result
            if time.monotonic() >= deadline:
                return result
            await asyncio.sleep(min(request.poll_interval_seconds, max(0.0, deadline - time.monotonic())))

    async def _download_markdown(self, client: httpx.AsyncClient, markdown_url: str) -> str:
        # Result links originate from Baidu, but the explicit object-storage allowlist
        # preserves Agency's SSRF boundary if an upstream response is ever malformed.
        _validate_baidu_url(markdown_url, BAIDU_RESULT_HOSTS)
        response = await client.get(markdown_url)
        if response.is_error:
            raise ToolExecutionError(f"Baidu Unlimited-OCR Markdown download failed with HTTP {response.status_code}.")
        return response.text


def registered_ocr_providers(
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, OCRProvider]:
    """Build the provider registry at execution time so credentials are never cached in module state."""

    settings = get_settings()
    return {
        "baidu_unlimited_ocr": BaiduUnlimitedOCRProvider(
            api_key=settings.baidu_ocr_api_key or os.getenv("BAIDU_OCR_API_KEY", ""),
            secret_key=settings.baidu_ocr_secret_key or os.getenv("BAIDU_OCR_SECRET_KEY", ""),
            http_client=http_client,
        )
    }


async def recognize_document(
    *,
    file_path: str | None = None,
    file_base64: str | None = None,
    file_url: str | None = None,
    filename: str | None = None,
    provider: str = "baidu_unlimited_ocr",
    model: str = "baidu/Unlimited-OCR",
    wait_for_completion: bool = True,
    timeout_seconds: int = 300,
    poll_interval_seconds: float = 5.0,
    include_markdown: bool = True,
    max_markdown_chars: int = 250_000,
    _provider: OCRProvider | None = None,
) -> dict[str, Any]:
    """Parse an image or document through the configured OCR adapter."""

    request = OCRDocumentInput.model_validate(
        {
            "file_path": file_path,
            "file_base64": file_base64,
            "file_url": file_url,
            "filename": filename,
            "provider": provider,
            "model": model,
            "wait_for_completion": wait_for_completion,
            "timeout_seconds": timeout_seconds,
            "poll_interval_seconds": poll_interval_seconds,
            "include_markdown": include_markdown,
            "max_markdown_chars": max_markdown_chars,
        }
    )
    adapter = _provider or registered_ocr_providers().get(request.provider)
    if adapter is None:
        raise ToolExecutionError(f"OCR provider is not registered: {request.provider}")
    return await adapter.parse(request)


def _document_source(request: OCRDocumentInput) -> tuple[str, str, str]:
    if request.file_url:
        return _validated_filename(request.filename or "document"), "file_url", request.file_url
    if request.file_path:
        source_path = Path(request.file_path).expanduser().resolve()
        if not source_path.is_file():
            raise ToolExecutionError(f"OCR source file was not found: {source_path}")
        size = source_path.stat().st_size
        if size > MAX_DIRECT_UPLOAD_BYTES:
            raise ToolExecutionError("Documents larger than 100 MB must be supplied with file_url for Unlimited-OCR.")
        return _validated_filename(source_path.name), "file_data", base64.b64encode(source_path.read_bytes()).decode("ascii")
    raw_base64 = _base64_payload(request.file_base64 or "")
    if len(raw_base64) > MAX_DIRECT_UPLOAD_BYTES:
        raise ToolExecutionError("Documents larger than 100 MB must be supplied with file_url for Unlimited-OCR.")
    return _validated_filename(request.filename or "document"), "file_data", base64.b64encode(raw_base64).decode("ascii")


def _validated_filename(filename: str) -> str:
    safe_name = Path(filename).name
    if not safe_name or safe_name in {".", ".."} or Path(safe_name).suffix.lower() not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ToolExecutionError(f"Unsupported OCR document type. Supported extensions: {supported}.")
    return safe_name


def _base64_payload(value: str) -> bytes:
    payload = value.strip()
    if payload.lower().startswith("data:") and "," in payload:
        _, payload = payload.split(",", 1)
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolExecutionError("file_base64 must contain valid base64 document bytes.") from exc


def _response_json(response: httpx.Response, operation: str, *, require_success: bool = False) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ToolExecutionError(f"{operation} returned a non-JSON response (HTTP {response.status_code}).") from exc
    if not isinstance(payload, dict):
        raise ToolExecutionError(f"{operation} returned an invalid response payload.")
    if response.is_error or (require_success and payload.get("error_code") not in {None, 0, "0"}):
        raise ToolExecutionError(_baidu_error(f"{operation} failed", payload, response.status_code))
    return payload


def _baidu_error(prefix: str, payload: dict[str, Any], status_code: int | None = None) -> str:
    detail = str(payload.get("error_msg") or payload.get("error_description") or "unknown error")
    code = payload.get("error_code") or payload.get("error")
    suffix = f" (HTTP {status_code})" if status_code else ""
    return f"{prefix}: {detail}" + (f" [code: {code}]" if code else "") + suffix


def _task_id(task_response: dict[str, Any]) -> str:
    result = task_response.get("result")
    task_id = result.get("task_id") if isinstance(result, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise ToolExecutionError("Baidu Unlimited-OCR task submission did not return a task id.")
    return task_id


def _result_payload(
    request: OCRDocumentInput,
    *,
    task_id: str,
    status: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = result or {}
    return {
        "status": status,
        "provider": request.provider,
        "model": request.model,
        "task_id": task_id,
        "markdown_url": result.get("markdown_url"),
        "parse_result_url": result.get("parse_result_url"),
        "task_error": result.get("task_error"),
    }


def _truncate(value: str, limit: int) -> tuple[str, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _validate_baidu_url(url: str, allowed_hosts: set[str]) -> None:
    # urlparse protects against accidentally treating a relative task path as remote.
    if not urlparse(url).hostname:
        raise ToolExecutionError("OCR provider returned an invalid URL.")
    try:
        validate_outbound_http_url(url, allowed_hosts=allowed_hosts)
    except ValueError as exc:
        raise ToolExecutionError(f"OCR provider URL was rejected: {exc}") from exc


__all__ = [
    "BaiduUnlimitedOCRProvider",
    "OCRDocumentInput",
    "OCRProvider",
    "recognize_document",
    "registered_ocr_providers",
]
