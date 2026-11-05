"""Tests for the tracker state machine and the loop (M2).

These cover the failure modes that would corrupt the study rather than the
happy path:

  * an idle period credited to the app that was open when the user walked away
  * a sleep or hibernate that leaves no trace, so dwell spans it
  * a heartbeat that stops, which makes "no switches" look like "dead"
  * a second tracker instance double-counting every switch

Expected values are worked out by hand from the rule, not read back from the
code.
"""

from __future__ import annotations

import sqlite3

import pytest

import config
from src import db, providers, tracker
from src.tracker import EXIT_ALREADY_RUNNING, EXIT_OK, InstanceLock, SqliteWriter, TrackerState

T0 = 1_700_000_000.0  # arbitrary fixed instant; all times are relative to it


# --- Helpers ------------------------------------------------------
def apps(emissions) -> list:
    """The app token of every switch row, in order."""
    return [e.app for e in emissions if e.kind == "switch"]


def times(emissions) -> list:
    """The timestamp of every switch row, in order."""
    return [e.ts for e in emissions if e.kind == "switch"]


def beats(emissions) -> list:
    return [e.ts for e in emissions if e.kind == "heartbeat"]


def simulate(state, start, end, app_fn, idle_fn, step: float = 1.0):
    """Poll once per `step` seconds, like the real loop, and collect emissions.

    A test must not jump time in one big step unless it means to simulate a
    stalled loop. Any jump larger than GAP_THRESHOLD_SECONDS is a gap by
    definition, and the state machine says so.
    """
    out = []
    t = start
    while t <= end:
        out += state.poll(t, app_fn(t), idle_fn(t))
        t += step
    return out


class FakeSensor:
    """A scripted desktop: one (app, idle_seconds) pair per poll."""

    def __init__(self, script):
        self.script = list(script)
        self.i = 0
        self._idle = 0.0

    def foreground_process(self):
        app, idle = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        if isinstance(app, Exception):
            raise app
        self._idle = idle
        return app

    def idle_seconds(self):
        return self._idle


class FakeClock:
    def __init__(self, start: float = T0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "telemetry.db"


def run_tracker(script, db_path, tmp_path, max_polls=None, poll_seconds=1.0, logger=None):
    """Run the real loop with a fake desktop and a fake clock."""
    clock = FakeClock()
    sensor = FakeSensor(script)
    log = logger or tracker.setup_logger(tmp_path / "tracker.log")
    code = tracker.run(
        sensor=sensor,
        db_path=db_path,
        poll_seconds=poll_seconds,
        max_polls=max_polls if max_polls is not None else len(script),
        clock=clock,
        sleep=clock.advance,
        logger=log,
        lock_path=tmp_path / "tracker.lock",
    )
    return code, clock


def read_switches(db_path):
    con = db.connect(db_path)
    try:
        return [tuple(r) for r in con.execute("SELECT ts, app, tracker_version FROM switches ORDER BY ts, rowid")]
    finally:
        con.close()


# ==========================================================================
# The state machine: one behaviour per test
# ==========================================================================
def test_first_poll_writes_a_heartbeat_and_the_current_app():
    state = TrackerState(T0)
    out = state.poll(T0, "Code.exe", 0)
    assert [e.kind for e in out] == ["heartbeat", "switch"]
    assert apps(out) == ["Code.exe"]
    assert beats(out) == [T0]


def test_same_app_repeated_writes_nothing():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    for i in range(1, 50):
        assert state.poll(T0 + i, "Code.exe", 0) == []


def test_app_change_writes_one_row_at_the_time_of_the_change():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    out = state.poll(T0 + 7, "Slack.exe", 0)
    assert apps(out) == ["Slack.exe"]
    assert times(out) == [T0 + 7]


def test_heartbeat_every_60_seconds_even_with_no_changes():
    state = TrackerState(T0)
    stamps = []
    for i in range(0, 181):
        stamps += beats(state.poll(T0 + i, "Code.exe", 0))
    assert stamps == [T0, T0 + 60, T0 + 120, T0 + 180]


def test_idle_is_back_dated_to_the_last_input():
    """6 minutes without input: the away time belongs to __IDLE__, not to Code."""
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)  # last input at T0

    out = simulate(state, T0 + 1, T0 + 360, lambda t: "Code.exe", lambda t: t - T0)

    switch_rows = [(e.app, e.ts) for e in out if e.kind == "switch"]
    assert switch_rows == [(config.IDLE, T0)]  # back-dated to the last input
    assert state.counts["idle"] == 1
    assert state.counts["gap"] == 0


