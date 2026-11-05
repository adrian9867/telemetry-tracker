"""Tests that the database refuses bad data (M1 test list, sections 4 and 12).

Each test tries to write something that should be impossible and asserts the
database says no. If one of these ever passes a bad row, the constraint has
been lost and every number downstream becomes untrustworthy.
"""

from __future__ import annotations

import sqlite3

import pytest

import config
from src import db


@pytest.fixture()
def con(tmp_path):
    """An initialised database in a temporary directory."""
    path = tmp_path / "telemetry.db"
    db.initialise(path)
    connection = db.connect(path)
    yield connection
    connection.close()


def insert_morning(con, day="2026-11-05", **overrides):
    row = {
        "day": day,
        "ts": 1762_000_000.0,
        "sleep_h": 7.5,
        "awakenings": 1,
        "sleepy": 6,
        "meetings_prev": 2,
        "rest_prev_min": 90,
        "caffeine": 1,
    }
    row.update(overrides)
    con.execute(
        "INSERT INTO morning(day, ts, sleep_h, awakenings, sleepy, meetings_prev, rest_prev_min, caffeine)"
        " VALUES(:day, :ts, :sleep_h, :awakenings, :sleepy, :meetings_prev, :rest_prev_min, :caffeine)",
        row,
    )
    con.commit()


# --- Structure and metadata ---------------------------------------
def test_all_tables_exist(con):
    names = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "switches", "heartbeat", "morning", "evening", "checkin_log",
        "commits", "meetings", "prompts", "exit_survey", "meta", "settings",
    } <= names


def test_schema_version_is_recorded_and_matches_config(con):
    assert db.schema_version(con) == config.SCHEMA_VERSION == 1
    assert db.get_meta(con, "schema_version") == "1"


def test_meta_has_created_at_and_tracker_version(con):
    assert db.get_meta(con, "created_at") is not None
    assert db.get_meta(con, "tracker_version") == config.TRACKER_VERSION


def test_settings_have_phase_and_blind_mode(con):
    assert db.get_setting(con, "phase") == "0"
    assert db.get_setting(con, "blind_mode") in {"0", "1"}


def test_wal_mode_is_enabled(con):
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_integrity_check_is_ok(con):
    assert db.integrity_ok(con) is True


def test_counts_helper_starts_empty(con):
    assert set(db.counts(con).values()) == {0}


def test_initialise_twice_is_idempotent(tmp_path):
    path = tmp_path / "telemetry.db"
    db.initialise(path)
    con = db.connect(path)
    created_at = db.get_meta(con, "created_at")
    version = db.schema_version(con)
    con.close()

    db.initialise(path)  # must not raise, must not duplicate or reset anything

    con = db.connect(path)
    assert db.get_meta(con, "created_at") == created_at
    assert db.schema_version(con) == version == config.SCHEMA_VERSION
    assert con.execute("SELECT COUNT(*) FROM meta WHERE key='schema_version'").fetchone()[0] == 1
    con.close()


