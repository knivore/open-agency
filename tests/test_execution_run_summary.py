from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.api.context import create_test_api_context
from app.core.config import reset_settings_cache
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionStatus,
    ModelProfileDefinition,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.llm.base import ModelResponse
from app.llm.registry import LLMEnvironmentConfig
from app.services import ExecutionRunSummaryService


class _RunSummaryFakeModelClient:
    provider_key = "fake"

    def __init__(self, profile: ModelProfileDefinition, env: LLMEnvironmentConfig):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="Final answer", provider=self.profile.provider, model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content={"ok": True}, provider=self.profile.provider, model=self.profile.model)

    def stream_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        yield "chunk"

    def count_tokens(self, messages, **kwargs):
        return 1

    def health_check(self):
        return {"ok": True}


class ExecutionRunSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.context = create_test_api_context()
        self.context.llm_provider_registry.register("fake", lambda profile, env: _RunSummaryFakeModelClient(profile, env))
        self.profile = ModelProfileDefinition(
            id="profile-fake",
            name="Fake",
            provider="fake",
            model="fake-model",
        )
        await self.context.model_profile_repo.save(self.profile)
        self.workflow = WorkflowDefinition(
            id="workflow-run-summary",
            name="Run Summary Workflow",
            entrypoint="node-1",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-1",
                    name="Task Node",
                    node_type="task",
                    task_id="task-1",
                    agent_id="agent-1",
                )
            ],
            task_definitions=[
                TaskDefinition(
                    id="task-1",
                    name="Task",
                    description="Do work",
                    agent_id="agent-1",
                )
            ],
            agent_definitions=[
                AgentDefinition(
                    id="agent-1",
                    name="Agent",
                    instructions="Be concise",
                    model_profile_id=self.profile.id,
                )
            ],
            metadata={
                "created_by": "user-1",
                "owner_ids": ["user-1"],
                "persistent_run_summary": {
                    "enabled": True,
                    "scope": "workflow",
                    "importance": 55,
                },
            },
            default_runtime_adapter_id="native",
        )
        await self.context.runtime_registry.register_workflow(self.workflow)

    async def asyncTearDown(self) -> None:
        os.environ.pop("AGENT_PERSISTENT_RUN_SUMMARY_ENABLED", None)
        reset_settings_cache()

    async def test_native_execution_creates_run_summary_when_flag_and_workflow_opt_in_enabled(self) -> None:
        with patch.dict(os.environ, {"AGENT_PERSISTENT_RUN_SUMMARY_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            execution = await self.context.runtime_registry.create_execution(
                self.workflow.id,
                {"topic": "memory rollout"},
                {"source": "test", "created_by": "user-1"},
                runtime_adapter_id="native",
            )
            result = await self.context.runtime_registry.start_execution(execution.id)
            summaries = await self.context.memory_repo.query(
                workflow_id=self.workflow.id,
                memory_kinds=["run_summary"],
                statuses=["active"],
                limit=10,
            )

        self.assertEqual(result.status.value, "completed")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].memory_kind.value, "run_summary")
        self.assertEqual(summaries[0].source_execution_id, execution.id)
        self.assertEqual(summaries[0].metadata["execution_status"], "completed")

    async def test_run_summary_not_written_when_feature_flag_disabled(self) -> None:
        execution = await self.context.runtime_registry.create_execution(
            self.workflow.id,
            {"topic": "memory rollout"},
            {"source": "test", "created_by": "user-1"},
            runtime_adapter_id="native",
        )
        await self.context.runtime_registry.start_execution(execution.id)
        summaries = await self.context.memory_repo.query(
            workflow_id=self.workflow.id,
            memory_kinds=["run_summary"],
            limit=10,
        )
        self.assertEqual(summaries, [])

    async def test_duplicate_run_summary_is_suppressed(self) -> None:
        service = ExecutionRunSummaryService(self.context)
        execution = Execution(
            id="execution-1",
            workflow_id=self.workflow.id,
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            output_payload={"final_output": "Final answer"},
            created_by="user-1",
        )
        with patch.dict(os.environ, {"AGENT_PERSISTENT_RUN_SUMMARY_ENABLED": "true"}, clear=False):
            reset_settings_cache()
            first = await service.maybe_persist_run_summary(execution=execution, workflow=self.workflow)
            second = await service.maybe_persist_run_summary(execution=execution, workflow=self.workflow)
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "skipped")
        self.assertEqual(second["reason"], "duplicate")


if __name__ == "__main__":
    unittest.main()
