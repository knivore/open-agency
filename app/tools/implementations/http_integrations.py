from __future__ import annotations

import requests
import re
import json
from pydantic import BaseModel, Field
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import uuid4

from app.core.config import get_settings
from app.core.outbound_http import validate_outbound_http_url
from app.core.onecli_http import ONECLI_BLOCKED_HEADER_NAMES, ONECLI_BLOCKED_QUERY_PARAM_NAMES
from app.integrations.onecli import build_onecli_proxy_url
from app.tools.input_mapping import convert_str_to_dict

ONECLI_CORRELATION_HEADER_NAMES = {
    "X-Agency-OneCLI-Correlation-ID",
    "X-Agency-User-ID",
    "X-Agency-Execution-ID",
    "X-Agency-Workflow-ID",
    "X-Agency-Task-ID",
    "X-Agency-Agent-ID",
    "X-Agency-Tool-Call-ID",
}

_KNOWN_TEMPLATE_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)\}")


def _interpolate_http_value(value: Any, context: dict[str, Any]) -> Any:
    """Replace known connector placeholders without treating literal JSON as a template."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if token in context:
                return str(context[token])
            if token.startswith("target_scope[") and token.endswith("]"):
                key = token[len("target_scope["):-1].strip("'\"")
                target_scope = context.get("target_scope")
                if isinstance(target_scope, dict) and key in target_scope:
                    return str(target_scope[key])
            return match.group(0)

        return _KNOWN_TEMPLATE_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: _interpolate_http_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_interpolate_http_value(item, context) for item in value]
    return value


def _coerce_json_body(value: Any) -> Any:
    """Accept model-produced JSON strings while leaving ordinary text bodies unchanged."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return value
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, (dict, list)) else value


class CustomAPIInput(BaseModel):
    url: str = Field(..., description="The specific URL for the API call")
    method: str = Field(..., description="HTTP method to use")
    headers: dict[str, str] | None = Field(default=None, description="HTTP headers")
    query_params: dict[str, Any] | None = Field(default=None, description="Query parameters")
    body: Any | None = Field(default=None, description="Request body")
    verify_ssl: bool = Field(default=True, description="Whether to verify TLS certificates")
    credential_mode: Literal["none", "onecli"] | None = Field(
        default=None,
        description="Credential handling mode. Use onecli to route through the OneCLI gateway.",
    )


