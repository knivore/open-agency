from __future__ import annotations

import asyncio
import httpx
import os
from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from app.domain import ModelProfileDefinition, ModelProviderDefinition, ModelProviderType

CURATED_MODEL_OPTIONS: dict[str, list[dict[str, str]]] = {
    "openai": [
        {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
        {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
        {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
        {"id": "gpt-5.5", "name": "GPT-5.5"},
        {"id": "gpt-5.5-pro", "name": "GPT-5.5 Pro"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-pro", "name": "GPT-5.4 Pro"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
        {"id": "gpt-5.4-nano", "name": "GPT-5.4 nano"},
        {"id": "gpt-5-mini", "name": "GPT-5 mini"},
        {"id": "gpt-5-nano", "name": "GPT-5 nano"},
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "gpt-4.1", "name": "GPT-4.1"},
        {"id": "gpt-4.1-mini", "name": "GPT-4.1 mini"},
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4o-mini", "name": "GPT-4o mini"},
    ],
    "openai_codex": [
        {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol"},
        {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"},
        {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna"},
        {"id": "gpt-5.4", "name": "GPT-5.4"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini"},
        {"id": "gpt-5.3-codex", "name": "GPT-5.3 Codex"},
        {"id": "gpt-5.3-codex-spark", "name": "GPT-5.3 Codex Spark"},
        {"id": "gpt-5.2", "name": "GPT-5.2"},
    ],
    "anthropic": [
        {"id": "claude-opus-4-7", "name": "Claude Opus 4.7"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001", "name": "Claude Haiku 4.5"},
        {"id": "claude-sonnet-4-5", "name": "Claude Sonnet 4.5"},
        {"id": "claude-opus-4-1", "name": "Claude Opus 4.1"},
        {"id": "claude-3-5-haiku-latest", "name": "Claude 3.5 Haiku"},
    ],
    "google": [
        {"id": "gemini-3.1-pro-preview", "name": "Gemini 3.1 Pro Preview"},
        {"id": "gemini-3.1-pro-preview-customtools", "name": "Gemini 3.1 Pro Preview Custom Tools"},
        {"id": "gemini-3-flash-preview", "name": "Gemini 3 Flash Preview"},
        {"id": "gemini-3.1-flash-lite", "name": "Gemini 3.1 Flash-Lite"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"id": "gemini-2.5-flash-lite", "name": "Gemini 2.5 Flash-Lite"},
    ],
    "azure_openai": [
        {"id": "gpt-5.5", "name": "GPT-5.5 deployment"},
        {"id": "gpt-5.4", "name": "GPT-5.4 deployment"},
        {"id": "gpt-5.4-mini", "name": "GPT-5.4 mini deployment"},
        {"id": "gpt-4o", "name": "GPT-4o deployment"},
        {"id": "gpt-4.1", "name": "GPT-4.1 deployment"},
    ],
    "ollama": [
        {"id": "llama3:8b", "name": "Llama 3 8B"},
        {"id": "qwen3:30b", "name": "Qwen3 30B"},
        {"id": "deepseek-r1:14b", "name": "DeepSeek R1 14B"},
    ],
    "openai_compatible": [
        {"id": "model-id", "name": "Custom model ID"},
    ],
    "huggingface": [
        {"id": "meta-llama/Llama-3.1-8B-Instruct", "name": "Llama 3.1 8B Instruct"},
        {"id": "mistralai/Mistral-7B-Instruct-v0.3", "name": "Mistral 7B Instruct"},
    ],
    "xai": [
        {"id": "grok-4.3", "name": "Grok 4.3"},
        {"id": "grok-4.20", "name": "Grok 4.20"},
        {"id": "grok-4-1-fast-reasoning", "name": "Grok 4.1 Fast Reasoning"},
        {"id": "grok-4-1-fast-non-reasoning", "name": "Grok 4.1 Fast Non-Reasoning"},
        {"id": "grok-4-fast-reasoning", "name": "Grok 4 Fast Reasoning"},
        {"id": "grok-4-fast-non-reasoning", "name": "Grok 4 Fast Non-Reasoning"},
        {"id": "grok-4", "name": "Grok 4"},
    ],
    "deepseek": [
        {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
        {"id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro"},
        {"id": "deepseek-chat", "name": "DeepSeek Chat (legacy)"},
        {"id": "deepseek-reasoner", "name": "DeepSeek Reasoner (legacy)"},
    ],
    "qwen": [
        {"id": "qwen-plus", "name": "Qwen Plus"},
        {"id": "qwen-max", "name": "Qwen Max"},
        {"id": "qwen-flash", "name": "Qwen Flash"},
        {"id": "qwen3.5-plus", "name": "Qwen3.5 Plus"},
        {"id": "qwen3.5-flash", "name": "Qwen3.5 Flash"},
        {"id": "qwen3-coder-plus", "name": "Qwen3 Coder Plus"},
    ],
}


@dataclass(slots=True)
class ModelCatalogService:
    context: ApiContext

    def _provider_type(self, provider: ModelProviderDefinition) -> str:
        return provider.provider_type.value if hasattr(provider.provider_type, "value") else str(
            provider.provider_type
        )

    def _provider_family(self, provider: ModelProviderDefinition) -> str:
        family = (provider.config or {}).get("provider_family")
        return str(family or self._provider_type(provider)).strip().lower()

    def _provider_base_url(self, provider: ModelProviderDefinition) -> str | None:
        configured = provider.endpoint.base_url if provider.endpoint else (provider.config or {}).get("base_url")
        if configured:
            return configured
        provider_type = self._provider_type(provider)
        if provider_type == ModelProviderType.OPENAI.value:
            return "https://api.openai.com/v1"
        if provider_type == ModelProviderType.OPENAI_CODEX.value:
            return "https://api.openai.com/v1"
        if provider_type == ModelProviderType.DEEPSEEK.value:
            return "https://api.deepseek.com"
        if provider_type == ModelProviderType.QWEN.value:
            return "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        return None

    def _provider_auth_profile(self, provider: ModelProviderDefinition) -> dict[str, Any]:
        config = dict(provider.config or {})
        profile_id = config.get("default_oauth_profile_id") or "default"
        auth_profiles = config.get("auth_profiles")
        if isinstance(auth_profiles, dict):
            record = auth_profiles.get(profile_id)
            if isinstance(record, dict):
                return {**config, **record}
        return config

    def _provider_api_key(self, provider: ModelProviderDefinition) -> str | None:
        config = self._provider_auth_profile(provider)
        provider_type = self._provider_type(provider)
        explicit = config.get("api_key") or config.get("apiKey") or config.get("access_token")
        if explicit:
            return str(explicit)
        if provider_type == ModelProviderType.OPENAI.value:
            return os.getenv("OPENAI_API_KEY")
        if provider_type == ModelProviderType.ANTHROPIC.value:
            return os.getenv("ANTHROPIC_API_KEY")
        if provider_type == ModelProviderType.GOOGLE.value:
            return os.getenv("GOOGLE_API_KEY")
        if provider_type == ModelProviderType.DEEPSEEK.value:
            return os.getenv("DEEPSEEK_API_KEY")
        if provider_type == ModelProviderType.QWEN.value:
            return os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY")
        return None

    def _curated_models(self, provider: ModelProviderDefinition) -> list[dict[str, str]]:
        provider_type = self._provider_type(provider)
        family = self._provider_family(provider)
        return (
                CURATED_MODEL_OPTIONS.get(family)
                or CURATED_MODEL_OPTIONS.get(provider_type)
                or CURATED_MODEL_OPTIONS["openai_compatible"]
        )

    def _model_response(
            self,
            provider: ModelProviderDefinition,
            *,
            source: str,
            models: list[dict[str, Any]],
            error: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "target_type": "model_provider",
            "target_id": provider.id,
            "provider_type": self._provider_type(provider),
            "provider_family": self._provider_family(provider),
            "source": source,
            "models": models,
        }
        if error:
            payload["error"] = error
        return payload

    def _fallback_model_response(self, provider: ModelProviderDefinition, error: str | None = None) -> dict[str, Any]:
        return self._model_response(
            provider,
            source="curated",
            models=self._curated_models(provider),
            error=error,
        )

    def _openai_models_url(self, base_url: str) -> str:
        normalized = base_url.rstrip("/")
        return f"{normalized}/models"

    def _anthropic_models_url(self, base_url: str) -> str:
        normalized = base_url.rstrip("/")
        return f"{normalized}/models" if normalized.endswith("/v1") else f"{normalized}/v1/models"

    async def _list_ollama_models(self, provider: ModelProviderDefinition) -> dict[str, Any]:
        base_url = self._provider_base_url(provider) or os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
        data = response.json()
        models = [
            {"id": item["name"], "name": item["name"]}
            for item in data.get("models", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        return self._model_response(provider, source="live", models=models or self._curated_models(provider))

    async def _list_openai_compatible_models(self, provider: ModelProviderDefinition) -> dict[str, Any]:
        base_url = self._provider_base_url(provider)
        if not base_url:
            return self._fallback_model_response(provider, "Provider base URL is not configured.")
        headers: dict[str, str] = {}
        api_key = self._provider_api_key(provider)
        if (
                self._provider_type(provider)
                in {
            ModelProviderType.OPENAI.value,
            ModelProviderType.OPENAI_CODEX.value,
            ModelProviderType.DEEPSEEK.value,
            ModelProviderType.QWEN.value,
        }
                and not api_key
        ):
            return self._fallback_model_response(
                provider,
                f"{self._provider_type(provider)} API key or OAuth token is not configured.",
            )
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(self._openai_models_url(base_url), headers=headers)
            response.raise_for_status()
        data = response.json()
        models = [
            {"id": item["id"], "name": item.get("id")}
            for item in data.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return self._model_response(provider, source="live", models=models or self._curated_models(provider))

    async def _list_anthropic_models(self, provider: ModelProviderDefinition) -> dict[str, Any]:
        api_key = self._provider_api_key(provider)
        if not api_key:
            return self._fallback_model_response(provider, "Anthropic API key is not configured.")
        base_url = self._provider_base_url(provider) or "https://api.anthropic.com"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                self._anthropic_models_url(base_url),
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            response.raise_for_status()
        data = response.json()
        models = [
            {"id": item["id"], "name": item.get("display_name") or item["id"]}
            for item in data.get("data", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        return self._model_response(provider, source="live", models=models or self._curated_models(provider))

    async def _list_google_models(self, provider: ModelProviderDefinition) -> dict[str, Any]:
        api_key = self._provider_api_key(provider)
        if not api_key:
            return self._fallback_model_response(provider, "Google API key is not configured.")
        base_url = (self._provider_base_url(provider) or "https://generativelanguage.googleapis.com").rstrip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url}/v1beta/models", params={"key": api_key})
            response.raise_for_status()
        data = response.json()
        models = []
        for item in data.get("models", []):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            methods = item.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            model_id = item["name"].removeprefix("models/")
            models.append({"id": model_id, "name": item.get("displayName") or model_id})
        return self._model_response(provider, source="live", models=models or self._curated_models(provider))

    def _profile_for_provider_health(self, provider: ModelProviderDefinition) -> ModelProfileDefinition:
        config = dict(provider.config or {})
        auth_profile_id = config.get("default_oauth_profile_id") or "default"
        auth_profiles = config.get("auth_profiles")
        auth_config = {}
        if isinstance(auth_profiles, dict):
            record = auth_profiles.get(auth_profile_id)
            if isinstance(record, dict):
                auth_config = dict(record)
        model = (
                config.get("health_check_model")
                or config.get("default_model")
                or config.get("model")
                or "health-check"
        )
        base_url = provider.endpoint.base_url if provider.endpoint else config.get("base_url")
        return ModelProfileDefinition(
            id=f"{provider.id}-health",
            name=f"{provider.name} Health Check",
            provider=provider.provider_type.value,
            model=str(model),
            base_url=base_url,
            parameters={
                "provider_id": provider.id,
                "skip_provider_hydration": True,
                "auth_mode": auth_config.get("auth_mode") or config.get("auth_mode"),
                "access_token": auth_config.get("access_token") or config.get("access_token"),
                "refresh_token": auth_config.get("refresh_token") or config.get("refresh_token"),
                "expires_at": auth_config.get("expires_at") or config.get("expires_at"),
                "client_id": auth_config.get("client_id") or config.get("client_id"),
                "redirect_uri": auth_config.get("redirect_uri") or config.get("redirect_uri"),
                "oauth_profile_id": auth_profile_id,
                "account_id": auth_config.get("account_id") or config.get("account_id"),
            },
        )

    async def _health_payload(self, profile: ModelProfileDefinition) -> dict[str, Any]:
        try:
            client = self.context.llm_provider_registry.resolve(profile)
            health = await asyncio.to_thread(client.health_check)
            payload = {
                "ok": bool(health.get("ok")),
                "provider": profile.provider,
                "model": profile.model,
                "health": health,
            }
            for key in (
                    "auth_status",
                    "auth_required",
                    "reauthorization_required",
                    "auth_mode",
                    "auth_action",
                    "auth_endpoint",
                    "auth_profile_id",
                    "provider_id",
                    "error_code",
            ):
                if key in health:
                    payload[key] = health[key]
            if "error" in health:
                payload["error"] = health["error"]
            return payload
        except Exception as exc:
            return {
                "ok": False,
                "provider": profile.provider,
                "model": profile.model,
                "error": str(exc),
            }

    async def test_model_provider(self, provider_id: str) -> dict[str, Any] | None:
        provider = await self.context.model_provider_repo.get(provider_id)
        if provider is None:
            return None
        profile = self._profile_for_provider_health(provider)
        return {
            "target_type": "model_provider",
            "target_id": provider.id,
            "provider_type": provider.provider_type.value,
            **await self._health_payload(profile),
        }

    async def test_model_profile(self, profile_id: str) -> dict[str, Any] | None:
        profile = await self.context.model_profile_repo.get(profile_id)
        if profile is None:
            return None
        return {
            "target_type": "model_profile",
            "target_id": profile.id,
            **await self._health_payload(profile),
        }

    async def list_model_provider_models(self, provider_id: str) -> dict[str, Any] | None:
        provider = await self.context.model_provider_repo.get(provider_id)
        if provider is None:
            return None
        provider_type = self._provider_type(provider)
        family = self._provider_family(provider)
        try:
            if provider_type == ModelProviderType.OLLAMA.value:
                return await self._list_ollama_models(provider)
            if provider_type == ModelProviderType.GOOGLE.value:
                return await self._list_google_models(provider)
            if provider_type == ModelProviderType.ANTHROPIC.value:
                return await self._list_anthropic_models(provider)
            if provider_type in {
                ModelProviderType.OPENAI.value,
                ModelProviderType.OPENAI_COMPATIBLE.value,
                ModelProviderType.OPENAI_CODEX.value,
                ModelProviderType.DEEPSEEK.value,
                ModelProviderType.QWEN.value,
            }:
                return await self._list_openai_compatible_models(provider)
        except Exception as exc:
            return self._fallback_model_response(provider, str(exc))

        if family in {"xai", "deepseek", "qwen"}:
            try:
                return await self._list_openai_compatible_models(provider)
            except Exception as exc:
                return self._fallback_model_response(provider, str(exc))
        return self._fallback_model_response(provider)

    async def sync_mcp_catalog(self, server_id: str | None = None) -> dict[str, Any]:
        return await self.context.sync_mcp_catalog(server_id=server_id)

    async def ensure_builtin_mcp_servers_seeded(self) -> dict[str, Any]:
        seeded = await self.context.ensure_builtin_mcp_servers_seed_data()
        return {server_id: item.model_dump(mode="json") for server_id, item in seeded.items()}

    async def ensure_runtime_adapters_seeded(self) -> None:
        await self.context.ensure_runtime_adapter_seed_data()
