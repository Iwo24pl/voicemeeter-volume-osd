"""ctypes binding for Voicemeeter's official Remote API.

The DLL is the one shipped with every Voicemeeter installation
(``VoicemeeterRemote64.dll`` for 64-bit, ``VoicemeeterRemote.dll`` for 32-bit).
Nothing is injected into Voicemeeter and no file is modified: the DLL talks to
the running application through VB-Audio's shared-memory pipe, exactly like the
official SDK example ``vmr_osd``.

Relevant official functions used here:

    VBVMR_Login / VBVMR_Logout
    VBVMR_GetVoicemeeterType / VBVMR_GetVoicemeeterVersion
    VBVMR_IsParametersDirty
    VBVMR_GetParameterFloat
    VBVMR_GetParameterStringA
    VBVMR_RunVoicemeeter

Nothing writes a parameter: this app only observes Voicemeeter.

Return codes (from VoicemeeterRemote.h): 0 OK, 1 OK but not launched (Login),
-2 no server / not running, -3 unknown parameter.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import struct
import sys
from ctypes import wintypes as wt
from pathlib import Path

IS_64BIT = struct.calcsize("P") == 8
DLL_NAME = "VoicemeeterRemote64.dll" if IS_64BIT else "VoicemeeterRemote.dll"
OTHER_DLL_NAME = "VoicemeeterRemote.dll" if IS_64BIT else "VoicemeeterRemote64.dll"

KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ------------------------------------------------------------------ exceptions
class VoicemeeterError(RuntimeError):
    """Any non-zero Remote API result."""

    def __init__(self, code: int, function: str, param: str = ""):
        self.code = code
        self.function = function
        self.param = param
        detail = {
            -1: "communication error",
            -2: "Voicemeeter is not running",
            -3: "unknown parameter",
            -4: "out of range",
            -5: "structure mismatch",
        }.get(code, "error")
        suffix = f" ({param})" if param else ""
        super().__init__(f"{function} failed: {detail} [{code}]{suffix}")


class VoicemeeterNotRunning(VoicemeeterError):
    """The pipe exists but the Voicemeeter application is gone (-2)."""


class VoicemeeterDllNotFound(RuntimeError):
    """No usable VoicemeeterRemote DLL was found."""


# ------------------------------------------------------------------ discovery
def _registry_candidates() -> list[Path]:
    """Install directories advertised by the Voicemeeter uninstaller."""
    try:
        import winreg
    except ImportError:  # pragma: no cover - Windows only
        return []

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    found: list[Path] = []
    for hive, path in roots:
        try:
            with winreg.OpenKey(hive, path) as uninstall:
                count = winreg.QueryInfoKey(uninstall)[0]
                for index in range(count):
                    try:
                        name = winreg.EnumKey(uninstall, index)
                        with winreg.OpenKey(uninstall, name) as entry:
                            def value(key):
                                try:
                                    return winreg.QueryValueEx(entry, key)[0]
                                except OSError:
                                    return ""

                            display = str(value("DisplayName") or "")
                            if "voicemeeter" not in display.lower():
                                continue
                            location = str(value("InstallLocation") or "").strip()
                            if location:
                                found.append(Path(location))
                            icon = str(value("DisplayIcon") or "").strip()
                            if icon:
                                exe = Path(icon.split(",")[0].strip('"'))
                                found.append(exe.parent)
                            uninstaller = str(value("UninstallString") or "").strip()
                            if uninstaller:
                                candidate = Path(uninstaller.split(",")[0].strip('"'))
                                if candidate.suffix.lower() in (".exe", ".msi"):
                                    candidate = candidate.parent
                                found.append(candidate)
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _standard_dirs() -> list[Path]:
    dirs: list[Path] = []
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if not base:
            continue
        root = Path(base) / "VB"
        for name in ("Voicemeeter", "Voicemeeter Banana", "Voicemeeter Potato", "Voicemeeter8"):
            dirs.append(root / name)
    return dirs


def _usable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def find_remote_dll(explicit: str = "") -> tuple[Path | None, list[str]]:
    """Locate the Remote API DLL.

    Returns ``(path_or_None, notes)`` where notes explain every place that was
    searched, which the settings dialog shows when the connection fails.
    """
    notes: list[str] = []
    seen: set[str] = set()

    def consider(raw: str | Path, origin: str) -> Path | None:
        try:
            candidate = Path(raw).expanduser()
        except (TypeError, ValueError):
            return None
        if candidate.is_dir():
            candidate = candidate / DLL_NAME
        key = str(candidate).lower()
        if key in seen:
            return None
        seen.add(key)
        notes.append(f"checked {candidate} ({origin})")
        return candidate if _usable(candidate) else None

    if explicit:
        found = consider(explicit, "configured path")
        if found:
            return found, notes
        notes.append(f"configured path is not a file: {explicit}")

    env = os.environ.get("VOICEMEETER_REMOTE_DLL", "")
    if env:
        found = consider(env, "VOICEMEETER_REMOTE_DLL")
        if found:
            return found, notes

    from .config import app_dir  # local import avoids a config dependency at import time

    for folder in (app_dir(), app_dir() / "libs", app_dir() / "lib"):
        found = consider(folder, "next to the app")
        if found:
            return found, notes

    for folder in _registry_candidates():
        found = consider(folder, "registry")
        if found:
            return found, notes

    for folder in _standard_dirs():
        found = consider(folder, "standard install path")
        if found:
            return found, notes

    on_path = shutil.which(DLL_NAME)
    if on_path:
        found = consider(on_path, "PATH")
        if found:
            return found, notes

    other = shutil.which(OTHER_DLL_NAME)
    if other:
        notes.append(
            f"found {OTHER_DLL_NAME} but this Python is {'64' if IS_64BIT else '32'}-bit; "
            f"use a {'32' if IS_64BIT else '64'}-bit Python or install the matching Voicemeeter"
        )

    return None, notes


# ------------------------------------------------------------------ client
def version_string(raw: int) -> str:
    return ".".join(str((raw >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def type_name(type_id: int) -> str:
    return {1: "Voicemeeter", 2: "Voicemeeter Banana", 3: "Voicemeeter Potato",
            6: "Voicemeeter Potato (x64)"}.get(type_id, f"Voicemeeter (type {type_id})")


class VoicemeeterRemote:
    """Thin, thread-unsafe wrapper.  Use it from one thread only."""

    def __init__(self, dll_path: Path):
        self.dll_path = Path(dll_path)
        self._dll_dir_handle = None
        try:
            # Dependencies of the DLL live next to it; add_dll_directory lets
            # Windows resolve them when the DLL is loaded by absolute path.
            self._dll_dir_handle = os.add_dll_directory(str(self.dll_path.parent))
        except (AttributeError, OSError):
            self._dll_dir_handle = None
        try:
            self._dll = ctypes.WinDLL(str(self.dll_path))
        except OSError as exc:
            raise VoicemeeterDllNotFound(
                f"Could not load {self.dll_path.name}: {exc}. "
                f"Check that Voicemeeter is installed for {64 if IS_64BIT else 32}-bit Windows."
            ) from exc

        self._logged_in = False
        self._bind()

    def _bind(self) -> None:
        def fn(name: str, restype, argtypes):
            try:
                function = getattr(self._dll, name)
            except AttributeError:
                return None
            function.restype = restype
            function.argtypes = argtypes
            return function

        self._login = fn("VBVMR_Login", ctypes.c_long, [])
        self._logout = fn("VBVMR_Logout", ctypes.c_long, [])
        self._get_type = fn("VBVMR_GetVoicemeeterType", ctypes.c_long, [ctypes.POINTER(ctypes.c_long)])
        self._get_version = fn("VBVMR_GetVoicemeeterVersion", ctypes.c_long, [ctypes.POINTER(ctypes.c_long)])
        self._is_dirty = fn("VBVMR_IsParametersDirty", ctypes.c_long, [])
        self._get_float = fn("VBVMR_GetParameterFloat", ctypes.c_long,
                             [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)])
        self._get_string = fn("VBVMR_GetParameterStringA", ctypes.c_long, [ctypes.c_char_p, ctypes.c_char_p])
        self._run = fn("VBVMR_RunVoicemeeter", ctypes.c_long, [ctypes.c_long])

        # Old API compatibility: some builds require this before Login.
        try:
            set_hinstance = self._dll.VBVMR_SetHinstance
            set_hinstance.argtypes = [ctypes.c_void_p]
            set_hinstance.restype = None
            hinstance = KERNEL32.GetModuleHandleW(None)
            set_hinstance(ctypes.c_void_p(hinstance))
        except AttributeError:
            pass

    # -- lifecycle ---------------------------------------------------------
    def login(self) -> int:
        """Return Login's raw code: 0 = connected, 1 = VM not launched yet."""
        if self._logged_in:
            return 0
        code = int(self._login())
        if code == -2:
            # A previous process (or our own crashed retry) left a login behind.
            try:
                self._logout()
            except Exception:
                pass
            code = int(self._login())
        if code in (0, 1):
            self._logged_in = True
            return code
        raise VoicemeeterError(code, "VBVMR_Login")

    def logout(self) -> None:
        if self._logged_in:
            try:
                self._logout()
            except Exception:
                pass
        self._logged_in = False

    # -- information -------------------------------------------------------
    def type_id(self) -> int:
        value = ctypes.c_long()
        code = int(self._get_type(ctypes.byref(value)))
        if code == -2:
            raise VoicemeeterNotRunning(code, "VBVMR_GetVoicemeeterType")
        if code != 0:
            raise VoicemeeterError(code, "VBVMR_GetVoicemeeterType")
        return int(value.value)

    def version(self) -> str:
        value = ctypes.c_long()
        code = int(self._get_version(ctypes.byref(value)))
        if code != 0:
            return ""
        return version_string(int(value.value) & 0xFFFFFFFF)

    def is_dirty(self) -> int:
        """1 when Voicemeeter has new parameter values; -2 when it is gone."""
        if not self._is_dirty:
            return 0
        return int(self._is_dirty())

    # -- parameters --------------------------------------------------------
    def get_float(self, name: str) -> float:
        value = ctypes.c_float()
        code = int(self._get_float(name.encode("ascii"), ctypes.byref(value)))
        if code == -2:
            raise VoicemeeterNotRunning(code, "VBVMR_GetParameterFloat", name)
        if code != 0:
            raise VoicemeeterError(code, "VBVMR_GetParameterFloat", name)
        return float(value.value)

    def get_string(self, name: str, size: int = 512) -> str:
        buffer = ctypes.create_string_buffer(size)
        code = int(self._get_string(name.encode("ascii"), buffer))
        if code == -2:
            raise VoicemeeterNotRunning(code, "VBVMR_GetParameterStringA", name)
        if code != 0:
            raise VoicemeeterError(code, "VBVMR_GetParameterStringA", name)
        raw = buffer.value
        # Voicemeeter returns 8-bit strings in the system ANSI codepage.
        for encoding in ("mbcs", "utf-8", "latin-1"):
            try:
                return raw.decode(encoding).strip("\x00 ")
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("latin-1", "replace").strip("\x00 ")

    def launch(self, edition: int) -> None:
        """Start the Voicemeeter GUI (1 Basic, 2 Banana, 3 Potato, 6 Potato x64)."""
        if not self._run:
            raise VoicemeeterError(-1, "VBVMR_RunVoicemeeter")
        code = int(self._run(int(edition)))
        if code != 0:
            raise VoicemeeterError(code, "VBVMR_RunVoicemeeter")

    def close(self) -> None:
        self.logout()
        self._dll = None
        handle = self._dll_dir_handle
        self._dll_dir_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass


def load_remote(explicit_path: str = "") -> VoicemeeterRemote:
    path, notes = find_remote_dll(explicit_path)
    if path is None:
        detail = "\n".join(notes[-6:]) if notes else "no search paths available"
        raise VoicemeeterDllNotFound(
            f"{DLL_NAME} was not found. Install Voicemeeter, or point the app at "
            f"VoicemeeterRemote64.dll in Settings.\n\nSearched:\n{detail}"
        )
    return VoicemeeterRemote(path)
