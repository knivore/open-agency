from __future__ import annotations

import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.context import create_test_api_context
from app.domain import Execution, ExecutionEvent, ExecutionEventType, ExecutionStatus, \
    WorkflowDefinition, WorkflowNodeDefinition
from app.observability.service import ObservabilityService
from app.services.agents import AgentService
from app.services.executions import ExecutionService
from app.services.schedules import ScheduleService
from app.services.workflows import WorkflowService


class ServiceLayerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = create_test_api_context()

    async def test_agent_service_lists_agent_executions(self):
        service = AgentService(self.context)
        execution = Execution(
            id="exec-agent-service",
            workflow_id="workflow-1",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            input_payload={},
            metadata={"agent_ids": ["agent-service"]},
        )
        await self.context.execution_store.save_execution(execution)

        result = await service.list_agent_executions("agent-service")
        self.assertEqual(result["items"][0]["id"], "exec-agent-service")

    async def test_workflow_service_publish_and_list_executions(self):
        workflow = WorkflowDefinition(
            id="workflow-service",
            name="Service Workflow",
            nodes=[WorkflowNodeDefinition(id="node-1", name="Node", node_type="task", task_id="task-1",
                                          agent_id="agent-1")],
            edges=[],
            entrypoint="node-1",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.workflow_repo.create(workflow)
        service = WorkflowService(self.context)

        published = await service.publish_workflow("workflow-service")
        self.assertTrue(published["versioning"]["is_published"])

        unpublished = await service.unpublish_workflow("workflow-service")
        self.assertFalse(unpublished["versioning"]["is_published"])
        self.assertEqual(unpublished["versioning"]["revision"], 3)
        self.assertIsNone(unpublished["metadata"]["published_at"])
        self.assertTrue(unpublished["metadata"]["unpublished_at"])

        versions = await service.list_workflow_versions("workflow-service")
        self.assertEqual([item["revision"] for item in versions["items"]], [3, 2, 1])
        self.assertTrue(versions["items"][0]["is_current"])
        self.assertEqual(versions["items"][0]["status"], "draft")
        self.assertFalse(versions["items"][1]["is_current"])
        self.assertEqual(versions["items"][1]["status"], "published")
        version_one = await service.get_workflow_version("workflow-service", 1)
        self.assertEqual(version_one["definition"]["versioning"]["revision"], 1)
        self.assertFalse(version_one["is_current"])

        execution = Execution(
            id="exec-workflow-service",
            workflow_id="workflow-service",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            input_payload={},
        )
        await self.context.execution_store.save_execution(execution)
        listing = await service.list_workflow_executions("workflow-service")
        self.assertEqual(listing["items"][0]["id"], "exec-workflow-service")

    async def test_workflow_publish_can_replace_active_executions(self):
        workflow = WorkflowDefinition(
            id="workflow-replace-active",
            name="Replace Active Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.workflow_repo.create(workflow)
        replace_mock = AsyncMock(return_value=["execution-replacement"])
        self.context.control_plane.replace_active_executions_for_workflow_revision = replace_mock

        service = WorkflowService(self.context)
        await service.publish_workflow(
            "workflow-replace-active",
            {"restart_active_executions": True},
        )

        replace_mock.assert_awaited_once_with(
            workflow_id="workflow-replace-active",
            previous_revision=1,
            replacement_revision=2,
            source="workflow_publish",
        )

    async def test_schedule_service_create_and_patch(self):
        workflow = WorkflowDefinition(
            id="workflow-schedule-service",
            name="Schedule Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.workflow_repo.create(workflow)
        service = ScheduleService(self.context)

        created = await service.create_schedule(
            {
                "id": "schedule-service",
                "name": "Nightly",
                "workflow_id": "workflow-schedule-service",
                "schedule_type": "cron",
                "cron": "0 2 * * *",
                "input_payload": {},
                "timezone": "UTC",
            }
        )
        self.assertEqual(created.id, "schedule-service")

        patched = await service.patch_schedule("schedule-service", {"enabled": False})
        self.assertFalse(patched.enabled)

    async def test_execution_service_update_and_approval_shape(self):
        service = ExecutionService(self.context)
        execution = Execution(
            id="exec-update-service",
            workflow_id="workflow-update",
            runtime_adapter_id="native",
            status=ExecutionStatus.CREATED,
            input_payload={},
        )
        await self.context.execution_store.save_execution(execution)

        updated = await service.update_execution("exec-update-service", {"input_payload": {"topic": "updated"}})
        self.assertEqual(updated["input_payload"]["topic"], "updated")

        with self.assertRaises(Exception):
            await service.list_execution_events("missing-execution")

    async def test_observability_service_metrics(self):
        service = ObservabilityService(self.context)
        execution = Execution(
            id="exec-observe-service",
            workflow_id="workflow-observe",
            runtime_adapter_id="native",
            status="completed",
            input_payload={},
            metadata={"agent_ids": ["agent-observe"]},
        )
        await self.context.execution_store.save_execution(execution)
        await self.context.execution_store.save_event(
            ExecutionEvent(
                execution_id="exec-observe-service",
                workflow_id="workflow-observe",
                agent_id="agent-observe",
                event_type=ExecutionEventType.LLM_RESPONSE_CREATED,
                sequence=1,
                payload={"usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}},
                metrics={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5, "model_provider": "fake",
                         "model_name": "fake-model"},
            )
        )

        metrics = await service.get_agent_metrics("agent-observe")
        self.assertEqual(metrics["total_tokens"], 5)


class ToolServiceLayerTests(unittest.TestCase):
    def test_builtin_tool_seed_data_is_app_owned(self):
        context = create_test_api_context()
        tools = asyncio.run(context.ensure_builtin_tool_seed_data())
        tool_ids = {tool.id for tool in tools}
        self.assertIn("agency.file.write-text", tool_ids)
        self.assertIn("agency.command.run", tool_ids)

    def test_tool_repository_normalizes_stale_privileged_sandbox_metadata(self):
        context = create_test_api_context()
        tools = asyncio.run(context.ensure_builtin_tool_seed_data())
        repo_inspect = next(tool for tool in tools if tool.id == "agency.repo.inspect")
        stale_security = repo_inspect.security.model_copy(update={"sandbox_required": False})
        stale_tool = repo_inspect.model_copy(update={"security": stale_security})
        asyncio.run(context.tool_repo.save(stale_tool))

        repaired = asyncio.run(context.tool_repo.get("agency.repo.inspect"))
        self.assertIsNotNone(repaired)
        self.assertTrue(repaired.security.sandbox_required)
        self.assertTrue(repaired.security.allow_filesystem)
        self.assertTrue(repaired.security.read_only)
        self.assertFalse(repaired.security.requires_approval)

    def test_builtin_seed_tracks_optional_tool_install_remove_and_reinstall(self):
        context = create_test_api_context()
        tools = asyncio.run(context.ensure_builtin_tool_seed_data())
        command_tool = next(tool for tool in tools if tool.id == "agency.command.run")
        owned_tool = command_tool.model_copy(
            update={
                "implementation": command_tool.implementation.model_copy(
                    update={
                        "config": {
                            **command_tool.implementation.config,
                            "agency_optional_module_key": "test_pack",
                        }
                    }
                )
            }
        )
        active_spec = SimpleNamespace(key="test_pack", available=lambda: True)

        with patch("app.api.context.builtin_tool_definitions", return_value=[owned_tool]), patch(
            "app.modules.registry.optional_module_specs",
            return_value=(active_spec,),
        ):
            asyncio.run(context.ensure_builtin_tool_seed_data())

        stale = owned_tool.model_copy(
            update={"implementation": owned_tool.implementation.model_copy(update={"target": "removed.core.module"})}
        )
        asyncio.run(context.tool_repo.save(stale))

        with patch("app.api.context.builtin_tool_definitions", return_value=[owned_tool]), patch(
            "app.modules.registry.optional_module_specs",
            return_value=(active_spec,),
        ):
            asyncio.run(context.ensure_builtin_tool_seed_data())

        repaired = asyncio.run(context.tool_repo.get(command_tool.id))
        self.assertIsNotNone(repaired)
        self.assertEqual(repaired.implementation.target, command_tool.implementation.target)

        with patch("app.api.context.builtin_tool_definitions", return_value=[]), patch(
            "app.modules.registry.optional_module_specs",
            return_value=(),
        ):
            asyncio.run(context.ensure_builtin_tool_seed_data())

        self.assertIsNone(asyncio.run(context.tool_repo.get(command_tool.id)))
        self.assertIsNotNone(asyncio.run(context.tool_repo.get(command_tool.id, include_deleted=True)))

        with patch("app.api.context.builtin_tool_definitions", return_value=[owned_tool]), patch(
            "app.modules.registry.optional_module_specs",
            return_value=(active_spec,),
        ):
            asyncio.run(context.ensure_builtin_tool_seed_data())

        restored = asyncio.run(context.tool_repo.get(command_tool.id))
        self.assertIsNotNone(restored)
        self.assertEqual(restored.implementation.target, command_tool.implementation.target)


if __name__ == "__main__":
    unittest.main()
