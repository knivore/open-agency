from datetime import datetime, timedelta
from uuid import uuid4

from app.core.time import ensure_utc, utc_now
from app.db.models import ScheduleORM
from app.domain import ScheduleDefinition

from .catalog import InMemoryCatalogRepository, MongoCatalogRepository
from .sql import SQLAlchemyRepository


class ScheduleRepository(SQLAlchemyRepository[ScheduleORM]):
    def __init__(self, session):
        super().__init__(session, ScheduleORM)


class MongoScheduleRepository(MongoCatalogRepository[ScheduleDefinition]):
    def __init__(self, db=None):
        super().__init__(ScheduleDefinition, "schedule_definitions", db)


class InMemoryScheduleRepository(InMemoryCatalogRepository[ScheduleDefinition]):
    def __init__(self):
        super().__init__(ScheduleDefinition)
        self._fire_claims: dict[tuple[str, str], dict] = {}

    def _claim_key(self, schedule_id: str, scheduled_fire_at: datetime) -> tuple[str, str]:
        return schedule_id, ensure_utc(scheduled_fire_at).isoformat()

    async def acquire_schedule_fire_claim(
            self,
            *,
            schedule_id: str,
            scheduled_fire_at: datetime,
            claimed_by: str,
            lease_seconds: int,
    ) -> bool:
        now = utc_now()
        key = self._claim_key(schedule_id, scheduled_fire_at)
        existing = self._fire_claims.get(key)
        if existing is not None:
            lease_expires_at = ensure_utc(existing["lease_expires_at"])
            if existing["status"] == "fired":
                return False
            if existing["status"] == "claimed" and lease_expires_at > now:
                return False
        self._fire_claims[key] = {
            "id": str(uuid4()),
            "schedule_id": schedule_id,
            "scheduled_fire_at": ensure_utc(scheduled_fire_at),
            "claimed_by": claimed_by,
            "lease_expires_at": now + timedelta(seconds=lease_seconds),
            "status": "claimed",
            "execution_id": None,
            "updated_at": now,
        }
        return True

    async def mark_schedule_fire_claim_fired(
            self,
            *,
            schedule_id: str,
            scheduled_fire_at: datetime,
            execution_id: str,
            claimed_by: str,
    ) -> None:
        claim = self._fire_claims.get(self._claim_key(schedule_id, scheduled_fire_at))
        if claim is None or claim.get("claimed_by") != claimed_by:
            return
        claim["status"] = "fired"
        claim["execution_id"] = execution_id
        claim["lease_expires_at"] = utc_now()
        claim["updated_at"] = utc_now()

    async def mark_schedule_fire_claim_failed(
            self,
            *,
            schedule_id: str,
            scheduled_fire_at: datetime,
            claimed_by: str,
    ) -> None:
        claim = self._fire_claims.get(self._claim_key(schedule_id, scheduled_fire_at))
        if claim is None or claim.get("claimed_by") != claimed_by:
            return
        claim["status"] = "failed"
        claim["lease_expires_at"] = utc_now()
        claim["updated_at"] = utc_now()
