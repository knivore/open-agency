from __future__ import annotations

import unittest

from app.domain import RuntimeRevision, RuntimeRevisionStatus
from app.runtime.containers import RuntimeContainerState
from app.runtime.lifecycle import container_payload, runtime_revision_payload


class RuntimeLifecyclePayloadTests(unittest.TestCase):
    def test_runtime_revision_payload_is_standardized(self) -> None:
        revision = RuntimeRevision(
            id="rev-1",
            fingerprint="fp-1",
            source_path="integrations/",
            build_status=RuntimeRevisionStatus.READY,
            image_name="agency-runtime",
            image_tag="rev-1",
            base_image="agency-runtime-base:latest",
        )

        payload = runtime_revision_payload(revision, reason="resolved")

        self.assertEqual(payload["runtime_revision_id"], "rev-1")
        self.assertEqual(payload["fingerprint"], "fp-1")
        self.assertEqual(payload["build_status"], "ready")
        self.assertEqual(payload["reason"], "resolved")

    def test_container_payload_is_standardized(self) -> None:
        state = RuntimeContainerState(
            container_id="container-1",
            name="agency-execution-1",
            image="agency-runtime:rev-1",
            status="running",
            labels={"agency.managed": "true"},
            exit_code=None,
        )

        payload = container_payload(state, runtime_revision_id="rev-1", reason="started")

        self.assertEqual(payload["container_id"], "container-1")
        self.assertEqual(payload["container_name"], "agency-execution-1")
        self.assertEqual(payload["runtime_revision_id"], "rev-1")
        self.assertEqual(payload["reason"], "started")
        self.assertEqual(payload["labels"]["agency.managed"], "true")
