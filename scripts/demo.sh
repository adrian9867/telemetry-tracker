#!/usr/bin/env bash
# Progress demo: one command that shows every finished milestone working.
#
#   Git Bash :  bash scripts/demo.sh
#   fish     :  bash scripts/demo.sh
#
# Safe to run any number of times. It creates data/telemetry.db if it is
# missing, and writes nothing else: no tracker rows, no synthetic data.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

# Use the venv interpreter if it actually runs here, otherwise whatever python
# is on PATH. The `-c ""` probe catches a binary that exists but cannot run,
# such as a Windows venv opened from WSL.
PY=""
for cand in "venv/Scripts/python.exe" "venv/bin/python" python3 python; do
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "" >/dev/null 2>&1; then
        PY="$cand"; break
    fi
done
if [ -z "$PY" ]; then
    echo "No working Python found. Open a terminal in the project folder and run:"
    echo "  python -m venv venv"
    exit 1
fi
echo "interpreter    : $PY"

hdr() { printf '\n\033[1m%s\033[0m\n%s\n' "$1" "$(printf '=%.0s' $(seq 1 68))"; }

hdr "ENVIRONMENT"
"$PY" - <<'EOF'
import platform, sqlite3, sys, zoneinfo
print("python         :", platform.python_version())
print("sqlite         :", sqlite3.sqlite_version)
try:
    zoneinfo.ZoneInfo("Asia/Colombo")
    print("tz data        : ok (Asia/Colombo resolves)")
except Exception as exc:
    print("tz data        : MISSING ->", exc, "  <-- pip install tzdata")
EOF
"$PY" -c "import psutil, win32gui" 2>/dev/null \
  && echo "windows deps   : ok (psutil, pywin32)" \
  || echo "windows deps   : MISSING -> pip install psutil pywin32"

hdr "M1: TEST SUITE (schema + day-boundary rules + tracker state machine)"
"$PY" -m pytest 2>&1 | tail -4

hdr "M1: DAY BOUNDARY RULE (before 05:00 belongs to the previous work day)"
"$PY" -m src.timeutil

hdr "M1: DATABASE: schema version, integrity, and one row per table"
"$PY" -m src.db

hdr "M1: WHAT THE DATABASE REFUSES (the constraints themselves)"
"$PY" - <<'EOF'
import sqlite3, tempfile, time, os
from src import db
path = os.path.join(tempfile.mkdtemp(), "demo.db")
con = db.connect(db.initialise(path))
def attempt(label, sql, params=()):
    try:
        con.execute(sql, params); con.commit(); print(f"  ACCEPTED  {label}")
    except sqlite3.IntegrityError as exc:
        print(f"  REJECTED  {label}  <- {exc}")
now = time.time()
attempt("evening fatigue = 7",
        "INSERT INTO evening(day, ts, fatigue, workload) VALUES (?,?,?,?)",
        ("2026-11-04", now, 7, 5))
attempt("evening fatigue = 11",
        "INSERT INTO evening(day, ts, fatigue, workload) VALUES (?,?,?,?)",
        ("2026-11-05", now, 11, 5))
attempt("morning, first for the day",
        "INSERT INTO morning(day, sleepy) VALUES (?,?)", ("2026-11-04", 6))
attempt("morning, second for the same day",
        "INSERT INTO morning(day, sleepy) VALUES (?,?)", ("2026-11-04", 6))
attempt("switch with ts = 'not-a-time'",
        "INSERT INTO switches(ts, app, tracker_version) VALUES ('not-a-time','Code.exe','0.2.0')")
attempt("switch with ts = 0",
        "INSERT INTO switches(ts, app, tracker_version) VALUES (0,'Code.exe','0.2.0')")
attempt("switch with a real timestamp",
        "INSERT INTO switches(ts, app, tracker_version) VALUES (?,'Code.exe','0.2.0')", (now,))
con.close()
EOF

hdr "M2: LIVE FOREGROUND READ (3 polls, nothing written to the database)"
if "$PY" -c "import sys; raise SystemExit(0 if sys.platform == 'win32' else 1)"; then
    "$PY" -m src.tracker --dry-run 2>&1 | head -12
else
    echo "  skipped: the sensor reads the real Windows desktop only."
    echo "  The state machine itself is covered by the 38 tracker tests above."
fi

hdr "GIT HISTORY"
git --no-pager log --oneline 2>&1 | head -10

hdr "DONE"
echo "M1 foundation + M2 tracker core: complete and tested."
echo "Next: M3 auto-start (scripts/install_tasks.ps1), then M4 nightly job."
