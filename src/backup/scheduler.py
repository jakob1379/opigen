from __future__ import annotations

import time as time_module
from collections.abc import Callable
from datetime import datetime, timedelta

import structlog

from backup.config import ScheduleConfig

LOGGER = structlog.get_logger(__name__)


def calculate_next_run(now: datetime, schedule: ScheduleConfig) -> datetime:
    if schedule.frequency == "hourly":
        base = now.replace(minute=0, second=0, microsecond=0)
        return base + timedelta(hours=1)

    candidate = datetime.combine(now.date(), schedule.preferred_time, tzinfo=now.tzinfo)
    if schedule.frequency == "daily":
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if schedule.frequency == "weekly":
        days_until_monday = (7 - candidate.weekday()) % 7
        candidate += timedelta(days=days_until_monday)
        if candidate <= now:
            candidate += timedelta(weeks=1)
        return candidate

    raise ValueError(f"Unsupported schedule frequency: {schedule.frequency}")


def serve(
    schedule: ScheduleConfig,
    run_once: Callable[[], bool],
    *,
    sleep: Callable[[float], None] = time_module.sleep,
    on_next_run: Callable[[datetime], None] | None = None,
) -> None:
    if not schedule.enabled:
        LOGGER.info("schedule_disabled_running_once")
        run_once()
        return

    while True:
        now = datetime.now().astimezone()
        next_run = calculate_next_run(now, schedule)
        if on_next_run is not None:
            on_next_run(next_run)
        delay = max(0.0, (next_run - now).total_seconds())
        LOGGER.info("next_run_scheduled", next_run=next_run.isoformat())
        sleep(delay)
        run_once()
