from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any, Optional

from app.domain import ModelFallbackPolicy, ModelFallbackTarget, ModelProfileDefinition
from app.llm.base import BaseModelClient, ModelMessage, ModelResponse, ModelStreamEvent

DEFAULT_AUTO_FALLBACK_MODELS: dict[str, tuple[str, str]] = {
    "openai": ("gpt-5-mini", "gpt-4o-mini"),
    "openai_compatible": ("gpt-4o-mini", "gpt-4.1-mini"),
    "openai_codex": ("gpt-5.6-luna", "gpt-5.4-mini"),
    "azure_openai": ("gpt-5.4-mini", "gpt-4o"),
    "anthropic": ("claude-sonnet-4-5", "claude-3-5-haiku-latest"),
    "google": ("gemini-2.5-flash", "gemini-2.5-flash-lite"),
    "ollama": ("llama3:8b", "qwen3:30b"),
    "deepseek": ("deepseek-v4-flash", "deepseek-v4-pro"),
    "qwen": ("qwen-plus", "qwen-flash"),
}

CURATED_FALLBACK_MODEL_CAPABILITIES: dict[str, dict[str, dict[str, bool]]] = {
    "openai": {
        "gpt-5-mini": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
        "gpt-4o-mini": {"tools": True, "structured_output": True, "vision": True, "streaming": True},
    },
    "openai_compatible": {
        "gpt-4o-mini": {"tools": True, "structured_output": True, "vision": True, "streaming": True},
        "gpt-4.1-mini": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
    },
    "openai_codex": {
        "gpt-5.6-luna": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
        "gpt-5.4-mini": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
    },
    "azure_openai": {
        "gpt-5.4-mini": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
        "gpt-4o": {"tools": True, "structured_output": True, "vision": True, "streaming": True},
    },
    "anthropic": {
        "claude-sonnet-4-5": {"tools": True, "structured_output": False, "vision": True, "streaming": True},
        "claude-3-5-haiku-latest": {"tools": True, "structured_output": False, "vision": True, "streaming": True},
    },
    "google": {
        "gemini-2.5-flash": {"tools": True, "structured_output": True, "vision": True, "streaming": True},
        "gemini-2.5-flash-lite": {"tools": True, "structured_output": True, "vision": True, "streaming": True},
    },
    "ollama": {
        "llama3:8b": {"tools": False, "structured_output": False, "vision": False, "streaming": True},
        "qwen3:30b": {"tools": True, "structured_output": False, "vision": False, "streaming": True},
    },
    "deepseek": {
        "deepseek-v4-flash": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
        "deepseek-v4-pro": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
    },
    "qwen": {
        "qwen-plus": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
        "qwen-flash": {"tools": True, "structured_output": True, "vision": False, "streaming": True},
    },
}

FALLBACK_STATUS_CODES = {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}
FALLBACK_ERROR_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "connection",
    "connect",
    "network",
    "unauthorized",
    "forbidden",
    "access denied",
    "429",
    "408",
    "401",
    "403",
    "500",
    "502",
    "503",
    "504",
)


class ModelFallbackExhaustedError(RuntimeError):
    """Raised after every fallback candidate has failed for a retryable reason."""

    def __init__(self, message: str, *, attempts: list[dict[str, Any]], last_error: Exception):
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


def fallback_error_category(exc: Exception) -> str | None:
    status_code = _exception_status_code(exc)
    if status_code in {401, 403}:
        return "auth"
    if status_code == 429 or "rate" in exc.__class__.__name__.lower():
        return "rate_limit"
    if status_code in {408, 425, 504}:
        return "timeout"
    if status_code in {409, 500, 502, 503}:
        return "service_unavailable"

    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    if "ratelimit" in name or "rate limit" in text or "rate_limit" in text or "too many requests" in text or "quota" in text or "429" in text:
        return "rate_limit"
    if "timeout" in name or "timeout" in text or "timed out" in text or "408" in text:
        return "timeout"
    if any(marker in name for marker in ("connection", "network")) or any(
            marker in text for marker in ("connection", "connect", "network")):
        return "network"
    if any(marker in text for marker in ("unauthorized", "forbidden", "access denied", "401", "403")):
        return "auth"
    if any(marker in text for marker in
           ("temporarily unavailable", "service unavailable", "overloaded", "500", "502", "503", "504")):
        return "service_unavailable"
    return None


