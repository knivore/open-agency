"""Async, owner-scoped lifecycle management for durable engine sessions."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .contracts import ChallengeResult, OwnerClaims, SessionStatus


class AsyncBrowserHandle(Protocol):
    engine: str
    page: Any

    async def close(self) -> None: ...


class SessionRegistryError(RuntimeError):
    pass


class SessionNotFoundError(SessionRegistryError):
    pass


class SessionAccessError(SessionRegistryError):
    pass


class SessionLimitError(SessionRegistryError):
    pass


@dataclass(slots=True)
class SessionRecord:
    session_id: str
    owner: OwnerClaims
    handle: AsyncBrowserHandle
    engine: str
    created_at: float
    last_used_at: float
    idle_expires_at: float
    maximum_expires_at: float
    idle_ttl_seconds: int
    artifact_retention_seconds: int
    allowed_hosts: tuple[str, ...]
    current_url: str | None = None
    challenge: ChallengeResult = field(default_factory=ChallengeResult)
    artifacts: dict[str, str] = field(default_factory=dict)
    status: str = "active"
    correlation_id: str | None = None

    def expired(self, now: float) -> bool:
        return now >= self.idle_expires_at or now >= self.maximum_expires_at

    def public_status(self) -> SessionStatus:
        return SessionStatus(
            session_id=self.session_id,
            engine=self.engine,
            status=self.status,
            current_url=self.current_url,
            created_at=self.created_at,
            last_used_at=self.last_used_at,
            idle_expires_at=self.idle_expires_at,
            maximum_expires_at=self.maximum_expires_at,
            challenge=self.challenge,
        )


class AsyncSessionRegistry:
    def __init__(
            self,
            *,
            idle_ttl_seconds: int | None = None,
            maximum_ttl_seconds: int | None = None,
            max_sessions_per_owner: int | None = None,
            max_sessions_total: int | None = None,
            clock: Callable[[], float] = time.time,
    ) -> None:
        self.idle_ttl_seconds = max(1, idle_ttl_seconds or int(os.getenv("BROWSER_SESSION_IDLE_TTL_SECONDS", "900")))
        self.maximum_ttl_seconds = max(
            self.idle_ttl_seconds,
            maximum_ttl_seconds or int(os.getenv("BROWSER_SESSION_MAXIMUM_TTL_SECONDS", "3600")),
        )
        self.max_sessions_per_owner = max(
            1, max_sessions_per_owner or int(os.getenv("BROWSER_SESSION_MAX_PER_OWNER", "3"))
        )
        self.max_sessions_total = max(
            self.max_sessions_per_owner,
            max_sessions_total or int(os.getenv("BROWSER_SESSION_MAX_TOTAL", "8")),
        )
        self._clock = clock
        self._records: dict[str, SessionRecord] = {}
        self._lock = asyncio.Lock()
        self.cleanup_failures = 0

    async def create(
            self,
            *,
            owner: OwnerClaims,
            handle: AsyncBrowserHandle,
            allowed_hosts: list[str] | tuple[str, ...],
            current_url: str | None = None,
            correlation_id: str | None = None,
            idle_ttl_seconds: int | None = None,
            maximum_ttl_seconds: int | None = None,
            max_sessions_per_owner: int | None = None,
            max_sessions_total: int | None = None,
            artifact_retention_seconds: int = 86_400,
    ) -> SessionRecord:
        if not owner.is_identified:
            raise SessionAccessError("Browser sessions require an identified owner")
        expired: list[SessionRecord]
        async with self._lock:
            now = self._clock()
            expired = self._remove_expired(now)
            owned = sum(owner.owns(record.owner) for record in self._records.values())
            owner_limit = min(max_sessions_per_owner or self.max_sessions_per_owner, self.max_sessions_per_owner)
            total_limit = min(max_sessions_total or self.max_sessions_total, self.max_sessions_total)
            if owned >= owner_limit:
                raise SessionLimitError(f"Browser session owner limit reached ({owner_limit})")
            if len(self._records) >= total_limit:
                raise SessionLimitError(f"Browser session runtime limit reached ({total_limit})")
            effective_maximum_ttl = min(maximum_ttl_seconds or self.maximum_ttl_seconds, self.maximum_ttl_seconds)
            effective_idle_ttl = min(idle_ttl_seconds or self.idle_ttl_seconds, effective_maximum_ttl)
            record = SessionRecord(
                session_id=f"brs_{secrets.token_urlsafe(24)}",
                owner=owner,
                handle=handle,
                engine=handle.engine,
                created_at=now,
                last_used_at=now,
                idle_expires_at=now + effective_idle_ttl,
                maximum_expires_at=now + effective_maximum_ttl,
                idle_ttl_seconds=effective_idle_ttl,
                artifact_retention_seconds=artifact_retention_seconds,
                allowed_hosts=tuple(sorted(set(allowed_hosts))),
                current_url=current_url,
                correlation_id=correlation_id,
            )
            self._records[record.session_id] = record
        await self._close_records(expired, status="expired")
        return record

    async def resolve(self, *, owner: OwnerClaims, session_id: str, touch: bool = True) -> SessionRecord:
        async with self._lock:
            now = self._clock()
            expired = self._remove_expired(now)
            record = self._records.get(session_id)
            error: Exception | None
            if record is None:
                error = SessionNotFoundError(f"Browser session '{session_id}' was not found or expired")
            elif not owner.owns(record.owner):
                error = SessionAccessError("Browser session is owned by a different execution or actor")
            else:
                error = None
                if touch:
                    record.last_used_at = now
                    record.idle_expires_at = min(now + record.idle_ttl_seconds, record.maximum_expires_at)
        await self._close_records(expired, status="expired")
        if error:
            raise error
        assert record is not None
        return record

    async def close(self, *, owner: OwnerClaims, session_id: str, status: str = "closed") -> bool:
        async with self._lock:
            record = self._records.get(session_id)
            if record is None:
                return False
            if not owner.owns(record.owner):
                raise SessionAccessError("Browser session is owned by a different execution or actor")
            self._records.pop(session_id, None)
        await self._close_records([record], status=status)
        return True

    async def close_all_for_execution(self, execution_id: str, *, status: str = "execution_ended") -> int:
        async with self._lock:
            records = [record for record in self._records.values() if record.owner.execution_id == execution_id]
            for record in records:
                self._records.pop(record.session_id, None)
        await self._close_records(records, status=status)
        return len(records)

    async def expire(self) -> int:
        async with self._lock:
            records = self._remove_expired(self._clock())
        await self._close_records(records, status="expired")
        return len(records)

    async def close_all(self, *, status: str = "shutdown") -> int:
        async with self._lock:
            records = list(self._records.values())
            self._records.clear()
        await self._close_records(records, status=status)
        return len(records)

    async def list_for_owner(self, owner: OwnerClaims) -> list[SessionStatus]:
        async with self._lock:
            return [record.public_status() for record in self._records.values() if owner.owns(record.owner)]

    @property
    def active_count(self) -> int:
        return len(self._records)

    def _remove_expired(self, now: float) -> list[SessionRecord]:
        records = [
            record for record in self._records.values()
            if (
                record.expired(now)
                or bool(getattr(record.handle, "closed", False))
                or bool(getattr(record.handle, "crashed", False))
            )
        ]
        for record in records:
            self._records.pop(record.session_id, None)
        return records

    async def _close_records(self, records: list[SessionRecord], *, status: str) -> None:
        if not records:
            return
        for record in records:
            record.status = status
        results = await asyncio.gather(*(record.handle.close() for record in records), return_exceptions=True)
        self.cleanup_failures += sum(isinstance(result, Exception) for result in results)

