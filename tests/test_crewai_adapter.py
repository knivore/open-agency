from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.domain import AgentDefinition, Execution, ExecutionEventType, ExecutionStatus, FrameworkHints, \
    ModelProfileDefinition, TaskDefinition, WorkflowDefinition, WorkflowNodeDefinition
from app.llm.base import ModelResponse
from app.runtime.adapters import CrewAIRuntimeAdapter, NativeRuntimeAdapter
from app.runtime.adapters.crewai import CrewAIRuntimeAdapter as CrewAIImportSurface
from app.runtime.adapters.crewai.errors import CrewAIUnsupportedOperationError, CrewAIUnavailableError
from app.runtime.adapters.crewai.llm_bridge import AgencyModelClientLLM
from app.runtime.adapters.crewai.mapper import agent_definition_to_crewai_config, task_definition_to_crewai_config, \
    workflow_to_crewai_config
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.state import InMemoryExecutionStore, InMemoryModelProfileRepository, InMemoryWorkflowRepository
from app.runtime.registry import RuntimeAdapterRegistry


def _simple_workflow() -> WorkflowDefinition:
    agent = AgentDefinition(
        id="agent-1",
        name="Research Agent",
        role="Researcher",
        instructions="Answer the question",
        tool_ids=["agency.http.request"],
        framework_hints=FrameworkHints(adapter_config={"verbose": True, "llm": "gpt-4o-mini"}),
    )
    task = TaskDefinition(
        id="task-1",
        name="Research Task",
        description="Investigate the prompt",
        expected_output="A concise answer",
        agent_id=agent.id,
        tool_ids=["agency.http.request"],
    )
    node = WorkflowNodeDefinition(
        id="node-1",
        name="Task Node",
        node_type="task",
        agent_id=agent.id,
        task_id=task.id,
    )
    return WorkflowDefinition(
        id="workflow-1",
        name="Crew Workflow",
        entrypoint=node.id,
        nodes=[node],
        task_definitions=[task],
        agent_definitions=[agent],
        allowed_runtime_adapter_ids=["native", "crewai"],
        default_runtime_adapter_id="crewai",
    )


class CrewAIAdapterStructureTests(unittest.TestCase):
    def test_public_import_surface_points_to_new_package(self):
        self.assertIs(CrewAIImportSurface, CrewAIRuntimeAdapter)

    def test_registry_registers_exactly_one_crewai_adapter(self):
        context = create_test_api_context()
        names = context.runtime_registry.registered_adapter_names()
        self.assertEqual(names.count("crewai"), 1)
        self.assertIn("native", names)

    def test_native_adapter_has_no_crewai_dependency(self):
        source = Path("app/runtime/adapters/native_adapter.py").read_text(encoding="utf-8")
        self.assertNotIn("crewai", source)


class CrewAIAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_reports_unavailable_gracefully(self):
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=InMemoryWorkflowRepository(),
            model_profile_repository=InMemoryModelProfileRepository(),
            execution_store=InMemoryExecutionStore(),
        )
        workflow = _simple_workflow()
        with patch("app.runtime.adapters.crewai.availability.is_crewai_installed", return_value=False):
            status = adapter.get_status()
            self.assertFalse(status.available)
            self.assertFalse(await adapter.supports(workflow))
            with self.assertRaises(CrewAIUnavailableError):
                await adapter.start_execution("missing")

    async def test_pause_resume_cancel_are_clearly_unsupported(self):
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=InMemoryWorkflowRepository(),
            model_profile_repository=InMemoryModelProfileRepository(),
            execution_store=InMemoryExecutionStore(),
        )
        with self.assertRaises(CrewAIUnsupportedOperationError):
            await adapter.pause_execution("exec-1")
        with self.assertRaises(CrewAIUnsupportedOperationError):
            await adapter.resume_execution("exec-1")
        with self.assertRaises(CrewAIUnsupportedOperationError):
            await adapter.cancel_execution("exec-1")


