"""Per-user autostart via HKCU\\...\\Run (no admin, no Task Scheduler)."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VoiceMeeterPopup"


def command_line() -> str:
    """Command that starts the app minimised in the tray."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    from .config import app_dir

    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    if windowless.exists():
        python = windowless
    script = app_dir() / "main.py"
    return f'"{python}" "{script}" --minimized'


def is_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(str(value).strip())
    except OSError:
        return False
    except ImportError:  # pragma: no cover
        return False


def set_enabled(enabled: bool) -> bool:
    """Create/remove the Run entry.  Returns True when the registry matches."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command_line())
            else:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
        return is_enabled() == bool(enabled)
    except OSError as exc:
        _LOGGER.warning("Could not update the Run key: %s", exc)
        return False
    except ImportError:  # pragma: no cover
        return False
