from __future__ import annotations

ONECLI_BLOCKED_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "x-amz-security-token",
    }
)

ONECLI_BLOCKED_QUERY_PARAM_NAMES = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth_token",
        "client_secret",
        "key",
        "token",
    }
)
