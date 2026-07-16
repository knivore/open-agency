from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.domain import ModelProfileDefinition
from app.core.outbound_http import validate_model_provider_url
from app.llm.fallback import FallbackModelClient, ModelFallbackExhaustedError
from app.llm.base import ModelMessage, ModelResponse, ModelToolCall
from app.llm.azure import AzureOpenAIModelClient
from app.llm.ollama import OllamaModelClient
from app.llm.openai_codex import OpenAICodexModelClient
from app.llm.openai_compatible import OpenAICompatibleModelClient
from app.llm.openai_helpers import sanitize_openai_message_name
from app.llm.openrouter import DEFAULT_OPENROUTER_BASE_URL, OpenRouterModelClient
from app.llm.registry import LLMEnvironmentConfig, ModelProviderRegistry


class FakeModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(
            content="ok",
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=1.0,
        )

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(
            content={"ok": True},
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=1.0,
        )

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "a"
        yield "b"

    def count_tokens(self, messages, **kwargs):
        return 42

    def health_check(self):
        return {"ok": True}


class SwitchingModelClient(FakeModelClient):
    calls: list[str] = []

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        self.__class__.calls.append(self.profile.model)
        if self.profile.model == "primary-model":
            raise TimeoutError("model request timed out")
        return ModelResponse(
            content=f"ok:{self.profile.model}",
            provider=self.profile.provider,
            model=self.profile.model,
            latency_ms=1.0,
        )


class AlwaysFailingModelClient(FakeModelClient):
    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        raise TimeoutError(f"{self.profile.model} timed out")