def execute_custom_api(
        url: str,
        method: str,
        headers: dict[str, str] | None = None,
        query_params: dict[str, Any] | None = None,
        body: Any | None = None,
        verify_ssl: bool = True,
        auth: Any | None = None,
        proxies: dict[str, str] | None = None,
        ca_bundle_path: str | None = None,
        credential_mode: str | None = None,
        emit_onecli_events: bool = True,
        tool_context: Any | None = None,
        **kwargs: Any,
) -> dict[str, Any]:
    trusted_context = _connector_interpolation_context(tool_context)
    collisions = sorted(set(kwargs).intersection(trusted_context))
    if collisions:
        # Runtime-selected connector identity is authoritative; rejecting the
        # collision makes attempted tenant/credential substitution auditable.
        raise ValueError(
            "Dynamic HTTP arguments cannot override connector context keys: " + ", ".join(collisions)
        )
    interpolation_context = {**kwargs, **trusted_context}

    resolved_url = _interpolate_http_value(str(url).rstrip("/"), interpolation_context)
    resolved_method = method.upper()
    resolved_headers = {**convert_str_to_dict(headers or {}), **convert_str_to_dict(kwargs.get("headers") or {})}
    resolved_headers = _interpolate_http_value(resolved_headers, interpolation_context)
    resolved_query_params = _interpolate_http_value(
        {**(query_params or {}), **(kwargs.get("query_params") or {})},
        interpolation_context,
    )
    resolved_body = _interpolate_http_value(_coerce_json_body(body), interpolation_context)

    settings = get_settings()
    resolved_credential_mode = str(credential_mode or "").strip().lower()
    if not resolved_credential_mode:
        resolved_credential_mode = "onecli" if settings.onecli_force_for_http_tools else "none"
    onecli_metadata: dict[str, Any] | None = None
    if resolved_credential_mode == "onecli":
        _reject_onecli_direct_credentials(resolved_headers, resolved_query_params)
        fallback_secret_ref = (
            settings.onecli_agent_token_secret_ref
            if settings.onecli_allow_global_agent_token_fallback
            else None
        )
        onecli_metadata = _onecli_http_metadata(
            url=resolved_url,
            gateway_url=settings.onecli_gateway_url,
            agent_token_secret_ref_configured=bool(fallback_secret_ref),
            tool_context=tool_context,
            correlation_id=f"onecli-http:{uuid4()}",
            agent_identity={
                "mapping": "development_global_fallback" if fallback_secret_ref else "none",
                "agent_token_secret_ref_configured": bool(fallback_secret_ref),
            },
        )
        if settings.onecli_external_calls_disabled:
            if emit_onecli_events:
                _publish_onecli_http_event(
                    lifecycle_type="onecli.http.request.denied",
                    verdict="deny",
                    onecli_metadata=onecli_metadata,
                    extra={"denial_reasons": ["ONECLI_EXTERNAL_CALLS_DISABLED is true"]},
                )
            raise ValueError("OneCLI-routed external calls are disabled by ONECLI_EXTERNAL_CALLS_DISABLED.")
        if not settings.onecli_enabled:
            raise ValueError("OneCLI credential mode requested, but ONECLI_ENABLED is false.")
        if proxies is None:
            proxy_url = build_onecli_proxy_url(
                settings.onecli_gateway_url,
                fallback_secret_ref,
            )
            proxies = {"http": proxy_url, "https": proxy_url}
        # OneCLI proxy presents its own certificate chain for tunneled HTTPS.
        if ca_bundle_path is None:
            ca_bundle_path = settings.onecli_gateway_ca_bundle_path
        resolved_headers = {
            **resolved_headers,
            **build_onecli_correlation_headers(onecli_metadata),
        }
        if emit_onecli_events:
            _publish_onecli_http_event(
                lifecycle_type="onecli.http.request.started",
                verdict=None,
                onecli_metadata=onecli_metadata,
            )

    # Reject credential-policy violations before URL policy so callers receive
    # the most actionable denial, but validate the destination before any I/O.
    validate_outbound_http_url(resolved_url, allowed_hosts=settings.parsed_tool_http_allowed_hosts)

    # Discord rejects message content over 2,000 characters. Models can
    # occasionally miscount escaped JSON content, so split the exact content
    # at the transport boundary for the channel-message endpoint. This keeps
    # delivery lossless while preserving the normal one-call behavior for all
    # other HTTP APIs.
    discord_chunks = _discord_message_chunks(resolved_url, resolved_method, resolved_body)
    responses = []
    try:
        for request_body in discord_chunks or [resolved_body]:
            response = requests.request(
                method=resolved_method,
                url=resolved_url,
                headers=resolved_headers,
                auth=auth,
                params=resolved_query_params,
                json=request_body,
                verify=ca_bundle_path or verify_ssl,
                proxies=proxies,
                allow_redirects=False,
                timeout=30,
            )
            responses.append(response)
            if response.status_code >= 400:
                break
    except Exception as exc:
        if onecli_metadata is not None and emit_onecli_events:
            fail_closed = resolved_credential_mode == "onecli" and settings.app_env == "production"
            _publish_onecli_http_event(
                lifecycle_type="onecli.http.request.failed",
                verdict="deny" if fail_closed else "warn",
                onecli_metadata=onecli_metadata,
                extra={"error_type": exc.__class__.__name__, "fail_closed": fail_closed},
            )
        raise

    response = responses[-1]
    content_type = response.headers.get("Content-Type", "")
    payload = response.json() if "application/json" in content_type else response.text
    result = {"status_code": response.status_code, "response": payload}
    if discord_chunks and response.status_code < 400:
        result["discord_chunks_sent"] = len(responses)
    if onecli_metadata is not None:
        if emit_onecli_events:
            lifecycle_type = (
                "onecli.http.request.rate_limited"
                if response.status_code == 429
                else "onecli.http.request.completed"
            )
            _publish_onecli_http_event(
                lifecycle_type=lifecycle_type,
                verdict="warn" if response.status_code == 429 else "ok",
                onecli_metadata=onecli_metadata,
                extra={"status_code": response.status_code},
            )
        result["credential_mode"] = "onecli"
        result["onecli"] = onecli_metadata
    return result


def _discord_message_chunks(url: str, method: str, body: Any) -> list[dict[str, str]] | None:
    """Return lossless Discord content chunks when a model overfills a message."""
    parsed = urlparse(url)
    if (
        method != "POST"
        or parsed.hostname != "discord.com"
        or not re.fullmatch(r"/api/v\d+/channels/\d+/messages", parsed.path)
        or not isinstance(body, dict)
        or set(body) != {"content"}
        or not isinstance(body.get("content"), str)
        or len(body["content"]) <= 2000
    ):
        return None
    content = body["content"]
    return [{"content": content[index:index + 1900]} for index in range(0, len(content), 1900)]


def _reject_onecli_direct_credentials(
        headers: dict[str, Any] | None,
        query_params: dict[str, Any] | None,
) -> None:
    header_names = {str(key).strip().lower() for key in (headers or {})}
    blocked_headers = sorted(header_names.intersection(ONECLI_BLOCKED_HEADER_NAMES))
    if blocked_headers:
        raise ValueError("OneCLI mode rejects direct credential-bearing headers: " + ", ".join(blocked_headers))
    query_names = {str(key).strip().lower() for key in (query_params or {})}
    blocked_query_params = sorted(query_names.intersection(ONECLI_BLOCKED_QUERY_PARAM_NAMES))
    if blocked_query_params:
        raise ValueError(
            "OneCLI mode rejects direct credential-bearing query parameters: " + ", ".join(blocked_query_params)
        )


