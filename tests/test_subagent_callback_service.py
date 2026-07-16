from __future__ import annotations

import unittest

from app.domain import Execution, ExecutionEventType, ExecutionStatus, TaskDefinition, WorkflowDefinition
from app.runtime.callbacks.callback_service import SubAgentCallbackService
from app.runtime.native.errors import ExecutionNotFoundError
from app.runtime.native.state import InMemoryExecutionStore


class RecordingDispatcher:
    def __init__(self) -> None:
        self.actions: list[tuple[str, str]] = []

    async def pause(self, execution_id: str):
        self.actions.append(("pause", execution_id))

    async def resume(self, execution_id: str):
        self.actions.append(("resume", execution_id))


class FakeWorkflowRepository:
    def __init__(self, workflows):
        self.workflows = {workflow.id: workflow for workflow in workflows}

    async def get_workflow(self, workflow_id: str):
        return self.workflows.get(workflow_id)


class RecordingWebhookClient:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    async def send(self, **kwargs):
        self.sends.append(kwargs)
        return {"ok": True}


class SubAgentCallbackServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryExecutionStore()
        self.dispatcher = RecordingDispatcher()
        self.service = SubAgentCallbackService(execution_store=self.store, dispatcher=self.dispatcher)
        await self.store.save_execution(
            Execution(
                id="execution-1",
                workflow_id="workflow-1",
                runtime_adapter_id="native",
                status=ExecutionStatus.RUNNING,
                input_payload={},
            )
        )

    async def test_progress_callback_records_event_and_checkpoint(self) -> None:
        receipt = await self.service.record_subagent_progress(
            run_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"message": "working", "percent": 40},
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")

        self.assertTrue(receipt.ok)
        self.assertEqual(receipt.status, "recorded")
        self.assertEqual(receipt.event_id, events[0].id)
        self.assertEqual(events[0].event_type, ExecutionEventType.SUBAGENT_PROGRESS_UPDATED)
        self.assertEqual(events[0].source, "subagent:agent-1")
        self.assertEqual(events[0].status, "running")
        self.assertIsNotNone(events[0].payload_sha256)
        self.assertEqual(execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]["status"], "running")
        recovery = events[0].payload["supervisor_recovery"]
        self.assertEqual(recovery["run_id"], "execution-1")
        self.assertEqual(recovery["workflow_id"], "workflow-1")
        self.assertEqual(recovery["agent_id"], "agent-1")
        self.assertEqual(recovery["step_id"], "task-1")
        self.assertEqual(recovery["status"], "running")
        self.assertEqual(recovery["payload_keys"], ["message", "percent"])
        checkpoint_recovery = execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]["supervisor_recovery"]
        self.assertEqual(checkpoint_recovery["event_id"], events[0].id)
        self.assertEqual(checkpoint_recovery["payload_sha256"], events[0].payload_sha256)
        self.assertEqual(self.dispatcher.actions, [])

    async def test_progress_callback_normalizes_structured_status_update(self) -> None:
        await self.service.record_subagent_progress(
            run_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={
                "status": "blocked",
                "current_task": "Validate rollout plan",
                "blocker": "Missing production window",
                "confidence": 1.5,
                "progress_percent": 125,
                "token_usage": {
                    "provider": "fake",
                    "model": "fake-model",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
                "context_health": {
                    "estimated_prompt_tokens": 100,
                    "reserved_completion_tokens": 20,
                    "estimated_total_context_tokens": 120,
                    "context_window": 1000,
                    "remaining_context_tokens": 880,
                    "usage_ratio": 0.12,
                    "status": "normal",
                },
            },
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")
        event_payload = events[-1].payload
        checkpoint = execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]

        self.assertEqual(event_payload["status"], "blocked")
        self.assertEqual(event_payload["current_task"], "Validate rollout plan")
        self.assertEqual(event_payload["blocker"], "Missing production window")
        self.assertEqual(event_payload["confidence"], 1.0)
        self.assertEqual(event_payload["progress_percent"], 100.0)
        self.assertEqual(event_payload["token_usage"]["total_tokens"], 15)
        self.assertEqual(event_payload["context_health"]["status"], "normal")
        self.assertEqual(event_payload["status_update"]["status"], "blocked")
        self.assertEqual(event_payload["supervisor_recovery"]["subagent_status"], "blocked")
        self.assertEqual(event_payload["supervisor_recovery"]["current_task"], "Validate rollout plan")
        self.assertEqual(event_payload["supervisor_recovery"]["progress_percent"], 100.0)
        self.assertEqual(event_payload["supervisor_recovery"]["token_usage"]["total_tokens"], 15)
        self.assertEqual(checkpoint["status"], "running")
        self.assertEqual(checkpoint["subagent_status"], "blocked")
        self.assertEqual(checkpoint["current_task"], "Validate rollout plan")
        self.assertEqual(checkpoint["blocker"], "Missing production window")
        self.assertEqual(checkpoint["confidence"], 1.0)
        self.assertEqual(checkpoint["progress_percent"], 100.0)
        self.assertEqual(checkpoint["token_usage"]["total_tokens"], 15)
        self.assertEqual(checkpoint["context_health"]["status"], "normal")
        self.assertEqual(checkpoint["supervisor_recovery"]["subagent_status"], "blocked")
        self.assertEqual(checkpoint["supervisor_recovery"]["context_health"]["status"], "normal")

    async def test_progress_callback_records_previous_checkpoint_for_supervisor_recovery(self) -> None:
        await self.service.record_subagent_status(
            run_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            step_id="task-1",
            status="working",
            current_task="Draft plan",
            progress_percent=25,
        )

        await self.service.record_subagent_status(
            run_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            step_id="task-1",
            status="blocked",
            current_task="Validate plan",
            blocker="Needs approval",
            progress_percent=50,
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")
        recovery = events[-1].payload["supervisor_recovery"]
        checkpoint_recovery = execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]["supervisor_recovery"]

        self.assertEqual(recovery["previous_checkpoint"]["subagent_status"], "working")
        self.assertEqual(recovery["previous_checkpoint"]["current_task"], "Draft plan")
        self.assertEqual(recovery["previous_checkpoint"]["progress_percent"], 25.0)
        self.assertEqual(checkpoint_recovery["previous_checkpoint"]["subagent_status"], "working")
        self.assertEqual(checkpoint_recovery["blocker"], "Needs approval")

    async def test_record_subagent_status_helper_writes_structured_progress(self) -> None:
        await self.service.record_subagent_status(
            run_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            step_id="task-1",
            status="working",
            current_task="Compare candidate outputs",
            completed_step="Loaded execution trace",
            confidence=0.7,
            progress_percent=45,
            next_action="Summarize findings",
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")
        checkpoint = execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]

        self.assertEqual(events[-1].event_type, ExecutionEventType.SUBAGENT_PROGRESS_UPDATED)
        self.assertEqual(events[-1].payload["status"], "working")
        self.assertEqual(events[-1].payload["completed_step"], "Loaded execution trace")
        self.assertEqual(checkpoint["subagent_status"], "working")
        self.assertEqual(checkpoint["next_action"], "Summarize findings")
        self.assertEqual(checkpoint["progress_percent"], 45.0)

    async def test_completion_callback_resumes_dispatcher(self) -> None:
        receipt = await self.service.record_subagent_completed(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"result": "done"},
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")

        self.assertEqual(events[-1].event_type, ExecutionEventType.SUBAGENT_STEP_COMPLETED)
        self.assertEqual(execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]["status"], "completed")
        self.assertEqual(self.dispatcher.actions, [("resume", "execution-1")])
        self.assertEqual(receipt.step_id, "task-1")

    async def test_failure_callback_records_failed_checkpoint_without_dispatcher_resume(self) -> None:
        await self.service.record_subagent_failed(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"error": "tool failed"},
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")

        self.assertEqual(events[-1].event_type, ExecutionEventType.SUBAGENT_STEP_FAILED)
        self.assertEqual(events[-1].status, "failed")
        self.assertEqual(execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]["status"], "failed")
        self.assertEqual(self.dispatcher.actions, [])

    async def test_failure_callback_applies_retry_policy_and_resumes_dispatcher(self) -> None:
        await self.service.record_subagent_failed(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"error": "tool failed", "retry_policy": {"max_retries": 2}},
        )

        execution = await self.store.get_execution("execution-1")
        checkpoint = execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]

        self.assertEqual(checkpoint["status"], "retry_queued")
        self.assertEqual(checkpoint["retry"]["attempts"], 1)
        self.assertEqual(checkpoint["retry"]["max_retries"], 2)
        self.assertTrue(checkpoint["retry"]["retry_available"])
        self.assertEqual(self.dispatcher.actions, [("resume", "execution-1")])

    async def test_completion_records_ready_dependent_steps_from_workflow(self) -> None:
        workflow = WorkflowDefinition(
            id="workflow-1",
            name="Workflow",
            entrypoint="task-1",
            task_definitions=[
                TaskDefinition(id="task-1", name="First", description="First"),
                TaskDefinition(id="task-2", name="Second", description="Second", depends_on_task_ids=["task-1"]),
            ],
        )
        service = SubAgentCallbackService(
            execution_store=self.store,
            dispatcher=self.dispatcher,
            workflow_repository=FakeWorkflowRepository([workflow]),
        )

        await service.record_subagent_completed(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"result": "done"},
        )

        execution = await self.store.get_execution("execution-1")
        checkpoint = execution.metadata["runtime_callbacks"]["checkpoints"]["task-1"]

        self.assertEqual(checkpoint["status"], "completed")
        self.assertEqual(checkpoint["ready_dependent_step_ids"], ["task-2"])
        self.assertEqual(self.dispatcher.actions, [("resume", "execution-1")])

    async def test_needs_input_marks_execution_waiting_and_pauses_dispatcher(self) -> None:
        await self.service.record_subagent_needs_input(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"question": "Which region should I deploy to?"},
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")

        self.assertEqual(events[-2].event_type, ExecutionEventType.SUBAGENT_NEEDS_INPUT)
        self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_WAITING)
        self.assertEqual(execution.status, ExecutionStatus.WAITING_FOR_INPUT)
        self.assertEqual(execution.metadata["pending_subagent_input"]["status"], "needs_input")
        waits = await self.store.list_execution_waits("execution-1")
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].kind.value, "input")
        self.assertEqual(execution.metadata["active_wait"]["wait_id"], waits[0].id)
        self.assertEqual(self.dispatcher.actions, [("pause", "execution-1")])

    async def test_needs_input_can_send_optional_outbound_webhook(self) -> None:
        webhook_client = RecordingWebhookClient()
        service = SubAgentCallbackService(
            execution_store=self.store,
            dispatcher=self.dispatcher,
            webhook_client=webhook_client,
        )

        await service.record_subagent_needs_input(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={
                "question": "Which region?",
                "outbound_webhook_target": "discord_ops",
            },
        )

        self.assertEqual(len(webhook_client.sends), 1)
        send = webhook_client.sends[0]
        self.assertEqual(send["target"], "discord_ops")
        self.assertEqual(send["event_type"], "subagent.needs_input")
        self.assertEqual(send["payload"]["status"], "needs_input")
        self.assertEqual(send["run_id"], "execution-1")

    async def test_needs_approval_marks_execution_waiting_for_approval(self) -> None:
        await self.service.record_subagent_needs_approval(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"approval_type": "deploy"},
        )

        events = await self.store.list_events("execution-1")
        execution = await self.store.get_execution("execution-1")

        self.assertEqual(events[-2].event_type, ExecutionEventType.SUBAGENT_NEEDS_APPROVAL)
        self.assertEqual(events[-1].event_type, ExecutionEventType.EXECUTION_WAITING)
        self.assertEqual(execution.status, ExecutionStatus.WAITING_FOR_APPROVAL)
        self.assertEqual(execution.metadata["pending_subagent_approval"]["status"], "needs_approval")
        waits = await self.store.list_execution_waits("execution-1")
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].kind.value, "approval")
        self.assertEqual(self.dispatcher.actions, [("pause", "execution-1")])

    async def test_metadata_configured_webhook_target_is_used_for_approval(self) -> None:
        execution = await self.store.get_execution("execution-1")
        execution.metadata = {
            "runtime_callbacks": {
                "outbound_webhooks": {
                    "subagent.needs_approval": {"target": "approval_ops"},
                }
            }
        }
        await self.store.update_execution(execution)
        webhook_client = RecordingWebhookClient()
        service = SubAgentCallbackService(
            execution_store=self.store,
            dispatcher=self.dispatcher,
            webhook_client=webhook_client,
        )

        await service.record_subagent_needs_approval(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"approval_type": "deploy"},
        )

        self.assertEqual(webhook_client.sends[0]["target"], "approval_ops")
        self.assertEqual(webhook_client.sends[0]["event_type"], "subagent.needs_approval")

    async def test_idempotency_key_returns_existing_receipt_without_duplicate_event(self) -> None:
        first = await self.service.record_subagent_completed(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"result": "done"},
            idempotency_key="execution-1:task-1:completed",
        )
        second = await self.service.record_subagent_completed(
            run_id="execution-1",
            agent_id="agent-1",
            step_id="task-1",
            payload={"result": "done again"},
            idempotency_key="execution-1:task-1:completed",
        )

        events = await self.store.list_events("execution-1")

        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.dispatcher.actions, [("resume", "execution-1")])

    async def test_unknown_execution_raises(self) -> None:
        with self.assertRaises(ExecutionNotFoundError):
            await self.service.record_subagent_progress(
                run_id="missing-execution",
                agent_id="agent-1",
                step_id="task-1",
                payload={},
            )

    async def test_required_identifiers_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            await self.service.record_subagent_progress(
                run_id="execution-1",
                agent_id=" ",
                step_id="task-1",
                payload={},
            )


if __name__ == "__main__":
    unittest.main()
