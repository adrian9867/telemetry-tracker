"""Settings, paths and constants.

Every other module reads its thresholds from here, so a value only has to be
changed in one place. Anything marked ASSUMPTION is a default still to be
checked against the protocol, and any change gets an entry in DATA_LOG.md.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Paths --------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "telemetry.db"          # real data only, never synthetic
SYNTHETIC_DB_PATH = DATA_DIR / "synthetic.db"  # demo/team-view data only
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"
LOG_DIR = DATA_DIR / "logs"
DOCS_DIR = PROJECT_ROOT / "docs"
ENV_PATH = PROJECT_ROOT / ".env"

# --- Versions (5A.5 version freeze: bump these and log it in DATA_LOG.md) ----
SCHEMA_VERSION = 1        # bump only with a new migration in src/db.py
TRACKER_VERSION = "0.2.0"  # written into every switches row; bump + log in DATA_LOG.md

# --- Time rules (4A.1) --------------------------------------------
LOCAL_TZ = "Asia/Colombo"   # UTC+5:30, no daylight saving
DAY_START_HOUR = 5          # ASSUMPTION: local time before 05:00 belongs to the previous work day
LATE_NIGHT_START_HOUR = 22  # ASSUMPTION: 22:00 -> 05:00 counts as late night

# --- 4A.2 constants. Every value here is an assumption. -----------
POLL_SECONDS = 1                  # foreground-app poll interval
HEARTBEAT_SECONDS = 60            # heartbeat row interval
IDLE_SECONDS = 300                # inactivity before __IDLE__
GAP_THRESHOLD_SECONDS = 30         # loop delay that counts as sleep/hibernate
FRAGMENT_DWELL_SECONDS = 30        # dwell shorter than this is "fragmented"
WORK_START = "09:00"              # local working hours, used for uptime only
WORK_END = "18:00"
MORNING_WINDOW = ("05:00", "12:00")   # morning check-in due window
EVENING_START = "18:00"               # evening check-in becomes due
EVENING_REMIND_UNTIL = "23:00"        # stop reminding after this
NIGHTLY_TIME = "23:00"                # nightly backup / health check
BACK_TO_BACK_MINUTES = 10             # gap <= this between meetings = back-to-back
MAX_PROMPTS_PER_DAY = 3               # break-prompt cap (Tier 3)
MIN_GROUP_SIZE = 5                    # team view k-anonymity threshold
MIN_DAYS_DESCRIPTIVE = 14             # descriptive charts only before this
MIN_DAYS_ASSOCIATION = 21             # association cards after this many paired days
BOOTSTRAP_RESAMPLES = 2000            # confidence intervals
UPTIME_TARGET = 0.90                  # ASSUMPTION: >= 90% of work-hour minutes alive
UNKNOWN_TARGET = 0.05                 # ASSUMPTION: < 5% unknown-app share
HEARTBEAT_GAP_ALERT_SECONDS = 300     # 5 missed heartbeats => flag
BURN_IN_DAYS = 2                      # first N days of Phase 0 excluded (5A.9)

# --- Special tokens written to switches.app (4A.3) ----------------
IDLE = "__IDLE__"
LOCKED = "__LOCKED__"
UNKNOWN = "__UNKNOWN__"
GAP = "__GAP__"
SPECIAL_TOKENS = frozenset({IDLE, LOCKED, UNKNOWN, GAP})
SPECIAL_TOKEN_PREFIX = "__"

# TODO(M2): confirm these process names on the target Windows machine
# (Windows 10 vs 11 differ: LockApp.exe on 11, LogonUI.exe on 10).
LOCK_PROCESSES = frozenset({"LockApp.exe", "LogonUI.exe"})


def is_real_app(app: str) -> bool:
    """True for a genuine application name, False for a special token."""
    return not app.startswith(SPECIAL_TOKEN_PREFIX)


# --- Secrets: read from the environment, optionally seeded from .env ----
def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ (existing values win).

    No third-party dependency, and values are never printed or logged.
    """
    env_path = Path(path or ENV_PATH)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


load_env()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPOS = [r for r in os.environ.get("GITHUB_REPOS", "").split(",") if r]
GOOGLE_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "")
GOOGLE_TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "")

if __name__ == "__main__":  # quick check: python config.py
    for name in ("LOCAL_TZ", "DAY_START_HOUR", "POLL_SECONDS", "DB_PATH"):
        print(f"{name} = {globals()[name]}")
    print("special tokens:", sorted(SPECIAL_TOKENS))
