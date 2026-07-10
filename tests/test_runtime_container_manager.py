from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import reset_settings_cache
from app.runtime.containers import (
    ContainerRuntimeError,
    DockerRuntimeManager,
    RuntimeContainerConfig,
    RuntimeContainerSpec,
    RuntimeImageBuildSpec,
    RuntimeMount,
    default_container_name,
    managed_container_labels,
)


class _FakeImage:
    def __init__(self, tags: list[str] | None = None):
        self.tags = tags or []


class _FakeContainer:
    def __init__(self, *, container_id: str = "container-1", name: str = "agency-execution-test",
                 image: str = "agency-runtime:rev-1"):
        self.id = container_id
        self.name = name
        self.status = "created"
        self.image = _FakeImage(tags=[image])
        self.attrs = {
            "State": {"Status": "created", "StartedAt": None, "FinishedAt": None, "ExitCode": None},
            "Config": {"Image": image, "Labels": {}},
        }
        self.started = False
        self.stopped = False
        self.removed = False

    def start(self) -> None:
        self.started = True
        self.status = "running"
        self.attrs["State"]["Status"] = "running"

    def stop(self, timeout: int = 10) -> None:
        self.stopped = True
        self.status = "exited"
        self.attrs["State"]["Status"] = "exited"
        self.attrs["State"]["ExitCode"] = 0

    def remove(self, force: bool = False) -> None:
        self.removed = True

    def reload(self) -> None:
        return None


class _FakeContainersAPI:
    def __init__(self):
        self.created_kwargs = None
        self._container = _FakeContainer()

    def create(self, **kwargs):
        self.created_kwargs = kwargs
        self._container.name = kwargs["name"]
        self._container.attrs["Config"]["Labels"] = kwargs["labels"]
        self._container.attrs["Config"]["Image"] = kwargs["image"]
        self._container.image = _FakeImage(tags=[kwargs["image"]])
        return self._container

    def get(self, container_id: str):
        return self._container

    def list(self, all: bool = True, filters: dict | None = None):
        _ = filters
        return [self._container]


class _FakeImagesAPI:
    def __init__(self):
        self.build_kwargs = None

    def build(self, **kwargs):
        self.build_kwargs = kwargs
        return _FakeImage(tags=[kwargs["tag"]]), [{"stream": "ok"}]


class _FakeDockerClient:
    def __init__(self):
        self.containers = _FakeContainersAPI()
        self.images = _FakeImagesAPI()


class RuntimeContainerManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake_client = _FakeDockerClient()
        self.manager = DockerRuntimeManager(
            config=RuntimeContainerConfig(
                runtime_base_image="agency-runtime-base:latest",
                network_name="agency-test-network",
                workdir="/app",
                memory_limit_mb=768,
                cpu_limit=1.5,
                auto_remove=False,
                bind_integrations_read_only=True,
            ),
            client_factory=lambda: self.fake_client,
        )

    def test_managed_labels_include_required_keys(self) -> None:
        labels = managed_container_labels(
            execution_id="exec-1",
            workflow_id="workflow-1",
            runtime_revision_id="rev-1",
            goal_id="goal-1",
            extra={"agency.extra": "yes"},
        )

        self.assertEqual(labels["agency.managed"], "true")
        self.assertEqual(labels["agency.execution_id"], "exec-1")
        self.assertEqual(labels["agency.workflow_id"], "workflow-1")
        self.assertEqual(labels["agency.goal_id"], "goal-1")
        self.assertEqual(labels["agency.runtime_revision_id"], "rev-1")
        self.assertEqual(labels["agency.extra"], "yes")

    def test_default_container_name_is_stable(self) -> None:
        self.assertEqual(default_container_name("execution-1"), "agency-execution-execution-1")
        self.assertEqual(default_container_name("execution:1"), "agency-execution-execution-1")

    def test_build_runtime_image_uses_revision_labels(self) -> None:
        image_ref = self.manager.build_runtime_image(
            RuntimeImageBuildSpec(
                runtime_revision_id="rev-1",
                image_name="agency-runtime",
                image_tag="rev-1",
                context_path=".",
                dockerfile="docker/backend/Dockerfile",
            )
        )

        self.assertEqual(image_ref, "agency-runtime:rev-1")
        self.assertEqual(self.fake_client.images.build_kwargs["tag"], image_ref)
        self.assertEqual(
            self.fake_client.images.build_kwargs["labels"]["agency.runtime_revision_id"],
            "rev-1",
        )

    def test_create_execution_container_builds_expected_kwargs(self) -> None:
        spec = RuntimeContainerSpec(
            execution_id="exec-1",
            workflow_id="workflow-1",
            runtime_revision_id="rev-1",
            goal_id="goal-1",
            image="agency-runtime:rev-1",
            command=["python", "-m", "worker"],
            env={"DATABASE_URL": "sqlite://"},
            mounts=[RuntimeMount(source="/tmp/source", target="/app/integrations", read_only=True)],
        )

        state = self.manager.create_execution_container(spec)
        kwargs = self.fake_client.containers.created_kwargs

        self.assertEqual(state.container_id, "container-1")
        self.assertEqual(kwargs["image"], "agency-runtime:rev-1")
        self.assertEqual(kwargs["name"], "agency-execution-exec-1")
        self.assertEqual(kwargs["network"], "agency-test-network")
        self.assertEqual(kwargs["mem_limit"], "768m")
        self.assertEqual(kwargs["nano_cpus"], 1_500_000_000)
        self.assertEqual(kwargs["volumes"]["/tmp/source"]["mode"], "ro")
        self.assertEqual(kwargs["labels"]["agency.execution_id"], "exec-1")
        self.assertEqual(kwargs["labels"]["agency.goal_id"], "goal-1")
        self.assertEqual(state.labels["agency.goal_id"], "goal-1")

    def test_container_spec_can_override_network(self) -> None:
        spec = RuntimeContainerSpec(
            execution_id="exec-egress",
            workflow_id="workflow-1",
            runtime_revision_id="rev-1",
            image="agency-runtime:rev-1",
            network_name="agency_onecli_worker_egress",
        )

        self.manager.create_execution_container(spec)
        kwargs = self.fake_client.containers.created_kwargs

        self.assertEqual(kwargs["network"], "agency_onecli_worker_egress")

    def test_create_execution_container_checks_explicit_read_write_mounts(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            spec = RuntimeContainerSpec(
                execution_id="exec-rw",
                workflow_id="workflow-1",
                runtime_revision_id="rev-1",
                image="agency-runtime:rev-1",
                mounts=[
                    RuntimeMount(source=workspace, target="/workspace/other-repo", read_only=False)
                ],
            )

            with patch("app.runtime.containers._is_path_writable", return_value=False):
                with self.assertRaises(ContainerRuntimeError) as exc:
                    self.manager.create_execution_container(spec)

        self.assertIn("Workflow mount", str(exc.exception))
        self.assertIn("EXECUTION_CONTAINER_EXTRA_MOUNTS", str(exc.exception))

    def test_lifecycle_methods_delegate_to_client(self) -> None:
        spec = RuntimeContainerSpec(
            execution_id="exec-2",
            workflow_id="workflow-2",
            runtime_revision_id="rev-2",
            image="agency-runtime:rev-2",
        )
        created = self.manager.create_execution_container(spec)
        started = self.manager.start_container(created.container_id)
        stopped = self.manager.stop_container(created.container_id)
        self.manager.remove_container(created.container_id)

        self.assertEqual(started.status, "running")
        self.assertEqual(stopped.status, "exited")
        self.assertTrue(self.fake_client.containers._container.removed)

    def test_default_mounts_include_integrations_directory_when_present(self) -> None:
        mounts = self.manager.default_mounts()

        self.assertTrue(any(Path(mount.source).name == "integrations" for mount in mounts))

    def test_default_mounts_uses_host_workspace_override_for_docker_socket(self) -> None:
        with patch.dict(os.environ, {"AGENCY_BACKEND_HOST_WORKSPACE": "/host/agency"}, clear=False):
            mounts = self.manager.default_mounts()

        integrations_mount = next(mount for mount in mounts if mount.target == "/app/integrations")
        self.assertEqual(integrations_mount.source, "/host/agency/integrations")

    def test_default_mounts_include_backend_and_frontend_workspace_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENCY_BACKEND_HOST_WORKSPACE": "/host/agency",
                "AGENCY_BACKEND_WORKSPACE": "/workspace/agency",
                "AGENCY_FRONTEND_HOST_WORKSPACE": "/host/agency-fe",
                "AGENCY_FRONTEND_WORKSPACE": "/workspace/agency-fe",
            },
            clear=False,
        ):
            mounts = self.manager.default_mounts()

        mount_by_target = {mount.target: mount for mount in mounts}
        self.assertEqual(mount_by_target["/workspace/agency"].source, "/host/agency")
        self.assertEqual(mount_by_target["/workspace/agency-fe"].source, "/host/agency-fe")
        self.assertFalse(mount_by_target["/workspace/agency"].read_only)
        self.assertFalse(mount_by_target["/workspace/agency-fe"].read_only)

    def test_default_workspace_mounts_fail_fast_when_visible_source_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            with patch.dict(
                    os.environ,
                    {
                        "AGENCY_BACKEND_HOST_WORKSPACE": workspace,
                        "AGENCY_BACKEND_WORKSPACE": "/workspace/agency",
                    },
                    clear=False,
            ), patch("app.runtime.containers._is_path_writable", return_value=False):
                with self.assertRaises(ContainerRuntimeError) as exc:
                    self.manager.default_mounts()

        self.assertIn("configured read-write", str(exc.exception))
        self.assertIn("Ask the user to approve filesystem write access", str(exc.exception))

    def test_default_mounts_include_writable_codex_home_volume(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME_VOLUME": "agency_codex_home", "CODEX_HOME": "/codex"}, clear=False):
            mounts = self.manager.default_mounts()

        codex_mount = next(mount for mount in mounts if mount.target == "/codex")
        self.assertEqual(codex_mount.source, "agency_codex_home")
        self.assertFalse(codex_mount.read_only)

    def test_default_mounts_default_codex_home_target_for_workers(self) -> None:
        with patch.dict(os.environ, {"CODEX_HOME_VOLUME": "agency_codex_home"}, clear=False):
            os.environ.pop("CODEX_HOME", None)
            os.environ.pop("EXECUTION_CODEX_HOME", None)
            mounts = self.manager.default_mounts()

        codex_mount = next(mount for mount in mounts if mount.target == "/codex")
        self.assertEqual(codex_mount.source, "agency_codex_home")
        self.assertFalse(codex_mount.read_only)

    def test_default_mounts_include_onecli_ca_bundle_when_worker_enforced(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "ONECLI_GATEWAY_CA_BUNDLE_PATH": "/tmp/onecli-ca.pem",
                    "ONECLI_GATEWAY_CA_BUNDLE_CONTAINER_PATH": "/etc/agency/onecli/ca.pem",
                },
                clear=False,
        ):
            reset_settings_cache()
            mounts = self.manager.default_mounts()
        reset_settings_cache()

        ca_mount = next(mount for mount in mounts if mount.target == "/etc/agency/onecli/ca.pem")
        self.assertEqual(ca_mount.source, str(Path("/tmp/onecli-ca.pem").resolve()))
        self.assertTrue(ca_mount.read_only)

    def test_default_mounts_skip_workspace_and_codex_home_when_onecli_worker_enforced(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "AGENCY_BACKEND_HOST_WORKSPACE": "/host/agency",
                    "AGENCY_BACKEND_WORKSPACE": "/workspace/agency",
                    "AGENCY_FRONTEND_HOST_WORKSPACE": "/host/agency-fe",
                    "AGENCY_FRONTEND_WORKSPACE": "/workspace/agency-fe",
                    "CODEX_HOME_VOLUME": "agency_codex_home",
                    "CODEX_HOME": "/codex",
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                },
                clear=False,
        ):
            reset_settings_cache()
            mounts = self.manager.default_mounts()
        reset_settings_cache()

        targets = {mount.target for mount in mounts}
        self.assertNotIn("/workspace/agency", targets)
        self.assertNotIn("/workspace/agency-fe", targets)
        self.assertNotIn("/codex", targets)
        self.assertIn("/app/integrations", targets)

    def test_default_mounts_reject_sensitive_extra_mounts_when_onecli_worker_enforced(self) -> None:
        with patch.dict(
                os.environ,
                {
                    "ONECLI_ENABLED": "true",
                    "ONECLI_FORCE_FOR_ISOLATED_WORKERS": "true",
                    "EXECUTION_ISOLATION_ENABLED": "true",
                    "EXECUTION_CONTAINER_EXTRA_MOUNTS": (
                        '[{"source": "/host/agency/.env", "target": "/run/secrets/agency.env", "read_only": true}]'
                    ),
                },
                clear=False,
        ):
            reset_settings_cache()
            with self.assertRaises(ContainerRuntimeError) as exc:
                self.manager.default_mounts()
        reset_settings_cache()

        self.assertIn("Sensitive execution container extra mount", str(exc.exception))
