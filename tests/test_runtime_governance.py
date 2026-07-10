from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.core.config import reset_settings_cache
from app.domain import (
    AgentDefinition,
    Execution,
    ExecutionStatus,
    MemoryType,
    ModelProfileDefinition,
    TaskDefinition,
    WorkflowDefinition,
    WorkflowNodeDefinition,
)
from app.api.context import create_test_api_context
from app.llm.base import ModelMessage
from app.runtime.governance.budgets import resolve_token_budget_policy
from app.runtime.governance.compaction import RuntimeContextCompactor, deterministic_compact_messages
from app.runtime.governance.context_health import estimate_context_health
from app.runtime.governance.recorder import record_token_usage_snapshot
from app.runtime.governance.token_usage import normalize_token_usage
from app.runtime.native.state import NativeExecutionState


def _governance_inputs(
    *,
    workflow_metadata: dict | None = None,
    agent_metadata: dict | None = None,
    task_metadata: dict | None = None,
    input_payload: dict | None = None,
    trigger_payload: dict | None = None,
):
    workflow = WorkflowDefinition(
        id="workflow-1",
        name="Workflow",
        entrypoint="node-1",
        metadata=workflow_metadata or {},
    )
    agent = AgentDefinition(
        id="agent-1",
        name="Agent",
        instructions="Help.",
        metadata=agent_metadata or {},
    )
    task = TaskDefinition(
        id="task-1",
        name="Task",
        description="Do the work.",
        agent_id=agent.id,
        metadata=task_metadata or {},
    )
    execution = Execution(
        id="execution-1",
        workflow_id=workflow.id,
        status=ExecutionStatus.CREATED,
        runtime_adapter="native",
        input_json=input_payload or {},
        trigger_payload=trigger_payload or {},
    )
    return workflow, agent, task, execution


class RuntimeGovernanceBudgetTests(unittest.TestCase):
    def tearDown(self) -> None:
        for key in (
            "AGENT_TOKEN_BUDGET_WARN_RATIO",
            "AGENT_TOKEN_BUDGET_HARD_RATIO",
            "AGENT_RUN_TOTAL_TOKEN_BUDGET",
            "AGENT_TOKEN_BUDGET_ACTION",
        ):
            os.environ.pop(key, None)
        reset_settings_cache()

    def test_resolve_token_budget_policy_uses_global_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_RUN_TOTAL_TOKEN_BUDGET": "100",
                "AGENT_TOKEN_BUDGET_WARN_RATIO": "0.25",
                "AGENT_TOKEN_BUDGET_HARD_RATIO": "0.5",
                "AGENT_TOKEN_BUDGET_ACTION": "pause_execution",
            },
            clear=False,
        ):
            reset_settings_cache()
            workflow, agent, task, execution = _governance_inputs()

            policy = resolve_token_budget_policy(
                workflow=workflow,
                agent=agent,
                task=task,
                execution=execution,
            )

        assert policy is not None
        self.assertEqual(policy.run_total_tokens, 100)
        self.assertEqual(policy.warn_ratio, 0.25)
        self.assertEqual(policy.hard_ratio, 0.5)
        self.assertEqual(policy.action, "pause_execution")

    def test_explicit_budget_metadata_overrides_global_defaults(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_RUN_TOTAL_TOKEN_BUDGET": "100",
                "AGENT_TOKEN_BUDGET_ACTION": "fail_execution",
            },
            clear=False,
        ):
            reset_settings_cache()
            workflow, agent, task, execution = _governance_inputs(
                workflow_metadata={
                    "runtime_governance": {
                        "token_budget": {
                            "run_total_tokens": 40,
                            "warn_ratio": 0.6,
                            "hard_ratio": 1.2,
                            "action": "warn_only",
                        }
                    }
                }
            )

            policy = resolve_token_budget_policy(
                workflow=workflow,
                agent=agent,
                task=task,
                execution=execution,
            )

        assert policy is not None
        self.assertEqual(policy.run_total_tokens, 40)
        self.assertEqual(policy.warn_ratio, 0.6)
        self.assertEqual(policy.hard_ratio, 1.2)
        self.assertEqual(policy.action, "warn_only")

    def test_no_global_budget_returns_no_policy(self) -> None:
        reset_settings_cache()
        workflow, agent, task, execution = _governance_inputs()

        policy = resolve_token_budget_policy(
            workflow=workflow,
            agent=agent,
            task=task,
            execution=execution,
        )

        self.assertIsNone(policy)


