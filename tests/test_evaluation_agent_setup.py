from __future__ import annotations

import asyncio
import unittest

from app.api.context import create_test_api_context
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionArtifact,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionStatus,
    FrameworkHints,
    ModelProfileDefinition,
)
from app.services.agent_tools import (
    SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID,
    SYSTEM_EXECUTION_EVENTS_TOOL_ID,
    SYSTEM_EXECUTION_GET_TOOL_ID,
    SYSTEM_WORKFLOW_GET_TOOL_ID,
    SYSTEM_WORKFLOW_LIST_TOOL_ID,
)
from app.services.main_agent_setup import MainAgentSetupConfig, MainAgentSetupService
from scripts.setup import READ_ONLY_TOOL_IDS, setup_evaluation_agent


class EvaluationAgentSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()

    def _run(self, awaitable):
        return asyncio.run(awaitable)

    def _save_profile(self, profile_id: str, *, purpose: str | None = None) -> None:
        self._run(
            self.context.model_profile_repo.save(
                ModelProfileDefinition(
                    id=profile_id,
                    name=profile_id.replace("-", " ").title(),
                    provider="fake",
                    model=f"{profile_id}-model",
                    temperature=0.0 if purpose == "evaluation" else None,
                    parameters={"purpose": purpose} if purpose else {},
                    framework_hints=FrameworkHints(
                        metadata={"evaluator_profile": True} if purpose == "evaluation" else {}
                    ),
                )
            )
        )

    def _prepare_distinct_profiles_and_agents(self) -> None:
        self._save_profile("profile-main")
        self._save_profile("profile-coder")
        self._save_profile("profile-embedding", purpose="embedding")
        self._save_profile("profile-evaluation", purpose="evaluation")
        self._run(
            MainAgentSetupService(self.context).create_main_agent(
                MainAgentSetupConfig(
                    agent_name="Main Agent",
                    agent_description="Main test agent.",
                    agent_instructions="Answer briefly.",
                    model_profile_id="profile-main",
                    agent_id="main-agent",
                    profile_id="main-agent-profile",
                    workflow_id="main-workflow",
                )
            )
        )
        self._run(
            self.context.agent_repo.save(
                AgentDefinition(
                    id="coder",
                    name="Coder",
                    instructions="Code.",
                    model_profile_id="profile-coder",
                )
            )
        )
        self._run(
            self.context.agent_repo.save(
                AgentDefinition(
                    id="embedding",
                    name="Embedding",
                    instructions="Embed.",
                    model_profile_id="profile-embedding",
                    framework_hints=FrameworkHints(metadata={"agent_kind": "embedding"}),
                )
            )
        )

    def test_setup_evaluation_agent_picks_distinct_evaluator_profile_and_read_only_tools(self) -> None:
        self._prepare_distinct_profiles_and_agents()

        result = self._run(setup_evaluation_agent(context=self.context))

        self.assertEqual(result.agent.id, "evaluation")
        self.assertEqual(result.agent.name, "Evaluation")
        self.assertEqual(result.agent.model_profile_id, "profile-evaluation")
        self.assertEqual(result.model_profile.id, "profile-evaluation")
        self.assertEqual(set(result.agent.tool_ids), set(READ_ONLY_TOOL_IDS))
        self.assertIn(SYSTEM_EXECUTION_GET_TOOL_ID, result.agent.tool_ids)
        self.assertIn(SYSTEM_EXECUTION_EVENTS_TOOL_ID, result.agent.tool_ids)
        self.assertIn(SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID, result.agent.tool_ids)
        self.assertIn(SYSTEM_WORKFLOW_GET_TOOL_ID, result.agent.tool_ids)
        self.assertIn(SYSTEM_WORKFLOW_LIST_TOOL_ID, result.agent.tool_ids)
        self.assertFalse(result.agent.memory.enabled)
        self.assertEqual(result.agent.framework_hints.metadata["agent_kind"], "evaluation")
        self.assertEqual(result.agent.framework_hints.metadata["runtime_role"], "eval_judge")
        self.assertNotIn("profile-evaluation", result.reserved_model_profile_ids)
        self.assertTrue({"profile-main", "profile-coder", "profile-embedding"}.issubset(result.reserved_model_profile_ids))

    def test_setup_evaluation_agent_is_idempotent(self) -> None:
        self._prepare_distinct_profiles_and_agents()

        first = self._run(setup_evaluation_agent(context=self.context))
        second = self._run(setup_evaluation_agent(context=self.context))
        agents = self._run(self.context.agent_repo.list(include_deleted=True))

        self.assertEqual(first.agent.id, second.agent.id)
        self.assertEqual(second.agent.model_profile_id, "profile-evaluation")
        self.assertEqual([agent.id for agent in agents if agent.id == "evaluation"], ["evaluation"])

    def test_setup_evaluation_agent_rejects_reserved_explicit_profile(self) -> None:
        self._prepare_distinct_profiles_and_agents()

        with self.assertRaises(RuntimeError):
            self._run(setup_evaluation_agent(context=self.context, model_profile_id="profile-coder"))

    def test_execution_inspection_tools_read_existing_run_state(self) -> None:
        self._prepare_distinct_profiles_and_agents()
        self._run(setup_evaluation_agent(context=self.context))
        execution = Execution(
            id="execution-eval-target",
            workflow_id="workflow-eval-target",
            runtime_adapter_id="native",
            status=ExecutionStatus.COMPLETED,
            input_payload={"topic": "eval"},
            output_payload={"final_output": "done"},
        )
        self._run(self.context.execution_store.save_execution(execution))
        self._run(
            self.context.execution_store.save_event(
                ExecutionEvent(
                    execution_id=execution.id,
                    workflow_id=execution.workflow_id,
                    event_type=ExecutionEventType.EXECUTION_COMPLETED,
                    sequence=1,
                    payload={"output": execution.output_payload},
                )
            )
        )
        self._run(
            self.context.execution_store.save_artifact(
                ExecutionArtifact(
                    id="artifact-eval",
                    execution_id=execution.id,
                    artifact_type="text",
                    name="result.txt",
                    content_text="artifact content",
                )
            )
        )

        get_tool = self._run(self.context.tool_repo.get(SYSTEM_EXECUTION_GET_TOOL_ID))
        events_tool = self._run(self.context.tool_repo.get(SYSTEM_EXECUTION_EVENTS_TOOL_ID))
        artifacts_tool = self._run(self.context.tool_repo.get(SYSTEM_EXECUTION_ARTIFACTS_TOOL_ID))
        get_result = self._run(
            self.context.tool_service.tool_registry.execute(
                get_tool,
                {"execution_id": execution.id},
                execution_id="judge-execution",
            )
        )
        events_result = self._run(
            self.context.tool_service.tool_registry.execute(
                events_tool,
                {"execution_id": execution.id, "event_types": ["execution.completed"]},
                execution_id="judge-execution",
            )
        )
        artifacts_result = self._run(
            self.context.tool_service.tool_registry.execute(
                artifacts_tool,
                {"execution_id": execution.id, "include_content": True},
                execution_id="judge-execution",
            )
        )

        self.assertEqual(get_result["status"], "ok")
        self.assertEqual(get_result["execution"]["id"], execution.id)
        self.assertEqual(events_result["count"], 1)
        self.assertEqual(events_result["items"][0]["event_type"], "execution.completed")
        self.assertEqual(artifacts_result["count"], 1)
        self.assertEqual(artifacts_result["items"][0]["content_text"], "artifact content")


if __name__ == "__main__":
    unittest.main()
