from __future__ import annotations

from urllib.parse import quote, urlsplit, urlunsplit

from app.integrations.secrets import resolve_secret_ref


def build_onecli_proxy_url(gateway_url: str, agent_token_secret_ref: str | None) -> str:
    if not agent_token_secret_ref:
        return gateway_url

    resolved = resolve_secret_ref(agent_token_secret_ref)
    if not resolved.value:
        raise ValueError(
            "OneCLI agent token secret ref could not be resolved from server-side configuration."
        )

    parsed = urlsplit(gateway_url)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("ONECLI_GATEWAY_URL must be an absolute HTTP(S) URL.")

    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port is not None else ""
    userinfo = f"x:{quote(resolved.value, safe='')}@"
    return urlunsplit((
        parsed.scheme,
        f"{userinfo}{host}{port}",
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))
