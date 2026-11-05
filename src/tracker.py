"""Foreground-app tracker (M2).

TrackerState holds the rules: given a time, a foreground process and an idle
duration it returns the rows to write. It uses no database, no clock of its
own and no Windows call, so the rules can be tested exactly.

run() is the shell around it: sensor, SQLite writer, log file, instance lock.

What the state machine has to get right:
  * a switch row only when the foreground app changes, plus a heartbeat every
    HEARTBEAT_SECONDS, so a quiet hour cannot be mistaken for a dead tracker
  * __IDLE__ is back-dated to the last input, so the app before the away
    period is not credited with that time
  * __LOCKED__, __UNKNOWN__ and __GAP__ are explicit rows, never silence
  * rows are written in non-decreasing order even if the system clock jumps
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

import config
from src import db, providers, timeutil

# ASSUMPTION: priority when several conditions are true at once.
# Locked beats idle, because the user is away and the reason is known. Idle
# beats unknown, because no input explains the missing reading and the time
# belongs to the away period rather than to a real app's dwell. Unreadable
# while idle is counted separately so the quality problem stays visible.

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 2
EXIT_ERROR = 1

LOG_NAME = "tracker.log"


# --- What the state machine decided -------------------------------
@dataclass(frozen=True)
class Emission:
    """One row to write: either a switch event or a heartbeat."""

    kind: str                 # "switch" | "heartbeat"
    ts: float                 # UTC epoch seconds
    app: str | None = None    # process name or special token, for "switch"


class TrackerState:
    """The rules. Pure: same inputs, same outputs, forever."""

    def __init__(self, now: float) -> None:
        self.last_poll_ts = float(now)
        self.last_switch_ts = float(now)
        self.last_heartbeat_ts: float | None = None
        self.current_app: str | None = None
        # After a gap, time before the resume point is already accounted for by
        # the __GAP__ row, so idle back-dating must not reach behind it.
        self.gap_floor: float = 0.0
        self.counts = {
            "poll": 0,
            "switch": 0,
            "heartbeat": 0,
            "idle": 0,
            "locked": 0,
            "unknown": 0,
            "gap": 0,
            "clock_backwards": 0,
            "unreadable_while_idle": 0,
        }

    # ---- public
    def poll(self, now: float, app: str | None, idle_seconds: float | None) -> list[Emission]:
        """Advance the machine to `now` and return the rows that must be written.

        `app` is the raw process name from the sensor (None if unreadable) and
        `idle_seconds` the time since the last input (None if unknown).
        """
        out: list[Emission] = []

        # A clock that goes backwards would otherwise create out-of-order rows.
        # Treat it as "no time passed" and keep a counter so it is visible.
        if now < self.last_poll_ts:
            self.counts["clock_backwards"] += 1
            now = self.last_poll_ts

        delay = now - self.last_poll_ts

        # 1. Gap: the loop was asleep or blocked for longer than a poll cycle.
        #    Written at the last moment we actually observed the desktop, so the
        #    dwell of the previous row stops there and nothing spans the silence.
        if delay > config.GAP_THRESHOLD_SECONDS:
            self._write_switch(out, config.GAP, self.last_poll_ts)
            self.counts["gap"] += 1
            # Everything before this instant is explained by the gap itself.
            self.gap_floor = now

        # 2. Heartbeat: proves the loop was alive even with zero app changes.
        #    Emitted before the change check so the very first poll writes one.
        if self.last_heartbeat_ts is None or now - self.last_heartbeat_ts >= config.HEARTBEAT_SECONDS:
            self._write_heartbeat(out, now)

        # 3. Classify the current state into exactly one token.
        token, ts = self._classify(now, app, idle_seconds)

        # 4. Write only on change. A repeat of the same token adds no information
        #    and would inflate the row count without changing any dwell.
        if token != self.current_app:
            self._write_switch(out, token, ts)

        self.last_poll_ts = now
        self.counts["poll"] += 1
        return out

    # ---- internals
    def _classify(
        self, now: float, app: str | None, idle_seconds: float | None
    ) -> tuple[str, float]:
        """Return (token, timestamp). The timestamp is back-dated for idle.

        providers.describe does the mechanical part, mapping None to
        __UNKNOWN__ and the lock processes to __LOCKED__. The order of the
        checks is the assumption documented at the top of the file.
        """
        token = providers.describe(app)

        if token == config.LOCKED:
            return config.LOCKED, now

        idle = 0.0 if idle_seconds is None else float(idle_seconds)
        if idle >= config.IDLE_SECONDS:
            if token == config.UNKNOWN:
                # No reading to attribute. Count it first, so the unknown rate
                # is not flattered by the idle rule, then attribute the time.
                self.counts["unreadable_while_idle"] += 1
            # Back-date to the last input so the previous app is not credited
            # with the away period. Never earlier than the last row we wrote,
            # and never earlier than a gap that has already claimed that time.
            floor = max(self.last_switch_ts, self.gap_floor)
            return config.IDLE, max(now - idle, floor)

        return token, now

    def _write_switch(self, out: list[Emission], app: str, ts: float) -> None:
        # Clamp so rows are never out of order, whatever the clock does.
        ts = max(float(ts), self.last_switch_ts)
        out.append(Emission("switch", ts, app))
        self.last_switch_ts = ts
        self.current_app = app
        if app == config.IDLE:
            self.counts["idle"] += 1
        elif app == config.LOCKED:
            self.counts["locked"] += 1
        elif app == config.UNKNOWN:
            self.counts["unknown"] += 1
        else:
            self.counts["switch"] += 1

    def _write_heartbeat(self, out: list[Emission], ts: float) -> None:
        if self.last_heartbeat_ts is not None:
            ts = max(ts, self.last_heartbeat_ts)  # heartbeat table stays ordered
        out.append(Emission("heartbeat", ts))
        self.last_heartbeat_ts = ts
        self.counts["heartbeat"] += 1


# --- Writing ------------------------------------------------------
class SqliteWriter:
    """Writes emissions to the raw tables, committing after each poll cycle.

    Commit per cycle rather than per row: a cycle is at most one second of
    data, written atomically and never held in memory, so a killed process
    loses no row it has already reported.
    """

    def __init__(self, con, tracker_version: str = config.TRACKER_VERSION) -> None:
        self.con = con
        self.tracker_version = tracker_version

    def write(self, emissions: list[Emission]) -> int:
        if not emissions:
            return 0
        with self.con:  # transaction: committed on exit
            for e in emissions:
                if e.kind == "heartbeat":
                    self.con.execute("INSERT INTO heartbeat(ts) VALUES (?)", (e.ts,))
                else:
                    self.con.execute(
                        "INSERT INTO switches(ts, app, tracker_version) VALUES (?, ?, ?)",
                        (e.ts, e.app, self.tracker_version),
                    )
        return len(emissions)


# --- Single instance (5A.13) --------------------------------------
class InstanceLock:
    """Exclusive OS lock, so two trackers can never double-count.

    The lock is tied to the process handle, so a killed tracker leaves no
    stale lock to clean up by hand.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or (config.DATA_DIR / "tracker.lock"))
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                # msvcrt needs a byte to exist before it can lock one.
                if self.path.stat().st_size == 0:
                    fh.write(b"\0")
                    fh.flush()
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # another instance holds it
            fh.close()
            return False
        self._fh = fh
        self._write_pid()
        return True

    def _write_pid(self) -> None:
        """Record the PID for humans. Correctness comes from the OS lock."""
        try:
            if self._fh is not None:
                self._fh.seek(1)  # keep byte 0, which Windows has locked
                self._fh.truncate()
                self._fh.write(str(os.getpid()).encode("ascii"))
                self._fh.flush()
        except OSError:
            pass

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), 0, 1)  # LK_UNLCK
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def held(self) -> bool:
        """True if some process currently holds the lock (for --status)."""
        probe = InstanceLock(self.path)
        if probe.acquire():
            probe.release()
            return False
        return True


