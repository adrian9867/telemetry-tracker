import sqlite3
from pathlib import Path

DB_PATH = Path("data/telemetry.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS switches (
    ts  REAL NOT NULL CHECK (ts > 0),
    app TEXT NOT NULL CHECK (length(app) > 0)
) STRICT;
CREATE TABLE IF NOT EXISTS heartbeat (
    ts REAL NOT NULL CHECK (ts > 0)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_switches_ts  ON switches(ts);
CREATE INDEX IF NOT EXISTS idx_heartbeat_ts ON heartbeat(ts);
"""

def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con

def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)