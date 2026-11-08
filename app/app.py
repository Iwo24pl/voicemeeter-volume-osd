"""Application wiring: Tk root, tray, monitor controller and OSD.

Thread layout
-------------
Tk main thread      settings window, tray callbacks (via a queue), OSD animation
monitor thread      every Voicemeeter Remote API read, change detection, reconnect
UI queue            callables posted by the tray/monitor threads, drained by Tk

There is no keyboard hook and no write path: the app only observes Voicemeeter.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import queue
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from .backend import resolve_target
from .config import Config, config_path, data_dir, log_path
from .controller import Controller, Event, State
from .levels import display_percent
from .osd import OsdModel, OsdWindow
from .settings_ui import SettingsWindow
from .tray import TrayIcon
from .winapi import InstanceGuard, message_box, system_dpi

_LOGGER = logging.getLogger(__name__)


class App:
    def __init__(self, config_file: str | os.PathLike | None = None, demo: bool = False,
                 guard: InstanceGuard | None = None):
        self._guard = guard
        self._quitting = False

        self.config = Config.load(config_file)
        if demo:
            # Session-only override: do not persist it on exit unless the user
            # changes settings in the app.
            self.config = self.config.with_section("connection", demo_backend=True)
            self._skip_shutdown_save = True
        else:
            self._skip_shutdown_save = False
        self.config_file = config_file

        # Create the config file on first launch so the documented path exists.
        try:
            from pathlib import Path

            path = Path(config_file) if config_file else config_path()
            if not path.exists() and not demo:
                self.config.save(config_file)
        except Exception:
            pass

        # ---- Tk root: invisible; settings and OSD are toplevels.
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("VoiceMeeter Volume OSD")
        self.root.report_callback_exception = self._tk_exception
        dpi = system_dpi()
        self.ui_scale = max(0.75, min(2.5, dpi / 96.0))
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
            for name, size in (("TkDefaultFont", 9), ("TkTextFont", 9), ("TkMenuFont", 9)):
                tkfont.nametofont(name).configure(size=max(8, int(round(size * self.ui_scale))))
        except Exception:
            pass

        self.events: "queue.Queue[Event]" = queue.Queue()
        self.state = State(status="Starting…")
        self._ui_jobs: "queue.Queue[Callable[[], None]]" = queue.Queue()

        # ---- subsystems
        self.controller = Controller(self.config, self.events, _LOGGER)
        self.osd = OsdWindow(self.root, lambda: self.config)
        self.settings = SettingsWindow(self)
        self.tray = TrayIcon(self.post, self)

        self._pump_job = None

    # ------------------------------------------------------------------ start
    def run(self, show_settings: bool = False) -> None:
        self.controller.start()
        tray_ok = self.tray.start(self.state, self.config)
        if not tray_ok:
            _LOGGER.warning("Tray icon failed to start; opening settings instead")
            show_settings = True

        if self._guard is not None:
            self._guard.start_listener(lambda: self.post(self.show_settings))

        self.root.after(30, self._pump)
        if show_settings:
            self.root.after(250, self.show_settings)

        try:
            self.root.mainloop()
        finally:
            self.shutdown()

    # ------------------------------------------------------------------ events
    def _pump(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                try:
                    self._handle_event(event)
                except Exception:
                    _LOGGER.exception("Event handling failed: %s", event.kind)
        except queue.Empty:
            pass

        try:
            while True:
                job = self._ui_jobs.get_nowait()
                try:
                    job()
                except Exception:
                    _LOGGER.exception("UI job failed")
        except queue.Empty:
            pass

        if not self._quitting:
            self._pump_job = self.root.after(30, self._pump)

    def _handle_event(self, event: Event) -> None:
        if event.kind == "state":
            state: State = event.data["state"]
            self.state = state
            self.tray.refresh(state, self.config)
            self.settings.refresh(state, self.config)
        elif event.kind == "level":
            data = event.data
            target = data["target"]
            info = data.get("info") or self.state.info
            model = OsdModel(
                name=target.osd_name,
                kicker=info.kicker if info is not None else "VOICEMEETER",
                percent=int(data["percent"]),
                db=float(data["gain"]),
                muted=bool(data["muted"]),
                show_db=self.config.osd.show_db,
            )
            if self.config.osd.enabled:
                if self.osd.visible:
                    self.osd.update(model)
                else:
                    self.osd.show(model)
            if self.settings.visible:
                self.settings.update_level(self.state, self.config)
        elif event.kind == "test":
            if self.settings.visible:
                self.settings.show_test_report(bool(event.data.get("ok")), str(event.data.get("report", "")))
            else:
                _LOGGER.info("Connection test: %s", event.data.get("report"))
        elif event.kind == "error":
            message = str(event.data.get("message", "Voicemeeter error"))
            _LOGGER.warning("%s", message)
            if self.config.osd.enabled and self.settings.visible:
                self.osd.show(OsdModel(name=message[:70], kicker="VOICEMEETER",
                                       percent=None, db=None, warning=True, show_db=False))

    def post(self, job: Callable[[], None]) -> None:
        """Thread-safe: run ``job`` on the Tk thread."""
        self._ui_jobs.put(job)

    def _tk_exception(self, exc_type, exc_value, exc_tb) -> None:
        _LOGGER.error("Tk callback failed", exc_info=(exc_type, exc_value, exc_tb))

    # ------------------------------------------------------------------ config
    def set_config(self, config: Config) -> None:
        self.config = config.normalized()
        self._skip_shutdown_save = False
        self.config.save(self.config_file)
        self.controller.apply_config(self.config)
        self.tray.refresh(self.state, self.config)
        self.settings.refresh(self.state, self.config)

    def set_target(self, kind: str, index: int) -> None:
        cfg = self.config
        target = dict(cfg.to_dict()["target"])
        target["kind"] = kind
        if kind == "master":
            target["master_bus"] = index
        else:
            target["index"] = index
        self.set_config(cfg.replace(target=target))

    def set_autostart(self, enabled: bool) -> None:
        from . import startup

        ok = startup.set_enabled(enabled)
        if not ok:
            message_box("Windows would not let the app change the startup entry.\n"
                        "You can add it manually: shell:startup", error=True)
        self.config = self.config.with_section("general", start_with_windows=bool(ok and enabled))
        self.config.save(self.config_file)
        self.tray.refresh(self.state, self.config)
        self.settings.refresh(self.state, self.config)

    # ------------------------------------------------------------------ actions
    def show_settings(self) -> None:
        self.settings.show()

    def preview_osd(self) -> None:
        state = self.state
        target = state.target or resolve_target(self.config, state.info)
        info = state.info
        gain = state.gain if state.connected else -6.0
        muted = state.muted if state.connected else False
        self.osd.show(OsdModel(
            name=target.osd_name,
            kicker=info.kicker if info is not None else "VOICEMEETER",
            percent=display_percent(gain, self.config.osd.full_scale_db),
            db=gain,
            muted=muted,
            show_db=self.config.osd.show_db,
        ), sticky_ms=2600)

    def reconnect(self) -> None:
        self.controller.reconnect()

    def test_connection(self) -> None:
        self.controller.test_connection()

    def launch_voicemeeter(self, edition: int) -> None:
        self.controller.launch(edition)

    def demo_bump(self) -> None:
        self.controller.demo_bump()

    def open_config_folder(self) -> None:
        try:
            os.startfile(data_dir())
        except OSError as exc:
            _LOGGER.warning("Could not open %s: %s", data_dir(), exc)

    def open_log(self) -> None:
        try:
            if not log_path().exists():
                log_path().touch()
            os.startfile(log_path())
        except OSError as exc:
            _LOGGER.warning("Could not open %s: %s", log_path(), exc)

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            if self._pump_job:
                self.root.after_cancel(self._pump_job)
        except Exception:
            pass
        self.root.quit()

    def shutdown(self) -> None:
        _LOGGER.info("Shutting down")
        try:
            self.controller.stop()
        except Exception:
            _LOGGER.debug("Controller stop failed", exc_info=True)
        try:
            self.osd.close()
        except Exception:
            _LOGGER.debug("OSD close failed", exc_info=True)
        try:
            self.tray.stop()
        except Exception:
            _LOGGER.debug("Tray stop failed", exc_info=True)
        try:
            if self._guard is not None:
                self._guard.close()
        except Exception:
            pass
        try:
            if not self._skip_shutdown_save:
                self.config.save(self.config_file)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
