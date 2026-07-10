from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.api.streaming.graph_sse import (
    GRAPH_DELTA_EVENT_NAME,
    GRAPH_STREAM_CONNECTED_EVENT_NAME,
    GraphDeltaStreamFilter,
    graph_delta_sse_stream,
)
from app.core.config import reset_settings_cache
from app.core.time import utc_now
from app.domain import GraphProjectionEvent
from app.graph.delta import graph_projection_event_to_delta
from app.graph.service import GRAPH_NEIGHBORHOOD_PRESETS


class _ConnectedRequest:
    headers: dict[str, str] = {}
    query_params: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


def _parse_sse_payload(chunk: str) -> tuple[str | None, str | None, dict]:
    event_name = None
    event_id = None
    data_lines: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("id: "):
            event_id = line.removeprefix("id: ")
        elif line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))
    return event_name, event_id, json.loads("\n".join(data_lines))


class GraphStreamRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_physical_graph_presets_cover_device_audit_relationships(self) -> None:
        device_relationships = set(GRAPH_NEIGHBORHOOD_PRESETS["physical_device"]["relationship_types"])
        audit_relationships = set(GRAPH_NEIGHBORHOOD_PRESETS["physical_audit"]["relationship_types"])

        for relationship in {
            "LOCATED_AT",
            "MANAGES_DEVICE",
            "PRODUCED_DEVICE_EVENT",
            "TRIGGERED_WORKFLOW",
            "STARTED_WORKFLOW_RUN",
        }:
            self.assertIn(relationship, device_relationships)
            self.assertIn(relationship, audit_relationships)

    async def test_memory_delta_links_graph_derived_context_pack_to_provenance_nodes(self) -> None:
        event = GraphProjectionEvent(
            event_type="memory.created",
            aggregate_type="memory",
            aggregate_id="context-pack-1",
            payload={
                "memory_id": "context-pack-1",
                "summary": "Graph context pack",
                "memory_type": "context_pack",
                "graph_working_set_id": "graph-working-set-1",
                "graph_provenance": {
                    "working_set_id": "graph-working-set-1",
                    "anchors": [{"type": "task", "id": "task-1"}],
                    "visited_nodes": [{"id": "decision-1", "type": "Decision"}],
                    "selected_nodes": [{"id": "decision-1", "type": "Decision"}],
                },
            },
            status="projected",
            projected_at=utc_now(),
        )

        delta = graph_projection_event_to_delta(event)
        node_types = {node["type"] for node in delta["upsertNodes"]}
        edge_types = {edge["type"] for edge in delta["upsertEdges"]}

        self.assertIn("GraphWorkingSet", node_types)
        self.assertIn("Decision", node_types)
        self.assertIn("DERIVED_FROM_WORKING_SET", edge_types)
        self.assertIn("DERIVED_FROM_GRAPH_NODE", edge_types)
        self.assertIn("DERIVED_FROM_GRAPH_ANCHOR", edge_types)

    async def test_persona_delta_links_distilled_items_sources_and_package_assets(self) -> None:
        event = GraphProjectionEvent(
            event_type="persona.factory.item.approved",
            aggregate_type="persona",
            aggregate_id="persona-1",
            payload={
                "persona_id": "persona-1",
                "persona_slug": "audit-manager",
                "persona_name": "Audit Manager",
                "run_id": "run-1",
                "item_id": "item-1",
                "source_memory_id": "memory-source-1",
                "item_type": "decision_pattern",
                "memory_layer": "semantic",
                "title": "Audit observation severity",
                "review_status": "approved",
                "tools": [{"id": "jira", "name": "Jira"}],
                "workflows": [{"id": "workflow-audit-review", "name": "Audit Review"}],
                "artifacts": [{"id": "artifact-mlp", "name": "MLP Observation"}],
            },
            status="projected",
            projected_at=utc_now(),
        )

        delta = graph_projection_event_to_delta(event)
        node_types = {node["type"] for node in delta["upsertNodes"]}
        edge_types = {edge["type"] for edge in delta["upsertEdges"]}

        self.assertIn("Persona", node_types)
        self.assertIn("DistillationRun", node_types)
        self.assertIn("DistillationItem", node_types)
        self.assertIn("SourceMemory", node_types)
        self.assertIn("Tool", node_types)
        self.assertIn("Workflow", node_types)
        self.assertIn("Artifact", node_types)
        self.assertIn("PERSONA_HAS_DISTILLATION_RUN", edge_types)
        self.assertIn("RUN_EXTRACTED_ITEM", edge_types)
        self.assertIn("ITEM_DERIVED_FROM_MEMORY", edge_types)
        self.assertIn("PERSONA_USES_TOOL", edge_types)
        self.assertIn("PERSONA_FOLLOWS_WORKFLOW", edge_types)
        self.assertIn("PERSONA_PRODUCES_ARTIFACT", edge_types)

    async def test_graph_delta_stream_emits_projected_outbox_delta(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="execution.started",
            aggregate_type="workflow_run",
            aggregate_id="run-1",
            payload={"execution_id": "run-1", "workflow_id": "workflow-1", "status": "running"},
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            last_event_id=None,
        )

        connected_chunk = await anext(stream)
        connected_event, _, connected_payload = _parse_sse_payload(connected_chunk)
        self.assertEqual(connected_event, GRAPH_STREAM_CONNECTED_EVENT_NAME)
        self.assertEqual(connected_payload["type"], GRAPH_STREAM_CONNECTED_EVENT_NAME)

        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        event_name, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_name, GRAPH_DELTA_EVENT_NAME)
        self.assertEqual(event_id, event.event_id)
        self.assertEqual(payload["metadata"]["eventType"], "execution.started")
        self.assertEqual(payload["upsertNodes"][0]["type"], "WorkflowRun")
        self.assertEqual(payload["upsertEdges"][0]["type"], "HAS_RUN")

    async def test_graph_delta_stream_marks_execution_deletions_without_readding_edges(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="execution.deleted",
            aggregate_type="workflow_run",
            aggregate_id="run-delete",
            payload={"execution_id": "run-delete", "workflow_id": "workflow-1"},
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            last_event_id=None,
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        event_name, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_name, GRAPH_DELTA_EVENT_NAME)
        self.assertEqual(event_id, event.event_id)
        self.assertEqual(payload["metadata"]["eventType"], "execution.deleted")
        self.assertEqual(payload["upsertNodes"][0]["type"], "WorkflowRun")
        self.assertEqual(payload["upsertNodes"][0]["data"]["status"], "deleted")
        self.assertTrue(payload["upsertNodes"][0]["data"]["deleted"])
        self.assertEqual(payload["upsertEdges"], [])

    async def test_graph_delta_stream_resumes_after_last_event_id(self) -> None:
        context = create_test_api_context()
        first = GraphProjectionEvent(
            event_type="memory.created",
            aggregate_type="memory",
            aggregate_id="memory-1",
            payload={"memory_id": "memory-1", "summary": "Old"},
            status="projected",
            projected_at=utc_now(),
        )
        second = GraphProjectionEvent(
            event_type="memory.created",
            aggregate_type="memory",
            aggregate_id="memory-2",
            payload={"memory_id": "memory-2", "summary": "New"},
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(first)
        await context.graph_projection_event_repo.append(second)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            last_event_id=first.event_id,
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, second.event_id)
        self.assertEqual(payload["upsertNodes"][0]["id"], "memory-2")

    async def test_graph_delta_stream_filters_by_execution_id(self) -> None:
        context = create_test_api_context()
        drop = GraphProjectionEvent(
            event_type="execution.started",
            aggregate_type="workflow_run",
            aggregate_id="run-drop",
            payload={"execution_id": "run-drop", "workflow_id": "workflow-1"},
            status="projected",
            projected_at=utc_now(),
        )
        keep = GraphProjectionEvent(
            event_type="task.started",
            aggregate_type="step_run",
            aggregate_id="run-keep:task-1",
            payload={"execution_id": "run-keep", "workflow_id": "workflow-1", "task_id": "task-1"},
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(drop)
        await context.graph_projection_event_repo.append(keep)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(execution_id="run-keep"),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, keep.event_id)
        self.assertEqual(payload["metadata"]["eventType"], "task.started")
        self.assertEqual(payload["upsertNodes"][0]["id"], "run-keep:task-1")

    async def test_graph_delta_stream_treats_task_scoped_execution_failure_as_step_run(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="execution.failed",
            aggregate_type="step_run",
            aggregate_id="run-1:task-a",
            payload={
                "execution_id": "run-1",
                "workflow_id": "workflow-1",
                "task_id": "task-a",
                "status": "failed",
            },
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(execution_id="run-1"),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, event.event_id)
        self.assertEqual(payload["metadata"]["eventType"], "execution.failed")
        self.assertEqual(payload["upsertNodes"][0]["type"], "StepRun")
        self.assertEqual(payload["upsertNodes"][0]["id"], "run-1:task-a")

    async def test_graph_delta_stream_emits_execution_detail_event(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="tool.call.failed",
            aggregate_type="workflow_run",
            aggregate_id="run-1",
            source_event_id="event-tool-1",
            payload={
                "execution_id": "run-1",
                "workflow_id": "workflow-1",
                "sequence": 3,
                "status": "failed",
                "tool_call_id": "tool-call-1",
                "payload": {"tool_name": "read_file"},
            },
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(execution_id="run-1"),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, event.event_id)
        self.assertEqual(payload["metadata"]["eventType"], "tool.call.failed")
        node_types = {node["type"] for node in payload["upsertNodes"]}
        edge_types = {edge["type"] for edge in payload["upsertEdges"]}
        self.assertIn("ExecutionEvent", node_types)
        self.assertIn("ToolCall", node_types)
        self.assertIn("EMITTED_EVENT", edge_types)
        self.assertIn("OCCURRED_IN", edge_types)

    async def test_graph_delta_stream_filters_by_workflow_and_event_type(self) -> None:
        context = create_test_api_context()
        drop_workflow = GraphProjectionEvent(
            event_type="execution.started",
            aggregate_type="workflow_run",
            aggregate_id="run-drop-workflow",
            payload={"execution_id": "run-drop-workflow", "workflow_id": "workflow-drop"},
            status="projected",
            projected_at=utc_now(),
        )
        drop_type = GraphProjectionEvent(
            event_type="task.started",
            aggregate_type="step_run",
            aggregate_id="run-keep:task-drop",
            payload={"execution_id": "run-keep", "workflow_id": "workflow-keep", "task_id": "task-drop"},
            status="projected",
            projected_at=utc_now(),
        )
        keep = GraphProjectionEvent(
            event_type="execution.completed",
            aggregate_type="workflow_run",
            aggregate_id="run-keep",
            payload={"execution_id": "run-keep", "workflow_id": "workflow-keep"},
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(drop_workflow)
        await context.graph_projection_event_repo.append(drop_type)
        await context.graph_projection_event_repo.append(keep)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(
                workflow_id="workflow-keep",
                event_types={"execution.completed"},
            ),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, keep.event_id)
        self.assertEqual(payload["metadata"]["eventType"], "execution.completed")

    async def test_graph_delta_stream_emits_workflow_definition_topology(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="workflow.updated",
            aggregate_type="workflow",
            aggregate_id="workflow-1",
            payload={
                "workflow_id": "workflow-1",
                "name": "Graph Memory Workflow",
                "agents": [
                    {
                        "id": "agent-1",
                        "name": "researcher",
                        "model_profile_id": "model-profile-1",
                        "tool_ids": ["tool-1"],
                        "handoff_agent_ids": ["agent-2"],
                    }
                ],
                "tasks": [
                    {
                        "id": "task-1",
                        "name": "Collect context",
                        "agent_id": "agent-1",
                        "tool_ids": ["tool-1"],
                        "depends_on_task_ids": ["task-0"],
                    }
                ],
                "tools": [{"id": "tool-1", "name": "graph_search"}],
            },
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(workflow_id="workflow-1"),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, event.event_id)
        node_types = {node["type"] for node in payload["upsertNodes"]}
        edge_types = {edge["type"] for edge in payload["upsertEdges"]}
        self.assertIn("Workflow", node_types)
        self.assertIn("Agent", node_types)
        self.assertIn("Task", node_types)
        self.assertIn("Tool", node_types)
        self.assertIn("DEFINES_AGENT", edge_types)
        self.assertIn("DEFINES_TASK", edge_types)
        self.assertIn("DEFINES_TOOL", edge_types)
        self.assertIn("ASSIGNED_TO", edge_types)
        self.assertIn("USES_TOOL", edge_types)
        self.assertIn("CAN_USE", edge_types)
        self.assertIn("CAN_HANDOFF_TO", edge_types)
        self.assertIn("USED_MODEL", edge_types)
        self.assertIn("USES_MODEL_PROFILE", edge_types)
        self.assertIn("DEPENDS_ON", edge_types)

    async def test_graph_delta_stream_emits_model_request_lineage(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="llm.request.created",
            aggregate_type="workflow_run",
            aggregate_id="run-1",
            source_event_id="event-llm-1",
            payload={
                "execution_id": "run-1",
                "workflow_id": "workflow-1",
                "model_request_id": "model-request-1",
                "payload": {"provider": "openai", "model": "gpt-4.1"},
            },
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(execution_id="run-1"),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, event.event_id)
        node_types = {node["type"] for node in payload["upsertNodes"]}
        edge_types = {edge["type"] for edge in payload["upsertEdges"]}
        self.assertIn("ModelRequest", node_types)
        self.assertIn("Model", node_types)
        self.assertIn("ModelProvider", node_types)
        self.assertIn("USED_MODEL", edge_types)
        self.assertIn("USED_PROVIDER", edge_types)

    async def test_graph_delta_stream_emits_observability_nodes(self) -> None:
        context = create_test_api_context()
        event = GraphProjectionEvent(
            event_type="token.budget.exceeded",
            aggregate_type="workflow_run",
            aggregate_id="run-1",
            source_event_id="event-budget-1",
            payload={
                "execution_id": "run-1",
                "workflow_id": "workflow-1",
                "payload": {
                    "budget": {
                        "scope": "run",
                        "status": "exceeded",
                        "action": "compact_context",
                        "used_tokens": 1200,
                        "budget_tokens": 1000,
                        "usage_ratio": 1.2,
                    }
                },
            },
            status="projected",
            projected_at=utc_now(),
        )
        await context.graph_projection_event_repo.append(event)

        stream = graph_delta_sse_stream(
            _ConnectedRequest(),
            event_repository=context.graph_projection_event_repo,
            heartbeat_seconds=1,
            poll_seconds=0.25,
            event_filter=GraphDeltaStreamFilter(execution_id="run-1"),
        )

        await anext(stream)
        delta_chunk = await asyncio.wait_for(anext(stream), timeout=1)
        await stream.aclose()

        _, event_id, payload = _parse_sse_payload(delta_chunk)
        self.assertEqual(event_id, event.event_id)
        node_types = {node["type"] for node in payload["upsertNodes"]}
        edge_types = {edge["type"] for edge in payload["upsertEdges"]}
        self.assertIn("ExecutionEvent", node_types)
        self.assertIn("TokenBudget", node_types)
        self.assertIn("EMITTED_EVENT", edge_types)
        self.assertIn("HAS_BUDGET_SIGNAL", edge_types)

    async def test_physical_device_delta_emits_room_adapter_and_location_edges(self) -> None:
        event = GraphProjectionEvent(
            event_type="physical.device.registered",
            aggregate_type="physical_device",
            aggregate_id="device-light-1",
            payload={
                "device_id": "device-light-1",
                "name": "Kitchen Light",
                "type": "light",
                "vendor": "home_assistant",
                "location_id": "home-main",
                "room": "Kitchen",
                "capabilities": ["turn_on_off"],
                "status": "online",
            },
            status="projected",
            projected_at=utc_now(),
        )

        delta = graph_projection_event_to_delta(event)

        node_types = {node["type"] for node in delta["upsertNodes"]}
        edge_types = {edge["type"] for edge in delta["upsertEdges"]}
        self.assertIn("Device", node_types)
        self.assertIn("Room", node_types)
        self.assertIn("Adapter", node_types)
        self.assertIn("Location", node_types)
        self.assertIn("LOCATED_IN", edge_types)
        self.assertIn("MANAGES_DEVICE", edge_types)
        self.assertIn("LOCATED_AT", edge_types)

    async def test_physical_command_delta_links_actor_memory_and_device(self) -> None:
        event = GraphProjectionEvent(
            event_type="physical.device.command.sent",
            aggregate_type="physical_device_command",
            aggregate_id="command-1",
            payload={
                "command_id": "command-1",
                "device_id": "device-light-1",
                "command_type": "turn_on",
                "context_memory_ids": ["memory-pref-1"],
                "requested_by": "agent:operator",
                "status": "sent",
            },
            status="projected",
            projected_at=utc_now(),
        )

        delta = graph_projection_event_to_delta(event)

        node_types = {node["type"] for node in delta["upsertNodes"]}
        edge_types = {edge["type"] for edge in delta["upsertEdges"]}
        self.assertIn("Device", node_types)
        self.assertIn("DeviceCommand", node_types)
        self.assertIn("Entity", node_types)
        self.assertIn("Memory", node_types)
        self.assertIn("TARGETS_DEVICE", edge_types)
        self.assertIn("REQUESTED_DEVICE_COMMAND", edge_types)
        self.assertIn("INFLUENCED_DEVICE_COMMAND", edge_types)


class GraphStreamApiTests(unittest.TestCase):
    def test_graph_stream_route_is_registered(self) -> None:
        context = create_test_api_context()
        app = create_app(context=context)

        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/graph/stream/deltas", paths)

    def test_graph_stream_route_rejects_unsynced_attached_identity(self) -> None:
        context = create_test_api_context()
        client = TestClient(create_app(context=context))

        response = client.get(
            "/graph/stream/deltas",
            headers={"x-agency-user-email": "missing-graph-stream@example.com"},
        )

        self.assertEqual(response.status_code, 404)

    def test_graph_stream_route_rejects_anonymous_identity_outside_development(self) -> None:
        with patch.dict("os.environ", {"APP_ENV": "production"}, clear=False):
            reset_settings_cache()
            try:
                context = create_test_api_context()
                client = TestClient(create_app(context=context))

                response = client.get("/graph/stream/deltas")

                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"], "Graph stream identity is required")
            finally:
                reset_settings_cache()


if __name__ == "__main__":
    unittest.main()
