from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.domain import (
    AgentDefinition,
    ConversationMessage,
    ConversationMessageType,
    ConversationRole,
    CredentialDefinition,
    ModelProfileDefinition,
    SecuritySettings,
    ToolDefinition,
    ToolImplementationReference,
    ToolType,
    VersionDefinition,
    WorkflowDefinition,
)
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services.agent_tools import (
    SYSTEM_CONNECTOR_CAPABILITIES_TOOL_ID,
    SYSTEM_CONNECTOR_CREDENTIALS_TOOL_ID,
    SYSTEM_CONNECTOR_RESOLVE_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_EXECUTION_PAUSE_TOOL_ID,
)
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


class _FakeControlPlaneExecution:
    def __init__(self, execution_id: str, status: str):
        self.execution_id = execution_id
        self.status = status

    def model_dump(self, *, mode: str = "python"):
        return {"id": self.execution_id, "status": self.status}


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

    def _workflow_payload_with_command_tool(self, *, workflow_id: str, name: str) -> dict:
        workflow = self._workflow_payload(workflow_id=workflow_id, name=name)
        workflow["task_definitions"][0]["tool_ids"] = ["agency.command.run"]
        workflow["tool_definitions"] = [
            {
                "id": "agency.command.run",
                "name": "run_command",
                "description": "Run shell commands against the repository.",
                "tool_type": "shell_command",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
                "output_schema": {"type": "object", "properties": {}},
                "implementation": {
                    "implementation_type": "shell_command",
                    "target": "agency.system.command",
                    "callable_name": "run_command",
                },
                "security": {
                    "requires_approval": True,
                    "sandbox_required": True,
                    "allow_shell": True,
                    "allow_filesystem": True,
                    "read_only": False,
                    "dangerous": True,
                },
                "tags": ["command", "repo_mutation"],
            }
        ]
        return workflow

    def _workflow_payload_with_unguarded_network_tool(self, *, workflow_id: str, name: str) -> dict:
        workflow = self._workflow_payload(workflow_id=workflow_id, name=name)
        workflow["description"] = "Workflow that sends a network callback."
        workflow["task_definitions"][0]["tool_ids"] = ["tool-send-http"]
        workflow["tool_definitions"] = [
            {
                "id": "tool-send-http",
                "name": "send_http_request",
                "description": "Send an HTTP request to a configured integration endpoint.",
                "tool_type": "python_function",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                    "required": ["url"],
                },
                "output_schema": {"type": "object", "properties": {}},
                "implementation": {
                    "implementation_type": "python_function",
                    "target": "tests.native_test_tools",
                    "callable_name": "send_http_request",
                },
                "security": {
                    "allow_network": True,
                },
                "tags": ["network"],
            }
        ]
        return workflow

    def _workflow_payload_with_http_request_tool(
            self,
            *,
            workflow_id: str,
            name: str,
            implementation_target: str = "https://discord.com/api/webhooks/channel/token",
            description: str = "Post curated news to Discord.",
    ) -> dict:
        workflow = self._workflow_payload(workflow_id=workflow_id, name=name, description=description)
        workflow["task_definitions"][0]["name"] = "Post to Discord"
        workflow["task_definitions"][0]["description"] = "Send the formatted message to a Discord webhook."
        workflow["task_definitions"][0]["tool_ids"] = ["tool-http-request"]
        workflow["tool_definitions"] = [
            {
                "id": "tool-http-request",
                "name": "send_http_request",
                "description": "Send an HTTP request to the configured workflow endpoint.",
                "tool_type": "http_request",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "method": {"type": "string"},
                        "body": {"type": "object"},
                    },
                    "required": ["url", "method"],
                },
                "output_schema": {"type": "object", "properties": {}},
                "implementation": {
                    "implementation_type": "http_request",
                    "target": implementation_target,
                    "config": {"method": "POST"},
                },
                "security": {
                    "allow_network": True,
                },
                "tags": ["network", "webhook"],
            }
        ]
        return workflow

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

    async def test_workflow_create_with_command_tool_requires_repo_write_permission(self) -> None:
        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-create-command-workflow",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Draft a workflow that edits the repo",
                    "content": {
                        "workflow_proposal": {
                            "summary": "Create workflow 'Repo Editing Workflow'.",
                            "workflow": self._workflow_payload_with_command_tool(
                                workflow_id="workflow-repo-edit",
                                name="Repo Editing Workflow",
                            ),
                        }
                    },
                }
            },
        )

        approval = requested["approval_request"]
        permission = approval["proposed_payload"]["repo_write_permission"]
        self.assertEqual(permission["permission_type"], "repo_write")
        self.assertEqual(permission["status"], "pending_human_approval")
        self.assertEqual(permission["mounts"][0]["mode"], "rw")
        self.assertEqual(
            approval["proposed_payload"]["workflow"]["metadata"]["repo_write_permission"]["status"],
            "pending_human_approval",
        )
        self.assertEqual(
            requested["assistant_message"]["content"]["repo_write_permission"]["status"],
            "pending_human_approval",
        )

        approved = await self.service.approve_request(
            approval["id"],
            actor_user_id="user-1",
            reason="Allow repo edits",
        )
        saved_permission = approved["workflow"]["metadata"]["repo_write_permission"]
        self.assertEqual(saved_permission["status"], "approved")
        self.assertEqual(saved_permission["approved_by_user_id"], "user-1")
        self.assertEqual(saved_permission["approval_request_id"], approval["id"])

        workflow = await self.context.workflow_repo.get("workflow-repo-edit")
        assert workflow is not None
        self.assertEqual(workflow.metadata["repo_write_permission"]["status"], "approved")

    async def test_plain_text_run_inspect_goes_to_llm_tools(self) -> None:
        with patch(
            "app.services.conversations.core.ExecutionService.get_execution",
            new=AsyncMock(),
        ) as get_execution:
            response = await self.service.post_message(
                "conversation-1",
                {
                    "message": {
                        "role": ConversationRole.USER.value,
                        "message_type": ConversationMessageType.USER_TEXT.value,
                        "plain_text": "What happened in this run?",
                        "content": {"text": "What happened in this run?"},
                        "metadata": {
                            "page_context": {
                                "surface": "runs.detail",
                                "selection": {"runId": "run-page-context-1"},
                                "entities": [{"type": "run", "id": "run-page-context-1"}],
                            }
                        },
                    }
                },
            )

        get_execution.assert_not_awaited()
        self.assertEqual(response["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(response["assistant_message"]["plain_text"], "direct reply")

    async def test_plain_text_run_pause_goes_to_llm_tools(self) -> None:
        self.context.control_plane.pause = AsyncMock(
            return_value=_FakeControlPlaneExecution("run-page-context-1", "paused")
        )

        response = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "role": ConversationRole.USER.value,
                    "message_type": ConversationMessageType.USER_TEXT.value,
                    "plain_text": "Please pause this run.",
                    "content": {"text": "Please pause this run."},
                    "metadata": {
                        "page_context": {
                            "surface": "runs.detail",
                            "selection": {"runId": "run-page-context-1"},
                            "entities": [
                                {
                                    "type": "run",
                                    "id": "run-page-context-1",
                                    "name": "Run page context",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.context.control_plane.pause.assert_not_awaited()
        self.assertEqual(response["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(response["assistant_message"]["plain_text"], "direct reply")

    async def test_plain_text_run_cancel_ignores_runs_list_context(self) -> None:
        self.context.control_plane.cancel = AsyncMock(
            return_value=_FakeControlPlaneExecution("run-page-context-1", "cancelled")
        )

        response = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "role": ConversationRole.USER.value,
                    "message_type": ConversationMessageType.USER_TEXT.value,
                    "plain_text": "Please cancel this run.",
                    "content": {"text": "Please cancel this run."},
                    "metadata": {
                        "page_context": {
                            "surface": "runs.list",
                            "selection": {"runId": "run-page-context-1"},
                            "entities": [{"type": "run", "id": "run-page-context-1"}],
                        }
                    },
                }
            },
        )

        self.context.control_plane.cancel.assert_not_awaited()
        self.assertEqual(response["assistant_message"]["plain_text"], "direct reply")

    async def test_plain_text_run_approval_goes_to_llm_tools(self) -> None:
        self.context.control_plane.approve = AsyncMock(return_value=True)

        response = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "role": ConversationRole.USER.value,
                    "message_type": ConversationMessageType.USER_TEXT.value,
                    "plain_text": "Please approve the pending request.",
                    "content": {"text": "Please approve the pending request."},
                    "metadata": {
                        "page_context": {
                            "surface": "runs.detail",
                            "selection": {
                                "runId": "run-page-context-1",
                                "toolId": "tool-needs-approval",
                            },
                        }
                    },
                }
            },
        )

        self.context.control_plane.approve.assert_not_awaited()
        self.assertEqual(response["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(response["assistant_message"]["plain_text"], "direct reply")

    async def test_plain_text_workflow_create_request_goes_to_llm_tools(self) -> None:
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

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)
        build_workflow.assert_not_awaited()

    async def test_plain_text_workflow_create_fallback_is_not_used_before_llm(self) -> None:
        with patch.dict("os.environ", {"TZ": "Asia/Singapore"}), patch(
            "app.services.conversations.core.WorkflowBuilderService.build_workflow_definition",
            new=AsyncMock(side_effect=RuntimeError("missing_scope: model.request")),
        ) as build_workflow:
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

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)
        build_workflow.assert_not_awaited()
        schedules = await self.context.schedule_repo.list()
        self.assertEqual(len(schedules), 0)

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
        self.assertEqual(approved["message"]["content"]["action"], "workflow_update")
        self.assertEqual(approved["message"]["content"]["target_type"], "workflow")
        self.assertEqual(approved["message"]["content"]["target_id"], "workflow-update")

        workflow = await self.context.workflow_repo.get("workflow-update")
        assert workflow is not None
        self.assertEqual(workflow.description, "Updated description")
        self.assertEqual(workflow.versioning.revision, 2)
        self.assertEqual(workflow.versioning.parent_version, "1.0.0")
        self.assertEqual(workflow.metadata["created_by"], "user-1")
        self.assertEqual(workflow.metadata["owner_ids"], ["user-1"])
        self.assertEqual(workflow.metadata["provenance"]["decision"], "approved")

    async def test_plain_text_workflow_update_goes_to_llm_tools_with_page_context(self) -> None:
        current = WorkflowDefinition(
            id="workflow-context",
            name="Context Workflow",
            description="Original description",
            entrypoint="node-1",
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
            versioning={
                "version": "1.0.0",
                "revision": 1,
                "parent_version": None,
                "is_published": True,
                "labels": [],
            },
        )
        await self.context.workflow_repo.save(current)
        with patch(
            "app.services.conversations.core.WorkflowBuilderService.update_workflow_definition",
            new=AsyncMock(),
        ) as update_workflow:
            requested = await self.service.post_message(
                "conversation-1",
                {
                    "message": {
                        "id": "message-context-update",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Please modify this workflow to add a review step.",
                        "content": {"text": "Please modify this workflow to add a review step."},
                        "metadata": {
                            "page_context": {
                                "surface": "workflow.detail",
                                "entities": [
                                    {
                                        "type": "workflow",
                                        "id": "workflow-context",
                                        "name": "Context Workflow",
                                    }
                                ],
                            }
                        },
                    }
                },
            )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)
        update_workflow.assert_not_awaited()

    async def test_plain_text_workflow_description_update_goes_to_llm_tools(self) -> None:
        current = WorkflowDefinition.model_validate(
            self._workflow_payload(
                workflow_id="e2e-workflow-page-context",
                name="E2E Page Context Workflow",
                description="Original description",
            )
        ).model_copy(
            update={
                "versioning": VersionDefinition(
                    version="1.0.0",
                    revision=1,
                    parent_version=None,
                    is_published=True,
                    labels=[],
                ),
            }
        )
        await self.context.workflow_repo.save(current)

        with patch(
            "app.services.conversations.core.WorkflowBuilderService.update_workflow_definition",
            new=AsyncMock(),
        ) as update_workflow:
            requested = await self.service.post_message(
                "conversation-1",
                {
                    "message": {
                        "id": "message-workflow-description-context-update",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": (
                            "Please propose updating this workflow by changing its description "
                            "to `Verified by workflow popup e2e`."
                        ),
                        "content": {
                            "text": (
                                "Please propose updating this workflow by changing its description "
                                "to `Verified by workflow popup e2e`."
                            )
                        },
                        "metadata": {
                            "page_context": {
                                "surface": "workflow.detail",
                                "entities": [
                                    {
                                        "type": "workflow",
                                        "id": "e2e-workflow-page-context",
                                        "name": "E2E Page Context Workflow",
                                    }
                                ],
                            }
                        },
                    }
                },
            )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)
        update_workflow.assert_not_awaited()

    async def test_plain_text_workflow_description_update_with_explicit_id_goes_to_llm_tools(self) -> None:
        current = WorkflowDefinition.model_validate(
            self._workflow_payload(
                workflow_id="e2e-workflow-explicit-context",
                name="E2E Explicit Workflow",
                description="Original description",
            )
        )
        await self.context.workflow_repo.save(current)

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-explicit-id-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": (
                        "Please propose updating workflow `e2e-workflow-explicit-context` by changing its "
                        "description to `Explicit workflow id update`."
                    ),
                    "content": {
                        "text": (
                            "Please propose updating workflow `e2e-workflow-explicit-context` by changing its "
                            "description to `Explicit workflow id update`."
                        )
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)

    async def test_workflow_update_proposal_returns_noop_when_requested_change_matches_current_state(self) -> None:
        current = WorkflowDefinition.model_validate(
            self._workflow_payload(
                workflow_id="e2e-workflows-p1779866142367-nochange",
                name="E2E Workflow No Change",
                description="Smoke test: popup assistant can propose page-scoped workflow changes.",
            )
        )
        await self.context.workflow_repo.save(current)

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-nochange",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Propose the same workflow description again.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "e2e-workflows-p1779866142367-nochange",
                            "patch": {
                                "description": "Smoke test: popup assistant can propose page-scoped workflow changes."
                            },
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(
            requested["assistant_message"]["plain_text"],
            "I did not create a workflow update proposal because 'E2E Workflow No Change' already matches the requested changes.",
        )
        self.assertNotIn("approval_request", requested)

    async def test_structured_workflow_update_uses_page_context_workflow_id(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-structured-context",
                    name="Structured Context Workflow",
                    description="Original description",
                )
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-structured-context-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow description.",
                    "content": {
                        "workflow_update_proposal": {
                            "summary": "Update structured workflow from page context.",
                            "patch": {"description": "Updated from workflow page context"},
                        }
                    },
                    "metadata": {
                        "page_context": {
                            "surface": "workflow.detail",
                            "selection": {"workflowId": "workflow-structured-context"},
                            "entities": [
                                {
                                    "type": "workflow",
                                    "id": "workflow-structured-context",
                                    "name": "Structured Context Workflow",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        self.assertEqual(requested["approval_request"]["approval_type"], "workflow_update")
        self.assertEqual(requested["approval_request"]["target_type"], "workflow")
        self.assertEqual(requested["approval_request"]["target_id"], "workflow-structured-context")
        self.assertEqual(
            requested["approval_request"]["proposed_payload"]["workflow"]["description"],
            "Updated from workflow page context",
        )

    async def test_workflow_update_proposal_repairs_unguarded_network_tool_security(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-network-update",
                    name="Network Workflow",
                    description="Original description",
                )
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-network-tool-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow to send a Discord notification.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-network-update",
                            "summary": "Add network notification tool.",
                            "workflow": self._workflow_payload_with_unguarded_network_tool(
                                workflow_id="workflow-network-update",
                                name="Network Workflow",
                            ),
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        security = requested["approval_request"]["proposed_payload"]["workflow"]["tool_definitions"][0]["security"]
        self.assertTrue(security["allow_network"])
        self.assertTrue(security["requires_approval"])
        self.assertTrue(security["sandbox_required"])
        self.assertTrue(security["dangerous"])

    async def test_workflow_update_proposal_maps_generated_http_request_to_dynamic_api_tool(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-discord-update",
                    name="Discord Workflow",
                    description="Original description",
                )
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-discord-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow to post research to Discord.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-discord-update",
                            "summary": "Post research to Discord.",
                            "workflow": self._workflow_payload_with_http_request_tool(
                                workflow_id="workflow-discord-update",
                                name="Discord Workflow",
                            ),
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(workflow["task_definitions"][0]["tool_ids"], ["agency.http.request"])
        self.assertEqual(workflow["tool_definitions"][0]["id"], "agency.http.request")
        self.assertEqual(workflow["tool_definitions"][0]["tool_type"], "python_function")
        self.assertEqual(
            workflow["tool_definitions"][0]["implementation"]["target"],
            "app.tools.implementations.http_integrations",
        )
        self.assertEqual(requested["approval_request"]["metadata"]["validation_repair"]["remaining_errors"], [])

    async def test_workflow_update_proposal_auto_resolves_discord_connector_binding(self) -> None:
        await self.context.credential_repo.create(
            CredentialDefinition(
                id="credential-discord-support",
                owner_user_id="user-1",
                name="Discord Support",
                provider="discord-bot",
                secret_ref="secret://agency/discord-support-token",
                metadata={
                    "application_id": "app-discord-support",
                    "bot_user_id": "10002",
                    "default_guild_id": "guild-456",
                },
            )
        )
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-discord-auto-resolve",
                    name="Discord Workflow",
                    description="Original description",
                )
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-discord-auto-resolve",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow to post research to Discord.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-discord-auto-resolve",
                            "summary": "Post research to Discord.",
                                "workflow": {
                                **self._workflow_payload_with_http_request_tool(
                                    workflow_id="workflow-discord-auto-resolve",
                                    name="Discord Workflow",
                                ),
                                "metadata": {
                                    "connector_bindings": [
                                        {
                                            "provider": "discord-bot",
                                            "purpose": "discord_delivery",
                                            "target_scope": {"default_guild_id": "guild-456"},
                                        }
                                    ]
                                },
                            },
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        bindings = workflow["metadata"]["connector_bindings"]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["provider"], "discord-bot")
        self.assertEqual(bindings[0]["credential_id"], "credential-discord-support")
        self.assertEqual(requested["approval_request"]["metadata"]["validation_repair"]["remaining_errors"], [])

    async def test_workflow_update_proposal_uses_dynamic_api_tool_for_generic_http_research(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-research-http-update",
                    name="Research HTTP Workflow",
                    description="Original description",
                )
            )
        )

        workflow_payload = self._workflow_payload_with_http_request_tool(
            workflow_id="workflow-research-http-update",
            name="Research HTTP Workflow",
            implementation_target="agency.system.http",
            description="Research live news over HTTP.",
        )
        workflow_payload["task_definitions"][0]["name"] = "Research live news"
        workflow_payload["task_definitions"][0]["description"] = "Fetch current news items over HTTP."

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-research-http-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow to research live news over HTTP.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-research-http-update",
                            "summary": "Research live news.",
                            "workflow": workflow_payload,
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(workflow["task_definitions"][0]["tool_ids"], ["agency.http.request"])
        self.assertEqual(workflow["tool_definitions"][0]["id"], "agency.http.request")
        self.assertEqual(requested["approval_request"]["metadata"]["validation_repair"]["remaining_errors"], [])

    async def test_workflow_update_pins_dynamic_api_tool_when_repo_copy_has_stale_security(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="agency.http.request",
                name="send_http_request",
                description="Stale dynamic API tool copy missing approval and sandbox flags.",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="app.tools.implementations.http_integrations",
                    callable_name="execute_custom_api",
                ),
                security=SecuritySettings(allow_network=True),
            )
        )
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-stale-http-tool-update",
                    name="Stale HTTP Tool Workflow",
                    description="Original description",
                )
            )
        )
        proposed = self._workflow_payload(
            workflow_id="workflow-stale-http-tool-update",
            name="Stale HTTP Tool Workflow",
            description="Post to Discord through the dynamic API tool.",
        )
        proposed["agent_definitions"] = [
            {
                "id": "agent-discord",
                "name": "Discord Agent",
                "instructions": "Post workflow output to Discord.",
                "tool_ids": [],
                "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                "metadata": {},
            }
        ]
        proposed["task_definitions"][0]["agent_id"] = "agent-discord"
        proposed["task_definitions"][0]["tool_ids"] = ["agency.http.request"]
        proposed["tool_definitions"] = []

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-stale-http-tool-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow to post to Discord.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-stale-http-tool-update",
                            "summary": "Post to Discord.",
                            "workflow": proposed,
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(workflow["task_definitions"][0]["tool_ids"], ["agency.http.request"])
        self.assertEqual(workflow["agent_definitions"][0]["tool_ids"], ["agency.http.request"])
        http_tool = workflow["tool_definitions"][0]
        self.assertEqual(http_tool["id"], "agency.http.request")
        self.assertTrue(http_tool["security"]["requires_approval"])
        self.assertTrue(http_tool["security"]["sandbox_required"])
        self.assertTrue(http_tool["security"]["dangerous"])
        self.assertEqual(requested["approval_request"]["metadata"]["validation_repair"]["remaining_errors"], [])

    async def test_workflow_update_proposal_repairs_missing_task_tool_references(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-missing-tools-update",
                    name="News Workflow",
                    description="Original description",
                )
            )
        )
        proposed = self._workflow_payload(
            workflow_id="workflow-missing-tools-update",
            name="News Workflow",
            description="Updated news research workflow.",
        )
        proposed["task_definitions"] = [
            {
                **proposed["task_definitions"][0],
                "id": "task-research-news",
                "name": "Research and Curate News Items",
                "tool_ids": ["missing-news-search-tool"],
            },
            {
                **proposed["task_definitions"][0],
                "id": "task-expand-lookback",
                "name": "Expand Lookback for Category Shortages",
                "tool_ids": ["missing-lookback-tool"],
            },
            {
                **proposed["task_definitions"][0],
                "id": "task-validate-quality",
                "name": "Validate Research Quality Before Writing",
                "tool_ids": ["missing-quality-tool"],
            },
        ]

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-missing-tools-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow with a better research process.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-missing-tools-update",
                            "summary": "Improve news research workflow.",
                            "workflow": proposed,
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        task_definitions = requested["approval_request"]["proposed_payload"]["workflow"]["task_definitions"]
        self.assertEqual([task["tool_ids"] for task in task_definitions], [[], [], []])
        repair = requested["approval_request"]["metadata"]["validation_repair"]
        self.assertEqual(repair["remaining_errors"], [])
        self.assertTrue(any("references a missing tool" in error for error in repair["initial_errors"]))

    async def test_workflow_update_reapplies_safety_repair_after_model_repair(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-model-repair-update",
                    name="Model Repair Workflow",
                    description="Original description",
                )
            )
        )
        proposed = self._workflow_payload(
            workflow_id="workflow-model-repair-update",
            name="Model Repair Workflow",
            description="Updated workflow requiring model repair.",
        )
        proposed["edges"] = [
            {
                "source_node_id": "node-1",
                "target_node_id": "missing-node",
                "edge_type": "default",
                "metadata": {},
            }
        ]
        model_repaired_payload = self._workflow_payload_with_unguarded_network_tool(
            workflow_id="workflow-model-repair-update",
            name="Model Repair Workflow",
        )
        model_repaired_payload["agent_definitions"] = [
            {
                "id": "agent-format-validator",
                "name": "Research and Format Quality Validator",
                "instructions": "Validate research quality and formatting.",
                "tool_ids": ["missing-format-quality-tool"],
                "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                "metadata": {},
            }
        ]
        model_repaired_payload["task_definitions"][0]["agent_id"] = "agent-format-validator"

        with patch("app.services.conversations.core.WorkflowBuilderService.repair_workflow_definition", new=AsyncMock(
            return_value=WorkflowDefinition.model_validate(model_repaired_payload)
        )):
            requested = await self.service.post_message(
                "conversation-1",
                {
                    "message": {
                        "id": "message-workflow-model-repair-update",
                        "role": "user",
                        "message_type": "user_text",
                        "plain_text": "Update this workflow and repair validation issues.",
                        "content": {
                            "workflow_update_proposal": {
                                "workflow_id": "workflow-model-repair-update",
                                "summary": "Repair workflow.",
                                "workflow": proposed,
                            }
                        },
                    }
                },
            )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(workflow["agent_definitions"][0]["tool_ids"], ["agency.http.request"])
        security = workflow["tool_definitions"][0]["security"]
        self.assertTrue(security["requires_approval"])
        self.assertTrue(security["sandbox_required"])
        self.assertTrue(security["dangerous"])
        self.assertEqual(requested["approval_request"]["metadata"]["validation_repair"]["remaining_errors"], [])

    async def test_workflow_update_proposal_accepts_workflow_read_view_payload(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-read-view-update",
                    name="Read View Workflow",
                    description="Original description",
                )
            )
        )
        proposed = self._workflow_payload(
            workflow_id="workflow-read-view-update",
            name="Read View Workflow",
            description="Updated from read view payload.",
        )
        proposed["agents"] = [
            {
                "id": "agent-read-view",
                "name": "Research Agent",
                "instructions": "Research with available tools.",
                "tool_ids": ["agency.http.request"],
                "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
            }
        ]
        proposed["tasks"] = [
            {
                **proposed["task_definitions"][0],
                "agent_id": "agent-read-view",
                "tool_ids": ["agency.http.request"],
            }
        ]
        proposed.pop("agent_definitions", None)
        proposed.pop("task_definitions", None)
        proposed["tool_definitions"] = [
            {
                "id": "agency.http.request",
                "name": "send_http_request",
                "description": "Compact built-in tool summary from workflow read view.",
                "tool_type": "python_function",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "implementation_target": "agency.http.request",
                "security": {"allow_network": True},
                "mcp": {},
                "tags": [],
            },
            {
                "id": "agency.browser.open",
                "name": "open_browser",
                "description": "Compact built-in browser summary from workflow read view.",
                "tool_type": "python_function",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "implementation_target": "agency.browser.open",
                "security": {"allow_network": True, "allow_browser": True},
                "mcp": {},
                "tags": [],
            },
        ]
        proposed["input_keys"] = []
        proposed["protected_execution"] = False
        proposed["mutable_by_agent"] = True

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-workflow-read-view-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this workflow from the read view.",
                    "content": {
                        "workflow_update_proposal": {
                            "workflow_id": "workflow-read-view-update",
                            "summary": "Update from read view payload.",
                            "workflow": proposed,
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "workflow_update_proposal")
        workflow = requested["approval_request"]["proposed_payload"]["workflow"]
        self.assertEqual(workflow["agent_definitions"][0]["id"], "agent-read-view")
        self.assertEqual(workflow["task_definitions"][0]["tool_ids"], ["agency.http.request"])
        self.assertEqual(
            [tool["id"] for tool in workflow["tool_definitions"]],
            ["agency.http.request", "agency.browser.open"],
        )
        self.assertIn("implementation", workflow["tool_definitions"][0])
        self.assertEqual(workflow["metadata"]["input_keys"], [])
        self.assertFalse(workflow["metadata"]["protected_execution"])
        self.assertTrue(workflow["metadata"]["mutable_by_agent"])

    async def test_structured_agent_update_uses_page_context_agent_id(self) -> None:
        await self.context.agent_repo.save(
            AgentDefinition(
                id="agent-context",
                name="Context Agent",
                description="Original description",
                role="Original role",
                instructions="Original instructions",
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-agent-context-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update this agent description.",
                    "content": {
                        "agent_update_proposal": {
                            "summary": "Update agent 'Context Agent'.",
                            "patch": {"description": "Updated from page context"},
                        }
                    },
                    "metadata": {
                        "page_context": {
                            "surface": "agent.list",
                            "selection": {"agentId": "agent-context"},
                            "entities": [
                                {
                                    "type": "agent",
                                    "id": "agent-context",
                                    "name": "Context Agent",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(requested["approval_request"]["approval_type"], "other")
        self.assertEqual(requested["approval_request"]["target_type"], "agent")
        self.assertEqual(requested["approval_request"]["target_id"], "agent-context")
        self.assertEqual(
            requested["approval_request"]["proposed_payload"]["agent"]["description"],
            "Updated from page context",
        )

    async def test_plain_text_agent_update_goes_to_llm_tools(self) -> None:
        await self.context.agent_repo.save(
            AgentDefinition(
                id="agent-plain-context",
                name="Context Agent",
                description="Original description",
                role="Original role",
                instructions="Original instructions",
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-agent-plain-context-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Please rename this agent to Support Triage.",
                    "content": {"text": "Please rename this agent to Support Triage."},
                    "metadata": {
                        "page_context": {
                            "surface": "agent.list",
                            "selection": {"agentId": "agent-plain-context"},
                            "entities": [
                                {
                                    "type": "agent",
                                    "id": "agent-plain-context",
                                    "name": "Context Agent",
                                }
                            ],
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)

    async def test_plain_text_agent_update_with_explicit_id_goes_to_llm_tools(self) -> None:
        await self.context.agent_repo.save(
            AgentDefinition(
                id="e2e-agent-page-context",
                name="E2E Page Context Agent",
                description="Original description",
                role="Temporary QA agent.",
                instructions="Answer briefly.",
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-agent-explicit-id-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": (
                        "Please propose updating agent `e2e-agent-page-context` by changing its "
                        "description to `Verified by popup assistant e2e`."
                    ),
                    "content": {
                        "text": (
                            "Please propose updating agent `e2e-agent-page-context` by changing its "
                            "description to `Verified by popup assistant e2e`."
                        )
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(requested["assistant_message"]["plain_text"], "direct reply")
        self.assertNotIn("approval_request", requested)

    async def test_agent_update_proposal_returns_noop_when_requested_change_matches_current_state(self) -> None:
        await self.context.agent_repo.save(
            AgentDefinition(
                id="e2e-agents-p1779866142367-nochange",
                name="E2E Agent No Change",
                description="Smoke test: popup assistant can propose page-scoped workflow changes.",
                role="Temporary QA agent.",
                instructions="Answer briefly.",
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-agent-nochange",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Propose the same agent description again.",
                    "content": {
                        "agent_update_proposal": {
                            "agent_id": "e2e-agents-p1779866142367-nochange",
                            "patch": {
                                "description": "Smoke test: popup assistant can propose page-scoped workflow changes."
                            },
                        }
                    },
                }
            },
        )

        self.assertEqual(requested["assistant_message"]["message_type"], "assistant_text")
        self.assertEqual(
            requested["assistant_message"]["plain_text"],
            "I did not create an agent update proposal because 'E2E Agent No Change' already matches the requested changes.",
        )
        self.assertNotIn("approval_request", requested)

    async def test_agent_management_tool_proposes_update_from_page_context(self) -> None:
        await self.context.agent_repo.save(
            AgentDefinition(
                id="agent-tool-context",
                name="Tool Context Agent",
                description="Original description",
                role="Original role",
                instructions="Original instructions",
            )
        )
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-agent-tool-context",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Update this agent description.",
                content={"text": "Update this agent description."},
                metadata={
                    "page_context": {
                        "surface": "agent.list",
                        "selection": {"agentId": "agent-tool-context"},
                    }
                },
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.agent.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_agent_management_tool(
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={"patch": {"description": "Updated via agent tool"}},
            origin_message_id=origin.id,
        )

        approval = result["approval_payload"]["approval_request"]
        self.assertEqual(approval["approval_type"], "other")
        self.assertEqual(approval["target_type"], "agent")
        self.assertEqual(approval["target_id"], "agent-tool-context")
        self.assertEqual(
            approval["proposed_payload"]["agent"]["description"],
            "Updated via agent tool",
        )

    async def test_tool_context_resolves_selected_tool_entity(self) -> None:
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-tool-entity-context",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Inspect this tool.",
                content={"text": "Inspect this tool."},
                metadata={
                    "page_context": {
                        "surface": "tools.contracts",
                        "entities": [
                            {
                                "type": "tool",
                                "id": "selected-tool",
                                "name": "Selected Tool",
                            }
                        ],
                    }
                },
            )
        )

        self.assertEqual(self.service._tool_id_from_message_context(origin), "selected-tool")

    async def test_tool_management_update_uses_selected_tool_context_and_patch(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-page-context",
                name="page_context_tool",
                description="Original tool description",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
            )
        )
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-tool-update-context",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Update this tool description.",
                content={"text": "Update this tool description."},
                metadata={
                    "page_context": {
                        "surface": "tools.contracts",
                        "entities": [
                            {
                                "type": "tool",
                                "id": "tool-page-context",
                                "name": "page_context_tool",
                            }
                        ],
                    }
                },
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.tool.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_tool_management_tool(
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={
                "patch": {"description": "Updated from selected tool context"},
                "summary": "Update selected tool.",
            },
            origin_message_id=origin.id,
        )

        approval = result["approval_payload"]["approval_request"]
        self.assertEqual(approval["approval_type"], "tool_update")
        self.assertEqual(approval["target_type"], "tool")
        self.assertEqual(approval["target_id"], "tool-page-context")
        self.assertEqual(
            approval["proposed_payload"]["tool"]["description"],
            "Updated from selected tool context",
        )

    async def test_workflow_management_tool_uses_page_context_and_patch(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition.model_validate(
                self._workflow_payload(
                    workflow_id="workflow-tool-context",
                    name="Workflow Tool Context",
                    description="Original workflow tool description",
                )
            )
        )
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-workflow-tool-context",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Update this workflow description.",
                content={"text": "Update this workflow description."},
                metadata={
                    "page_context": {
                        "surface": "workflow.detail",
                        "selection": {"workflowId": "workflow-tool-context"},
                    }
                },
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.workflow.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_workflow_tool(
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={
                "patch": {"description": "Updated from workflow tool context"},
                "summary": "Update selected workflow.",
            },
            origin_message_id=origin.id,
        )

        approval = result["approval_payload"]["approval_request"]
        self.assertEqual(approval["approval_type"], "workflow_update")
        self.assertEqual(approval["target_type"], "workflow")
        self.assertEqual(approval["target_id"], "workflow-tool-context")
        self.assertEqual(
            approval["proposed_payload"]["workflow"]["description"],
            "Updated from workflow tool context",
        )

    async def test_tool_management_update_uses_page_context_and_approval_persists(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-popup-e2e",
                name="popup_e2e_tool",
                description="Original tool description",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
            )
        )

        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-tool-popup-e2e-update",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Update this selected tool description.",
                content={"text": "Update this selected tool description."},
                metadata={
                    "page_context": {
                        "surface": "tools.contracts",
                        "entities": [{"type": "tool", "id": "tool-popup-e2e", "name": "popup_e2e_tool"}],
                    }
                },
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.tool.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_tool_management_tool(
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={
                "patch": {"description": "Verified by tool popup e2e"},
                "summary": "Update selected tool.",
            },
            origin_message_id=origin.id,
        )
        requested = result["approval_payload"]

        self.assertEqual(requested["assistant_message"]["message_type"], "approval_request")
        self.assertEqual(requested["approval_request"]["approval_type"], "tool_update")
        self.assertEqual(requested["approval_request"]["target_id"], "tool-popup-e2e")
        self.assertEqual(
            requested["approval_request"]["proposed_payload"]["tool"]["description"],
            "Verified by tool popup e2e",
        )

        before = await self.context.tool_repo.get("tool-popup-e2e")
        assert before is not None
        self.assertEqual(before.description, "Original tool description")

        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Apply it",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertEqual(approved["tool"]["description"], "Verified by tool popup e2e")
        after = await self.context.tool_repo.get("tool-popup-e2e")
        assert after is not None
        self.assertEqual(after.description, "Verified by tool popup e2e")
        self.assertEqual(after.framework_hints.metadata["provenance"]["action"], "tool_update")
        self.assertEqual(
            after.framework_hints.metadata["provenance"]["approval_request_id"],
            requested["approval_request"]["id"],
        )

    async def test_tool_update_proposal_returns_noop_when_requested_change_matches_current_state(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="e2e-tools-p1779866142367-nochange",
                name="e2e_tool_nochange",
                description="Smoke test: popup assistant can propose page-scoped workflow changes.",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
            )
        )
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-tool-nochange",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Propose the same tool description again.",
                content={"text": "Propose the same tool description again."},
                metadata={
                    "page_context": {
                        "surface": "tools.contracts",
                        "selection": {"toolId": "e2e-tools-p1779866142367-nochange"},
                    }
                },
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.tool.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_tool_management_tool(
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={
                "patch": {
                    "description": "Smoke test: popup assistant can propose page-scoped workflow changes."
                },
                "summary": "Reapply the selected tool description.",
            },
            origin_message_id=origin.id,
        )

        self.assertEqual(result["result"]["status"], "error")
        self.assertEqual(
            result["result"]["error"],
            "I did not create a tool update proposal because 'e2e_tool_nochange' already matches the requested changes.",
        )

    async def test_tool_update_proposal_infers_tool_id_from_payload(self) -> None:
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-payload-fallback",
                name="tool_payload_fallback",
                description="Original fallback description",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
            )
        )
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-tool-payload-fallback",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Update the selected tool from the full payload.",
                content={"text": "Update the selected tool from the full payload."},
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.tool.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_tool_management_tool(  # noqa: SLF001
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={
                "summary": "Update the selected tool.",
                "tool": {
                    "id": "tool-payload-fallback",
                    "name": "tool_payload_fallback",
                    "description": "Updated fallback description",
                    "tool_type": "python_function",
                    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                    "output_schema": {"type": "object"},
                    "implementation": {
                        "implementation_type": "python_function",
                        "target": "tests.native_test_tools",
                        "callable_name": "echo_tool",
                    },
                },
            },
            origin_message_id=origin.id,
        )

        self.assertIn("approval_payload", result)
        self.assertEqual(result["approval_payload"]["approval_request"]["target_id"], "tool-payload-fallback")
        self.assertEqual(
            result["approval_payload"]["approval_request"]["proposed_payload"]["tool"]["description"],
            "Updated fallback description",
        )

    async def test_tool_update_proposal_auto_resolves_connector_credential(self) -> None:
        await self.context.credential_repo.create(
            CredentialDefinition(
                id="credential-discord-unrelated",
                owner_user_id="user-1",
                name="Discord Unrelated",
                provider="discord",
                secret_ref="env://DISCORD_UNRELATED",
                metadata={"guild_id": "guild-unrelated", "bot_user_id": "10001"},
            )
        )
        await self.context.credential_repo.create(
            CredentialDefinition(
                id="credential-discord-support",
                owner_user_id="user-1",
                name="Discord Support",
                provider="discord",
                secret_ref="env://DISCORD_SUPPORT",
                metadata={"guild_id": "guild-456", "bot_user_id": "10002"},
            )
        )
        await self.context.tool_repo.create(
            ToolDefinition(
                id="tool-discord-fallback",
                name="tool_discord_fallback",
                description="Original Discord tool description",
                tool_type=ToolType.PYTHON_FUNCTION,
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={"type": "object"},
                implementation=ToolImplementationReference(
                    implementation_type="python_function",
                    target="tests.native_test_tools",
                    callable_name="echo_tool",
                ),
            )
        )
        origin = await self.context.conversation_message_repo.create(
            ConversationMessage(
                id="message-tool-discord-fallback",
                conversation_id="conversation-1",
                role=ConversationRole.USER,
                message_type=ConversationMessageType.USER_TEXT,
                plain_text="Update the Discord connector tool.",
                content={"text": "Update the Discord connector tool."},
            )
        )
        profile = await self.context.main_agent_profile_repo.get("main-agent-profile")
        assert profile is not None
        tool = await self.context.tool_repo.get("agency.tool.propose-update")
        assert tool is not None

        result = await self.service._execute_conversation_tool_management_tool(  # noqa: SLF001
            profile=profile,
            conversation_id="conversation-1",
            tool=tool,
            arguments={
                "summary": "Update the Discord connector tool.",
                "tool": {
                    "id": "tool-discord-fallback",
                    "name": "tool_discord_fallback",
                    "description": "Updated Discord tool description",
                    "tool_type": "python_function",
                    "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
                    "output_schema": {"type": "object"},
                    "implementation": {
                        "implementation_type": "python_function",
                        "target": "tests.native_test_tools",
                        "callable_name": "echo_tool",
                    },
                    "security": {
                        "connector_bindings": [
                            {
                                "provider": "discord",
                                "purpose": "discord_dm_delivery",
                                "target_scope": {"guild_id": "guild-456", "bot_user_id": "10002"},
                            }
                        ]
                    },
                },
            },
            origin_message_id=origin.id,
        )

        self.assertIn("approval_payload", result)
        proposed = result["approval_payload"]["approval_request"]["proposed_payload"]["tool"]
        self.assertEqual(proposed["security"]["connector_bindings"][0]["credential_id"], "credential-discord-support")
        self.assertEqual(proposed["security"]["connector_bindings"][0]["provider"], "discord-bot")

    async def test_tool_update_request_normalizes_legacy_connector_ref_binding(self) -> None:
        current = ToolDefinition(
            id="discord-tool",
            name="Discord Connector Tool",
            description="Send messages through Discord.",
            tool_type=ToolType.PYTHON_FUNCTION,
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            output_schema={"type": "object"},
            implementation=ToolImplementationReference(
                implementation_type="python_function",
                target="tests.native_test_tools",
                callable_name="echo_tool",
                config={"provider": "discord"},
            ),
            security=SecuritySettings(),
        )

        proposed = self.service._tool_from_update_request(  # noqa: SLF001
            current=current,
            request={
                "tool": {
                    "name": current.name,
                    "description": current.description,
                    "tool_type": "python_function",
                    "input_schema": current.input_schema,
                    "output_schema": current.output_schema,
                    "implementation": {
                        "implementation_type": "python_function",
                        "target": "tests.native_test_tools",
                        "callable_name": "echo_tool",
                        "config": {"provider": "discord"},
                    },
                    "security": {
                        "connector_bindings": [
                            {
                                "ref": "credential-discord-support",
                                "purpose": "discord_dm_delivery",
                            }
                        ]
                    },
                }
            },
        )

        self.assertEqual(proposed.security.connector_bindings[0].provider, "discord-bot")
        self.assertEqual(proposed.security.connector_bindings[0].credential_id, "credential-discord-support")
        self.assertEqual(proposed.security.connector_bindings[0].purpose, "discord_dm_delivery")

    async def test_workflow_update_request_normalizes_legacy_connector_ref_binding(self) -> None:
        await self.context.workflow_repo.create(
            WorkflowDefinition(
                id="workflow-discord",
                name="Discord Workflow",
                description="Send Discord messages.",
                entrypoint="node-1",
                nodes=[
                    {
                        "id": "node-1",
                        "name": "Entry",
                        "node_type": "task",
                        "task_id": "task-1",
                        "config": {},
                        "metadata": {},
                    }
                ],
                task_definitions=[
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
                agent_definitions=[],
                tool_definitions=[],
                metadata={
                    "connector_bindings": [
                        {
                            "ref": "credential-discord-support",
                            "purpose": "discord_dm_delivery",
                        }
                    ]
                },
            )
        )

        updated = await self.service._workflow_from_update_request(  # noqa: SLF001
            current_workflow=await self.context.workflow_repo.get("workflow-discord"),
            profile=await self.context.main_agent_profile_repo.get("main-agent-profile"),
            request={
                "workflow": {
                    "id": "workflow-discord",
                    "name": "Discord Workflow",
                    "description": "Send Discord messages.",
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
                    "agent_definitions": [],
                    "tool_definitions": [],
                    "metadata": {
                        "connector_bindings": [
                            {
                                "ref": "credential-discord-support",
                                "purpose": "discord_dm_delivery",
                            }
                        ]
                    },
                }
            },
        )

        self.assertEqual(updated.metadata["connector_bindings"][0]["provider"], "discord-bot")
        self.assertEqual(updated.metadata["connector_bindings"][0]["credential_id"], "credential-discord-support")
        self.assertEqual(updated.metadata["connector_bindings"][0]["purpose"], "discord_dm_delivery")

    async def test_approved_agent_update_persists_agent_with_provenance(self) -> None:
        await self.context.agent_repo.save(
            AgentDefinition(
                id="agent-update",
                name="Agent Update",
                description="Original description",
                role="Original role",
                instructions="Original instructions",
            )
        )

        requested = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-agent-update",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Update the agent",
                    "content": {
                        "agent_update_proposal": {
                            "agent_id": "agent-update",
                            "patch": {
                                "description": "Updated description",
                                "instructions": "Updated instructions",
                            },
                        }
                    },
                }
            },
        )

        approved = await self.service.approve_request(
            requested["approval_request"]["id"],
            actor_user_id="user-1",
            reason="Apply it",
        )

        self.assertEqual(approved["approval_request"]["status"], "approved")
        self.assertEqual(approved["agent"]["description"], "Updated description")
        self.assertEqual(approved["agent"]["instructions"], "Updated instructions")
        self.assertEqual(approved["agent"]["system_prompt"], "Updated instructions")

        agent = await self.context.agent_repo.get("agent-update")
        assert agent is not None
        self.assertEqual(agent.description, "Updated description")
        self.assertEqual(agent.instructions, "Updated instructions")
        self.assertEqual(agent.metadata["provenance"]["action"], "agent_update")
        self.assertEqual(agent.metadata["provenance"]["approval_request_id"], requested["approval_request"]["id"])

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

    async def test_main_agent_has_connector_tools(self) -> None:
        profile = await self.setup_service.require_active_main_agent_profile()
        agent = await self.context.agent_repo.get(profile.agent_id)

        assert agent is not None
        self.assertIn(SYSTEM_CONNECTOR_CAPABILITIES_TOOL_ID, agent.tool_ids)
        self.assertIn(SYSTEM_CONNECTOR_CREDENTIALS_TOOL_ID, agent.tool_ids)
        self.assertIn(SYSTEM_EXECUTION_GET_TOOL_ID, agent.tool_ids)
        self.assertIn(SYSTEM_EXECUTION_PAUSE_TOOL_ID, agent.tool_ids)
        self.assertIsNotNone(await self.context.tool_repo.get(SYSTEM_CONNECTOR_CAPABILITIES_TOOL_ID))
        self.assertIsNotNone(await self.context.tool_repo.get(SYSTEM_EXECUTION_PAUSE_TOOL_ID))

    async def test_popup_provider_metadata_prefers_llm_tools_over_plain_text_shortcut(self) -> None:
        await self.context.workflow_repo.save(
            WorkflowDefinition(
                id="workflow-popup-llm-first",
                name="Popup LLM First",
                description="Original description",
                entrypoint="node-1",
                metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
                versioning={"version": "1.0.0", "revision": 1, "parent_version": None, "is_published": True,
                            "labels": []},
            )
        )

        result = await self.service.post_message(
            "conversation-1",
            {
                "message": {
                    "id": "message-popup-llm-first",
                    "role": "user",
                    "message_type": "user_text",
                    "plain_text": "Please change this workflow description to `LLM first`.",
                    "content": {"text": "Please change this workflow description to `LLM first`."},
                    "metadata": {
                        "page_context": {
                            "surface": "workflow.detail",
                            "selection": {"workflowId": "workflow-popup-llm-first"},
                        },
                        "assistant_providers": {
                            "version": "2026-05-27",
                            "providers": [
                                {
                                    "id": "workflow.provider",
                                    "label": "Workflow provider",
                                    "systemToolIds": ["agency.workflow.get", "agency.workflow.propose-update"],
                                }
                            ],
                        },
                    },
                }
            },
        )

        self.assertNotIn("approval_request", result)
        self.assertEqual(result["assistant_message"]["plain_text"], "direct reply")

    async def test_connector_capabilities_tool_returns_registry(self) -> None:
        tool = await self.context.tool_repo.get(SYSTEM_CONNECTOR_CAPABILITIES_TOOL_ID)

        assert tool is not None
        result = await self.service._execute_conversation_connector_tool(
            conversation_id="conversation-1",
            tool=tool,
            arguments={},
        )

        payload = result["result"]
        self.assertEqual(payload["status"], "ok")
        self.assertIn("telegram-bot", payload["connectors"])
        self.assertTrue(payload["connectors"]["telegram-bot"]["healthSupported"])

    async def test_connector_credentials_tool_redacts_secret_refs(self) -> None:
        await self.context.credential_repo.create(
            CredentialDefinition(
                id="credential-telegram",
                owner_user_id="user-1",
                name="Telegram Bot",
                provider="telegram",
                secret_ref="env://TELEGRAM_BOT_TOKEN",
                metadata={"phone_number_id": "123", "access_token": "secret-token"},
            )
        )
        await self.context.credential_repo.create(
            CredentialDefinition(
                id="credential-other-owner",
                owner_user_id="user-2",
                name="Other Owner Bot",
                provider="telegram",
                secret_ref="env://OTHER_TOKEN",
                metadata={},
            )
        )
        tool = await self.context.tool_repo.get(SYSTEM_CONNECTOR_CREDENTIALS_TOOL_ID)

        assert tool is not None
        result = await self.service._execute_conversation_connector_tool(
            conversation_id="conversation-1",
            tool=tool,
            arguments={"provider": "telegram"},
        )

        payload = result["result"]
        self.assertEqual(payload["status"], "ok")
        self.assertEqual([item["id"] for item in payload["items"]], ["credential-telegram"])
        credential = payload["items"][0]
        self.assertNotIn("secret_ref", credential)
        self.assertTrue(credential["secret_ref_present"])
        self.assertEqual(credential["secret_ref_scheme"], "env")
        self.assertEqual(credential["metadata"]["phone_number_id"], "123")
        self.assertEqual(credential["metadata"]["access_token"], "[REDACTED]")

    async def test_connector_resolve_tool_returns_match_or_ambiguity(self) -> None:
        for credential_id, repo in (
            ("credential-github-api", "api"),
            ("credential-github-web", "web"),
        ):
            await self.context.credential_repo.create(
                CredentialDefinition(
                    id=credential_id,
                    owner_user_id="user-1",
                    name=f"GitHub acme/{repo}",
                    provider="github",
                    secret_ref=f"env://{credential_id.upper().replace('-', '_')}",
                    metadata={"owner": "acme", "repo": repo},
                )
            )
        tool = await self.context.tool_repo.get(SYSTEM_CONNECTOR_RESOLVE_TOOL_ID)

        assert tool is not None
        matched = await self.service._execute_conversation_connector_tool(
            conversation_id="conversation-1",
            tool=tool,
            arguments={"provider": "github", "filters": {"owner": "acme", "repo": "api"}},
        )
        self.assertEqual(matched["result"]["status"], "matched")
        self.assertEqual(matched["result"]["credential"]["id"], "credential-github-api")

        ambiguous = await self.service._execute_conversation_connector_tool(
            conversation_id="conversation-1",
            tool=tool,
            arguments={"provider": "github", "filters": {"owner": "acme"}},
        )
        self.assertEqual(ambiguous["result"]["status"], "ambiguous")
        self.assertEqual(ambiguous["result"]["match_count"], 2)
