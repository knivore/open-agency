from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.domain import ModelProfileDefinition
from app.llm.base import ModelMessage, ModelResponse
from app.llm.ollama import OllamaModelClient
from app.llm.openai_compatible import OpenAICompatibleModelClient
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


class ModelProviderRegistryTests(unittest.TestCase):
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

        self.assertIsInstance(client, OpenAICompatibleModelClient)
        self.assertEqual(client.base_url, "http://localhost:1234/v1")

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
