"""Configuration model, validation and persistence.

This app is a *monitor*: it never writes to Voicemeeter and never intercepts
keyboard input.  The configuration therefore describes what to watch, how the
popup looks, and how to reach the Remote API.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

APP_NAME = "VoiceMeeterPopup"
CONFIG_VERSION = 2

#: Nine anchor points for the OSD, in reading order.
POSITIONS = (
    "top-left", "top-center", "top-right",
    "middle-left", "middle-center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
)

TARGET_KINDS = ("master", "bus", "strip")
MONITORS = ("cursor", "primary")
WATCH_MODES = ("any", "target")

_LOGGER = logging.getLogger(__name__)


# --------------------------------------------------------------------------- paths
def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory that holds ``main.py`` (or the built .exe)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def asset_path(name: str) -> Path:
    """Path to a bundled asset, also inside a PyInstaller one-file bundle."""
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base / "assets" / name


def data_dir() -> Path:
    """Writable per-user directory for config and logs (no admin needed)."""
    override = os.environ.get("VOICEMEETER_POPUP_HOME")
    if override:
        path = Path(override).expanduser()
    else:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            path = Path(base) / APP_NAME
        else:  # unusual: no shell folders available
            path = Path.home() / ".config" / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path() -> Path:
    return data_dir() / "config.json"


def log_path() -> Path:
    return data_dir() / "app.log"


# --------------------------------------------------------------------------- clamp helpers
def _clamp(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:  # NaN
        return float(default)
    return max(low, min(high, number))


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _choice(value: Any, options: tuple[str, ...], default: str) -> str:
    return value if value in options else default


def _text(value: Any, default: str = "", limit: int = 400) -> str:
    if isinstance(value, str):
        return value.strip()[:limit]
    return default


# --------------------------------------------------------------------------- sections
@dataclass
class TargetConfig:
    """Which Voicemeeter fader is highlighted / watched in target mode."""

    kind: str = "master"          # master | bus | strip
    index: int = 0                # bus/strip index (ignored for master)
    master_bus: int = 0           # bus index used when kind == "master"

    def normalized(self) -> "TargetConfig":
        return TargetConfig(
            kind=_choice(self.kind, TARGET_KINDS, "master"),
            index=int(_clamp(self.index, 0, 0, 63)),
            master_bus=int(_clamp(self.master_bus, 0, 0, 7)),
        )


@dataclass
class MonitorConfig:
    """What triggers the popup and how often Voicemeeter is polled."""

    mode: str = "any"             # any | target
    poll_ms: int = 50             # 25 .. 250 ms
    show_mute: bool = True
    show_on_target_change: bool = True

    def normalized(self) -> "MonitorConfig":
        return MonitorConfig(
            mode=_choice(self.mode, WATCH_MODES, "any"),
            poll_ms=int(_clamp(self.poll_ms, 50, 25, 250)),
            show_mute=_bool(self.show_mute, True),
            show_on_target_change=_bool(self.show_on_target_change, True),
        )


@dataclass
class OsdConfig:
    enabled: bool = True
    position: str = "bottom-center"
    offset_x: int = 0
    offset_y: int = 0
    scale: float = 1.0                    # 0.6 .. 2.0
    opacity: float = 0.96                 # 0.25 .. 1.0
    hold_seconds: float = 1.8             # 0.3 .. 15
    fade_ms: int = 220                    # 0 .. 1200
    show_db: bool = True
    monitor: str = "cursor"               # cursor | primary
    full_scale_db: float = 0.0            # gain that reads 100 %

    def normalized(self) -> "OsdConfig":
        return OsdConfig(
            enabled=_bool(self.enabled, True),
            position=_choice(self.position, POSITIONS, "bottom-center"),
            offset_x=int(_clamp(self.offset_x, 0, -2000, 2000)),
            offset_y=int(_clamp(self.offset_y, 0, -2000, 2000)),
            scale=round(_clamp(self.scale, 1.0, 0.6, 2.0), 2),
            opacity=round(_clamp(self.opacity, 0.96, 0.25, 1.0), 2),
            hold_seconds=round(_clamp(self.hold_seconds, 1.8, 0.3, 15.0), 2),
            fade_ms=int(_clamp(self.fade_ms, 220, 0, 1200)),
            show_db=_bool(self.show_db, True),
            monitor=_choice(self.monitor, MONITORS, "cursor"),
            full_scale_db=round(_clamp(self.full_scale_db, 0.0, -40.0, 12.0), 1),
        )


@dataclass
class ConnectionConfig:
    dll_path: str = ""                    # empty = auto-detect
    demo_backend: bool = False
    simulate_unavailable: bool = False    # demo only: exercise reconnect UI
    launch_edition: int = 2               # 1 Basic, 2 Banana, 3 Potato, 6 Potato x64

    def normalized(self) -> "ConnectionConfig":
        try:
            edition = int(float(self.launch_edition))
        except (TypeError, ValueError):
            edition = 2
        if edition not in (1, 2, 3, 6):
            edition = 2
        return ConnectionConfig(
            dll_path=_text(self.dll_path, "", 400),
            demo_backend=_bool(self.demo_backend, False),
            simulate_unavailable=_bool(self.simulate_unavailable, False),
            launch_edition=edition,
        )


@dataclass
class GeneralConfig:
    start_with_windows: bool = False
    start_minimized: bool = True

    def normalized(self) -> "GeneralConfig":
        return GeneralConfig(
            start_with_windows=_bool(self.start_with_windows, False),
            start_minimized=_bool(self.start_minimized, True),
        )


# --------------------------------------------------------------------------- root
@dataclass
class Config:
    version: int = CONFIG_VERSION
    target: TargetConfig = field(default_factory=TargetConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    osd: OsdConfig = field(default_factory=OsdConfig)
    connection: ConnectionConfig = field(default_factory=ConnectionConfig)
    general: GeneralConfig = field(default_factory=GeneralConfig)

    # -- construction ------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Any) -> "Config":
        """Build a config from JSON, tolerating missing/unknown/wrong values.

        Older configs (which had a ``keys`` section and ``step_db``) load fine:
        unknown keys are ignored.
        """
        if not isinstance(raw, dict):
            raw = {}

        def section(name: str, dc):
            value = raw.get(name)
            if not isinstance(value, dict):
                return {}
            allowed = {f.name for f in fields(dc)}
            return {k: v for k, v in value.items() if k in allowed}

        target_raw = raw.get("target") if isinstance(raw.get("target"), dict) else {}
        cfg = cls(
            version=CONFIG_VERSION,
            target=TargetConfig(
                kind=target_raw.get("kind", "master"),
                index=target_raw.get("index", 0),
                master_bus=target_raw.get("master_bus", 0),
            ),
            monitor=MonitorConfig(**section("monitor", MonitorConfig)),
            osd=OsdConfig(**section("osd", OsdConfig)),
            connection=ConnectionConfig(**section("connection", ConnectionConfig)),
            general=GeneralConfig(**section("general", GeneralConfig)),
        )
        return cfg.normalized()

    def normalized(self) -> "Config":
        return Config(
            version=CONFIG_VERSION,
            target=self.target.normalized(),
            monitor=self.monitor.normalized(),
            osd=self.osd.normalized(),
            connection=self.connection.normalized(),
            general=self.general.normalized(),
        )

    def replace(self, **changes: Any) -> "Config":
        """Return a validated copy with top-level fields replaced."""
        data = self.to_dict()
        data.update(changes)
        return Config.from_dict(data)

    def with_section(self, name: str, **changes: Any) -> "Config":
        """Return a validated copy with some values of one section changed."""
        data = self.to_dict()
        if name not in data:
            raise KeyError(name)
        data[name].update(changes)
        return Config.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "target": {
                "kind": self.target.kind,
                "index": self.target.index,
                "master_bus": self.target.master_bus,
            },
            "monitor": {
                "mode": self.monitor.mode,
                "poll_ms": self.monitor.poll_ms,
                "show_mute": self.monitor.show_mute,
                "show_on_target_change": self.monitor.show_on_target_change,
            },
            "osd": {
                "enabled": self.osd.enabled,
                "position": self.osd.position,
                "offset_x": self.osd.offset_x,
                "offset_y": self.osd.offset_y,
                "scale": self.osd.scale,
                "opacity": self.osd.opacity,
                "hold_seconds": self.osd.hold_seconds,
                "fade_ms": self.osd.fade_ms,
                "show_db": self.osd.show_db,
                "monitor": self.osd.monitor,
                "full_scale_db": self.osd.full_scale_db,
            },
            "connection": {
                "dll_path": self.connection.dll_path,
                "demo_backend": self.connection.demo_backend,
                "simulate_unavailable": self.connection.simulate_unavailable,
                "launch_edition": self.connection.launch_edition,
            },
            "general": {
                "start_with_windows": self.general.start_with_windows,
                "start_minimized": self.general.start_minimized,
            },
        }

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return cls()
        except (OSError, ValueError) as exc:
            _LOGGER.warning("Could not read %s (%s); using defaults", path, exc)
            return cls()
        return cls.from_dict(raw)

    def save(self, path: Path | None = None) -> bool:
        """Atomically write the config.  Never raises: a failure must not kill the app."""
        path = path or config_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                os.replace(tmp_name, path)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
            return True
        except OSError as exc:
            _LOGGER.warning("Could not save config to %s: %s", path, exc)
            return False
