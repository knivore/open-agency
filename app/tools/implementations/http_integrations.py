from __future__ import annotations

import json
import os
import requests
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from typing import Any

from app.tools.input_mapping import convert_str_to_dict, interpolate


class CustomAPIInput(BaseModel):
    url: str = Field(..., description="The specific URL for the API call")
    method: str = Field(..., description="HTTP method to use")
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers")
    query_params: dict[str, Any] | None = Field(default=None, description="Query parameters")
    body: Any | None = Field(default=None, description="Request body")
    verify_ssl: bool = Field(default=True, description="Whether to verify TLS certificates")


def execute_custom_api(
        url: str,
        method: str,
        headers: dict[str, str] | None = None,
        query_params: dict[str, Any] | None = None,
        body: Any | None = None,
        verify_ssl: bool = True,
        auth: Any | None = None,
        **kwargs: Any,
) -> dict[str, Any]:
    resolved_url = interpolate(str(url).rstrip("/"), kwargs)
    resolved_method = method.upper()
    resolved_headers = {**convert_str_to_dict(headers or {}), **convert_str_to_dict(kwargs.get("headers") or {})}
    resolved_headers = interpolate(resolved_headers, kwargs)
    resolved_query_params = interpolate({**(query_params or {}), **(kwargs.get("query_params") or {})}, kwargs)
    resolved_body = body
    if isinstance(resolved_body, list):
        resolved_body = [{key: interpolate(value, kwargs) for key, value in item.items()} for item in resolved_body]
    else:
        resolved_body = interpolate(resolved_body, kwargs)

    response = requests.request(
        method=resolved_method,
        url=resolved_url,
        headers=resolved_headers,
        auth=auth,
        params=resolved_query_params,
        json=resolved_body,
        verify=verify_ssl,
    )

    content_type = response.headers.get("Content-Type", "")
    payload = response.json() if "application/json" in content_type else response.text
    return {"status_code": response.status_code, "response": payload}
