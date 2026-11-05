"""The only module that knows the database schema.

Bad rows are rejected by the schema itself:
  * NOT NULL on every required column;
  * CHECK ranges on ratings, so fatigue = 11 fails;
  * morning and evening keyed on the day, so a second answer fails;
  * STRICT tables, so text cannot land in a REAL column;
  * WAL mode, so the tracker can write while the dashboard reads.

Schema changes are never made by hand: add a numbered entry to MIGRATIONS,
bump SCHEMA_VERSION in config.py, and log it in DATA_LOG.md.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import config

# --- Migrations: version -> ordered SQL, one transaction per version ----
_V1_STATEMENTS = (
    # Raw event stream: one row per change of foreground app or special event.
    """
    CREATE TABLE switches (
        ts              REAL NOT NULL CHECK (ts > 0),
        app             TEXT NOT NULL CHECK (length(app) > 0),
        tracker_version TEXT NOT NULL CHECK (length(tracker_version) > 0)
    ) STRICT
    """,
    # Proof that the tracker was alive, even when nothing changed.
    """
    CREATE TABLE heartbeat (
        ts REAL NOT NULL CHECK (ts > 0)
    ) STRICT
    """,
    # Morning diary. A missing answer is no row, never a zero.
    """
    CREATE TABLE morning (
        day           TEXT PRIMARY KEY CHECK (length(day) = 10),
        ts            REAL,
        sleep_h       REAL CHECK (sleep_h BETWEEN 0 AND 24),
        awakenings    INT  CHECK (awakenings BETWEEN 0 AND 20),
        sleepy        INT  CHECK (sleepy BETWEEN 0 AND 10),
        meetings_prev INT  CHECK (meetings_prev >= 0),
        rest_prev_min INT  CHECK (rest_prev_min >= 0),
        caffeine      INT  CHECK (caffeine IN (0, 1))
    ) STRICT
    """,
    # Evening diary (outcome).
    """
    CREATE TABLE evening (
        day      TEXT PRIMARY KEY CHECK (length(day) = 10),
        ts       REAL,
        fatigue  INT CHECK (fatigue BETWEEN 0 AND 10),
        workload INT CHECK (workload BETWEEN 0 AND 10)
    ) STRICT
    """,
    # Compliance log: answered / skipped / snoozed / missed, with no value.
    """
    CREATE TABLE checkin_log (
        day    TEXT NOT NULL CHECK (length(day) = 10),
        kind   TEXT NOT NULL CHECK (kind IN ('morning', 'evening')),
        status TEXT NOT NULL CHECK (status IN ('answered', 'skipped', 'snoozed', 'missed')),
        ts     REAL NOT NULL CHECK (ts > 0)
    ) STRICT
    """,
    # Commit timestamps only; the SHA exists purely to de-duplicate.
    """
    CREATE TABLE commits (
        sha TEXT PRIMARY KEY CHECK (length(sha) > 0),
        ts  REAL NOT NULL CHECK (ts > 0)
    ) STRICT
    """,
    # Meeting start/end only: no titles, no attendees.
    """
    CREATE TABLE meetings (
        start_ts REAL NOT NULL CHECK (start_ts > 0),
        end_ts   REAL NOT NULL CHECK (end_ts >= start_ts),
        PRIMARY KEY (start_ts, end_ts)
    ) STRICT
    """,
    # Tier 3 micro-randomised prompts.
    """
    CREATE TABLE prompts (
        ts       REAL NOT NULL CHECK (ts > 0),
        kind     TEXT NOT NULL,
        arm      TEXT,
        response TEXT
    ) STRICT
    """,
    # Tier 3 exit survey (SUS items etc.).
    """
    CREATE TABLE exit_survey (
        day   TEXT NOT NULL CHECK (length(day) = 10),
        item  TEXT NOT NULL,
        value INT  CHECK (value BETWEEN 0 AND 10)
    ) STRICT
    """,
    """
    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT
    """,
    "CREATE INDEX idx_switches_ts ON switches(ts)",
    "CREATE INDEX idx_heartbeat_ts ON heartbeat(ts)",
    "CREATE INDEX idx_checkin_log_day ON checkin_log(day)",
    "CREATE INDEX idx_commits_ts ON commits(ts)",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {1: _V1_STATEMENTS}

DEFAULT_SETTINGS = {
    "phase": "0",       # 0 = shakedown, A = baseline, B = feedback
    "blind_mode": "0",  # ASSUMPTION: 0 during Phase 0 so bugs are visible;
                        # set to 1 for Phase A (section 6)
}


# --- Connections --------------------------------------------------
def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database with the pragmas the project depends on.

    WAL lets the tracker write while the dashboard reads. synchronous=NORMAL
    is the usual pairing with WAL: safe against a process crash, because the
    -wal file is replayed on the next open.
    """
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=15000")
    return con


def schema_version(con: sqlite3.Connection) -> int:
    """Current schema version (0 if the database has never been initialised)."""
    try:
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["value"]) if row else 0


def initialise(path: str | Path | None = None) -> Path:
    """Create or migrate the database to the latest schema. Safe to repeat."""
    db_path = Path(path or config.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = connect(db_path)
    try:
        apply_migrations(con)
    finally:
        con.close()
    return db_path


def apply_migrations(con: sqlite3.Connection) -> int:
    """Run every migration newer than the stored version, one transaction each."""
    current = schema_version(con)
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        with con:  # one transaction per migration: all or nothing
            for statement in MIGRATIONS[version]:
                con.execute(statement)
            _set_meta(con, "schema_version", str(version))
        current = version
    _ensure_defaults(con)
    return current


def _ensure_defaults(con: sqlite3.Connection) -> None:
    """Write meta/settings rows that should exist exactly once."""
    created = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    with con:
        con.execute(
            "INSERT INTO meta(key, value) VALUES('created_at', ?) "
            "ON CONFLICT(key) DO NOTHING",
            (created,),
        )
        # tracker_version reflects the code that is running now, so it is updated.
        con.execute(
            "INSERT INTO meta(key, value) VALUES('tracker_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (config.TRACKER_VERSION,),
        )
        for key, value in DEFAULT_SETTINGS.items():
            con.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
    con.commit()


# --- Small helpers used by other modules --------------------------
def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def get_setting(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def counts(con: sqlite3.Connection) -> dict[str, int]:
    """Row count per table, used by backups, audits and the dashboard."""
    tables = [
        "switches", "heartbeat", "morning", "evening", "checkin_log",
        "commits", "meetings", "prompts", "exit_survey",
    ]
    return {t: con.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}


def integrity_ok(con: sqlite3.Connection) -> bool:
    """True if SQLite's own integrity check reports 'ok'."""
    return con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


if __name__ == "__main__":  # create the real database: python -m src.db
    path = initialise()
    con = connect(path)
    print("initialised:", path)
    print("schema_version:", schema_version(con))
    print("integrity:", "ok" if integrity_ok(con) else "FAILED")
    print("counts:", counts(con))
    con.close()
