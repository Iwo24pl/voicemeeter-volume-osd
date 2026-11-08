"""Small, dependency-free Win32 helpers used across the app.

Only documented user-mode APIs are used.  Nothing here needs administrator
rights: layered windows, mutexes, monitor enumeration and HKCU writes all work
for a normal user process.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes as wt
from dataclasses import dataclass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Explicit prototypes: without them 64-bit handles are truncated to c_int.
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
user32.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p, ctypes.c_void_p, wt.LPARAM]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL

ERROR_ALREADY_EXISTS = 183

# --- DPI -------------------------------------------------------------------
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_dpi_awareness() -> None:
    """Make the process per-monitor DPI aware so the OSD is pixel-exact.

    Must run before any window is created.  Failures are harmless (very old
    Windows): the app then simply gets a scaled OSD.
    """
    try:
        user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def system_dpi() -> int:
    try:
        user32.GetDpiForSystem.restype = wt.UINT
        dpi = int(user32.GetDpiForSystem())
        if dpi > 0:
            return dpi
    except Exception:
        pass
    try:
        hdc = user32.GetDC(None)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi32.GetDeviceCaps.argtypes = [wt.HDC, ctypes.c_int]
        dpi = int(gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
        user32.ReleaseDC(None, hdc)
        return dpi or 96
    except Exception:
        return 96


# --- monitors --------------------------------------------------------------
class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wt.DWORD),
        ("szDevice", wt.WCHAR * 32),
    ]


MONITORINFOF_PRIMARY = 0x00000001
MONITORENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HANDLE, wt.HDC, ctypes.POINTER(RECT), wt.LPARAM)


@dataclass(frozen=True)
class Monitor:
    handle: int
    rect: tuple[int, int, int, int]      # virtual-screen coordinates
    work: tuple[int, int, int, int]      # excludes the taskbar
    primary: bool
    device: str

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def work_width(self) -> int:
        return self.work[2] - self.work[0]

    @property
    def work_height(self) -> int:
        return self.work[3] - self.work[1]


def monitors() -> list[Monitor]:
    """All monitors, primary first."""
    found: list[Monitor] = []

    def _callback(handle, _hdc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            found.append(
                Monitor(
                    handle=int(handle) if handle else 0,
                    rect=(info.rcMonitor.left, info.rcMonitor.top, info.rcMonitor.right, info.rcMonitor.bottom),
                    work=(info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom),
                    primary=bool(info.dwFlags & MONITORINFOF_PRIMARY),
                    device=info.szDevice,
                )
            )
        return True

    callback = MONITORENUMPROC(_callback)
    user32.EnumDisplayMonitors(None, None, callback, 0)
    found.sort(key=lambda m: (not m.primary, m.rect[0], m.rect[1]))
    return found


def cursor_position() -> tuple[int, int]:
    point = wt.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return int(point.x), int(point.y)


def monitor_for_point(x: int, y: int) -> Monitor | None:
    all_monitors = monitors()
    for monitor in all_monitors:
        left, top, right, bottom = monitor.rect
        if left <= x < right and top <= y < bottom:
            return monitor
    return all_monitors[0] if all_monitors else None


def primary_monitor() -> Monitor | None:
    all_monitors = monitors()
    for monitor in all_monitors:
        if monitor.primary:
            return monitor
    return all_monitors[0] if all_monitors else None


# --- single instance -------------------------------------------------------
class InstanceGuard:
    """Named mutex + event: second launch asks the first one to show settings."""

    def __init__(self, app_id: str):
        self._mutex = kernel32.CreateMutexW(None, False, f"Local\\{app_id}.mutex")
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS
        self._event = kernel32.CreateEventW(None, False, False, f"Local\\{app_id}.show")
        self._thread: threading.Thread | None = None

    def signal_existing(self) -> None:
        if self._event:
            kernel32.SetEvent(self._event)

    def start_listener(self, callback) -> None:
        """Call ``callback`` whenever another instance asks us to come forward."""
        if not self._event:
            return

        def _wait() -> None:
            # WaitForSingleObject on an auto-reset event: one wake per signal.
            while True:
                if kernel32.WaitForSingleObject(self._event, 0xFFFFFFFF) != 0:
                    return
                try:
                    callback()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_wait, name="instance-signal", daemon=True)
        self._thread.start()

    def close(self) -> None:
        for handle_name in ("_event", "_mutex"):
            handle = getattr(self, handle_name, None)
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
                setattr(self, handle_name, None)


# --- misc ------------------------------------------------------------------
def message_box(text: str, title: str = "VoiceMeeter Volume OSD", error: bool = False) -> None:
    """Last-resort dialog for failures before/without Tk."""
    try:
        flags = 0x00040000 if error else 0x00000040  # MB_ICONERROR : MB_ICONINFORMATION
        flags |= 0x00000000  # MB_OK
        user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


def user32_present() -> bool:
    return bool(user32)
