"""Controller: watches Voicemeeter on one worker thread.

This is a passive monitor.  It never writes a parameter and never touches the
keyboard.  Every ``poll_ms`` it asks Voicemeeter whether parameters changed
(``VBVMR_IsParametersDirty``); when they did, it reads the watched gains/mutes
(``VBVMR_GetParameterFloat``), compares them with the previous snapshot and
posts a ``level`` event for the fader that moved.

All Remote API calls happen on this thread, as the API documentation requires
for IsParametersDirty / GetLevel.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

from .backend import (
    MockClient,
    ResolvedTarget,
    VmInfo,
    make_client,
    resolve_target,
)
from .config import Config
from .levels import clamp_db, display_percent
from .vmapi import VoicemeeterDllNotFound, VoicemeeterError, VoicemeeterNotRunning

_LOGGER = logging.getLogger(__name__)

#: A gain change smaller than this is treated as noise (the GUI itself uses 0.1 dB steps).
CHANGE_EPSILON_DB = 0.05


@dataclass(frozen=True)
class State:
    connected: bool = False
    status: str = "Starting…"
    detail: str = ""
    info: VmInfo | None = None
    target: ResolvedTarget | None = None
    gain: float = 0.0
    muted: bool = False
    version: int = 0


@dataclass(frozen=True)
class Event:
    kind: str                     # state | level | test | error
    data: dict[str, Any]


class Controller:
    def __init__(self, cfg: Config, events: "queue.Queue[Event]", logger: logging.Logger | None = None,
                 client=None):
        self._cfg = cfg
        self._events = events
        self._log = logger or _LOGGER
        self._commands: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._state = State()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client = client if client is not None else make_client(cfg)
        self._retry_at = 0.0
        self._next_poll = 0.0
        # (kind, index) -> (gain, muted) as last reported by Voicemeeter
        self._values: dict[tuple[str, int], tuple[float, bool]] = {}
        self._settle_until = 0.0

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> State:
        return self._state

    @property
    def config(self) -> Config:
        return self._cfg

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="voicemeeter-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._commands.put(("__stop__", None))
        if self._thread:
            self._thread.join(3.0)
        try:
            self._client.close()
        except Exception:
            pass

    def refresh(self) -> None:
        """Force an immediate poll (used after changing the target)."""
        self._commands.put(("refresh", None))

    def reconnect(self) -> None:
        self._commands.put(("reconnect", None))

    def apply_config(self, cfg: Config) -> None:
        self._commands.put(("config", cfg))

    def test_connection(self) -> None:
        self._commands.put(("test", None))

    def launch(self, edition: int) -> None:
        self._commands.put(("launch", int(edition)))

    def demo_bump(self) -> None:
        """Demo backend only: change a value so the poll pipeline reports it."""
        self._commands.put(("demo-bump", None))

    # ------------------------------------------------------------- worker
    def _run(self) -> None:
        self._log.info("Monitor started (demo=%s, mode=%s)", self._cfg.connection.demo_backend,
                       self._cfg.monitor.mode)
        while not self._stop.is_set():
            try:
                command, payload = self._commands.get(timeout=0.02)
            except queue.Empty:
                command, payload = "", None

            if command == "__stop__":
                break
            if command:
                try:
                    self._handle(command, payload)
                except Exception:
                    self._log.exception("Unhandled controller command %r", command)

            now = time.monotonic()
            if not self._state.connected:
                if now >= self._retry_at:
                    self._connect()
            elif now >= self._next_poll:
                self._next_poll = now + self._cfg.monitor.poll_ms / 1000.0
                self._poll()
        try:
            self._client.close()
        except Exception:
            pass
        self._log.info("Monitor stopped")

    def _handle(self, command: str, payload: Any) -> None:
        if command == "refresh":
            self._next_poll = 0.0
            self._initialize_snapshot()
        elif command == "reconnect":
            self._force_reconnect()
        elif command == "config":
            self._apply_config(payload)
        elif command == "test":
            self._do_test()
        elif command == "launch":
            self._do_launch(int(payload))
        elif command == "demo-bump":
            if isinstance(self._client, MockClient):
                try:
                    self._client.bump()
                except VoicemeeterNotRunning:
                    pass
                self._next_poll = 0.0

    # ------------------------------------------------------------- polling
    def _watched_targets(self) -> list[ResolvedTarget]:
        if not self._state.connected or self._state.info is None:
            return []
        info = self._state.info
        if self._cfg.monitor.mode == "target":
            target = resolve_target(self._cfg, info)
            return [target]

        master = self._cfg.target
        targets = []
        for channel in info.channels():
            is_master = (
                channel.kind == "bus"
                and master.kind == "master"
                and channel.index == master.master_bus
            )
            targets.append(ResolvedTarget.from_channel(channel, is_master))
        return targets

    def _poll(self) -> None:
        try:
            if not self._client.poll_dirty():
                return
        except VoicemeeterNotRunning:
            self._mark_disconnected()
            return
        except VoicemeeterError as exc:
            self._log.debug("IsParametersDirty failed: %s", exc)
            return

        changed: list[tuple[float, bool, ResolvedTarget, float, bool]] = []
        for target in self._watched_targets():
            key = (target.kind, target.index)
            try:
                gain = self._client.read_gain(target)
                muted = self._client.read_mute(target) if self._cfg.monitor.show_mute else False
            except VoicemeeterNotRunning:
                self._mark_disconnected()
                return
            except VoicemeeterError as exc:
                # A single unreadable parameter must not stop the whole monitor.
                self._log.debug("Could not read %s: %s", target.base, exc)
                continue

            previous = self._values.get(key)
            self._values[key] = (gain, muted)
            if previous is None:
                continue  # first reading establishes the baseline, no popup
            delta = abs(gain - previous[0])
            mute_changed = muted != previous[1]
            if delta >= CHANGE_EPSILON_DB or mute_changed:
                changed.append((delta, mute_changed, target, gain, muted))

        if not changed:
            self._sync_state_from_values()
            return

        if len(changed) > 3:
            # Loading a Voicemeeter preset changes many faders at once; that is not
            # somebody turning a knob, so just rebase the snapshot.
            self._log.debug("Bulk parameter change (%d channels); rebasing", len(changed))
            self._sync_state_from_values()
            return

        changed.sort(key=lambda item: (item[0], int(item[1])), reverse=True)
        _delta, _mute_changed, target, gain, muted = changed[0]
        if time.monotonic() < self._settle_until:
            # The first reads right after connecting can still be the tail of the
            # previous cache.  Take them as the baseline instead of popping up.
            self._sync_state_from_values()
            return
        self._log.debug("Change: %s %.1f dB muted=%s", target.base, gain, muted)
        self._update_state_for(target, gain, muted)
        self._emit(Event("level", {
            "target": target,
            "gain": gain,
            "muted": muted,
            "percent": display_percent(gain, self._cfg.osd.full_scale_db),
            "info": self._state.info,
            "source": "change",
        }))

    def _initialize_snapshot(self) -> None:
        """Read every watched parameter without emitting events."""
        self._values.clear()
        for target in self._watched_targets():
            try:
                gain = self._client.read_gain(target)
                muted = self._client.read_mute(target) if self._cfg.monitor.show_mute else False
            except (VoicemeeterError, VoicemeeterNotRunning):
                continue
            self._values[(target.kind, target.index)] = (gain, muted)
        self._sync_state_from_values()

    def _sync_state_from_values(self) -> None:
        """Keep the displayed selected level in sync when only it changed."""
        target = self._state.target
        if target is None:
            return
        value = self._values.get((target.kind, target.index))
        if value is None:
            return
        gain, muted = value
        if abs(gain - self._state.gain) >= 0.005 or muted != self._state.muted:
            self._state = replace(self._state, gain=gain, muted=muted)
            self._emit(Event("state", {"state": self._state}))

    def _update_state_for(self, target: ResolvedTarget, gain: float, muted: bool) -> None:
        selected = self._state.target
        if selected is not None and (selected.kind, selected.index) != (target.kind, target.index):
            return
        if abs(gain - self._state.gain) < 0.005 and muted == self._state.muted:
            return
        self._state = replace(self._state, gain=gain, muted=muted)
        self._emit(Event("state", {"state": self._state}))

    # ------------------------------------------------------------- connection
    def _connect(self) -> None:
        try:
            info = self._client.connect()
        except VoicemeeterDllNotFound as exc:
            self._log.error("Remote API unavailable: %s", exc)
            self._retry_at = time.monotonic() + 6.0
            self._set_disconnected_state(
                "Voicemeeter Remote API not found",
                "Install Voicemeeter or set the DLL path in Settings → Connection.",
                str(exc),
            )
            return
        except VoicemeeterNotRunning:
            self._retry_at = time.monotonic() + 1.2
            self._set_disconnected_state(
                "Voicemeeter is not running",
                "Waiting for Voicemeeter to start…",
                self._client_notes(),
            )
            return
        except VoicemeeterError as exc:
            self._log.warning("Voicemeeter connect failed: %s", exc)
            self._retry_at = time.monotonic() + 2.0
            self._set_disconnected_state("Cannot talk to Voicemeeter", str(exc), self._client_notes())
            return

        target = resolve_target(self._cfg, info)
        gain, muted = 0.0, False
        try:
            gain = self._client.read_gain(target)
            muted = self._client.read_mute(target) if self._cfg.monitor.show_mute else False
        except VoicemeeterNotRunning:
            self._retry_at = time.monotonic() + 1.2
            self._set_disconnected_state("Voicemeeter is not running", "Waiting for Voicemeeter to start…",
                                         self._client_notes())
            return
        except VoicemeeterError as exc:
            self._log.warning("Could not read target: %s", exc)

        self._state = State(
            connected=True,
            status="Connected",
            detail=f"{info.name}{' ' + info.version if info.version else ''}",
            info=info,
            target=target,
            gain=gain,
            muted=muted,
            version=self._state.version + 1,
        )
        self._log.info("Connected to %s (%s); watching %s", info.name, info.version,
                       "any fader" if self._cfg.monitor.mode == "any" else target.base)
        self._emit(Event("state", {"state": self._state}))
        self._initialize_snapshot()
        self._settle_until = time.monotonic() + 0.6
        self._next_poll = time.monotonic() + self._cfg.monitor.poll_ms / 1000.0

    def _force_reconnect(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
        self._values.clear()
        self._state = replace(self._state, connected=False, info=None,
                              status="Reconnecting…", detail="")
        self._emit(Event("state", {"state": self._state}))
        self._retry_at = 0.0

    def _mark_disconnected(self) -> None:
        self._log.warning("Lost the Voicemeeter connection")
        try:
            self._client.close()
        except Exception:
            pass
        self._values.clear()
        self._retry_at = time.monotonic() + 1.2
        self._set_disconnected_state(
            "Voicemeeter is not running",
            "The connection was lost. Watching for Voicemeeter to come back…",
            self._client_notes(),
        )

    def _set_disconnected_state(self, status: str, detail: str, extra: str = "") -> None:
        target = self._state.target or resolve_target(self._cfg, None)
        self._state = State(
            connected=False,
            status=status,
            detail=(detail + ("\n" + extra if extra else "")).strip(),
            info=None,
            target=target,
            gain=self._state.gain,
            muted=self._state.muted,
            version=self._state.version + 1,
        )
        self._emit(Event("state", {"state": self._state}))

    def _client_notes(self) -> str:
        notes = getattr(self._client, "dll_notes", None)
        if notes:
            return "\n".join(notes[-4:])
        return ""

    # ------------------------------------------------------------- config
    def _apply_config(self, cfg: Config) -> None:
        old = self._cfg
        backend_changed = (
            cfg.connection.demo_backend != old.connection.demo_backend
            or cfg.connection.dll_path != old.connection.dll_path
        )
        self._cfg = cfg

        if backend_changed:
            self._log.info("Switching backend (demo=%s, dll=%s)",
                           cfg.connection.demo_backend, cfg.connection.dll_path or "auto")
            try:
                self._client.close()
            except Exception:
                pass
            self._client = make_client(cfg)
            self._values.clear()
            self._state = replace(self._state, connected=False, info=None,
                                  status="Connecting…", detail="")
            self._emit(Event("state", {"state": self._state}))
            self._retry_at = 0.0
            return

        try:
            self._client.apply_config(cfg.connection)
        except AttributeError:
            pass

        mode_changed = cfg.monitor.mode != old.monitor.mode
        target_changed = cfg.target != old.target
        if mode_changed:
            self._values.clear()
            self._next_poll = 0.0

        if self._state.connected and self._state.info is not None:
            target = resolve_target(cfg, self._state.info)
            self._state = replace(self._state, target=target)
            if target_changed or mode_changed:
                gain, muted = self._state.gain, self._state.muted
                try:
                    gain = self._client.read_gain(target)
                    muted = self._client.read_mute(target) if cfg.monitor.show_mute else False
                except (VoicemeeterError, VoicemeeterNotRunning):
                    pass
                self._state = replace(self._state, gain=gain, muted=muted)
                self._emit(Event("state", {"state": self._state}))
                if cfg.monitor.show_on_target_change:
                    self._emit(Event("level", {
                        "target": target,
                        "gain": gain,
                        "muted": muted,
                        "percent": display_percent(gain, self._cfg.osd.full_scale_db),
                        "info": self._state.info,
                        "source": "target",
                    }))
                if mode_changed:
                    self._initialize_snapshot()
        else:
            self._state = replace(self._state, target=resolve_target(cfg, None))
            self._emit(Event("state", {"state": self._state}))

        if cfg.connection.simulate_unavailable and isinstance(self._client, MockClient):
            self._mark_disconnected()

    # ------------------------------------------------------------- test / launch
    def _do_test(self) -> None:
        lines: list[str] = []
        ok = False
        demo = self._cfg.connection.demo_backend
        lines.append(f"Backend: {'DEMO (no Voicemeeter)' if demo else 'Voicemeeter Remote API (read-only)'}")
        if not demo:
            lines.append(f"Remote DLL: {getattr(self._client, 'dll_path', '') or 'searching…'}")
            for note in getattr(self._client, "dll_notes", [])[:6]:
                lines.append(f"  {note}")
            lines.append(f"Login code: {getattr(self._client, 'last_login_code', None)}  (0 = OK, 1 = app not running)")

        info = self._state.info
        if info is None:
            try:
                info = self._client.connect()
            except Exception as exc:
                lines.append(f"Connection: FAILED — {exc}")
                self._emit(Event("test", {"ok": False, "report": "\n".join(lines)}))
                return

        lines.append(f"Voicemeeter: {info.name}")
        lines.append(f"Version: {info.version or 'unknown'}")
        lines.append(f"Strips: {len(info.strips)}   Buses: {len(info.buses)}")
        lines.append("")
        lines.append("Channels:")
        for channel in info.channels():
            gain = muted = None
            try:
                target = ResolvedTarget.from_channel(channel)
                gain = self._client.read_gain(target)
                muted = self._client.read_mute(target)
            except VoicemeeterError:
                pass
            extra = ""
            if gain is not None:
                extra = f"   {gain:+6.1f} dB" + ("   [muted]" if muted else "")
            lines.append(f"  {channel.base:<10} {channel.display}{extra}")

        target = self._state.target or resolve_target(self._cfg, info)
        lines.append("")
        lines.append(f"Selected target: {target.base}  ({target.short_name})")
        lines.append("Mode: " + ("follow any fader" if self._cfg.monitor.mode == "any" else "selected channel only"))
        lines.append("Write access: none — this app is read-only.")
        try:
            gain = self._client.read_gain(target)
            muted = self._client.read_mute(target)
            lines.append(f"Read {target.gain_param} = {gain:+.1f} dB ({display_percent(gain, self._cfg.osd.full_scale_db)} %)")
            lines.append(f"Read {target.mute_param} = {'1 (muted)' if muted else '0 (unmuted)'}")
            ok = True
        except Exception as exc:
            lines.append(f"Read test: FAILED — {exc}")

        if not demo:
            lines.append("")
            lines.append("Windows permissions: none required (no admin, no driver, no injection).")
        self._emit(Event("test", {"ok": ok, "report": "\n".join(lines)}))

    def _do_launch(self, edition: int) -> None:
        try:
            self._client.launch(edition)
        except Exception as exc:
            self._emit(Event("error", {"message": f"Could not launch Voicemeeter: {exc}"}))
            return
        self._emit(Event("test", {"ok": True,
                                  "report": "Voicemeeter launch requested. Waiting for it to appear…"}))
        self._retry_at = 0.0

    # ------------------------------------------------------------- events
    def _emit(self, event: Event) -> None:
        self._events.put(event)
