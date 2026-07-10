from __future__ import annotations

import argparse
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from scripts import setup as setup_script


class SetupScriptTests(unittest.TestCase):
    def test_run_from_args_dispatches_local_onboarding(self) -> None:
        with patch.object(setup_script, "setup_local_onboarding", new=AsyncMock(return_value=0)) as mocked:
            exit_code = asyncio.run(
                setup_script.run_from_args(argparse.Namespace(command="local-onboarding"))
            )

        self.assertEqual(exit_code, 0)
        mocked.assert_awaited_once()

    def test_run_from_args_dispatches_recommended_agents(self) -> None:
        mocked_result = SimpleNamespace(
            coder_agent=SimpleNamespace(id="coder"),
            embedding=SimpleNamespace(agent=SimpleNamespace(id="embedding")),
            evaluation=SimpleNamespace(agent=SimpleNamespace(id="evaluation")),
        )
        with patch.object(setup_script, "sync_recommended_agents", new=AsyncMock(return_value=mocked_result)) as mocked:
            exit_code = asyncio.run(
                setup_script.run_from_args(argparse.Namespace(command="recommended-agents"))
            )

        self.assertEqual(exit_code, 0)
        mocked.assert_awaited_once()

    def test_local_onboarding_can_bootstrap_admin_model_and_main_agent(self) -> None:
        context = create_test_api_context()

        with (
            patch("scripts.setup.input", side_effect=[
                "Local Admin",
                "admin@example.com",
                "",
                "",
                "",
                "",
                "",
            ]),
            patch("scripts.setup.getpass", side_effect=[
                "change-me-123",
                "change-me-123",
                "sk-test-setup",
            ]),
        ):
            exit_code = asyncio.run(setup_script.setup_local_onboarding(context=context))

        self.assertEqual(exit_code, 0)

        async def _assertions() -> None:
            users = await context.user_repo.list()
            self.assertEqual(len(users), 1)
            self.assertIn("admin", users[0].roles)

            profiles = await context.model_profile_repo.list()
            self.assertGreaterEqual(len(profiles), 1)
            self.assertEqual(profiles[0].id, "setup-profile-openai")

            main_agent = await context.main_agent_profile_repo.get("main-agent-profile")
            self.assertIsNotNone(main_agent)
            self.assertEqual(main_agent.default_model_profile_id, "setup-profile-openai")

            coder = await context.agent_repo.get("coder")
            embedding = await context.agent_repo.get("embedding")
            evaluation = await context.agent_repo.get("evaluation")
            self.assertIsNotNone(coder)
            self.assertIsNotNone(embedding)
            self.assertIsNotNone(evaluation)

        asyncio.run(_assertions())


if __name__ == "__main__":
    unittest.main()