def should_try_model_fallback(exc: Exception, policy: ModelFallbackPolicy | None = None) -> bool:
    category = fallback_error_category(exc)
    if category is None:
        return any(marker in str(exc).lower() for marker in FALLBACK_ERROR_MARKERS)
    if policy is not None and category not in set(policy.retry_on):
        return False
    return True


def build_fallback_profiles(profile: ModelProfileDefinition) -> list[ModelProfileDefinition]:
    if profile.fallback_strategy == "disabled":
        return []

    if profile.fallback_models:
        targets = profile.fallback_models[:5]
    elif profile.fallback_strategy == "manual":
        targets = []
    else:
        targets = [
            ModelFallbackTarget(model=model)
            for model in DEFAULT_AUTO_FALLBACK_MODELS.get(_provider_key(profile.provider), ())
            if model != profile.model and _auto_target_compatible(profile, ModelFallbackTarget(model=model))
        ][:2]

    profiles: list[ModelProfileDefinition] = []
    seen = {(profile.provider, profile.model, profile.base_url)}
    for index, target in enumerate(targets[:5], start=1):
        if profile.fallback_policy.same_provider_only and target.provider and target.provider != profile.provider:
            continue
        if not _auto_target_compatible(profile, target):
            continue
        fallback_profile = _profile_from_target(profile, target, index=index)
        identity = (fallback_profile.provider, fallback_profile.model, fallback_profile.base_url)
        if identity in seen:
            continue
        seen.add(identity)
        profiles.append(fallback_profile)
    return profiles