# --- Logging (5A.4: log errors, never content) --------------------
def setup_logger(log_path: str | Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """Rotating log in data/logs/tracker.log. Never records window content.

    Messages carry counts and error types only, so the log stays free of app
    names and is safe to paste into a report.
    """
    path = Path(log_path) if log_path else config.LOG_DIR / LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tracker")
    logger.setLevel(level)
    if not any(getattr(h, "baseFilename", None) == str(path) for h in logger.handlers):
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
    return logger


# --- The loop -----------------------------------------------------
def run(
    *,
    sensor: providers.Sensor | None = None,
    db_path: str | Path | None = None,
    poll_seconds: float = config.POLL_SECONDS,
    max_polls: int | None = None,
    clock=time.time,
    sleep=time.sleep,
    logger: logging.Logger | None = None,
    lock_path: str | Path | None = None,
) -> int:
    """Poll the sensor until stopped. Returns a process exit code.

    Each iteration is wrapped in try/except: a transient failure, such as a
    locked database or an odd window state, must not end the day's recording.
    Repeated failures back off instead of spinning.
    """
    log = logger or setup_logger()
    lock = InstanceLock(lock_path)
    sensor = sensor or providers.default_sensor()

    if not lock.acquire():
        log.warning("another tracker instance is running; exiting")
        return EXIT_ALREADY_RUNNING

    con = None
    try:
        path = db.initialise(db_path)
        con = db.connect(path)
        writer = SqliteWriter(con)
        state = TrackerState(clock())
        log.info("tracker %s started (schema %s)", config.TRACKER_VERSION, config.SCHEMA_VERSION)

        polls = 0
        consecutive_errors = 0
        while max_polls is None or polls < max_polls:
            try:
                emissions = state.poll(clock(), sensor.foreground_process(), sensor.idle_seconds())
                written = writer.write(emissions)
                consecutive_errors = 0
                if written and emissions and any(e.kind == "switch" for e in emissions):
                    log.debug("wrote %d rows", written)
            except Exception:
                consecutive_errors += 1
                # exc_info gives the type and traceback. No app name, no content.
                log.exception("poll failed (%d in a row); continuing", consecutive_errors)
                sleep(min(poll_seconds * consecutive_errors, 30))
            polls += 1
            sleep(poll_seconds)

        log.info("tracker stopped: %s", json.dumps(state.counts, sort_keys=True))
        return EXIT_OK
    except KeyboardInterrupt:
        log.info("tracker interrupted by user")
        return EXIT_OK
    except Exception:
        log.exception("tracker stopped on a fatal error")
        return EXIT_ERROR
    finally:
        if con is not None:
            con.close()
        lock.release()


# --- CLI: python -m src.tracker [--once] [--status] [--dry-run] ----
def _status(db_path: str | Path | None = None) -> int:
    """Print what the database currently holds. Useful before and after calibration."""
    path = db.initialise(db_path)
    con = db.connect(path)
    try:
        print("database:", path)
        print("schema_version:", db.schema_version(con))
        print("counts:", db.counts(con))
        row = con.execute("SELECT MAX(ts) AS ts FROM heartbeat").fetchone()
        if row and row["ts"]:
            age = time.time() - row["ts"]
            print("last heartbeat:", timeutil.local_str(row["ts"]), f"({age:.0f}s ago)")
        else:
            print("last heartbeat: none")
        row = con.execute("SELECT ts, app FROM switches ORDER BY ts DESC LIMIT 1").fetchone()
        print("last switch:", f"{row['app']} at {timeutil.local_str(row['ts'])}" if row else "none")
        print("instance lock held:", InstanceLock().held())
    finally:
        con.close()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Foreground-app tracker (metadata only).")
    parser.add_argument("--once", action="store_true", help="take one reading, write it, exit")
    parser.add_argument("--status", action="store_true", help="print database state and exit")
    parser.add_argument("--dry-run", action="store_true", help="print rows instead of writing them")
    args = parser.parse_args(argv)

    if args.status:
        return _status()

    if args.dry_run:
        sensor = providers.default_sensor()
        state = TrackerState(time.time())
        for _ in range(3):
            for e in state.poll(time.time(), sensor.foreground_process(), sensor.idle_seconds()):
                print(e)
            time.sleep(config.POLL_SECONDS)
        return EXIT_OK

    return run(sensor=providers.default_sensor(), max_polls=1 if args.once else None)


if __name__ == "__main__":
    sys.exit(main())
