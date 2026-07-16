from __future__ import annotations

import httpx
import json
import time
from openai import OpenAI
from typing import Any, Dict, Iterator, List, Optional

from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse, ModelToolCall
from app.llm.openai_helpers import sanitize_openai_message_name
from app.llm.registry import LLMEnvironmentConfig


class OpenAICompatibleModelClient:
    provider_key = "openai_compatible"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.env_config = env_config
        self.base_url = profile.base_url or self._provider_env_base_url() or env_config.local_openai_base_url
        # Provider-specific keys keep OpenAI-compatible services from accidentally using OPENAI_API_KEY.
        self.api_key = (
                profile.api_key_ref
                or self._provider_env_api_key()
                or self._local_endpoint_api_key()
                or "not-required"
        )
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _provider_env_base_url(self) -> Optional[str]:
        provider = self.profile.provider.strip().lower().replace("-", "_")
        if provider == "deepseek":
            return self.env_config.deepseek_base_url or "https://api.deepseek.com"
        if provider == "qwen":
            return (
                    self.env_config.qwen_base_url
                    or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
            )
        return None

    def _local_endpoint_api_key(self) -> Optional[str]:
        configured_url = self.env_config.local_openai_base_url
        if not configured_url or not self.env_config.local_openai_api_key or not self.base_url:
            return None
        if self.base_url.strip().rstrip("/") != configured_url.strip().rstrip("/"):
            return None
        # The local gateway credential is a destination-bound capability, not a
        # fallback for every operator-allowlisted compatible endpoint.
        return self.env_config.local_openai_api_key

    def _provider_env_api_key(self) -> Optional[str]:
        provider = self.profile.provider.strip().lower().replace("-", "_")
        if provider == "deepseek":
            return self.env_config.deepseek_api_key
        if provider == "qwen":
            return self.env_config.qwen_api_key
        if provider == "openai":
            return self.env_config.openai_api_key
        # Custom compatible endpoints must never inherit the credential for the
        # official OpenAI service; doing so turns an endpoint choice into a
        # credential-exfiltration primitive.
        return None

    def _to_openai_messages(self, messages: List[ModelMessage]) -> List[Dict[str, Any]]:
        payload = []
        for message in messages:
            item: Dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": sanitize_openai_message_name(tool_call.name) or tool_call.name,
                            "arguments": json.dumps(tool_call.arguments or {}),
                        },
                    }
                    for tool_call in message.tool_calls
                ]
            if message.name:
                sanitized_name = sanitize_openai_message_name(message.name)
                if sanitized_name:
                    item["name"] = sanitized_name
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            payload.append(item)
        return payload

    def _build_response(self, response: Any, started_at: float) -> ModelResponse:
        choice = response.choices[0].message if response.choices else None
        tool_calls: List[ModelToolCall] = []
        if choice and getattr(choice, "tool_calls", None):
            for tool_call in choice.tool_calls:
                arguments = tool_call.function.arguments
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {"raw": arguments}
                tool_calls.append(
                    ModelToolCall(
                        id=tool_call.id,
                        name=tool_call.function.name,
                        arguments=arguments,
                        raw=tool_call,
                    )
                )

        usage = {}
        if getattr(response, "usage", None):
            usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else dict(response.usage)

        content = choice.content if choice else None
        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            raw_response=response,
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )

    def _chat_options(
            self,
            *,
            temperature: Optional[float],
            max_tokens: Optional[int],
            stream: bool,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.profile.model,
            "stream": stream,
            **kwargs,
        }
        resolved_temperature = temperature if temperature is not None else self.profile.temperature
        resolved_max_tokens = max_tokens if max_tokens is not None else self.profile.max_tokens
        if resolved_temperature is not None and not self.profile.model.startswith("gpt-5"):
            payload["temperature"] = resolved_temperature
        if resolved_max_tokens is not None:
            payload["max_tokens"] = resolved_max_tokens
        return payload

    def generate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        started_at = time.perf_counter()
        response = self.client.chat.completions.create(
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                **kwargs,
            ),
        )
        return self._build_response(response, started_at)

    def generate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        started_at = time.perf_counter()
        response = self.client.chat.completions.create(
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": kwargs.pop("schema_name", "structured_output"),
                        "schema": schema,
                    },
                },
                **kwargs,
            ),
        )
        model_response = self._build_response(response, started_at)
        if isinstance(model_response.content, str):
            try:
                model_response.content = json.loads(model_response.content)
            except json.JSONDecodeError:
                pass
        return model_response

    def stream_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            messages=self._to_openai_messages(messages),
            **self._chat_options(
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                **kwargs,
            ),
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content

    def count_tokens(self, messages: List[ModelMessage], **kwargs: Any) -> Optional[int]:
        return None

    def embed_texts(self, texts: List[str], **kwargs: Any) -> List[List[float]]:
        payload: Dict[str, Any] = {
            "model": kwargs.get("model") or self.profile.model,
            "input": texts,
        }
        dimensions = kwargs.get("dimensions") or self.profile.parameters.get("embedding_dimensions")
        if dimensions is not None:
            payload["dimensions"] = dimensions
        response = self.client.embeddings.create(**payload)
        return [list(item.embedding) for item in response.data]

    def health_check(self) -> Dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "provider": self.profile.provider, "error": "base_url is not configured"}

        url = self.base_url.rstrip("/") + "/models"
        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(url, headers={"Authorization": f"Bearer {self.api_key}"})
            return {
                "ok": response.status_code < 500,
                "provider": self.profile.provider,
                "base_url": self.base_url,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "base_url": self.base_url, "error": str(exc)}