def test_idle_threshold_boundary():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)

    out = simulate(state, T0 + 1, T0 + 299, lambda t: "Code.exe", lambda t: t - T0)
    assert config.IDLE not in apps(out)

    out += simulate(state, T0 + 300, T0 + 300, lambda t: "Code.exe", lambda t: t - T0)
    assert apps(out) == [config.IDLE]


def test_idle_written_once_while_it_continues():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    out = simulate(state, T0 + 1, T0 + 600, lambda t: "Code.exe", lambda t: t - T0)
    assert apps(out).count(config.IDLE) == 1


def test_return_from_idle_repeats_the_same_app():
    """After __IDLE__ the real app is written again, even if it never changed."""
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    simulate(state, T0 + 1, T0 + 300, lambda t: "Code.exe", lambda t: t - T0)  # goes idle

    out = state.poll(T0 + 301, "Code.exe", 1)  # input returned one second ago

    assert apps(out) == ["Code.exe"]
    assert times(out) == [T0 + 301]


def test_idle_never_written_before_the_previous_row():
    """The clamp: a suspiciously large idle value cannot create a row in the past."""
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    state.poll(T0 + 100, "Slack.exe", 0)      # last row at T0+100
    out = state.poll(T0 + 120, "Slack.exe", 10_000)

    assert apps(out) == [config.IDLE]
    assert times(out) == [T0 + 100]           # clamped to the last real row


def test_lock_screen_writes_locked():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    out = state.poll(T0 + 5, "LockApp.exe", 5)
    assert apps(out) == [config.LOCKED]


def test_lock_screen_name_is_case_insensitive_and_covers_logonui():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    assert apps(state.poll(T0 + 1, "lockapp.exe", 1)) == [config.LOCKED]
    state2 = TrackerState(T0)
    state2.poll(T0, "Code.exe", 0)
    assert apps(state2.poll(T0 + 1, "LogonUI.exe", 1)) == [config.LOCKED]


def test_locked_beats_idle():
    """A locked screen after 10 idle minutes is __LOCKED__, not __IDLE__."""
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)

    out = simulate(state, T0 + 1, T0 + 600, lambda t: "LockApp.exe", lambda t: t - T0)

    assert apps(out) == [config.LOCKED]
    assert state.counts["idle"] == 0


def test_unlock_writes_the_real_app():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    state.poll(T0 + 5, "LockApp.exe", 5)
    out = state.poll(T0 + 9, "Code.exe", 1)
    assert apps(out) == ["Code.exe"]
    assert times(out) == [T0 + 9]


def test_unreadable_process_writes_unknown_once():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    assert apps(state.poll(T0 + 1, None, 1)) == [config.UNKNOWN]
    assert apps(state.poll(T0 + 2, None, 2)) == []
    assert apps(state.poll(T0 + 3, "", 3)) == []
    assert apps(state.poll(T0 + 4, "Code.exe", 4)) == ["Code.exe"]


def test_eight_hour_jump_writes_gap_at_the_previous_poll_time():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    state.poll(T0 + 50, "Code.exe", 0)  # last confirmed sighting at T0+50

    out = state.poll(T0 + 50 + 8 * 3600, "Code.exe", 0)

    assert apps(out) == [config.GAP, "Code.exe"]
    assert times(out) == [T0 + 50, T0 + 50 + 8 * 3600]


def test_no_dwell_inflated_by_a_gap():
    """Section 12: no real-app dwell may span the simulated 8-hour jump."""
    state = TrackerState(T0)
    rows = []
    for i in range(0, 20):
        rows += [(e.app, e.ts) for e in state.poll(T0 + i, "Code.exe", 0) if e.kind == "switch"]
    rows += [(e.app, e.ts) for e in state.poll(T0 + 20 + 8 * 3600, "Code.exe", 0) if e.kind == "switch"]

    assert rows[1][0] == config.GAP  # [0] is the app row at T0
    for (app, ts), (_next_app, next_ts) in zip(rows, rows[1:]):
        if config.is_real_app(app):
            assert next_ts - ts <= config.GAP_THRESHOLD_SECONDS, f"{app} dwell spans the gap"


def test_gap_then_different_app():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    out = state.poll(T0 + 8 * 3600, "Slack.exe", 0)
    assert apps(out) == [config.GAP, "Slack.exe"]


