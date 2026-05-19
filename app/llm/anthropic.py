from __future__ import annotations

import httpx
import json
import time
from typing import Any, Dict, Iterator, List, Optional

from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse
from app.llm.registry import LLMEnvironmentConfig


class AnthropicModelClient:
    provider_key = "anthropic"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.base_url = profile.base_url or "https://api.anthropic.com"
        self.api_key = profile.api_key_ref or env_config.anthropic_api_key

    def _payload(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float],
            max_tokens: Optional[int],
    ) -> Dict[str, Any]:
        system_messages = [m.content for m in messages if m.role == "system"]
        non_system_messages = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
        payload: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": non_system_messages,
            "max_tokens": max_tokens if max_tokens is not None else self.profile.max_tokens or 1024,
        }
        if system_messages:
            payload["system"] = "\n".join(str(item) for item in system_messages)
        if temperature is not None or self.profile.temperature is not None:
            payload["temperature"] = temperature if temperature is not None else self.profile.temperature
        return payload

    def _headers(self) -> Dict[str, str]:
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY is not configured")
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def generate_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        started_at = time.perf_counter()
        response = httpx.post(
            self.base_url.rstrip("/") + "/v1/messages",
            json=self._payload(messages, temperature=temperature, max_tokens=max_tokens),
            headers=self._headers(),
            timeout=60.0,
        )
        response.raise_for_status()
        data = response.json()
        text_blocks = [block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"]
        return ModelResponse(
            content="".join(text_blocks),
            tool_calls=[],
            usage=data.get("usage", {}),
            raw_response=data,
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=(time.perf_counter() - started_at) * 1000,
        )

    def generate_structured(
            self,
            messages: List[ModelMessage],
            *,
            schema: Dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        response = self.generate_text(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
        if isinstance(response.content, str):
            try:
                response.content = json.loads(response.content)
            except json.JSONDecodeError:
                pass
        return response

    def stream_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]:
        payload = self._payload(messages, temperature=temperature, max_tokens=max_tokens)
        payload["stream"] = True
        with httpx.stream(
                "POST",
                self.base_url.rstrip("/") + "/v1/messages",
                json=payload,
                headers=self._headers(),
                timeout=60.0,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                event = json.loads(data)
                delta = event.get("delta", {})
                text = delta.get("text")
                if text:
                    yield text

    def count_tokens(self, messages: List[ModelMessage], **kwargs: Any) -> Optional[int]:
        return None

    def health_check(self) -> Dict[str, Any]:
        try:
            response = httpx.get(self.base_url.rstrip("/") + "/v1/models", headers=self._headers(), timeout=5.0)
            return {
                "ok": response.status_code < 500,
                "provider": self.profile.provider,
                "base_url": self.base_url,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "base_url": self.base_url, "error": str(exc)}
