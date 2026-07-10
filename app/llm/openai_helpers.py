from __future__ import annotations

import re

OPENAI_MESSAGE_NAME_PATTERN = re.compile(r"^[^\s<|\\/>]+$")


def sanitize_openai_message_name(name: str | None) -> str | None:
    """Return a chat-message name that satisfies OpenAI's request validator.

    We keep the internal label untouched for UI/runtime purposes, but the API
    payload must use a compact token without spaces or reserved punctuation.
    """
    if not isinstance(name, str):
        return None
    normalized = name.strip()
    if not normalized:
        return None
    if OPENAI_MESSAGE_NAME_PATTERN.fullmatch(normalized):
        return normalized
    sanitized = re.sub(r"[\s<|\\/>]+", "_", normalized)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or None
