from __future__ import annotations

import unittest

from app.graph.neo4j_read import GraphReadConfig, Neo4jGraphReader


class FakeNode:
    def __init__(self, element_id: str, labels: list[str], properties: dict):
        self.element_id = element_id
        self.labels = set(labels)
        self._properties = properties

    def __iter__(self):
        return iter(self._properties.items())


class FakeRelationship:
    def __init__(self, element_id: str, rel_type: str, source: FakeNode, target: FakeNode, properties: dict):
        self.element_id = element_id
        self.type = rel_type
        self.start_node = source
        self.end_node = target
        self._properties = properties

    def __iter__(self):
        return iter(self._properties.items())


class FakePath:
    def __init__(self, nodes: list[FakeNode], relationships: list[FakeRelationship]):
        self.nodes = nodes
        self.relationships = relationships


class FakeAsyncResult:
    def __init__(self, records: list[dict]):
        self.records = records

    def __aiter__(self):
        self._iter = iter(self.records)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class FakeNeo4jReadSession:
    def __init__(self, driver: "FakeNeo4jReadDriver"):
        self.driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, exc, _tb):
        return None

    async def run(self, query: str, parameters: dict | None = None, **params):
        self.driver.calls.append({"cypher": query, "params": {**(parameters or {}), **params}})
        return FakeAsyncResult(self.driver.records)


class FakeNeo4jReadDriver:
    def __init__(self, records: list[dict]):
        self.records = records
        self.calls: list[dict] = []
        self.session_kwargs: list[dict] = []
        self.closed = False

    def session(self, **kwargs):
        self.session_kwargs.append(kwargs)
        return FakeNeo4jReadSession(self)

    async def close(self):
        self.closed = True


