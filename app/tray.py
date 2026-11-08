"""Tray icon and menu (pystray + Pillow).

Callbacks never touch Tk or the Voicemeeter API directly: they post callables to
the app's UI queue, which the Tk main loop drains.
"""
from __future__ import annotations

import logging
from typing import Callable

from PIL import Image, ImageDraw

from .config import Config
from .controller import State

_LOGGER = logging.getLogger(__name__)

ACCENT = (122, 162, 247, 255)
DIM = (110, 118, 136, 255)
MUTED = (238, 120, 120, 255)
CARD = (18, 21, 28, 255)


def _speaker(draw: ImageDraw.ImageDraw, size: int, color):
    s = float(size)
    body = (0.20 * s, 0.36 * s, 0.34 * s, 0.64 * s)
    draw.rounded_rectangle(body, radius=max(1, int(0.03 * s)), fill=color)
    draw.polygon(
        [
            (0.34 * s, 0.36 * s),
            (0.56 * s, 0.22 * s),
            (0.56 * s, 0.78 * s),
            (0.34 * s, 0.64 * s),
        ],
        fill=color,
    )
    for radius, width in ((0.16, 0.07), (0.27, 0.06)):
        box = (0.58 * s - radius * s, 0.5 * s - radius * s,
               0.58 * s + radius * s, 0.5 * s + radius * s)
        draw.arc(box, start=-55, end=55, fill=color, width=max(1, int(width * s)))


def make_icon(connected: bool, muted: bool = False, size: int = 64) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=int(size * 0.22),
                           fill=CARD, outline=(255, 255, 255, 26), width=1)
    color = MUTED if muted else (ACCENT if connected else DIM)
    _speaker(draw, size, color)
    if muted:
        draw.line((size * 0.70, size * 0.26, size * 0.86, size * 0.42),
                  fill=(255, 255, 255, 235), width=max(2, int(size * 0.05)))
        draw.line((size * 0.86, size * 0.26, size * 0.70, size * 0.42),
                  fill=(255, 255, 255, 235), width=max(2, int(size * 0.05)))
    elif not connected:
        # small status dot: gray = waiting
        draw.ellipse((size * 0.70, size * 0.68, size * 0.86, size * 0.84),
                     fill=(250, 200, 110, 255))
    return image


class TrayIcon:
    def __init__(self, post: Callable[[Callable[[], None]], None], actions):
        """``post`` marshals a callable onto the Tk thread.

        ``actions`` provides: show_settings(), preview_osd(), reconnect(),
        set_target(kind, index), set_autostart(bool), open_config_folder(),
        open_log(), quit().
        """
        self._post = post
        self._actions = actions
        self._icon = None
        self._state: State | None = None
        self._config: Config | None = None
        self._target_items: list[tuple[str, str, int]] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self, state: State, config: Config) -> bool:
        try:
            import pystray
        except Exception as exc:  # pystray missing/broken -> app still works
            _LOGGER.warning("Tray icon unavailable: %s", exc)
            return False
        self._state = state
        self._config = config
        try:
            self._icon = pystray.Icon(
                "voicemeeter-popup",
                icon=make_icon(state.connected, state.muted),
                title="VoiceMeeter Volume OSD",
                menu=self._build_menu(),
            )
            self._icon.run_detached()
            return True
        except Exception:
            _LOGGER.exception("Could not start the tray icon")
            self._icon = None
            return False

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None

    def refresh(self, state: State, config: Config) -> None:
        if self._icon is None:
            return
        self._state, self._config = state, config
        try:
            self._icon.icon = make_icon(state.connected, state.muted)
            header = state.status
            if state.connected and state.info:
                header = f"Connected — {state.info.name}"
            self._icon.title = f"VoiceMeeter Volume OSD\n{header}"
            self._icon.menu = self._build_menu()
            self._icon.update_menu()
        except Exception:
            _LOGGER.debug("Tray refresh failed", exc_info=True)

    # ------------------------------------------------------------------ menu
    def _build_menu(self):
        import pystray

        state = self._state or State()
        cfg = self._config or Config()
        connected = state.connected
        header = state.status
        if connected and state.info:
            header = f"● {state.info.name} — connected"
        elif state.status:
            header = f"○ {state.status}"

        def run(func, *args):
            self._post(lambda: func(*args))

        items = [
            pystray.MenuItem(header, None, enabled=False),
        ]
        if state.target is not None:
            items.append(pystray.MenuItem(
                f"{state.target.short_name}: muted" if state.muted
                else f"{state.target.short_name}: {state.gain:+.1f} dB",
                None, enabled=False))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Settings…", lambda *_: run(self._actions.show_settings), default=True),
            pystray.MenuItem("Target", self._target_menu()),
            pystray.MenuItem("Show test popup", lambda *_: run(self._actions.preview_osd)),
            pystray.MenuItem("Reconnect", lambda *_: run(self._actions.reconnect)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start with Windows",
                             lambda *_: run(self._actions.set_autostart, not cfg.general.start_with_windows),
                             checked=lambda *_: bool(self._config and self._config.general.start_with_windows)),
            pystray.MenuItem("Open config folder", lambda *_: run(self._actions.open_config_folder)),
            pystray.MenuItem("Open log file", lambda *_: run(self._actions.open_log)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", lambda *_: run(self._actions.quit)),
        ]
        return pystray.Menu(*items)

    def _target_menu(self):
        import pystray

        state = self._state or State()
        cfg = self._config or Config()
        entries = []
        if state.info is not None:
            master_bus = state.info.buses[cfg.target.master_bus] if state.info.buses else None
            if master_bus is not None:
                entries.append(
                    pystray.MenuItem(
                        f"Master ({master_bus.name})",
                        lambda *_: self._post(lambda: self._actions.set_target("master", 0)),
                        checked=lambda *_: cfg.target.kind == "master",
                        radio=True,
                    )
                )
            for bus in state.info.buses:
                entries.append(
                    pystray.MenuItem(
                        f"Bus {bus.name}"[:60],
                        (lambda index: lambda *_: self._post(lambda: self._actions.set_target("bus", index)))(bus.index),
                        checked=(lambda index: lambda *_: cfg.target.kind == "bus" and cfg.target.index == index)(bus.index),
                        radio=True,
                    )
                )
            if state.info.strips:
                entries.append(pystray.Menu.SEPARATOR)
            for strip in state.info.strips:
                entries.append(
                    pystray.MenuItem(
                        strip.display[:60],
                        (lambda index: lambda *_: self._post(lambda: self._actions.set_target("strip", index)))(strip.index),
                        checked=(lambda index: lambda *_: cfg.target.kind == "strip" and cfg.target.index == index)(strip.index),
                        radio=True,
                    )
                )
        else:
            entries.append(pystray.MenuItem("Voicemeeter not connected", None, enabled=False))
        return pystray.Menu(*entries)
