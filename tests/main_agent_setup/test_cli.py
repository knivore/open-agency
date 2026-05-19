from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.cli import main
from app.core.config import reset_settings_cache
from app.domain import ModelProfileDefinition
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService
from app.services.main_agent_setup.prompt_doc import extract_prompt_from_doc


class MainAgentCliTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()

    def tearDown(self) -> None:
        reset_settings_cache()

    def _run(self, awaitable):
        import asyncio

        return asyncio.run(awaitable)

    def test_check_main_agent_returns_success_when_setup_complete(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
            )
        )
        self._run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                    agent_id="main-agent",
                    workflow_id="main-workflow",
                )
            )
        )
        buffer = io.StringIO()
        with patch("app.cli.get_default_api_context", return_value=self.context), redirect_stdout(buffer):
            code = main(["check-main-agent"])

        self.assertEqual(code, 0)
        self.assertIn("Main-agent setup is complete.", buffer.getvalue())

    def test_setup_main_agent_command_can_run_interactively(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
            )
        )
        answers = iter(
            [
                "Agency Assistant",
                "Main agent for this workspace",
                "Be helpful.",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
                "Main Workflow",
                "Workflow for the main agent",
                "y",
            ]
        )
        buffer = io.StringIO()
        with (
            patch("app.cli.get_default_api_context", return_value=self.context),
            patch("builtins.input", side_effect=lambda prompt="": next(answers)),
            redirect_stdout(buffer),
        ):
            code = main(["setup-main-agent"])

        self.assertEqual(code, 0)
        self.assertIn("Active main-agent profile:", buffer.getvalue())

    def test_setup_main_agent_command_can_run_non_interactively_from_env(self) -> None:
        buffer = io.StringIO()
        with (
            patch("app.cli.get_default_api_context", return_value=self.context),
            patch.dict(
                os.environ,
                {
                    "MAIN_AGENT_BOOTSTRAP_ENABLED": "true",
                    "MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY": "ollama",
                    "MAIN_AGENT_BOOTSTRAP_MODEL_NAME": "llama3:8b",
                    "MAIN_AGENT_BOOTSTRAP_PROFILE_NAME": "Ollama Main",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_NAME": "Agency Assistant",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_DESCRIPTION": "Main agent for this workspace",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_INSTRUCTIONS": "Be helpful.",
                },
                clear=False,
            ),
            redirect_stdout(buffer),
        ):
            reset_settings_cache()
            code = main(["setup-main-agent", "--non-interactive"])

        self.assertEqual(code, 0)
        self.assertIn("Active main-agent profile:", buffer.getvalue())

    def test_sync_main_agent_prompt_command_updates_existing_agent(self) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
            )
        )
        self._run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Old instructions.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                    agent_id="main-agent",
                    workflow_id="main-workflow",
                )
            )
        )
        buffer = io.StringIO()
        with patch("app.cli.get_default_api_context", return_value=self.context), redirect_stdout(buffer):
            code = main(["sync-main-agent-prompt"])

        self.assertEqual(code, 0)
        self.assertIn("Synced main-agent instructions", buffer.getvalue())
        agent = self._run(self.context.agent_repo.get("main-agent"))
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertIn("## Evaluation And Improvement", agent.instructions)

    def test_default_prompt_mentions_evaluation_agent_validation_loop(self) -> None:
        prompt = extract_prompt_from_doc()

        self.assertIn("## Evaluation And Improvement", prompt)
        self.assertIn("Evaluation agent", prompt)
        self.assertIn("read-only judge", prompt)
        self.assertIn("deterministic assertions", prompt)


if __name__ == "__main__":
    unittest.main()
