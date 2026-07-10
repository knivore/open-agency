from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.core.config import get_settings, reset_settings_cache
from app.domain import ModelProfileDefinition
from app.services.main_agent_setup.service import (
    MainAgentModelProfileRequiredError,
    MainAgentSetupConfig,
    MainAgentSetupInvalidError,
    MainAgentSetupRequiredError,
    MainAgentSetupService,
)


class MainAgentSetupServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        reset_settings_cache()
        self.context = create_test_api_context()
        self.service = MainAgentSetupService(self.context)

    def tearDown(self) -> None:
        reset_settings_cache()

    async def test_missing_model_profiles_is_detected(self) -> None:
        self.assertFalse(await self.service.has_usable_model_profiles())
        with self.assertRaises(MainAgentModelProfileRequiredError):
            await self.service.require_usable_model_profiles()

    async def test_missing_main_agent_setup_is_detected(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        self.assertFalse(await self.service.is_main_agent_setup_complete())
        with self.assertRaises(MainAgentSetupRequiredError):
            await self.service.require_active_main_agent_profile()

    async def test_create_main_agent_makes_setup_complete(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        created = await self.service.create_main_agent(
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

        self.assertEqual(created.id, "main-agent-profile")
        self.assertTrue(await self.service.has_usable_model_profiles())
        self.assertTrue(await self.service.is_main_agent_setup_complete())
        resolved = await self.service.require_active_main_agent_profile()
        self.assertEqual(resolved.id, "main-agent-profile")
        self.assertEqual(
            resolved.metadata["main_agent_monitoring"]["approval_conversation_id"],
            "main-agent-profile-monitor",
        )
        self.assertTrue(resolved.metadata["main_agent_monitoring"]["route_improvement_proposals_to_approval"])
        monitor_conversation = await self.context.conversation_repo.get("main-agent-profile-monitor")
        self.assertIsNotNone(monitor_conversation)
        assert monitor_conversation is not None
        self.assertEqual(monitor_conversation.title, "Main Agent Monitor")
        agent = await self.context.agent_repo.get("main-agent")
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertIn("agency.speech.listen", agent.tool_ids)
        self.assertIn("agency.speech.speak", agent.tool_ids)
        self.assertIn("agency.speech.continue", agent.tool_ids)

    async def test_ensure_startup_ready_non_interactive_requires_main_agent(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        with self.assertRaises(MainAgentSetupRequiredError):
            await self.service.ensure_startup_ready(interactive=False)

    async def test_ensure_startup_ready_interactive_creates_main_agent(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
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
        with patch("builtins.input", side_effect=lambda prompt="": next(answers)):
            created = await self.service.ensure_startup_ready(interactive=True)

        self.assertEqual(created.name, "Agency Assistant")
        self.assertEqual(created.default_model_profile_id, "profile-fake")
        self.assertEqual(created.metadata["operator_chat_access"]["mode"], "direct_cli")
        self.assertTrue(await self.service.is_main_agent_setup_complete())

    async def test_ensure_startup_ready_interactive_can_onboard_provider_and_profile(self) -> None:
        answers = iter(
            [
                "8",
                "",
                "",
                "llama3:8b",
                "",
                "",
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
        with patch("builtins.input", side_effect=lambda prompt="": next(answers)):
            created = await self.service.ensure_startup_ready(interactive=True)

        profiles = await self.service.list_usable_model_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].provider, "ollama")
        self.assertEqual(profiles[0].model, "llama3:8b")
        self.assertEqual(created.default_model_profile_id, profiles[0].id)
        self.assertTrue(await self.service.is_main_agent_setup_complete())

    async def test_ollama_default_uses_localhost_when_backend_runs_on_host(self) -> None:
        with patch.dict(os.environ, {"AGENCY_BACKEND_RUN_MODE": "host"}, clear=False):
            _, _, base_url, _ = self.service._provider_defaults("ollama")

        self.assertEqual(base_url, "http://localhost:11434")

    def test_chat_channel_setup_guidance_lists_provider_specific_steps(self) -> None:
        discord = self.service.chat_channel_setup_guidance("discord")
        telegram = self.service.chat_channel_setup_guidance("telegram")

        self.assertIn("Discord bot setup checklist:", discord)
        self.assertIn("/integrations/conversations/adapters/discord/webhook", discord)
        self.assertIn("Telegram bot setup checklist:", telegram)
        self.assertIn("/integrations/conversations/adapters/telegram/webhook", telegram)
        self.assertIn("trusted approvals and mutations", discord)

    async def test_ensure_startup_ready_non_interactive_can_bootstrap_from_env(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "MAIN_AGENT_BOOTSTRAP_ENABLED": "true",
                    "MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY": "ollama",
                    "MAIN_AGENT_BOOTSTRAP_MODEL_NAME": "llama3:8b",
                    "MAIN_AGENT_BOOTSTRAP_PROFILE_NAME": "Ollama Main",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_NAME": "Agency Assistant",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_DESCRIPTION": "Main agent for this workspace",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_INSTRUCTIONS": "Be helpful.",
                    "MAIN_AGENT_BOOTSTRAP_WORKFLOW_NAME": "Main Workflow",
                    "MAIN_AGENT_BOOTSTRAP_WORKFLOW_DESCRIPTION": "Workflow for the main agent",
                },
                clear=False,
        ):
            reset_settings_cache()
            created = await self.service.ensure_startup_ready(interactive=False, settings=get_settings())

        profiles = await self.service.list_usable_model_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].provider, "ollama")
        self.assertEqual(profiles[0].model, "llama3:8b")
        self.assertEqual(created.name, "Agency Assistant")
        self.assertEqual(created.default_model_profile_id, profiles[0].id)
        self.assertEqual(created.metadata["setup_mode"], "env")

    async def test_ensure_startup_ready_can_bootstrap_codex_cli_profile_without_api_key(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "MAIN_AGENT_BOOTSTRAP_ENABLED": "true",
                    "MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY": "openai_codex",
                    "MAIN_AGENT_BOOTSTRAP_MODEL_NAME": "gpt-5.3-codex",
                    "MAIN_AGENT_BOOTSTRAP_PROFILE_NAME": "Codex Main",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_NAME": "Agency Assistant",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_DESCRIPTION": "Main agent for this workspace",
                    "MAIN_AGENT_BOOTSTRAP_AGENT_INSTRUCTIONS": "Be helpful.",
                },
                clear=False,
        ):
            reset_settings_cache()
            created = await self.service.ensure_startup_ready(interactive=False, settings=get_settings())

        profiles = await self.service.list_usable_model_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].provider, "openai_codex")
        self.assertEqual(profiles[0].model, "gpt-5.3-codex")
        self.assertEqual(profiles[0].parameters["auth_mode"], "chatgpt")
        self.assertEqual(profiles[0].parameters["codex_cli_timeout_seconds"], 1800)
        self.assertEqual(created.default_model_profile_id, profiles[0].id)

    async def test_ensure_startup_ready_non_interactive_bootstrap_requires_model_name(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "MAIN_AGENT_BOOTSTRAP_ENABLED": "true",
                    "MAIN_AGENT_BOOTSTRAP_PROVIDER_FAMILY": "ollama",
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaisesRegex(
                    MainAgentSetupInvalidError,
                    "MAIN_AGENT_BOOTSTRAP_MODEL_NAME is required",
            ):
                await self.service.ensure_startup_ready(interactive=False, settings=get_settings())

    async def test_update_active_main_agent_profile_switches_model_everywhere(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-a", name="Profile A", provider="fake", model="model-a")
        )
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-b", name="Profile B", provider="fake", model="model-b")
        )
        created = await self.service.create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-a",
                profile_id="main-agent-profile",
                agent_id="main-agent",
                workflow_id="main-workflow",
            )
        )

        updated = await self.service.update_active_main_agent_profile(default_model_profile_id="profile-b")

        self.assertEqual(updated.id, created.id)
        self.assertEqual(updated.default_model_profile_id, "profile-b")

        agent = await self.context.agent_repo.get(created.agent_id)
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent.model_profile_id, "profile-b")

        workflow = await self.context.workflow_repo.get(created.default_workflow_id)
        self.assertIsNotNone(workflow)
        assert workflow is not None
        self.assertEqual(workflow.agent_definitions[0].id, created.agent_id)
        self.assertEqual(workflow.agent_definitions[0].model_profile_id, "profile-b")

    async def test_sync_active_main_agent_instructions_updates_agent_and_workflow_copy(self) -> None:
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        created = await self.service.create_main_agent(
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

        updated_agent = await self.service.sync_active_main_agent_instructions("New evaluation-aware instructions.")

        self.assertEqual(updated_agent.instructions, "New evaluation-aware instructions.")
        saved_agent = await self.context.agent_repo.get(created.agent_id)
        self.assertIsNotNone(saved_agent)
        assert saved_agent is not None
        self.assertEqual(saved_agent.instructions, "New evaluation-aware instructions.")
        workflow = await self.context.workflow_repo.get(created.default_workflow_id)
        self.assertIsNotNone(workflow)
        assert workflow is not None
        self.assertEqual(workflow.agent_definitions[0].instructions, "New evaluation-aware instructions.")


if __name__ == "__main__":
    unittest.main()
