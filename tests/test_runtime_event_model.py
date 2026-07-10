from __future__ import annotations

import unittest

from app.domain import ExecutionEventType
from app.runtime.events.factory import RuntimeEventEnvelope, RuntimeEventStatus, create_execution_event_from_runtime_event
from app.runtime.events.payloads import payload_sha256
from app.runtime.native.state import _event_from_orm, _event_to_orm


class RuntimeEventModelTests(unittest.TestCase):
    def test_phase_two_event_types_are_registered(self) -> None:
        required_values = {
            "workflow.started",
            "workflow.completed",
            "workflow.failed",
            "agent.step.started",
            "agent.step.completed",
            "agent.step.failed",
            "subagent.task.assigned",
            "subagent.progress.updated",
            "subagent.step.completed",
            "subagent.step.failed",
            "subagent.needs_input",
            "subagent.needs_approval",
            "token.usage.recorded",
            "token.budget.warning",
            "token.budget.exceeded",
            "context.health.recorded",
            "context.compaction.started",
            "context.compaction.completed",
            "context.compaction.failed",
            "outbound_webhook.queued",
            "outbound_webhook.sent",
            "outbound_webhook.failed",
        }

        registered_values = {event_type.value for event_type in ExecutionEventType}

        self.assertTrue(required_values.issubset(registered_values))

    def test_payload_sha256_is_canonical_and_hex_encoded(self) -> None:
        first = payload_sha256({"b": 2, "a": {"z": 1, "y": [3, 2, 1]}})
        second = payload_sha256({"a": {"y": [3, 2, 1], "z": 1}, "b": 2})

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_runtime_event_envelope_maps_to_execution_event(self) -> None:
        envelope = RuntimeEventEnvelope(
            event_id="event-1",
            event_type=ExecutionEventType.SUBAGENT_PROGRESS_UPDATED,
            run_id="execution-1",
            workflow_id="workflow-1",
            agent_id="agent-1",
            step_id="task-1",
            source="subagent:agent-1",
            status=RuntimeEventStatus.RUNNING,
            payload={"message": "halfway done", "percent": 50},
        )

        event = create_execution_event_from_runtime_event(envelope, trace_id="trace-1")

        self.assertEqual(event.id, "event-1")
        self.assertEqual(event.event_type, ExecutionEventType.SUBAGENT_PROGRESS_UPDATED)
        self.assertEqual(event.execution_id, "execution-1")
        self.assertEqual(event.task_id, "task-1")
        self.assertEqual(event.source, "subagent:agent-1")
        self.assertEqual(event.status, "running")
        self.assertEqual(event.payload_sha256, payload_sha256(envelope.payload))
        self.assertEqual(event.metadata["runtime_event"], True)
        self.assertEqual(event.metadata["run_id"], "execution-1")
        self.assertEqual(event.metadata["step_id"], "task-1")

    def test_runtime_event_metadata_survives_sql_json_round_trip(self) -> None:
        envelope = RuntimeEventEnvelope(
            event_id="event-webhook-queued",
            event_type=ExecutionEventType.OUTBOUND_WEBHOOK_QUEUED,
            run_id="execution-1",
            workflow_id="workflow-1",
            source="webhook_client",
            status=RuntimeEventStatus.QUEUED,
            payload={"target": "discord_ops"},
        )
        event = create_execution_event_from_runtime_event(envelope)

        restored = _event_from_orm(_event_to_orm(event))

        self.assertEqual(restored.id, event.id)
        self.assertEqual(restored.event_type, ExecutionEventType.OUTBOUND_WEBHOOK_QUEUED)
        self.assertEqual(restored.source, "webhook_client")
        self.assertEqual(restored.status, "queued")
        self.assertEqual(restored.payload, {"target": "discord_ops"})
        self.assertEqual(restored.payload_sha256, payload_sha256({"target": "discord_ops"}))
        self.assertEqual(restored.metadata["payload_sha256"], restored.payload_sha256)

    def test_runtime_event_requires_run_id_and_source(self) -> None:
        with self.assertRaises(ValueError):
            RuntimeEventEnvelope(
                event_type=ExecutionEventType.SUBAGENT_STEP_COMPLETED,
                run_id=" ",
                source="subagent:agent-1",
                status=RuntimeEventStatus.COMPLETED,
            )

        with self.assertRaises(ValueError):
            RuntimeEventEnvelope(
                event_type=ExecutionEventType.SUBAGENT_STEP_COMPLETED,
                run_id="execution-1",
                source=" ",
                status=RuntimeEventStatus.COMPLETED,
            )


if __name__ == "__main__":
    unittest.main()
