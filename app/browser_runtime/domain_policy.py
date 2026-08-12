"""Small persistent domain history and concurrency/rate policy."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class DomainHistory:
    domain: str
    successes: int = 0
    failures: int = 0
    challenges: int = 0
    fallback_successes: int = 0
    last_engine: str | None = None
    last_challenge: str | None = None
    cooldown_until: float = 0.0
    last_success_at: float | None = None
    updated_at: float = 0.0
    engine_successes: dict[str, int] | None = None
    engine_failures: dict[str, int] | None = None
    strategy_successes: dict[str, int] | None = None


@dataclass(slots=True)
class _DomainConcurrencyState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    active_limits: dict[int, int] = field(default_factory=dict)
    next_token: int = 0


class DomainPolicyStore:
    """Persist non-secret strategy evidence without coupling to AION DynamoDB."""

    def __init__(self, path: str | Path, *, clock=time.time) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.history_ttl_seconds = max(300, int(os.getenv("BROWSER_DOMAIN_HISTORY_TTL_SECONDS", "604800")))
        self.cooldown_seconds = max(1, int(os.getenv("BROWSER_DOMAIN_CHALLENGE_COOLDOWN_SECONDS", "60")))
        self.max_concurrency = max(1, int(os.getenv("BROWSER_DOMAIN_MAX_CONCURRENCY", "2")))
        self.minimum_interval_seconds = max(0.0, float(os.getenv("BROWSER_DOMAIN_MIN_INTERVAL_SECONDS", "0.25")))
        self._history = self._load()
        self._concurrency: dict[str, _DomainConcurrencyState] = {}
        self._last_started: dict[str, float] = {}
        self._timing_lock = asyncio.Lock()

    async def acquire(
            self,
            domain: str,
            *,
            max_concurrency: int | None = None,
            minimum_interval_seconds: float | None = None,
    ) -> "DomainLease":
        requested_limit = min(max_concurrency or self.max_concurrency, self.max_concurrency)
        state = self._concurrency.setdefault(domain, _DomainConcurrencyState())
        async with state.condition:
            await state.condition.wait_for(
                lambda: len(state.active_limits) + 1
                <= min([requested_limit, *state.active_limits.values()])
            )
            state.next_token += 1
            token = state.next_token
            state.active_limits[token] = requested_limit
        lease = DomainLease(state, token)
        try:
            async with self._timing_lock:
                now = self.clock()
                requested_interval = max(
                    self.minimum_interval_seconds,
                    minimum_interval_seconds or self.minimum_interval_seconds,
                )
                delay = requested_interval - (now - self._last_started.get(domain, 0.0))
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_started[domain] = self.clock()
            return lease
        except BaseException:
            # Cancellation during pacing must not leak a concurrency slot.
            await lease.release()
            raise

    def record(
            self,
            domain: str,
            *,
            engine: str,
            success: bool,
            challenge: str = "none",
            fallback: bool = False,
            cooldown_seconds: int | None = None,
    ) -> None:
        now = self.clock()
        self.prune(now=now)
        item = self._history.setdefault(domain, DomainHistory(domain=domain))
        item.updated_at = now
        item.last_engine = engine
        item.engine_successes = dict(item.engine_successes or {})
        item.engine_failures = dict(item.engine_failures or {})
        item.strategy_successes = dict(item.strategy_successes or {})
        if success:
            item.successes += 1
            item.last_success_at = now
            item.engine_successes[engine] = item.engine_successes.get(engine, 0) + 1
            strategy = "fallback" if fallback else "primary"
            item.strategy_successes[strategy] = item.strategy_successes.get(strategy, 0) + 1
            if fallback:
                item.fallback_successes += 1
        else:
            item.failures += 1
            item.engine_failures[engine] = item.engine_failures.get(engine, 0) + 1
        if challenge != "none":
            item.challenges += 1
            item.last_challenge = challenge
            # Honor publisher-provided retry windows but cap them so stale or
            # malicious headers cannot pin a domain indefinitely.
            requested_cooldown = cooldown_seconds if cooldown_seconds is not None else self.cooldown_seconds
            maximum_cooldown = max(self.cooldown_seconds, int(os.getenv("BROWSER_DOMAIN_MAX_COOLDOWN_SECONDS", "3600")))
            item.cooldown_until = now + min(max(self.cooldown_seconds, requested_cooldown), maximum_cooldown)
        self._save()

    def get(self, domain: str) -> DomainHistory | None:
        self.prune()
        return self._history.get(domain)

    def prune(self, *, now: float | None = None) -> int:
        current = self.clock() if now is None else now
        stale = [domain for domain, item in self._history.items()
                 if item.updated_at and current - item.updated_at >= self.history_ttl_seconds]
        for domain in stale:
            self._history.pop(domain, None)
        if stale:
            self._save()
        return len(stale)

    def _load(self) -> dict[str, DomainHistory]:
        if not self.path.exists():
            return {}
        try:
            raw: dict[str, dict[str, Any]] = json.loads(self.path.read_text(encoding="utf-8"))
            return {domain: DomainHistory(**value) for domain, value in raw.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({domain: asdict(item) for domain, item in self._history.items()}, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class DomainLease:
    def __init__(self, state: _DomainConcurrencyState, token: int) -> None:
        self._state = state
        self._token = token
        self._released = False

    async def release(self) -> None:
        if not self._released:
            self._released = True
            async with self._state.condition:
                self._state.active_limits.pop(self._token, None)
                self._state.condition.notify_all()
