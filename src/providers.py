"""Sensors: the foreground process name and the seconds since last input.

The tracker's rules are tested on any machine, so the Windows imports sit
inside the functions that need them and only the sensor is swapped out in
tests.

Privacy: only the process image name is read. Window titles, file names, URLs
and page text are never touched.
"""

from __future__ import annotations

import sys
from typing import Protocol

import config

# Computed once: process-name comparison happens every second.
_LOCK_NAMES_LOWER = frozenset(name.lower() for name in config.LOCK_PROCESSES)


class Sensor(Protocol):
    """What the tracker loop needs from the operating system."""

    def foreground_process(self) -> str | None:
        """Name of the foreground process, or None if it cannot be read."""

    def idle_seconds(self) -> float:
        """Seconds since the last keyboard or mouse input."""


class WindowsSensor:
    """Real sensor for Windows (pywin32 + psutil).

    Both readings can fail for ordinary reasons: an elevated process denies
    access to its name, and there is sometimes no foreground window at all.
    Failures return None or 0 instead of raising, because a tracker that dies
    on one unreadable process is worse than one that logs __UNKNOWN__.
    """

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError(
                "WindowsSensor needs Windows; on other platforms use the tests' fake sensor"
            )
        import psutil  # imported here so this module stays importable everywhere
        import win32api
        import win32gui
        import win32process

        self._psutil = psutil
        self._win32api = win32api
        self._win32gui = win32gui
        self._win32process = win32process

    # ---- foreground window
    def foreground_process(self) -> str | None:
        """Process name owning the foreground window; None when unreadable.

        GetForegroundWindow also returns NULL while the desktop is switching
        or a full-screen app has focus on another virtual desktop, so None is
        a normal outcome rather than an error.
        """
        try:
            hwnd = self._win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            _thread_id, pid = self._win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return None
            name = self._psutil.Process(pid).name()
            return name or None
        except Exception:  # AccessDenied, NoSuchProcess, pywin32 error, race
            return None

    # ---- idle time
    def idle_seconds(self) -> float:
        """Seconds since the last user input, from GetLastInputInfo.

        GetLastInputInfo reports a 32-bit tick count that wraps every 49.7
        days; masking the subtraction keeps a long-uptime machine from
        reporting a multi-day idle period.
        """
        try:
            last_input = self._win32api.GetLastInputInfo()
            now_tick = self._win32api.GetTickCount()
            return ((now_tick - last_input) & 0xFFFFFFFF) / 1000.0
        except Exception:
            return 0.0


def default_sensor() -> Sensor:
    """The sensor for this platform, or a clear error on the wrong one."""
    if sys.platform == "win32":
        return WindowsSensor()
    raise RuntimeError(
        "no real sensor on this platform: the tracker only records on Windows. "
        "Run the test suite here instead (pytest uses a fake sensor)."
    )


def describe(process_name: str | None) -> str:
    """Map a raw process name to what gets written to switches.app (4A.3).

    Lock detection is case-insensitive: Windows reports `LockApp.exe`, but the
    casing is not a promise, so the comparison normalises it once at import.
    """
    if not process_name or not process_name.strip():
        return config.UNKNOWN
    name = process_name.strip()
    if name.lower() in _LOCK_NAMES_LOWER:
        return config.LOCKED
    return name
