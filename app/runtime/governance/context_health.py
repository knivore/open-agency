from __future__ import annotations

import json
from typing import Any

from app.domain import ContextHealth, ModelProfileDefinition
from app.llm.base import ModelMessage

ESTIMATE_METHOD = "plain_text_chars_div_4_with_message_overhead"
ESTIMATOR_VERSION = "v1"


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def estimate_text_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else _json_dump(value)
    if not text.strip():
        return 0
    return max(1, len(text) // 4)


def estimate_model_messages_tokens(messages: list[ModelMessage]) -> int:
    if not messages:
        return 0
    total = 2
    for message in messages:
        total += 4
        if message.name:
            total += 1
        if message.tool_call_id:
            total += 1
        total += estimate_text_tokens(message.content)
    return total


def resolve_model_context_window(model_profile: ModelProfileDefinition | None) -> int | None:
    if model_profile is None:
        return None
    candidates: list[Any] = [model_profile.context_window]
    parameters = model_profile.parameters or {}
    for key in (
            "context_window",
            "context_length",
            "context_tokens",
            "max_context_tokens",
            "model_context_window",
            "num_ctx",
    ):
        candidates.append(parameters.get(key))
    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, int) and candidate > 0:
            return candidate
        if isinstance(candidate, str):
            try:
                parsed = int(candidate.strip())
            except ValueError:
                continue
            if parsed > 0:
                return parsed
    return None


def context_usage_status(usage_ratio: float | None) -> str:
    if usage_ratio is None:
        return "unknown"
    if usage_ratio >= 1:
        return "overflow"
    if usage_ratio >= 0.85:
        return "critical"
    if usage_ratio >= 0.70:
        return "warning"
    return "normal"


def estimate_context_health(
        messages: list[ModelMessage],
        *,
        model_profile: ModelProfileDefinition | None,
        reserved_completion_tokens: int | None = None,
) -> ContextHealth:
    prompt_tokens = estimate_model_messages_tokens(messages)
    reserved = max(int(reserved_completion_tokens or model_profile.max_tokens or 0), 0) if model_profile else 0
    total = prompt_tokens + reserved
    context_window = resolve_model_context_window(model_profile)
    usage_ratio = total / context_window if context_window and context_window > 0 else None
    remaining = max(context_window - total, 0) if context_window is not None else None
    return ContextHealth(
        estimated_prompt_tokens=prompt_tokens,
        reserved_completion_tokens=reserved,
        estimated_total_context_tokens=total,
        context_window=context_window,
        remaining_context_tokens=remaining,
        usage_ratio=round(usage_ratio, 6) if usage_ratio is not None else None,
        status=context_usage_status(usage_ratio),
        estimate_method=ESTIMATE_METHOD,
        estimator_version=ESTIMATOR_VERSION,
    )
