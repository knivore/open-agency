from __future__ import annotations

import os
import tempfile
import unittest
from fastapi.testclient import TestClient
from pathlib import Path
from sqlalchemy import select
from unittest.mock import AsyncMock, patch

from app.api.context import create_database_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.db.models import Base, ToolInvocationORM
from app.db.session import get_async_engine, get_session_maker, reset_session_state
from app.domain import (
    AgentDefinition,
    FrameworkHints,
    MemorySettings,
    ModelProfileDefinition,
    ScheduleDefinition,
    TaskDefinition,
    ToolDefinition,
    UserDefinition,
    VersionDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.runtime.adapters import NativeRuntimeAdapter, RuntimeAdapterRegistry
from app.runtime.native.engine import ExecutionEngine
from app.runtime.native.state import SQLExecutionStore
from app.tools.registry import ToolRegistry
from tests.test_native_execution_engine import FakeModelClient


class StorageMigrationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "storage-migration.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.env_patch = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "DATABASE_URL": self.db_url,
            },
            clear=False,
        )
        self.env_patch.start()
        reset_settings_cache()
        reset_session_state()

    async def asyncSetUp(self) -> None:
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = get_session_maker()
        self.context = create_database_test_api_context()
        self.context.scheduler.execution_starter = AsyncMock()

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    def _tool_definition(self, *, tool_id: str = "tool-echo") -> ToolDefinition:
        return ToolDefinition.model_validate(
            {
                "id": tool_id,
                "name": "Echo Tool",
                "description": "Echoes text",
                "tool_type": "python_function",
                "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                "output_schema": {"type": "object"},
                "implementation": {
                    "implementation_type": "python_function",
                    "target": "tests.native_test_tools",
                    "callable_name": "echo_tool",
                    "config": {},
                },
                "security": {
                    "requires_approval": False,
                    "sandbox_required": False,
                    "allow_shell": False,
                    "allow_browser": False,
                    "allow_filesystem": False,
                    "allow_network": False,
                    "allowlisted_domains": [],
                    "allowlisted_mcp_servers": [],
                    "module_allowlist": ["tests.native_test_tools"],
                    "function_allowlist": ["echo_tool"],
                    "read_only_sql": True,
                    "approval_on_rejection": "fail",
                    "credential_references": [],
                    "redaction_enabled": False,
                    "redaction_rules": [],
                    "read_only": False,
                    "dangerous": False,
                    "allowed_paths": [],
                },
                "mcp_exposure": {
                    "expose_as_mcp_tool": False,
                    "expose_as_mcp_resource": False,
                    "expose_as_mcp_prompt": False,
                    "name_override": None,
                    "tags": [],
                },
                "framework_hints": {"preferred_adapter": None, "adapter_config": {}, "metadata": {}},
            }
        )

    def _schedule_definition(self, *, schedule_id: str, workflow_id: str) -> ScheduleDefinition:
        return ScheduleDefinition.model_validate(
            {
                "id": schedule_id,
                "name": "DB Schedule",
                "workflow_id": workflow_id,
                "trigger_type": "manual",
                "trigger_config": {},
                "input_template": {"source": "scheduler"},
                "timezone": "UTC",
                "max_concurrent_executions": 1,
            }
        )

    def _workflow_definition(self, tool: ToolDefinition) -> WorkflowDefinition:
        agent = AgentDefinition(
            id="agent-storage",
            name="Storage Agent",
            instructions="Use tools to answer",
            model_profile_id="profile-storage",
            tool_ids=[tool.id],
            memory=MemorySettings(enabled=True),
            framework_hints=FrameworkHints(adapter_config={"max_iterations": 3}),
        )
        task = TaskDefinition(
            id="task-storage",
            name="Storage Task",
            description="Run the tool",
            agent_id=agent.id,
            tool_ids=[tool.id],
        )
        node = WorkflowNodeDefinition(
            id="node-storage",
            name="Storage Node",
            node_type="task",
            task_id=task.id,
            agent_id=agent.id,
        )
        return WorkflowDefinition(
            id="workflow-storage",
            name="Storage Workflow",
            description="Persists executions in SQL",
            nodes=[node],
            edges=[],
            entrypoint=node.id,
            task_definitions=[task],
            agent_definitions=[agent],
            tool_definitions=[tool],
            default_runtime_adapter_id="native",
            versioning=VersionDefinition(revision=1, is_published=True),
        )

    async def test_api_creates_and_reads_records_from_database_backed_repositories(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-api",
            name="API Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
            versioning=VersionDefinition(revision=1, is_published=True),
            metadata={"created_by": "user-storage-api", "owner_ids": ["user-storage-api"]},
        )
        await self.context.workflow_repo.create(workflow)

        client = TestClient(create_app(context=self.context))
        await self.context.user_repo.create(
            UserDefinition(id="user-storage-api", email="storage-api@example.com", display_name="Storage API User")
        )
        client.headers.update(
            {
                "x-agency-user-id": "user-storage-api",
                "x-agency-user-email": "storage-api@example.com",
            }
        )

        provider_response = client.post(
            "/model-providers",
            json={
                "id": "provider-api",
                "name": "OpenAI",
                "provider_type": "openai",
                "endpoint": {"base_url": "https://api.openai.com/v1"},
                "config": {"tier": "default"},
            },
        )
        self.assertEqual(provider_response.status_code, 200)

        execution_response = client.post(
            "/executions",
            json={
                "workflowId": workflow.id,
                "input": {"topic": "storage"},
                "trigger": {"type": "manual", "created_by": "api-test"},
            },
        )
        self.assertEqual(execution_response.status_code, 200)
        execution_id = execution_response.json()["id"]

        listed = client.get("/executions")
        self.assertEqual(listed.status_code, 200)
        execution_ids = {item["id"] for item in listed.json()["items"]}
        self.assertIn(execution_id, execution_ids)

        detail = client.get(f"/executions/{execution_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["execution"]["workflow_id"], workflow.id)

        stored_provider = await self.context.model_provider_repo.get("provider-api")
        self.assertIsNotNone(stored_provider)
        stored_execution = await self.context.execution_store.get_execution(execution_id)
        self.assertIsNotNone(stored_execution)
        self.assertEqual(stored_execution.created_by, "api-test")

    async def test_native_runtime_persists_execution_events_and_tool_invocations_in_database(self) -> None:
        tool = self._tool_definition()
        workflow = self._workflow_definition(tool)
        profile = ModelProfileDefinition(
            id="profile-storage",
            name="Storage Profile",
            provider="fake",
            model="fake-model",
            supports_tools=True,
        )

        model_registry = self.context.llm_provider_registry
        model_registry.register("fake", lambda profile, env: FakeModelClient(profile, env))

        await self.context.tool_repo.create(tool)
        await self.context.workflow_repo.create(workflow)
        await self.context.model_profile_repo.create(profile)

        engine = ExecutionEngine(
            workflow_repository=self.context.workflow_repo,
            model_profile_repository=self.context.model_profile_repo,
            execution_store=self.context.execution_store,
            model_provider_registry=model_registry,
            approval_manager=self.context.control_plane.approval_manager,
        )
        runtime_registry = RuntimeAdapterRegistry(
            workflow_repository=self.context.workflow_repo,
            model_profile_repository=self.context.model_profile_repo,
            execution_store=self.context.execution_store,
        )
        runtime_registry.register(NativeRuntimeAdapter(engine))
        engine.agent_executor.tool_executor.tool_registry.runtime_registry = runtime_registry
        engine.agent_executor.tool_executor.tool_registry.execution_store = self.context.execution_store

        execution = await runtime_registry.create_execution(workflow.id, {"prompt": "hi"}, {"source": "db-test"})
        result = await runtime_registry.start_execution(execution.id)
        events = await self.context.execution_store.list_events(execution.id)

        self.assertEqual(result.status.value, "completed")
        self.assertGreaterEqual(len(events), 4)
        self.assertEqual(events[0].event_type.value, "execution.created")
        self.assertTrue(any(event.event_type.value == "tool.call.completed" for event in events))

        async with self.session_factory() as session:
            rows = (await session.execute(
                select(ToolInvocationORM).order_by(ToolInvocationORM.started_at.asc()))).scalars().all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].execution_id, execution.id)
        self.assertEqual(rows[0].tool_id, tool.id)
        self.assertEqual(rows[0].status, "completed")

    async def test_scheduler_creates_execution_records_in_database(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-scheduled-db",
            name="Scheduled DB Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
            versioning=VersionDefinition(revision=1, is_published=True),
        )
        await self.context.workflow_repo.create(workflow)

        schedule = await self.context.scheduler.create_schedule(
            self._schedule_definition(schedule_id="schedule-db", workflow_id=workflow.id)
        )
        result = await self.context.scheduler.trigger_now(schedule.id)
        execution = await self.context.execution_store.get_execution(result.execution_id)
        executions_for_workflow = await self.context.execution_store.list_executions_by_workflow(workflow.id)

        self.assertIsNotNone(execution)
        assert execution is not None
        self.assertEqual(execution.workflow_id, workflow.id)
        self.assertEqual(execution.metadata["trigger"]["schedule_id"], schedule.id)
        self.assertIn(execution.id, {item.id for item in executions_for_workflow})

    async def test_database_schedule_fire_claim_blocks_duplicate_claims(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-claim-db",
            name="Claim DB Workflow",
            nodes=[],
            edges=[],
            entrypoint="manual",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
            versioning=VersionDefinition(revision=1, is_published=True),
        )
        await self.context.workflow_repo.create(workflow)
        await self.context.scheduler.create_schedule(
            self._schedule_definition(schedule_id="schedule-claim-db", workflow_id=workflow.id)
        )
        fire_at = utc_now()

        first = await self.context.schedule_repo.acquire_schedule_fire_claim(
            schedule_id="schedule-claim-db",
            scheduled_fire_at=fire_at,
            claimed_by="scheduler-a",
            lease_seconds=300,
        )
        second = await self.context.schedule_repo.acquire_schedule_fire_claim(
            schedule_id="schedule-claim-db",
            scheduled_fire_at=fire_at,
            claimed_by="scheduler-b",
            lease_seconds=300,
        )

        self.assertTrue(first)
        self.assertFalse(second)

        await self.context.schedule_repo.mark_schedule_fire_claim_fired(
            schedule_id="schedule-claim-db",
            scheduled_fire_at=fire_at,
            execution_id="execution-claim-db",
            claimed_by="scheduler-a",
        )
        after_fired = await self.context.schedule_repo.acquire_schedule_fire_claim(
            schedule_id="schedule-claim-db",
            scheduled_fire_at=fire_at,
            claimed_by="scheduler-b",
            lease_seconds=300,
        )
        self.assertFalse(after_fired)

    async def test_tool_registry_resolves_database_backed_tools(self) -> None:
        tool = self._tool_definition(tool_id="tool-db-registry")
        await self.context.tool_repo.create(tool)

        registry = ToolRegistry(
            approval_manager=self.context.control_plane.approval_manager,
            runtime_registry=self.context.runtime_registry,
            mcp_registry=self.context.mcp_registry,
            execution_store=self.context.execution_store,
            tool_repository=self.context.tool_repo,
        )
        resolved = await registry.get_tool_definition(tool.id)
        enabled = await registry.list_enabled_tools()

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.id, tool.id)
        self.assertIn(tool.id, {item.id for item in enabled})

    async def test_database_context_uses_sql_backed_repositories_for_core_entities(self) -> None:
        self.assertEqual(type(self.context.execution_store), SQLExecutionStore)
        self.assertTrue(type(self.context.agent_repo).__name__.startswith("SQL"))
        self.assertTrue(type(self.context.tool_repo).__name__.startswith("SQL"))
        self.assertTrue(type(self.context.workflow_repo).__name__.startswith("SQL"))
        self.assertTrue(type(self.context.model_profile_repo).__name__.startswith("SQL"))
        self.assertTrue(type(self.context.schedule_repo).__name__.startswith("SQL"))


if __name__ == "__main__":
    unittest.main()
