from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from app.llm.base import ModelResponse, ModelStreamEvent, ModelToolCall


def stream_openai_chat_response(
        stream: Any,
        *,
        provider: str,
        model: str,
        started_at: float | None = None,
) -> Iterator[ModelStreamEvent]:
    """Emit text immediately while retaining fragmented tool calls for the final response."""

    started = started_at if started_at is not None else time.perf_counter()
    content_parts: list[str] = []
    tool_call_parts: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}

    for chunk in stream:
        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage.model_dump() if hasattr(chunk_usage, "model_dump") else dict(chunk_usage)

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = choices[0].delta
        content = getattr(delta, "content", None)
        if isinstance(content, str) and content:
            content_parts.append(content)
            yield ModelStreamEvent(text_delta=content)

        for position, tool_call in enumerate(getattr(delta, "tool_calls", None) or []):
            index = getattr(tool_call, "index", None)
            resolved_index = index if isinstance(index, int) else position
            buffer = tool_call_parts.setdefault(
                resolved_index,
                {"id": None, "name_parts": [], "argument_parts": [], "raw_parts": []},
            )
            tool_call_id = getattr(tool_call, "id", None)
            if tool_call_id:
                buffer["id"] = tool_call_id
            function = getattr(tool_call, "function", None)
            function_name = getattr(function, "name", None) if function is not None else None
            function_arguments = getattr(function, "arguments", None) if function is not None else None
            if function_name:
                buffer["name_parts"].append(function_name)
            if function_arguments:
                buffer["argument_parts"].append(function_arguments)
            buffer["raw_parts"].append(tool_call)

    assembled_tool_calls: list[ModelToolCall] = []
    for index in sorted(tool_call_parts):
        buffer = tool_call_parts[index]
        raw_arguments = "".join(buffer["argument_parts"])
        if raw_arguments:
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {"raw": raw_arguments}
        else:
            arguments = {}
        assembled_tool_calls.append(
            ModelToolCall(
                id=buffer["id"],
                name="".join(buffer["name_parts"]),
                arguments=arguments,
                raw=buffer["raw_parts"],
            )
        )

    yield ModelStreamEvent(
        response=ModelResponse(
            content="".join(content_parts) or None,
            tool_calls=assembled_tool_calls,
            usage=usage,
            raw_response=None,
            provider=provider,
            model=model,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
    )
