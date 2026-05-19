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


class RuntimeRevisionRepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "runtime_revisions.db"
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
        engine = get_async_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = get_session_maker()
        self.repo = SQLRuntimeRevisionRepository(self.session_factory)

    async def asyncTearDown(self) -> None:
        engine = get_async_engine(optional=True)
        if engine is not None:
            await engine.dispose()
        reset_session_state()
        reset_settings_cache()
        self.env_patch.stop()
        self.temp_dir.cleanup()

    async def test_create_and_lookup_by_fingerprint(self) -> None:
        created = await self.repo.create(
            RuntimeRevision(
                id="runtime-rev-1",
                fingerprint="fp-1",
                source_path="integrations/",
                build_status=RuntimeRevisionStatus.READY,
                image_name="agency-runtime",
                image_tag="rev-1",
            )
        )

        loaded = await self.repo.get_by_fingerprint("fp-1")

        self.assertEqual(created.id, "runtime-rev-1")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.image_tag, "rev-1")
        self.assertEqual(loaded.build_status, RuntimeRevisionStatus.READY)

    async def test_get_latest_ready_and_invalidate(self) -> None:
        await self.repo.create(
            RuntimeRevision(
                id="runtime-rev-old",
                fingerprint="fp-old",
                build_status=RuntimeRevisionStatus.READY,
            )
        )
        await self.repo.create(
            RuntimeRevision(
                id="runtime-rev-new",
                fingerprint="fp-new",
                build_status=RuntimeRevisionStatus.BUILDING,
            )
        )

        latest_ready = await self.repo.get_latest_ready()
        self.assertIsNotNone(latest_ready)
        assert latest_ready is not None
        self.assertEqual(latest_ready.id, "runtime-rev-old")

        invalidated = await self.repo.invalidate_revision("runtime-rev-old", reason="superseded")
        self.assertIsNotNone(invalidated)
        assert invalidated is not None
        self.assertEqual(invalidated.build_status, RuntimeRevisionStatus.INVALIDATED)
        self.assertEqual(invalidated.invalidation_reason, "superseded")

        listed = await self.repo.list()
        self.assertEqual([item.id for item in listed], ["runtime-rev-new"])