def test_gap_threshold_is_strictly_greater_than():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    assert config.GAP not in apps(state.poll(T0 + config.GAP_THRESHOLD_SECONDS, "Code.exe", 0))

    state2 = TrackerState(T0)
    state2.poll(T0, "Code.exe", 0)
    assert config.GAP in apps(state2.poll(T0 + config.GAP_THRESHOLD_SECONDS + 1, "Code.exe", 0))


def test_gap_then_idle_does_not_reclaim_the_gap_as_idle_time():
    """The gap already owns that time; idle may only start at the resume point."""
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    out = state.poll(T0 + 8 * 3600, "Code.exe", 8 * 3600)

    gap_ts = times(out)[0]
    idle_ts = times(out)[1]
    assert apps(out) == [config.GAP, config.IDLE]
    assert idle_ts == T0 + 8 * 3600          # resumes where the gap stopped
    assert idle_ts - gap_ts == 8 * 3600      # the gap keeps its own duration


def test_clock_going_backwards_never_writes_out_of_order_rows():
    state = TrackerState(T0)
    state.poll(T0, "Code.exe", 0)
    out = state.poll(T0 - 100, "Slack.exe", 0)

    assert apps(out) == ["Slack.exe"]
    assert times(out) == [T0]
    assert state.counts["clock_backwards"] == 1


def test_switch_timestamps_are_never_decreasing_over_a_wild_sequence():
    state = TrackerState(T0)
    seen: list = []
    now = T0
    script = ["Code.exe", "Slack.exe", None, "Code.exe", "LockApp.exe", "Code.exe"]
    steps = [1, 2, -5, 31, 0.5, 8 * 3600, 1, 60, 300, 1]
    for i, step in enumerate(steps):
        now += step
        seen += times(state.poll(now, script[i % len(script)], max(0, step)))
    assert seen == sorted(seen)


def test_unreadable_while_idle_is_counted_not_hidden():
    state = TrackerState(T0)
    state.poll(T0, None, 0)
    state.poll(T0 + 400, None, 400)
    assert state.counts["unreadable_while_idle"] == 1
    assert apps(state.poll(T0 + 401, None, 401)) == []


# ==========================================================================
# The writer: what actually lands in SQLite
# ==========================================================================
def test_writer_inserts_and_commits(db_path):
    path = db.initialise(db_path)
    con = db.connect(path)
    try:
        SqliteWriter(con).write(
            [
                tracker.Emission("heartbeat", T0),
                tracker.Emission("switch", T0, "Code.exe"),
            ]
        )
    finally:
        con.close()

    other = db.connect(path)  # a second connection sees the rows: committed
    try:
        assert other.execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0] == 1
        assert other.execute("SELECT app FROM switches").fetchone()[0] == "Code.exe"
        assert db.integrity_ok(other)
    finally:
        other.close()


def test_writer_stamps_the_tracker_version(db_path):
    path = db.initialise(db_path)
    con = db.connect(path)
    try:
        SqliteWriter(con).write([tracker.Emission("switch", T0, "Code.exe")])
    finally:
        con.close()
    assert read_switches(path)[0][2] == config.TRACKER_VERSION


def test_writer_rejects_a_bad_timestamp(db_path):
    path = db.initialise(db_path)
    con = db.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        SqliteWriter(con).write([tracker.Emission("switch", 0, "Code.exe")])
    con.close()


# ==========================================================================
# The loop, end to end with a fake desktop
# ==========================================================================
def test_five_apps_produce_five_switch_rows(db_path, tmp_path):
    """The M2 manual test, automated: switch apps, see one row per change."""
    script = [("Code.exe", 0)] * 3 + [("Slack.exe", 0)] * 3 + [("chrome.exe", 0)] * 3
    script += [("Teams.exe", 0)] * 3 + [("explorer.exe", 0)] * 3

    code, _ = run_tracker(script, db_path, tmp_path)

    assert code == EXIT_OK
    rows = read_switches(db_path)
    assert [app for _ts, app, _v in rows] == [
        "Code.exe", "Slack.exe", "chrome.exe", "Teams.exe", "explorer.exe",
    ]
    assert len(rows) >= 5


def test_a_quiet_hour_is_one_row_plus_heartbeats(db_path, tmp_path):
    """Stayed in one app for three minutes: 1 switch row, 4 heartbeats."""
    code, _ = run_tracker([("Code.exe", 0)] * 181, db_path, tmp_path)

    assert code == EXIT_OK
    assert len(read_switches(db_path)) == 1
    con = db.connect(db_path)
    try:
        beats_count = con.execute("SELECT COUNT(*) FROM heartbeat").fetchone()[0]
    finally:
        con.close()
    assert beats_count == 4


