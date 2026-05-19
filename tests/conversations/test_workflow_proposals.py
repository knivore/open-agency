from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.domain import ModelProfileDefinition, WorkflowDefinition
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.conversations import ConversationService
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


class ConversationWorkflowProposalsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _FakeModelClient(profile, env))
        await self.context.model_profile_repo.save(
            ModelProfileDefinition(id="profile-fake", name="Fake", provider="fake", model="fake-model")
        )
        self.service = ConversationService(self.context)
        self.setup_service = MainAgentSetupService(self.context)
        await self.setup_service.create_main_agent(
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
        await self.service.create_conversation(
            {
                "id": "conversation-1",
                "created_by_user_id": "user-1",
                "channel_type": "api",
            }
        )

    def _workflow_payload(self, *, workflow_id: str, name: str, description: str = "Workflow description") -> dict:
        return {
            "id": workflow_id,
            "name": name,
            "description": description,
            "entrypoint": "node-1",
            "nodes": [
                {
                    "id": "node-1",
                    "name": "Entry",
                    "node_type": "task",
                    "task_id": "task-1",
                    "config": {},
                    "metadata": {},
                }
            ],
            "task_definitions": [
                {
                    "id": "task-1",
                    "name": "Task One",
                    "description": "Do the work",
                    "tool_ids": [],
                    "depends_on_task_ids": [],
                    "input_schema": {},
                    "output_schema": {},
                    "human_approval_required": False,
                    "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                    "metadata": {},
                }
            ],
            "versioning": {
                "version": "1.0.0",
                "revision": 1,
                "parent_version": None,
                "is_published": False,
                "labels": [],
            },
            "metadata": {
                "visible_to_main_agent": True,
                "mutable_by_main_agent": True,
            },
        }

    async def test_approved_create_persists_workflow_with_provenance(self) -> None:
        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-create",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create a workflow",
                    "content": {
                        "workflow_proposal": {
                            "summary": "Create workflow 'Workflow Create'.",
                            "workflow": self._workflow_payload(workflow_id="workflow-create", name="Workflow Create"),
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_proposal")
        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Looks good",
        )
        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertEqual(approved["workflow"]["id"], "workflow-create")

        workflow = await self.context.workflow_repo.get("workflow-create")
        assert workflow is not None
        self.assertEqual(workflow.name, "Workflow Create")
        self.assertEqual(workflow.metadata["created_by"], "user-1")
        self.assertEqual(workflow.metadata["owner_ids"], ["user-1"])
        self.assertEqual(workflow.metadata["provenance"]["approval_request_id"], requested["approval_request"]["id"])
        self.assertEqual(workflow.metadata["provenance"]["decision"], "approved")

    async def test_plain_text_workflow_create_request_creates_proposal(self) -> None:
        workflow = WorkflowDefinition.model_validate(
            self._workflow_payload(workflow_id="workflow-plain-text", name="Plain Text Workflow")
        )
        with patch(
            "app.services.conversations.core.WorkflowBuilderService.build_workflow_definition",
            new=AsyncMock(return_value=workflow),
        ) as build_workflow:
            requested = await self.service.post_message(
                "conversation-1",
                {
                    "message": {
                        "id": "message-plain-text-create",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Can you build me a workflow that reviews the repo every day?",
                        "content": {"text": "Can you build me a workflow that reviews the repo every day?"},
                    }
                },
            )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_proposal")
        self.assertEqual(requested["approval_request"]["status"], "pending")
        build_workflow.assert_awaited_once()
        self.assertIn("build me a workflow", build_workflow.await_args.kwargs["goal"])

    async def test_plain_text_workflow_create_falls_back_and_schedules_on_approval(self) -> None:
        with patch.dict("os.environ", {"TZ": "Asia/Singapore"}), patch(
            "app.services.conversations.core.WorkflowBuilderService.build_workflow_definition",
            new=AsyncMock(side_effect=RuntimeError("missing_scope: model.request")),
        ):
            requested = await self.service.post_message(
                "conversation-1",
                {
                    "message": {
                        "id": "message-plain-text-create-fallback",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": (
                            "Can you build me a workflow where it comes up with 1 new ideas everyday at 7am "
                            "on how to improve this agency repo and identify vulnerabilities or fixes?"
                        ),
                        "content": {
                            "text": (
                                "Can you build me a workflow where it comes up with 1 new ideas everyday at 7am "
                                "on how to improve this agency repo and identify vulnerabilities or fixes?"
                            )
                        },
                    }
                },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(workflow["metadata"]["generated_by"], "conversation_plain_text_fallback")
        self.assertEqual(workflow["metadata"]["requested_schedule"]["trigger_config"]["cron"], "0 7 * * *")
        self.assertEqual(workflow["metadata"]["requested_schedule"]["timezone"], "Asia/Singapore")
        self.assertEqual(len(workflow["agent_definitions"]), 2)
        self.assertEqual(len(workflow["task_definitions"]), 4)
        self.assertEqual(workflow["tool_definitions"][0]["id"], "agency.command.run")
        implementation_task = workflow["task_definitions"][3]
        self.assertEqual(implementation_task["name"], "Implement TODOs from daily brief")
        self.assertEqual(implementation_task["depends_on_task_ids"], [workflow["task_definitions"][2]["id"]])
        self.assertEqual(implementation_task["tool_ids"], ["agency.command.run"])

        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Run it daily",
        )
        self.assertEqual(approved["approval_request"]["status"], "approved")
        schedules = await self.context.schedule_repo.list()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0].workflow_id, workflow["id"])
        self.assertEqual(schedules[0].trigger_type.value, "cron")
        self.assertEqual(schedules[0].trigger_config["cron"], "0 7 * * *")
        self.assertEqual(schedules[0].timezone, "Asia/Singapore")

    async def test_rejected_create_saves_draft_workflow(self) -> None:
        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-create-reject",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Draft a workflow",
                    "content": {
                        "workflow_proposal": {
                            "summary": "Create workflow 'Workflow Draft'.",
                            "workflow": self._workflow_payload(workflow_id="workflow-draft", name="Workflow Draft"),
                        }
                    },
                }
            },
        )

        rejected = await self.service.reject_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Keep as draft",
        )
        self.assertEqual(rejected["approval_request"]["status"], "rejected")
        self.assertEqual(rejected["workflow"]["id"], "workflow-draft")

        workflow = await self.context.workflow_repo.get("workflow-draft")
        assert workflow is not None
        self.assertFalse(workflow.versioning.is_published)
        self.assertIn("draft", workflow.versioning.labels)
        self.assertEqual(workflow.metadata["created_by"], "user-1")
        self.assertEqual(workflow.metadata["owner_ids"], ["user-1"])
        self.assertEqual(workflow.metadata["provenance"]["decision"], "rejected_saved_as_draft")

    async def test_approved_update_creates_new_active_revision(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-update",
                name="Workflow Update",
                description="Original description",
                entrypoint="node-1",
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={"version": "1.0.0", "revision": 1, "parent_version": None, "is_published": True,
                            "labels": []},
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update the workflow",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-update",
                            "summary": "Update workflow 'Workflow Update'.",
                            "workflow": self._workflow_payload(
                                workflow_id="workflow-update",
                                name="Workflow Update",
                                description="Updated description",
                            ),
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Ship it",
        )
        self.assertEqual(approved["workflow"]["versioning"]["revision"], 2)
        self.assertTrue(approved["workflow"]["versioning"]["is_published"])

        workflow = await self.context.workflow_repo.get("workflow-update")
        assert workflow is not None
        self.assertEqual(workflow.description, "Updated description")
        self.assertEqual(workflow.versioning.revision, 2)
        self.assertEqual(workflow.versioning.parent_version, "1.0.0")
        self.assertEqual(workflow.metadata["created_by"], "user-1")
        self.assertEqual(workflow.metadata["owner_ids"], ["user-1"])
        self.assertEqual(workflow.metadata["provenance"]["decision"], "approved")

    async def test_rejected_update_does_not_mutate_active_workflow(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-no-update",
                name="Workflow No Update",
                description="Original description",
                entrypoint="node-1",
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={"version": "1.0.0", "revision": 1, "parent_version": None, "is_published": True,
                            "labels": []},
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-update-reject",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Reject the update",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-no-update",
                            "workflow": self._workflow_payload(
                                workflow_id="workflow-no-update",
                                name="Workflow No Update",
                                description="Should not be applied",
                            ),
                        }
                    },
                }
            },
        )

        rejected = await self.service.reject_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Not now",
        )
        self.assertEqual(rejected["approval_request"]["status"], "rejected")
        self.assertNotIn("workflow", rejected)

        workflow = await self.context.workflow_repo.get("workflow-no-update")
        assert workflow is not None
        self.assertEqual(workflow.description, "Original description")
        self.assertEqual(workflow.versioning.revision, 1)

    async def test_untrusted_external_identity_cannot_create_workflow(self) -> None:
        await self.service.create_conversation(
            {
                "id": "conversation-external",
                "channel_type": "telegram",
                "channel_user_id": "tg-1",
                "created_by_user_id": None,
            }
        )

        result = await self.service.post_message(
            "conversation-external",
            {
                "message": {
                    "id": "message-external-create",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Create externally",
                    "content": {
                        "workflow_proposal": {
                            "workflow": self._workflow_payload(workflow_id="workflow-external",
                                                               name="External Workflow")
                        }
                    },
                }
            },
        )

        self.assertEqual(
            result["assistant_message"]["plain_text"],
            "This channel is not allowed to create or update workflows without a trusted mapped identity.",
        )

    async def test_immutable_workflow_cannot_be_updated(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-immutable",
                name="Immutable Workflow",
                description="Original description",
                entrypoint="node-1",
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": False},
                versioning={"version": "1.0.0", "revision": 1, "parent_version": None, "is_published": True,
                            "labels": []},
            )
        )

        result = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-immutable",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update immutable workflow",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-immutable",
                            "workflow": self._workflow_payload(
                                workflow_id="workflow-immutable",
                                name="Immutable Workflow",
                                description="Updated description",
                            ),
                        }
                    },
                }
            },
        )

        self.assertEqual(
            result["assistant_message"]["plain_text"],
            "I cannot update workflow 'Immutable Workflow' because it is not marked mutable by this agent.",
        )
