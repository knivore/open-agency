from __future__ import annotations

import asyncio
import unittest

from app.api.context import create_test_api_context
from app.domain import ModelProfileDefinition
from app.services.agent_tools import SYSTEM_COMMAND_RUN_TOOL_ID, SYSTEM_GRAPH_CONTEXT_TOOL_ID
from scripts.setup import setup_coder_agent


class CoderAgentSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()

    def _run(self, awaitable):
        return asyncio.run(awaitable)

    def test_setup_coder_agent_prefers_openai_codex_profile(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(
                    id="profile-name-only-codex",
                    name="Codex-ish Custom",
                    provider="fake",
                    model="fake-model",
                )
            )
        )
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(
                    id="profile-openai-codex",
                    name="OpenAI Codex",
                    provider="openai-codex",
                    model="gpt-5.3-codex",
                )
            )
        )

        agent = self._run(
            setup_coder_agent(
                name="Coder",
                role="Senior Software Engineer",
                context=self.context,
            )
        )

        self.assertEqual(agent.model_profile_id, "profile-openai-codex")
        self.assertIn(SYSTEM_COMMAND_RUN_TOOL_ID, agent.tool_ids)
        self.assertIn(SYSTEM_GRAPH_CONTEXT_TOOL_ID, agent.tool_ids)
        self.assertTrue(agent.graph_context.enabled)
        self.assertTrue(agent.graph_context.auto_retrieval_enabled)
        self.assertTrue(agent.graph_context.coding_agent_resume_enabled)
        self.assertEqual(agent.graph_context.default_intent, "resume")
        self.assertEqual(agent.graph_context.default_budget, "balanced")
        self.assertFalse(agent.graph_context.include_raw_graph)


if __name__ == "__main__":
    unittest.main()
