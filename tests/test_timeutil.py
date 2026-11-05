"""Tests for the time rules (4A.1) and the known answers in section 12.

These are the cheapest tests in the project and the most valuable: every
feature, every lag and every report is labelled with a work day, so a boundary
bug here would silently corrupt the whole dataset.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import timeutil as tu

TZ = ZoneInfo("Asia/Colombo")


def local_ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    """Epoch seconds for a local (Colombo) wall-clock time."""
    return datetime(year, month, day, hour, minute, second, tzinfo=TZ).timestamp()


# --- The 04:59 / 05:00 boundary -----------------------------------
def test_just_before_five_belongs_to_previous_day():
    assert tu.work_day(local_ts(2026, 11, 6, 4, 59)) == "2026-11-05"


def test_last_second_before_five_belongs_to_previous_day():
    assert tu.work_day(local_ts(2026, 11, 6, 4, 59, 59)) == "2026-11-05"


def test_exactly_five_starts_the_new_day():
    assert tu.work_day(local_ts(2026, 11, 6, 5, 0)) == "2026-11-06"


def test_midnight_belongs_to_the_previous_work_day():
    # A commit at 00:30 local is late-night work from the day that just ended.
    assert tu.work_day(local_ts(2026, 11, 6, 0, 30)) == "2026-11-05"


def test_just_before_midnight_belongs_to_the_same_day():
    assert tu.work_day(local_ts(2026, 11, 6, 23, 59)) == "2026-11-06"


# --- Day bounds ---------------------------------------------------
def test_bounds_are_a_24_hour_half_open_window():
    start, end = tu.work_day_bounds("2026-11-05")
    assert end - start == 24 * 60 * 60
    assert tu.local_str(start) == "2026-11-05 05:00:00"
    assert tu.local_str(end) == "2026-11-06 05:00:00"
    # Half-open: the start belongs to the day, the end belongs to the next one.
    assert tu.work_day(start) == "2026-11-05"
    assert tu.work_day(end - 1) == "2026-11-05"
    assert tu.work_day(end) == "2026-11-06"


def test_bounds_tile_every_timestamp_exactly_once():
    start, end = tu.work_day_bounds("2026-03-14")
    ts = start
    seen = set()
    while ts < end:
        seen.add(tu.work_day(ts))
        ts += 1800  # every 30 minutes
    assert seen == {"2026-03-14"}


def test_bounds_across_a_month_end():
    start, end = tu.work_day_bounds("2026-11-30")
    assert tu.local_str(start) == "2026-11-30 05:00:00"
    assert tu.local_str(end) == "2026-12-01 05:00:00"


# --- Late night ---------------------------------------------------
@pytest.mark.parametrize(
    "hour, minute, expected",
    [
        (21, 59, False),
        (22, 0, True),
        (23, 59, True),
        (0, 0, True),
        (4, 59, True),
        (5, 0, False),
        (12, 0, False),
    ],
)
def test_late_night_window(hour, minute, expected):
    assert tu.is_late_night(local_ts(2026, 11, 6, hour, minute)) is expected


# --- Section 12: the two commit timestamps with known answers -----
def test_commit_at_1830_utc_is_local_midnight_and_counts_as_late_night():
    ts = datetime(2026, 1, 1, 18, 30, tzinfo=timezone.utc).timestamp()
    assert tu.local_str(ts) == "2026-01-02 00:00:00"
    assert tu.work_day(ts) == "2026-01-01"   # still the previous work day
    assert tu.is_late_night(ts) is True


def test_commit_at_2330_utc_is_local_five_am_and_is_not_late_night():
    ts = datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).timestamp()
    assert tu.local_str(ts) == "2026-01-02 05:00:00"
    assert tu.work_day(ts) == "2026-01-02"   # a new work day has started
    assert tu.is_late_night(ts) is False


# --- Refusals and helpers -----------------------------------------
def test_naive_datetime_is_rejected():
    with pytest.raises(ValueError):
        tu.to_ts(datetime(2026, 11, 6, 5, 0))


def test_roundtrip_aware_datetime():
    dt = datetime(2026, 11, 6, 9, 30, tzinfo=TZ)
    assert tu.to_local(tu.from_ts(tu.to_ts(dt))) == dt


def test_day_add_crosses_month_and_year_boundaries():
    assert tu.day_add("2026-11-30", 1) == "2026-12-01"
    assert tu.day_add("2026-01-01", -1) == "2025-12-31"
    assert tu.day_add("2026-11-05", 0) == "2026-11-05"


@pytest.mark.parametrize("value, expected", [
    ("2026-11-05", True),
    ("2026-1-5", False),
    ("05/11/2026", False),
    ("", False),
    (None, False),
])
def test_is_valid_day(value, expected):
    assert tu.is_valid_day(value) is expected


def test_now_ts_is_between_before_and_after():
    # Bounded with time.time(), the same clock now_ts() reads. datetime.timestamp()
    # only keeps microseconds, so mixing the two made this test fail about once in
    # a hundred runs with `now` ahead of `after` by less than one microsecond.
    before = time.time()
    now = tu.now_ts()
    after = time.time()
    assert before <= now <= after


def test_a_full_local_day_maps_to_exactly_two_work_days():
    # 00:00 -> 24:00 local must split at 05:00, i.e. two work days, not one.
    start = local_ts(2026, 11, 6, 0, 0)
    days = {tu.work_day(start + timedelta(minutes=m).total_seconds()) for m in range(0, 24 * 60, 5)}
    assert days == {"2026-11-05", "2026-11-06"}
