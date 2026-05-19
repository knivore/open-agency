from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.runtime.streaming import (
    AGENCY_RUNTIME_EVENT_SCHEMA_VERSION,
    RuntimeEventActor,
    RuntimeEventLevel,
    RuntimeEventSourceType,
    RuntimeEventTask,
    RuntimeEventType,
    RuntimeEventWorkflow,
    RuntimeStreamEvent,
)


class RuntimeStreamingModelTests(unittest.TestCase):
    def test_runtime_event_serializes_to_frontend_contract(self):
        event = RuntimeStreamEvent(
            id="evt:agency:task-progress",
            source="agency-runtime",
            sourceType="agency",
            type=RuntimeEventType.TASK_PROGRESS,
            timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
            actor=RuntimeEventActor(id="agent:research", name="Research Agent", avatarAssetId="agent-avatar"),
            workflow=RuntimeEventWorkflow(id="workflow:demo", name="Demo workflow", roomId="room:runtime-floor"),
            task=RuntimeEventTask(id="task:research", title="Research", progress=0.42),
            level=RuntimeEventLevel.INFO,
            message="Research in progress",
            metadata={"visualAction": "reading", "attempt": 2},
        )

        payload = event.to_external_event()

        self.assertEqual(payload["schemaVersion"], AGENCY_RUNTIME_EVENT_SCHEMA_VERSION)
        self.assertEqual(payload["sourceType"], "agency")
        self.assertEqual(payload["type"], "task_progress")
        self.assertEqual(payload["actor"]["avatarAssetId"], "agent-avatar")
        self.assertEqual(payload["workflow"]["roomId"], "room:runtime-floor")
        self.assertEqual(payload["task"]["progress"], 0.42)
        self.assertEqual(payload["timestamp"], "2026-05-10T12:00:00Z")

    def test_runtime_event_constants_match_agency_contract(self):
        self.assertEqual(AGENCY_RUNTIME_EVENT_SCHEMA_VERSION, "agency.runtime-event.v1")
        self.assertEqual(
            {item.value for item in RuntimeEventSourceType},
            {"agency", "hermes", "claude_code", "codex", "custom", "local"},
        )
        self.assertEqual(
            {item.value for item in RuntimeEventLevel},
            {"debug", "info", "warning", "error", "success"},
        )
        self.assertIn("agent_status_changed", {item.value for item in RuntimeEventType})
        self.assertIn("workflow_transitioned", {item.value for item in RuntimeEventType})

    def test_runtime_event_rejects_non_json_metadata(self):
        with self.assertRaises(ValidationError):
            RuntimeStreamEvent(
                type=RuntimeEventType.LOG_RECEIVED,
                metadata={"bad": object()},
            )

    def test_runtime_event_rejects_out_of_range_progress(self):
        with self.assertRaises(ValidationError):
            RuntimeStreamEvent(
                type=RuntimeEventType.TASK_PROGRESS,
                task=RuntimeEventTask(id="task:bad", progress=1.2),
            )

    def test_runtime_event_round_trips_from_json_payload(self):
        event = RuntimeStreamEvent(
            id="evt:codex:tool",
            source="codex",
            sourceType="codex",
            type="tool_started",
            timestamp=datetime(2026, 5, 10, 12, 5, tzinfo=timezone.utc),
            actor={"id": "agent:codex"},
            workflow={"id": "workflow:demo", "roomId": "room:codex"},
            task={"id": "task:test", "progress": 0},
            level="debug",
        )

        round_trip = RuntimeStreamEvent.model_validate(event.to_external_event())

        self.assertEqual(round_trip.source_type, RuntimeEventSourceType.CODEX)
        self.assertEqual(round_trip.type, RuntimeEventType.TOOL_STARTED)
        self.assertEqual(round_trip.workflow.room_id, "room:codex")
        self.assertEqual(round_trip.task.progress, 0)


if __name__ == "__main__":
    unittest.main()
