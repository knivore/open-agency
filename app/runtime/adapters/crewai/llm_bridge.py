from __future__ import annotations

import asyncio
import json
from crewai.llms.base_llm import BaseLLM
from pydantic import BaseModel, PrivateAttr
from typing import Any
from uuid import uuid4

from app.core.time import utc_now
from app.domain import ExecutionEvent, ExecutionEventType, ModelProfileDefinition
from app.llm.base import BaseModelClient, ModelMessage
from app.observability.event_bus import get_default_event_bus
from app.runtime.governance.context_health import estimate_context_health
from app.runtime.governance.recorder import record_context_health_snapshot, record_token_usage_snapshot
from app.runtime.governance.token_usage import normalize_token_usage


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
    _profile: ModelProfileDefinition = PrivateAttr()
    _model_client: BaseModelClient = PrivateAttr()
    _model_event_loop: asyncio.AbstractEventLoop | None = PrivateAttr(default=None)
    _execution_store: Any | None = PrivateAttr(default=None)
    _execution_id: str | None = PrivateAttr(default=None)
    _workflow_id: str | None = PrivateAttr(default=None)
    _agent_id: str | None = PrivateAttr(default=None)

    def __init__(
            self,
            *,
            profile: ModelProfileDefinition,
            model_client: BaseModelClient,
            model_event_loop: asyncio.AbstractEventLoop | None = None,
            execution_store: Any | None = None,
            execution_id: str | None = None,
            workflow_id: str | None = None,
            agent_id: str | None = None,
    ):
        super().__init__(
            model=profile.model,
            provider=profile.provider,
            temperature=profile.temperature,
            base_url=profile.base_url,
        )
        self._profile = profile
        self._model_client = model_client
        self._model_event_loop = model_event_loop
        self._execution_store = execution_store
        self._execution_id = execution_id
        self._workflow_id = workflow_id
        self._agent_id = agent_id

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
        # CrewAI supplies callback hooks here; Agency records model lifecycle through
        # runtime events instead, but keeping the parameter preserves adapter compatibility.
        _ = callbacks
        formatted_messages = self._format_messages(messages)
        model_messages = _to_model_messages(formatted_messages)
        model_request_id = str(uuid4())
        task_id = self._task_id_from_crewai(from_task)
        context_health = estimate_context_health(
            model_messages,
            model_profile=self._profile,
            reserved_completion_tokens=self._profile.max_tokens,
        )
        self._record_context_health(
            model_request_id=model_request_id,
            context_health=context_health,
            messages=model_messages,
            tools=tools,
            response_model=response_model,
            task_id=task_id,
            from_task=from_task,
            from_agent=from_agent,
        )
        try:
            response_kind = "structured" if response_model is not None else "text"
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
            self._record_model_response(
                model_request_id=model_request_id,
                context_health=context_health,
                response=response,
                response_content=content,
                response_kind=response_kind,
                task_id=task_id,
                from_task=from_task,
                from_agent=from_agent,
            )
            return self._invoke_after_llm_call_hooks(formatted_messages, content, from_agent)
        except Exception as exc:
            self._emit_call_failed_event(str(exc), from_task=from_task, from_agent=from_agent)
            raise

    def _record_context_health(
            self,
            *,
            model_request_id: str,
            context_health: Any,
            messages: list[ModelMessage],
            tools: list[dict[str, Any]] | None,
            response_model: type[BaseModel] | None,
            task_id: str | None,
            from_task: Any | None,
            from_agent: Any | None,
    ) -> None:
        if not self._governance_enabled():
            return
        payload = {
            **context_health.model_dump(mode="json"),
            **self._base_governance_payload(
                messages=messages,
                tools=tools,
                response_model=response_model,
                task_id=task_id,
                from_task=from_task,
                from_agent=from_agent,
            ),
        }
        metrics = self._context_metrics(context_health)
        context_event = self._run_governance_coro(
            self._emit_execution_event(
                ExecutionEventType.CONTEXT_HEALTH_RECORDED,
                payload=payload,
                metrics=metrics,
                metadata={"source": "crewai_llm_bridge", "call_kind": "crewai_bridge"},
                model_request_id=model_request_id,
                task_id=task_id,
            )
        )
        event_id = context_event.id if isinstance(context_event, ExecutionEvent) else None
        self._run_governance_coro(
            record_context_health_snapshot(
                self._execution_store,
                execution_id=self._execution_id or "",
                context_health=context_health,
                agent_id=self._agent_id,
                task_id=task_id,
                event_id=event_id,
            )
        )
        self._run_governance_coro(
            self._emit_execution_event(
                ExecutionEventType.LLM_REQUEST_CREATED,
                payload={
                    **self._base_governance_payload(
                        messages=messages,
                        tools=tools,
                        response_model=response_model,
                        task_id=task_id,
                        from_task=from_task,
                        from_agent=from_agent,
                    ),
                    "context_health": context_health.model_dump(mode="json"),
                },
                metrics=metrics,
                metadata={"source": "crewai_llm_bridge", "call_kind": "crewai_bridge"},
                model_request_id=model_request_id,
                task_id=task_id,
            )
        )

    def _record_model_response(
            self,
            *,
            model_request_id: str,
            context_health: Any,
            response: Any,
            response_content: str,
            response_kind: str,
            task_id: str | None,
            from_task: Any | None,
            from_agent: Any | None,
    ) -> None:
        if not self._governance_enabled():
            return
        usage = normalize_token_usage(
            response.usage,
            provider=response.provider or self._profile.provider,
            model=response.model or self._profile.model,
            profile=self._profile,
            estimated_prompt_tokens=context_health.estimated_prompt_tokens,
            response_content=response_content,
        )
        payload_base = {
            "call_kind": "crewai_bridge",
            "runtime_adapter_id": "crewai",
            "model_profile_id": self._profile.id,
            "provider": usage.provider,
            "model": usage.model,
            "workflow_id": self._workflow_id,
            "agent_id": self._agent_id,
            "task_id": task_id,
            "crewai_task_name": self._crewai_name(from_task),
            "crewai_agent_name": self._crewai_name(from_agent),
        }
        fallback = usage.provider_usage.get("model_fallback")
        if isinstance(fallback, dict) and fallback.get("used") is True:
            self._run_governance_coro(
                self._emit_execution_event(
                    ExecutionEventType.MODEL_FALLBACK_USED,
                    payload={
                        **payload_base,
                        "model_request_id": model_request_id,
                        **fallback,
                    },
                    metrics={
                        "fallback_index": fallback.get("fallback_index"),
                        "fallback_attempt_count": len(fallback.get("attempts") or []),
                    },
                    metadata={"source": "crewai_llm_bridge", "call_kind": "crewai_bridge"},
                    model_request_id=model_request_id,
                    task_id=task_id,
                )
            )
        response_event = self._run_governance_coro(
            self._emit_execution_event(
                ExecutionEventType.LLM_RESPONSE_CREATED,
                payload={
                    **payload_base,
                    "response_kind": f"crewai_bridge_{response_kind}_model_call",
                    "content": response_content,
                    "tool_calls": [
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                        }
                        for tool_call in response.tool_calls
                    ],
                    "usage": usage.model_dump(mode="json"),
                },
                metrics={
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "estimated_cost": usage.estimated_cost,
                    "input_tokens": usage.prompt_tokens,
                    "output_tokens": usage.completion_tokens,
                    "token_usage_estimated": usage.estimated,
                    "latency_ms": response.latency_ms,
                },
                metadata={
                    "source": "crewai_llm_bridge",
                    "call_kind": "crewai_bridge",
                    "provider": usage.provider,
                    "model": usage.model,
                },
                model_request_id=model_request_id,
                task_id=task_id,
            )
        )
        response_event_id = response_event.id if isinstance(response_event, ExecutionEvent) else None
        token_event = self._run_governance_coro(
            self._emit_execution_event(
                ExecutionEventType.TOKEN_USAGE_RECORDED,
                payload={
                    **payload_base,
                    "usage": usage.model_dump(mode="json"),
                },
                metrics={
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "estimated_cost": usage.estimated_cost,
                    "token_usage_estimated": usage.estimated,
                },
                metadata={
                    "source": "crewai_llm_bridge",
                    "call_kind": "crewai_bridge",
                    "response_event_id": response_event_id,
                },
                model_request_id=model_request_id,
                task_id=task_id,
            )
        )
        token_event_id = token_event.id if isinstance(token_event, ExecutionEvent) else None
        self._run_governance_coro(
            record_token_usage_snapshot(
                self._execution_store,
                execution_id=self._execution_id or "",
                usage=usage,
                agent_id=self._agent_id,
                task_id=task_id,
                workflow_id=self._workflow_id,
                model_request_id=model_request_id,
                event_id=token_event_id,
            )
        )

    async def _emit_execution_event(
            self,
            event_type: ExecutionEventType,
            *,
            payload: dict[str, Any],
            metrics: dict[str, Any],
            metadata: dict[str, Any],
            model_request_id: str,
            task_id: str | None,
    ) -> ExecutionEvent | None:
        if not self._governance_enabled():
            return None
        execution_id = self._execution_id
        if not execution_id:
            return None
        events = await self._execution_store.list_events(execution_id)
        event = ExecutionEvent(
            execution_id=execution_id,
            workflow_id=self._workflow_id,
            agent_id=self._agent_id,
            task_id=task_id,
            model_request_id=model_request_id,
            parent_event_id=events[-1].id if events else None,
            trace_id=self._execution_id,
            event_type=event_type,
            sequence=len(events) + 1,
            actor=self._agent_id,
            payload=payload,
            metrics=metrics,
            metadata=metadata,
        )
        prepared = get_default_event_bus().publish(event)
        saved = await self._execution_store.save_event(prepared)
        execution = await self._execution_store.get_execution(execution_id)
        if execution is not None:
            execution.updated_at = utc_now()
            await self._execution_store.update_execution(execution)
        return saved

    def _run_governance_coro(self, coro: Any) -> Any:
        if not self._governance_enabled():
            return None
        try:
            if self._model_event_loop is not None and self._model_event_loop.is_running():
                try:
                    current_loop = asyncio.get_running_loop()
                except RuntimeError:
                    current_loop = None
                if current_loop is self._model_event_loop:
                    current_loop.create_task(coro)
                    return None
                if current_loop is not self._model_event_loop:
                    return asyncio.run_coroutine_threadsafe(coro, self._model_event_loop).result()
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not None and current_loop.is_running():
                current_loop.create_task(coro)
                return None
            return asyncio.run(coro)
        except Exception:
            return None

    def _governance_enabled(self) -> bool:
        return self._execution_store is not None and bool(self._execution_id)

    def _base_governance_payload(
            self,
            *,
            messages: list[ModelMessage],
            tools: list[dict[str, Any]] | None,
            response_model: type[BaseModel] | None,
            task_id: str | None,
            from_task: Any | None,
            from_agent: Any | None,
    ) -> dict[str, Any]:
        return {
            "call_kind": "crewai_bridge",
            "runtime_adapter_id": "crewai",
            "model_profile_id": self._profile.id,
            "provider": self._profile.provider,
            "model": self._profile.model,
            "workflow_id": self._workflow_id,
            "agent_id": self._agent_id,
            "task_id": task_id,
            "message_count": len(messages),
            "tool_count": len(tools or []),
            "structured_output": response_model is not None,
            "crewai_task_name": self._crewai_name(from_task),
            "crewai_agent_name": self._crewai_name(from_agent),
        }

    @staticmethod
    def _context_metrics(context_health: Any) -> dict[str, Any]:
        return {
            "estimated_prompt_tokens": context_health.estimated_prompt_tokens,
            "reserved_completion_tokens": context_health.reserved_completion_tokens,
            "estimated_total_context_tokens": context_health.estimated_total_context_tokens,
            "context_window": context_health.context_window or 0,
            "context_usage_ratio": context_health.usage_ratio or 0,
            "context_status": context_health.status,
        }

    @staticmethod
    def _crewai_name(value: Any | None) -> str | None:
        if value is None:
            return None
        for attr in ("name", "role", "description"):
            candidate = getattr(value, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _task_id_from_crewai(value: Any | None) -> str | None:
        if value is None:
            return None
        for attr in ("id", "task_id"):
            candidate = getattr(value, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