class RuntimeGovernanceTokenUsageTests(unittest.TestCase):
    def test_openai_usage_aliases_include_cached_reasoning_and_cost(self) -> None:
        profile = ModelProfileDefinition(
            id="profile-1",
            name="Profile",
            provider="openai",
            model="gpt-test",
            parameters={
                "input_token_cost_per_1m": 1.0,
                "output_token_cost_per_1m": 2.0,
                "currency": "USD",
            },
        )

        usage = normalize_token_usage(
            {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
            profile=profile,
        )

        self.assertEqual(usage.prompt_tokens, 100)
        self.assertEqual(usage.completion_tokens, 30)
        self.assertEqual(usage.total_tokens, 130)
        self.assertEqual(usage.cached_tokens, 20)
        self.assertEqual(usage.reasoning_tokens, 7)
        self.assertEqual(usage.estimated_cost, 0.00014)
        self.assertEqual(usage.currency, "USD")

    def test_anthropic_usage_aliases_compute_total(self) -> None:
        usage = normalize_token_usage(
            {
                "input_tokens": 11,
                "output_tokens": 13,
                "cache_read_input_tokens": 5,
            },
            provider="anthropic",
            model="claude-test",
        )

        self.assertEqual(usage.prompt_tokens, 11)
        self.assertEqual(usage.completion_tokens, 13)
        self.assertEqual(usage.total_tokens, 24)
        self.assertEqual(usage.cached_tokens, 5)
        self.assertEqual(usage.provider, "anthropic")
        self.assertEqual(usage.model, "claude-test")

    def test_gemini_usage_aliases_are_preserved(self) -> None:
        usage = normalize_token_usage(
            {
                "promptTokenCount": 21,
                "candidatesTokenCount": 8,
                "totalTokenCount": 29,
            },
            provider="google",
            model="gemini-test",
        )

        self.assertEqual(usage.prompt_tokens, 21)
        self.assertEqual(usage.completion_tokens, 8)
        self.assertEqual(usage.total_tokens, 29)
        self.assertEqual(usage.provider_usage["totalTokenCount"], 29)

    def test_missing_provider_usage_is_estimated_from_prompt_and_response(self) -> None:
        for provider in ("ollama", "openai-codex"):
            with self.subTest(provider=provider):
                usage = normalize_token_usage(
                    {},
                    provider=provider,
                    model="unknown",
                    estimated_prompt_tokens=12,
                    response_content="abcd efgh ijkl",
                )

                self.assertEqual(usage.prompt_tokens, 12)
                self.assertGreater(usage.completion_tokens, 0)
                self.assertEqual(usage.total_tokens, usage.prompt_tokens + usage.completion_tokens)
                self.assertTrue(usage.estimated)
                self.assertEqual(usage.estimate_method, "estimated_prompt_and_completion_chars_div_4")


class RuntimeGovernanceTokenUsageSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_usage_snapshot_records_model_fallbacks(self) -> None:
        context = create_test_api_context()
        execution = Execution(
            id="execution-fallback-usage",
            workflow_id="workflow-fallback",
            status=ExecutionStatus.RUNNING,
            runtime_adapter="native",
            input_json={},
        )
        await context.execution_store.save_execution(execution)
        usage = normalize_token_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "model_fallback": {
                    "used": True,
                    "primary_provider": "openai",
                    "primary_model": "gpt-primary",
                    "fallback_provider": "anthropic",
                    "fallback_model": "claude-backup",
                    "fallback_index": 1,
                },
            },
            provider="anthropic",
            model="claude-backup",
        )

        await record_token_usage_snapshot(
            context.execution_store,
            execution_id=execution.id,
            usage=usage,
            agent_id="agent-1",
            task_id="task-1",
            workflow_id="workflow-fallback",
            model_request_id="request-1",
            event_id="event-1",
        )

        updated = await context.execution_store.get_execution(execution.id)
        assert updated is not None
        token_usage = updated.metadata["runtime_governance"]["token_usage"]
        self.assertEqual(token_usage["fallback_count"], 1)
        self.assertEqual(token_usage["total"]["fallback_count"], 1)
        self.assertEqual(token_usage["by_agent"]["agent-1"]["fallback_count"], 1)
        self.assertEqual(token_usage["by_task"]["task-1"]["fallback_count"], 1)
        self.assertEqual(token_usage["by_model"]["anthropic:claude-backup"]["fallback_count"], 1)
        self.assertEqual(token_usage["model_fallbacks"][0]["primary_model"], "gpt-primary")
        self.assertEqual(token_usage["model_fallbacks"][0]["fallback_model"], "claude-backup")