class CrewAIMapperTests(unittest.TestCase):
    def test_mapper_builds_config_from_canonical_workflow(self):
        workflow = _simple_workflow()
        config = workflow_to_crewai_config(workflow, default_model="gpt-4o-mini")
        self.assertEqual(config["name"], "Crew Workflow")
        self.assertEqual(config["agents"][0]["agent_id"], "agent-1")
        self.assertEqual(config["tasks"][0]["task_id"], "task-1")
        self.assertEqual(config["tasks"][0]["agent_id"], "agent-1")

    def test_mapper_builds_agent_and_task_configs(self):
        workflow = _simple_workflow()
        agent = workflow.agent_definitions[0]
        task = workflow.task_definitions[0]
        node = workflow.nodes[0]
        agent_config = agent_definition_to_crewai_config(agent, default_model="gpt-4o-mini")
        task_config = task_definition_to_crewai_config(task, node, {node.id: node})
        self.assertEqual(agent_config["name"], "Research Agent")
        self.assertEqual(agent_config["llm"], "gpt-4o-mini")
        self.assertEqual(task_config["task_id"], "task-1")


class CrewAIRuntimeOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_model_profile_override_replaces_saved_profile(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        saved_profile = ModelProfileDefinition(
            id="profile-saved",
            name="Saved Profile",
            provider="openai",
            model="gpt-4o-mini",
        )
        runtime_profile = ModelProfileDefinition(
            id="profile-runtime",
            name="Runtime Profile",
            provider="ollama",
            model="llama3.2",
            base_url="http://localhost:11434",
        )
        await profile_repo.save_profile(saved_profile)
        await profile_repo.save_profile(runtime_profile)
        workflow = _simple_workflow()
        workflow.agent_definitions[0].model_profile_id = saved_profile.id
        workflow.agent_definitions[0].metadata["runtime_config"] = {
            "model_profile_id": runtime_profile.id,
        }

        profiles = await adapter._resolve_model_profiles(workflow)

        self.assertEqual(profiles["agent-1"].id, runtime_profile.id)
        self.assertEqual(profiles["agent-1"].provider, "ollama")

    async def test_runtime_llm_override_builds_transient_profile(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        base_profile = ModelProfileDefinition(
            id="profile-saved",
            name="Saved Profile",
            provider="openai",
            model="gpt-4o-mini",
            temperature=0.4,
            max_tokens=2048,
        )
        await profile_repo.save_profile(base_profile)
        workflow = _simple_workflow()
        workflow.agent_definitions[0].model_profile_id = base_profile.id
        workflow.agent_definitions[0].metadata["runtime_config"] = {
            "llm_override": {
                "provider": "openai_compatible",
                "model": "openai/llama3.2",
                "base_url": "http://localhost:11434/v1",
                "api_key": "ollama",
            }
        }

        profiles = await adapter._resolve_model_profiles(workflow)

        profile = profiles["agent-1"]
        self.assertEqual(profile.id, "runtime-override-agent-1")
        self.assertEqual(profile.provider, "openai_compatible")
        self.assertEqual(profile.model, "openai/llama3.2")
        self.assertEqual(profile.base_url, "http://localhost:11434/v1")
        self.assertEqual(profile.api_key_ref, "ollama")
        self.assertEqual(profile.temperature, 0.4)


class CrewAIExecutionEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_execution_passes_runtime_llm_override_to_crewai_runner(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        workflow = _simple_workflow()
        workflow.agent_definitions[0].metadata["runtime_config"] = {
            "llm_override": {
                "provider": "openai_compatible",
                "model": "openai/llama3.2",
                "base_url": "http://host.docker.internal:11434/v1",
                "api_key": "ollama",
            }
        }
        await workflow_repo.save_workflow(workflow)
        execution = Execution(
            id="exec-runtime-override",
            workflow_id=workflow.id,
            runtime_adapter="crewai",
            status=ExecutionStatus.CREATED,
            input_json={"question": "Hello"},
        )
        await adapter.prepare_execution(execution)

        async def fake_to_thread(func, workflow_arg, inputs, queue, process_id, run_by, *, default_model,
                                 model_profiles, model_provider_registry, model_event_loop):
            self.assertEqual(workflow_arg.id, workflow.id)
            self.assertEqual(default_model, "openai/llama3.2")
            self.assertIs(model_event_loop, asyncio.get_running_loop())
            profile = model_profiles["agent-1"]
            self.assertEqual(profile.provider, "openai_compatible")
            self.assertEqual(profile.model, "openai/llama3.2")
            self.assertEqual(profile.base_url, "http://host.docker.internal:11434/v1")
            self.assertEqual(profile.api_key_ref, "ollama")
            queue.put("Final answer")
            return "Final answer"

        with (
            patch("app.runtime.adapters.crewai.adapter.asyncio.to_thread", new=AsyncMock(side_effect=fake_to_thread)),
            patch("app.runtime.adapters.crewai.availability.is_crewai_installed", return_value=True),
        ):
            result = await adapter.start_execution(execution.id)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)

    async def test_mocked_execution_replays_internal_events(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        workflow = _simple_workflow()
        await workflow_repo.save_workflow(workflow)
        execution = Execution(
            id="exec-1",
            workflow_id=workflow.id,
            runtime_adapter="crewai",
            status=ExecutionStatus.CREATED,
            input_json={"question": "Hello"},
        )
        await adapter.prepare_execution(execution)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "exec-1.json"
            log_path.write_text(
                json.dumps(
                    [
                        {"agent_name": "Research Agent", "thought": "Need a tool", "tool": "agency.http.request",
                         "tool_input": {"url": "https://example.test"}, "result": "done"},
                        {
                            "agent_name": "Research Agent",
                            "output": "Final answer",
                            "thought": "Failed to parse LLM response",
                        },
                    ]
                ),
                encoding="utf-8",
            )

            async def fake_to_thread(func, workflow_arg, inputs, queue, process_id, run_by, *, default_model,
                                     model_profiles, model_provider_registry, model_event_loop):
                self.assertEqual(workflow_arg.id, workflow.id)
                self.assertEqual(default_model, "gpt-4o-mini")
                self.assertEqual(model_profiles, {})
                self.assertIs(model_event_loop, asyncio.get_running_loop())
                queue.put("Final answer")
                return "Final answer"

            with (
                patch("app.runtime.adapters.crewai.adapter.asyncio.to_thread",
                      new=AsyncMock(side_effect=fake_to_thread)),
                patch.object(adapter, "_initialize_log_file", return_value=str(log_path)),
                patch("app.runtime.adapters.crewai.availability.is_crewai_installed", return_value=True),
            ):
                result = await adapter.start_execution(execution.id)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        events = await execution_store.list_events(execution.id)
        event_types = [event.event_type for event in events]
        self.assertIn(ExecutionEventType.EXECUTION_STARTED, event_types)
        self.assertIn(ExecutionEventType.TOOL_CALL_STARTED, event_types)
        self.assertIn(ExecutionEventType.TOOL_CALL_COMPLETED, event_types)
        self.assertIn(ExecutionEventType.LLM_REQUEST_CREATED, event_types)
        self.assertIn(ExecutionEventType.LLM_RESPONSE_CREATED, event_types)
        self.assertIn(ExecutionEventType.ARTIFACT_CREATED, event_types)
        self.assertIn(ExecutionEventType.EXECUTION_COMPLETED, event_types)
        llm_response = next(event for event in events if event.event_type == ExecutionEventType.LLM_RESPONSE_CREATED)
        self.assertIsNone(llm_response.payload["thought"])
        self.assertTrue(llm_response.payload["thought_parse_error"])
        artifacts = await execution_store.list_artifacts(execution.id)
        final_artifact = next(artifact for artifact in artifacts if artifact.name == "final_output.txt")
        self.assertEqual(final_artifact.content_text, "Final answer")
        self.assertEqual(final_artifact.size_bytes, len("Final answer".encode("utf-8")))

    async def test_mocked_execution_streams_internal_events_before_completion(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        adapter = CrewAIRuntimeAdapter(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        workflow = _simple_workflow()
        await workflow_repo.save_workflow(workflow)
        execution = Execution(
            id="exec-2",
            workflow_id=workflow.id,
            runtime_adapter="crewai",
            status=ExecutionStatus.CREATED,
            input_json={"question": "Hello"},
        )
        await adapter.prepare_execution(execution)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "exec-2.json"
            log_path.write_text("[]", encoding="utf-8")

            async def fake_to_thread(func, workflow_arg, inputs, queue, process_id, run_by, *, default_model,
                                     model_profiles, model_provider_registry, model_event_loop):
                self.assertEqual(workflow_arg.id, workflow.id)
                self.assertIs(model_event_loop, asyncio.get_running_loop())
                log_path.write_text(
                    json.dumps(
                        [
                            {
                                "agent_name": "Research Agent",
                                "thought": "Need a tool",
                                "tool": "agency.http.request",
                                "tool_input": {"url": "https://example.test"},
                                "result": "done",
                            }
                        ]
                    ),
                    encoding="utf-8",
                )
                for _ in range(30):
                    events = await execution_store.list_events(execution.id)
                    if any(event.event_type == ExecutionEventType.TOOL_CALL_STARTED for event in events):
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(
                    any(event.event_type == ExecutionEventType.TOOL_CALL_STARTED for event in
                        await execution_store.list_events(execution.id))
                )
                log_path.write_text(
                    json.dumps(
                        [
                            {
                                "agent_name": "Research Agent",
                                "thought": "Need a tool",
                                "tool": "agency.http.request",
                                "tool_input": {"url": "https://example.test"},
                                "result": "done",
                            },
                            {"agent_name": "Research Agent", "output": "Final answer"},
                        ]
                    ),
                    encoding="utf-8",
                )
                queue.put("Final answer")
                return "Final answer"

            with (
                patch("app.runtime.adapters.crewai.adapter.asyncio.to_thread",
                      new=AsyncMock(side_effect=fake_to_thread)),
                patch.object(adapter, "_initialize_log_file", return_value=str(log_path)),
                patch("app.runtime.adapters.crewai.availability.is_crewai_installed", return_value=True),
            ):
                result = await adapter.start_execution(execution.id)

        self.assertEqual(result.status, ExecutionStatus.COMPLETED)
        events = await execution_store.list_events(execution.id)
        event_types = [event.event_type for event in events]
        self.assertIn(ExecutionEventType.TOOL_CALL_STARTED, event_types)
        self.assertIn(ExecutionEventType.LLM_RESPONSE_CREATED, event_types)
        self.assertIn(ExecutionEventType.EXECUTION_COMPLETED, event_types)

    async def test_codex_profile_can_still_create_crewai_execution(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        workflow = _simple_workflow()
        workflow.agent_definitions[0].model_profile_id = "profile-codex"
        await workflow_repo.save_workflow(workflow)
        await profile_repo.save_profile(
            ModelProfileDefinition(
                id="profile-codex",
                name="Codex",
                provider="openai-codex",
                model="gpt-5.3-codex",
            )
        )
        registry = RuntimeAdapterRegistry(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        engine = ExecutionEngine.create_in_memory(
            model_provider_registry=create_test_api_context().llm_provider_registry
        )
        registry.register(NativeRuntimeAdapter(engine))
        registry.register(
            CrewAIRuntimeAdapter(
                workflow_repository=workflow_repo,
                model_profile_repository=profile_repo,
                execution_store=execution_store,
            )
        )

        execution = await registry.create_execution(
            workflow.id,
            {},
            {"type": "manual"},
            runtime_adapter_id="crewai",
        )

        self.assertEqual(execution.runtime_adapter_id, "crewai")
        self.assertEqual(execution.metadata["requested_adapter"], "crewai")


class CrewAIModelBridgeTests(unittest.TestCase):
    def test_agency_model_client_llm_delegates_to_registry_client(self):
        profile = ModelProfileDefinition(
            id="profile-codex",
            name="Codex",
            provider="openai-codex",
            model="gpt-5.3-codex",
            temperature=0.2,
        )

        class FakeModelClient:
            provider_key = "openai_codex"

            def __init__(self):
                self.messages = None
                self.temperature = None

            def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
                self.messages = messages
                self.temperature = temperature
                return ModelResponse(
                    content="CrewAI used the Agency model client.",
                    provider=profile.provider,
                    model=profile.model,
                )

            def generate_structured(self, *args, **kwargs):
                raise AssertionError("structured output was not requested")

            def stream_text(self, *args, **kwargs):
                raise AssertionError("streaming was not requested")

            def count_tokens(self, *args, **kwargs):
                return None

            def embed_texts(self, *args, **kwargs):
                return []

            def health_check(self):
                return {"ok": True}

        client = FakeModelClient()
        llm = AgencyModelClientLLM(profile=profile, model_client=client)

        result = llm.call([{"role": "user", "content": "Hello"}])

        self.assertEqual(result, "CrewAI used the Agency model client.")
        self.assertEqual(client.messages[0].role, "user")
        self.assertEqual(client.messages[0].content, "Hello")
        self.assertEqual(client.temperature, 0.2)


class CrewAIModelBridgeAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_agency_model_client_llm_runs_async_client_on_backend_loop(self):
        profile = ModelProfileDefinition(
            id="profile-codex",
            name="Codex",
            provider="openai-codex",
            model="gpt-5.3-codex",
            temperature=0.2,
        )
        backend_loop = asyncio.get_running_loop()

        class FakeAsyncModelClient:
            provider_key = "openai_codex"

            def __init__(self):
                self.loop = None
                self.messages = None

            def generate_text(self, *args, **kwargs):
                raise AssertionError("sync generation should not run from CrewAI worker thread")

            async def agenerate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
                self.loop = asyncio.get_running_loop()
                self.messages = messages
                return ModelResponse(
                    content="CrewAI used the backend event loop.",
                    provider=profile.provider,
                    model=profile.model,
                )

            def generate_structured(self, *args, **kwargs):
                raise AssertionError("structured output was not requested")

            def stream_text(self, *args, **kwargs):
                raise AssertionError("streaming was not requested")

            def count_tokens(self, *args, **kwargs):
                return None

            def embed_texts(self, *args, **kwargs):
                return []

            def health_check(self):
                return {"ok": True}

        client = FakeAsyncModelClient()
        llm = AgencyModelClientLLM(
            profile=profile,
            model_client=client,
            model_event_loop=backend_loop,
        )

        result = await asyncio.to_thread(llm.call, [{"role": "user", "content": "Hello"}])

        self.assertEqual(result, "CrewAI used the backend event loop.")
        self.assertIs(client.loop, backend_loop)
        self.assertEqual(client.messages[0].content, "Hello")


class RuntimeRegistryConstructionTests(unittest.TestCase):
    def test_registry_uses_single_stable_keys(self):
        workflow_repo = InMemoryWorkflowRepository()
        profile_repo = InMemoryModelProfileRepository()
        execution_store = InMemoryExecutionStore()
        engine = ExecutionEngine.create_in_memory(
            model_provider_registry=create_test_api_context().llm_provider_registry)
        registry = RuntimeAdapterRegistry(
            workflow_repository=workflow_repo,
            model_profile_repository=profile_repo,
            execution_store=execution_store,
        )
        registry.register(NativeRuntimeAdapter(engine))
        registry.register(
            CrewAIRuntimeAdapter(
                workflow_repository=workflow_repo,
                model_profile_repository=profile_repo,
                execution_store=execution_store,
            )
        )
        self.assertEqual(registry.registered_adapter_names(), ["crewai", "native"])


if __name__ == "__main__":
    unittest.main()
