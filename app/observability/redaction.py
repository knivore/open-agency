from __future__ import annotations

import os
import re
from typing import Any

DEFAULT_SECRET_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{10,}",
    r"Bearer\s+[A-Za-z0-9._\-]+",
    r"(?i)(api[_-]?key|token|password|secret)",
]

SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "password",
    "secret",
}


def key_looks_secret(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in SECRET_KEY_NAMES:
        return True
    if normalized.endswith(("_api_key", "_apikey", "_auth_token", "_access_token", "_refresh_token")):
        return True
    if normalized.endswith(("_password", "_secret")):
        return True
    return False


class Redactor:
    def __init__(self, *, enabled: bool = True, extra_patterns: list[str] | None = None):
        self.enabled = enabled
        env_patterns = [item.strip() for item in os.getenv("OBSERVABILITY_SECRET_PATTERNS", "").split(",") if
                        item.strip()]
        self.patterns = [re.compile(pattern) for pattern in
                         [*DEFAULT_SECRET_PATTERNS, *(extra_patterns or []), *env_patterns]]

    def redact_text(self, value: str) -> tuple[str, list[str]]:
        if not self.enabled:
            return value, []
        redacted = value
        fields: list[str] = []
        for pattern in self.patterns:
            if pattern.search(redacted):
                redacted = pattern.sub("[REDACTED]", redacted)
                fields.append(pattern.pattern)
        return redacted, fields

    def redact_value(self, value: Any, *, path: str = "") -> tuple[Any, list[str]]:
        if not self.enabled:
            return value, []
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            result = {}
            fields: list[str] = []
            for key, item in value.items():
                nested_path = f"{path}.{key}" if path else str(key)
                if key_looks_secret(str(key)):
                    result[key] = "[REDACTED]"
                    fields.append(nested_path)
                else:
                    redacted_item, nested_fields = self.redact_value(item, path=nested_path)
                    result[key] = redacted_item
                    fields.extend(nested_fields)
            return result, fields
        if isinstance(value, list):
            items = []
            fields: list[str] = []
            for index, item in enumerate(value):
                redacted_item, nested_fields = self.redact_value(item, path=f"{path}[{index}]")
                items.append(redacted_item)
                fields.extend(nested_fields)
            return items, fields
        return value, []
