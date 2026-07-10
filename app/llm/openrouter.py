from __future__ import annotations

from typing import Any, Dict, Optional

from openai import OpenAI

from app.domain import ModelProfileDefinition
from app.llm.openai_compatible import OpenAICompatibleModelClient
from app.llm.registry import LLMEnvironmentConfig

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterModelClient(OpenAICompatibleModelClient):
    """OpenRouter adapter behind Agency's shared OpenAI-compatible client flow."""

    provider_key = "openrouter"

    def __init__(self, profile: ModelProfileDefinition, env_config: LLMEnvironmentConfig):
        self.profile = profile
        self.env_config = env_config
        self.base_url = profile.base_url or env_config.openrouter_base_url or DEFAULT_OPENROUTER_BASE_URL
        self.api_key = profile.api_key_ref or env_config.openrouter_api_key or "not-required"
        self._site_url = (env_config.openrouter_site_url or "").strip() or None
        self._app_name = (env_config.openrouter_app_name or "").strip() or None
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def _chat_options(
            self,
            *,
            temperature: Optional[float],
            max_tokens: Optional[int],
            stream: bool,
            **kwargs: Any,
    ) -> Dict[str, Any]:
        options = super()._chat_options(
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            **kwargs,
        )
        route_models = self._route_models(kwargs)
        if route_models:
            options["models"] = route_models
        provider = self._provider_preferences(kwargs)
        if provider:
            options["provider"] = provider
        max_price = kwargs.pop("max_price", self.profile.parameters.get("max_price"))
        if max_price is not None:
            options["max_price"] = max_price
        extra_headers = self._extra_headers()
        if extra_headers:
            options["extra_headers"] = extra_headers
        return options

    def _route_models(self, kwargs: Dict[str, Any]) -> list[str]:
        raw_models = kwargs.pop("models", None)
        if raw_models is None:
            raw_models = self.profile.parameters.get("models") or self.profile.parameters.get("route_models")
        normalized = [str(item).strip() for item in raw_models or [] if str(item).strip()]
        seen: set[str] = set()
        ordered: list[str] = []
        primary = self.profile.model.strip()
        for candidate in [primary, *normalized]:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            ordered.append(candidate)
        # OpenRouter only needs `models` when there is an actual fallback chain.
        return ordered if len(ordered) > 1 else []

    def _provider_preferences(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        provider: Dict[str, Any] = {}
        parameter_map = {
            "order": ("provider_order", "order"),
            "allow_fallbacks": ("allow_fallbacks",),
            "require_parameters": ("require_parameters",),
            "data_collection": ("data_collection",),
            "zdr": ("zdr",),
            "only": ("provider_only", "only"),
            "ignore": ("provider_ignore", "ignore"),
            "sort": ("provider_sort", "sort"),
            "preferred_min_throughput": ("preferred_min_throughput",),
            "preferred_max_latency": ("preferred_max_latency",),
        }
        for target_key, source_keys in parameter_map.items():
            value = None
            for source_key in source_keys:
                if source_key in kwargs:
                    value = kwargs.pop(source_key)
                    break
                if source_key in self.profile.parameters:
                    value = self.profile.parameters.get(source_key)
                    break
            if value is None:
                continue
            provider[target_key] = value
        return provider

    def _extra_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._site_url:
            headers["HTTP-Referer"] = self._site_url
        if self._app_name:
            headers["X-OpenRouter-Title"] = self._app_name
        return headers