class RuntimeGovernanceContextHealthTests(unittest.TestCase):
    def test_context_health_thresholds(self) -> None:
        profile = ModelProfileDefinition(
            id="profile-context",
            name="Context Profile",
            provider="fake",
            model="fake-model",
            context_window=100,
            max_tokens=0,
        )

        normal = estimate_context_health(
            [ModelMessage(role="user", content="x" * 64)],
            model_profile=profile,
        )
        warning = estimate_context_health(
            [ModelMessage(role="user", content="x" * 264)],
            model_profile=profile,
        )
        critical = estimate_context_health(
            [ModelMessage(role="user", content="x" * 336)],
            model_profile=profile,
        )
        overflow = estimate_context_health(
            [ModelMessage(role="user", content="x" * 416)],
            model_profile=profile,
        )

        self.assertEqual(normal.status, "normal")
        self.assertEqual(warning.status, "warning")
        self.assertEqual(critical.status, "critical")
        self.assertEqual(overflow.status, "overflow")


class RuntimeGovernanceCompactionSafeguardTests(unittest.TestCase):
    def test_compaction_retains_protected_messages_and_latest_user_input(self) -> None:
        agent = AgentDefinition(
            id="agent-compact",
            name="Compact Agent",
            role="Supervisor",
            instructions="Follow the security policy and ask before risky actions.",
        )
        task = TaskDefinition(
            id="task-compact",
            name="Compact Task",
            description="Complete the governed workflow.",
            instructions="Do not discard approval or error context.",
            agent_id=agent.id,
        )
        workflow = WorkflowDefinition(
            id="workflow-compact",
            name="Compact Workflow",
            entrypoint="node-compact",
            nodes=[
                WorkflowNodeDefinition(
                    id="node-compact",
                    name="Compact Node",
                    node_type="task",
                    task_id=task.id,
                )
            ],
            task_definitions=[task],
            agent_definitions=[agent],
            metadata={
                "runtime_governance": {
                    "context_compaction": {
                        "enabled": True,
                        "preserve_recent_messages": 0,
                        "min_estimated_tokens_saved": 0,
                        "oversized_message_tokens": 50,
                    }
                }
            },
        )
        profile = ModelProfileDefinition(
            id="profile-compact",
            name="Compact Profile",
            provider="fake",
            model="fake-model",
            context_window=300,
            max_tokens=0,
        )
        execution = Execution(
            id="execution-compact",
            workflow_id=workflow.id,
            status=ExecutionStatus.RUNNING,
            runtime_adapter="native",
            input_json={"objective": "ship protected compaction"},
        )
        state = NativeExecutionState(
            execution_id=execution.id,
            workflow_id=workflow.id,
            current_agent_id=agent.id,
            current_task_id=task.id,
        )

        system_message = ModelMessage(role="system", content="Critical system instruction: require approvals.")
        old_assistant = ModelMessage(role="assistant", content="old assistant context " + ("x" * 1200))
        latest_user = ModelMessage(role="user", content="Latest user input must remain raw.")
        pending_approval = ModelMessage(
            role="tool",
            name="desktop_mutation",
            tool_call_id="call-approval",
            content='{"status": "waiting_for_approval", "approval_request_id": "approval-1"}',
        )
        unresolved_error = ModelMessage(
            role="tool",
            name="browser_action",
            tool_call_id="call-error",
            content='{"status": "failed", "error": "selector not found"}',
        )
        compactable_tail = ModelMessage(role="assistant", content="another compactable segment " + ("y" * 1000))
        messages = [
            system_message,
            old_assistant,
            latest_user,
            pending_approval,
            unresolved_error,
            compactable_tail,
        ]
        context_health = estimate_context_health(messages, model_profile=profile)

        result = deterministic_compact_messages(
            workflow=workflow,
            task=task,
            agent=agent,
            profile=profile,
            execution=execution,
            execution_input=execution.input_payload,
            state=state,
            messages=messages,
            context_health=context_health,
            source_model_request_id="request-compact",
        )

        self.assertTrue(result.record.compacted)
        self.assertIn(system_message, result.messages)
        self.assertIn(latest_user, result.messages)
        self.assertIn(pending_approval, result.messages)
        self.assertIn(unresolved_error, result.messages)
        self.assertNotIn(old_assistant, result.messages)
        self.assertNotIn(compactable_tail, result.messages)
        self.assertIn("Protected Context Retained", result.summary)
        self.assertIn("pending_human_decision", result.summary)
        self.assertIn("unresolved_tool_error", result.summary)
        self.assertGreaterEqual(result.record.metadata["protected_message_count"], 4)


class RuntimeGovernanceCompactionPersistencePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_context_pack_persistence_requires_explicit_opt_in(self) -> None:
        context = create_test_api_context()
        agent = AgentDefinition(
            id="agent-persist-policy",
            name="Persist Policy Agent",
            instructions="Compact only when needed.",
        )
        task = TaskDefinition(
            id="task-persist-policy",
            name="Persist Policy Task",
            description="Exercise compaction persistence policy.",
            agent_id=agent.id,
        )
        node = WorkflowNodeDefinition(
            id="node-persist-policy",
            name="Persist Policy Node",
            node_type="task",
            task_id=task.id,
        )
        profile = ModelProfileDefinition(
            id="profile-persist-policy",
            name="Persist Policy Profile",
            provider="fake",
            model="fake-model",
            context_window=300,
            max_tokens=0,
        )
        execution = Execution(
            id="execution-persist-policy",
            workflow_id="workflow-persist-policy",
            status=ExecutionStatus.RUNNING,
            runtime_adapter="native",
            input_json={"prompt": "compact"},
        )
        messages = [
            ModelMessage(role="system", content="Keep policies."),
            ModelMessage(role="assistant", content="compactable " + ("x" * 1400)),
            ModelMessage(role="user", content="latest request"),
        ]
        context_health = estimate_context_health(messages, model_profile=profile)
        state = NativeExecutionState(
            execution_id=execution.id,
            workflow_id=execution.workflow_id,
            current_agent_id=agent.id,
            current_task_id=task.id,
            sequence=5,
        )
        compactor = RuntimeContextCompactor(context)

        default_workflow = WorkflowDefinition(
            id=execution.workflow_id,
            name="Persist Policy Workflow",
            entrypoint=node.id,
            nodes=[node],
            task_definitions=[task],
            agent_definitions=[agent],
            metadata={
                "runtime_governance": {
                    "context_compaction": {
                        "preserve_recent_messages": 0,
                        "min_estimated_tokens_saved": 0,
                    }
                }
            },
        )
        default_result = await compactor(
            default_workflow,
            task,
            agent,
            profile,
            execution,
            execution.input_payload,
            state,
            messages,
            context_health,
            "request-default-persist-policy",
        )
        default_packs = await context.memory_repo.query(
            workflow_id=default_workflow.id,
            source="runtime_context_compaction",
            source_execution_id=execution.id,
            memory_types=[MemoryType.CONTEXT_PACK.value],
        )

        opt_in_workflow = WorkflowDefinition(
            id=execution.workflow_id,
            name="Persist Policy Workflow",
            entrypoint=node.id,
            nodes=[node],
            task_definitions=[task],
            agent_definitions=[agent],
            metadata={
                "runtime_governance": {
                    "context_compaction": {
                        "preserve_recent_messages": 0,
                        "min_estimated_tokens_saved": 0,
                        "persist_context_pack": True,
                    }
                }
            },
        )
        opt_in_result = await compactor(
            opt_in_workflow,
            task,
            agent,
            profile,
            execution,
            execution.input_payload,
            state,
            messages,
            context_health,
            "request-opt-in-persist-policy",
        )
        opt_in_packs = await context.memory_repo.query(
            workflow_id=opt_in_workflow.id,
            source="runtime_context_compaction",
            source_execution_id=execution.id,
            memory_types=[MemoryType.CONTEXT_PACK.value],
        )

        self.assertTrue(default_result.record.compacted)
        self.assertIsNone(default_result.record.memory_id)
        self.assertEqual(default_packs, [])
        self.assertTrue(opt_in_result.record.compacted)
        self.assertIsNotNone(opt_in_result.record.memory_id)
        self.assertEqual(len(opt_in_packs), 1)


if __name__ == "__main__":
    unittest.main()
