from __future__ import annotations

import os
import time
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api.context import create_test_api_context
from app.api.main import create_app
from app.core.config import reset_settings_cache
from app.services.conversation_daily_summary import DailySummaryScheduleCoordinator


class DailySummaryScheduleCoordinatorTests(unittest.TestCase):
    def test_due_target_date_returns_previous_day_after_target_time(self) -> None:
        coordinator = DailySummaryScheduleCoordinator(timezone_name="UTC", target_hour=0, target_minute=15)

        not_due = coordinator.due_target_date(
            now=datetime(2026, 5, 9, 0, 10, tzinfo=timezone.utc),
            last_completed_target_date=None,
        )
        self.assertIsNone(not_due)

        due = coordinator.due_target_date(
            now=datetime(2026, 5, 9, 0, 16, tzinfo=timezone.utc),
            last_completed_target_date=None,
        )
        self.assertEqual(due, date(2026, 5, 8))

    def test_due_target_date_skips_when_target_day_already_completed(self) -> None:
        coordinator = DailySummaryScheduleCoordinator(timezone_name="UTC", target_hour=0, target_minute=15)
        due = coordinator.due_target_date(
            now=datetime(2026, 5, 9, 0, 16, tzinfo=timezone.utc),
            last_completed_target_date=date(2026, 5, 8),
        )
        self.assertIsNone(due)


class DailySummaryLifespanTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["MEMORY_DAILY_SUMMARY_ENABLED"] = "true"
        os.environ["MEMORY_DAILY_SUMMARY_TIMEZONE"] = "UTC"
        os.environ["MEMORY_DAILY_SUMMARY_TARGET_HOUR"] = "0"
        os.environ["MEMORY_DAILY_SUMMARY_TARGET_MINUTE"] = "0"
        os.environ["MEMORY_DAILY_SUMMARY_INTERVAL_SECONDS"] = "1"
        reset_settings_cache()
        self.context = create_test_api_context()

    def tearDown(self) -> None:
        for key in [
            "MEMORY_DAILY_SUMMARY_ENABLED",
            "MEMORY_DAILY_SUMMARY_TIMEZONE",
            "MEMORY_DAILY_SUMMARY_TARGET_HOUR",
            "MEMORY_DAILY_SUMMARY_TARGET_MINUTE",
            "MEMORY_DAILY_SUMMARY_INTERVAL_SECONDS",
        ]:
            os.environ.pop(key, None)
        reset_settings_cache()

    def test_app_lifespan_starts_daily_summary_loop_when_enabled(self) -> None:
        summary_mock = AsyncMock(return_value={"status": "ok", "created": 0, "processed": 0, "skipped": 0, "failed": 0})
        with patch(
            "app.services.conversation_daily_summary.ConversationDailySummaryService.summarize_day",
            summary_mock,
        ):
            with TestClient(create_app(context=self.context)):
                time.sleep(0.1)
        self.assertGreaterEqual(summary_mock.await_count, 1)
        self.assertEqual(summary_mock.await_args.kwargs["timezone_name"], "UTC")


if __name__ == "__main__":
    unittest.main()
