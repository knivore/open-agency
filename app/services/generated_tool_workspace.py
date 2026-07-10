"""Workspace service for coder-agent-authored generated tools.

This keeps generated tool package creation and registration behind one service
instead of letting runtime handlers hand-roll file layout, manifest updates, and
ToolDefinition assembly in multiple places.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain import FrameworkHints, SecuritySettings, ToolDefinition, ToolImplementationReference, ToolType
from app.tools.discovery import generated_tools_root

_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")


class GeneratedToolWorkspaceError(ValueError):
    pass


def _slugify(value: str, *, fallback: str) -> str:
    normalized = _SLUG_PATTERN.sub("_", value.strip().lower()).strip("_")
    return normalized or fallback


def _python_identifier(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        normalized = fallback
    if normalized[0].isdigit():
        normalized = f"tool_{normalized}"
    return normalized


def _display_name(value: str) -> str:
    words = [part for part in re.split(r"[^A-Za-z0-9]+", value) if part]
    return " ".join(word[:1].upper() + word[1:] for word in words) or "Generated Tool"


@dataclass(slots=True)
class GeneratedToolWorkspaceService:
    context: Any
    root_path: Path | None = None

    @property
    def root(self) -> Path:
        return self.root_path or generated_tools_root()

    def list_packages(self) -> dict[str, Any]:
        packages: list[dict[str, Any]] = []
        for manifest_path in sorted(self.root.glob("*/manifest.yaml")):
            summary = self._package_summary_from_manifest(manifest_path)
            if summary:
                packages.append(summary)
        return {"packages": packages, "count": len(packages)}

    async def list_packages_with_registry(self) -> dict[str, Any]:
        packages = self.list_packages()
        tools = await self.context.tool_repo.list() if hasattr(self.context, "tool_repo") else []
        tool_by_id = {tool.id: tool for tool in tools}
        for package in packages["packages"]:
            module_root = str(package.get("module_root") or "")
            tool_modules = package.get("tool_modules") if isinstance(package.get("tool_modules"), list) else []
            registered_tools = [
                tool.model_dump(mode="json")
                for tool in tool_by_id.values()
                if _tool_belongs_to_generated_package(tool, module_root=module_root, tool_modules=tool_modules)
            ]
            package["registered_tools"] = registered_tools
            package["published_tool_count"] = len(registered_tools)
            package["package_state"] = "published" if registered_tools else "scaffolded"
        return packages

    async def inspect_package(self, package_id: str) -> dict[str, Any]:
        package = self._package_manifest(package_id)
        slug = _slugify(package_id, fallback="generated-tool")
        package_root = self.root / slug
        manifest_path = package_root / "manifest.yaml"
        summary = self._package_summary_from_manifest(manifest_path)
        if summary is None:
            raise GeneratedToolWorkspaceError(f"Generated tool package '{slug}' was not found.")
        tools = await self.context.tool_repo.list() if hasattr(self.context, "tool_repo") else []
        module_root = str(summary.get("module_root") or "")
        tool_modules = summary.get("tool_modules") if isinstance(summary.get("tool_modules"), list) else []
        registered_tools = [
            tool.model_dump(mode="json")
            for tool in tools
            if _tool_belongs_to_generated_package(tool, module_root=module_root, tool_modules=tool_modules)
        ]
        readme_path = package_root / "README.md"
        readme_preview = None
        if readme_path.exists():
            readme_preview = readme_path.read_text(encoding="utf-8")[:4000]
        files = []
        for path in sorted(package_root.iterdir()):
            if path.name == "__pycache__":
                continue
            files.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "kind": "directory" if path.is_dir() else "file",
                    "size_bytes": path.stat().st_size if path.is_file() else None,
                }
            )
        return {
            **summary,
            "manifest": package,
            "registered_tools": registered_tools,
            "published_tool_count": len(registered_tools),
            "package_state": "published" if registered_tools else "scaffolded",
            "files": files,
            "readme_preview": readme_preview,
        }

    def scaffold_package(
            self,
            *,
            package_id: str,
            name: str,
            description: str | None = None,
            function_name: str | None = None,
            overwrite: bool = False,
    ) -> dict[str, Any]:
        slug = _slugify(package_id, fallback="generated-tool")
        package_root = self.root / slug
        module_root = f"generated_tools.{slug}"
        callable_name = _python_identifier(function_name or "run", fallback="run")

        if package_root.exists() and not overwrite:
            raise GeneratedToolWorkspaceError(f"Generated tool package '{slug}' already exists.")

        package_root.mkdir(parents=True, exist_ok=True)
        init_path = package_root / "__init__.py"
        manifest_path = package_root / "manifest.yaml"
        requirements_path = package_root / "requirements.txt"
        readme_path = package_root / "README.md"
        tools_path = package_root / "tools.py"

        manifest_payload = {
            "id": slug,
            "name": name.strip() or _display_name(slug),
            "version": "0.1.0",
            "enabled": True,
            "module_root": module_root,
            "tool_modules": [f"{module_root}.tools"],
            "requirements_file": "requirements.txt",
            "metadata": {
                "owner": "coder-agent",
                "visibility": "shared",
                "description": (description or "").strip(),
            },
        }
        init_path.write_text('"""Coder-agent-authored generated tool package."""\n', encoding="utf-8")
        manifest_path.write_text(yaml.safe_dump(manifest_payload, sort_keys=False), encoding="utf-8")
        requirements_path.write_text("", encoding="utf-8")
        if not readme_path.exists() or overwrite:
            readme_path.write_text(
                _package_readme_template(
                    package_id=slug,
                    name=manifest_payload["name"],
                    module_root=module_root,
                    callable_name=callable_name,
                    description=manifest_payload["metadata"]["description"],
                ),
                encoding="utf-8",
            )
        if not tools_path.exists() or overwrite:
            tools_path.write_text(
                _tool_template(callable_name=callable_name, description=description),
                encoding="utf-8",
            )

        return {
            "package_id": slug,
            "name": manifest_payload["name"],
            "root_path": str(package_root),
            "manifest_path": str(manifest_path),
            "module_root": module_root,
            "tool_modules": manifest_payload["tool_modules"],
            "callable_name": callable_name,
            "files": [
                str(init_path),
                str(manifest_path),
                str(requirements_path),
                str(readme_path),
                str(tools_path),
            ],
        }

    async def publish_tool(
            self,
            *,
            package_id: str,
            tool_id: str,
            name: str,
            description: str,
            callable_name: str,
            input_schema: dict[str, Any],
            output_schema: dict[str, Any],
            tags: list[str] | None = None,
            security: dict[str, Any] | None = None,
            display_name: str | None = None,
    ) -> ToolDefinition:
        package = self._package_manifest(package_id)
        module_root = str(package.get("module_root") or "")
        tool_modules = package.get("tool_modules") if isinstance(package.get("tool_modules"), list) else []
        if not tool_modules:
            raise GeneratedToolWorkspaceError(
                f"Generated tool package '{package_id}' does not declare any tool modules.")
        module_name = str(tool_modules[0])
        self._assert_callable_exists(module_name, callable_name)

        security_payload = {
            "requires_approval": False,
            "sandbox_required": False,
            "allow_shell": False,
            "allow_browser": False,
            "allow_filesystem": False,
            "allow_network": False,
            "allowlisted_domains": [],
            "allowlisted_mcp_servers": [],
            "module_allowlist": [module_name],
            "function_allowlist": [callable_name],
            "read_only_sql": True,
            "read_only": False,
            "dangerous": False,
            "approval_on_rejection": "fail",
            "credential_references": [],
            "connector_bindings": [],
            "redaction_enabled": False,
            "redaction_rules": [],
        }
        if isinstance(security, dict):
            security_payload.update(security)
            security_payload["module_allowlist"] = [module_name]
            security_payload["function_allowlist"] = [callable_name]

        tool = ToolDefinition(
            id=tool_id.strip(),
            name=name.strip(),
            display_name=(display_name or "").strip() or None,
            description=description.strip(),
            tool_type=ToolType.PYTHON_FUNCTION,
            input_schema=input_schema,
            output_schema=output_schema,
            implementation=ToolImplementationReference(
                implementation_type="python_function",
                target=module_name,
                callable_name=callable_name,
                config={},
            ),
            security=SecuritySettings.model_validate(security_payload),
            tags=sorted(set([*(tags or []), "generated_tool", f"generated_package:{package_id}"])),
            framework_hints=FrameworkHints(
                metadata={
                    "generated_tool": {
                        "package_id": package_id,
                        "module_root": module_root,
                        "module_name": module_name,
                        "callable_name": callable_name,
                    }
                }
            ),
        )
        return await self.context.tool_repo.save(tool)

    def _package_manifest(self, package_id: str) -> dict[str, Any]:
        slug = _slugify(package_id, fallback="generated-tool")
        manifest_path = self.root / slug / "manifest.yaml"
        if not manifest_path.exists():
            raise GeneratedToolWorkspaceError(f"Generated tool package '{slug}' was not found.")
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise GeneratedToolWorkspaceError(f"Generated tool package '{slug}' has an invalid manifest.")
        return payload

    def _package_summary_from_manifest(self, manifest_path: Path) -> dict[str, Any] | None:
        package_root = manifest_path.parent
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            return None
        module_root = str(payload.get("module_root") or "")
        tool_modules = payload.get("tool_modules") if isinstance(payload.get("tool_modules"), list) else []
        readme_path = package_root / "README.md"
        requirements_path = package_root / "requirements.txt"
        return {
            "package_id": payload.get("id") or package_root.name,
            "name": payload.get("name") or package_root.name,
            "root_path": str(package_root),
            "manifest_path": str(manifest_path),
            "readme_path": str(readme_path),
            "requirements_path": str(requirements_path),
            "module_root": module_root,
            "tool_modules": tool_modules,
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
            "package_state": "scaffolded",
            "has_readme": readme_path.exists(),
            "has_requirements": requirements_path.exists(),
            "registered_tools": [],
        }

    def _assert_callable_exists(self, module_name: str, callable_name: str) -> None:
        module = self._load_generated_module_from_root(module_name)
        if module is not None:
            if not hasattr(module, callable_name):
                raise GeneratedToolWorkspaceError(
                    f"Callable '{callable_name}' was not found in generated tool module '{module_name}'."
                )
            return
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # pragma: no cover - surfaced as user-facing validation
            raise GeneratedToolWorkspaceError(
                f"Generated tool module '{module_name}' could not be imported: {exc}"
            ) from exc
        if not hasattr(module, callable_name):
            raise GeneratedToolWorkspaceError(
                f"Callable '{callable_name}' was not found in generated tool module '{module_name}'."
            )

    def _load_generated_module_from_root(self, module_name: str) -> Any | None:
        prefix = "generated_tools."
        if not module_name.startswith(prefix):
            return None
        relative_parts = module_name.removeprefix(prefix).split(".")
        module_path = self.root.joinpath(*relative_parts).with_suffix(".py")
        if not module_path.exists():
            return None
        # Validate the just-scaffolded file by path so test and runtime import
        # caches for another generated_tools package cannot shadow this root.
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _tool_template(*, callable_name: str, description: str | None) -> str:
    note = (description or "Implement tool logic here.").strip()
    return (
        "from __future__ import annotations\n\n\n"
        f"def {callable_name}(text: str = \"\", tool_context=None) -> dict[str, object]:\n"
        f"    \"\"\"{note}\"\"\"\n"
        "    # Keep the initial scaffold small and deterministic so coder-agent edits stay local.\n"
        "    return {\n"
        f"        \"message\": \"TODO: implement {callable_name}\",\n"
        "        \"text\": text,\n"
        "        \"execution_id\": getattr(tool_context, \"execution_id\", None) if tool_context is not None else None,\n"
        "    }\n"
    )


def _package_readme_template(
        *,
        package_id: str,
        name: str,
        module_root: str,
        callable_name: str,
        description: str,
) -> str:
    details = description.strip() or "Coder-agent-authored generated tool package."
    return (
        f"# {name}\n\n"
        f"{details}\n\n"
        "## Purpose\n\n"
        "This package lives under `generated_tools/` so coder-agent-authored shared tools stay separate "
        "from core app implementations.\n\n"
        "## Package Contract\n\n"
        f"- Package id: `{package_id}`\n"
        f"- Module root: `{module_root}`\n"
        f"- Starter callable: `{callable_name}`\n"
        "- Manifest: `manifest.yaml`\n"
        "- Module entrypoint: `tools.py`\n"
        "- Optional dependencies: `requirements.txt`\n\n"
        "## Publish Flow\n\n"
        "1. Implement or update the callable in `tools.py`.\n"
        "2. Keep the callable importable from the module declared in `manifest.yaml`.\n"
        "3. Publish a ToolDefinition through `agency.tool.workspace.publish` so workflows, agents, and personas can grant and use the tool.\n"
    )


def _tool_belongs_to_generated_package(tool: ToolDefinition, *, module_root: str, tool_modules: list[str]) -> bool:
    if tool.implementation.target in tool_modules:
        return True
    metadata = tool.framework_hints.metadata if isinstance(tool.framework_hints.metadata, dict) else {}
    generated = metadata.get("generated_tool") if isinstance(metadata.get("generated_tool"), dict) else {}
    return generated.get("module_root") == module_root
