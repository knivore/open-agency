from __future__ import annotations

import asyncio
import json
from typing import Any

from crewai.llms.base_llm import BaseLLM
from pydantic import BaseModel, PrivateAttr

from app.domain import ModelProfileDefinition
from app.llm.base import BaseModelClient, ModelMessage


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
            else:
                parts.append(json.dumps(item, default=str))
        return "\n".join(parts)
    return json.dumps(content, default=str)


def _to_model_messages(messages: str | list[dict[str, Any]]) -> list[ModelMessage]:
    if isinstance(messages, str):
        return [ModelMessage(role="user", content=messages)]

    normalized: list[ModelMessage] = []
    for item in messages:
        role = item.get("role", "user")
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        normalized.append(
            ModelMessage(
                role=role,
                content=_content_to_text(item.get("content", "")),
                name=item.get("name"),
                tool_call_id=item.get("tool_call_id"),
            )
        )
    return normalized


class AgencyModelClientLLM(BaseLLM):
    """CrewAI BaseLLM wrapper around Agency's provider registry clients."""

    llm_type: str = "agency_model_client"
    _model_client: BaseModelClient = PrivateAttr()
    _model_event_loop: asyncio.AbstractEventLoop | None = PrivateAttr(default=None)

    def __init__(
            self,
            *,
            profile: ModelProfileDefinition,
            model_client: BaseModelClient,
            model_event_loop: asyncio.AbstractEventLoop | None = None,
    ):
        super().__init__(
            model=profile.model,
            provider=profile.provider,
            temperature=profile.temperature,
            base_url=profile.base_url,
        )
        self._model_client = model_client
        self._model_event_loop = model_event_loop

    def _call_model_client(
            self,
            sync_method_name: str,
            async_method_name: str,
            *args: Any,
            **kwargs: Any,
    ) -> Any:
        async_method = getattr(self._model_client, async_method_name, None)
        if async_method is not None and self._model_event_loop is not None and self._model_event_loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not self._model_event_loop:
                future = asyncio.run_coroutine_threadsafe(
                    async_method(*args, **kwargs),
                    self._model_event_loop,
                )
                return future.result()

        sync_method = getattr(self._model_client, sync_method_name)
        return sync_method(*args, **kwargs)

    def call(
            self,
            messages: str | list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            callbacks: list[Any] | None = None,
            available_functions: dict[str, Any] | None = None,
            from_task: Any | None = None,
            from_agent: Any | None = None,
            response_model: type[BaseModel] | None = None,
    ) -> str | Any:
        formatted_messages = self._format_messages(messages)
        model_messages = _to_model_messages(formatted_messages)
        try:
            if response_model is not None:
                response = self._call_model_client(
                    "generate_structured",
                    "agenerate_structured",
                    model_messages,
                    schema=response_model.model_json_schema(),
                    temperature=self.temperature,
                )
            else:
                response = self._call_model_client(
                    "generate_text",
                    "agenerate_text",
                    model_messages,
                    temperature=self.temperature,
                    tools=tools if tools else None,
                )

            if response.tool_calls and available_functions:
                tool_call = response.tool_calls[0]
                return self._handle_tool_execution(
                    tool_call.name,
                    tool_call.arguments,
                    available_functions,
                    from_task=from_task,
                    from_agent=from_agent,
                )

            content = "" if response.content is None else str(response.content)
            content = self._apply_stop_words(content)
            self._track_token_usage_internal(response.usage or {})
            return self._invoke_after_llm_call_hooks(formatted_messages, content, from_agent)
        except Exception as exc:
            self._emit_call_failed_event(str(exc), from_task=from_task, from_agent=from_agent)
            raise
