from __future__ import annotations

import httpx
import json
from pathlib import Path
import time
from urllib.parse import urlparse, urlunparse
from typing import Any, Dict, Iterator, List, Optional

from app.core.config import get_settings
from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse
from app.llm.registry import LLMEnvironmentConfig


class OllamaModelClient:
    provider_key = "ollama"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.base_url = self._normalize_base_url(
            profile.base_url or env_config.ollama_base_url or "http://localhost:11434"
        )
        self.timeout_seconds = self._timeout_seconds()

    def _normalize_base_url(self, base_url: str) -> str:
        parsed = urlparse(base_url)
        if (
            Path("/.dockerenv").exists()
            and parsed.hostname in {"localhost", "127.0.0.1"}
            and (parsed.port or 11434) == 11434
        ):
            netloc = "host.docker.internal"
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunparse(parsed._replace(netloc=netloc))
        return base_url

    def _timeout_seconds(self) -> float:
        raw = self.profile.parameters.get("request_timeout_seconds")
        if raw is None:
            raw = self.profile.parameters.get("timeout_seconds")
        if raw is None:
            return get_settings().llm_request_timeout_seconds
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            return get_settings().llm_request_timeout_seconds
        return max(timeout, 0.1)

    def _payload(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float],
            max_tokens: Optional[int],
            stream: bool,
            format_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.profile.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
            "options": {},
        }
        if temperature is not None or self.profile.temperature is not None:
            payload["options"]["temperature"] = temperature if temperature is not None else self.profile.temperature
        if max_tokens is not None or self.profile.max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens if max_tokens is not None else self.profile.max_tokens
        if format_schema is not None:
            payload["format"] = format_schema
        think = self.profile.parameters.get("think")
        if think is None:
            think = self.profile.parameters.get("enable_thinking")
        if think is not None:
            payload["think"] = bool(think)
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
        format_schema = kwargs.get("format")
        response = httpx.post(
            self.base_url.rstrip("/") + "/api/chat",
            json=self._payload(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
                format_schema=format_schema,
            ),
            timeout=self.timeout_seconds,
            trust_env=False,
        )
        response.raise_for_status()
        data = response.json()
        return ModelResponse(
            content=data.get("message", {}).get("content"),
            tool_calls=[],
            usage={},
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
        response = self.generate_text(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            format=schema,
            **kwargs,
        )
        if isinstance(response.raw_response, dict):
            content = response.raw_response.get("message", {}).get("content")
            if isinstance(content, str):
                try:
                    response.content = json.loads(content)
                except json.JSONDecodeError:
                    response.content = content
        return response

    def stream_text(
            self,
            messages: List[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]:
        with httpx.stream(
                "POST",
                self.base_url.rstrip("/") + "/api/chat",
                json=self._payload(messages, temperature=temperature, max_tokens=max_tokens, stream=True),
                timeout=self.timeout_seconds,
                trust_env=False,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                data = json.loads(line)
                content = data.get("message", {}).get("content")
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
        response = httpx.post(self.base_url.rstrip("/") + "/api/embed", json=payload, timeout=self.timeout_seconds)
        if response.status_code == 404 and len(texts) == 1:
            legacy = httpx.post(
                self.base_url.rstrip("/") + "/api/embeddings",
                json={"model": payload["model"], "prompt": texts[0]},
                timeout=self.timeout_seconds,
            )
            legacy.raise_for_status()
            embedding = legacy.json().get("embedding")
            return [embedding] if isinstance(embedding, list) else []
        response.raise_for_status()
        data = response.json()
        embeddings = data.get("embeddings")
        if isinstance(embeddings, list):
            return [list(item) for item in embeddings if isinstance(item, list)]
        embedding = data.get("embedding")
        return [embedding] if isinstance(embedding, list) else []

    def health_check(self) -> Dict[str, Any]:
        try:
            response = httpx.get(self.base_url.rstrip("/") + "/api/tags", timeout=5.0)
            return {
                "ok": response.status_code < 500,
                "provider": self.profile.provider,
                "base_url": self.base_url,
                "status_code": response.status_code,
            }
        except Exception as exc:
            return {"ok": False, "provider": self.profile.provider, "base_url": self.base_url, "error": str(exc)}
