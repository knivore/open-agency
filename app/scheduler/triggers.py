from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain import ScheduleDefinition, ScheduleType


def _match_field(value: int, expr: str, *, minimum: int, maximum: int) -> bool:
    if expr == "*":
        return True
    if expr.startswith("*/"):
        step = int(expr[2:])
        return (value - minimum) % step == 0
    if "," in expr:
        return any(_match_field(value, part.strip(), minimum=minimum, maximum=maximum) for part in expr.split(","))
    return value == int(expr)


def next_cron_fire(cron_expression: str, *, now: datetime, timezone_name: str = "UTC") -> datetime:
    minute_expr, hour_expr, day_expr, month_expr, weekday_expr = cron_expression.split()
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local_now = now.astimezone(zone)
    candidate = local_now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = candidate + timedelta(days=366)
    while candidate <= limit:
        weekday = (candidate.weekday() + 1) % 7
        if (
                _match_field(candidate.minute, minute_expr, minimum=0, maximum=59)
                and _match_field(candidate.hour, hour_expr, minimum=0, maximum=23)
                and _match_field(candidate.day, day_expr, minimum=1, maximum=31)
                and _match_field(candidate.month, month_expr, minimum=1, maximum=12)
                and _match_field(weekday, weekday_expr, minimum=0, maximum=6)
        ):
            return candidate.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    raise ValueError(f"Unable to compute next fire time for cron '{cron_expression}'")


@dataclass(slots=True)
class TriggerComputation:
    next_fire_at: datetime | None
    ready: bool


def compute_next_fire(schedule: ScheduleDefinition, *, now: datetime) -> TriggerComputation:
    if not schedule.enabled:
        return TriggerComputation(next_fire_at=None, ready=False)
    if schedule.trigger_type == ScheduleType.MANUAL:
        return TriggerComputation(next_fire_at=None, ready=False)
    if schedule.trigger_type == ScheduleType.CRON:
        cron_expression = schedule.trigger_config["cron"]
        next_fire = next_cron_fire(cron_expression, now=now, timezone_name=schedule.timezone)
        ready = schedule.next_fire_at is not None and schedule.next_fire_at <= now
        return TriggerComputation(next_fire_at=next_fire, ready=ready)
    if schedule.trigger_type == ScheduleType.INTERVAL:
        interval_seconds = int(schedule.trigger_config["interval_seconds"])
        if schedule.last_fire_at is None:
            next_fire = (schedule.next_fire_at or now)
            return TriggerComputation(next_fire_at=next_fire, ready=next_fire <= now)
        next_fire = schedule.last_fire_at + timedelta(seconds=interval_seconds)
        return TriggerComputation(next_fire_at=next_fire, ready=next_fire <= now)
    return TriggerComputation(next_fire_at=schedule.next_fire_at, ready=False)