class ModelProviderRegistryTests(unittest.TestCase):
    def test_official_model_provider_rejects_plaintext_http(self):
        with self.assertRaisesRegex(ValueError, "requires HTTPS"):
            validate_model_provider_url(
                "http://api.openai.com/v1",
                provider_key="openai",
                allowed_custom_hosts=set(),
            )

    @patch("app.llm.openai_compatible.OpenAI")
    def test_custom_compatible_endpoint_does_not_inherit_local_gateway_key(self, openai_cls):
        profile = ModelProfileDefinition(
            name="Custom Compatible",
            provider="openai_compatible",
            model="custom-model",
            base_url="https://custom-provider.example/v1",
        )
        client = OpenAICompatibleModelClient(
            profile,
            LLMEnvironmentConfig(
                local_openai_base_url="http://localhost:11434/v1",
                local_openai_api_key="local-gateway-secret",
            ),
        )

        self.assertEqual(client.api_key, "not-required")
        openai_cls.assert_called_once_with(
            base_url="https://custom-provider.example/v1",
            api_key="not-required",
        )

    def test_sanitize_openai_message_name_strips_invalid_punctuation(self):
        self.assertEqual(sanitize_openai_message_name("Coder Agent"), "Coder_Agent")
        self.assertEqual(sanitize_openai_message_name(" tool/result "), "tool_result")
        self.assertIsNone(sanitize_openai_message_name("   "))

    def test_openai_compatible_serializes_assistant_tool_calls(self):
        profile = ModelProfileDefinition(
            name="OpenAI Compatible",
            provider="openai_compatible",
            model="local-model",
            base_url="http://localhost:1234/v1",
        )
        client = OpenAICompatibleModelClient(profile, LLMEnvironmentConfig(local_openai_api_key="abc"))

        payload = client._to_openai_messages(
            [
                ModelMessage(role="user", content="List tools"),
                ModelMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ModelToolCall(id="call-1", name="ListTools", arguments={"limit": 5})],
                ),
                ModelMessage(role="tool", content='{"ok":true}', name="ListTools", tool_call_id="call-1"),
            ]
        )

        self.assertEqual(payload[1]["role"], "assistant")
        self.assertEqual(
            payload[1]["tool_calls"],
            [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "ListTools", "arguments": "{\"limit\": 5}"},
                }
            ],
        )
        self.assertEqual(payload[2]["tool_call_id"], "call-1")

    def test_openai_clients_sanitize_message_names(self):
        profile = ModelProfileDefinition(
            name="OpenAI Compatible",
            provider="openai_compatible",
            model="local-model",
            base_url="http://localhost:1234/v1",
        )
        messages = [ModelMessage(role="assistant", content="ok", name="Coder Agent")]

        compatible = OpenAICompatibleModelClient(profile, LLMEnvironmentConfig(local_openai_api_key="abc"))
        azure = AzureOpenAIModelClient(
            profile,
            LLMEnvironmentConfig(local_openai_api_key="abc", openai_api_key="abc")
        )
        codex = OpenAICodexModelClient(profile, LLMEnvironmentConfig(local_openai_api_key="abc"))

        self.assertEqual(compatible._to_openai_messages(messages)[0]["name"], "Coder_Agent")
        self.assertEqual(azure._to_openai_messages(messages)[0]["name"], "Coder_Agent")
        self.assertEqual(codex._to_openai_messages(messages)[0]["name"], "Coder_Agent")

        tool_payload = compatible._to_openai_messages(
            [
                ModelMessage(
                    role="assistant",
                    content=None,
                    tool_calls=[ModelToolCall(id="call-1", name="tool/result", arguments={})],
                )
            ]
        )
        self.assertEqual(tool_payload[0]["tool_calls"][0]["function"]["name"], "tool_result")

    def test_registry_register_and_resolve_custom_provider(self):
        registry = ModelProviderRegistry(
            env_config=LLMEnvironmentConfig(local_openai_base_url="http://localhost:1234/v1")
        )
        registry.register("fake", lambda profile, env: FakeModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Fake Profile",
            provider="fake",
            model="fake-model",
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, FakeModelClient)
        self.assertEqual(client.profile.model, "fake-model")

    def test_registry_wraps_manual_fallback_models(self):
        registry = ModelProviderRegistry()
        registry.register("switch", lambda profile, env: SwitchingModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Primary",
            provider="switch",
            model="primary-model",
            fallback_strategy="manual",
            fallback_models=[{"model": "backup-model"}],
        )

        client = registry.resolve(profile)
        result = client.generate_text([ModelMessage(role="user", content="hello")])

        self.assertIsInstance(client, FallbackModelClient)
        self.assertEqual(result.content, "ok:backup-model")
        self.assertEqual(result.model, "backup-model")
        self.assertEqual(result.usage["model_fallback"]["primary_model"], "primary-model")
        self.assertEqual(result.usage["model_fallback"]["attempts"][0]["model"], "primary-model")

    def test_registry_raises_exhausted_error_with_attempts_when_all_fallbacks_fail(self):
        registry = ModelProviderRegistry()
        registry.register("fail", lambda profile, env: AlwaysFailingModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Primary",
            provider="fail",
            model="primary-model",
            fallback_strategy="manual",
            fallback_models=[{"model": "backup-model"}],
        )

        client = registry.resolve(profile)

        with self.assertRaises(ModelFallbackExhaustedError) as raised:
            client.generate_text([ModelMessage(role="user", content="hello")])

        self.assertEqual([attempt["model"] for attempt in raised.exception.attempts], ["primary-model", "backup-model"])

    def test_registry_respects_retry_policy_when_primary_error_is_excluded(self):
        registry = ModelProviderRegistry()
        registry.register("fail", lambda profile, env: AlwaysFailingModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Primary",
            provider="fail",
            model="primary-model",
            fallback_strategy="manual",
            fallback_policy={"retry_on": ["rate_limit"]},
            fallback_models=[{"model": "backup-model"}],
        )

        client = registry.resolve(profile)

        with self.assertRaises(TimeoutError):
            client.generate_text([ModelMessage(role="user", content="hello")])

    def test_registry_filters_cross_provider_backups_when_policy_requires_same_provider(self):
        registry = ModelProviderRegistry()
        registry.register("fake", lambda profile, env: FakeModelClient(profile, env))
        registry.register("other", lambda profile, env: FakeModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Primary",
            provider="fake",
            model="primary-model",
            fallback_strategy="manual",
            fallback_policy={"same_provider_only": True},
            fallback_models=[
                {"provider": "other", "model": "other-backup"},
                {"provider": "fake", "model": "same-provider-backup"},
            ],
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, FallbackModelClient)
        self.assertEqual([fallback.model for fallback in client.fallback_profiles], ["same-provider-backup"])

    def test_registry_respects_disabled_fallback_strategy(self):
        registry = ModelProviderRegistry()
        registry.register("switch", lambda profile, env: SwitchingModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Primary",
            provider="switch",
            model="primary-model",
            fallback_strategy="disabled",
            fallback_models=[{"model": "backup-model"}],
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, SwitchingModelClient)

    def test_auto_fallback_avoids_non_vision_curated_targets_for_vision_profiles(self):
        registry = ModelProviderRegistry()
        registry.register("openai", lambda profile, env: FakeModelClient(profile, env))
        profile = ModelProfileDefinition(
            name="Vision Primary",
            provider="openai",
            model="gpt-4o",
            supports_vision=True,
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, FallbackModelClient)
        self.assertEqual([fallback.model for fallback in client.fallback_profiles], ["gpt-4o-mini"])

    def test_registry_uses_openai_compatible_for_local_base_url(self):
        registry = ModelProviderRegistry.create_default(
            env_config=LLMEnvironmentConfig(local_openai_base_url="http://localhost:1234/v1")
        )
        profile = ModelProfileDefinition(
            name="Local Profile",
            provider="openai",
            model="local-model",
            base_url="http://localhost:1234/v1",
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, FallbackModelClient)
        self.assertIsInstance(client.primary_client, OpenAICompatibleModelClient)
        self.assertEqual(client.base_url, "http://localhost:1234/v1")

    def test_registry_routes_deepseek_through_openai_compatible_defaults(self):
        registry = ModelProviderRegistry.create_default(
            env_config=LLMEnvironmentConfig(deepseek_api_key="deepseek-env-key")
        )
        profile = ModelProfileDefinition(
            name="DeepSeek",
            provider="deepseek",
            model="deepseek-v4-flash",
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, FallbackModelClient)
        self.assertIsInstance(client.primary_client, OpenAICompatibleModelClient)
        self.assertEqual(client.primary_client.base_url, "https://api.deepseek.com")
        self.assertEqual(client.primary_client.api_key, "deepseek-env-key")

    def test_registry_routes_qwen_through_openai_compatible_defaults(self):
        registry = ModelProviderRegistry.create_default(
            env_config=LLMEnvironmentConfig(qwen_api_key="qwen-env-key")
        )
        profile = ModelProfileDefinition(
            name="Qwen",
            provider="qwen",
            model="qwen-plus",
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, FallbackModelClient)
        self.assertIsInstance(client.primary_client, OpenAICompatibleModelClient)
        self.assertEqual(
            client.primary_client.base_url,
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.assertEqual(client.primary_client.api_key, "qwen-env-key")

    def test_registry_routes_openrouter_through_registered_adapter(self):
        registry = ModelProviderRegistry.create_default(
            env_config=LLMEnvironmentConfig(openrouter_api_key="openrouter-env-key")
        )
        profile = ModelProfileDefinition(
            name="OpenRouter",
            provider="openrouter",
            model="openai/gpt-4o-mini",
        )

        client = registry.resolve(profile)

        self.assertIsInstance(client, OpenRouterModelClient)
        self.assertEqual(client.base_url, DEFAULT_OPENROUTER_BASE_URL)
        self.assertEqual(client.api_key, "openrouter-env-key")

    @patch("app.llm.openrouter.OpenAI")
    def test_openrouter_client_includes_route_and_provider_preferences(self, openai_cls):
        openai_cls.return_value = MagicMock()
        profile = ModelProfileDefinition(
            name="OpenRouter",
            provider="openrouter",
            model="openai/gpt-4o-mini",
            parameters={
                "route_models": ["anthropic/claude-3.5-sonnet"],
                "provider_sort": "latency",
                "allow_fallbacks": False,
                "max_price": {"prompt": "0.50", "completion": "1.50"},
            },
        )
        client = OpenRouterModelClient(
            profile,
            LLMEnvironmentConfig(
                openrouter_api_key="openrouter-env-key",
                openrouter_site_url="https://agency.example",
                openrouter_app_name="Agency",
            ),
        )

        options = client._chat_options(
            temperature=None,
            max_tokens=None,
            stream=False,
        )

        self.assertEqual(
            options["models"],
            ["openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
        )
        self.assertEqual(
            options["provider"],
            {"sort": "latency", "allow_fallbacks": False},
        )
        self.assertEqual(options["max_price"], {"prompt": "0.50", "completion": "1.50"})
        self.assertEqual(
            options["extra_headers"],
            {
                "HTTP-Referer": "https://agency.example",
                "X-OpenRouter-Title": "Agency",
            },
        )

    @patch("app.llm.openai_compatible.OpenAI")
    def test_openai_compatible_generate_text_uses_profile_fields(self, openai_cls):
        message = MagicMock()
        message.content = "hello"
        message.tool_calls = None
        response = MagicMock()
        response.choices = [MagicMock(message=message)]
        response.usage = MagicMock()
        response.usage.model_dump.return_value = {"prompt_tokens": 10, "completion_tokens": 5}

        client_instance = MagicMock()
        client_instance.chat.completions.create.return_value = response
        openai_cls.return_value = client_instance

        profile = ModelProfileDefinition(
            name="OpenAI Compatible",
            provider="openai_compatible",
            model="gpt-test",
            base_url="http://localhost:11434/v1",
            temperature=0.2,
            max_tokens=123,
        )
        client = OpenAICompatibleModelClient(profile, LLMEnvironmentConfig(local_openai_api_key="abc"))

        result = client.generate_text([ModelMessage(role="user", content="hello")])

        self.assertEqual(result.content, "hello")
        self.assertEqual(result.provider, "openai_compatible")
        self.assertEqual(result.model, "gpt-test")
        client_instance.chat.completions.create.assert_called_once()

    @patch("app.llm.openai_compatible.OpenAI")
    def test_openai_compatible_omits_unset_optional_chat_options(self, openai_cls):
        message = MagicMock()
        message.content = "hello"
        response = MagicMock()
        response.choices = [MagicMock(message=message)]
        response.usage = None

        client_instance = MagicMock()
        client_instance.chat.completions.create.return_value = response
        openai_cls.return_value = client_instance

        profile = ModelProfileDefinition(
            name="OpenAI Compatible",
            provider="openai",
            model="gpt-test",
            base_url="https://api.openai.com/v1",
            temperature=None,
            max_tokens=None,
        )
        client = OpenAICompatibleModelClient(profile, LLMEnvironmentConfig(openai_api_key="abc"))

        client.generate_text([ModelMessage(role="user", content="hello")])

        call_kwargs = client_instance.chat.completions.create.call_args.kwargs
        self.assertNotIn("temperature", call_kwargs)
        self.assertNotIn("max_tokens", call_kwargs)

    @patch("app.llm.openai_compatible.OpenAI")
    def test_openai_compatible_stream_assembles_fragmented_tool_calls(self, openai_cls):
        usage = SimpleNamespace(
            model_dump=lambda: {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
        )
        stream = [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="Checking ",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id="call-weather",
                                    function=SimpleNamespace(name="get_", arguments='{"city":'),
                                )
                            ],
                        )
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(
                            content="now",
                            tool_calls=[
                                SimpleNamespace(
                                    index=0,
                                    id=None,
                                    function=SimpleNamespace(name="weather", arguments='"Paris"}'),
                                )
                            ],
                        )
                    )
                ],
                usage=None,
            ),
            SimpleNamespace(choices=[], usage=usage),
        ]
        client_instance = MagicMock()
        client_instance.chat.completions.create.return_value = iter(stream)
        openai_cls.return_value = client_instance
        profile = ModelProfileDefinition(
            name="OpenAI",
            provider="openai",
            model="gpt-test",
            base_url="https://api.openai.com/v1",
        )
        client = OpenAICompatibleModelClient(profile, LLMEnvironmentConfig(openai_api_key="abc"))

        events = list(client.stream_generate_text([ModelMessage(role="user", content="Weather?")]))

        self.assertEqual([event.text_delta for event in events[:-1]], ["Checking ", "now"])
        response = events[-1].response
        self.assertIsNotNone(response)
        assert response is not None
        self.assertEqual(response.content, "Checking now")
        self.assertEqual(response.usage["total_tokens"], 19)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].id, "call-weather")
        self.assertEqual(response.tool_calls[0].name, "get_weather")
        self.assertEqual(response.tool_calls[0].arguments, {"city": "Paris"})
        call_kwargs = client_instance.chat.completions.create.call_args.kwargs
        self.assertEqual(call_kwargs["stream_options"], {"include_usage": True})

    @patch("app.llm.ollama.httpx.post")
    def test_ollama_generate_text_uses_profile_timeout(self, post):
        response = MagicMock()
        response.json.return_value = {"message": {"content": "hello"}}
        post.return_value = response
        profile = ModelProfileDefinition(
            name="Ollama",
            provider="ollama",
            model="llama3",
            parameters={"request_timeout_seconds": 3},
        )
        client = OllamaModelClient(profile, LLMEnvironmentConfig(ollama_base_url="http://localhost:11434"))

        result = client.generate_text([ModelMessage(role="user", content="hello")])

        self.assertEqual(result.content, "hello")
        self.assertEqual(post.call_args.kwargs["timeout"], 3.0)
        self.assertIs(post.call_args.kwargs["trust_env"], False)

    @patch("app.llm.ollama.httpx.post")
    def test_ollama_generate_text_passes_thinking_flag(self, post):
        response = MagicMock()
        response.json.return_value = {"message": {"content": "hello"}}
        post.return_value = response
        profile = ModelProfileDefinition(
            name="Ollama",
            provider="ollama",
            model="qwen3.6:35b",
            parameters={"think": False},
        )
        client = OllamaModelClient(profile, LLMEnvironmentConfig(ollama_base_url="http://localhost:11434"))

        result = client.generate_text([ModelMessage(role="user", content="hello")])

        self.assertEqual(result.content, "hello")
        self.assertIs(post.call_args.kwargs["json"]["think"], False)
        self.assertIs(post.call_args.kwargs["trust_env"], False)


if __name__ == "__main__":
    unittest.main()
