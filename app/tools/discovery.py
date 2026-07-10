from __future__ import annotations

import yaml
from pathlib import Path
from pydantic import Field, ValidationError, field_validator
from typing import Any

from app.domain import DomainModel


class IntegrationManifest(DomainModel):
    id: str
    name: str
    version: str = "0.1.0"
    enabled: bool = True
    module_root: str
    tool_modules: list[str] = Field(default_factory=list)
    requirements_file: str | None = "requirements.txt"
    env: list[str] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tool_modules")
    @classmethod
    def ensure_tool_modules_present(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("tool_modules must include at least one module")
        return value


class DiscoveredIntegration(DomainModel):
    manifest_path: str
    root_path: str
    manifest: IntegrationManifest

    @property
    def tool_modules(self) -> list[str]:
        return self.manifest.tool_modules


def _discover_manifest_modules(*, root: Path, strict: bool = False) -> list[DiscoveredIntegration]:
    if not root.exists():
        return []

    discovered: list[DiscoveredIntegration] = []
    manifest_paths = sorted(root.glob("*/manifest.yaml"))
    for manifest_path in manifest_paths:
        try:
            payload = _load_manifest_payload(manifest_path)
            manifest = IntegrationManifest.model_validate(payload)
            if not manifest.enabled:
                continue
            integration_root = manifest_path.parent
            for module_name in manifest.tool_modules:
                relative_parts = module_name.split(".")
                module_path = root.parent / Path(*relative_parts)
                if not module_path.with_suffix(".py").exists():
                    raise ValueError(f"tool module '{module_name}' does not resolve to a file")
            discovered.append(
                DiscoveredIntegration(
                    manifest_path=str(manifest_path),
                    root_path=str(integration_root),
                    manifest=manifest,
                )
            )
        except (OSError, ValidationError, ValueError) as exc:
            if strict:
                raise RuntimeError(f"Invalid integration manifest at {manifest_path}: {exc}") from exc
            continue
    return sorted(discovered, key=lambda item: (item.manifest.name.lower(), item.manifest.id))


def discover_builtin_tool_modules() -> list[str]:
    root = Path(__file__).resolve().parent / "implementations"
    modules: list[str] = []
    if not root.exists():
        return modules

    for path in root.rglob("*.py"):
        if path.name == "__init__.py":
            continue
        relative = path.relative_to(root.parent)
        module = ".".join(relative.with_suffix("").parts)
        modules.append(module)
    return sorted(set(modules))


def discover_app_tool_modules() -> list[str]:
    return discover_builtin_tool_modules()


def integrations_root() -> Path:
    return Path(__file__).resolve().parents[2] / "integrations"


def generated_tools_root() -> Path:
    return Path(__file__).resolve().parents[2] / "generated_tools"


def _load_manifest_payload(manifest_path: Path) -> dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a mapping")
    return payload


def discover_integrations(*, root: Path | None = None, strict: bool = False) -> list[DiscoveredIntegration]:
    root = root or integrations_root()
    return _discover_manifest_modules(root=root, strict=strict)


def discover_integration_tool_modules(*, root: Path | None = None, strict: bool = False) -> list[str]:
    modules: list[str] = []
    for integration in discover_integrations(root=root, strict=strict):
        modules.extend(integration.tool_modules)
    return sorted(set(modules))


def discover_generated_tool_modules(*, root: Path | None = None, strict: bool = False) -> list[str]:
    modules: list[str] = []
    for integration in _discover_manifest_modules(root=root or generated_tools_root(), strict=strict):
        modules.extend(integration.tool_modules)
    return sorted(set(modules))


def discover_allowed_python_tool_modules(*, strict: bool = False) -> list[str]:
    return sorted(
        set(
            [
                *discover_builtin_tool_modules(),
                *discover_integration_tool_modules(strict=strict),
                *discover_generated_tool_modules(strict=strict),
            ]
        )
    )
