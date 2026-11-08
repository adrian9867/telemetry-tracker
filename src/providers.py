import psutil
import win32api
import win32gui
import win32process


def foreground_process() -> str | None:
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if not pid:
            return None
        return psutil.Process(pid).name() or None
    except Exception:
        return None


def idle_seconds() -> float:
    try:
        last_input = win32api.GetLastInputInfo()
        now_tick = win32api.GetTickCount()
        return ((now_tick - last_input) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return 0.0


if __name__ == "__main__":
    import time

    for _ in range(15):
        print(foreground_process(), f"idle={idle_seconds():.1f}s")
        time.sleep(1)