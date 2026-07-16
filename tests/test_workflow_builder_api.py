from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes import create_api_router
from app.core.config import reset_settings_cache
from app.domain import (
    AgentDefinition,
    ExecutionArtifact,
    MemoryRecord,
    ModelProfileDefinition,
    NodeType,
    TaskDefinition,
    ToolDefinition,
    UserDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.llm.base import ModelResponse
from app.services.workflow_builder import WorkflowBuilderService
from app.services.workflow_validation import WorkflowValidationService
from app.tools.builtins import builtin_tool_definitions


class FakeBuilderModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition):
        self.profile = profile

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="ok", provider=self.profile.provider, model=self.profile.model, latency_ms=1.0)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        schema_name = kwargs.get("schema_name")
        if schema_name == "workflow_builder_rewrite_agent":
            content = {
                "name": "Research Agent",
                "role": "Senior Research Strategist",
                "instructions": "Produce a clear research output",
                "backstory": "Specializes in structured analytical work.",
            }
        elif schema_name == "workflow_builder_rewrite_task":
            content = {
                "name": "Draft Decision Memo",
                "description": "Write a clear memo that captures the decision context and recommendation.",
                "expected_output": "A concise decision memo.",
            }
        elif schema_name == "workflow_builder_task_list":
            content = {
                "assistant_message": "I drafted a workflow outline with clear execution steps.",
                "tasks": [
                    {
                        "name": "Outline Launch Strategy",
                        "description": "Define the launch objective, audience, and release approach.",
                        "expected_output": "A launch strategy outline.",
                    }
                ],
            }
        elif schema_name == "workflow_builder_agent_list":
            content = {
                "agents": [
                    {
                        "name": "Launch Strategist",
                        "role": "Plans the workflow approach",
                        "instructions": "Create a coherent rollout plan",
                        "backstory": "Experienced in product launches and structured planning.",
                    }
                ]
            }
        elif schema_name == "workflow_builder_workflow_summary":
            content = {
                "workflow": {
                    "name": "Launch Strategy Workflow",
                    "description": "Coordinates the planning steps for a product launch workflow.",
                }
            }
        else:
            raise AssertionError(f"Unexpected schema_name: {schema_name}")

        return ModelResponse(content=content, provider=self.profile.provider, model=self.profile.model, latency_ms=1.0)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "ok"

    def count_tokens(self, messages, **kwargs):
        return 0

    def health_check(self):
        return {"ok": True}


