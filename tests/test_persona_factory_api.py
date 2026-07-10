from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.domain import (
    MainAgentProfile,
    ModelProfileDefinition,
    PersonaDistillationItemType,
    PersonaMemoryLayer,
)
from app.graph.neo4j_read import GraphReadDocument, GraphReadEdge, GraphReadNode
from app.llm.base import ModelResponse
from app.services.persona_distillation_pipeline import PersonaDistillationCandidate
from app.services.persona_factory import HybridDistillationMerger


class _PersonaFactoryFakeModelClient:
    provider_key = "persona_fake"
    structured_responses: list[dict] = []
    structured_exceptions: list[Exception] = []
    structured_delay_seconds: float = 0.0

    def __init__(self, profile: ModelProfileDefinition, env):
        self.profile = profile
        self.env = env

    def generate_text(self, messages, *, temperature=None, max_tokens=None, **kwargs):
        return ModelResponse(content="ok", provider=self.profile.provider, model=self.profile.model)

    def generate_structured(self, messages, *, schema, temperature=None, max_tokens=None, **kwargs):
        schema_name = kwargs.get("schema_name")
        if schema_name == "persona_llm_distillation_candidates" and self.__class__.structured_delay_seconds:
            time.sleep(self.__class__.structured_delay_seconds)
        if schema_name == "persona_llm_distillation_candidates" and self.__class__.structured_exceptions:
            raise self.__class__.structured_exceptions.pop(0)
        if self.__class__.structured_responses:
            return ModelResponse(
                content=self.__class__.structured_responses.pop(0),
                provider=self.profile.provider,
                model=self.profile.model,
            )
        if schema_name in {"persona_factory_source_classification", "source_intelligence_classification"}:
            return ModelResponse(
                content={
                    "label": "decision",
                    "confidence": 0.93,
                    "signals": ["model_decision_signal"],
                    "document_kind": "workpaper",
                    "content_roles": ["decision", "domain_knowledge"],
                    "extraction_targets": ["decision_pattern", "domain_knowledge"],
                    "memory_layers": ["procedural", "semantic"],
                    "vector_tags": ["audit", "privileged-access"],
                    "graph_entities": [
                        {
                            "label": "Decision",
                            "name": "Privileged access review escalation",
                            "confidence": 0.88,
                            "evidence": "privileged access review excludes administrators",
                        }
                    ],
                    "graph_relationships": [
                        {
                            "source_name": "Privileged access review escalation",
                            "relationship_type": "RELATES_TO",
                            "target_name": "Audit observation",
                            "confidence": 0.8,
                            "evidence": "escalate the audit observation",
                        }
                    ],
                    "should_include": True,
                    "rationale": "The source describes an audit decision rule.",
                },
                provider=self.profile.provider,
                model=self.profile.model,
            )
        if schema_name == "persona_llm_distillation_candidates":
            return ModelResponse(
                content={
                    "candidates": [
                        {
                            "item_type": "decision_pattern",
                            "memory_layer": "procedural",
                            "title": "Escalate incomplete privileged access reviews",
                            "content": (
                                "When privileged access review excludes administrators, "
                                "escalate the audit observation."
                            ),
                            "confidence": 0.88,
                            "source_evidence": "privileged access review excludes administrators",
                            "source_span": {"start": 5, "end": 54},
                            "review_reasons": ["source_backed"],
                            "structured_payload": {
                                "rule": "Escalate incomplete privileged access reviews."
                            },
                            "inference_type": "extractive",
                        }
                    ]
                },
                provider=self.profile.provider,
                model=self.profile.model,
            )
        if schema_name == "persona_factory_normalization":
            return ModelResponse(
                content={
                    "updates": [
                        {
                            "item_id": "replace-me",
                            "title": "Model normalized decision rule",
                            "confidence": 0.88,
                            "needs_review": True,
                            "rationale": "Tighter title for reviewer clarity.",
                        }
                    ],
                    "superseded": [],
                    "conflict_groups": [],
                    "summary": "One item title normalized.",
                },
                provider=self.profile.provider,
                model=self.profile.model,
            )
        if schema_name == "persona_package_polish":
            prompt = kwargs.get("prompt")
            if not isinstance(prompt, str) and len(messages) > 1:
                prompt = getattr(messages[1], "content", None)
            base_package = {}
            if isinstance(prompt, str):
                try:
                    base_package = json.loads(prompt).get("base_package") or {}
                except json.JSONDecodeError:
                    base_package = {}
            package = dict(base_package)
            persona = dict(package.get("persona") or {})
            persona["summary"] = "Source-grounded persona for approved audit decisions."
            package["persona"] = persona
            return ModelResponse(
                content={
                    "package": package,
                    "summary": "Polished persona wording without changing source-backed sections.",
                },
                provider=self.profile.provider,
                model=self.profile.model,
            )
        return ModelResponse(content={}, provider=self.profile.provider, model=self.profile.model)


class _FakePersonaGraphReadService:
    def __init__(self):
        self.calls: list[dict] = []

    async def get_graph_preset(self, preset: str, **kwargs):
        self.calls.append({"preset": preset, **kwargs})
        persona_id = kwargs.get("persona_id") or "persona-1"
        return GraphReadDocument(
            nodes=[
                GraphReadNode(
                    id=persona_id,
                    type="Persona",
                    labels=["Persona"],
                    properties={"name": "Graph Context Persona"},
                ),
                GraphReadNode(
                    id="source-intelligence:workflow:release",
                    type="Workflow",
                    labels=["Entity", "Workflow"],
                    properties={"name": "Release Workflow", "evidence": "approved source intelligence"},
                ),
            ],
            edges=[
                GraphReadEdge(
                    id="edge-persona-workflow",
                    source=persona_id,
                    target="source-intelligence:workflow:release",
                    type="MENTIONS",
                )
            ],
            meta={"source": "fake-persona-graph"},
        )


class PersonaFactoryApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = create_test_api_context()
        _PersonaFactoryFakeModelClient.structured_responses = []
        _PersonaFactoryFakeModelClient.structured_exceptions = []
        _PersonaFactoryFakeModelClient.structured_delay_seconds = 0.0
        self.client = TestClient(create_app(context=self.context))
        self.headers = {
            "x-agency-user-id": "persona-user",
            "x-agency-user-email": "persona@example.com",
        }
        self.client.post(
            "/users/sync",
            json={"id": "persona-user", "email": "persona@example.com", "display_name": "Persona User"},
        )

    def _create_memory(
            self,
            memory_id: str,
            content: str,
            *,
            memory_type: str = "fact",
            sensitive: bool = False,
            metadata: dict | None = None,
    ) -> None:
        response = self.client.post(
            "/memories",
            headers=self.headers,
            json={
                "memory": {
                    "id": memory_id,
                    "scope": "user",
                    "content": content,
                    "summary": content[:80],
                    "tags": ["persona-source"],
                    "memory_type": memory_type,
                    "importance": 70,
                    "sensitive": sensitive,
                    "metadata": metadata or {},
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def _create_model_profile(self, *, profile_id: str = "persona-factory-model") -> None:
        self.context.llm_provider_registry.register(
            "persona_fake",
            lambda profile, env: _PersonaFactoryFakeModelClient(profile, env),
        )
        asyncio.run(
            self.context.model_profile_repo.create(
                ModelProfileDefinition(
                    id=profile_id,
                    name="Persona Factory Fake Model",
                    provider="persona_fake",
                    model="fake-structured",
                    supports_structured_output=True,
                )
            )
        )

    def _create_main_agent_profile(
            self,
            *,
            profile_id: str = "main-agent-profile",
            default_model_profile_id: str = "persona-factory-model",
    ) -> None:
        asyncio.run(
            self.context.main_agent_profile_repo.create(
                MainAgentProfile(
                    id=profile_id,
                    name="Main Agent",
                    agent_id="main-agent",
                    default_workflow_id="main-workflow",
                    default_model_profile_id=default_model_profile_id,
                )
            )
        )

    def _publish_basic_persona(self) -> dict:
        self._create_memory(
            "feedback-base-memory",
            "Decision rule: escalate audit evidence gaps when privileged access review scope is incomplete.",
            memory_type="decision",
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Feedback Ready Persona",
                "description": "Persona used to test continuous learning feedback.",
                "source_memory_ids": ["feedback-base-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        for item in body["items"]:
            approve_item = self.client.post(f"/persona-factory/items/{item['id']}/approve", headers=self.headers)
            self.assertEqual(approve_item.status_code, 200, approve_item.text)
        synthesize = self.client.post(
            f"/persona-factory/runs/{body['run']['id']}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        approve = self.client.post(
            f"/persona-factory/runs/{body['run']['id']}/approve",
            headers=self.headers,
            json={"version": "1.0.0"},
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        publish = self.client.post(f"/persona-factory/runs/{body['run']['id']}/publish", headers=self.headers)
        self.assertEqual(publish.status_code, 200, publish.text)
        return {"distill": body, "publish": publish.json()}

    def test_persona_graph_context_endpoint_returns_prompt_and_graph_payload(self) -> None:
        self.context.graph_read_service = _FakePersonaGraphReadService()
        create = self.client.post(
            "/persona",
            headers=self.headers,
            json={"name": "Graph Context Persona", "description": "Uses reviewed graph context."},
        )
        self.assertEqual(create.status_code, 200, create.text)
        persona_id = create.json()["id"]

        response = self.client.get(
            f"/persona/{persona_id}/graph-context",
            headers=self.headers,
            params={"query": "release workflow", "limit": 12},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["persona"]["id"], persona_id)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["policy"]["invocation_type"], "persona_inspection")
        self.assertEqual(body["policy"]["preset"], "persona_lineage")
        self.assertIn("# Persona Graph Context", body["prompt"])
        self.assertIn("Policy: preset=persona_lineage", body["prompt"])
        self.assertIn("Release Workflow", body["prompt"])
        self.assertEqual(body["graph"]["nodes"][0]["id"], persona_id)
        self.assertEqual(body["graph"]["edges"][0]["type"], "MENTIONS")
        self.assertEqual(body["graph"]["meta"]["preset"], "persona_lineage")
        self.assertEqual(self.context.graph_read_service.calls[0]["preset"], "persona_lineage")
        self.assertEqual(self.context.graph_read_service.calls[0]["persona_id"], persona_id)
        self.assertEqual(self.context.graph_read_service.calls[0]["limit"], 12)

    def test_persona_graph_context_endpoint_supports_capability_map_preset(self) -> None:
        self.context.graph_read_service = _FakePersonaGraphReadService()
        create = self.client.post(
            "/persona",
            headers=self.headers,
            json={"name": "Capability Persona", "description": "Uses reviewed graph capabilities."},
        )
        self.assertEqual(create.status_code, 200, create.text)
        persona_id = create.json()["id"]

        response = self.client.get(
            f"/persona/{persona_id}/graph-context",
            headers=self.headers,
            params={"preset": "persona_capability_map", "limit": 8},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["policy"]["preset"], "persona_capability_map")
        self.assertEqual(body["graph"]["meta"]["preset"], "persona_capability_map")
        self.assertEqual(self.context.graph_read_service.calls[0]["preset"], "persona_capability_map")
        self.assertEqual(self.context.graph_read_service.calls[0]["limit"], 8)

    def test_distill_approve_and_publish_persona(self) -> None:
        self._create_memory(
            "persona-memory-1",
            "Audit observations should be graded by risk, evidence quality, and management impact.",
            memory_type="decision",
        )
        self._create_memory(
            "persona-memory-2",
            "The audit review workflow starts with planning, then testing, issue validation, and MLP drafting.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Audit Manager Persona",
                "description": "Reviews audit evidence and drafts management-level observations.",
                "source_memory_ids": ["persona-memory-1", "persona-memory-2"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        self.assertEqual(body["persona"]["slug"], "audit-manager-persona")
        self.assertEqual(body["run"]["status"], "needs_review")
        self.assertEqual(body["run"]["distillation_mode"], "deterministic")
        self.assertIsNone(body["run"]["llm_model_source"])
        run_metadata = body["run"]["distillation_metrics"]["run_metadata"]
        self.assertEqual(run_metadata["distillation_mode"], "deterministic")
        self.assertEqual(run_metadata["deterministic_distiller_version"], "specialized-distillers-v1")
        self.assertEqual(run_metadata["deterministic_pipeline_version"], "classify-extract-normalize-validate-v1")
        self.assertEqual(
            run_metadata["source_classification_strategy"],
            "stored_source_intelligence_or_upload_intelligence_then_deterministic",
        )
        self.assertEqual(len(body["sources"]), 2)
        self.assertEqual(len(body["items"]), 2)
        self.assertEqual(
            {item["item_type"] for item in body["items"]},
            {"decision_pattern", "workflow"},
        )
        self.assertEqual(
            {item["memory_layer"] for item in body["items"]},
            {"procedural"},
        )
        package = body["run"]["output_package"]
        self.assertEqual(package["schema_version"], 1)
        self.assertEqual(package["identity"]["kind"], "persona")
        self.assertEqual(package["governance"]["persona_type"], "professional")
        self.assertTrue(package["knowledge"])
        self.assertTrue(package["decision_patterns"])

        run_id = body["run"]["id"]
        item_types = self.client.get("/persona-factory/item-types", headers=self.headers)
        self.assertEqual(item_types.status_code, 200)
        self.assertIn("decision_pattern", item_types.json()["item_types"])
        self.assertIn("procedural", item_types.json()["memory_layers"])
        self.assertIn("workflow", item_types.json()["source_classifications"])
        self.assertIn("workpaper", item_types.json()["document_kinds"])
        self.assertIn("Decision", item_types.json()["graph_entity_labels"])
        self.assertIn("RELATES_TO", item_types.json()["graph_relationship_types"])

        run_detail = self.client.get(f"/persona-factory/runs/{run_id}", headers=self.headers)
        self.assertEqual(run_detail.status_code, 200, run_detail.text)
        self.assertEqual(run_detail.json()["run"]["id"], run_id)
        self.assertEqual(len(run_detail.json()["items"]), 2)

        runs = self.client.get(
            "/persona-factory/runs",
            headers=self.headers,
            params={"persona_id": body["persona"]["id"], "status": "needs_review"},
        )
        self.assertEqual(runs.status_code, 200, runs.text)
        self.assertEqual(runs.json()["items"][0]["id"], run_id)

        runs_by_creator = self.client.get(
            "/persona-factory/runs",
            headers=self.headers,
            params={"created_by_user_id": "persona-user"},
        )
        self.assertEqual(runs_by_creator.status_code, 200, runs_by_creator.text)
        self.assertTrue(any(item["id"] == run_id for item in runs_by_creator.json()["items"]))

        runs_by_other_creator = self.client.get(
            "/persona-factory/runs",
            headers=self.headers,
            params={"created_by_user_id": "other-user"},
        )
        self.assertEqual(runs_by_other_creator.status_code, 200, runs_by_other_creator.text)
        self.assertFalse(any(item["id"] == run_id for item in runs_by_other_creator.json()["items"]))

        items = self.client.get(f"/persona-factory/runs/{run_id}/items", headers=self.headers)
        self.assertEqual(items.status_code, 200, items.text)
        self.assertEqual(items.json()["total"], 2)
        self.assertEqual(items.json()["filtered_count"], 2)
        self.assertEqual(items.json()["limit"], 100)
        self.assertEqual(items.json()["offset"], 0)
        self.assertEqual(items.json()["counts"]["item_types"]["decision_pattern"], 1)
        self.assertEqual(items.json()["counts"]["item_types"]["workflow"], 1)
        decision_item = next(item for item in items.json()["items"] if item["item_type"] == "decision_pattern")
        workflow_item = next(item for item in items.json()["items"] if item["item_type"] == "workflow")

        filtered_items = self.client.get(
            f"/persona-factory/runs/{run_id}/items",
            headers=self.headers,
            params={
                "item_type": "decision_pattern",
                "memory_layer": "procedural",
                "source_key": decision_item["structured_payload"]["source_ref"]["memory_id"],
                "limit": 1,
                "offset": 0,
            },
        )
        self.assertEqual(filtered_items.status_code, 200, filtered_items.text)
        self.assertEqual(filtered_items.json()["total"], 2)
        self.assertEqual(filtered_items.json()["filtered_count"], 1)
        self.assertEqual(len(filtered_items.json()["items"]), 1)
        self.assertEqual(filtered_items.json()["items"][0]["id"], decision_item["id"])
        self.assertEqual(filtered_items.json()["filters"]["item_type"], "decision_pattern")

        invalid_filter = self.client.get(
            f"/persona-factory/runs/{run_id}/items",
            headers=self.headers,
            params={"item_type": "not-a-type"},
        )
        self.assertEqual(invalid_filter.status_code, 422)

        patch_item = self.client.patch(
            f"/persona-factory/items/{decision_item['id']}",
            headers=self.headers,
            json={
                "patch": {
                    "title": "Audit severity rule",
                    "confidence": 0.91,
                    "review_status": "draft",
                    "needs_review": False,
                }
            },
        )
        self.assertEqual(patch_item.status_code, 200, patch_item.text)
        self.assertEqual(patch_item.json()["title"], "Audit severity rule")
        self.assertEqual(patch_item.json()["confidence"], 0.91)

        approve_item = self.client.post(
            f"/persona-factory/items/{decision_item['id']}/approve",
            headers=self.headers,
        )
        self.assertEqual(approve_item.status_code, 200, approve_item.text)
        self.assertEqual(approve_item.json()["review_status"], "approved")
        self.assertFalse(approve_item.json()["needs_review"])

        reject_item = self.client.post(
            f"/persona-factory/items/{workflow_item['id']}/reject",
            headers=self.headers,
            json={"reason": "Workflow needs a clearer source."},
        )
        self.assertEqual(reject_item.status_code, 200, reject_item.text)
        self.assertEqual(reject_item.json()["review_status"], "rejected")
        self.assertEqual(reject_item.json()["metadata"]["rejection_reason"], "Workflow needs a clearer source.")

        synthesize = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        package = synthesize.json()["run"]["output_package"]
        self.assertEqual(package["provenance"]["strategy"], "item-synthesis-v1")
        self.assertEqual(package["decision_patterns"][0]["title"], "Audit severity rule")
        self.assertEqual(package["decision_patterns"][0]["review_status"], "approved")
        self.assertEqual(package["workflows"], [])
        self.assertIn(workflow_item["id"], package["provenance"]["excluded_item_ids"])

        package["persona"]["communication_style"].append("management-focused")
        update = self.client.patch(
            f"/persona-factory/runs/{run_id}/package",
            headers=self.headers,
            json={"package": package},
        )
        self.assertEqual(update.status_code, 200, update.text)

        approve = self.client.post(
            f"/persona-factory/runs/{run_id}/approve",
            headers=self.headers,
            json={"version": "1.0.0"},
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        self.assertEqual(approve.json()["persona_version"]["status"], "approved")

        publish = self.client.post(f"/persona-factory/runs/{run_id}/publish", headers=self.headers)
        self.assertEqual(publish.status_code, 200, publish.text)
        published = publish.json()
        self.assertEqual(published["persona"]["status"], "published")
        self.assertEqual(published["agent"]["metadata"]["persona_id"], published["persona"]["id"])
        self.assertTrue(published["memory_ids"])
        published_memory = self.context.memory_repo._items[published["memory_ids"][0]]
        self.assertEqual(published_memory["metadata"]["distillation_item_id"], decision_item["id"])
        self.assertEqual(published_memory["metadata"]["review_status"], "approved")
        self.assertEqual(published_memory["metadata"]["source_memory_id"], "persona-memory-1")
        self.assertEqual(published_memory["metadata"]["item_type"], "decision_pattern")
        projection_events = self.context.graph_projection_event_repo._items.values()
        persona_event_types = {
            event.event_type
            for event in projection_events
            if event.aggregate_type == "persona"
        }
        self.assertIn("persona.factory.distilled", persona_event_types)
        self.assertIn("persona.factory.item.updated", persona_event_types)
        self.assertIn("persona.factory.item.approved", persona_event_types)
        self.assertIn("persona.factory.item.rejected", persona_event_types)
        self.assertIn("persona.factory.package.synthesized", persona_event_types)
        self.assertIn("persona.factory.package.updated", persona_event_types)
        self.assertIn("persona.factory.run.approved", persona_event_types)
        self.assertIn("persona.factory.version.published", persona_event_types)

        personas = self.client.get("/persona", headers=self.headers)
        self.assertEqual(personas.status_code, 200)
        self.assertEqual(personas.json()["items"][0]["published_agent_id"], published["agent"]["id"])

        versions = self.client.get(f"/persona/{published['persona']['id']}/versions", headers=self.headers)
        self.assertEqual(versions.status_code, 200)
        self.assertEqual(versions.json()["items"][0]["status"], "published")

        json_export = self.client.get(f"/persona/{published['persona']['id']}/export", headers=self.headers)
        self.assertEqual(json_export.status_code, 200, json_export.text)
        self.assertEqual(json_export.json()["export_type"], "persona_package_json")
        self.assertEqual(json_export.json()["persona"]["id"], published["persona"]["id"])
        self.assertEqual(json_export.json()["persona_version"]["id"], published["persona_version"]["id"])
        self.assertEqual(json_export.json()["package"]["provenance"]["strategy"], "item-synthesis-v1")
        self.assertIn("skill", json_export.json()["terminology_note"].lower())

        markdown_export = self.client.get(
            f"/persona/{published['persona']['id']}/export",
            headers=self.headers,
            params={"format": "skill_markdown"},
        )
        self.assertEqual(markdown_export.status_code, 200, markdown_export.text)
        files = markdown_export.json()["files"]
        self.assertEqual(markdown_export.json()["export_type"], "skill_style_markdown")
        self.assertIn("skill.md", files)
        self.assertIn("persona.md", files)
        self.assertIn("workflow.md", files)
        self.assertIn("decision_patterns.md", files)
        self.assertIn("tools.yaml", files)
        self.assertIn("guardrails.md", files)
        self.assertIn("examples.md", files)
        self.assertIn("Agency Persona export", files["skill.md"])

    def test_llm_polished_package_uses_only_approved_items_and_records_provenance(self) -> None:
        self._create_model_profile()
        self._create_memory(
            "polished-package-memory",
            "When audit evidence is missing, escalate the observation.",
            memory_type="decision",
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Polished Package Persona",
                "source_memory_ids": ["polished-package-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]
        decision_item = next(item for item in distill.json()["items"] if item["item_type"] == "decision_pattern")
        rejected_items = [item for item in distill.json()["items"] if item["id"] != decision_item["id"]]

        approve = self.client.post(
            f"/persona-factory/items/{decision_item['id']}/approve",
            headers=self.headers,
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        for item in rejected_items:
            reject = self.client.post(
                f"/persona-factory/items/{item['id']}/reject",
                headers=self.headers,
                json={"reason": "Keep the polished package focused on one approved item."},
            )
            self.assertEqual(reject.status_code, 200, reject.text)

        synthesize = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
            json={
                "package_synthesis_mode": "llm_polished",
                "llm_polishing_model_profile_id": "persona-factory-model",
            },
        )

        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        package = synthesize.json()["run"]["output_package"]
        provenance = package["provenance"]
        self.assertEqual(provenance["package_synthesis_mode"], "llm_polished")
        self.assertTrue(provenance["llm_polishing_used"])
        self.assertEqual(provenance["approved_item_ids"], [decision_item["id"]])
        self.assertEqual(provenance["polishing_model_profile_id"], "persona-factory-model")
        self.assertEqual(provenance["polishing_prompt_version"], "persona-package-polish-v1")
        self.assertEqual(package["persona"]["summary"], "Source-grounded persona for approved audit decisions.")
        self.assertEqual(package["decision_patterns"][0]["distillation_item_id"], decision_item["id"])
        for item in rejected_items:
            self.assertIn(item["id"], provenance["excluded_item_ids"])

    def test_llm_polished_package_rejects_unsupported_claims(self) -> None:
        self._create_model_profile()
        self._create_memory(
            "unsupported-polish-memory",
            "When audit evidence is missing, escalate the observation.",
            memory_type="decision",
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Unsupported Polish Persona",
                "source_memory_ids": ["unsupported-polish-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]
        decision_item = next(item for item in distill.json()["items"] if item["item_type"] == "decision_pattern")
        approve = self.client.post(
            f"/persona-factory/items/{decision_item['id']}/approve",
            headers=self.headers,
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        for item in distill.json()["items"]:
            if item["id"] != decision_item["id"]:
                reject = self.client.post(
                    f"/persona-factory/items/{item['id']}/reject",
                    headers=self.headers,
                    json={"reason": "Narrow package fixture."},
                )
                self.assertEqual(reject.status_code, 200, reject.text)

        base = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(base.status_code, 200, base.text)
        bad_package = dict(base.json()["run"]["output_package"])
        bad_package["persona"] = {
            **bad_package["persona"],
            "summary": "Quantum blockchain automation expert for unsupported controls.",
        }
        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "package": bad_package,
                "summary": "Introduced unsupported claims.",
            }
        ]

        synthesize = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
            json={
                "package_synthesis_mode": "llm_polished",
                "llm_polishing_model_profile_id": "persona-factory-model",
            },
        )

        self.assertEqual(synthesize.status_code, 422, synthesize.text)
        self.assertIn("unsupported terms", synthesize.json()["detail"])

    def test_bulk_review_persona_distillation_items(self) -> None:
        self._create_memory(
            "persona-bulk-memory-1",
            "If release evidence is missing, escalate to the release owner before approval.",
            memory_type="decision",
        )
        self._create_memory(
            "persona-bulk-memory-2",
            "Teams must not deploy without change approval evidence.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Bulk Review Persona",
                "source_memory_ids": ["persona-bulk-memory-1", "persona-bulk-memory-2"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        item_ids = [item["id"] for item in distill.json()["items"]]
        self.assertGreaterEqual(len(item_ids), 2)

        approve = self.client.post(
            "/persona-factory/items/bulk-review",
            headers=self.headers,
            json={"item_ids": item_ids, "action": "approve"},
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        self.assertEqual(approve.json()["action"], "approve")
        self.assertEqual(approve.json()["count"], len(item_ids))
        self.assertTrue(all(item["review_status"] == "approved" for item in approve.json()["items"]))
        self.assertTrue(all(item["needs_review"] is False for item in approve.json()["items"]))

        reject = self.client.post(
            "/persona-factory/items/bulk-review",
            headers=self.headers,
            json={
                "item_ids": [item_ids[0], item_ids[0]],
                "action": "reject",
                "reason": "Bulk rejection test.",
            },
        )
        self.assertEqual(reject.status_code, 200, reject.text)
        self.assertEqual(reject.json()["count"], 1)
        self.assertEqual(reject.json()["items"][0]["review_status"], "rejected")
        self.assertEqual(reject.json()["items"][0]["metadata"]["rejection_reason"], "Bulk rejection test.")

        invalid = self.client.post(
            "/persona-factory/items/bulk-review",
            headers=self.headers,
            json={"item_ids": [], "action": "approve"},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_bulk_review_persona_distillation_items_by_run_filters(self) -> None:
        self._create_memory(
            "persona-filter-bulk-memory-1",
            "If release evidence is missing, escalate to the release owner before approval.",
            memory_type="decision",
        )
        self._create_memory(
            "persona-filter-bulk-memory-2",
            "Teams must not deploy without change approval evidence.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Filtered Bulk Review Persona",
                "source_memory_ids": ["persona-filter-bulk-memory-1", "persona-filter-bulk-memory-2"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        run_id = body["run"]["id"]
        decision_items = [
            item for item in body["items"]
            if item["item_type"] == "decision_pattern"
            and item["source_memory_id"] == "persona-filter-bulk-memory-1"
        ]
        self.assertTrue(decision_items)

        preview = self.client.post(
            f"/persona-factory/runs/{run_id}/items/bulk-review/preview",
            headers=self.headers,
            json={
                "action": "approve",
                "filters": {
                    "source_key": "persona-filter-bulk-memory-1",
                    "item_type": "decision_pattern",
                    "review_status": "draft",
                    "min_confidence": 0.7,
                },
                "limit": 10,
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        preview_body = preview.json()
        self.assertEqual(preview_body["action"], "approve")
        self.assertEqual(preview_body["count"], len(decision_items))
        self.assertEqual(preview_body["matched_count"], len(decision_items))
        self.assertEqual(preview_body["reviewable_count"], len(decision_items))
        self.assertEqual(len(preview_body["items"]), len(decision_items))
        self.assertTrue(all(item["review_status"] == "draft" for item in preview_body["items"]))

        approve = self.client.post(
            f"/persona-factory/runs/{run_id}/items/bulk-review",
            headers=self.headers,
            json={
                "action": "approve",
                "filters": {
                    "source_key": "persona-filter-bulk-memory-1",
                    "item_type": "decision_pattern",
                    "review_status": "draft",
                    "min_confidence": 0.7,
                },
                "limit": 10,
            },
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        approved_body = approve.json()
        self.assertEqual(approved_body["action"], "approve")
        self.assertEqual(approved_body["filters"]["item_type"], "decision_pattern")
        self.assertEqual(approved_body["count"], len(decision_items))
        self.assertEqual(approved_body["matched_count"], len(decision_items))
        self.assertFalse(approved_body["has_more"])
        self.assertTrue(all(item["review_status"] == "approved" for item in approved_body["items"]))

        reject = self.client.post(
            f"/persona-factory/runs/{run_id}/items/bulk-review",
            headers=self.headers,
            json={
                "action": "reject",
                "reason": "Filtered source rejected.",
                "filters": {
                    "source_key": "persona-filter-bulk-memory-2",
                    "review_status": "draft",
                },
            },
        )
        self.assertEqual(reject.status_code, 200, reject.text)
        rejected_body = reject.json()
        self.assertGreaterEqual(rejected_body["count"], 1)
        self.assertTrue(all(item["review_status"] == "rejected" for item in rejected_body["items"]))
        self.assertTrue(
            all(item["metadata"]["rejection_reason"] == "Filtered source rejected." for item in rejected_body["items"])
        )

        empty = self.client.post(
            f"/persona-factory/runs/{run_id}/items/bulk-review",
            headers=self.headers,
            json={"action": "approve", "filters": {"source_key": "missing-source"}},
        )
        self.assertEqual(empty.status_code, 422)

    def test_feedback_creates_reviewable_item_and_supports_version_rollback(self) -> None:
        published = self._publish_basic_persona()
        persona = published["publish"]["persona"]
        original_version = published["publish"]["persona_version"]
        original_item_id = published["distill"]["items"][0]["id"]

        feedback = self.client.post(
            "/persona-factory/feedback",
            headers=self.headers,
            json={
                "persona_id": persona["id"],
                "title": "Tighter audit severity rule",
                "content": (
                    "Accepted correction: escalate the observation when privileged access review excludes "
                    "vendor or administrator accounts."
                ),
                "item_type": "decision_pattern",
                "memory_layer": "procedural",
                "feedback_type": "accepted_edit",
                "accepted_edit_of_item_id": original_item_id,
                "source_conversation_id": "conversation-feedback-1",
                "source_message_id": "message-feedback-1",
                "source_run_id": "run-feedback-1",
                "metadata": {"reviewer_note": "Accepted during MLP drafting."},
            },
        )
        self.assertEqual(feedback.status_code, 200, feedback.text)
        feedback_body = feedback.json()
        feedback_run_id = feedback_body["run"]["id"]
        feedback_item = feedback_body["items"][0]
        self.assertEqual(feedback_body["persona"]["status"], "published")
        self.assertEqual(feedback_body["persona"]["current_version_id"], original_version["id"])
        self.assertEqual(feedback_body["run"]["status"], "needs_review")
        self.assertEqual(feedback_item["review_status"], "needs_review")
        self.assertTrue(feedback_item["needs_review"])
        self.assertEqual(feedback_item["metadata"]["accepted_edit_of_item_id"], original_item_id)
        self.assertTrue(feedback_item["metadata"]["requires_review_before_publish"])
        self.assertEqual(feedback_body["source_memory"]["source"], "persona_feedback")
        self.assertTrue(feedback_body["source_memory"]["metadata"]["candidate_only"])

        synthesize = self.client.post(
            f"/persona-factory/runs/{feedback_run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        blocked_approval = self.client.post(
            f"/persona-factory/runs/{feedback_run_id}/approve",
            headers=self.headers,
            json={},
        )
        self.assertEqual(blocked_approval.status_code, 422)
        self.assertIn("needs_review", blocked_approval.text)

        approve_feedback_item = self.client.post(
            f"/persona-factory/items/{feedback_item['id']}/approve",
            headers=self.headers,
        )
        self.assertEqual(approve_feedback_item.status_code, 200, approve_feedback_item.text)
        resynthesize = self.client.post(
            f"/persona-factory/runs/{feedback_run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(resynthesize.status_code, 200, resynthesize.text)
        approve_v2 = self.client.post(
            f"/persona-factory/runs/{feedback_run_id}/approve",
            headers=self.headers,
            json={},
        )
        self.assertEqual(approve_v2.status_code, 200, approve_v2.text)
        self.assertEqual(approve_v2.json()["persona_version"]["version"], "1.0.1")
        publish_v2 = self.client.post(f"/persona-factory/runs/{feedback_run_id}/publish", headers=self.headers)
        self.assertEqual(publish_v2.status_code, 200, publish_v2.text)
        self.assertEqual(publish_v2.json()["persona"]["current_version_id"], approve_v2.json()["persona_version"]["id"])

        rollback = self.client.post(
            f"/persona/{persona['id']}/versions/{original_version['id']}/rollback",
            headers=self.headers,
        )
        self.assertEqual(rollback.status_code, 200, rollback.text)
        self.assertEqual(rollback.json()["persona"]["current_version_id"], original_version["id"])
        self.assertEqual(rollback.json()["rollback"]["restored_version_id"], original_version["id"])
        projection_events = self.context.graph_projection_event_repo._items.values()
        persona_event_types = {
            event.event_type
            for event in projection_events
            if event.aggregate_type == "persona"
        }
        self.assertIn("persona.factory.feedback.captured", persona_event_types)
        self.assertIn("persona.factory.version.rolled_back", persona_event_types)

    def test_import_skill_style_markdown_creates_draft_persona_version(self) -> None:
        import_response = self.client.post(
            "/persona/import",
            headers=self.headers,
            json={
                "name": "Imported Audit Persona",
                "slug": "imported-audit-persona",
                "format": "skill_markdown",
                "files": {
                    "skill.md": "# Imported Audit Persona\n\nAgency Persona export.",
                    "persona.md": "# Persona\n\nReviews audit evidence in a concise style.",
                    "decision_patterns.md": (
                        "# Decision Patterns\n\n"
                        "## Audit Severity\n\n"
                        "Escalate when privileged access review excludes administrators."
                    ),
                    "workflow.md": (
                        "# Workflows\n\n"
                        "## Audit Review\n\n"
                        "Plan, test, validate issues, then draft the MLP observation."
                    ),
                    "tools.yaml": 'tools:\n  - name: "Jira"\n    tool_id: "jira"\n    granted: false\n',
                    "guardrails.md": "# Guardrails\n\n## Evidence\n\nDo not invent missing evidence.",
                    "examples.md": "# Examples\n\n## Observation\n\nAccess review scope excluded administrators.",
                },
            },
        )
        self.assertEqual(import_response.status_code, 200, import_response.text)
        body = import_response.json()
        self.assertEqual(body["import_type"], "skill_style_markdown")
        self.assertEqual(body["persona"]["slug"], "imported-audit-persona")
        self.assertEqual(body["persona_version"]["status"], "draft")
        self.assertEqual(body["package"]["provenance"]["strategy"], "skill-style-import-v1")
        self.assertEqual(body["package"]["decision_patterns"][0]["title"], "Audit Severity")
        self.assertEqual(body["package"]["workflows"][0]["title"], "Audit Review")
        self.assertEqual(body["package"]["tools"][0]["tool_id"], "jira")

        exported = self.client.get(f"/persona/{body['persona']['id']}/export", headers=self.headers)
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.json()["package"]["provenance"]["strategy"], "skill-style-import-v1")

    def test_distillation_pipeline_extracts_multiple_structured_items_from_rich_source(self) -> None:
        self._create_memory(
            "persona-rich-source",
            (
                "SOP: Release review workflow:\n"
                "1. Confirm the Jira ticket and testing evidence.\n"
                "2. Validate Workday regression results.\n"
                "3. Record approval in ServiceNow before release.\n\n"
                "If privileged access review excludes vendor or admin accounts, escalate the finding.\n"
                "Teams must not rely on incomplete SOC coverage without additional procedures.\n"
                "Example observation: access review scope excluded administrators.\n"
                "Tone: formal, concise, and diplomatic."
            ),
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Rich Source Persona",
                "source_memory_ids": ["persona-rich-source"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        items = distill.json()["items"]
        item_types = {item["item_type"] for item in items}
        self.assertIn("workflow", item_types)
        self.assertIn("decision_pattern", item_types)
        self.assertIn("guardrail", item_types)
        self.assertIn("example", item_types)
        self.assertIn("writing_style", item_types)
        self.assertIn("tool_usage", item_types)

        tool_names = {
            item["structured_payload"].get("tool_name")
            for item in items
            if item["item_type"] == "tool_usage"
        }
        self.assertTrue({"jira", "workday", "servicenow"}.issubset(tool_names))
        for item in items:
            payload = item["structured_payload"]
            self.assertEqual(payload["pipeline"], "classify-extract-normalize-validate-v1")
            self.assertEqual(payload["extractor"], "deterministic-multi-distiller-v1")
            self.assertIn("source_classification", payload)
            self.assertEqual(payload["source_ref"]["memory_id"], "persona-rich-source")

    def test_distillation_uses_document_intelligence_metadata_in_source_refs_and_routing(self) -> None:
        self._create_memory(
            "persona-upload-intel-source",
            (
                "Release SOP requires approval evidence before deployment. "
                "Teams must not bypass the change approval record."
            ),
            memory_type="archive",
            metadata={
                "document_id": "doc-intel",
                "filename": "release-sop.md",
                "content_sha256": "sha-test",
                "storage_uri": "memory://doc-intel",
                "upload_mode": "vector",
                "chunk_index": 0,
                "chunk_count": 1,
                "start_char": 0,
                "end_char": 112,
                "upload_intelligence": {
                    "source": "main_agent_llm",
                    "document_kind": "policy_sop",
                    "confidence": 0.91,
                    "recommended": {"tags": ["release", "approval"]},
                },
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Document Intelligence Persona",
                "source_memory_ids": ["persona-upload-intel-source"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        guardrail = next(item for item in distill.json()["items"] if item["item_type"] == "guardrail")
        payload = guardrail["structured_payload"]
        source_ref = payload["source_ref"]

        self.assertEqual(payload["distiller"], "guardrail_distiller")
        self.assertEqual(payload["distiller_version"], "specialized-distillers-v1")
        self.assertEqual(payload["routing"]["document_kind"], "policy_sop")
        self.assertIn("release", payload["routing"]["vector_tags"])
        self.assertEqual(source_ref["document_id"], "doc-intel")
        self.assertEqual(source_ref["filename"], "release-sop.md")
        self.assertEqual(source_ref["content_sha256"], "sha-test")
        self.assertEqual(source_ref["upload_mode"], "vector")
        self.assertEqual(source_ref["document_kind"], "policy_sop")
        self.assertEqual(source_ref["upload_intelligence_source"], "main_agent_llm")

    def test_run_source_map_summarizes_source_intelligence_and_review_state(self) -> None:
        self._create_memory(
            "persona-source-map-memory",
            (
                "Release SOP requires approval evidence before deployment. "
                "Teams must not bypass the change approval record."
            ),
            memory_type="archive",
            metadata={
                "document_id": "doc-source-map",
                "filename": "release-sop.md",
                "content_sha256": "sha-source-map",
                "storage_uri": "memory://doc-source-map",
                "upload_mode": "vector",
                "chunk_index": 0,
                "chunk_count": 1,
                "upload_intelligence": {
                    "source": "main_agent_llm",
                    "document_kind": "policy_sop",
                    "confidence": 0.91,
                    "recommended": {"tags": ["release", "approval"]},
                },
            },
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Source Map Persona",
                "source_memory_ids": ["persona-source-map-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        guardrail = next(item for item in body["items"] if item["item_type"] == "guardrail")
        approve = self.client.post(f"/persona-factory/items/{guardrail['id']}/approve", headers=self.headers)
        self.assertEqual(approve.status_code, 200, approve.text)

        response = self.client.get(
            f"/persona-factory/runs/{body['run']['id']}/source-map",
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        source_map = response.json()
        self.assertEqual(source_map["run_id"], body["run"]["id"])
        self.assertEqual(source_map["source_count"], 1)
        self.assertEqual(source_map["item_count"], len(body["items"]))
        entry = source_map["items"][0]
        self.assertEqual(entry["key"], "doc-source-map")
        self.assertEqual(entry["label"], "release-sop.md")
        self.assertEqual(entry["memory_id"], "persona-source-map-memory")
        self.assertEqual(entry["document_kind"], "policy_sop")
        self.assertEqual(entry["upload_intelligence_source"], "main_agent_llm")
        self.assertIn("release", entry["vector_tags"])
        self.assertIn("guardrail_distiller", entry["distillers"])
        self.assertEqual(entry["approved_count"], 1)
        self.assertEqual(entry["review_statuses"]["approved"], 1)
        self.assertEqual(entry["source_ref"]["content_sha256"], "sha-source-map")

        detail = self.client.get(
            f"/persona-factory/runs/{body['run']['id']}/sources/doc-source-map",
            headers=self.headers,
            params={"item_type": "guardrail", "limit": 10},
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_body = detail.json()
        self.assertEqual(detail_body["source"]["key"], "doc-source-map")
        self.assertEqual(detail_body["source"]["label"], "release-sop.md")
        self.assertGreaterEqual(detail_body["filtered_count"], 1)
        self.assertTrue(any(item["id"] == guardrail["id"] for item in detail_body["items"]))
        self.assertEqual(detail_body["filters"]["source_key"], "doc-source-map")
        self.assertEqual(detail_body["filters"]["item_type"], "guardrail")

        missing = self.client.get(
            f"/persona-factory/runs/{body['run']['id']}/sources/missing-source",
            headers=self.headers,
        )
        self.assertEqual(missing.status_code, 404)

    def test_source_classification_correction_and_redistill_preserve_source_provenance(self) -> None:
        self._create_memory(
            "persona-source-correction-memory",
            (
                "Release SOP requires approval evidence before deployment. "
                "If test evidence is missing, escalate to the release owner. "
                "Teams must not bypass the change approval record."
            ),
            memory_type="archive",
            metadata={
                "document_id": "doc-source-correction",
                "filename": "release-source.md",
                "content_sha256": "sha-source-correction",
                "upload_intelligence": {
                    "source": "main_agent_llm",
                    "document_kind": "policy_sop",
                    "confidence": 0.91,
                    "recommended": {"tags": ["release"]},
                },
            },
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Source Correction Persona",
                "source_memory_ids": ["persona-source-correction-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        original_item_ids = {item["id"] for item in body["items"]}

        correction = self.client.patch(
            f"/persona-factory/runs/{body['run']['id']}/sources/doc-source-correction/classification",
            headers=self.headers,
            json={
                "classification": "workflow",
                "document_kind": "ticket",
                "content_roles": ["workflow"],
                "extraction_targets": ["workflow", "decision_pattern"],
                "memory_layers": ["procedural"],
                "vector_tags": ["release", "manual-flow"],
                "confidence": 0.97,
                "rationale": "Reviewer identified this source as operational flow evidence.",
            },
        )
        self.assertEqual(correction.status_code, 200, correction.text)
        corrected = correction.json()
        self.assertEqual(corrected["classification"]["label"], "workflow")
        self.assertEqual(corrected["classification"]["document_kind"], "ticket")
        self.assertIn("manual-flow", corrected["classification"]["vector_tags"])
        self.assertEqual(corrected["source_detail"]["source"]["classification"], "workflow")
        self.assertEqual(corrected["source_detail"]["source"]["document_kind"], "ticket")
        self.assertIn("manual-flow", corrected["source_detail"]["source"]["vector_tags"])

        source_memory = self.context.memory_repo._items["persona-source-correction-memory"]
        source_intelligence = source_memory["metadata"]["source_intelligence"]
        self.assertEqual(source_intelligence["review_status"], "approved")
        self.assertEqual(source_intelligence["classification"]["label"], "workflow")
        self.assertIn("manual-flow", source_intelligence["classification"]["vector_tags"])
        self.assertIn("manual-flow", source_memory["tags"])

        redistill = self.client.post(
            f"/persona-factory/runs/{body['run']['id']}/sources/doc-source-correction/redistill",
            headers=self.headers,
            json={"limit": 10},
        )
        self.assertEqual(redistill.status_code, 200, redistill.text)
        redistilled = redistill.json()
        self.assertEqual(redistilled["superseded_count"], len(original_item_ids))
        self.assertGreater(redistilled["created_count"], 0)
        self.assertTrue(
            all(item["review_status"] == "superseded" for item in redistilled["superseded_items"])
        )
        self.assertTrue(
            all(item["id"] not in original_item_ids for item in redistilled["items"])
        )
        self.assertTrue(
            all(
                item["structured_payload"]["source_classification"]["label"] == "workflow"
                for item in redistilled["items"]
            )
        )
        self.assertTrue(
            any(
                "manual-flow" in item["structured_payload"]["routing"]["vector_tags"]
                for item in redistilled["items"]
            )
        )
        self.assertGreaterEqual(
            redistilled["source_detail"]["counts"]["review_statuses"].get("superseded", 0),
            len(original_item_ids),
        )

        persona_event_types = {
            event.event_type
            for event in self.context.graph_projection_event_repo._items.values()
            if event.aggregate_type == "persona"
        }
        self.assertIn("persona.factory.source.classification.updated", persona_event_types)
        self.assertIn("persona.factory.source.redistilled", persona_event_types)

    def test_model_profile_classifier_and_normalizer_validate_structured_output(self) -> None:
        self._create_model_profile()
        self._create_memory(
            "model-backed-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            memory_type="fact",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Model Backed Persona",
                "source_memory_ids": ["model-backed-memory"],
                "distillation_mode": "deterministic",
                "model_profile_id": "persona-factory-model",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        item = body["items"][0]
        self.assertEqual(item["structured_payload"]["source_classification"]["label"], "decision")
        self.assertIn("llm_classifier", item["structured_payload"]["source_classification"]["signals"])
        self.assertEqual(item["structured_payload"]["source_classification"]["document_kind"], "workpaper")
        self.assertEqual(
            item["structured_payload"]["source_classification"]["extraction_targets"],
            ["decision_pattern", "domain_knowledge"],
        )
        self.assertEqual(
            item["structured_payload"]["source_classification"]["memory_layers"],
            ["procedural", "semantic"],
        )
        self.assertIn("privileged-access", item["structured_payload"]["source_classification"]["vector_tags"])
        self.assertEqual(
            item["structured_payload"]["source_classification"]["graph_entities"][0]["label"],
            "Decision",
        )
        self.assertEqual(
            item["structured_payload"]["source_classification"]["graph_relationships"][0]["relationship_type"],
            "RELATES_TO",
        )

        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "updates": [
                    {
                        "item_id": item["id"],
                        "title": "Model normalized decision rule",
                        "confidence": 0.88,
                        "needs_review": True,
                        "rationale": "Tighter title for reviewer clarity.",
                    }
                ],
                "superseded": [],
                "conflict_groups": [],
                "summary": "One item title normalized.",
            }
        ]
        normalize = self.client.post(
            f"/persona-factory/runs/{body['run']['id']}/normalize",
            headers=self.headers,
        )
        self.assertEqual(normalize.status_code, 200, normalize.text)
        normalized_item = normalize.json()["items"][0]
        self.assertEqual(normalized_item["title"], "Model normalized decision rule")
        self.assertEqual(normalized_item["metadata"]["llm_normalization"]["model_profile_id"], "persona-factory-model")
        self.assertEqual(normalize.json()["normalization"]["llm_normalization"]["update_count"], 1)

    def test_llm_mode_defaults_to_main_agent_model_profile(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        self._create_memory(
            "main-agent-model-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Main Agent Model Persona",
                "source_memory_ids": ["main-agent-model-memory"],
                "distillation_mode": "llm",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        self.assertEqual(run["distillation_mode"], "llm")
        self.assertEqual(run["llm_model_source"], "main_agent")
        self.assertEqual(run["model_profile_id"], "persona-factory-model")
        self.assertEqual(run["resolved_model_profile_id"], "persona-factory-model")
        self.assertEqual(run["resolved_model_provider"], "persona_fake")
        self.assertEqual(run["resolved_model"], "fake-structured")
        run_metadata = run["distillation_metrics"]["run_metadata"]
        self.assertEqual(run_metadata["distillation_mode"], "llm")
        self.assertEqual(run_metadata["llm_model_source"], "main_agent")
        self.assertEqual(run_metadata["llm_distiller_version"], "llm-distillers-v1")
        self.assertEqual(run_metadata["llm_extractor"], "llm-distillation-v1")
        self.assertNotIn("deterministic_distiller_version", run_metadata)
        llm_metrics = run["distillation_metrics"]["llm_distillation"]
        self.assertEqual(llm_metrics["call_count"], 1)
        self.assertEqual(llm_metrics["success_count"], 1)
        self.assertEqual(llm_metrics["failure_count"], 0)
        self.assertEqual(llm_metrics["sources"][0]["source_memory_id"], "main-agent-model-memory")
        item = distill.json()["items"][0]
        self.assertEqual(item["structured_payload"]["extractor"], "llm-distillation-v1")
        self.assertEqual(item["metadata"]["generated_by"], "llm")
        self.assertEqual(item["metadata"]["llm_distillation_call"]["call_index"], 1)
        self.assertIn("llm_classifier", item["structured_payload"]["source_classification"]["signals"])

    def test_hybrid_mode_defaults_to_main_agent_model_profile(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        self._create_memory(
            "hybrid-main-agent-model-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Main Agent Model Persona",
                "source_memory_ids": ["hybrid-main-agent-model-memory"],
                "distillation_mode": "hybrid",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        self.assertEqual(run["distillation_mode"], "hybrid")
        self.assertEqual(run["llm_model_source"], "main_agent")
        self.assertEqual(run["model_profile_id"], "persona-factory-model")
        self.assertEqual(run["resolved_model_profile_id"], "persona-factory-model")
        self.assertEqual(run["resolved_model_provider"], "persona_fake")
        self.assertEqual(run["resolved_model"], "fake-structured")
        run_metadata = run["distillation_metrics"]["run_metadata"]
        self.assertEqual(run_metadata["distillation_mode"], "hybrid")
        self.assertEqual(run_metadata["deterministic_distiller_version"], "specialized-distillers-v1")
        self.assertEqual(run_metadata["llm_distiller_version"], "llm-distillers-v1")
        self.assertEqual(run_metadata["merge_strategy"], "hybrid-exact-semantic-conflict-merge-v1")

    def test_default_distillation_mode_uses_llm(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        self._create_memory(
            "configured-default-llm-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Default LLM Persona",
                "source_memory_ids": ["configured-default-llm-memory"],
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        self.assertEqual(run["distillation_mode"], "llm")
        self.assertEqual(run["llm_model_source"], "main_agent")
        catalog = self.client.get("/persona-factory/item-types", headers=self.headers)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["operational_settings"]["default_distillation_mode"], "llm")
        self.assertEqual(catalog.json()["operational_settings"]["default_llm_model_source"], "main_agent")

    def test_llm_and_hybrid_modes_can_be_disabled_by_settings(self) -> None:
        self._create_memory(
            "disabled-llm-memory",
            "Audit review evidence should be complete before issue validation.",
        )
        self._create_memory(
            "disabled-hybrid-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        try:
            with patch.dict(os.environ, {"PERSONA_FACTORY_LLM_DISTILLATION_ENABLED": "false"}):
                reset_settings_cache()
                llm = self.client.post(
                    "/persona-factory/distill",
                    headers=self.headers,
                    json={
                        "name": "Disabled LLM Persona",
                        "source_memory_ids": ["disabled-llm-memory"],
                        "distillation_mode": "llm",
                    },
                )
            with patch.dict(os.environ, {"PERSONA_FACTORY_HYBRID_DISTILLATION_ENABLED": "false"}):
                reset_settings_cache()
                hybrid = self.client.post(
                    "/persona-factory/distill",
                    headers=self.headers,
                    json={
                        "name": "Disabled Hybrid Persona",
                        "source_memory_ids": ["disabled-hybrid-memory"],
                        "distillation_mode": "hybrid",
                    },
                )
        finally:
            reset_settings_cache()

        self.assertEqual(llm.status_code, 422)
        self.assertIn("LLM-backed persona distillation is disabled", llm.text)
        self.assertEqual(hybrid.status_code, 422)
        self.assertIn("Hybrid persona distillation is disabled", hybrid.text)

    def test_hybrid_mode_accepts_explicit_model_profile_source(self) -> None:
        self._create_model_profile(profile_id="persona-hybrid-model")
        self._create_memory(
            "hybrid-model-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Model Persona",
                "source_memory_ids": ["hybrid-model-memory"],
                "distillation_mode": "hybrid",
                "llm_model_source": "model_profile",
                "model_profile_id": "persona-hybrid-model",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        self.assertEqual(run["distillation_mode"], "hybrid")
        self.assertEqual(run["llm_model_source"], "model_profile")
        self.assertEqual(run["model_profile_id"], "persona-hybrid-model")
        self.assertEqual(run["resolved_model_profile_id"], "persona-hybrid-model")
        self.assertEqual(run["resolved_model_provider"], "persona_fake")
        self.assertEqual(run["resolved_model"], "fake-structured")

    def test_llm_mode_maps_legacy_model_profile_id_to_model_profile_source(self) -> None:
        self._create_model_profile(profile_id="legacy-client-model")
        self._create_memory(
            "legacy-client-model-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Legacy Client Model Persona",
                "source_memory_ids": ["legacy-client-model-memory"],
                "distillation_mode": "llm",
                "model_profile_id": "legacy-client-model",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        self.assertEqual(run["distillation_mode"], "llm")
        self.assertEqual(run["llm_model_source"], "model_profile")
        self.assertEqual(run["model_profile_id"], "legacy-client-model")
        self.assertEqual(run["resolved_model_profile_id"], "legacy-client-model")

    def test_hybrid_mode_merges_exact_duplicate_candidates_and_preserves_provenance(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        self._create_memory(
            "hybrid-exact-merge-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Exact Merge Persona",
                "source_memory_ids": ["hybrid-exact-merge-memory"],
                "distillation_mode": "hybrid",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        decision_items = [
            item for item in body["items"]
            if item["item_type"] == "decision_pattern"
        ]
        self.assertEqual(len(decision_items), 1)
        item = decision_items[0]
        self.assertEqual(item["structured_payload"]["extractor"], "hybrid-distillation-v1")
        self.assertEqual(item["metadata"]["generated_by"], "hybrid")
        self.assertEqual(item["metadata"]["merge_strategy"], "exact_duplicate")
        self.assertIn("both_agreed", item["metadata"]["review_flags"])
        self.assertIn("decision_distiller", item["metadata"]["merged_from_distillers"])
        self.assertIn("llm_decision_distiller", item["metadata"]["merged_from_distillers"])
        self.assertGreaterEqual(len(item["structured_payload"]["source_refs"]), 1)
        hybrid_metrics = body["run"]["distillation_metrics"]["hybrid_merge"]
        self.assertEqual(hybrid_metrics["exact_duplicate_merged_count"], 1)
        self.assertEqual(hybrid_metrics["both_agreed_count"], 1)

    def test_hybrid_mode_merges_semantic_duplicates(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "candidates": [
                    {
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "title": "Escalate missing admin review coverage",
                        "content": (
                            "Escalate audit observations when administrator accounts are missing "
                            "from privileged access review."
                        ),
                        "confidence": 0.9,
                        "source_evidence": "privileged access review excludes administrators",
                        "source_span": None,
                        "review_reasons": ["source_backed"],
                        "structured_payload": {"rule": "Escalate missing administrator review coverage."},
                        "inference_type": "abstractive",
                    }
                ]
            }
        ]
        self._create_memory(
            "hybrid-semantic-merge-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                    }
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Semantic Merge Persona",
                "source_memory_ids": ["hybrid-semantic-merge-memory"],
                "distillation_mode": "hybrid",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        decision_items = [
            item for item in body["items"]
            if item["item_type"] == "decision_pattern"
        ]
        self.assertEqual(len(decision_items), 1)
        item = decision_items[0]
        self.assertEqual(item["metadata"]["merge_strategy"], "semantic_duplicate")
        self.assertIsNotNone(item["metadata"]["semantic_duplicate_group_id"])
        self.assertIn("both_agreed", item["metadata"]["review_flags"])
        hybrid_metrics = body["run"]["distillation_metrics"]["hybrid_merge"]
        self.assertEqual(hybrid_metrics["semantic_duplicate_merged_count"], 1)

    def test_hybrid_merger_folds_llm_micro_candidates_subsumed_by_same_source_candidate(self) -> None:
        source_ref = {"memory_id": "style-memory", "source_id": "style-source"}
        deterministic = PersonaDistillationCandidate(
            item_type=PersonaDistillationItemType.WRITING_STYLE,
            memory_layer=PersonaMemoryLayer.PERSONA,
            title="Writing style",
            content=(
                "Writing preference: keep recommendations concise, concrete, and ordered by priority. "
                "Avoid long preambles. Use short factual summaries before action items."
            ),
            confidence=0.84,
            structured_payload={
                "source_ref": source_ref,
                "extractor": "deterministic-multi-distiller-v1",
                "distiller": "persona_style_distiller",
            },
            metadata={"distiller": "persona_style_distiller"},
            needs_review=False,
        )
        llm_candidates = [
            PersonaDistillationCandidate(
                item_type=PersonaDistillationItemType.WRITING_STYLE,
                memory_layer=PersonaMemoryLayer.PERSONA,
                title="Avoid long preambles",
                content="Avoid long preambles.",
                confidence=0.9,
                structured_payload={
                    "source_ref": {**source_ref, "evidence": {"text": "Avoid long preambles."}},
                    "source_evidence": "Avoid long preambles.",
                    "extractor": "llm-distillation-v1",
                    "distiller": "llm_writing_style_distiller",
                },
                metadata={
                    "generated_by": "llm",
                    "distiller": "llm_writing_style_distiller",
                    "evidence_grounding": {"verified": True},
                },
                needs_review=False,
            ),
            PersonaDistillationCandidate(
                item_type=PersonaDistillationItemType.WRITING_STYLE,
                memory_layer=PersonaMemoryLayer.PERSONA,
                title="Short factual summaries",
                content="Use short factual summaries before action items.",
                confidence=0.88,
                structured_payload={
                    "source_ref": {
                        **source_ref,
                        "evidence": {"text": "Use short factual summaries before action items."},
                    },
                    "source_evidence": "Use short factual summaries before action items.",
                    "extractor": "llm-distillation-v1",
                    "distiller": "llm_writing_style_distiller",
                },
                metadata={
                    "generated_by": "llm",
                    "distiller": "llm_writing_style_distiller",
                    "evidence_grounding": {"verified": True},
                },
                needs_review=False,
            ),
        ]

        merged, metrics = HybridDistillationMerger().merge(
            deterministic_candidates=[deterministic],
            llm_candidates=llm_candidates,
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merge_strategy"], "source_subsumed")
        self.assertIn("both_agreed", merged[0].metadata["review_flags"])
        self.assertEqual(metrics["source_subsumed_merged_count"], 2)
        self.assertEqual(metrics["output_count"], 1)

    def test_hybrid_merger_merges_same_source_tool_usage_variants(self) -> None:
        source_ref = {"memory_id": "tool-memory", "source_id": "tool-source"}
        deterministic = PersonaDistillationCandidate(
            item_type=PersonaDistillationItemType.TOOL_USAGE,
            memory_layer=PersonaMemoryLayer.TOOL,
            title="Jira",
            content="The team tracks Jira evidence tickets for each audit observation.",
            confidence=0.72,
            structured_payload={
                "source_ref": source_ref,
                "extractor": "deterministic-multi-distiller-v1",
                "distiller": "tool_distiller",
            },
            metadata={"distiller": "tool_distiller"},
            needs_review=True,
        )
        llm = PersonaDistillationCandidate(
            item_type=PersonaDistillationItemType.TOOL_USAGE,
            memory_layer=PersonaMemoryLayer.TOOL,
            title="Jira is used for evidence ticket tracking",
            content=(
                "Jira is used as the tracking reference for evidence tickets in this audit process, "
                "including linking the relevant ticket during the workflow."
            ),
            confidence=0.9,
            structured_payload={
                "source_ref": {
                    **source_ref,
                    "evidence": {"text": "Jira tracking reference for evidence tickets", "verified": True},
                },
                "source_evidence": "Jira tracking reference for evidence tickets",
                "extractor": "llm-distillation-v1",
                "distiller": "llm_tool_usage_distiller",
            },
            metadata={
                "generated_by": "llm",
                "distiller": "llm_tool_usage_distiller",
                "evidence_grounding": {"verified": True},
            },
            needs_review=True,
        )

        merged, metrics = HybridDistillationMerger().merge(
            deterministic_candidates=[deterministic],
            llm_candidates=[llm],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].metadata["merge_strategy"], "semantic_duplicate")
        self.assertIn("both_agreed", merged[0].metadata["review_flags"])
        self.assertEqual(metrics["semantic_duplicate_merged_count"], 1)

    def test_hybrid_mode_marks_material_conflicts_for_review(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "candidates": [
                    {
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "title": "Administrator review inclusion rule",
                        "content": "Administrators must not be included in privileged access reviews.",
                        "confidence": 0.89,
                        "source_evidence": "Administrators must be included in privileged access reviews.",
                        "source_span": None,
                        "review_reasons": ["source_backed"],
                        "structured_payload": {"rule": "Do not include administrators in access reviews."},
                        "inference_type": "abstractive",
                    }
                ]
            }
        ]
        self._create_memory(
            "hybrid-conflict-memory",
            "Administrators must be included in privileged access reviews.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                    }
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Conflict Persona",
                "source_memory_ids": ["hybrid-conflict-memory"],
                "distillation_mode": "hybrid",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        decision_items = [
            item for item in body["items"]
            if item["item_type"] == "decision_pattern"
        ]
        self.assertEqual(len(decision_items), 2)
        conflict_group_ids = {item["metadata"].get("conflict_group_id") for item in decision_items}
        self.assertEqual(len(conflict_group_ids), 1)
        self.assertTrue(all(item["needs_review"] for item in decision_items))
        self.assertTrue(all("material_conflict" in item["metadata"]["review_flags"] for item in decision_items))
        hybrid_metrics = body["run"]["distillation_metrics"]["hybrid_merge"]
        self.assertEqual(hybrid_metrics["conflict_group_count"], 1)

    def test_review_summary_exposes_hybrid_provenance_filters_and_model_options(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "candidates": [
                    {
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "title": "Administrator review inclusion rule",
                        "content": "Administrators must not be included in privileged access reviews.",
                        "confidence": 0.89,
                        "source_evidence": "Administrators must be included in privileged access reviews.",
                        "source_span": None,
                        "review_reasons": ["source_backed"],
                        "structured_payload": {"rule": "Do not include administrators in access reviews."},
                        "inference_type": "abstractive",
                    }
                ]
            }
        ]
        self._create_memory(
            "hybrid-review-ux-memory",
            "Administrators must be included in privileged access reviews.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                    }
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Review UX Persona",
                "source_memory_ids": ["hybrid-review-ux-memory"],
                "distillation_mode": "hybrid",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]

        catalog = self.client.get("/persona-factory/item-types", headers=self.headers)
        self.assertEqual(catalog.status_code, 200, catalog.text)
        self.assertIn("hybrid", catalog.json()["distillation_modes"])
        self.assertIn("main_agent", catalog.json()["llm_model_sources"])
        self.assertIn("llm", catalog.json()["extraction_sources"])
        self.assertIn("prefer_llm", catalog.json()["reviewer_actions"])
        self.assertEqual(catalog.json()["model_profiles"][0]["id"], "persona-factory-model")

        run_detail = self.client.get(f"/persona-factory/runs/{run_id}", headers=self.headers)
        self.assertEqual(run_detail.status_code, 200, run_detail.text)
        self.assertEqual(run_detail.json()["review_summary"]["distillation_mode"], "hybrid")
        self.assertIn("review_metadata", run_detail.json()["items"][0])

        summary = self.client.get(f"/persona-factory/runs/{run_id}/review-summary", headers=self.headers)
        self.assertEqual(summary.status_code, 200, summary.text)
        summary_body = summary.json()
        self.assertEqual(summary_body["distillation_mode"], "hybrid")
        self.assertEqual(summary_body["resolved_model"]["model_profile_id"], "persona-factory-model")
        self.assertEqual(summary_body["counts"]["extraction_sources"]["llm"], 1)
        self.assertEqual(summary_body["counts"]["extraction_sources"]["deterministic"], 1)
        self.assertEqual(summary_body["hybrid_comparison"]["conflict_group_count"], 1)
        conflict_group_id = summary_body["hybrid_comparison"]["conflict_groups"][0]["id"]
        self.assertIn(conflict_group_id, summary_body["filter_options"]["conflict_group_ids"])

        llm_items = self.client.get(
            f"/persona-factory/runs/{run_id}/items",
            headers=self.headers,
            params={
                "extraction_source": "llm",
                "distiller": "llm_decision_distiller",
                "review_flag": "material_conflict",
                "conflict_group_id": conflict_group_id,
            },
        )
        self.assertEqual(llm_items.status_code, 200, llm_items.text)
        self.assertEqual(llm_items.json()["filtered_count"], 1)
        llm_item = llm_items.json()["items"][0]
        self.assertEqual(llm_item["review_metadata"]["extraction_source"], "llm")
        self.assertEqual(llm_item["review_metadata"]["model"]["provider"], "persona_fake")
        self.assertEqual(llm_item["review_metadata"]["source_evidence"], "Administrators must be included in privileged access reviews.")
        self.assertIn("prefer_llm", llm_item["review_metadata"]["reviewer_actions"])

        source_map = self.client.get(f"/persona-factory/runs/{run_id}/source-map", headers=self.headers)
        self.assertEqual(source_map.status_code, 200, source_map.text)
        self.assertEqual(source_map.json()["items"][0]["extraction_sources"]["llm"], 1)
        self.assertEqual(source_map.json()["items"][0]["extraction_sources"]["deterministic"], 1)

    def test_review_action_can_prefer_llm_conflict_items(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "candidates": [
                    {
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "title": "Administrator review inclusion rule",
                        "content": "Administrators must not be included in privileged access reviews.",
                        "confidence": 0.89,
                        "source_evidence": "Administrators must be included in privileged access reviews.",
                        "source_span": None,
                        "review_reasons": ["source_backed"],
                        "structured_payload": {"rule": "Do not include administrators in access reviews."},
                        "inference_type": "abstractive",
                    }
                ]
            }
        ]
        self._create_memory(
            "hybrid-review-action-memory",
            "Administrators must be included in privileged access reviews.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                    }
                }
            },
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Review Action Persona",
                "source_memory_ids": ["hybrid-review-action-memory"],
                "distillation_mode": "hybrid",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]
        summary = self.client.get(f"/persona-factory/runs/{run_id}/review-summary", headers=self.headers)
        self.assertEqual(summary.status_code, 200, summary.text)
        conflict_group_id = summary.json()["hybrid_comparison"]["conflict_groups"][0]["id"]

        action = self.client.post(
            f"/persona-factory/runs/{run_id}/review-actions",
            headers=self.headers,
            json={
                "action": "prefer_llm",
                "conflict_group_id": conflict_group_id,
                "reason": "LLM item captures the reviewer interpretation.",
            },
        )

        self.assertEqual(action.status_code, 200, action.text)
        action_body = action.json()
        self.assertEqual(action_body["action"], "prefer_llm")
        self.assertEqual(action_body["count"], 2)
        statuses_by_source = {
            item["review_metadata"]["extraction_source"]: item["review_status"]
            for item in action_body["items"]
        }
        self.assertEqual(statuses_by_source["llm"], "approved")
        self.assertEqual(statuses_by_source["deterministic"], "rejected")
        self.assertEqual(action_body["review_summary"]["counts"]["review_statuses"]["approved"], 1)
        self.assertEqual(action_body["review_summary"]["counts"]["review_statuses"]["rejected"], 1)

    def test_llm_mode_accepts_explicit_provider_model_source(self) -> None:
        self._create_model_profile()
        self._create_memory(
            "explicit-provider-model-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Explicit Provider Model Persona",
                "source_memory_ids": ["explicit-provider-model-memory"],
                "distillation_mode": "llm",
                "llm_model_source": "model",
                "llm_model_provider": "persona_fake",
                "llm_model": "fake-structured",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        self.assertEqual(run["distillation_mode"], "llm")
        self.assertEqual(run["llm_model_source"], "model")
        self.assertIsNone(run["model_profile_id"])
        self.assertIsNone(run["resolved_model_profile_id"])
        self.assertEqual(run["resolved_model_provider"], "persona_fake")
        self.assertEqual(run["resolved_model"], "fake-structured")
        item = distill.json()["items"][0]
        self.assertEqual(item["structured_payload"]["extractor"], "llm-distillation-v1")
        self.assertEqual(item["metadata"]["generated_by"], "llm")
        self.assertIn("llm_classifier", item["structured_payload"]["source_classification"]["signals"])

    def test_approved_llm_item_projects_reviewed_graph_hints(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [
            {
                "candidates": [
                    {
                        "item_type": "decision_pattern",
                        "memory_layer": "procedural",
                        "title": "Escalate incomplete privileged access reviews",
                        "content": (
                            "When privileged access review excludes administrators, "
                            "escalate the audit observation."
                        ),
                        "confidence": 0.9,
                        "source_evidence": "privileged access review excludes administrators",
                        "source_span": {"start": 5, "end": 54},
                        "review_reasons": ["source_backed"],
                        "structured_payload": {"rule": "Escalate incomplete access review scope."},
                        "inference_type": "extractive",
                        "suggested_graph_entities": [
                            {
                                "label": "Decision",
                                "name": "Privileged access review escalation",
                                "confidence": 0.88,
                                "evidence": "privileged access review excludes administrators",
                            },
                            {
                                "label": "Event",
                                "name": "Audit observation escalation",
                                "confidence": 0.82,
                                "evidence": "escalate the audit observation",
                            },
                        ],
                        "suggested_graph_relationships": [
                            {
                                "source_name": "Privileged access review escalation",
                                "relationship_type": "ESCALATES_TO",
                                "target_name": "Audit observation escalation",
                                "confidence": 0.84,
                                "evidence": "escalate the audit observation",
                            }
                        ],
                    }
                ]
            }
        ]
        self._create_memory(
            "llm-graph-hints-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                    }
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "LLM Graph Hint Persona",
                "source_memory_ids": ["llm-graph-hints-memory"],
                "distillation_mode": "llm",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        item = distill.json()["items"][0]
        self.assertEqual(item["structured_payload"]["graph_hints_review_status"], "pending_item_approval")
        before_approval_events = [
            event for event in self.context.graph_projection_event_repo._items.values()
            if event.event_type == "memory.source_intelligence.graph_hints.approved"
        ]
        self.assertEqual(before_approval_events, [])

        approve = self.client.post(
            f"/persona-factory/items/{item['id']}/approve",
            headers=self.headers,
        )

        self.assertEqual(approve.status_code, 200, approve.text)
        graph_events = [
            event for event in self.context.graph_projection_event_repo._items.values()
            if event.event_type == "memory.source_intelligence.graph_hints.approved"
        ]
        self.assertEqual(len(graph_events), 1)
        event = graph_events[0]
        self.assertEqual(event.aggregate_type, "memory")
        self.assertEqual(event.aggregate_id, "llm-graph-hints-memory")
        self.assertEqual(event.payload["persona_id"], distill.json()["persona"]["id"])
        self.assertEqual(event.payload["distillation_item_id"], item["id"])
        self.assertEqual(event.payload["graph_hint_source"], "persona_llm_distillation")
        self.assertEqual(event.payload["entities"][0]["label"], "Decision")
        self.assertEqual(event.payload["relationships"][0]["relationship_type"], "ESCALATES_TO")

    def test_llm_mode_rejects_invalid_llm_distillation_output(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [{"candidates": [{"bad": "shape"}]}]
        self._create_memory(
            "invalid-llm-distiller-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                        "vector_tags": ["access-review"],
                    }
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid LLM Distiller Persona",
                "source_memory_ids": ["invalid-llm-distiller-memory"],
                "distillation_mode": "llm",
            },
        )

        self.assertEqual(distill.status_code, 422)
        self.assertIn("LLM distillation output failed schema validation", distill.text)

    def test_hybrid_mode_falls_back_to_deterministic_when_llm_distillation_fails(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [{"candidates": [{"bad": "shape"}]}]
        self._create_memory(
            "hybrid-fallback-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                        "vector_tags": ["access-review"],
                    }
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Hybrid Fallback Persona",
                "source_memory_ids": ["hybrid-fallback-memory"],
                "distillation_mode": "hybrid",
            },
        )

        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        self.assertTrue(any(warning["type"] == "llm_distillation_failed" for warning in body["run"]["warnings"]))
        self.assertEqual(body["run"]["distillation_metrics"]["llm_distillation"]["failure_count"], 1)
        self.assertEqual(
            body["run"]["distillation_metrics"]["llm_distillation"]["sources"][0]["failure_reason"],
            "PersonaLLMDistillationError",
        )
        self.assertTrue(
            all(item["structured_payload"]["extractor"] == "deterministic-multi-distiller-v1" for item in body["items"])
        )

    def test_hybrid_mode_enforces_llm_call_limit_with_deterministic_fallback(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        for index in range(2):
            self._create_memory(
                f"hybrid-call-limit-memory-{index}",
                f"When privileged access review excludes administrators {index}, escalate the audit observation.",
                metadata={
                    "source_intelligence": {
                        "classification": {
                            "label": "decision",
                            "confidence": 0.91,
                            "document_kind": "workpaper",
                            "extraction_targets": ["decision_pattern"],
                            "memory_layers": ["procedural"],
                            "vector_tags": ["access-review"],
                        }
                    }
                },
            )

        with patch.dict(os.environ, {"PERSONA_FACTORY_LLM_MAX_CALLS_PER_RUN": "1"}):
            reset_settings_cache()
            distill = self.client.post(
                "/persona-factory/distill",
                headers=self.headers,
                json={
                    "name": "Hybrid Call Limit Persona",
                    "source_memory_ids": ["hybrid-call-limit-memory-0", "hybrid-call-limit-memory-1"],
                    "distillation_mode": "hybrid",
                },
            )
        reset_settings_cache()

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        llm_metrics = run["distillation_metrics"]["llm_distillation"]
        self.assertEqual(llm_metrics["call_count"], 1)
        self.assertEqual(llm_metrics["success_count"], 1)
        self.assertEqual(llm_metrics["failure_count"], 1)
        self.assertEqual(llm_metrics["last_failure_reason"], "call_limit_exceeded")
        self.assertTrue(
            any(warning["type"] == "llm_distillation_call_limit_exceeded" for warning in run["warnings"])
        )

    def test_hybrid_mode_times_out_llm_distillation_with_deterministic_fallback(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_delay_seconds = 0.05
        self._create_memory(
            "hybrid-timeout-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                        "vector_tags": ["access-review"],
                    }
                }
            },
        )

        with patch.dict(os.environ, {"PERSONA_FACTORY_LLM_TIMEOUT_SECONDS": "0.01"}):
            reset_settings_cache()
            distill = self.client.post(
                "/persona-factory/distill",
                headers=self.headers,
                json={
                    "name": "Hybrid Timeout Persona",
                    "source_memory_ids": ["hybrid-timeout-memory"],
                    "distillation_mode": "hybrid",
                },
            )
        reset_settings_cache()
        _PersonaFactoryFakeModelClient.structured_delay_seconds = 0.0

        self.assertEqual(distill.status_code, 200, distill.text)
        run = distill.json()["run"]
        llm_metrics = run["distillation_metrics"]["llm_distillation"]
        self.assertEqual(llm_metrics["call_count"], 1)
        self.assertEqual(llm_metrics["failure_count"], 1)
        self.assertEqual(llm_metrics["timeout_count"], 1)
        self.assertEqual(llm_metrics["last_failure_reason"], "timeout")
        self.assertTrue(any(warning.get("failure_reason") == "timeout" for warning in run["warnings"]))
        self.assertTrue(
            all(item["structured_payload"]["extractor"] == "deterministic-multi-distiller-v1" for item in distill.json()["items"])
        )

    def test_llm_distillation_retries_transient_model_errors(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_exceptions = [RuntimeError("temporary provider outage")]
        self._create_memory(
            "llm-retry-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "review_status": "approved",
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                        "vector_tags": ["access-review"],
                    },
                }
            },
        )

        try:
            with patch.dict(os.environ, {"PERSONA_FACTORY_LLM_RETRY_ATTEMPTS": "1"}):
                reset_settings_cache()
                distill = self.client.post(
                    "/persona-factory/distill",
                    headers=self.headers,
                    json={
                        "name": "LLM Retry Persona",
                        "source_memory_ids": ["llm-retry-memory"],
                        "distillation_mode": "llm",
                    },
                )
        finally:
            reset_settings_cache()

        self.assertEqual(distill.status_code, 200, distill.text)
        llm_metrics = distill.json()["run"]["distillation_metrics"]["llm_distillation"]
        self.assertEqual(llm_metrics["call_count"], 2)
        self.assertEqual(llm_metrics["retry_count"], 1)
        self.assertEqual(llm_metrics["transient_failure_count"], 1)
        self.assertEqual(llm_metrics["success_count"], 1)
        self.assertEqual(llm_metrics["failure_count"], 0)
        self.assertEqual([source["status"] for source in llm_metrics["sources"]], ["retrying", "success"])
        item = distill.json()["items"][0]
        self.assertEqual(item["metadata"]["llm_distillation_call"]["attempt_count"], 2)
        self.assertEqual(item["metadata"]["llm_distillation_call"]["retry_count"], 1)

    def test_llm_mode_reports_missing_main_agent_default_model(self) -> None:
        self._create_memory(
            "missing-main-agent-model-memory",
            "Audit review evidence should be complete before issue validation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Missing Main Agent Model Persona",
                "source_memory_ids": ["missing-main-agent-model-memory"],
                "distillation_mode": "llm",
            },
        )

        self.assertEqual(distill.status_code, 422)
        self.assertIn("Active main-agent profile was not found", distill.text)

    def test_llm_model_profile_source_requires_model_profile_id(self) -> None:
        self._create_memory(
            "missing-explicit-model-memory",
            "Audit review evidence should be complete before issue validation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Missing Explicit Model Persona",
                "source_memory_ids": ["missing-explicit-model-memory"],
                "distillation_mode": "llm",
                "llm_model_source": "model_profile",
            },
        )

        self.assertEqual(distill.status_code, 422)
        self.assertIn("model_profile_id is required", distill.text)

    def test_llm_model_source_requires_provider_and_model(self) -> None:
        self._create_memory(
            "missing-provider-model-memory",
            "Audit review evidence should be complete before issue validation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Missing Provider Model Persona",
                "source_memory_ids": ["missing-provider-model-memory"],
                "distillation_mode": "llm",
                "llm_model_source": "model",
            },
        )

        self.assertEqual(distill.status_code, 422)
        self.assertIn("llm_model_provider and llm_model are required", distill.text)

    def test_model_profile_classifier_rejects_invalid_structured_output(self) -> None:
        self._create_model_profile(profile_id="persona-factory-invalid-model")
        _PersonaFactoryFakeModelClient.structured_responses = [
            {"label": "unsupported_label", "confidence": 0.9, "signals": []}
        ]
        self._create_memory(
            "invalid-model-memory",
            "Audit review evidence should be complete before issue validation.",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid Model Persona",
                "source_memory_ids": ["invalid-model-memory"],
                "model_profile_id": "persona-factory-invalid-model",
            },
        )
        self.assertEqual(distill.status_code, 422)
        self.assertIn("schema validation", distill.text)

    def test_llm_distillation_failure_emits_audit_event(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        _PersonaFactoryFakeModelClient.structured_responses = [
            {"candidates": [{"item_type": "decision_pattern"}]}
        ]
        self._create_memory(
            "invalid-llm-candidate-memory",
            "When privileged access review excludes administrators, escalate the audit observation.",
            metadata={
                "source_intelligence": {
                    "review_status": "approved",
                    "classification": {
                        "label": "decision",
                        "confidence": 0.91,
                        "document_kind": "workpaper",
                        "extraction_targets": ["decision_pattern"],
                        "memory_layers": ["procedural"],
                        "vector_tags": ["access-review"],
                    },
                }
            },
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid LLM Candidate Persona",
                "source_memory_ids": ["invalid-llm-candidate-memory"],
                "distillation_mode": "llm",
            },
        )

        self.assertEqual(distill.status_code, 422)
        self.assertIn("schema validation", distill.text)
        projection_events = list(self.context.graph_projection_event_repo._items.values())
        failed_events = [
            event for event in projection_events
            if event.event_type == "persona.factory.distillation.failed"
        ]
        self.assertEqual(len(failed_events), 1)
        payload = failed_events[0].payload
        self.assertEqual(payload["distillation_mode"], "llm")
        self.assertEqual(payload["error"]["type"], "PersonaDistillationError")
        self.assertEqual(
            payload["distillation_metrics"]["llm_distillation"]["last_failure_reason"],
            "PersonaLLMDistillationError",
        )

    def test_distillation_enforces_configured_source_memory_limit(self) -> None:
        for index in range(3):
            self._create_memory(
                f"limit-memory-{index}",
                f"Audit source {index}: evidence should be reviewed before relying on the control.",
            )

        with patch.dict(os.environ, {"PERSONA_FACTORY_MAX_SOURCE_MEMORIES_PER_RUN": "2"}):
            reset_settings_cache()
            distill = self.client.post(
                "/persona-factory/distill",
                headers=self.headers,
                json={
                    "name": "Oversized Persona",
                    "source_memory_ids": ["limit-memory-0", "limit-memory-1", "limit-memory-2"],
                    "distillation_mode": "deterministic",
                },
            )
        reset_settings_cache()

        self.assertEqual(distill.status_code, 422)
        self.assertIn("Too many source memories", distill.text)

    def test_llm_distillation_enforces_llm_specific_source_limits(self) -> None:
        self._create_model_profile()
        self._create_main_agent_profile()
        for index in range(2):
            self._create_memory(
                f"llm-limit-memory-{index}",
                f"Audit source {index}: evidence should be reviewed before relying on the control.",
            )

        try:
            with patch.dict(os.environ, {"PERSONA_FACTORY_LLM_MAX_SOURCE_MEMORIES_PER_RUN": "1"}):
                reset_settings_cache()
                llm = self.client.post(
                    "/persona-factory/distill",
                    headers=self.headers,
                    json={
                        "name": "LLM Oversized Persona",
                        "source_memory_ids": ["llm-limit-memory-0", "llm-limit-memory-1"],
                        "distillation_mode": "llm",
                    },
                )
                deterministic = self.client.post(
                    "/persona-factory/distill",
                    headers=self.headers,
                    json={
                        "name": "Deterministic Limit Bypass Persona",
                        "source_memory_ids": ["llm-limit-memory-0", "llm-limit-memory-1"],
                        "distillation_mode": "deterministic",
                    },
                )
        finally:
            reset_settings_cache()

        self.assertEqual(llm.status_code, 422)
        self.assertIn("Too many source memories selected for one LLM persona distillation run", llm.text)
        self.assertEqual(deterministic.status_code, 200, deterministic.text)
        self.assertEqual(deterministic.json()["run"]["distillation_mode"], "deterministic")

    def test_distillation_classifies_conversation_samples(self) -> None:
        self._create_memory(
            "persona-conversation-source",
            (
                "User: Can you review this draft?\n"
                "Assistant: I would like to understand the evidence first, then I can help tighten the observation."
            ),
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Conversation Source Persona",
                "source_memory_ids": ["persona-conversation-source"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        item = distill.json()["items"][0]

        self.assertEqual(item["structured_payload"]["source_classification"]["label"], "conversation")
        self.assertEqual(item["metadata"]["source_classification"], "conversation")
        self.assertTrue(item["needs_review"])

    def test_workflow_distiller_extracts_operational_fields(self) -> None:
        self._create_memory(
            "workflow-fields-source",
            (
                "Release workflow starts when a production change request is opened.\n"
                "Owner: release manager.\n"
                "Inputs: Jira ticket, test evidence, and approval record.\n"
                "1. Confirm the change ticket.\n"
                "2. Validate testing evidence.\n"
                "Outputs: ServiceNow approval record and release notes.\n"
                "If testing fails, escalate to the release owner and block deployment."
            ),
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Workflow Fields Persona",
                "source_memory_ids": ["workflow-fields-source"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        workflow_item = next(item for item in distill.json()["items"] if item["item_type"] == "workflow")
        payload = workflow_item["structured_payload"]

        self.assertTrue(payload["steps"])
        self.assertTrue(payload["triggers"])
        self.assertTrue(payload["owners"])
        self.assertTrue(payload["inputs"])
        self.assertTrue(payload["outputs"])
        self.assertTrue(payload["failure_paths"])

    def test_normalize_run_merges_duplicate_items_and_preserves_sources(self) -> None:
        duplicate_content = "Audit evidence includes reviewer, timestamp, control ID, and test conclusion."
        self._create_memory("duplicate-memory-1", duplicate_content)
        self._create_memory("duplicate-memory-2", duplicate_content)

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Duplicate Normalization Persona",
                "source_memory_ids": ["duplicate-memory-1", "duplicate-memory-2"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]
        self.assertEqual(len(distill.json()["items"]), 2)
        items = distill.json()["items"]
        self.client.patch(
            f"/persona-factory/items/{items[0]['id']}",
            headers=self.headers,
            json={"patch": {"confidence": 0.91}},
        )
        self.client.patch(
            f"/persona-factory/items/{items[1]['id']}",
            headers=self.headers,
            json={"patch": {"confidence": 0.65}},
        )

        normalize = self.client.post(
            f"/persona-factory/runs/{run_id}/normalize",
            headers=self.headers,
        )
        self.assertEqual(normalize.status_code, 200, normalize.text)
        body = normalize.json()
        self.assertEqual(body["normalization"]["strategy"], "deterministic-normalization-v1")
        self.assertEqual(body["normalization"]["merged_duplicate_count"], 1)
        self.assertEqual(body["normalization"]["superseded_count"], 1)

        active = [item for item in body["items"] if item["review_status"] != "superseded"]
        superseded = [item for item in body["items"] if item["review_status"] == "superseded"]
        self.assertEqual(len(active), 1)
        self.assertEqual(len(superseded), 1)
        source_memory_ids = {
            ref["memory_id"]
            for ref in active[0]["structured_payload"]["source_refs"]
        }
        self.assertEqual(source_memory_ids, {"duplicate-memory-1", "duplicate-memory-2"})
        self.assertEqual(superseded[0]["metadata"]["superseded_by_item_id"], active[0]["id"])
        self.assertEqual(active[0]["confidence"], 0.91)

    def test_normalize_run_retains_conflicting_claims_for_review(self) -> None:
        self._create_memory(
            "conflict-memory-1",
            "Access review can rely on admin exclusion when compensating controls exist.",
            memory_type="decision",
        )
        self._create_memory(
            "conflict-memory-2",
            "Access review should not rely on admin exclusion when compensating controls exist.",
            memory_type="decision",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Conflict Normalization Persona",
                "source_memory_ids": ["conflict-memory-1", "conflict-memory-2"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]
        for item in distill.json()["items"]:
            self.client.patch(
                f"/persona-factory/items/{item['id']}",
                headers=self.headers,
                json={"patch": {"metadata": {**item["metadata"], "topic": "access review admin exclusion"}}},
            )

        normalize = self.client.post(
            f"/persona-factory/runs/{run_id}/normalize",
            headers=self.headers,
        )
        self.assertEqual(normalize.status_code, 200, normalize.text)
        body = normalize.json()
        self.assertEqual(body["normalization"]["conflict_group_count"], 1)
        active = [item for item in body["items"] if item["review_status"] != "superseded"]
        self.assertEqual(len(active), 2)
        self.assertTrue(all(item["needs_review"] for item in active))
        self.assertTrue(all(item["review_status"] == "needs_review" for item in active))
        self.assertTrue(all(item["metadata"]["conflict"] for item in active))
        self.assertTrue(all(item["metadata"]["conflicting_item_ids"] for item in active))

    def test_personal_identity_claims_are_forced_to_needs_review(self) -> None:
        self._create_memory(
            "identity-claim-memory",
            "If my ex-girlfriend Sarah is simulated, do not claim to be the actual person.",
            memory_type="decision",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Identity Claim Persona",
                "source_memory_ids": ["identity-claim-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        decision_item = next(item for item in distill.json()["items"] if item["item_type"] == "decision_pattern")

        self.assertTrue(decision_item["needs_review"])
        self.assertEqual(decision_item["review_status"], "needs_review")
        self.assertIn("personal_identity_claim", decision_item["structured_payload"]["review_flags"])
        self.assertIn("intimate_relationship", decision_item["structured_payload"]["identity_claim_signals"])
        self.assertTrue(decision_item["metadata"]["personal_identity_claim"])

    def test_item_synthesized_package_blocks_approval_when_items_need_review(self) -> None:
        self._create_memory(
            "persona-memory-1",
            "Jira is used to track audit evidence requests.",
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Needs Review Persona",
                "source_memory_ids": ["persona-memory-1"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]

        synthesize = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        self.assertEqual(synthesize.json()["run"]["output_package"]["provenance"]["needs_review_count"], 1)

        approve = self.client.post(
            f"/persona-factory/runs/{run_id}/approve",
            headers=self.headers,
            json={},
        )
        self.assertEqual(approve.status_code, 422, approve.text)
        self.assertIn("needs_review", approve.json()["detail"])

    def test_package_validation_errors_include_invalid_sections(self) -> None:
        self._create_memory(
            "invalid-package-memory",
            "Audit evidence must be reviewed before reporting.",
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid Package Persona",
                "source_memory_ids": ["invalid-package-memory"],
                "distillation_mode": "deterministic",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        run_id = distill.json()["run"]["id"]
        package = dict(distill.json()["run"]["output_package"])
        package["schema_version"] = 2
        package["knowledge"] = {"bad": "shape"}
        package["memory_layers"] = {"semantic": {"bad": "shape"}}

        update = self.client.patch(
            f"/persona-factory/runs/{run_id}/package",
            headers=self.headers,
            json={"package": package},
        )

        self.assertEqual(update.status_code, 422, update.text)
        detail = update.json()["detail"]
        self.assertIn("schema_version: must be 1", detail)
        self.assertIn("knowledge: must be a list", detail)
        self.assertIn("memory_layers.semantic: must be a list", detail)

    def test_personal_item_synthesized_package_requires_explicit_item_approval(self) -> None:
        self._create_memory(
            "persona-memory-1",
            "Older sibling persona writes in a direct and caring style.",
            memory_type="preference",
        )
        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Personal Draft Persona",
                "source_memory_ids": ["persona-memory-1"],
                "distillation_mode": "deterministic",
                "persona_type": "personal",
                "capability_mode": "persona_only",
                "consent_status": "explicit_consent",
                "source_basis": "chat_export",
                "sensitivity_level": "intimate",
                "visibility": "private",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        run_id = body["run"]["id"]
        item_id = body["items"][0]["id"]
        patch_item = self.client.patch(
            f"/persona-factory/items/{item_id}",
            headers=self.headers,
            json={"patch": {"review_status": "draft", "needs_review": False}},
        )
        self.assertEqual(patch_item.status_code, 200, patch_item.text)

        synthesize = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        approve = self.client.post(
            f"/persona-factory/runs/{run_id}/approve",
            headers=self.headers,
            json={},
        )
        self.assertEqual(approve.status_code, 422, approve.text)
        self.assertIn("explicitly approved", approve.json()["detail"])

        approve_item = self.client.post(
            f"/persona-factory/items/{item_id}/approve",
            headers=self.headers,
        )
        self.assertEqual(approve_item.status_code, 200, approve_item.text)
        synthesize = self.client.post(
            f"/persona-factory/runs/{run_id}/synthesize-package",
            headers=self.headers,
        )
        self.assertEqual(synthesize.status_code, 200, synthesize.text)
        approve = self.client.post(
            f"/persona-factory/runs/{run_id}/approve",
            headers=self.headers,
            json={},
        )
        self.assertEqual(approve.status_code, 200, approve.text)

    def test_persona_factory_governance_labels(self) -> None:
        labels = self.client.get("/persona-factory/governance-labels", headers=self.headers)
        self.assertEqual(labels.status_code, 200)
        label_body = labels.json()
        self.assertEqual(label_body["defaults"]["representation_policy"], "simulated_persona")
        self.assertIn("personal", label_body["allowed_values"]["persona_type"])
        self.assertTrue(label_body["validation_rules"])

        self._create_memory(
            "persona-memory-1",
            "Older sibling persona gives direct advice, avoids pretending to know private feelings, and de-escalates conflict.",
            memory_type="preference",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Older Sibling",
                "description": "A supportive personal persona based on provided source memories.",
                "source_memory_ids": ["persona-memory-1"],
                "distillation_mode": "deterministic",
                "persona_type": "personal",
                "capability_mode": "persona_only",
                "consent_status": "explicit_consent",
                "source_basis": "chat_export",
                "sensitivity_level": "intimate",
                "visibility": "private",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        body = distill.json()
        package = body["run"]["output_package"]
        self.assertEqual(body["persona"]["slug"], "older-sibling")
        self.assertEqual(body["items"][0]["item_type"], "writing_style")
        self.assertEqual(body["items"][0]["memory_layer"], "persona")
        self.assertEqual(body["items"][0]["review_status"], "needs_review")
        self.assertEqual(package["identity"]["persona_type"], "personal")
        self.assertEqual(package["governance"]["capability_mode"], "persona_only")
        self.assertEqual(package["governance"]["sensitivity_level"], "intimate")
        guardrail_titles = {item["title"] for item in package["guardrails"]}
        self.assertIn("Simulated persona disclosure", guardrail_titles)
        self.assertIn("No unsupported private facts", guardrail_titles)
        self.assertIn("Weak source uncertainty", guardrail_titles)

        run_id = body["run"]["id"]
        approve = self.client.post(
            f"/persona-factory/runs/{run_id}/approve",
            headers=self.headers,
            json={},
        )
        self.assertEqual(approve.status_code, 200, approve.text)
        publish = self.client.post(f"/persona-factory/runs/{run_id}/publish", headers=self.headers)
        self.assertEqual(publish.status_code, 200, publish.text)
        published = publish.json()
        self.assertIn("simulated persona", published["agent"]["instructions"])

        personas = self.client.get("/persona", headers=self.headers)
        self.assertEqual(personas.status_code, 200)
        self.assertEqual(personas.json()["items"][0]["slug"], "older-sibling")

        versions = self.client.get(f"/persona/{published['persona']['id']}/versions", headers=self.headers)
        self.assertEqual(versions.status_code, 200)
        self.assertEqual(versions.json()["items"][0]["package"]["governance"]["persona_type"], "personal")

    def test_governance_label_validation_rejects_invalid_consent_and_visibility(self) -> None:
        self._create_memory(
            "governance-memory-1",
            "Persona source material for governance validation.",
        )

        personal_public_material = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid Personal Public",
                "source_memory_ids": ["governance-memory-1"],
                "distillation_mode": "deterministic",
                "persona_type": "personal",
                "consent_status": "public_material",
                "source_basis": "public_sources",
                "visibility": "private",
            },
        )
        self.assertEqual(personal_public_material.status_code, 422, personal_public_material.text)
        self.assertIn("Personal personas require", personal_public_material.json()["detail"])

        private_marketplace = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid Marketplace",
                "source_memory_ids": ["governance-memory-1"],
                "distillation_mode": "deterministic",
                "persona_type": "professional",
                "consent_status": "explicit_consent",
                "source_basis": "uploaded_private_material",
                "visibility": "marketplace",
            },
        )
        self.assertEqual(private_marketplace.status_code, 422, private_marketplace.text)
        self.assertIn("Marketplace personas cannot be based", private_marketplace.json()["detail"])

        public_figure_marketplace = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Invalid Public Figure Marketplace",
                "source_memory_ids": ["governance-memory-1"],
                "distillation_mode": "deterministic",
                "persona_type": "public_figure",
                "consent_status": "explicit_consent",
                "source_basis": "mixed",
                "visibility": "marketplace",
            },
        )
        self.assertEqual(public_figure_marketplace.status_code, 422, public_figure_marketplace.text)
        self.assertIn("Marketplace public-figure personas require", public_figure_marketplace.json()["detail"])

    def test_governance_label_validation_allows_fictional_marketplace_persona(self) -> None:
        self._create_memory(
            "governance-memory-1",
            "Fictional helper persona uses concise coaching language.",
            memory_type="preference",
        )

        distill = self.client.post(
            "/persona-factory/distill",
            headers=self.headers,
            json={
                "name": "Fictional Coach",
                "source_memory_ids": ["governance-memory-1"],
                "distillation_mode": "deterministic",
                "persona_type": "fictional",
                "capability_mode": "persona_only",
                "consent_status": "fictional",
                "source_basis": "user_description",
                "sensitivity_level": "standard",
                "visibility": "marketplace",
            },
        )
        self.assertEqual(distill.status_code, 200, distill.text)
        package = distill.json()["run"]["output_package"]
        self.assertEqual(package["governance"]["persona_type"], "fictional")
        self.assertEqual(package["governance"]["visibility"], "marketplace")


if __name__ == "__main__":
    unittest.main()