# --- Evening ratings: 0-10 only -----------------------------------
def test_fatigue_eleven_is_rejected(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO evening(day, ts, fatigue) VALUES('2026-11-05', 1.0, 11)")
    assert db.counts(con)["evening"] == 0


def test_fatigue_minus_one_is_rejected(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO evening(day, ts, fatigue) VALUES('2026-11-05', 1.0, -1)")


def test_workload_eleven_is_rejected(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO evening(day, ts, fatigue, workload) VALUES('2026-11-05', 1.0, 5, 11)")


def test_fatigue_boundaries_are_accepted(con):
    for day, value in (("2026-11-05", 0), ("2026-11-06", 10)):
        con.execute("INSERT INTO evening(day, ts, fatigue) VALUES(?, ?, ?)", (day, 1.0, value))
    con.commit()
    assert db.counts(con)["evening"] == 2


def test_missing_fatigue_is_null_not_zero(con):
    con.execute("INSERT INTO evening(day, ts, fatigue) VALUES('2026-11-05', 1.0, NULL)")
    con.commit()
    value = con.execute("SELECT fatigue FROM evening WHERE day='2026-11-05'").fetchone()[0]
    assert value is None


# --- One answer per day per check-in ------------------------------
def test_second_morning_entry_for_the_same_day_is_rejected(con):
    insert_morning(con)
    with pytest.raises(sqlite3.IntegrityError):
        insert_morning(con, sleepy=3)   # a second answer, same day
    assert db.counts(con)["morning"] == 1


def test_second_evening_entry_for_the_same_day_is_rejected(con):
    con.execute("INSERT INTO evening(day, ts, fatigue) VALUES('2026-11-05', 1.0, 4)")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO evening(day, ts, fatigue) VALUES('2026-11-05', 2.0, 8)")


@pytest.mark.parametrize("column, bad_value", [
    ("sleep_h", 25),
    ("sleep_h", -1),
    ("awakenings", 21),
    ("sleepy", 11),
    ("meetings_prev", -1),
    ("rest_prev_min", -5),
    ("caffeine", 2),
])
def test_morning_fields_are_range_checked(con, column, bad_value):
    with pytest.raises(sqlite3.IntegrityError):
        insert_morning(con, **{column: bad_value})
    assert db.counts(con)["morning"] == 0


# --- Check-in log and "a skip is not a zero" ----------------------
def test_checkin_log_status_is_limited_to_the_four_outcomes(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO checkin_log(day, kind, status, ts) VALUES('2026-11-05','morning','maybe',1.0)"
        )


def test_checkin_log_kind_is_limited(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO checkin_log(day, kind, status, ts) VALUES('2026-11-05','afternoon','missed',1.0)"
        )


def test_a_skipped_checkin_leaves_no_value_row(con):
    con.execute(
        "INSERT INTO checkin_log(day, kind, status, ts) VALUES('2026-11-05','morning','skipped',1.0)"
    )
    con.commit()
    assert db.counts(con)["morning"] == 0
    assert db.counts(con)["checkin_log"] == 1


# --- Switches, strict typing and required columns -----------------
def test_switches_requires_app_and_version(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO switches(ts, app, tracker_version) VALUES(1.0, NULL, '0.1.0')")
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO switches(ts, app, tracker_version) VALUES(1.0, 'Code.exe', NULL)")


def test_switches_rejects_text_in_a_real_column(con):
    # STRICT tables: this is why ts cannot silently become a string.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO switches(ts, app, tracker_version) VALUES('not-a-time', 'Code.exe', '0.1.0')")


def test_switches_rejects_nonpositive_timestamp(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO switches(ts, app, tracker_version) VALUES(0, 'Code.exe', '0.1.0')")


def test_special_token_can_be_stored(con):
    for token in sorted(config.SPECIAL_TOKENS):
        con.execute(
            "INSERT INTO switches(ts, app, tracker_version) VALUES(?, ?, ?)",
            (1762_000_000.0, token, config.TRACKER_VERSION),
        )
    con.commit()
    stored = {r["app"] for r in con.execute("SELECT app FROM switches")}
    assert stored == config.SPECIAL_TOKENS
    assert all(not config.is_real_app(a) for a in stored)


# --- Commits and meetings -----------------------------------------
def test_commit_sha_is_a_primary_key(con):
    con.execute("INSERT INTO commits(sha, ts) VALUES('abc123', 1.0)")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO commits(sha, ts) VALUES('abc123', 2.0)")


def test_meeting_end_cannot_precede_start(con):
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO meetings(start_ts, end_ts) VALUES(100.0, 50.0)")


def test_duplicate_meeting_is_rejected(con):
    con.execute("INSERT INTO meetings(start_ts, end_ts) VALUES(100.0, 200.0)")
    con.commit()
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO meetings(start_ts, end_ts) VALUES(100.0, 200.0)")


# --- Committed data is visible to other processes (tracker + dashboard) ----
def test_committed_write_is_visible_from_a_second_connection(tmp_path):
    path = tmp_path / "telemetry.db"
    db.initialise(path)

    writer = db.connect(path)
    writer.execute(
        "INSERT INTO switches(ts, app, tracker_version) VALUES(?, ?, ?)",
        (1762_000_000.0, "Code.exe", config.TRACKER_VERSION),
    )
    writer.commit()

    reader = db.connect(path)
    assert db.counts(reader)["switches"] == 1
    reader.close()
    writer.close()
