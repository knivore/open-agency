from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.cli import main
from app.core.config import reset_settings_cache
from app.domain import ChannelIdentityMapping, CredentialDefinition, ConversationChannelType, ModelProfileDefinition, UserDefinition
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService
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
                "",
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
        self.assertIn("chat access: direct CLI chat", buffer.getvalue())

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

    def test_chat_main_agent_command_streams_a_response(self) -> None:
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

        class _FakeConversationService:
            last_message: dict | None = None

            def __init__(self, context):
                self.context = context

            async def create_conversation(self, payload):
                return SimpleNamespace(id=payload["id"])

            async def post_message(self, conversation_id, payload):
                _FakeConversationService.last_message = {"conversation_id": conversation_id, "payload": payload}
                return {"assistant_message": {"plain_text": "Hello from the main agent"}}

        buffer = io.StringIO()
        with (
            patch("app.cli.get_default_api_context", return_value=self.context),
            patch("app.cli.ConversationService", _FakeConversationService),
            patch("builtins.input", side_effect=["Hello there", "quit"]),
            redirect_stdout(buffer),
        ):
            code = main(["chat-main-agent"])

        self.assertEqual(code, 0)
        self.assertIn("Chatting with main-agent profile:", buffer.getvalue())
        self.assertIn("assistant> Hello from the main agent", buffer.getvalue())
        self.assertIsNotNone(_FakeConversationService.last_message)
        self.assertEqual(_FakeConversationService.last_message["payload"]["message"]["plain_text"], "Hello there")

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

    def test_setup_chat_channel_command_prints_discord_guidance(self) -> None:
        buffer = io.StringIO()
        with patch("app.cli.get_default_api_context", return_value=self.context), redirect_stdout(buffer):
            code = main(["setup-chat-channel", "discord"])

        self.assertEqual(code, 0)
        self.assertIn("Discord bot setup checklist:", buffer.getvalue())
        self.assertIn("/integrations/conversations/adapters/discord/webhook", buffer.getvalue())

    def test_smoke_test_discord_command_reports_ready_installation(self) -> None:
        self._run(
            self.context.user_repo.save(
                UserDefinition(id="dev-user", email="dev@example.com", display_name="Dev User")
            )
        )
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-discord-ready",
                    owner_user_id="dev-user",
                    name="Discord",
                    provider="discord-bot",
                    secret_ref="onecli://users/dev-user/discord-bot/credential-discord-ready",
                    metadata={
                        "application_id": "app-1",
                        "bot_user_id": "bot-1",
                        "default_guild_id": "guild-1",
                        "webhook_public_key": "a" * 64,
                    },
                )
            )
        )
        self._run(
            self.context.channel_identity_mapping_repo.create(
                ChannelIdentityMapping(
                    id="discord-map-ready",
                    channel_type=ConversationChannelType.DISCORD,
                    channel_user_id="discord-user-1",
                    internal_user_id="dev-user",
                    channel_display_name="Dev Discord",
                    trusted=True,
                )
            )
        )

        class _FakeConnectorService:
            def __init__(self, context):
                self.context = context

            async def test_credential_for_owner(self, credential_id: str, owner_user_id: str) -> dict[str, object] | None:
                return {"ok": True, "provider": "discord-bot", "credential_id": credential_id}

        buffer = io.StringIO()
        with (
            patch("app.cli.get_default_api_context", return_value=self.context),
            patch("app.cli.ConnectorService", _FakeConnectorService),
            redirect_stdout(buffer),
        ):
            code = main(["smoke-test-discord", "--owner-user-id", "dev-user", "--discord-user-id", "discord-user-1"])

        self.assertEqual(code, 0)
        self.assertIn("Discord smoke test: PASS", buffer.getvalue())
        self.assertIn("webhook_public_key_present: yes", buffer.getvalue())
        self.assertIn("webhook_public_key_valid: yes", buffer.getvalue())
        self.assertIn("trusted_mapping_ok: yes", buffer.getvalue())

    def test_smoke_test_discord_command_reports_missing_webhook_key(self) -> None:
        self._run(
            self.context.user_repo.save(
                UserDefinition(id="dev-user", email="dev@example.com", display_name="Dev User")
            )
        )
        self._run(
            self.context.credential_repo.create(
                CredentialDefinition(
                    id="credential-discord-missing-key",
                    owner_user_id="dev-user",
                    name="Discord",
                    provider="discord-bot",
                    secret_ref="onecli://users/dev-user/discord-bot/credential-discord-missing-key",
                    metadata={
                        "application_id": "app-1",
                        "bot_user_id": "bot-1",
                        "default_guild_id": "guild-1",
                    },
                )
            )
        )

        class _FakeConnectorService:
            def __init__(self, context):
                self.context = context

            async def test_credential_for_owner(self, credential_id: str, owner_user_id: str) -> dict[str, object] | None:
                return {"ok": True, "provider": "discord-bot", "credential_id": credential_id}

        buffer = io.StringIO()
        with (
            patch("app.cli.get_default_api_context", return_value=self.context),
            patch("app.cli.ConnectorService", _FakeConnectorService),
            redirect_stdout(buffer),
        ):
            code = main(["smoke-test-discord", "--owner-user-id", "dev-user"])

        self.assertEqual(code, 1)
        self.assertIn("Discord smoke test: FAIL", buffer.getvalue())
        self.assertIn("webhook_public_key_present: no", buffer.getvalue())
        self.assertIn("add metadata.webhook_public_key", buffer.getvalue())

    def test_default_prompt_mentions_evaluation_agent_validation_loop(self) -> None:
        prompt = extract_prompt_from_doc()

        self.assertIn("## Evaluation And Improvement", prompt)
        self.assertIn("Evaluation agent", prompt)
        self.assertIn("read-only judge", prompt)
        self.assertIn("deterministic assertions", prompt)

    def test_default_prompt_requires_execution_evidence_for_run_failures(self) -> None:
        prompt = extract_prompt_from_doc()

        self.assertIn("agency.execution.get", prompt)
        self.assertIn("agency.execution.events", prompt)
        self.assertIn("earliest supporting failure event", prompt)
        self.assertIn("Do not substitute workflow or agent definitions", prompt)


if __name__ == "__main__":
    unittest.main()
