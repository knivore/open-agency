"""Persona graph-context retrieval for CLI/API inspection and runtime prompts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.graph.neo4j_read import GraphReadDocument, Neo4jGraphReadError
from app.graph.service import (
    GraphReadUnavailableError,
    close_graph_reader_if_needed,
    graph_document_payload,
    resolve_graph_reader,
)
from app.services.personas import PersonaNotFoundError


class PersonaGraphContextError(RuntimeError):
    pass


PERSONA_GRAPH_CONTEXT_PRESETS = {"persona_lineage", "persona_capability_map"}
PERSONA_GRAPH_CONTEXT_RUNTIME_POLICY = {
    "invocation_type": "persona_runtime",
    "preset": "persona_lineage",
    "limit": 24,
    "max_edges": 24,
    "fallback": "skip_graph_context_without_failing_invocation",
    "source_priority": [
        "persona_package",
        "approved_persona_memory",
        "persona_graph_context",
        "conversation_context",
    ],
}


@dataclass(slots=True)
class PersonaGraphContextService:
    context: Any

    async def build_context(
            self,
            persona_id: str,
            *,
            query: str | None = None,
            limit: int = 24,
            preset: str = "persona_lineage",
            current_user: Any | None = None,
    ) -> dict[str, Any]:
        persona = await self.context.persona_repo.get(persona_id, include_deleted=True)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{persona_id}' not found")
        policy = self.inspection_policy(preset=preset, limit=limit)
        document = await self._load_document(persona, query=query, policy=policy, current_user=current_user)
        prompt = self.render_prompt(document, policy=policy)
        return {
            "persona": persona.model_dump(mode="json") if hasattr(persona, "model_dump") else persona,
            "status": "ok",
            "policy": policy,
            "prompt": prompt,
            "graph": graph_document_payload(
                document,
                query_meta={
                    "query": "persona_graph_context",
                    "preset": policy["preset"],
                    "persona_id": persona.id,
                    "query_text": query,
                    "invocation_type": policy["invocation_type"],
                },
                limit=limit,
                max_edges=int(policy["max_edges"]),
            ),
        }

    async def prompt_for_persona(self, persona: Any, *, query: str | None = None, limit: int = 24) -> str:
        context = await self.prompt_context_for_persona(persona, query=query, limit=limit)
        return str(context.get("prompt") or "")

    async def prompt_context_for_persona(self, persona: Any, *, query: str | None = None, limit: int = 24) -> dict[
        str, Any]:
        policy = self.runtime_policy(limit=limit)
        document = await self._load_document(persona, query=query, policy=policy, current_user=None)
        prompt = self.render_prompt(document, policy=policy)
        return {
            "status": "ok" if prompt.strip() else "empty",
            "policy": policy,
            "prompt": prompt,
            "node_count": len(list(getattr(document, "nodes", []) or [])),
            "edge_count": len(list(getattr(document, "edges", []) or [])),
            "meta": getattr(document, "meta", {}) if isinstance(getattr(document, "meta", {}), dict) else {},
        }

    async def _load_document(
            self,
            persona: Any,
            *,
            query: str | None,
            policy: dict[str, Any],
            current_user: Any | None,
    ) -> GraphReadDocument:
        reader, close_after = resolve_graph_reader(self.context)
        if getattr(current_user, "id", None):
            setattr(reader, "access_user_id", current_user.id)
        limit = max(1, min(int(policy.get("limit") or 24), 100))
        preset = str(policy.get("preset") or "persona_lineage")
        try:
            if hasattr(reader, "get_graph_preset"):
                return await reader.get_graph_preset(
                    preset,
                    persona_id=persona.id,
                    limit=limit,
                )
            return await reader.search_nodes(
                query or getattr(persona, "name", None) or getattr(persona, "slug", None) or persona.id,
                labels=["Persona", "Memory", "Entity", "Tool", "Workflow", "Artifact", "Decision", "Knowledge"],
                limit=limit,
            )
        finally:
            await close_graph_reader_if_needed(reader, close_after)

    @staticmethod
    def runtime_policy(*, limit: int = 24) -> dict[str, Any]:
        policy = dict(PERSONA_GRAPH_CONTEXT_RUNTIME_POLICY)
        policy["limit"] = max(1, min(int(limit), 100))
        policy["max_edges"] = policy["limit"]
        return policy

    @staticmethod
    def inspection_policy(*, preset: str = "persona_lineage", limit: int = 24) -> dict[str, Any]:
        preset_key = str(preset or "persona_lineage").strip().lower().replace("-", "_")
        if preset_key not in PERSONA_GRAPH_CONTEXT_PRESETS:
            allowed = ", ".join(sorted(PERSONA_GRAPH_CONTEXT_PRESETS))
            raise ValueError(f"Unsupported persona graph context preset '{preset}'. Use one of: {allowed}")
        bounded_limit = max(1, min(int(limit), 100))
        return {
            "invocation_type": "persona_inspection",
            "preset": preset_key,
            "limit": bounded_limit,
            "max_edges": bounded_limit,
            "fallback": "surface_graph_read_error_to_caller",
            "source_priority": [
                "persona_package",
                "approved_persona_memory",
                "persona_graph_context",
            ],
        }

    @staticmethod
    def render_prompt(document: Any, *, policy: dict[str, Any] | None = None) -> str:
        nodes = list(getattr(document, "nodes", []) or [])
        edges = list(getattr(document, "edges", []) or [])
        if not nodes and not edges:
            return ""
        policy = policy or PERSONA_GRAPH_CONTEXT_RUNTIME_POLICY
        node_names: dict[str, str] = {}
        lines = [
            "# Persona Graph Context",
            (
                "Use this reviewed graph context as supporting context; do not treat it as a replacement for "
                "source-backed persona memory."
            ),
            (
                f"Policy: preset={policy.get('preset')}, limit={policy.get('limit')}, "
                f"fallback={policy.get('fallback')}."
            ),
            "If graph context conflicts with approved persona memory or the persona package, prefer the approved memory/package.",
        ]
        if nodes:
            lines.append("Nodes:")
            for node in nodes[:10]:
                properties = getattr(node, "properties", {}) if isinstance(getattr(node, "properties", {}),
                                                                           dict) else {}
                node_id = str(getattr(node, "id", "") or "")
                node_type = str(getattr(node, "type", "") or "")
                label = _first_graph_text(
                    properties.get("name"),
                    properties.get("summary"),
                    properties.get("title"),
                    properties.get("filename"),
                    properties.get("source_label"),
                    node_id,
                )
                node_names[node_id] = label
                details = _first_graph_text(
                    properties.get("evidence"),
                    properties.get("review_status"),
                    properties.get("entity_type"),
                    properties.get("status"),
                )
                suffix = f" - {details}" if details and details != label else ""
                lines.append(f"- [{node_type or 'Node'}] {label}{suffix}")
        if edges:
            lines.append("Relationships:")
            for edge in edges[:12]:
                source = node_names.get(str(getattr(edge, "source", "") or ""),
                                        str(getattr(edge, "source", "") or "source"))
                target = node_names.get(str(getattr(edge, "target", "") or ""),
                                        str(getattr(edge, "target", "") or "target"))
                relationship_type = str(getattr(edge, "type", "") or "RELATED_TO")
                lines.append(f"- {source} -{relationship_type}-> {target}")
        return "\n".join(lines)


def _first_graph_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return _truncate_text(text, 180)
    return ""


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}... [truncated]"


__all__ = [
    "GraphReadUnavailableError",
    "Neo4jGraphReadError",
    "PersonaGraphContextError",
    "PersonaGraphContextService",
]
