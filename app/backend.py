"""Voicemeeter backends — read-only.

``RealClient`` observes the running Voicemeeter through the official Remote API.
It deliberately exposes **no write functions**: this app only displays what
Voicemeeter is doing.  ``MockClient`` implements the same read interface with
in-memory values so the OSD, reconnect logic and UI can be exercised without
Voicemeeter, plus a ``bump`` helper used by the settings window's demo button.

Parameters used (documented in VoicemeeterRemote.h):

    Strip[i].Gain / Strip[i].Mute
    Bus[i].Gain   / Bus[i].Mute
    Strip[i].Label / Bus[i].Label      (read-only, for channel names)
    Strip[i].device.name / Bus[i].device.name
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config, ConnectionConfig
from .levels import clamp_db
from .vmapi import (
    VoicemeeterError,
    VoicemeeterNotRunning,
    VoicemeeterRemote,
    find_remote_dll,
    load_remote,
    type_name,
)

_LOGGER = logging.getLogger(__name__)

#: strip/bus counts per Voicemeeter type (see VoicemeeterRemote.h index tables).
STRIP_COUNTS = {1: 3, 2: 5, 3: 8, 6: 8}
BUS_COUNTS = {1: 2, 2: 5, 3: 8, 6: 8}

_BUS_LABELS = {
    1: ("A", "B"),
    2: ("A1", "A2", "A3", "B1", "B2"),
    3: ("A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3"),
    6: ("A1", "A2", "A3", "A4", "A5", "B1", "B2", "B3"),
}


def bus_default_label(type_id: int, index: int) -> str:
    labels = _BUS_LABELS.get(type_id, _BUS_LABELS[3])
    return labels[index] if 0 <= index < len(labels) else f"Bus {index + 1}"


# ------------------------------------------------------------------ data model
@dataclass(frozen=True)
class Channel:
    kind: str          # "strip" | "bus"
    index: int
    label: str         # user label from Voicemeeter, may be empty
    device: str        # device name, may be empty
    default_label: str

    @property
    def base(self) -> str:
        prefix = "Strip" if self.kind == "strip" else "Bus"
        return f"{prefix}[{self.index}]"

    @property
    def name(self) -> str:
        """Best human name: user label, then default, then device."""
        return self.label or self.default_label

    @property
    def display(self) -> str:
        if self.device and self.device.lower() != self.name.lower():
            return f"{self.name}  ·  {self.device}"
        return self.name


@dataclass(frozen=True)
class VmInfo:
    type_id: int
    version: str
    strips: tuple[Channel, ...]
    buses: tuple[Channel, ...]
    dll_path: str
    demo: bool = False

    @property
    def name(self) -> str:
        return type_name(self.type_id)

    @property
    def kicker(self) -> str:
        if self.demo:
            return "VOICEMEETER DEMO"
        return f"VOICEMEETER {self.name.replace('Voicemeeter ', '').upper()}"

    def channels(self) -> tuple[Channel, ...]:
        return self.strips + self.buses

    def channel(self, kind: str, index: int) -> Channel | None:
        pool = self.strips if kind == "strip" else self.buses
        if 0 <= index < len(pool):
            return pool[index]
        return None


def channel_device_short(device: str) -> str:
    """Drop the parenthetical driver detail from a device name.

    'Głośniki (Realtek High Definition Audio)' -> 'Głośniki'
    """
    if not device:
        return ""
    cut = device.find("(")
    if cut > 0:
        head = device[:cut].strip()
        if head:
            return head
    return device.strip()


@dataclass(frozen=True)
class ResolvedTarget:
    kind: str            # "strip" | "bus"
    index: int
    label: str           # e.g. "A1", user label, default
    device: str
    is_master: bool

    @property
    def base(self) -> str:
        prefix = "Strip" if self.kind == "strip" else "Bus"
        return f"{prefix}[{self.index}]"

    @property
    def gain_param(self) -> str:
        return f"{self.base}.Gain"

    @property
    def mute_param(self) -> str:
        return f"{self.base}.Mute"

    @property
    def osd_name(self) -> str:
        """Short, readable title for the OSD."""
        if self.is_master:
            head = f"Master · {self.label}"
        elif self.kind == "bus":
            head = f"Bus {self.label}"
        else:
            head = self.label
        device = channel_device_short(self.device)
        if device and device.lower() not in head.lower():
            head = f"{head} · {device}"
        return head

    @property
    def short_name(self) -> str:
        if self.is_master:
            return f"Master ({self.label})"
        if self.kind == "bus":
            return f"Bus {self.label}"
        return self.label

    @classmethod
    def from_channel(cls, channel: Channel, is_master: bool = False) -> "ResolvedTarget":
        return cls(channel.kind, channel.index, channel.name, channel.device, is_master)


def resolve_target(cfg: Config, info: VmInfo | None) -> ResolvedTarget:
    """Map the config selection onto a concrete strip/bus.

    When Voicemeeter is unavailable we still return a sensible target from the
    config alone, so the UI can render without a connection.
    """
    target_cfg = cfg.target
    kind = target_cfg.kind

    if info is None:
        if kind == "strip":
            return ResolvedTarget("strip", max(0, target_cfg.index),
                                  f"Strip {target_cfg.index + 1}", "", False)
        index = target_cfg.master_bus if kind == "master" else target_cfg.index
        return ResolvedTarget("bus", max(0, index), bus_default_label(6, index), "", kind == "master")

    if kind == "strip":
        channel = info.strips[min(max(0, target_cfg.index), len(info.strips) - 1)] if info.strips else None
        if channel is None:
            return ResolvedTarget("bus", 0, bus_default_label(info.type_id, 0), "", False)
        return ResolvedTarget.from_channel(channel, False)

    index = target_cfg.master_bus if kind == "master" else target_cfg.index
    channel = info.buses[min(max(0, index), len(info.buses) - 1)] if info.buses else None
    if channel is None:
        return ResolvedTarget("bus", 0, "A1", "", kind == "master")
    return ResolvedTarget.from_channel(channel, kind == "master")


# ------------------------------------------------------------------ real client
class RealClient:
    """One thread at a time: the controller serialises every call."""

    demo = False
    writable = False

    def __init__(self, cfg: ConnectionConfig):
        self._cfg = cfg
        self._remote: VoicemeeterRemote | None = None
        self._info: VmInfo | None = None
        self.dll_path = ""
        self.last_login_code: int | None = None
        self.dll_notes: list[str] = []

    # -- lifecycle ---------------------------------------------------------
    def apply_config(self, cfg: ConnectionConfig) -> None:
        if cfg.dll_path != self._cfg.dll_path and self._remote is not None:
            self.close()
        self._cfg = cfg

    def connect(self) -> VmInfo:
        if self._remote is None:
            _path, self.dll_notes = find_remote_dll(self._cfg.dll_path)
            self._remote = load_remote(self._cfg.dll_path)
            self.dll_path = str(self._remote.dll_path)
            _LOGGER.info("Loaded Remote API from %s", self.dll_path)
        self.last_login_code = self._remote.login()
        type_id = self._remote.type_id()  # raises VoicemeeterNotRunning (-2)
        self._info = self._build_info(type_id)
        return self._info

    def close(self) -> None:
        if self._remote is not None:
            try:
                self._remote.close()
            except Exception:
                pass
        self._remote = None
        self._info = None

    def launch(self, edition: int) -> None:
        if self._remote is None:
            self._remote = load_remote(self._cfg.dll_path)
            self.dll_path = str(self._remote.dll_path)
            self._remote.login()
        self._remote.launch(edition)

    # -- read-only parameters ---------------------------------------------
    def read_gain(self, target: ResolvedTarget) -> float:
        assert self._remote is not None
        return clamp_db(self._remote.get_float(target.gain_param))

    def read_mute(self, target: ResolvedTarget) -> bool:
        assert self._remote is not None
        return self._remote.get_float(target.mute_param) >= 0.5

    def poll_dirty(self) -> bool:
        """True when Voicemeeter has fresh parameter values.

        Raises ``VoicemeeterNotRunning`` when the application has gone away.
        """
        if self._remote is None:
            return False
        code = self._remote.is_dirty()
        if code == -2:
            raise VoicemeeterNotRunning(code, "VBVMR_IsParametersDirty")
        return code == 1

    # -- metadata ----------------------------------------------------------
    def _safe_string(self, name: str) -> str:
        assert self._remote is not None
        try:
            return self._remote.get_string(name)
        except VoicemeeterNotRunning:
            raise
        except VoicemeeterError:
            # -3 (parameter not present in this edition) / -1 (empty) are normal.
            return ""

    def _build_info(self, type_id: int) -> VmInfo:
        if type_id not in STRIP_COUNTS:
            raise VoicemeeterError(-1, "VBVMR_GetVoicemeeterType", f"unknown type {type_id}")
        strips = tuple(
            Channel(
                kind="strip",
                index=index,
                label=self._safe_string(f"Strip[{index}].Label"),
                device=self._safe_string(f"Strip[{index}].device.name"),
                default_label=f"Strip {index + 1}",
            )
            for index in range(STRIP_COUNTS[type_id])
        )
        buses = tuple(
            Channel(
                kind="bus",
                index=index,
                label=self._safe_string(f"Bus[{index}].Label"),
                device=self._safe_string(f"Bus[{index}].device.name"),
                default_label=bus_default_label(type_id, index),
            )
            for index in range(BUS_COUNTS[type_id])
        )
        version = ""
        try:
            version = self._remote.version() if self._remote else ""
        except Exception:
            pass
        return VmInfo(type_id, version, strips, buses, self.dll_path, demo=False)


# ------------------------------------------------------------------ mock client
class MockClient:
    """Deterministic fake used for demos, screenshots and headless tests."""

    demo = True
    writable = False

    def __init__(self, cfg: ConnectionConfig):
        self._cfg = cfg
        self._available = not cfg.simulate_unavailable
        self._gains: dict[tuple[str, int], float] = {
            ("bus", 0): -6.0, ("bus", 1): -3.0, ("bus", 2): 0.0, ("bus", 3): -12.0, ("bus", 4): 0.0,
            ("strip", 0): -1.5, ("strip", 1): -9.0, ("strip", 2): 0.0, ("strip", 3): -2.0, ("strip", 4): -2.0,
        }
        self._mutes: dict[tuple[str, int], bool] = {}
        self._demo_step = 0
        self.dll_path = "(demo backend)"
        self.last_login_code = 0
        self.dll_notes = ["Demo backend: no Voicemeeter connection is used"]

    def apply_config(self, cfg: ConnectionConfig) -> None:
        self._cfg = cfg
        self._available = not cfg.simulate_unavailable

    def connect(self) -> VmInfo:
        if not self._available:
            raise VoicemeeterNotRunning(-2, "VBVMR_GetVoicemeeterType")
        return self._info()

    def close(self) -> None:
        pass

    def launch(self, edition: int) -> None:
        self._available = True

    def _info(self) -> VmInfo:
        strip_names = ("Mic1", "Music", "Game", "Computer", "Comms")
        strip_devices = ("Wireless Microphone", "", "", "", "")
        bus_names = ("Głośniki (Realtek)", "Headphones", "Speakers 2", "Voicemeeter Out B1", "Voicemeeter Out B2")
        strips = tuple(
            Channel("strip", i, strip_names[i], strip_devices[i], f"Strip {i + 1}") for i in range(5)
        )
        buses = tuple(
            Channel("bus", i, "", bus_names[i], bus_default_label(2, i)) for i in range(5)
        )
        return VmInfo(2, "3.0.3.4 (demo)", strips, buses, "(demo backend)", demo=True)

    # -- read-only parameters ---------------------------------------------
    def _check(self) -> None:
        if not self._available:
            raise VoicemeeterNotRunning(-2, "VBVMR_GetParameterFloat")

    def read_gain(self, target: ResolvedTarget) -> float:
        self._check()
        return clamp_db(self._gains.get((target.kind, target.index), 0.0))

    def read_mute(self, target: ResolvedTarget) -> bool:
        self._check()
        return self._mutes.get((target.kind, target.index), False)

    def poll_dirty(self) -> bool:
        self._check()
        return True

    # -- demo only ---------------------------------------------------------
    def bump(self) -> None:
        """Change a value so the polling pipeline has something to report."""
        self._check()
        self._demo_step += 1
        key = ("bus", 0) if self._demo_step % 3 else ("strip", 1)
        current = self._gains.get(key, 0.0)
        if self._demo_step % 4 == 0:
            self._mutes[key] = not self._mutes.get(key, False)
        else:
            nxt = current + 3.0
            if nxt > 6.0:
                nxt = -12.0
            self._gains[key] = clamp_db(nxt)


def make_client(cfg: Config):
    """Pick the backend for the current settings (demo or real)."""
    return MockClient(cfg.connection) if cfg.connection.demo_backend else RealClient(cfg.connection)


__all__ = [
    "Channel",
    "MockClient",
    "RealClient",
    "ResolvedTarget",
    "VmInfo",
    "bus_default_label",
    "make_client",
    "resolve_target",
]