class WorkflowBuilderApiTests(unittest.TestCase):
    def setUp(self):
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: FakeBuilderModelClient(profile))
        app = FastAPI()
        app.include_router(create_api_router(self.context))
        self.client = TestClient(app)
        self.owner_headers = {
            "x-agency-user-id": "user-owner",
            "x-agency-user-email": "owner@example.com",
        }
        self.client.headers.update(self.owner_headers)
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(id="user-owner", email="owner@example.com", display_name="Owner")
            )
        )
        self._seed_builder_profile()

    def _seed_builder_profile(self):
        profile = ModelProfileDefinition(
            id="profile-builder",
            name="Builder Test Profile",
            provider="fake",
            model="builder-model",
            supports_structured_output=True,
        )
        asyncio.run(self.context.model_profile_repo.create(profile))

    def _tool_payload(self, *, tool_id: str = "tool-1", dangerous: bool = False):
        return {
            "id": tool_id,
            "name": "Example Tool",
            "description": "Example tool",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "output_schema": {"type": "object"},
            "implementation": {
                "implementation_type": "python",
                "target": "tests.native_test_tools",
                "callable_name": "echo_tool",
                "config": {},
            },
            "security": {
                "approval_required": False,
                "sandbox_required": False if dangerous else True,
                "allow_shell": dangerous,
                "allow_browser": False,
                "allow_filesystem": False,
                "allow_network": False,
                "allowlisted_mcp_servers": [],
                "secret_references": [],
                "redaction_enabled": False,
                "redaction_rules": [],
            },
            "mcp_exposure": {
                "expose_as_mcp_tool": False,
                "expose_as_mcp_resource": False,
                "expose_as_mcp_prompt": False,
                "name_override": None,
                "tags": [],
            },
            "tags": [],
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }

    def _agent_payload(self, *, agent_id: str = "agent-1", tool_ids=None, model_profile_id: str | None = None):
        return {
            "id": agent_id,
            "name": "Example Agent",
            "description": "Agent description",
            "instructions": "Do the task carefully.",
            "role": "operator",
            "backstory": "helpful",
            "model_profile_id": model_profile_id,
            "tool_ids": tool_ids or [],
            "handoff_agent_ids": [],
            "guardrails": [],
            "memory": {
                "enabled": False,
                "strategy": None,
                "scope": None,
                "backend_ref": None,
                "max_entries": None,
                "ttl_seconds": None,
                "config": {},
            },
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
            "metadata": {},
        }

    def _workflow_payload(self, *, tool_id: str = "tool-1", agent_id: str = "agent-1",
                          model_profile_id: str | None = None):
        return {
            "id": "workflow-1",
            "name": "Example Workflow",
            "description": "Workflow description",
            "nodes": [
                {
                    "id": "node-1",
                    "name": "Task Node",
                    "node_type": "task",
                    "agent_id": agent_id,
                    "task_id": "task-1",
                    "tool_id": None,
                    "config": {},
                    "metadata": {},
                }
            ],
            "edges": [],
            "entrypoint": "node-1",
            "task_definitions": [
                {
                    "id": "task-1",
                    "name": "Task One",
                    "description": "Do work",
                    "instructions": "Use the provided context",
                    "expected_output": "A concise result",
                    "agent_id": agent_id,
                    "tool_ids": [tool_id],
                    "depends_on_task_ids": [],
                    "input_schema": {},
                    "output_schema": {},
                    "human_approval_required": False,
                    "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
                    "metadata": {},
                }
            ],
            "agent_definitions": [
                self._agent_payload(agent_id=agent_id, tool_ids=[tool_id], model_profile_id=model_profile_id)],
            "tool_definitions": [self._tool_payload(tool_id=tool_id)],
            "allowed_runtime_adapter_ids": ["native", "crewai"],
            "default_runtime_adapter_id": "native",
            "versioning": {
                "version": "0.1.0",
                "revision": 1,
                "parent_version": None,
                "is_published": False,
                "labels": ["draft"],
            },
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
            "metadata": {},
        }

    def _minimal_workflow_for_tool_planning(self, *, task_description: str) -> WorkflowDefinition:
        agent = AgentDefinition(
            id="agent-planner",
            name="Planner Agent",
            instructions="Execute the assigned workflow task.",
        )
        task = TaskDefinition(
            id="task-planning",
            name="Run tool-assisted step",
            description=task_description,
            instructions=task_description,
            expected_output="Completed step output.",
            agent_id=agent.id,
        )
        node = WorkflowNodeDefinition(
            id="node-planning",
            name=task.name,
            node_type=NodeType.TASK,
            task_id=task.id,
        )
        return WorkflowDefinition(
            id="workflow-tool-planning",
            name="Tool Planning Workflow",
            description="Tests tool planning.",
            entrypoint=node.id,
            nodes=[node],
            task_definitions=[task],
            agent_definitions=[agent],
            metadata={"visible_to_main_agent": True, "mutable_by_main_agent": True},
        )

    def test_workflow_builder_assigns_existing_matching_tool_to_task(self):
        asyncio.run(
            self.context.tool_repo.save(
                ToolDefinition.model_validate(
                    {
                        **self._tool_payload(tool_id="tool-discord-send"),
                        "name": "send_discord_message",
                        "description": "Send a message to a Discord channel or webhook.",
                    }
                )
            )
        )
        workflow = self._minimal_workflow_for_tool_planning(
            task_description="Send the final news brief to a Discord channel webhook."
        )

        enriched = asyncio.run(
            WorkflowBuilderService(self.context).enrich_with_existing_tools(
                workflow=workflow,
                goal="Create a news workflow that sends the final brief to Discord.",
            )
        )

        self.assertEqual(enriched.task_definitions[0].tool_ids, ["tool-discord-send"])
        self.assertEqual(enriched.agent_definitions[0].tool_ids, ["tool-discord-send"])
        self.assertFalse(enriched.metadata.get("tool_creation_required", False))
        self.assertEqual(
            enriched.metadata["tool_planning"]["task_tool_matches"][0]["source"],
            "existing_tool_match",
        )

    def test_workflow_builder_marks_missing_tool_creation_requirement(self):
        workflow = self._minimal_workflow_for_tool_planning(
            task_description="Send the final news brief to a Discord channel webhook."
        )

        enriched = asyncio.run(
            WorkflowBuilderService(self.context).enrich_with_existing_tools(
                workflow=workflow,
                goal="Create a news workflow that sends the final brief to Discord.",
            )
        )

        self.assertTrue(enriched.metadata["tool_creation_required"])
        recommendation = enriched.metadata["tool_creation_recommendation"]
        self.assertEqual(recommendation["recommended_agent"], "Coder Agent")
        self.assertEqual(recommendation["recommended_existing_tool_id"], "agency.command.run")
        self.assertEqual(recommendation["suggested_tools"][0]["capability"], "Discord delivery")

    def test_workflow_builder_repair_accepts_bare_workflow_response(self):
        workflow = WorkflowDefinition.model_validate(self._workflow_payload())
        repaired_payload = workflow.model_copy(update={"description": "Repaired description"}).model_dump(mode="json")

        with patch.object(WorkflowBuilderService, "_generate_structured", return_value=repaired_payload):
            repaired = asyncio.run(
                WorkflowBuilderService(self.context).repair_workflow_definition(
                    workflow=workflow,
                    validation_errors=["entrypoint is invalid"],
                    goal="Repair the workflow.",
                    model_profile_id="profile-builder",
                )
            )

        self.assertEqual(repaired.description, "Repaired description")

    def test_workflow_builder_update_accepts_bare_workflow_response(self):
        workflow = WorkflowDefinition.model_validate(self._workflow_payload())
        updated_payload = workflow.model_copy(update={"description": "Updated description"}).model_dump(mode="json")

        with patch.object(WorkflowBuilderService, "_generate_structured", return_value=updated_payload):
            updated = asyncio.run(
                WorkflowBuilderService(self.context).update_workflow_definition(
                    workflow=workflow,
                    goal="Update the description.",
                    model_profile_id="profile-builder",
                )
            )

        self.assertEqual(updated.description, "Updated description")

    def test_workflow_builder_does_not_treat_storage_prefix_as_code_fix_request(self):
        service = WorkflowBuilderService(self.context)
        workflow = WorkflowDefinition(
            id="workflow-read-only-learning",
            name="Agency Learning Workflow",
            description="Teach from Agency repo improvement ideas without modifying code.",
            entrypoint="node-voice",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-voice",
                    name="Generate voice lesson",
                    node_type=NodeType.TASK,
                    task_id="task-voice",
                )
            ],
            task_definitions=[
                TaskDefinition(
                    id="task-voice",
                    name="Generate voice lesson",
                    description="Generate the read-only lesson narration.",
                    instructions='Use storage_key_prefix="media/learning" for the voice artifact.',
                    expected_output="A voice artifact.",
                    agent_id="agent-learning",
                    tool_ids=["agency.voice.generate"],
                )
            ],
            agent_definitions=[
                AgentDefinition(
                    id="agent-learning",
                    name="Learning Agent",
                    instructions="Explain source code without changing it.",
                    tool_ids=["agency.voice.generate"],
                )
            ],
        )

        goal = (
            "Keep this read-only: explain the Agency repo code and add robust Discord notifications, "
            "but do not modify code."
        )
        enhanced = service._ensure_recommendation_to_code_pipeline(workflow=workflow, goal=goal)

        self.assertIs(enhanced, workflow)
        self.assertFalse(service._goal_requests_recommendation_to_code(goal=goal, workflow=workflow))
        self.assertEqual(enhanced.task_definitions[0].agent_id, "agent-learning")
        self.assertNotIn("agency.command.run", enhanced.task_definitions[0].tool_ids)
        self.assertNotIn("Coder Agent", [agent.name for agent in enhanced.agent_definitions])

    def test_workflow_builder_keeps_voice_task_out_of_explicit_code_pipeline(self):
        service = WorkflowBuilderService(self.context)
        workflow = WorkflowDefinition(
            id="workflow-explicit-code",
            name="Agency Improvement Workflow",
            description="Review recommendation ideas for the Agency repository.",
            entrypoint="node-voice",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-voice",
                    name="Generate voice lesson",
                    node_type=NodeType.TASK,
                    task_id="task-voice",
                )
            ],
            task_definitions=[
                TaskDefinition(
                    id="task-voice",
                    name="Generate voice lesson",
                    description="Generate narration for the recommendation brief.",
                    instructions='Use storage_key_prefix="media/learning" for the voice artifact.',
                    expected_output="A voice artifact.",
                    agent_id="agent-learning",
                    tool_ids=["agency.voice.generate"],
                )
            ],
            agent_definitions=[
                AgentDefinition(
                    id="agent-learning",
                    name="Learning Agent",
                    instructions="Explain the recommendation clearly.",
                    tool_ids=["agency.voice.generate"],
                )
            ],
        )

        enhanced = service._ensure_recommendation_to_code_pipeline(
            workflow=workflow,
            goal="Implement the approved Agency repository recommendation as code, then verify it with tests.",
        )

        voice_task = next(task for task in enhanced.task_definitions if task.id == "task-voice")
        self.assertEqual(voice_task.agent_id, "agent-learning")
        self.assertEqual(voice_task.tool_ids, ["agency.voice.generate"])
        self.assertIn("Coder Agent", [agent.name for agent in enhanced.agent_definitions])
        self.assertTrue(any("implement" in task.name.lower() for task in enhanced.task_definitions))
        self.assertTrue(any("verify" in task.name.lower() for task in enhanced.task_definitions))

    def test_workflow_validation_accepts_allowlisted_read_only_repo_inspection(self):
        repo_inspect = next(tool for tool in builtin_tool_definitions() if tool.id == "agency.repo.inspect")
        asyncio.run(self.context.tool_repo.save(repo_inspect))
        workflow = WorkflowDefinition(
            id="workflow-read-only-repo-inspection",
            name="Read-only repo lesson",
            entrypoint="node-inspect",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-inspect",
                    name="Inspect source",
                    node_type=NodeType.TASK,
                    task_id="task-inspect",
                )
            ],
            task_definitions=[
                TaskDefinition(
                    id="task-inspect",
                    name="Inspect source",
                    description="Inspect a bounded source-code scope without modifying it.",
                    instructions="Read one bounded source-code scope.",
                    expected_output="A source-code lesson.",
                    agent_id="agent-inspect",
                    tool_ids=[repo_inspect.id],
                )
            ],
            agent_definitions=[
                AgentDefinition(
                    id="agent-inspect",
                    name="Source teacher",
                    instructions="Read source without modifying it.",
                    tool_ids=[repo_inspect.id],
                )
            ],
        )

        result = asyncio.run(WorkflowValidationService(self.context).validate(workflow))

        error_codes = {item["code"] for item in result.validation_errors}
        self.assertNotIn("tool.security.dangerous", error_codes)

    def test_workflow_builder_rewrite_routes(self):
        agent_response = self.client.post(
            "/workflow-builder/rewrite/agent",
            json={
                "agent": {
                    "name": "Research Agent",
                    "role": "research",
                    "instructions": "find things",
                    "backstory": "generalist",
                }
            },
        )
        self.assertEqual(agent_response.status_code, 200)
        self.assertEqual(agent_response.json()["data"]["role"], "Senior Research Strategist")

        task_response = self.client.post(
            "/workflow-builder/rewrite/task",
            json={
                "task": {
                    "name": "Draft memo",
                    "description": "write it",
                    "expected_output": "memo",
                }
            },
        )
        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(task_response.json()["data"]["name"], "Draft Decision Memo")

    def test_workflow_runtime_governance_controls_can_be_read_and_updated(self):
        create_response = self.client.post("/workflows", json=self._workflow_payload())
        self.assertEqual(create_response.status_code, 200)

        initial = self.client.get("/workflows/workflow-1/runtime-governance")
        self.assertEqual(initial.status_code, 200)
        initial_payload = initial.json()
        self.assertFalse(initial_payload["token_budget"]["configured"])
        self.assertFalse(initial_payload["context_compaction"]["persist_context_pack"])
        self.assertFalse(initial_payload["execution_policy"]["configured"])
        self.assertEqual(initial_payload["execution_policy"]["approval_mode"], "task_policy")
        self.assertEqual(initial_payload["operator_actions"]["update_controls"], "/workflows/workflow-1/runtime-governance")

        updated = self.client.patch(
            "/workflows/workflow-1/runtime-governance",
            json={
                "tokenBudget": {
                    "runTotalTokens": 1000,
                    "warnRatio": 0.5,
                    "hardRatio": 0.9,
                    "action": "compact_context",
                },
                "contextCompaction": {
                    "enabled": True,
                    "persistContextPack": True,
                    "preserveRecentMessages": 3,
                    "maxSummaryChars": 3000,
                },
                "executionPolicy": {
                    "maxRuntimeSeconds": 1800,
                    "maxRetries": 2,
                    "concurrencyLimit": 1,
                    "approvalMode": "all_tasks",
                },
            },
        )

        self.assertEqual(updated.status_code, 200)
        body = updated.json()
        runtime = body["runtime_governance"]
        self.assertTrue(runtime["token_budget"]["configured"])
        self.assertEqual(runtime["token_budget"]["run_total_tokens"], 1000)
        self.assertEqual(runtime["token_budget"]["action"], "compact_context")
        self.assertTrue(runtime["context_compaction"]["persist_context_pack"])
        self.assertEqual(runtime["context_compaction"]["persist_context_pack_source"], "workflow")
        self.assertEqual(runtime["context_compaction"]["preserve_recent_messages"], 3)
        self.assertTrue(runtime["execution_policy"]["configured"])
        self.assertEqual(runtime["execution_policy"]["max_runtime_seconds"], 1800)
        self.assertEqual(runtime["execution_policy"]["max_retries"], 2)
        self.assertEqual(runtime["execution_policy"]["concurrency_limit"], 1)
        self.assertEqual(runtime["execution_policy"]["approval_mode"], "all_tasks")
        self.assertEqual(runtime["execution_policy"]["effective_concurrency_limit"], 1)
        persisted = asyncio.run(self.context.workflow_repo.get("workflow-1"))
        governance = persisted.metadata["runtime_governance"]
        self.assertEqual(governance["token_budget"]["run_total_tokens"], 1000)
        self.assertTrue(governance["context_compaction"]["persist_context_pack"])
        self.assertEqual(persisted.max_runtime_seconds, 1800)
        self.assertEqual(persisted.max_retries, 2)
        self.assertEqual(persisted.concurrency_limit, 1)
        self.assertEqual(persisted.approval_mode, "all_tasks")

        detail = self.client.get("/workflows/workflow-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["runtime_governance"]["token_budget"]["run_total_tokens"], 1000)
        self.assertEqual(detail.json()["runtime_governance"]["execution_policy"]["approval_mode"], "all_tasks")

    def test_workflow_runtime_governance_rejects_invalid_token_budget_action(self):
        create_response = self.client.post("/workflows", json=self._workflow_payload())
        self.assertEqual(create_response.status_code, 200)

        response = self.client.patch(
            "/workflows/workflow-1/runtime-governance",
            json={"token_budget": {"action": "delete_everything"}},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Input should be", response.json()["detail"])

    def test_workflow_runtime_governance_clearing_compaction_fields_restores_defaults(self):
        create_response = self.client.post("/workflows", json=self._workflow_payload())
        self.assertEqual(create_response.status_code, 200)

        updated = self.client.patch(
            "/workflows/workflow-1/runtime-governance",
            json={
                "contextCompaction": {
                    "persistContextPack": True,
                    "preserveRecentMessages": 3,
                }
            },
        )
        self.assertEqual(updated.status_code, 200)

        cleared = self.client.patch(
            "/workflows/workflow-1/runtime-governance",
            json={
                "contextCompaction": {
                    "persistContextPack": None,
                    "preserveRecentMessages": None,
                }
            },
        )
        self.assertEqual(cleared.status_code, 200)
        payload = cleared.json()["runtime_governance"]["context_compaction"]
        self.assertFalse(payload["persist_context_pack"])
        self.assertEqual(payload["persist_context_pack_source"], "global_default")
        self.assertEqual(payload["preserve_recent_messages"], 1)

        persisted = asyncio.run(self.context.workflow_repo.get("workflow-1"))
        governance = persisted.metadata["runtime_governance"]
        self.assertNotIn("context_compaction", governance)

    def test_workflow_runtime_governance_rejects_unknown_fields(self):
        create_response = self.client.post("/workflows", json=self._workflow_payload())
        self.assertEqual(create_response.status_code, 200)

        response = self.client.patch(
            "/workflows/workflow-1/runtime-governance",
            json={"contextCompaction": {"unknownControl": True}},
        )

        self.assertEqual(response.status_code, 422)

    def test_workflow_steering_approval_can_be_requested_from_graph_target(self):
        create_response = self.client.post("/workflows", json=self._workflow_payload())
        self.assertEqual(create_response.status_code, 200)

        response = self.client.post(
            "/workflows/workflow-1/steering-approvals",
            json={
                "recommendedAction": "request_replan",
                "reason": "Selected from graph node.",
                "targetTaskId": "task-1",
                "operatorParameters": {
                    "instructions": "Replan validation steps without hiding the task graph.",
                },
                "metadata": {"source": "workflow_graph_node"},
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["workflow_id"], "workflow-1")
        self.assertEqual(body["approval"]["target_task_id"], "task-1")
        self.assertEqual(body["approval"]["recommended_action"], "request_replan")
        self.assertEqual(body["approval"]["status"], "approval_requested")
        self.assertEqual(body["approval"]["created_by"], "user-owner")
        self.assertEqual(body["approval"]["metadata"]["source"], "workflow_graph_node")
        self.assertIsNotNone(body["approval"]["approval_request_id"])
        self.assertEqual(body["approval"]["approval_request_id"], body["approval_request"]["id"])
        response_steering = body["workflow"]["metadata"]["main_agent_monitoring"]["steering_approvals"][0]
        self.assertEqual(response_steering["status"], "approval_requested")
        self.assertEqual(response_steering["approval_request_id"], body["approval_request"]["id"])

        persisted = asyncio.run(self.context.workflow_repo.get("workflow-1"))
        steering = persisted.metadata["main_agent_monitoring"]["steering_approvals"][0]
        self.assertEqual(steering["target_task_id"], "task-1")
        self.assertEqual(steering["status"], "approval_requested")
        self.assertEqual(steering["approval_request_id"], body["approval_request"]["id"])

    def test_workflow_builder_generate_routes(self):
        tasks_response = self.client.post(
            "/workflow-builder/drafts/generate",
            json={
                "draft_type": "tasks",
                "latest_instruction": "Create a launch plan",
                "conversation_history": "We need a concise rollout workflow.",
            },
        )
        self.assertEqual(tasks_response.status_code, 200)
        self.assertEqual(tasks_response.json()["tasks"][0]["name"], "Outline Launch Strategy")

        agents_response = self.client.post(
            "/workflow-builder/drafts/generate",
            json={
                "draft_type": "agents",
                "tasks": [
                    {"name": "Outline Launch Strategy", "description": "Create the plan", "expected_output": "A plan"}],
            },
        )
        self.assertEqual(agents_response.status_code, 200)
        self.assertEqual(agents_response.json()["agents"][0]["name"], "Launch Strategist")

        workflow_response = self.client.post(
            "/workflow-builder/drafts/generate",
            json={
                "draft_type": "workflow",
                "tasks": [
                    {"name": "Outline Launch Strategy", "description": "Create the plan", "expected_output": "A plan"}],
                "agents": [{"name": "Launch Strategist", "role": "strategist", "instructions": "shape the plan",
                            "backstory": "experienced"}],
            },
        )
        self.assertEqual(workflow_response.status_code, 200)
        self.assertEqual(workflow_response.json()["workflow"]["name"], "Launch Strategy Workflow")

    def test_agent_crud(self):
        payload = self._agent_payload()
        create = self.client.post("/agents", json=payload)
        self.assertEqual(create.status_code, 200)

        listing = self.client.get("/agents")
        self.assertEqual(len(listing.json()["items"]), 1)

        fetch = self.client.get("/agents/agent-1")
        self.assertEqual(fetch.status_code, 200)

        update = self.client.put("/agents/agent-1", json={"description": "Updated agent"})
        self.assertEqual(update.json()["description"], "Updated agent")

        delete = self.client.delete("/agents/agent-1")
        self.assertEqual(delete.status_code, 200)

    def test_builder_reuses_matching_global_agent_definitions(self):
        asyncio.run(
            self.context.agent_repo.create(
                AgentDefinition(
                    id="agent-launch-strategist",
                    name="Launch Strategist",
                    role="Plans the workflow approach",
                    description="Reusable strategist for launch planning workflows.",
                    instructions="Use the existing catalog strategist playbook.",
                    backstory="Catalog agent",
                    model_profile_id="profile-builder",
                )
            )
        )
        service = WorkflowBuilderService(self.context)

        workflow = asyncio.run(
            service.build_workflow_definition(
                goal="Create a workflow that drafts a concise product launch plan.",
                conversation_history="The user wants reusable launch planning.",
                model_profile_id="profile-builder",
                default_agent_model_profile_id="profile-builder",
            )
        )

        self.assertEqual(workflow.agent_definitions[0].id, "agent-launch-strategist")
        self.assertEqual(
            workflow.agent_definitions[0].instructions,
            "Use the existing catalog strategist playbook.",
        )
        self.assertEqual(
            workflow.agent_definitions[0].metadata["workflow_builder_reused_global_agent_id"],
            "agent-launch-strategist",
        )
        self.assertEqual(workflow.task_definitions[0].agent_id, "agent-launch-strategist")

    def test_tool_crud(self):
        payload = self._tool_payload()
        self.assertEqual(self.client.post("/tools", json=payload).status_code, 200)
        tool_ids = {tool["id"] for tool in self.client.get("/tools").json()["items"]}
        self.assertIn("tool-1", tool_ids)
        self.assertEqual(self.client.get("/tools/tool-1").status_code, 200)
        self.assertEqual(self.client.put("/tools/tool-1", json={"description": "Updated"}).json()["description"],
                         "Updated")
        self.assertEqual(self.client.delete("/tools/tool-1").status_code, 200)

    def test_model_provider_and_profile_crud(self):
        provider = {
            "id": "provider-1",
            "name": "Local OpenAI",
            "provider_type": "openai_compatible",
            "description": "Local endpoint",
            "endpoint": {"base_url": "http://localhost:1234/v1", "api_version": None, "region": None, "headers": {}},
            "capabilities": ["chat"],
            "default_headers": {},
            "secret_references": [],
            "config": {},
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }
        profile = {
            "id": "profile-1",
            "name": "Local Chat",
            "provider": "provider-1",
            "model": "local-model",
            "description": "Local profile",
            "base_url": "http://localhost:1234/v1",
            "api_key_ref": None,
            "temperature": 0.1,
            "max_tokens": 128,
            "context_window": 8192,
            "top_p": None,
            "supports_tools": True,
            "supports_structured_output": True,
            "supports_vision": False,
            "supports_streaming": True,
            "parameters": {},
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }

        self.assertEqual(self.client.post("/model-providers", json=provider).status_code, 200)
        self.assertEqual(self.client.post("/model-profiles", json=profile).status_code, 200)
        self.assertEqual(self.client.get("/model-providers/provider-1").status_code, 200)
        self.assertEqual(self.client.get("/model-profiles/profile-1").status_code, 200)
        self.assertEqual(self.client.put("/model-profiles/profile-1", json={"temperature": 0.3}).json()["temperature"],
                         0.3)
        self.assertEqual(self.client.delete("/model-providers/provider-1").status_code, 200)

    def test_model_profile_crud_redacts_api_key_ref_without_overwriting_it(self):
        profile = {
            "id": "profile-secret",
            "name": "OpenAI Chat",
            "provider": "openai",
            "model": "gpt-test",
            "base_url": "https://api.openai.com/v1",
            "api_key_ref": "sk-test-secret",
        }

        created = self.client.post("/model-profiles", json=profile).json()
        self.assertEqual(created["api_key_ref"], "[REDACTED]")

        fetched = self.client.get("/model-profiles/profile-secret").json()
        self.assertEqual(fetched["api_key_ref"], "[REDACTED]")

        updated = self.client.put(
            "/model-profiles/profile-secret",
            json={"api_key_ref": "[REDACTED]", "temperature": 0.5},
        ).json()
        self.assertEqual(updated["api_key_ref"], "[REDACTED]")
        self.assertEqual(updated["temperature"], 0.5)

        stored = asyncio.run(self.context.model_profile_repo.get("profile-secret"))
        self.assertEqual(stored.api_key_ref, "sk-test-secret")

    def test_schedule_crud(self):
        payload = {
            "id": "schedule-1",
            "name": "Nightly",
            "workflow_id": "workflow-1",
            "runtime_adapter_id": "native",
            "schedule_type": "cron",
            "cron": "0 2 * * *",
            "interval_seconds": None,
            "timezone": "UTC",
            "enabled": True,
            "input_payload": {},
            "next_run_at": None,
            "metadata": {},
        }
        self.assertEqual(self.client.post("/schedules", json=payload).status_code, 200)
        self.assertEqual(self.client.get("/schedules/schedule-1").status_code, 200)
        self.assertEqual(self.client.put("/schedules/schedule-1", json={"enabled": False}).json()["enabled"], False)
        self.assertEqual(self.client.delete("/schedules/schedule-1").status_code, 200)

    def test_runtime_adapter_crud_and_protection(self):
        listing = self.client.get("/runtime-adapters")
        self.assertEqual(listing.status_code, 200)
        ids = {item["id"] for item in listing.json()["items"]}
        self.assertIn("native", ids)
        self.assertIn("crewai", ids)

        payload = {
            "id": "adapter-custom",
            "name": "Custom Adapter",
            "adapter_type": "other",
            "description": "Custom runtime",
            "version": "1.0.0",
            "capabilities": ["custom"],
            "config_schema": {},
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }
        self.assertEqual(self.client.post("/runtime-adapters", json=payload).status_code, 403)
        admin_headers = {
            "x-agency-user-id": "user-runtime-admin",
            "x-agency-user-email": "runtime-admin@example.com",
        }
        asyncio.run(
            self.context.user_repo.create(
                UserDefinition(
                    id="user-runtime-admin",
                    email="runtime-admin@example.com",
                    display_name="Runtime Admin",
                    roles=["admin"],
                )
            )
        )
        self.assertEqual(
            self.client.post("/runtime-adapters", headers=admin_headers, json=payload).status_code,
            200,
        )
        self.assertEqual(
            self.client.put(
                "/runtime-adapters/adapter-custom",
                headers=admin_headers,
                json={"description": "Updated"},
            ).json()["description"],
            "Updated")
        self.assertEqual(
            self.client.delete("/runtime-adapters/adapter-custom", headers=admin_headers).status_code,
            200,
        )
        protected = self.client.delete("/runtime-adapters/native", headers=admin_headers)
        self.assertEqual(protected.status_code, 400)

    def test_workflow_crud_publish_and_validate(self):
        provider = {
            "id": "provider-1",
            "name": "Local OpenAI",
            "provider_type": "openai_compatible",
            "description": "Local endpoint",
            "endpoint": {"base_url": "http://localhost:1234/v1", "api_version": None, "region": None, "headers": {}},
            "capabilities": ["chat"],
            "default_headers": {},
            "secret_references": [],
            "config": {},
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }
        profile = {
            "id": "profile-1",
            "name": "No Tools Model",
            "provider": "provider-1",
            "model": "local-model",
            "description": "Local profile",
            "base_url": "http://localhost:1234/v1",
            "api_key_ref": None,
            "temperature": 0.1,
            "max_tokens": 128,
            "context_window": 8192,
            "top_p": None,
            "supports_tools": False,
            "supports_structured_output": False,
            "supports_vision": False,
            "supports_streaming": True,
            "parameters": {},
            "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
        }
        self.client.post("/model-providers", json=provider)
        self.client.post("/model-profiles", json=profile)
        self.client.post("/agents", json=self._agent_payload(agent_id="agent-1", tool_ids=["tool-1"],
                                                             model_profile_id="profile-1"))
        self.client.post("/tools", json=self._tool_payload(tool_id="tool-1", dangerous=True))

        workflow = self._workflow_payload(model_profile_id="profile-1")
        workflow["metadata"] = {
            **workflow.get("metadata", {}),
            "created_by": "user-owner",
            "owner_ids": ["user-owner"],
        }
        workflow["tool_definitions"] = [self._tool_payload(tool_id="tool-1", dangerous=True)]
        create = self.client.post("/workflows", json=workflow)
        self.assertEqual(create.status_code, 200)

        listing = self.client.get("/workflows")
        self.assertEqual(len(listing.json()["items"]), 1)

        update = self.client.put(
            "/workflows/workflow-1",
            headers=self.owner_headers,
            json={"description": "Updated workflow"},
        )
        self.assertEqual(update.json()["description"], "Updated workflow")

        publish = self.client.post(
            "/workflows/workflow-1/publish",
            headers=self.owner_headers,
            json={"version": "0.2.0"},
        )
        self.assertTrue(publish.json()["versioning"]["is_published"])
        self.assertEqual(publish.json()["versioning"]["version"], "0.2.0")

        versions = self.client.get("/workflows/workflow-1/versions")
        self.assertEqual(versions.status_code, 200)
        version_items = versions.json()["items"]
        self.assertEqual([item["revision"] for item in version_items], [2, 1])
        self.assertTrue(version_items[0]["is_current"])
        self.assertEqual(version_items[0]["version"], "0.2.0")
        self.assertEqual(version_items[0]["status"], "published")
        self.assertFalse(version_items[1]["is_current"])
        self.assertEqual(version_items[1]["version"], "0.1.0")
        self.assertEqual(version_items[1]["status"], "draft")

        version_one = self.client.get("/workflows/workflow-1/versions/1")
        self.assertEqual(version_one.status_code, 200)
        self.assertEqual(version_one.json()["definition"]["versioning"]["version"], "0.1.0")
        self.assertFalse(version_one.json()["is_current"])

        unpublish = self.client.post(
            "/workflows/workflow-1/unpublish",
            headers=self.owner_headers,
        )
        self.assertEqual(unpublish.status_code, 200)
        self.assertFalse(unpublish.json()["versioning"]["is_published"])
        self.assertEqual(unpublish.json()["versioning"]["version"], "0.2.0")

        versions = self.client.get("/workflows/workflow-1/versions")
        self.assertEqual(versions.status_code, 200)
        version_items = versions.json()["items"]
        self.assertEqual([item["revision"] for item in version_items], [3, 2, 1])
        self.assertTrue(version_items[0]["is_current"])
        self.assertEqual(version_items[0]["status"], "draft")
        self.assertEqual(version_items[1]["status"], "published")

        missing_version = self.client.get("/workflows/workflow-1/versions/404")
        self.assertEqual(missing_version.status_code, 404)

        validate = self.client.post("/workflows/validate", json=workflow)
        self.assertEqual(validate.status_code, 200)
        payload = validate.json()
        self.assertEqual(payload["entrypoint"] if "entrypoint" in payload else workflow["entrypoint"],
                         workflow["entrypoint"])
        self.assertTrue(payload["available_tools"])
        self.assertTrue(payload["available_agents"])
        self.assertTrue(payload["compatible_runtime_adapters"])
        error_codes = {item["code"] for item in payload["validation_errors"]}
        self.assertIn("agent.model_profile.incompatible", error_codes)
        self.assertIn("tool.security.dangerous", error_codes)

        detail = self.client.get("/workflows/workflow-1")
        self.assertEqual(detail.status_code, 200)
        round_trip_validate = self.client.post("/workflows/validate", json=detail.json())
        self.assertEqual(round_trip_validate.status_code, 200)

        self.assertEqual(self.client.delete("/workflows/workflow-1", headers=self.owner_headers).status_code, 200)

    def test_workflow_shared_memory_controls(self):
        workflow = self._workflow_payload()
        create = self.client.post("/workflows", json=workflow)
        self.assertEqual(create.status_code, 200)

        initial = self.client.get("/workflows/workflow-1/shared-memory", headers=self.owner_headers)
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["enabled"])

        update = self.client.patch(
            "/workflows/workflow-1/shared-memory",
            headers=self.owner_headers,
            json={
                "enabled": True,
                "apply_to_agents": True,
                "limit_per_layer": {
                    "decisions": 2,
                    "commitments": 3,
                    "unknown": 99,
                },
            },
        )
        self.assertEqual(update.status_code, 200)
        payload = update.json()
        self.assertTrue(payload["shared_memory"]["enabled"])
        self.assertEqual(payload["shared_memory"]["limit_per_layer"], {"decisions": 2, "commitments": 3})
        self.assertTrue(payload["shared_memory"]["agent_states"][0]["enabled"])
        self.assertEqual(payload["shared_memory"]["agent_states"][0]["scope"], "workflow")
        self.assertTrue(payload["workflow"]["metadata"]["shared_memory"]["enabled"])
        self.assertTrue(payload["workflow"]["agent_definitions"][0]["memory"]["enabled"])

    def test_workflow_validation_ignores_unreferenced_dangerous_tools(self):
        self.client.post("/tools", json=self._tool_payload(tool_id="unused-dangerous-tool", dangerous=True))
        workflow = self._workflow_payload(tool_id="safe-tool")
        workflow["task_definitions"][0]["tool_ids"] = []
        workflow["agent_definitions"][0]["tool_ids"] = []
        workflow["tool_definitions"] = []

        validate = self.client.post("/workflows/validate", json=workflow)
        self.assertEqual(validate.status_code, 200)
        error_codes = {item["code"] for item in validate.json()["validation_errors"]}
        self.assertNotIn("tool.security.dangerous", error_codes)

    def test_workflow_validation_rejects_connector_tool_without_binding(self):
        workflow = self._workflow_payload(tool_id="connector-tool")
        workflow["tool_definitions"] = [self._tool_payload(tool_id="connector-tool")]
        workflow["tool_definitions"][0]["tags"] = ["connector"]
        workflow["tool_definitions"][0]["implementation"]["config"] = {"provider": "slack"}

        validate = self.client.post("/workflows/validate", json=workflow)

        self.assertEqual(validate.status_code, 200)
        errors = validate.json()["validation_errors"]
        error_codes = {item["code"] for item in errors}
        self.assertIn("tool.connector_binding.missing", error_codes)
        self.assertIn("requires a connector binding", errors[-1]["message"])

    def test_workflow_validation_accepts_workflow_connector_default_binding(self):
        workflow = self._workflow_payload(tool_id="connector-tool")
        workflow["tool_definitions"] = [self._tool_payload(tool_id="connector-tool")]
        workflow["tool_definitions"][0]["tags"] = ["connector"]
        workflow["tool_definitions"][0]["implementation"]["config"] = {"provider": "slack"}
        workflow["metadata"] = {
            **workflow.get("metadata", {}),
            "connector_bindings": [
                {
                    "provider": "slack",
                    "credential_id": "credential-slack-support",
                    "purpose": "support_delivery",
                    "target_scope": {"channel_id": "C123"},
                }
            ],
        }

        validate = self.client.post("/workflows/validate", json=workflow)

        self.assertEqual(validate.status_code, 200)
        error_codes = {item["code"] for item in validate.json()["validation_errors"]}
        self.assertNotIn("tool.connector_binding.missing", error_codes)

    def test_workflow_validation_rejects_dependency_without_executable_node(self):
        workflow = self._workflow_payload()
        workflow["task_definitions"].append(
            {
                "id": "task-unmapped-dependency",
                "name": "Unmapped dependency",
                "description": "This task was accidentally omitted from the graph.",
                "agent_id": workflow["agent_definitions"][0]["id"],
                "tool_ids": [],
            }
        )
        workflow["task_definitions"][0]["depends_on_task_ids"] = ["task-unmapped-dependency"]

        result = asyncio.run(
            WorkflowValidationService(self.context).validate(WorkflowDefinition.model_validate(workflow))
        )

        error_codes = {item["code"] for item in result.validation_errors}
        self.assertIn("task.dependency.not_executable", error_codes)

    def test_execution_create_list_and_get(self):
        workflow = self._workflow_payload()
        asyncio.run(
            self.context.memory_repo.create(
                MemoryRecord(
                    id="context-pack-exec-1",
                    scope="user",
                    created_by_user_id="user-owner",
                    content="Selected compact context for this workflow execution.",
                    summary="Selected execution context.",
                    source="compact_tool",
                    memory_type="context_pack",
                    tags=["context_pack", "user", "handoff"],
                    metadata={"mode": "handoff"},
                )
            )
        )
        create = self.client.post(
            "/executions",
            json={
                "workflowId": "workflow-exec-1",
                "input": {"topic": "hello"},
                "trigger": {"created_by": "tester"},
                "contextPackId": "context-pack-exec-1",
                "runtimeAdapterId": "native",
                "workflow_definition": {**workflow, "id": "workflow-exec-1"},
                "model_profiles": [],
            },
        )
        self.assertEqual(create.status_code, 200)
        created_payload = create.json()
        execution_id = created_payload["id"]
        self.assertEqual(created_payload["input_payload"]["context_pack_id"], "context-pack-exec-1")
        self.assertEqual(created_payload["trigger_payload"]["context_pack_id"], "context-pack-exec-1")
        self.assertEqual(created_payload["trigger_payload"]["context_pack"]["summary"], "Selected execution context.")

        listing = self.client.get("/executions")
        self.assertEqual(len(listing.json()["items"]), 1)

        update = self.client.put(f"/executions/{execution_id}", json={"metadata": {"note": "draft"}})
        self.assertEqual(update.status_code, 200)
        self.assertEqual(update.json()["metadata"]["note"], "draft")

        fetch = self.client.get(f"/executions/{execution_id}")
        self.assertEqual(fetch.status_code, 200)
        self.assertEqual(fetch.json()["execution"]["workflow_id"], "workflow-exec-1")

        events = self.client.get(f"/executions/{execution_id}/events")
        self.assertEqual(events.status_code, 200)
        self.assertGreaterEqual(len(events.json()["items"]), 1)

        asyncio.run(
            self.context.execution_store.save_artifact(
                ExecutionArtifact(
                    execution_id=execution_id,
                    artifact_type="json",
                    name="result.json",
                    content_json={"ok": True},
                    mime_type="application/json",
                )
            )
        )
        artifacts = self.client.get(f"/executions/{execution_id}/artifacts")
        self.assertEqual(artifacts.status_code, 200)
        self.assertEqual(len(artifacts.json()["items"]), 1)


if __name__ == "__main__":
    unittest.main()
