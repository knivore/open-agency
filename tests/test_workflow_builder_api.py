from __future__ import annotations

import asyncio
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.routes import create_api_router
from app.domain import ExecutionArtifact, ModelProfileDefinition, UserDefinition
from app.llm.base import ModelResponse


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
        self.assertEqual(self.client.post("/runtime-adapters", json=payload).status_code, 200)
        self.assertEqual(
            self.client.put("/runtime-adapters/adapter-custom", json={"description": "Updated"}).json()["description"],
            "Updated")
        self.assertEqual(self.client.delete("/runtime-adapters/adapter-custom").status_code, 200)
        protected = self.client.delete("/runtime-adapters/native")
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

    def test_execution_create_list_and_get(self):
        workflow = self._workflow_payload()
        create = self.client.post(
            "/executions",
            json={
                "workflowId": "workflow-exec-1",
                "input": {"topic": "hello"},
                "trigger": {"created_by": "tester"},
                "runtimeAdapterId": "native",
                "workflow_definition": {**workflow, "id": "workflow-exec-1"},
                "model_profiles": [],
            },
        )
        self.assertEqual(create.status_code, 200)
        execution_id = create.json()["id"]

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
