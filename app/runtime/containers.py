"""Docker container manager for isolated execution workers.

The manager builds runtime images, creates worker containers with constrained
mounts and environment variables, inspects lifecycle state, streams logs, and
removes stale resources. Keep credential and mount policy checks here so the
control plane can request containers without duplicating Docker safety rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable

from app.core.config import get_settings

try:  # pragma: no cover - import itself is trivial, behavior is tested via injection
    import docker
except Exception:  # pragma: no cover
    docker = None


class ContainerRuntimeError(RuntimeError):
    """Raised when the Docker-backed runtime manager cannot complete an action."""


SENSITIVE_MOUNT_PATH_MARKERS = (
    ".env",
    "api_key",
    "apikey",
    "credential",
    "credentials",
    "key",
    "keys",
    "secret",
    "secrets",
    "token",
    "tokens",
)

WRITE_PROBE_FILENAME = ".agency-workspace-write-test"


@dataclass(frozen=True, slots=True)
class RuntimeContainerConfig:
    runtime_base_image: str
    network_name: str
    workdir: str
    memory_limit_mb: int
    cpu_limit: float
    auto_remove: bool
    bind_integrations_read_only: bool

    @classmethod
    def from_settings(cls) -> "RuntimeContainerConfig":
        settings = get_settings()
        return cls(
            runtime_base_image=settings.execution_runtime_base_image,
            network_name=settings.execution_container_network,
            workdir=settings.execution_container_workdir,
            memory_limit_mb=settings.execution_container_memory_limit_mb,
            cpu_limit=settings.execution_container_cpu_limit,
            auto_remove=settings.execution_container_auto_remove,
            bind_integrations_read_only=settings.execution_container_bind_integrations_read_only,
        )


@dataclass(frozen=True, slots=True)
class RuntimeMount:
    source: str
    target: str
    read_only: bool = True


@dataclass(frozen=True, slots=True)
class RuntimeImageBuildSpec:
    runtime_revision_id: str
    image_name: str
    image_tag: str
    context_path: str
    dockerfile: str
    buildargs: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def image_ref(self) -> str:
        return f"{self.image_name}:{self.image_tag}"


@dataclass(frozen=True, slots=True)
class RuntimeContainerSpec:
    execution_id: str
    workflow_id: str
    runtime_revision_id: str
    image: str
    goal_id: str | None = None
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    mounts: list[RuntimeMount] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    network_name: str | None = None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeContainerState:
    container_id: str
    name: str
    image: str
    status: str
    labels: dict[str, str]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeImageState:
    image_id: str
    tags: list[str]
    labels: dict[str, str]
    created_at: datetime | None = None


def default_container_name(execution_id: str) -> str:
    safe_suffix = execution_id.replace(":", "-").replace("/", "-")
    return f"agency-execution-{safe_suffix}"


def managed_container_labels(
        *,
        execution_id: str,
        workflow_id: str,
        runtime_revision_id: str,
        goal_id: str | None = None,
        extra: dict[str, str] | None = None,
) -> dict[str, str]:
    labels = {
        "agency.managed": "true",
        "agency.execution_id": execution_id,
        "agency.workflow_id": workflow_id,
        "agency.runtime_revision_id": runtime_revision_id,
    }
    if goal_id:
        labels["agency.goal_id"] = goal_id
    if extra:
        labels.update(extra)
    return labels


def onecli_isolated_mount_lockdown_enabled(settings) -> bool:
    return bool(settings.onecli_enabled and settings.onecli_force_for_isolated_workers)


def sensitive_mount_path_reason(source: str, target: str) -> str | None:
    normalized = f"{source} {target}".lower()
    path_parts = []
    for value in (source, target):
        path_parts.extend(part.lower() for part in Path(value).parts if part)
    for marker in SENSITIVE_MOUNT_PATH_MARKERS:
        if marker == ".env":
            if any(part == ".env" or part.startswith(".env.") for part in path_parts):
                return marker
            continue
        if marker in normalized:
            return marker
    return None


def _is_path_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / WRITE_PROBE_FILENAME
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        return True
    except OSError:
        return False


def _workspace_write_probe_path(source: str, target: str) -> Path | None:
    source_path = Path(source).expanduser()
    if source_path.exists():
        return source_path

    target_path = Path(target)
    if target_path.exists():
        return target_path

    return None


def require_read_write_mount_writable(source: str, target: str, *, env_name: str) -> None:
    probe_path = _workspace_write_probe_path(source, target)
    if probe_path is None or _is_path_writable(probe_path):
        return

    raise ContainerRuntimeError(
        f"Workflow mount '{source}' -> '{target}' is configured read-write, "
        f"but Agency cannot write to '{probe_path}'. Ask the user to approve filesystem "
        "write access or fix the host directory permissions, then retry. "
        f"Check {env_name} and the Docker mount for this path."
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


class DockerRuntimeManager:
    def __init__(
            self,
            *,
            config: RuntimeContainerConfig | None = None,
            client_factory: Callable[[], Any] | None = None,
    ):
        self.config = config or RuntimeContainerConfig.from_settings()
        self._client_factory = client_factory or self._default_client_factory
        self._client: Any | None = None

    def _default_client_factory(self) -> Any:
        if docker is None:
            raise ContainerRuntimeError("docker SDK is not available")
        return docker.from_env()

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    def revision_image_ref(self, runtime_revision_id: str) -> str:
        return f"{self.config.runtime_base_image.rsplit(':', 1)[0]}:{runtime_revision_id}"

    def _default_integrations_source(self) -> Path:
        host_workspace = os.getenv("AGENCY_BACKEND_HOST_WORKSPACE")
        if host_workspace:
            return Path(host_workspace).expanduser() / "integrations"

        backend_workspace = os.getenv("AGENCY_BACKEND_WORKSPACE")
        if backend_workspace:
            return Path(backend_workspace).expanduser() / "integrations"

        return Path(__file__).resolve().parents[2] / "integrations"

    def _workspace_mounts(self) -> list[RuntimeMount]:
        mounts: list[RuntimeMount] = []
        workspace_pairs = [
            ("AGENCY_BACKEND_HOST_WORKSPACE", "AGENCY_BACKEND_WORKSPACE"),
            ("AGENCY_FRONTEND_HOST_WORKSPACE", "AGENCY_FRONTEND_WORKSPACE"),
        ]
        for host_env, target_env in workspace_pairs:
            source = os.getenv(host_env)
            target = os.getenv(target_env)
            if not source or not target:
                continue
            source_path = Path(source).expanduser().resolve()
            # Trusted local dev workers need write access for repo-editing agents.
            # Probe when the path is visible here so permission problems fail
            # before Codex reports a lower-level read-only filesystem error.
            require_read_write_mount_writable(str(source_path), target, env_name=host_env)
            mounts.append(RuntimeMount(source=str(source_path), target=target, read_only=False))
        return mounts

    def _codex_home_mount(self) -> RuntimeMount | None:
        source = os.getenv("CODEX_HOME_VOLUME")
        if not source:
            return None
        target = os.getenv("EXECUTION_CODEX_HOME") or os.getenv("CODEX_HOME") or "/codex"
        return RuntimeMount(source=source, target=target, read_only=False)

    def default_mounts(self) -> list[RuntimeMount]:
        settings = get_settings()
        onecli_mount_lockdown = onecli_isolated_mount_lockdown_enabled(settings)
        integrations_dir = self._default_integrations_source()
        mounts = [] if onecli_mount_lockdown else self._workspace_mounts()
        codex_home_mount = self._codex_home_mount()
        if codex_home_mount is not None and not onecli_mount_lockdown:
            mounts.append(codex_home_mount)
        if os.getenv("AGENCY_BACKEND_HOST_WORKSPACE") or integrations_dir.exists():
            mounts.append(
                RuntimeMount(
                    source=str(integrations_dir),
                    target=f"{self.config.workdir.rstrip('/')}/integrations",
                    read_only=self.config.bind_integrations_read_only,
                )
            )
        for mount in settings.parsed_execution_container_extra_mounts:
            if onecli_mount_lockdown:
                reason = sensitive_mount_path_reason(str(mount["source"]), str(mount["target"]))
                if reason:
                    raise ContainerRuntimeError(
                        "Sensitive execution container extra mount is not allowed when "
                        "ONECLI_FORCE_FOR_ISOLATED_WORKERS is true: "
                        f"{mount['source']} -> {mount['target']} matched '{reason}'"
                    )
            mounts.append(
                RuntimeMount(
                    source=str(mount["source"]),
                    target=str(mount["target"]),
                    read_only=bool(mount["read_only"]),
                )
            )
        if (
                settings.onecli_enabled
                and settings.onecli_force_for_isolated_workers
                and settings.onecli_gateway_ca_bundle_path
        ):
            mounts.append(
                RuntimeMount(
                    source=str(Path(settings.onecli_gateway_ca_bundle_path).expanduser().resolve()),
                    target=settings.onecli_gateway_ca_bundle_container_path,
                    read_only=True,
                )
            )
        return mounts

    def build_runtime_image(self, spec: RuntimeImageBuildSpec) -> str:
        try:
            self.client.images.build(
                path=spec.context_path,
                dockerfile=spec.dockerfile,
                tag=spec.image_ref,
                buildargs=spec.buildargs,
                labels=managed_container_labels(
                    execution_id="build",
                    workflow_id="runtime-build",
                    runtime_revision_id=spec.runtime_revision_id,
                    extra=spec.labels,
                ),
            )
        except Exception as exc:  # pragma: no cover - exercised through injection tests
            raise ContainerRuntimeError(f"Failed to build runtime image '{spec.image_ref}': {exc}") from exc
        return spec.image_ref

    def build_container_kwargs(self, spec: RuntimeContainerSpec) -> dict[str, Any]:
        labels = managed_container_labels(
            execution_id=spec.execution_id,
            workflow_id=spec.workflow_id,
            runtime_revision_id=spec.runtime_revision_id,
            goal_id=spec.goal_id,
            extra=spec.labels,
        )
        mounts = spec.mounts or self.default_mounts()
        volumes: dict[str, dict[str, str | bool]] = {}
        for mount in mounts:
            if not mount.read_only:
                require_read_write_mount_writable(
                    mount.source,
                    mount.target,
                    env_name="EXECUTION_CONTAINER_EXTRA_MOUNTS or workspace env",
                )
            volumes[mount.source] = {"bind": mount.target, "mode": "ro" if mount.read_only else "rw"}
        nano_cpus = int(self.config.cpu_limit * 1_000_000_000)
        return {
            "image": spec.image,
            "name": spec.name or default_container_name(spec.execution_id),
            "command": spec.command or None,
            "environment": spec.env,
            "labels": labels,
            "network": spec.network_name or self.config.network_name,
            "detach": True,
            "working_dir": self.config.workdir,
            "auto_remove": self.config.auto_remove,
            "mem_limit": f"{self.config.memory_limit_mb}m",
            "nano_cpus": nano_cpus,
            "volumes": volumes,
        }

    def create_execution_container(self, spec: RuntimeContainerSpec) -> RuntimeContainerState:
        kwargs = self.build_container_kwargs(spec)
        try:
            container = self.client.containers.create(**kwargs)
        except Exception as exc:  # pragma: no cover - exercised through injection tests
            raise ContainerRuntimeError(
                f"Failed to create execution container for '{spec.execution_id}': {exc}") from exc
        image_tags = getattr(getattr(container, "image", None), "tags", None) or [spec.image]
        return RuntimeContainerState(
            container_id=container.id,
            name=container.name,
            image=image_tags[0],
            status=getattr(container, "status", "created"),
            labels=kwargs["labels"],
        )

    def start_container(self, container_id: str) -> RuntimeContainerState:
        container = self._get_container(container_id)
        try:
            container.start()
            container.reload()
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to start container '{container_id}': {exc}") from exc
        return self.inspect_container(container_id)

    def stop_container(self, container_id: str, *, timeout: int = 10) -> RuntimeContainerState:
        container = self._get_container(container_id)
        try:
            container.stop(timeout=timeout)
            container.reload()
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to stop container '{container_id}': {exc}") from exc
        return self.inspect_container(container_id)

    def remove_container(self, container_id: str, *, force: bool = False) -> None:
        container = self._get_container(container_id)
        try:
            container.remove(force=force)
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to remove container '{container_id}': {exc}") from exc

    def inspect_container(self, container_id: str) -> RuntimeContainerState:
        container = self._get_container(container_id)
        attrs = getattr(container, "attrs", {}) or {}
        state = attrs.get("State", {})
        config = attrs.get("Config", {})
        image = config.get("Image") or (getattr(getattr(container, "image", None), "tags", None) or [container_id])[0]
        return RuntimeContainerState(
            container_id=container.id,
            name=container.name,
            image=image,
            status=state.get("Status") or getattr(container, "status", "unknown"),
            labels=config.get("Labels") or {},
            started_at=_parse_datetime(state.get("StartedAt")),
            finished_at=_parse_datetime(state.get("FinishedAt")),
            exit_code=state.get("ExitCode"),
        )

    def list_managed_containers(self, *, all_containers: bool = True) -> list[RuntimeContainerState]:
        try:
            containers = self.client.containers.list(all=all_containers, filters={"label": "agency.managed=true"})
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to list managed containers: {exc}") from exc
        return [self.inspect_container(container.id) for container in containers]

    def wait_for_container_exit(
            self,
            container_id: str,
            *,
            timeout_seconds: float = 60.0,
            poll_interval_seconds: float = 0.5,
    ) -> RuntimeContainerState:
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            state = self.inspect_container(container_id)
            if state.status in {"exited", "dead"}:
                return state
            sleep(poll_interval_seconds)
        raise ContainerRuntimeError(f"Timed out waiting for container '{container_id}' to exit")

    def read_container_logs(self, container_id: str) -> str:
        container = self._get_container(container_id)
        try:
            payload = container.logs(stdout=True, stderr=True)
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to read logs for container '{container_id}': {exc}") from exc
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)

    def list_managed_images(self) -> list[RuntimeImageState]:
        try:
            images = self.client.images.list(filters={"label": "agency.managed=true"})
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to list managed images: {exc}") from exc
        items: list[RuntimeImageState] = []
        for image in images:
            attrs = getattr(image, "attrs", {}) or {}
            labels = ((attrs.get("Config") or {}).get("Labels")) or {}
            tags = list(getattr(image, "tags", []) or [])
            items.append(
                RuntimeImageState(
                    image_id=getattr(image, "id", ""),
                    tags=tags,
                    labels=labels,
                    created_at=_parse_datetime(attrs.get("Created")),
                )
            )
        return items

    def remove_image(self, image_ref: str, *, force: bool = False) -> None:
        try:
            self.client.images.remove(image=image_ref, force=force)
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Failed to remove image '{image_ref}': {exc}") from exc

    def _get_container(self, container_id: str) -> Any:
        try:
            return self.client.containers.get(container_id)
        except Exception as exc:  # pragma: no cover
            raise ContainerRuntimeError(f"Container '{container_id}' was not found: {exc}") from exc
