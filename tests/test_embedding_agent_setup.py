from __future__ import annotations

import asyncio
import unittest

from app.api.context import create_test_api_context
from scripts.setup import (
    DEFAULT_MODEL,
    DEFAULT_MODEL_PROFILE_ID,
    DEFAULT_PROVIDER_ID,
    setup_embedding_agent,
)


class EmbeddingAgentSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()

    def _run(self, awaitable):
        return asyncio.run(awaitable)

    def test_setup_embedding_agent_creates_provider_profile_and_agent(self) -> None:
        result = self._run(setup_embedding_agent(context=self.context, base_url="http://localhost:11434"))

        self.assertEqual(result.provider.id, DEFAULT_PROVIDER_ID)
        self.assertEqual(result.provider.provider_type.value, "ollama")
        self.assertEqual(result.model_profile.id, DEFAULT_MODEL_PROFILE_ID)
        self.assertEqual(result.model_profile.model, DEFAULT_MODEL)
        self.assertEqual(result.model_profile.provider, DEFAULT_PROVIDER_ID)
        self.assertFalse(result.model_profile.supports_tools)
        self.assertFalse(result.model_profile.supports_streaming)
        self.assertEqual(result.model_profile.parameters["purpose"], "memory_embedding")
        self.assertEqual(result.agent.id, "embedding")
        self.assertEqual(result.agent.model_profile_id, DEFAULT_MODEL_PROFILE_ID)
        self.assertEqual(result.agent.framework_hints.metadata["agent_kind"], "embedding")

    def test_setup_embedding_agent_is_idempotent(self) -> None:
        first = self._run(setup_embedding_agent(context=self.context, base_url="http://localhost:11434"))
        second = self._run(setup_embedding_agent(context=self.context, base_url="http://localhost:11434"))

        providers = self._run(self.context.model_provider_repo.list(include_deleted=True))
        profiles = self._run(self.context.model_profile_repo.list(include_deleted=True))
        agents = self._run(self.context.agent_repo.list(include_deleted=True))

        self.assertEqual(first.agent.id, second.agent.id)
        self.assertEqual(first.model_profile.id, second.model_profile.id)
        self.assertEqual([provider.id for provider in providers], [DEFAULT_PROVIDER_ID])
        self.assertEqual([profile.id for profile in profiles], [DEFAULT_MODEL_PROFILE_ID])
        self.assertEqual([agent.id for agent in agents], ["embedding"])


if __name__ == "__main__":
    unittest.main()