class Neo4jGraphReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_neighborhood_returns_neutral_graph_document(self) -> None:
        workflow = FakeNode(
            "node-1",
            ["Workflow"],
            {
                "id": "workflow-1",
                "name": "Research",
                "content": "redacted",
                "accessToken": "secret-token",
                "token_count": 42,
                "details": {
                    "secretRef": "secret://graph",
                    "token_count": 7,
                },
            },
        )
        run = FakeNode("node-2", ["WorkflowRun"], {"id": "run-1", "status": "completed"})
        relationship = FakeRelationship(
            "rel-1",
            "HAS_RUN",
            workflow,
            run,
            {
                "started_at": "2026-05-24T00:00:00+00:00",
                "token": "redacted",
                "refresh_token": "refresh-secret",
                "token_count": 3,
            },
        )
        driver = FakeNeo4jReadDriver([{"p": FakePath([workflow, run], [relationship])}])
        reader = Neo4jGraphReader(driver, config=GraphReadConfig(database="neo4j"))

        document = await reader.get_neighborhood(
            "workflow-1",
            labels=["Workflow"],
            relationship_types=["HAS_RUN"],
            depth=2,
            limit=25,
        )

        payload = document.to_dict()
        self.assertEqual(payload["meta"]["query"], "neighborhood")
        self.assertEqual(payload["meta"]["depth"], 2)
        self.assertEqual(driver.session_kwargs[0], {"database": "neo4j"})
        self.assertIn("[:HAS_RUN*1..2]", driver.calls[0]["cypher"])
        self.assertEqual(len(payload["nodes"]), 2)
        self.assertEqual(len(payload["edges"]), 1)
        self.assertEqual(payload["nodes"][0]["id"], "workflow-1")
        self.assertNotIn("content", payload["nodes"][0]["properties"])
        self.assertNotIn("accessToken", payload["nodes"][0]["properties"])
        self.assertEqual(payload["nodes"][0]["properties"]["token_count"], 42)
        self.assertEqual(payload["nodes"][0]["properties"]["details"], {"token_count": 7})
        self.assertEqual(payload["edges"][0]["source"], "workflow-1")
        self.assertEqual(payload["edges"][0]["target"], "run-1")
        self.assertNotIn("token", payload["edges"][0]["properties"])
        self.assertNotIn("refresh_token", payload["edges"][0]["properties"])
        self.assertEqual(payload["edges"][0]["properties"]["token_count"], 3)

    async def test_search_nodes_bounds_limit_and_returns_nodes(self) -> None:
        memory = FakeNode("node-1", ["Memory"], {"id": "memory-1", "summary": "Important"})
        driver = FakeNeo4jReadDriver([{"n": memory}])
        reader = Neo4jGraphReader(driver)

        payload = (await reader.search_nodes("Important", labels=["Memory"], limit=5000)).to_dict()

        self.assertEqual(payload["meta"]["limit"], 1000)
        self.assertEqual(payload["nodes"][0]["type"], "Memory")
        self.assertEqual(driver.calls[0]["params"]["query"], "important")

    async def test_search_nodes_accepts_canonical_filters(self) -> None:
        error = FakeNode("node-1", ["Error"], {"id": "error-1", "message": "Tool timeout"})
        driver = FakeNeo4jReadDriver([{"n": error}])
        reader = Neo4jGraphReader(driver)

        payload = (
            await reader.search_nodes(
                node_types=["Error"],
                workflow_id="workflow-1",
                agent_id="agent-1",
                tool_id="tool-1",
                document_id="doc-1",
                entity_id="entity:organization:acme-corp",
                error_text="timeout",
                limit=25,
            )
        ).to_dict()

        self.assertEqual(payload["meta"]["node_types"], ["Error"])
        self.assertEqual(payload["meta"]["workflow_id"], "workflow-1")
        self.assertEqual(payload["nodes"][0]["type"], "Error")
        cypher = driver.calls[0]["cypher"]
        self.assertIn("label IN ['Error']", cypher)
        self.assertIn("n:Error", cypher)
        self.assertIn("MATCH (n)-[workflowRel*1..2]-(:Workflow", cypher)
        self.assertIn("MATCH (n)-[agentRel*1..2]-(:Agent", cypher)
        self.assertIn("MATCH (n)-[toolRel*1..2]-(:Tool", cypher)
        self.assertIn("MATCH (n)-[documentRel*1..2]-(:Document", cypher)
        self.assertIn("MATCH (n)-[entityRel*1..2]-(:Entity", cypher)
        params = driver.calls[0]["params"]
        self.assertEqual(params["error_text"], "timeout")
        self.assertEqual(params["workflow_id"], "workflow-1")

    async def test_search_nodes_applies_reader_access_context(self) -> None:
        memory = FakeNode("node-1", ["Memory"], {"id": "memory-1", "summary": "Important"})
        driver = FakeNeo4jReadDriver([{"n": memory}])
        reader = Neo4jGraphReader(driver)
        reader.access_user_id = "user-1"

        await reader.search_nodes("Important", labels=["Memory"], limit=25)

        cypher = driver.calls[0]["cypher"]
        params = driver.calls[0]["params"]
        self.assertEqual(params["access_user_id"], "user-1")
        self.assertIn("n.created_by_user_id = $access_user_id", cypher)
        self.assertIn("n.owner_user_id = $access_user_id", cypher)

    async def test_search_nodes_rejects_empty_unscoped_search(self) -> None:
        reader = Neo4jGraphReader(FakeNeo4jReadDriver([]))

        with self.assertRaises(ValueError):
            await reader.search_nodes(limit=25)

    async def test_shortest_path_returns_bounded_path_document(self) -> None:
        source = FakeNode("node-1", ["Memory"], {"id": "memory-1", "summary": "Important"})
        target = FakeNode("node-2", ["WorkflowRun"], {"id": "run-1", "status": "failed"})
        relationship = FakeRelationship("rel-1", "SOURCE_EXECUTION", source, target, {"token": "redacted"})
        driver = FakeNeo4jReadDriver([{"p": FakePath([source, target], [relationship])}])
        reader = Neo4jGraphReader(driver)

        payload = (
            await reader.get_shortest_path(
                "memory-1",
                "run-1",
                relationship_types=["SOURCE_EXECUTION"],
                max_depth=10,
                limit=5,
            )
        ).to_dict()

        self.assertEqual(payload["meta"]["query"], "shortest_path")
        self.assertEqual(payload["meta"]["max_depth"], 4)
        self.assertEqual(payload["nodes"][0]["id"], "memory-1")
        self.assertEqual(payload["edges"][0]["type"], "SOURCE_EXECUTION")
        self.assertNotIn("token", payload["edges"][0]["properties"])
        self.assertIn("shortestPath((source)-[:SOURCE_EXECUTION*1..4]-(target))", driver.calls[0]["cypher"])

    async def test_specialized_path_queries_use_bounded_allowlisted_relationships(self) -> None:
        memory = FakeNode("node-1", ["Memory"], {"id": "memory-1"})
        run = FakeNode("node-2", ["WorkflowRun"], {"id": "run-1", "status": "failed"})
        relationship = FakeRelationship("rel-1", "SOURCE_EXECUTION", memory, run, {})

        driver = FakeNeo4jReadDriver([{"p": FakePath([memory, run], [relationship])}])
        reader = Neo4jGraphReader(driver)

        memory_path = await reader.get_memory_source_run_path("memory-1", run_id="run-1", max_depth=9, limit=5000)
        failed_path = await reader.get_failed_run_root_cause_path("run-1", max_depth=3, limit=5)
        influence_path = await reader.get_influence_path("entity-1", anchor_type="entity", workflow_id="workflow-1")
        agent_path = await reader.get_agent_prior_runs_path("agent-1", run_id="run-1")

        self.assertEqual(memory_path.meta["query"], "memory_source_run_path")
        self.assertEqual(memory_path.meta["max_depth"], 4)
        self.assertEqual(memory_path.meta["limit"], 1000)
        self.assertEqual(failed_path.meta["query"], "failed_run_root_cause_path")
        self.assertEqual(influence_path.meta["query"], "influence_path")
        self.assertEqual(influence_path.meta["anchor_type"], "entity")
        self.assertEqual(agent_path.meta["query"], "agent_prior_runs_path")
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("SOURCE_EXECUTION", calls)
        self.assertIn("FAILED_WITH", calls)
        self.assertIn("MATCH (anchor:Entity {id: $anchor_id})", calls)
        self.assertIn("MATCH (anchor:Agent {id: $anchor_id})", calls)

    async def test_influence_path_rejects_unknown_anchor_type(self) -> None:
        reader = Neo4jGraphReader(FakeNeo4jReadDriver([]))

        with self.assertRaises(ValueError):
            await reader.get_influence_path("memory-1", anchor_type="memory")

    async def test_graph_preset_queries_named_templates(self) -> None:
        run = FakeNode("node-1", ["WorkflowRun"], {"id": "run-1", "status": "failed"})
        error = FakeNode("node-2", ["Error"], {"id": "error-1", "message": "failed"})
        relationship = FakeRelationship("rel-1", "FAILED_WITH", run, error, {})
        driver = FakeNeo4jReadDriver([{"p": FakePath([run, error], [relationship]), "run": run, "n": run}])
        reader = Neo4jGraphReader(driver)

        recent = await reader.get_graph_preset("recent_failures", workflow_id="workflow-1", limit=10)
        stale = await reader.get_graph_preset("stale_context", workflow_id="workflow-1", limit=10)
        missing = await reader.get_graph_preset("missing_embeddings", workflow_id="workflow-1", limit=10)
        high_cost = await reader.get_graph_preset("high_cost_runs", workflow_id="workflow-1", limit=10)
        tool_hotspots = await reader.get_graph_preset("tool_failure_hotspots", tool_id="tool-1", limit=10)
        coding_resume = await reader.get_graph_preset("coding_agent_resume", workflow_id="workflow-1", agent_id="agent-1", limit=10)
        persona_lineage = await reader.get_graph_preset("persona_lineage", persona_id="persona-1", limit=10)
        persona_capability_map = await reader.get_graph_preset("persona_capability_map", persona_id="persona-1", limit=10)
        physical_device_audit = await reader.get_graph_preset(
            "physical_device_audit",
            device_id="device-light-1",
            limit=10,
        )
        physical_room_context = await reader.get_graph_preset(
            "physical_room_context",
            room="Living Room",
            limit=10,
        )

        self.assertEqual(recent.meta["preset"], "recent_failures")
        self.assertEqual(stale.meta["preset"], "stale_context")
        self.assertEqual(missing.meta["preset"], "missing_embeddings")
        self.assertEqual(high_cost.meta["preset"], "high_cost_runs")
        self.assertEqual(tool_hotspots.meta["preset"], "tool_failure_hotspots")
        self.assertEqual(coding_resume.meta["preset"], "coding_agent_resume")
        self.assertEqual(persona_lineage.meta["preset"], "persona_lineage")
        self.assertEqual(persona_capability_map.meta["preset"], "persona_capability_map")
        self.assertEqual(physical_device_audit.meta["preset"], "physical_device_audit")
        self.assertEqual(physical_room_context.meta["preset"], "physical_room_context")
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("WorkflowRun", calls)
        self.assertIn("ContextHealth", calls)
        self.assertIn("missing_embedding", calls)
        self.assertIn("estimated_cost", calls)
        self.assertIn("Tool", calls)
        self.assertIn("Agent", calls)
        self.assertIn("Persona", calls)
        self.assertIn("PERSONA_HAS_DISTILLATION_RUN", calls)
        self.assertIn("PERSONA_USES_TOOL", calls)
        self.assertIn("MENTIONS", calls)
        self.assertIn("PRODUCES", calls)
        self.assertIn("MATCH (device:Device {id: $device_id})", calls)
        self.assertIn("MATCH (room:Room)", calls)
        self.assertIn("INFLUENCED_DEVICE_COMMAND", calls)

    async def test_persona_presets_surface_approved_llm_graph_hint_paths(self) -> None:
        persona = FakeNode("persona-node", ["Persona"], {"id": "persona-1", "name": "Audit Persona"})
        item = FakeNode("item-node", ["DistillationItem"], {"id": "item-1", "review_status": "approved"})
        decision = FakeNode("decision-node", ["Entity", "Decision"], {"id": "decision-1", "name": "Escalation"})
        item_rel = FakeRelationship(
            "rel-item",
            "MENTIONS",
            persona,
            item,
            {"source": "persona_llm_distillation", "review_status": "approved"},
        )
        hint_rel = FakeRelationship(
            "rel-hint",
            "MENTIONS",
            item,
            decision,
            {"source": "persona_llm_distillation", "review_status": "approved"},
        )
        driver = FakeNeo4jReadDriver(
            [{"p": FakePath([persona, item, decision], [item_rel, hint_rel]), "persona": persona}]
        )
        reader = Neo4jGraphReader(driver)

        lineage = await reader.get_graph_preset("persona_lineage", persona_id="persona-1", limit=10)
        capability_map = await reader.get_graph_preset("persona_capability_map", persona_id="persona-1", limit=10)

        self.assertEqual(lineage.meta["preset"], "persona_lineage")
        self.assertEqual(capability_map.meta["preset"], "persona_capability_map")
        self.assertTrue(any(node.type == "Decision" for node in lineage.nodes))
        self.assertTrue(any(edge.type == "MENTIONS" for edge in capability_map.edges))
        calls = "\n".join(call["cypher"] for call in driver.calls)
        self.assertIn("MENTIONS", calls)
        self.assertIn("persona_id", driver.calls[0]["params"])

    async def test_graph_preset_delegates_anchor_required_templates(self) -> None:
        memory = FakeNode("node-1", ["Memory"], {"id": "memory-1"})
        run = FakeNode("node-2", ["WorkflowRun"], {"id": "run-1"})
        relationship = FakeRelationship("rel-1", "SOURCE_EXECUTION", memory, run, {})
        driver = FakeNeo4jReadDriver([{"p": FakePath([memory, run], [relationship]), "workflow": run}])
        reader = Neo4jGraphReader(driver)

        root_cause = await reader.get_graph_preset("failed_run_root_cause", run_id="run-1", limit=10)
        lineage = await reader.get_graph_preset("workflow_lineage", workflow_id="workflow-1", limit=10)
        provenance = await reader.get_graph_preset("memory_provenance", memory_id="memory-1", limit=10)
        steering = await reader.get_graph_preset("sub_agent_steering", agent_id="agent-1", limit=10)

        self.assertEqual(root_cause.meta["preset"], "failed_run_root_cause")
        self.assertEqual(lineage.meta["preset"], "workflow_lineage")
        self.assertEqual(provenance.meta["preset"], "memory_provenance")
        self.assertEqual(steering.meta["preset"], "sub_agent_steering")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("failed_run_root_cause")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("workflow_lineage")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("memory_provenance")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("sub_agent_steering")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("persona_lineage")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("persona_capability_map")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("physical_device_audit")
        with self.assertRaises(ValueError):
            await reader.get_graph_preset("physical_room_context")

    async def test_graph_preset_rejects_unknown_template(self) -> None:
        reader = Neo4jGraphReader(FakeNeo4jReadDriver([]))

        with self.assertRaises(ValueError):
            await reader.get_graph_preset("unknown")


if __name__ == "__main__":
    unittest.main()
