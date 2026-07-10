from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.domain import ModelProfileDefinition
from app.llm.base import BaseModelClient
from app.llm.fallback import FallbackModelClient, build_fallback_profiles


@dataclass(slots=True)
class LLMEnvironmentConfig:
    local_openai_base_url: Optional[str] = None
    local_openai_api_key: Optional[str] = None
    deepseek_base_url: Optional[str] = None
    qwen_base_url: Optional[str] = None
    ollama_base_url: Optional[str] = None
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None
    deepseek_api_key: Optional[str] = None
    qwen_api_key: Optional[str] = None
    model_provider_repo: Optional[Any] = None

    @classmethod
    def from_env(cls, model_provider_repo: Optional[Any] = None) -> "LLMEnvironmentConfig":
        return cls(
            local_openai_base_url=os.getenv("LOCAL_OPENAI_BASE_URL"),
            local_openai_api_key=os.getenv("LOCAL_OPENAI_API_KEY"),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL"),
            qwen_base_url=os.getenv("QWEN_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            google_api_key=os.getenv("GOOGLE_API_KEY"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
            qwen_api_key=os.getenv("QWEN_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
            model_provider_repo=model_provider_repo,
        )


ProviderFactory = Callable[[ModelProfileDefinition, LLMEnvironmentConfig], BaseModelClient]


class ModelProviderRegistry:
    def __init__(self, env_config: Optional[LLMEnvironmentConfig] = None):
        self._providers: Dict[str, ProviderFactory] = {}
        self._env_config = env_config or LLMEnvironmentConfig.from_env()

    def register(self, key: str, factory: ProviderFactory) -> None:
        self._providers[key] = factory

    def get_factory(self, key: str) -> ProviderFactory:
        if key not in self._providers:
            raise KeyError(f"Provider '{key}' is not registered")
        return self._providers[key]

    def _normalize_provider_key(self, key: str) -> str:
        return key.strip().lower().replace("-", "_")

    def resolve_provider_key(self, profile: ModelProfileDefinition) -> str:
        profile_provider = self._normalize_provider_key(profile.provider)

        if profile_provider in self._providers:
            return profile_provider

        if profile.base_url:
            if "11434" in profile.base_url and "ollama" in self._providers:
                return "ollama"
            if "openai_compatible" in self._providers:
                return "openai_compatible"

        if self._env_config.local_openai_base_url and profile_provider in {"local", "openai"}:
            return "openai_compatible"

        raise KeyError(f"No provider registered for profile provider '{profile.provider}'")

    def _resolve_single(self, profile: ModelProfileDefinition) -> BaseModelClient:
        key = self.resolve_provider_key(profile)
        factory = self.get_factory(key)
        return factory(profile, self._env_config)

    def resolve(self, profile: ModelProfileDefinition) -> BaseModelClient:
        client = self._resolve_single(profile)
        fallback_profiles = build_fallback_profiles(profile)
        if not fallback_profiles:
            return client
        return FallbackModelClient(
            primary_profile=profile,
            primary_client=client,
            fallback_profiles=fallback_profiles,
            client_factory=self._resolve_single,
        )

    @classmethod
    def create_default(cls, env_config: Optional[LLMEnvironmentConfig] = None) -> "ModelProviderRegistry":
        from app.llm.anthropic import AnthropicModelClient
        from app.llm.azure import AzureOpenAIModelClient
        from app.llm.bedrock import BedrockModelClient
        from app.llm.google import GoogleModelClient
        from app.llm.ollama import OllamaModelClient
        from app.llm.openai_codex import OpenAICodexModelClient
        from app.llm.openai_compatible import OpenAICompatibleModelClient

        registry = cls(env_config=env_config)
        registry.register("openai_compatible", lambda profile, env: OpenAICompatibleModelClient(profile, env))
        registry.register("openai", lambda profile, env: OpenAICompatibleModelClient(profile, env))
        registry.register("deepseek", lambda profile, env: OpenAICompatibleModelClient(profile, env))
        registry.register("qwen", lambda profile, env: OpenAICompatibleModelClient(profile, env))
        registry.register("openai_codex", lambda profile, env: OpenAICodexModelClient(profile, env))
        registry.register("azure_openai", lambda profile, env: AzureOpenAIModelClient(profile, env))
        registry.register("lm_studio", lambda profile, env: OpenAICompatibleModelClient(profile, env))
        registry.register("vllm", lambda profile, env: OpenAICompatibleModelClient(profile, env))
        registry.register("ollama", lambda profile, env: OllamaModelClient(profile, env))
        registry.register("anthropic", lambda profile, env: AnthropicModelClient(profile, env))
        registry.register("google", lambda profile, env: GoogleModelClient(profile, env))
        registry.register("aws_bedrock", lambda profile, env: BedrockModelClient(profile, env))
        return registry