def test_no_two_consecutive_rows_are_identical(db_path, tmp_path):
    script = [("Code.exe", 0)] * 5 + [("Slack.exe", 0)] * 5 + [("Code.exe", 0)] * 5
    run_tracker(script, db_path, tmp_path)

    names = [app for _ts, app, _v in read_switches(db_path)]
    assert all(a != b for a, b in zip(names, names[1:]))


def test_idle_and_lock_appear_in_the_database(db_path, tmp_path):
    script = [("Code.exe", 0)] * 4 + [("Code.exe", 400)] + [("LockApp.exe", 400)]
    run_tracker(script, db_path, tmp_path)

    names = [app for _ts, app, _v in read_switches(db_path)]
    assert config.IDLE in names
    assert config.LOCKED in names
    assert names.index(config.IDLE) < names.index(config.LOCKED)


def test_a_sensor_error_does_not_stop_the_tracker(db_path, tmp_path):
    """Resilience: one bad reading is logged and the next poll continues."""
    script = [("Code.exe", 0), (RuntimeError("sensor exploded"), 0)] + [("Slack.exe", 0)] * 4
    code, _ = run_tracker(script, db_path, tmp_path)

    assert code == EXIT_OK
    names = [app for _ts, app, _v in read_switches(db_path)]
    assert names[0] == "Code.exe"
    assert "Slack.exe" in names
    assert "ERROR" in (tmp_path / "tracker.log").read_text(encoding="utf-8")


def test_rows_survive_a_failure_after_they_were_written(db_path, tmp_path):
    """Commit per poll means an uncommitted write cannot be lost later."""
    script = [("Code.exe", 0)] * 3 + [(RuntimeError("die"), 0)] * 3
    run_tracker(script, db_path, tmp_path, max_polls=6)
    assert len(read_switches(db_path)) == 1


def test_log_file_contains_no_app_names(db_path, tmp_path):
    """Privacy: the log is safe to paste into the report."""
    run_tracker([("SecretApp.exe", 0)] * 5, db_path, tmp_path)
    log = (tmp_path / "tracker.log").read_text(encoding="utf-8")
    assert "SecretApp.exe" not in log
    assert "started" in log


def test_a_second_instance_exits_before_writing_anything(db_path, tmp_path):
    """5A.13: two trackers cannot double-count."""
    lock = InstanceLock(tmp_path / "tracker.lock")
    assert lock.acquire() is True
    try:
        code = tracker.run(
            sensor=FakeSensor([("Code.exe", 0)] * 3),
            db_path=db_path,
            max_polls=3,
            clock=FakeClock(),
            sleep=lambda _s: None,
            logger=tracker.setup_logger(tmp_path / "tracker.log"),
            lock_path=tmp_path / "tracker.lock",
        )
    finally:
        lock.release()

    assert code == EXIT_ALREADY_RUNNING
    assert not db_path.exists(), "a blocked instance must not touch the database"


def test_lock_is_released_when_the_loop_ends(db_path, tmp_path):
    run_tracker([("Code.exe", 0)] * 2, db_path, tmp_path)
    probe = InstanceLock(tmp_path / "tracker.lock")
    assert probe.acquire() is True
    probe.release()


def test_lock_is_exclusive_and_reusable():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.lock"
        first = InstanceLock(path)
        second = InstanceLock(path)
        assert first.acquire() is True
        assert second.acquire() is False
        assert first.held() is True
        first.release()
        assert second.acquire() is True
        second.release()


# ==========================================================================
# Sensors: the pieces that only run on Windows
# ==========================================================================
def test_describe_maps_special_cases():
    assert providers.describe(None) == config.UNKNOWN
    assert providers.describe("") == config.UNKNOWN
    assert providers.describe("   ") == config.UNKNOWN
    assert providers.describe("LockApp.exe") == config.LOCKED
    assert providers.describe("LOCKAPP.EXE") == config.LOCKED
    assert providers.describe("Code.exe") == "Code.exe"


def test_default_sensor_refuses_on_the_wrong_platform():
    import sys

    if sys.platform == "win32":
        pytest.skip("real sensor is expected to work here")
    with pytest.raises(RuntimeError, match="only records on Windows"):
        providers.default_sensor()


def test_status_command_reports_counts(db_path, tmp_path, capsys):
    run_tracker([("Code.exe", 0)] * 3, db_path, tmp_path)
    assert tracker._status(db_path) == EXIT_OK
    out = capsys.readouterr().out
    assert "schema_version: 1" in out
    assert "Code.exe" in out
