from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api.context import create_database_test_api_context
from app.core.config import reset_settings_cache
from app.db.base import Base
from app.db.session import get_async_engine, reset_session_state
from app.domain import RuntimeRevision, RuntimeRevisionStatus, WorkflowDefinition
from app.runtime.containers import DockerRuntimeManager, RuntimeContainerConfig, RuntimeContainerSpec, \
    RuntimeImageBuildSpec, RuntimeMount


def _docker_integration_enabled() -> bool:
    return os.getenv("ENABLE_DOCKER_INTEGRATION_TESTS", "").lower() in {"1", "true", "yes"}


@unittest.skipUnless(_docker_integration_enabled(), "Docker integration tests are disabled")
class DockerWorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "worker_integration.db"
        self.host_db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.container_db_url = "sqlite+aiosqlite:////runtime-test/worker_integration.db"
        self.env_patch = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "DATABASE_URL": self.host_db_url,
                "EXECUTION_RUNTIME_DATABASE_URL": self.container_db_url,
            },
            clear=False,
        )
        self.env_patch.start()
        reset_settings_cache()
        reset_session_state()
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.context = create_database_test_api_context()
        self.manager = DockerRuntimeManager(
            config=RuntimeContainerConfig(
                runtime_base_image="agency-runtime-test:latest",
                network_name="bridge",
                workdir="/app",
                memory_limit_mb=1024,
                cpu_limit=1.0,
                auto_remove=False,
                bind_integrations_read_only=True,
            )
        )
        self._original_default_mounts = self.manager.default_mounts
        self.manager.default_mounts = lambda: [
            *self._original_default_mounts(),
            RuntimeMount(source=self.temp_dir.name, target="/runtime-test", read_only=False),
        ]

    def _build_test_image(self, *, revision_id: str = "docker-worker-test") -> str:
        return self.manager.build_runtime_image(
            RuntimeImageBuildSpec(
                runtime_revision_id=revision_id,
                image_name="agency-runtime-test",
                image_tag=revision_id,
                context_path=".",
                dockerfile="docker/backend/Dockerfile.runtime-test",
            )
        )

    async def _await_execution(self, execution_id: str, *, expected_status: str, timeout_seconds: float = 30.0):
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            current = await self.context.execution_store.get_execution(execution_id)
            if current is not None and current.status.value == expected_status:
                return current
            await asyncio.sleep(0.2)
        self.fail(f"Timed out waiting for execution '{execution_id}' to reach status '{expected_status}'")

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    async def test_worker_container_executes_workflow_end_to_end(self) -> None:
        image_ref = self._build_test_image()
        workflow = WorkflowDefinition(
            id="workflow-docker-worker",
            name="Docker Worker Workflow",
            nodes=[],
            edges=[],
            entrypoint="noop",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "docker"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )
        execution.runtime_revision_id = "docker-worker-test"
        execution.runtime_fingerprint = "fp-docker-worker"
        await self.context.execution_store.update_execution(execution)

        spec = RuntimeContainerSpec(
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            runtime_revision_id="docker-worker-test",
            image=image_ref,
            command=["python", "-m", "app.runtime.worker"],
            env={
                "APP_ENV": "test",
                "DATABASE_URL": self.container_db_url,
                "AGENCY_EXECUTION_ID": execution.id,
                "AGENCY_WORKFLOW_ID": execution.workflow_id,
                "AGENCY_RUNTIME_REVISION_ID": "docker-worker-test",
                "AGENCY_RUNTIME_ADAPTER_ID": "native",
                "AGENCY_WORKER_ID": f"container-worker-{execution.id}",
                "AGENCY_HEARTBEAT_INTERVAL_SECONDS": "0.05",
            },
            mounts=[RuntimeMount(source=self.temp_dir.name, target="/runtime-test", read_only=False)],
        )

        created = self.manager.create_execution_container(spec)
        try:
            self.manager.start_container(created.container_id)
            state = self.manager.wait_for_container_exit(created.container_id, timeout_seconds=180.0,
                                                         poll_interval_seconds=1.0)
            if state.exit_code != 0:
                self.fail(self.manager.read_container_logs(created.container_id))

            current = await self.context.execution_store.get_execution(execution.id)
            events = await self.context.execution_store.list_events(execution.id)
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(state.exit_code, 0)
            self.assertEqual(current.status.value, "completed")
            self.assertEqual(current.runtime_revision_id, "docker-worker-test")
            self.assertIn("execution.completed", [event.event_type.value for event in events])
        finally:
            try:
                self.manager.remove_container(created.container_id, force=True)
            except Exception:
                pass

    async def test_isolated_control_plane_queue_start_runs_real_worker_container(self) -> None:
        revision_id = "docker-control-plane-test"
        self._build_test_image(revision_id=revision_id)

        class FakeRuntimeRevisionService:
            async def resolve_current_revision(self, *, metadata=None, mark_ready=True, strict=True):
                return RuntimeRevision(
                    id=revision_id,
                    fingerprint=f"fp-{revision_id}",
                    image_name="agency-runtime-test",
                    image_tag=revision_id,
                    build_status=RuntimeRevisionStatus.READY,
                    metadata_json=metadata or {},
                )

            async def invalidate_superseded_revisions(self, active_revision_id: str, *, reason: str = "superseded"):
                return []

        self.context.control_plane.execution_isolation_enabled = True
        self.context.control_plane.runtime_revision_service = FakeRuntimeRevisionService()
        self.context.control_plane.runtime_container_manager = self.manager

        workflow = WorkflowDefinition(
            id="workflow-docker-control-plane",
            name="Docker Control Plane Workflow",
            nodes=[],
            edges=[],
            entrypoint="noop",
            task_definitions=[],
            agent_definitions=[],
            tool_definitions=[],
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(workflow)
        execution = await self.context.runtime_registry.create_execution(
            workflow.id,
            {"topic": "docker-control-plane"},
            {"created_by": "tester"},
            runtime_adapter_id="native",
        )

        await self.context.control_plane.queue_start(execution.id)

        current = await self._await_execution(execution.id, expected_status="completed")
        self.assertIsNotNone(current.container_id)
        assert current.container_id is not None
        state = self.manager.wait_for_container_exit(current.container_id, timeout_seconds=30.0,
                                                     poll_interval_seconds=0.5)
        if state.exit_code != 0:
            self.fail(self.manager.read_container_logs(current.container_id))

        events = await self.context.execution_store.list_events(execution.id)
        self.assertEqual(current.runtime_revision_id, revision_id)
        self.assertEqual(current.runtime_fingerprint, f"fp-{revision_id}")
        self.assertEqual(state.exit_code, 0)
        self.assertIn("execution.started", [event.event_type.value for event in events])
        self.assertIn("execution.completed", [event.event_type.value for event in events])

        try:
            self.manager.remove_container(current.container_id, force=True)
        except Exception:
            pass
