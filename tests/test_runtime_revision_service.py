from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.config import reset_settings_cache
from app.db.base import Base
from app.db.repositories.domain_sql import SQLRuntimeRevisionRepository
from app.db.session import get_async_engine, get_session_maker, reset_session_state
from app.domain import RuntimeRevision, RuntimeRevisionStatus
from app.runtime.revisions import RuntimeRevisionService, fingerprint_integrations


def _write_integration(root: Path, *, file_content: str = "return {'ok': True}\n") -> None:
    integration = root / "example"
    integration.mkdir(parents=True, exist_ok=True)
    (integration / "manifest.yaml").write_text(
        "\n".join(
            [
                "id: example",
                "name: Example",
                "version: 0.1.0",
                "enabled: true",
                "module_root: integrations.example",
                "tool_modules:",
                "  - integrations.example.tools",
            ]
        ),
        encoding="utf-8",
    )
    (integration / "requirements.txt").write_text("", encoding="utf-8")
    (integration / "tools.py").write_text(
        "from __future__ import annotations\n\n"
        "def tool() -> dict[str, bool]:\n"
        f"    {file_content}",
        encoding="utf-8",
    )


class RuntimeRevisionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "runtime_revisions_service.db"
        self.db_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.env_patch = patch.dict(
            os.environ,
            {
                "APP_ENV": "test",
                "DATABASE_URL": self.db_url,
            },
            clear=False,
        )
        self.env_patch.start()
        reset_settings_cache()
        reset_session_state()

    async def asyncSetUp(self) -> None:
        self.integration_root = Path(self.temp_dir.name) / "integrations"
        self.integration_root.mkdir()
        _write_integration(self.integration_root)
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = get_session_maker()
        self.repo = SQLRuntimeRevisionRepository(self.session_factory)
        self.service = RuntimeRevisionService(runtime_revision_repo=self.repo, root_path=self.integration_root)

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    async def test_fingerprint_is_stable_for_same_content(self) -> None:
        first = fingerprint_integrations(root=self.integration_root, strict=True)
        second = fingerprint_integrations(root=self.integration_root, strict=True)

        self.assertEqual(first, second)

    async def test_fingerprint_changes_when_content_changes(self) -> None:
        first = fingerprint_integrations(root=self.integration_root, strict=True)
        (self.integration_root / "example" / "tools.py").write_text(
            "from __future__ import annotations\n\n"
            "def tool() -> dict[str, bool]:\n"
            "    return {'ok': False}\n",
            encoding="utf-8",
        )
        second = fingerprint_integrations(root=self.integration_root, strict=True)

        self.assertNotEqual(first, second)

    async def test_resolve_current_revision_reuses_existing_revision(self) -> None:
        first = await self.service.resolve_current_revision(metadata={"source": "test"})
        second = await self.service.resolve_current_revision(metadata={"source": "test"})

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(second.build_status, RuntimeRevisionStatus.READY)

    async def test_invalidate_superseded_revisions_marks_older_active_revisions(self) -> None:
        active = await self.service.resolve_current_revision()
        other = await self.repo.create(
            RuntimeRevision(
                id="other-revision",
                fingerprint="other-fingerprint",
                build_status=RuntimeRevisionStatus.READY,
            )
        )
        failed = await self.repo.create(
            RuntimeRevision(
                id="failed-revision",
                fingerprint="failed-fingerprint",
                build_status=RuntimeRevisionStatus.FAILED,
            )
        )

        invalidated = await self.service.invalidate_superseded_revisions(active.id, reason="superseded")
        updated_other = await self.repo.get(other.id, include_deleted=True)
        updated_failed = await self.repo.get(failed.id, include_deleted=True)

        self.assertEqual([item.id for item in invalidated], [other.id])
        assert updated_other is not None
        assert updated_failed is not None
        self.assertEqual(updated_other.build_status, RuntimeRevisionStatus.INVALIDATED)
        self.assertEqual(updated_failed.build_status, RuntimeRevisionStatus.FAILED)

    async def test_invalidated_matching_revision_is_revived(self) -> None:
        resolved = await self.service.resolve_current_revision()
        await self.repo.invalidate_revision(resolved.id, reason="superseded")

        revived = await self.service.resolve_current_revision()

        self.assertEqual(revived.id, resolved.id)
        self.assertEqual(revived.build_status, RuntimeRevisionStatus.READY)
        self.assertIsNone(revived.invalidated_at)

    async def test_revision_status_transition_helpers(self) -> None:
        resolved = await self.service.resolve_current_revision(mark_ready=False)
        building = await self.service.mark_revision_building(resolved.id)
        ready = await self.service.mark_revision_ready(
            resolved.id,
            image_name="agency-runtime",
            image_tag="rev-1",
            metadata={"built_by": "test"},
        )
        failed = await self.service.mark_revision_failed(
            resolved.id,
            reason="build failed",
            metadata={"built_by": "test"},
        )

        self.assertIsNotNone(building)
        self.assertIsNotNone(ready)
        self.assertIsNotNone(failed)
        assert building is not None
        assert ready is not None
        assert failed is not None
        self.assertEqual(building.build_status, RuntimeRevisionStatus.BUILDING)
        self.assertEqual(ready.build_status, RuntimeRevisionStatus.READY)
        self.assertEqual(ready.image_name, "agency-runtime")
        self.assertEqual(failed.build_status, RuntimeRevisionStatus.FAILED)
        self.assertEqual(failed.invalidation_reason, "build failed")