class FallbackModelClient:
    """Try backup model profiles only for transient provider/access failures."""

    provider_key = "fallback"

    def __init__(
            self,
            *,
            primary_profile: ModelProfileDefinition,
            primary_client: BaseModelClient,
            fallback_profiles: list[ModelProfileDefinition],
            client_factory: Callable[[ModelProfileDefinition], BaseModelClient],
    ):
        self.profile = primary_profile
        self.primary_client = primary_client
        self.provider_key = str(getattr(primary_client, "provider_key", "fallback"))
        self.fallback_profiles = fallback_profiles
        self._client_factory = client_factory
        self._fallback_clients: dict[str, BaseModelClient] = {}

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary_client, name)

    def generate_text(
            self,
            messages: list[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        return self._try_call(
            "generate_text",
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def agenerate_text(
            self,
            messages: list[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        return await self._atry_call(
            "generate_text",
            "agenerate_text",
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def generate_structured(
            self,
            messages: list[ModelMessage],
            *,
            schema: dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        return self._try_call(
            "generate_structured",
            messages,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    async def agenerate_structured(
            self,
            messages: list[ModelMessage],
            *,
            schema: dict[str, Any],
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> ModelResponse:
        return await self._atry_call(
            "generate_structured",
            "agenerate_structured",
            messages,
            schema=schema,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

    def stream_text(
            self,
            messages: list[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[str]:
        last_error: Exception | None = None
        for index, (_profile, client) in enumerate(self._clients()):
            try:
                stream = client.stream_text(messages, temperature=temperature, max_tokens=max_tokens, **kwargs)
                for chunk in stream:
                    yield chunk
                return
            except Exception as exc:
                if not should_try_model_fallback(exc, self.profile.fallback_policy):
                    raise
                last_error = exc
                if index >= len(self.fallback_profiles):
                    break
        if last_error is not None:
            raise last_error

    def stream_generate_text(
            self,
            messages: list[ModelMessage],
            *,
            temperature: Optional[float] = None,
            max_tokens: Optional[int] = None,
            **kwargs: Any,
    ) -> Iterator[ModelStreamEvent]:
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        for index, (profile, client) in enumerate(self._clients()):
            emitted_text = False
            try:
                stream_method = getattr(client, "stream_generate_text", None)
                if stream_method is None:
                    response = client.generate_text(
                        messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                    )
                    yield ModelStreamEvent(
                        response=self._annotate_response(response, profile, index=index, attempts=attempts)
                    )
                    return
                for event in stream_method(
                        messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **kwargs,
                ):
                    if event.text_delta:
                        emitted_text = True
                    if event.response is not None:
                        event.response = self._annotate_response(
                            event.response,
                            profile,
                            index=index,
                            attempts=attempts,
                        )
                    yield event
                return
            except Exception as exc:
                attempts.append(self._attempt_record(profile, index=index, exc=exc))
                # Once a partial answer is visible, switching models would duplicate or
                # contradict it; surface the failure instead of silently restarting.
                if emitted_text or not should_try_model_fallback(exc, self.profile.fallback_policy):
                    raise
                last_error = exc
                if index >= len(self.fallback_profiles):
                    break
        if last_error is not None:
            raise ModelFallbackExhaustedError(
                f"All model fallback attempts failed after {len(attempts)} attempt(s): {last_error}",
                attempts=attempts,
                last_error=last_error,
            ) from last_error

    def count_tokens(self, messages: list[ModelMessage], **kwargs: Any) -> Optional[int]:
        return self.primary_client.count_tokens(messages, **kwargs)

    def embed_texts(self, texts: list[str], **kwargs: Any) -> list[list[float]]:
        return self._try_call("embed_texts", texts, **kwargs)

    def health_check(self) -> dict[str, Any]:
        primary_health = self.primary_client.health_check()
        payload = dict(primary_health)
        payload["primary"] = primary_health
        payload["fallbacks"] = []
        for profile, client in self._clients(include_primary=False):
            health = client.health_check()
            payload["fallbacks"].append(
                {
                    "profile_id": profile.id,
                    "provider": profile.provider,
                    "model": profile.model,
                    **health,
                }
            )
        payload["ok"] = bool(primary_health.get("ok")) or any(item.get("ok") for item in payload["fallbacks"])
        return payload

    def _try_call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        for index, (profile, client) in enumerate(self._clients()):
            try:
                method = getattr(client, method_name)
                response = method(*args, **kwargs)
                return self._annotate_response(response, profile, index=index, attempts=attempts)
            except Exception as exc:
                attempts.append(self._attempt_record(profile, index=index, exc=exc))
                if not should_try_model_fallback(exc, self.profile.fallback_policy):
                    raise
                last_error = exc
                if index >= len(self.fallback_profiles):
                    break
        if last_error is not None:
            raise ModelFallbackExhaustedError(
                f"All model fallback attempts failed after {len(attempts)} attempt(s): {last_error}",
                attempts=attempts,
                last_error=last_error,
            ) from last_error

    async def _atry_call(self, sync_method_name: str, async_method_name: str, *args: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        for index, (profile, client) in enumerate(self._clients()):
            try:
                async_method = getattr(client, async_method_name, None)
                if async_method is not None:
                    response = await async_method(*args, **kwargs)
                else:
                    sync_method = getattr(client, sync_method_name)
                    response = await asyncio.to_thread(sync_method, *args, **kwargs)
                return self._annotate_response(response, profile, index=index, attempts=attempts)
            except Exception as exc:
                attempts.append(self._attempt_record(profile, index=index, exc=exc))
                if not should_try_model_fallback(exc, self.profile.fallback_policy):
                    raise
                last_error = exc
                if index >= len(self.fallback_profiles):
                    break
        if last_error is not None:
            raise ModelFallbackExhaustedError(
                f"All model fallback attempts failed after {len(attempts)} attempt(s): {last_error}",
                attempts=attempts,
                last_error=last_error,
            ) from last_error

    def _clients(self, *, include_primary: bool = True) -> Iterator[tuple[ModelProfileDefinition, BaseModelClient]]:
        if include_primary:
            yield self.profile, self.primary_client
        for profile in self.fallback_profiles:
            yield profile, self._fallback_client(profile)

    def _fallback_client(self, profile: ModelProfileDefinition) -> BaseModelClient:
        client = self._fallback_clients.get(profile.id)
        if client is None:
            client = self._client_factory(profile)
            self._fallback_clients[profile.id] = client
        return client

    def _attempt_record(self, profile: ModelProfileDefinition, *, index: int, exc: Exception) -> dict[str, Any]:
        return {
            "provider": profile.provider,
            "model": profile.model,
            "fallback_index": index,
            "error_type": exc.__class__.__name__,
            "error": str(exc),
            "retryable": should_try_model_fallback(exc, self.profile.fallback_policy),
            "category": fallback_error_category(exc),
        }

    def _annotate_response(
            self,
            response: Any,
            profile: ModelProfileDefinition,
            *,
            index: int,
            attempts: list[dict[str, Any]],
    ) -> Any:
        if not isinstance(response, ModelResponse) or index == 0:
            return response
        usage = dict(response.usage or {})
        usage["model_fallback"] = {
            "used": True,
            "primary_provider": self.profile.provider,
            "primary_model": self.profile.model,
            "fallback_provider": profile.provider,
            "fallback_model": profile.model,
            "fallback_index": index,
            "attempts": attempts,
        }
        response.usage = usage
        return response


def _profile_from_target(
        profile: ModelProfileDefinition,
        target: ModelFallbackTarget,
        *,
        index: int,
) -> ModelProfileDefinition:
    parameters = dict(profile.parameters)
    parameters.update(target.parameters)
    return ModelProfileDefinition(
        id=f"{profile.id}-fallback-{index}",
        name=target.name or f"{profile.name} fallback {index}",
        provider=(target.provider or profile.provider).strip(),
        model=target.model.strip(),
        description=target.description or f"Fallback model {index} for {profile.name}",
        base_url=target.base_url if target.base_url is not None else profile.base_url,
        api_key_ref=target.api_key_ref if target.api_key_ref is not None else profile.api_key_ref,
        temperature=target.temperature if target.temperature is not None else profile.temperature,
        max_tokens=target.max_tokens if target.max_tokens is not None else profile.max_tokens,
        context_window=target.context_window if target.context_window is not None else profile.context_window,
        top_p=target.top_p if target.top_p is not None else profile.top_p,
        supports_tools=target.supports_tools if target.supports_tools is not None else profile.supports_tools,
        supports_structured_output=(
            target.supports_structured_output
            if target.supports_structured_output is not None
            else profile.supports_structured_output
        ),
        supports_vision=target.supports_vision if target.supports_vision is not None else profile.supports_vision,
        supports_streaming=target.supports_streaming if target.supports_streaming is not None else profile.supports_streaming,
        parameters=parameters,
        fallback_strategy="disabled",
        fallback_models=[],
        framework_hints=profile.framework_hints,
    )


def _exception_status_code(exc: Exception) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return response_status if isinstance(response_status, int) else None


def _provider_key(provider: str) -> str:
    return provider.strip().lower().replace("-", "_")


def _auto_target_compatible(profile: ModelProfileDefinition, target: ModelFallbackTarget) -> bool:
    if not profile.fallback_policy.require_capability_match:
        return True
    provider = _provider_key(target.provider or profile.provider)
    capabilities = CURATED_FALLBACK_MODEL_CAPABILITIES.get(provider, {}).get(target.model)
    if capabilities is None:
        explicit_values = (
            target.supports_tools,
            target.supports_structured_output,
            target.supports_vision,
            target.supports_streaming,
        )
        if all(value is None for value in explicit_values):
            return True
        capabilities = {
            "tools": bool(target.supports_tools),
            "structured_output": bool(target.supports_structured_output),
            "vision": bool(target.supports_vision),
            "streaming": bool(target.supports_streaming),
        }
    required = {
        "tools": profile.supports_tools,
        "structured_output": profile.supports_structured_output,
        "vision": profile.supports_vision,
        "streaming": profile.supports_streaming,
    }
    return all(not enabled or bool(capabilities.get(name)) for name, enabled in required.items())
