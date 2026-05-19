from __future__ import annotations

import asyncio
import unittest
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.domain import ModelProfileDefinition
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService


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


class ConversationApprovalsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        self._run(self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")))
        self._run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Configured for tests.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-fake",
                    profile_id="main-agent-profile",
                )
            )
        )
        self.client = TestClient(create_app(context=self.context))
        created = self.client.post(
            "/conversations",
            json={
                "id": "conversation-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            },
        )
        assert created.status_code == 200

    def _run(self, awaitable):
        return asyncio.run(awaitable)

    def test_approval_request_lifecycle(self) -> None:
        post = self.client.post(
            "/conversations/conversation-1/messages",
            json={
                "message": {
                    "id": "message-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Please request approval",
                    "content": {
                        "text": "Please request approval",
                        "approval_request": {
                            "approval_type": "workflow_execution",
                            "target_type": "workflow",
                            "target_id": "workflow-1",
                            "summary": "Run protected workflow workflow-1",
                        },
                    },
                },
                "response_mode": "sync",
            },
        )
        self.assertEqual(post.status_code, 200)
        payload = post.json()
        self.assertEqual(payload["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(payload["approval_request"]["status"], "pending")
        approval_id = payload["approval_request"]["id"]

        listed = self.client.get("/conversations/conversation-1/approval-requests")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), 1)

        forbidden = self.client.post(
            f"/conversations/approval-requests/{approval_id}/approve",
            json={"user_id": "user-2", "reason": "Not allowed"},
        )
        self.assertEqual(forbidden.status_code, 403)

        approved = self.client.post(
            f"/conversations/approval-requests/{approval_id}/approve",
            json={"user_id": "user-1", "reason": "Approved"},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json()["approval_request"]["status"], "approved")
        self.assertEqual(approved.json()["message"]["message_type"], "approval_result")

        conflict = self.client.post(
            f"/conversations/approval-requests/{approval_id}/reject",
            json={"user_id": "user-1", "reason": "Too late"},
        )
        self.assertEqual(conflict.status_code, 409)

        messages = self.client.get("/conversations/conversation-1/messages")
        self.assertEqual(messages.status_code, 200)
        self.assertEqual(
            [item["message_type"] for item in messages.json()["items"]],
            ["user_text", "approval_request", "approval_result"],
        )

    def test_request_changes_cancels_pending_approval_without_applying_it(self) -> None:
        post = self.client.post(
            "/conversations/conversation-1/messages",
            json={
                "message": {
                    "id": "message-request-changes-1",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Please request approval",
                    "content": {
                        "text": "Please request approval",
                        "approval_request": {
                            "approval_type": "workflow_update",
                            "target_type": "workflow",
                            "target_id": "workflow-1",
                            "summary": "Update workflow workflow-1",
                        },
                    },
                },
                "response_mode": "sync",
            },
        )
        self.assertEqual(post.status_code, 200)
        approval_id = post.json()["approval_request"]["id"]

        requested = self.client.post(
            f"/conversations/approval-requests/{approval_id}/request-changes",
            json={"user_id": "user-1", "reason": "Narrow this to validation only"},
        )
        self.assertEqual(requested.status_code, 200)
        approval = requested.json()["approval_request"]
        self.assertEqual(approval["status"], "cancelled")
        self.assertEqual(approval["metadata"]["revision_requested"], True)
        self.assertEqual(
            approval["metadata"]["last_revision_request"]["reason"],
            "Narrow this to validation only",
        )
        self.assertEqual(requested.json()["message"]["message_type"], "system_note")

        conflict = self.client.post(
            f"/conversations/approval-requests/{approval_id}/approve",
            json={"user_id": "user-1", "reason": "Approve old proposal"},
        )
        self.assertEqual(conflict.status_code, 409)

        messages = self.client.get("/conversations/conversation-1/messages")
        self.assertEqual(messages.status_code, 200)
        self.assertEqual(
            [item["message_type"] for item in messages.json()["items"]],
            ["user_text", "approval_request", "system_note"],
        )