def _safe_context_metadata(tool_context: Any | None) -> dict[str, Any]:
    if tool_context is None:
        return {
            "execution_id": None,
            "workflow_id": None,
            "task_id": None,
            "agent_id": None,
            "tool_call_id": None,
        }
    if hasattr(tool_context, "safe_metadata"):
        metadata = tool_context.safe_metadata()
        if isinstance(metadata, dict):
            connector_binding = getattr(tool_context, "connector_binding", None)
            if isinstance(connector_binding, dict):
                metadata = {**metadata, "connector_binding": connector_binding}
            return metadata
    metadata = {
        "execution_id": getattr(tool_context, "execution_id", None),
        "workflow_id": getattr(tool_context, "workflow_id", None),
        "task_id": getattr(tool_context, "task_id", None),
        "agent_id": getattr(tool_context, "agent_id", None),
        "tool_call_id": getattr(tool_context, "tool_call_id", None),
    }
    connector_binding = getattr(tool_context, "connector_binding", None)
    if isinstance(connector_binding, dict):
        metadata["connector_binding"] = connector_binding
    return metadata


def _connector_interpolation_context(tool_context: Any | None) -> dict[str, Any]:
    connector_binding = getattr(tool_context, "connector_binding", None)
    if not isinstance(connector_binding, dict):
        return {}
    target_scope = connector_binding.get("target_scope")
    target_scope = target_scope if isinstance(target_scope, dict) else {}

    # Connector bindings are runtime selection metadata, not user input. Exposing
    # them here lets HTTP tools reference saved credential and target scope
    # values without asking agents to paste credential ids into every call.
    context: dict[str, Any] = {
        **target_scope,
        "connector_binding": connector_binding,
        "target_scope": target_scope,
        "connector_provider": connector_binding.get("provider"),
        "credential_id": connector_binding.get("credential_id"),
        "connector_credential_id": connector_binding.get("credential_id"),
        "connector_purpose": connector_binding.get("purpose"),
        "connector_identity_summary": connector_binding.get("identity_summary"),
    }
    return {key: value for key, value in context.items() if value is not None}


def _safe_header_value(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return "".join(char if char.isprintable() and char not in "\r\n" else "-" for char in normalized)[:256]


def build_onecli_correlation_headers(
        onecli_metadata: dict[str, Any],
        *,
        actor: str | None = None,
) -> dict[str, str]:
    context = onecli_metadata.get("agency_context")
    if not isinstance(context, dict):
        context = {}
    header_values = {
        "X-Agency-OneCLI-Correlation-ID": onecli_metadata.get("correlation_id"),
        "X-Agency-User-ID": actor,
        "X-Agency-Execution-ID": context.get("execution_id"),
        "X-Agency-Workflow-ID": context.get("workflow_id"),
        "X-Agency-Task-ID": context.get("task_id"),
        "X-Agency-Agent-ID": context.get("agent_id"),
        "X-Agency-Tool-Call-ID": context.get("tool_call_id"),
    }
    return {
        header: safe_value
        for header, value in header_values.items()
        if (safe_value := _safe_header_value(value)) is not None
    }


def _onecli_http_metadata(
        *,
        url: str,
        gateway_url: str,
        agent_token_secret_ref_configured: bool,
        tool_context: Any | None,
        correlation_id: str,
        agent_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    parsed = urlparse(url)
    return {
        "correlation_id": correlation_id,
        "gateway_mode": "proxy",
        "gateway_url": gateway_url,
        "target_scheme": parsed.scheme,
        "target_host": parsed.hostname or "",
        "target_port": parsed.port,
        "agent_token_secret_ref_configured": agent_token_secret_ref_configured,
        "agency_context": _safe_context_metadata(tool_context),
        "forwarded_headers": sorted(ONECLI_CORRELATION_HEADER_NAMES),
        "agent_identity": agent_identity or {
            "mapping": "server_configured_agent_token",
            "agent_token_secret_ref_configured": agent_token_secret_ref_configured,
        },
    }


def _publish_onecli_http_event(
        *,
        lifecycle_type: str,
        verdict: str | None,
        onecli_metadata: dict[str, Any],
        extra: dict[str, Any] | None = None,
) -> None:
    from app.tools.runtime.events import publish_tool_runtime_event

    publish_tool_runtime_event(
        lifecycle_type=lifecycle_type,
        tool_name="agency.http.request",
        actor=onecli_metadata.get("agency_context", {}).get("agent_id"),
        verdict=verdict,
        metadata={**onecli_metadata, **(extra or {})},
    )
