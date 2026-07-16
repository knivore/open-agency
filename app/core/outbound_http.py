"""Shared fail-closed URL checks for model- and user-controlled outbound requests."""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import ParseResult, urlparse

OFFICIAL_MODEL_PROVIDER_HOSTS: dict[str, set[str]] = {
    "openai": {"api.openai.com"},
    "openai_codex": {"api.openai.com", "codex-api.openai.com"},
    "anthropic": {"api.anthropic.com"},
    "google": {"generativelanguage.googleapis.com"},
    "openrouter": {"openrouter.ai"},
    "deepseek": {"api.deepseek.com"},
    "qwen": {"dashscope-intl.aliyuncs.com"},
    "xai": {"api.x.ai"},
}


def host_matches_allowlist(host: str, allowed_hosts: list[str] | set[str]) -> bool:
    """Match explicit hosts only; a global wildcard would recreate the SSRF primitive."""

    normalized = host.strip().lower().rstrip(".")
    if not normalized:
        return False
    for raw_allowed in allowed_hosts:
        allowed = raw_allowed.strip().lower().rstrip(".")
        if not allowed or allowed == "*":
            continue
        if allowed.startswith("*.") and normalized.endswith(allowed[1:]):
            return True
        if normalized == allowed:
            return True
    return False


def validate_outbound_http_url(url: str, *, allowed_hosts: list[str] | set[str]) -> ParseResult:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL user information is not allowed.")

    host = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private, loopback, link-local, and reserved IP targets are not allowed.")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hostnames are not allowed.")
    if not host_matches_allowlist(host, allowed_hosts):
        raise ValueError(f"host is not allowlisted: {host}")
    if address is None and os.getenv("APP_ENV", "").strip().lower() != "test":
        try:
            answers = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        except OSError as exc:
            raise ValueError(f"host could not be resolved: {host}") from exc
        resolved_addresses = {
            item[4][0].split("%", 1)[0]
            for item in answers
            if item[4] and item[4][0]
        }
        if not resolved_addresses:
            raise ValueError(f"host could not be resolved: {host}")
        for resolved in resolved_addresses:
            try:
                resolved_address = ipaddress.ip_address(resolved)
            except ValueError as exc:
                raise ValueError(f"host resolved to an invalid address: {host}") from exc
            if not resolved_address.is_global:
                raise ValueError(f"host resolves to a non-public address: {host}")
    return parsed


def validate_model_provider_url(
        url: str,
        *,
        provider_key: str,
        allowed_custom_hosts: list[str] | set[str],
) -> ParseResult:
    """Prevent provider configuration from redirecting ambient credentials."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Model provider base_url must be an absolute HTTP(S) URL without user information.")
    host = parsed.hostname.lower().rstrip(".")
    normalized_provider = provider_key.strip().lower().replace("-", "_")
    official_hosts = OFFICIAL_MODEL_PROVIDER_HOSTS.get(normalized_provider)
    if official_hosts is not None:
        if parsed.scheme != "https":
            raise ValueError(f"Official provider '{normalized_provider}' requires HTTPS.")
        if host not in official_hosts:
            raise ValueError(f"Custom base_url is not allowed for provider '{normalized_provider}'.")
        return parsed
    if not host_matches_allowlist(host, allowed_custom_hosts):
        raise ValueError(f"Model provider host is not allowlisted: {host}")
    return parsed
