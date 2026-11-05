"""Time conversion rules (4A.1).

Timestamps are stored as UTC epoch seconds. Anything a person reads, such as
which day a row belongs to or whether it was late at night, is derived from
the local timezone, and the work day starts at 05:00 local rather than at
midnight. Both rules are defined here so they cannot drift apart elsewhere.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import DAY_START_HOUR, LATE_NIGHT_START_HOUR, LOCAL_TZ

_TZ = ZoneInfo(LOCAL_TZ)


def now_ts() -> float:
    """Current time as UTC epoch seconds."""
    return time.time()


def to_ts(dt: datetime) -> float:
    """Convert a timezone-aware datetime to epoch seconds.

    A naive datetime is rejected: guessing the timezone is the bug this
    module exists to prevent.
    """
    if dt.tzinfo is None:
        raise ValueError("naive datetime: attach a timezone with localize/to_ts")
    return dt.timestamp()


def from_ts(ts: float) -> datetime:
    """Epoch seconds -> UTC datetime."""
    return datetime.fromtimestamp(ts, tz=ZoneInfo("UTC"))


def to_local(dt: datetime) -> datetime:
    """Convert any aware datetime to the project's local timezone."""
    return dt.astimezone(_TZ)


def local_dt(ts: float) -> datetime:
    """Epoch seconds -> local datetime."""
    return datetime.fromtimestamp(ts, tz=_TZ)


def local_str(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Readable local timestamp, for logs and reports (never for storage)."""
    return local_dt(ts).strftime(fmt)


def work_day(ts: float) -> str:
    """Local date of a timestamp, shifted so that 05:00 is the day boundary.

    04:59 local on 6 Nov -> "2026-11-05"; 05:00 local on 6 Nov -> "2026-11-06".
    """
    local = local_dt(ts)
    if local.hour < DAY_START_HOUR:
        local -= timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def work_day_bounds(day: str) -> tuple[float, float]:
    """(start_ts, end_ts) of a work day: 05:00 local to 05:00 local next day.

    The end is exclusive, so every timestamp belongs to exactly one day.
    """
    start_local = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=_TZ) + timedelta(
        hours=DAY_START_HOUR
    )
    end_local = start_local + timedelta(days=1)
    return start_local.timestamp(), end_local.timestamp()


def is_late_night(ts: float) -> bool:
    """True from 22:00 local until (not including) 05:00 local."""
    hour = local_dt(ts).hour
    return hour >= LATE_NIGHT_START_HOUR or hour < DAY_START_HOUR


def day_add(day: str, delta: int) -> str:
    """Shift a 'YYYY-MM-DD' work day by delta days."""
    base = date.fromisoformat(day)
    return (base + timedelta(days=delta)).isoformat()


def is_valid_day(value: str) -> bool:
    """True if value looks like an ISO work day (YYYY-MM-DD)."""
    try:
        return date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


if __name__ == "__main__":  # quick manual check: python -m src.timeutil
    import config

    sample = datetime(2026, 11, 6, 4, 59, tzinfo=_TZ)
    print("local 04:59 on 6 Nov ->", work_day(to_ts(sample)))
    sample = sample.replace(hour=5)
    print("local 05:00 on 6 Nov ->", work_day(to_ts(sample)))
    print("bounds for 2026-11-05:", [local_str(t) for t in work_day_bounds("2026-11-05")])
    print("late night at 02:00:", is_late_night(to_ts(sample.replace(hour=2))))
    print("config.LOCAL_TZ:", config.LOCAL_TZ)
