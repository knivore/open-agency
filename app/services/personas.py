"""Persona catalog service and package materialization helpers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.api.context import ApiContext
from app.core.time import utc_now
from app.domain import (
    AgentDefinition,
    MemorySettings,
    PersonaDefinition,
    PersonaStatus,
    PersonaVersion,
    PersonaVersionStatus,
    UserDefinition,
)


class PersonaConflictError(ValueError):
    pass


class PersonaNotFoundError(ValueError):
    pass


def slugify_persona(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:96] or "persona"


@dataclass(slots=True)
class PersonaService:
    context: ApiContext

    async def list_personas(self, *, include_archived: bool = False) -> list[PersonaDefinition]:
        return await self.context.persona_repo.list(include_deleted=include_archived)

    async def get_persona(self, persona_id: str) -> PersonaDefinition | None:
        return await self.context.persona_repo.get(persona_id)

    async def create_persona(self, payload: dict[str, Any], *,
                             current_user: UserDefinition | None) -> PersonaDefinition:
        name = str(payload.get("name") or "").strip()
        slug = slugify_persona(str(payload.get("slug") or name))
        existing = await self.context.persona_repo.find_by_slug(slug)
        if existing is not None and existing.status != PersonaStatus.ARCHIVED:
            raise PersonaConflictError(f"Persona slug '{slug}' already exists.")
        persona = PersonaDefinition.model_validate(
            {
                **payload,
                "slug": slug,
                "name": name or slug.replace("-", " ").title(),
                "created_by_user_id": payload.get("created_by_user_id") or getattr(current_user, "id", None),
                "status": payload.get("status") or PersonaStatus.DRAFT.value,
            }
        )
        return await self.context.persona_repo.create(persona)

    async def update_persona(self, persona_id: str, patch: dict[str, Any]) -> PersonaDefinition | None:
        normalized = dict(patch)
        if "slug" in normalized and normalized["slug"] is not None:
            normalized["slug"] = slugify_persona(str(normalized["slug"]))
            existing = await self.context.persona_repo.find_by_slug(normalized["slug"])
            if existing is not None and existing.id != persona_id and existing.status != PersonaStatus.ARCHIVED:
                raise PersonaConflictError(f"Persona slug '{normalized['slug']}' already exists.")
        return await self.context.persona_repo.update(persona_id, normalized)

    async def archive_persona(self, persona_id: str) -> bool:
        return await self.context.persona_repo.soft_delete(persona_id)

    async def list_versions(self, persona_id: str) -> list[dict[str, Any]]:
        versions = await self.context.persona_version_repo.list_by_persona(persona_id)
        return [item.model_dump(mode="json") for item in versions]

    async def list_sources(self, persona_id: str) -> list[dict[str, Any]]:
        sources = await self.context.persona_source_repo.list_by_persona(persona_id)
        return [item.model_dump(mode="json") for item in sources]

    async def export_persona(
            self,
            persona_id: str,
            *,
            version_id: str | None = None,
            export_format: str = "json",
    ) -> dict[str, Any]:
        persona = await self.context.persona_repo.get(persona_id, include_deleted=True)
        if persona is None:
            raise PersonaNotFoundError(f"Persona '{persona_id}' not found.")
        version = await self._export_version(persona, version_id=version_id)
        sources = await self.context.persona_source_repo.list_by_persona(persona.id)
        payload = {
            "export_type": "persona_package_json",
            "schema_version": 1,
            "terminology_note": (
                "Agency uses Persona as the canonical term. Other ecosystems may call a similar exported package a skill."
            ),
            "generated_at": utc_now().isoformat(),
            "persona": persona.model_dump(mode="json"),
            "persona_version": version.model_dump(mode="json"),
            "package": version.package,
            "sources": [item.model_dump(mode="json") for item in sources],
        }
        normalized_format = export_format.strip().lower()
        if normalized_format in {"json", "persona_json", "persona_package_json"}:
            return payload
        if normalized_format in {"skill_markdown", "markdown", "skill"}:
            return {
                "export_type": "skill_style_markdown",
                "schema_version": 1,
                "terminology_note": payload["terminology_note"],
                "generated_at": payload["generated_at"],
                "persona": payload["persona"],
                "persona_version": payload["persona_version"],
                "files": self._skill_style_files(persona=persona, version=version),
            }
        raise PersonaNotFoundError("Unsupported persona export format. Use 'json' or 'skill_markdown'.")

    async def _export_version(self, persona: PersonaDefinition, *, version_id: str | None) -> PersonaVersion:
        if version_id:
            version = await self.context.persona_version_repo.get(version_id, include_deleted=True)
            if version is None or version.persona_id != persona.id:
                raise PersonaNotFoundError(f"Persona version '{version_id}' not found.")
            return version
        if persona.current_version_id:
            version = await self.context.persona_version_repo.get(persona.current_version_id, include_deleted=True)
            if version is not None:
                return version
        versions = await self.context.persona_version_repo.list_by_persona(persona.id)
        if not versions:
            raise PersonaNotFoundError(f"Persona '{persona.id}' has no exportable versions.")
        return versions[0]

    async def import_persona(
            self,
            payload: dict[str, Any],
            *,
            current_user: UserDefinition | None,
    ) -> dict[str, Any]:
        import_format = str(payload.get("format") or "skill_markdown").strip().lower()
        if import_format not in {"skill_markdown", "markdown", "skill"}:
            raise PersonaNotFoundError("Unsupported persona import format. Use 'skill_markdown'.")
        files = payload.get("files")
        if not isinstance(files, dict) or not files:
            raise PersonaNotFoundError("Skill-style persona import requires a non-empty files object.")
        name = str(
            payload.get("name") or self._title_from_markdown(files.get("skill.md")) or "Imported Persona").strip()
        description = str(payload.get("description") or self._first_paragraph(files.get("persona.md")) or "").strip()
        persona = await self.create_persona(
            {
                "name": name,
                "slug": payload.get("slug"),
                "description": description or f"Imported persona package for {name}.",
                "workspace_id": payload.get("workspace_id"),
                "metadata": {
                    **(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}),
                    "import_format": "skill_markdown",
                    "imported_from_skill_style_package": True,
                },
            },
            current_user=current_user,
        )
        package = self._package_from_skill_style_files(persona=persona, files=files)
        version = await self.context.persona_version_repo.create(
            PersonaVersion(
                persona_id=persona.id,
                version=str(payload.get("version") or "1.0.0"),
                status=PersonaVersionStatus.DRAFT,
                package=package,
            )
        )
        persona = await self.context.persona_repo.update(
            persona.id,
            {
                "current_version_id": version.id,
                "metadata": {
                    **persona.metadata,
                    "imported_version_id": version.id,
                },
            },
        )
        return {
            "persona": persona.model_dump(mode="json"),
            "persona_version": version.model_dump(mode="json"),
            "package": package,
            "import_type": "skill_style_markdown",
        }

    async def agent_definition_for_package(
            self,
            *,
            persona: PersonaDefinition,
            version_id: str,
            package: dict[str, Any],
    ) -> AgentDefinition:
        agent_id = persona.published_agent_id or f"persona-agent-{persona.slug}"[:64]
        tool_ids = [
            str(item.get("tool_id"))
            for item in package.get("tools", [])
            if isinstance(item, dict) and item.get("granted") and item.get("tool_id")
        ]
        return AgentDefinition(
            id=agent_id,
            name=persona.slug,
            display_name=persona.name,
            description=persona.description or package.get("persona", {}).get("summary"),
            role=package.get("runtime", {}).get("default_agent_name") or persona.name,
            instructions=self.instructions_for_package(persona=persona, package=package),
            system_prompt=self.instructions_for_package(persona=persona, package=package),
            tool_ids=list(dict.fromkeys(tool_ids)),
            memory=MemorySettings(enabled=True, strategy="persona_factory", scope="user"),
            metadata={
                "persona_id": persona.id,
                "persona_slug": persona.slug,
                "persona_version_id": version_id,
                "generated_from_persona_factory": True,
                "updated_at": utc_now().isoformat(),
            },
        )

    def instructions_for_package(self, *, persona: PersonaDefinition, package: dict[str, Any]) -> str:
        persona_section = package.get("persona") if isinstance(package.get("persona"), dict) else {}
        lines = [
            f"You are the {persona.name} persona.",
            self._governance_instruction(package.get("governance")),
            persona_section.get("summary") or "",
            self._bullet_section("Communication style", persona_section.get("communication_style")),
            self._bullet_section("Preferences", persona_section.get("preferences")),
            self._item_section("Knowledge", package.get("knowledge"), "content"),
            self._item_section("Decision patterns", package.get("decision_patterns"), "content"),
            self._item_section("Workflows", package.get("workflows"), "description"),
            self._item_section("Guardrails", package.get("guardrails"), "content"),
            self._item_section("Examples", package.get("examples"), "content"),
            "Use source-backed knowledge conservatively. If confidence is low or sources conflict, say so.",
        ]
        return "\n\n".join(line for line in lines if isinstance(line, str) and line.strip())

    def _skill_style_files(self, *, persona: PersonaDefinition, version: PersonaVersion) -> dict[str, str]:
        package = version.package if isinstance(version.package, dict) else {}
        return {
            "skill.md": self._skill_md(persona=persona, version=version, package=package),
            "persona.md": self._persona_md(package),
            "workflow.md": self._items_md("Workflows", package.get("workflows"), "description"),
            "decision_patterns.md": self._items_md("Decision Patterns", package.get("decision_patterns"), "content"),
            "tools.yaml": self._tools_yaml(package.get("tools")),
            "guardrails.md": self._items_md("Guardrails", package.get("guardrails"), "content"),
            "examples.md": self._items_md("Examples", package.get("examples"), "content"),
        }

    def _skill_md(self, *, persona: PersonaDefinition, version: PersonaVersion, package: dict[str, Any]) -> str:
        return "\n\n".join(
            section
            for section in (
                f"# {persona.name}",
                (
                    "Agency Persona export. This is a skill-style interoperability artifact; "
                    "Agency's internal source of truth remains Persona."
                ),
                f"- Persona slug: `{persona.slug}`",
                f"- Version: `{version.version}`",
                f"- Package strategy: `{package.get('provenance', {}).get('strategy')}`",
                self.instructions_for_package(persona=persona, package=package),
            )
            if section
        )

    @staticmethod
    def _persona_md(package: dict[str, Any]) -> str:
        persona_section = package.get("persona") if isinstance(package.get("persona"), dict) else {}
        governance = package.get("governance") if isinstance(package.get("governance"), dict) else {}
        lines = ["# Persona"]
        if persona_section.get("summary"):
            lines.extend(["", str(persona_section["summary"])])
        for key in ("communication_style", "preferences"):
            values = persona_section.get(key)
            if isinstance(values, list) and values:
                lines.extend(["", f"## {key.replace('_', ' ').title()}"])
                lines.extend(f"- {value}" for value in values if str(value).strip())
        if governance:
            lines.extend(["", "## Governance", "```json", json.dumps(governance, indent=2, sort_keys=True), "```"])
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _items_md(title: str, items: Any, content_key: str) -> str:
        lines = [f"# {title}"]
        if not isinstance(items, list) or not items:
            lines.append("\nNo source-backed items exported.")
            return "\n".join(lines).strip() + "\n"
        for item in items:
            if not isinstance(item, dict):
                continue
            label = item.get("title") or item.get("name") or "Item"
            content = item.get(content_key) or item.get("content") or item.get("summary") or ""
            lines.extend(["", f"## {label}", "", str(content)])
            if item.get("confidence") is not None:
                lines.append(f"\nConfidence: `{item.get('confidence')}`")
            if item.get("distillation_item_id"):
                lines.append(f"Distillation item: `{item.get('distillation_item_id')}`")
        return "\n".join(lines).strip() + "\n"

    @staticmethod
    def _tools_yaml(items: Any) -> str:
        lines = ["tools:"]
        if not isinstance(items, list) or not items:
            lines.append("  []")
            return "\n".join(lines) + "\n"
        for item in items:
            if not isinstance(item, dict):
                continue
            lines.append(f"  - name: {json.dumps(str(item.get('name') or 'Tool'))}")
            lines.append(f"    tool_id: {json.dumps(item.get('tool_id'))}")
            lines.append(f"    granted: {str(bool(item.get('granted'))).lower()}")
            if item.get("confidence") is not None:
                lines.append(f"    confidence: {item.get('confidence')}")
            if item.get("rationale"):
                lines.append(f"    rationale: {json.dumps(str(item.get('rationale')))}")
        return "\n".join(lines) + "\n"

    def _package_from_skill_style_files(self, *, persona: PersonaDefinition, files: dict[str, Any]) -> dict[str, Any]:
        persona_md = str(files.get("persona.md") or "")
        skill_md = str(files.get("skill.md") or "")
        summary = self._first_paragraph(persona_md) or self._first_paragraph(skill_md)
        decision_patterns = self._markdown_items(files.get("decision_patterns.md"), "content")
        workflows = self._markdown_items(files.get("workflow.md"), "description")
        guardrails = self._markdown_items(files.get("guardrails.md"), "content")
        examples = self._markdown_items(files.get("examples.md"), "content")
        tools = self._tools_from_yaml_text(str(files.get("tools.yaml") or ""))
        knowledge = [
            {"title": item.get("title") or item.get("name"), "content": item.get("content") or item.get("description")}
            for item in [*decision_patterns, *workflows]
        ]
        memory_layers = {
            "semantic": knowledge,
            "procedural": [
                {"title": item.get("title") or item.get("name"),
                 "content": item.get("description") or item.get("content")}
                for item in [*decision_patterns, *workflows]
            ],
            "episodic": examples,
            "persona": [{"title": "Imported persona", "content": persona_md or skill_md}],
            "tool": tools,
            "social": [],
        }
        return {
            "schema_version": 1,
            "identity": {
                "kind": "persona",
                "slug": persona.slug,
                "display_name": persona.name,
                "persona_type": "professional",
            },
            "persona": {
                "summary": summary or persona.description or f"Imported persona package for {persona.name}.",
                "communication_style": [],
                "preferences": [],
                "escalation_style": "Escalate uncertainty and missing source support for human review.",
                "response_style": "source-grounded and concise",
            },
            "governance": {
                "persona_type": "professional",
                "capability_mode": "persona_plus_expertise",
                "consent_status": "unspecified",
                "source_basis": "user_description",
                "sensitivity_level": "standard",
                "visibility": "private",
                "representation_policy": "simulated_persona",
            },
            "knowledge": knowledge[:50],
            "decision_patterns": decision_patterns[:30],
            "workflows": workflows[:30],
            "tools": tools[:30],
            "guardrails": guardrails[:20],
            "examples": examples[:20],
            "memory_layers": memory_layers,
            "runtime": {
                "default_agent_name": persona.name,
                "default_workflow_id": None,
                "invocation_names": [persona.slug, persona.name],
                "product_concept": "persona",
                "internal_package_type": "persona",
            },
            "provenance": {
                "source_ids": [],
                "source_memory_ids": [],
                "distillation_run_id": None,
                "generated_at": utc_now().isoformat(),
                "strategy": "skill-style-import-v1",
                "imported_files": sorted(str(key) for key in files),
            },
        }

    @staticmethod
    def _title_from_markdown(value: Any) -> str | None:
        for line in str(value or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
        return None

    @staticmethod
    def _first_paragraph(value: Any) -> str | None:
        text = str(value or "").strip()
        for block in re.split(r"\n\s*\n", text):
            cleaned = "\n".join(line.strip() for line in block.splitlines() if not line.strip().startswith("#"))
            cleaned = " ".join(cleaned.split())
            if cleaned and not cleaned.startswith("```"):
                return cleaned[:1000]
        return None

    @staticmethod
    def _markdown_items(value: Any, content_key: str) -> list[dict[str, Any]]:
        text = str(value or "").strip()
        if not text:
            return []
        items: list[dict[str, Any]] = []
        current_title: str | None = None
        current_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if current_title and current_lines:
                    content = " ".join(" ".join(current_lines).split())
                    items.append({"title": current_title, content_key: content})
                current_title = line[3:].strip()
                current_lines = []
            elif current_title and not line.startswith("#"):
                current_lines.append(line.strip())
        if current_title and current_lines:
            content = " ".join(" ".join(current_lines).split())
            items.append({"title": current_title, content_key: content})
        if items:
            return items
        paragraph = PersonaService._first_paragraph(text)
        return [{"title": "Imported item", content_key: paragraph}] if paragraph else []

    @staticmethod
    def _tools_from_yaml_text(value: str) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in value.splitlines():
            line = raw_line.strip()
            if line.startswith("- name:"):
                if current:
                    tools.append(current)
                current = {"name": PersonaService._unquote_yaml_value(line.split(":", 1)[1].strip()), "granted": False}
            elif current is not None and ":" in line:
                key, raw_value = line.split(":", 1)
                value_text = raw_value.strip()
                if key == "tool_id":
                    current["tool_id"] = PersonaService._unquote_yaml_value(value_text)
                elif key == "granted":
                    current["granted"] = value_text.lower() == "true"
                elif key == "confidence":
                    try:
                        current["confidence"] = float(value_text)
                    except ValueError:
                        current["confidence"] = None
                elif key == "rationale":
                    current["rationale"] = PersonaService._unquote_yaml_value(value_text)
        if current:
            tools.append(current)
        return tools

    @staticmethod
    def _unquote_yaml_value(value: str) -> Any:
        if value in {"null", "None", ""}:
            return None
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip("'\"")

    @staticmethod
    def _governance_instruction(governance: Any) -> str:
        if not isinstance(governance, dict):
            return "Represent this as a simulated persona based on provided source material, not as the actual person."
        persona_type = governance.get("persona_type") or "professional"
        consent_status = governance.get("consent_status") or "unspecified"
        sensitivity = governance.get("sensitivity_level") or "standard"
        lines = [
            "Represent this as a simulated persona based on provided source material, not as the actual person.",
            f"Governance labels: persona_type={persona_type}; consent_status={consent_status}; sensitivity_level={sensitivity}.",
        ]
        if persona_type == "personal" or consent_status == "unverified_private_person" or sensitivity == "intimate":
            lines.append(
                "Avoid claiming private access, certainty about feelings, or real-world continuity beyond the sources.")
        return " ".join(lines)

    @staticmethod
    def _bullet_section(title: str, items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        return f"{title}:\n" + "\n".join(f"- {item}" for item in items if str(item).strip())

    @staticmethod
    def _item_section(title: str, items: Any, content_key: str) -> str:
        if not isinstance(items, list) or not items:
            return ""
        lines = [f"{title}:"]
        for item in items[:20]:
            if isinstance(item, dict):
                label = item.get("title") or item.get("name") or "Item"
                content = item.get(content_key) or item.get("summary") or ""
                lines.append(f"- {label}: {content}")
            elif str(item).strip():
                lines.append(f"- {item}")
        return "\n".join(lines)


__all__ = ["PersonaConflictError", "PersonaNotFoundError", "PersonaService", "slugify_persona"]
