from __future__ import annotations

from typing import Any

from app.domain import ModelProfileDefinition, TokenUsage
from .context_health import estimate_text_tokens


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        return 0.0
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        return 0.0


def _nested_int(payload: dict[str, Any], *path: str) -> int:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return 0
        current = current.get(key)
    return _as_int(current)


def _pricing_value(profile: ModelProfileDefinition | None, *keys: str) -> float:
    if profile is None:
        return 0.0
    parameters = profile.parameters or {}
    for key in keys:
        value = parameters.get(key)
        parsed = _as_float(value)
        if parsed:
            return parsed
    return 0.0


def estimate_cost(usage: TokenUsage, profile: ModelProfileDefinition | None) -> tuple[float, str | None]:
    input_cost_per_1m = _pricing_value(profile, "input_token_cost_per_1m", "prompt_token_cost_per_1m")
    output_cost_per_1m = _pricing_value(profile, "output_token_cost_per_1m", "completion_token_cost_per_1m")
    cached_cost_per_1m = _pricing_value(profile, "cached_input_token_cost_per_1m", "cached_token_cost_per_1m")
    currency = None
    if profile is not None:
        raw_currency = (profile.parameters or {}).get("currency")
        currency = raw_currency if isinstance(raw_currency, str) and raw_currency.strip() else None

    billable_prompt_tokens = max(usage.prompt_tokens - usage.cached_tokens, 0)
    cost = (billable_prompt_tokens * input_cost_per_1m) / 1_000_000
    cost += (usage.completion_tokens * output_cost_per_1m) / 1_000_000
    if cached_cost_per_1m:
        cost += (usage.cached_tokens * cached_cost_per_1m) / 1_000_000
    return round(cost, 8), currency


def normalize_token_usage(
        raw_usage: dict[str, Any] | None,
        *,
        provider: str | None = None,
        model: str | None = None,
        profile: ModelProfileDefinition | None = None,
        estimated_prompt_tokens: int | None = None,
        response_content: Any = None,
) -> TokenUsage:
    usage = raw_usage if isinstance(raw_usage, dict) else {}

    prompt_tokens = (
            _as_int(usage.get("prompt_tokens"))
            or _as_int(usage.get("input_tokens"))
            or _as_int(usage.get("promptTokenCount"))
            or _as_int(usage.get("prompt_token_count"))
    )
    completion_tokens = (
            _as_int(usage.get("completion_tokens"))
            or _as_int(usage.get("output_tokens"))
            or _as_int(usage.get("candidatesTokenCount"))
            or _as_int(usage.get("completion_token_count"))
    )
    total_tokens = (
            _as_int(usage.get("total_tokens"))
            or _as_int(usage.get("totalTokenCount"))
            or _as_int(usage.get("total_token_count"))
    )
    cached_tokens = (
            _as_int(usage.get("cached_tokens"))
            or _nested_int(usage, "prompt_tokens_details", "cached_tokens")
            or _nested_int(usage, "input_token_details", "cache_read")
            or _nested_int(usage, "cache_creation_input_tokens")
            or _as_int(usage.get("cache_read_input_tokens"))
    )
    reasoning_tokens = (
            _as_int(usage.get("reasoning_tokens"))
            or _nested_int(usage, "completion_tokens_details", "reasoning_tokens")
            or _nested_int(usage, "output_token_details", "reasoning")
    )

    estimated = False
    estimate_parts: list[str] = []
    if prompt_tokens == 0 and estimated_prompt_tokens is not None:
        prompt_tokens = max(int(estimated_prompt_tokens), 0)
        estimated = True
        estimate_parts.append("prompt")
    if completion_tokens == 0 and response_content not in (None, ""):
        completion_tokens = estimate_text_tokens(response_content)
        estimated = True
        estimate_parts.append("completion")
    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens
        if prompt_tokens or completion_tokens:
            estimated = estimated or bool(estimate_parts)

    estimated_cost = _as_float(usage.get("estimated_cost"))
    normalized = TokenUsage(
        provider=provider or (profile.provider if profile is not None else None),
        model=model or (profile.model if profile is not None else None),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        estimated_cost=estimated_cost,
        estimated=estimated,
        estimate_method=(
            f"estimated_{'_and_'.join(estimate_parts)}_chars_div_4"
            if estimate_parts
            else None
        ),
        provider_usage=dict(usage),
    )
    if normalized.estimated_cost == 0:
        cost, currency = estimate_cost(normalized, profile)
        normalized.estimated_cost = cost
        normalized.currency = currency
    else:
        raw_currency = usage.get("currency")
        normalized.currency = raw_currency if isinstance(raw_currency, str) else None
    return normalized
