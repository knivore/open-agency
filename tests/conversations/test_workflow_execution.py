from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.core.config import reset_settings_cache
from app.domain import ExecutionStatus, ModelProfileDefinition, WorkflowDefinition
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations.core import ConversationService
from app.services.main_agent_setup.service import MainAgentSetupConfig, MainAgentSetupService


class _FakeModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="direct reply", provider="fake", model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider="fake", model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "unused"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class ConversationWorkflowExecutionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        await MainAgentSetupService(self.context).create_main_agent(
            MainAgentSetupConfig(
                agent_name="Main Agent",
                agent_description="Configured for tests.",
                agent_instructions="Answer briefly.",
                model_profile_id="profile-fake",
                profile_id="main-agent-profile",
            )
        )
        self.service = ConversationService(self.context)
        await self.service.create_conversation(
            {
                "id": "conversation-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

        async def fake_queue_start(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            assert execution is not None
            execution.status = ExecutionStatus.COMPLETED
            execution.output_payload = {"final_output": {"ok": True}}
            await self.context.execution_store.update_execution(execution)
            return execution

        self.context.control_plane.queue_start = fake_queue_start

    async def asyncTearDown(self) -> None:
        reset_settings_cache()

    async def test_hidden_workflow_is_not_accessible(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-hidden",
                name="Hidden Workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": False},
            )
        )

        result = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-hidden",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run hidden",
                    "content": {"execution_request": {"workflow_id": "workflow-hidden"}},
                }
            },
        )

        self.assertEqual(result["assistant_message"]["plain_text"], "I cannot access workflow 'Hidden Workflow'.")

    async def test_direct_visible_workflow_execution_records_conversation_linkage(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-visible",
                name="Visible Workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": False},
            )
        )

        result = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-visible",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run visible",
                    "content": {
                        "execution_request": {"workflow_id": "workflow-visible", "input_payload": {"topic": "demo"}}},
                }
            },
        )

        execution = await self.context.execution_store.get_execution(result["execution"]["id"])
        assert execution is not None
        self.assertEqual(execution.metadata["conversation_id"], "conversation-1")
        self.assertEqual(execution.metadata["origin_message_id"], "message-visible")
        self.assertEqual(execution.metadata["requested_by_profile_id"], "main-agent-profile")
        messages = await self.service.list_messages("conversation-1")
        self.assertIn("execution_started", [item["message_type"] for item in messages["items"]])
        self.assertIn("execution_completed", [item["message_type"] for item in messages["items"]])

    async def test_delayed_execution_completion_is_appended_to_conversation(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-delayed",
                name="Delayed Workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": False},
            )
        )

        async def delayed_queue_start(execution_id: str):
            execution = await self.context.execution_store.get_execution(execution_id)
            assert execution is not None
            execution.status = ExecutionStatus.RUNNING
            await self.context.execution_store.update_execution(execution)

            async def complete_later() -> None:
                await asyncio.sleep(0.05)
                latest = await self.context.execution_store.get_execution(execution_id)
                assert latest is not None
                latest.status = ExecutionStatus.COMPLETED
                latest.output_payload = {"final_output": {"ok": True}}
                await self.context.execution_store.update_execution(latest)

            asyncio.create_task(complete_later())
            return execution

        self.context.control_plane.queue_start = delayed_queue_start

        await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-delayed",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run delayed",
                    "content": {"execution_request": {"workflow_id": "workflow-delayed"}},
                }
            },
        )

        await asyncio.sleep(0.2)
        messages = await self.service.list_messages("conversation-1")
        message_types = [item["message_type"] for item in messages["items"]]
        self.assertIn("execution_started", message_types)
        self.assertIn("execution_completed", message_types)

    async def test_protected_workflow_requires_approval_and_launches_once(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-protected",
                name="Protected Workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": True},
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-protected",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run protected",
                    "content": {"execution_request": {"workflow_id": "workflow-protected"}},
                }
            },
        )
        self.assertEqual(requested["approval_request"]["status"], "pending")

        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Proceed",
        )
        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertIn("execution", approved)

        executions = await self.context.execution_store.list_executions()
        linked = [item for item in executions if item.metadata.get("conversation_id") == "conversation-1"]
        self.assertEqual(len(linked), 1)

        messages = await self.service.list_messages("conversation-1")
        message_types = [item["message_type"] for item in messages["items"]]
        self.assertIn("approval_request", message_types)
        self.assertIn("approval_result", message_types)
        self.assertIn("execution_started", message_types)

    async def test_protected_workflow_requests_approval_for_each_launch_attempt(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-protected-repeat",
                name="Repeated Protected Workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": True},
            )
        )

        first = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-protected-repeat-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run protected once",
                    "content": {"execution_request": {"workflow_id": "workflow-protected-repeat"}},
                }
            },
        )
        await self.service.approve_request(first["approval_request"]["id"], actor_user_id="user-1", reason=None)
        second = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-protected-repeat-2",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run protected again",
                    "content": {"execution_request": {"workflow_id": "workflow-protected-repeat"}},
                }
            },
        )

        self.assertEqual(first["approval_request"]["status"], "pending")
        self.assertEqual(second["approval_request"]["status"], "pending")
        self.assertNotEqual(first["approval_request"]["id"], second["approval_request"]["id"])

    async def test_untrusted_external_identity_cannot_launch_workflow(self) -> None:
        await self.service.create_conversation(
            {
                "id": "conversation-external",
                "channel_type": "telegram",
                "channel_user_id": "tg-1",
                "created_by_user_id": None,
            }
        )
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-external",
                name="External Workflow",
                entrypoint="manual",
                metadata={"visible_to_main_agent": True, "protected_execution": False},
            )
        )

        result = await self.service.post_message(
            "conversation-external",
            {
                "message": {
                    "id": "message-external",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Run external",
                    "content": {"execution_request": {"workflow_id": "workflow-external"}},
                }
            },
        )

        self.assertEqual(
            result["assistant_message"]["plain_text"],
            "This channel is not allowed to launch workflows without a trusted mapped identity.",
        )

    async def test_external_channel_daily_budget_blocks_excess_messages(self) -> None:
        await self.service.create_conversation(
            {
                "id": "conversation-budget",
                "channel_type": "telegram",
                "channel_user_id": "tg-budget",
                "created_by_user_id": "user-1",
            }
        )

        with patch.dict("os.environ", {"MAIN_AGENT_EXTERNAL_CHANNEL_DAILY_MESSAGE_BUDGET": "1"}):
            reset_settings_cache()
            first = await self.service.post_message(
                "conversation-budget",
                {
                    "message": {
                        "id": "message-budget-1",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "First message",
                        "content": {"text": "First message"},
                    }
                },
            )
            second = await self.service.post_message(
                "conversation-budget",
                {
                    "message": {
                        "id": "message-budget-2",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Second message",
                        "content": {"text": "Second message"},
                    }
                },
            )

        self.assertEqual(first["assistant_message"]["plain_text"], "direct reply")
        self.assertEqual(
            second["assistant_message"]["plain_text"],
            "This external channel has reached its main-agent request budget for today.",
        )
