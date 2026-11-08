"""Settings window (tkinter/ttk).

Every control applies immediately and is saved to disk.  The window is created
lazily by the tray icon or a second launch.

There is deliberately no key-binding UI: the app does not hook the keyboard.
"""
from __future__ import annotations

import logging
import os
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .backend import VmInfo, channel_device_short, resolve_target
from .config import POSITIONS, Config
from .controller import State
from .levels import display_percent, format_db
from .vmapi import DLL_NAME

_LOGGER = logging.getLogger(__name__)

POSITION_LABELS = {
    "top-left": "Top left", "top-center": "Top center", "top-right": "Top right",
    "middle-left": "Middle left", "middle-center": "Center", "middle-right": "Middle right",
    "bottom-left": "Bottom left", "bottom-center": "Bottom center", "bottom-right": "Bottom right",
}
KIND_LABELS = {"master": "Master volume", "bus": "A bus output", "strip": "An input strip"}
EDITION_LABELS = {1: "Voicemeeter", 2: "Voicemeeter Banana", 3: "Voicemeeter Potato", 6: "Potato x64"}


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.root: tk.Tk = app.root
        self.win: tk.Toplevel | None = None
        self._updating = False
        self._info_signature: tuple = ()
        self._channel_map: list[tuple[str, int]] = []
        self._master_map: list[tuple[str, int]] = []
        self._value_vars: dict[str, tk.DoubleVar] = {}

    # ------------------------------------------------------------------ window
    def show(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            self.refresh(self.app.state, self.app.config)
            return
        self._build()
        self.refresh(self.app.state, self.app.config)

    def hide(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()

    def destroy(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            try:
                self.win.destroy()
            except Exception:
                pass
        self.win = None

    @property
    def visible(self) -> bool:
        return bool(self.win is not None and self.win.winfo_exists() and self.win.state() != "withdrawn")

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        scale = getattr(self.app, "ui_scale", 1.0)
        self.win = tk.Toplevel(self.root)
        self.win.title("VoiceMeeter Volume OSD")
        self.win.minsize(int(700 * scale), int(640 * scale))
        self.win.geometry(f"{int(780 * scale)}x{int(720 * scale)}")
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        try:
            from .config import asset_path

            icon = asset_path("icon.ico")
            if icon.exists():
                self.win.iconbitmap(str(icon))
        except Exception:
            pass

        style = ttk.Style(self.win)
        try:
            if "vista" in style.theme_names():
                style.theme_use("vista")
        except Exception:
            pass

        body = ttk.Frame(self.win, padding=(18, 14, 18, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)

        # header
        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="VoiceMeeter Volume OSD",
                  font=tkfont.Font(family="Segoe UI", size=15, weight="bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Mirrors Voicemeeter's faders on screen — it never touches your keys",
                  foreground="#5d6472").grid(row=1, column=0, sticky="w", pady=(2, 0))
        status_box = ttk.Frame(header)
        status_box.grid(row=0, column=1, rowspan=2, sticky="e")
        self.dot = tk.Canvas(status_box, width=12, height=12, highlightthickness=0, bd=0)
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self._dot_item = self.dot.create_oval(2, 2, 10, 10, fill="#c9a227", outline="")
        self.var_status = tk.StringVar(value="Starting…")
        ttk.Label(status_box, textvariable=self.var_status,
                  font=tkfont.Font(size=10, weight="bold")).grid(row=0, column=1, sticky="w")
        self.var_status_detail = tk.StringVar(value="")
        ttk.Label(status_box, textvariable=self.var_status_detail,
                  foreground="#5d6472").grid(row=1, column=1, sticky="e")

        notebook = ttk.Notebook(body)
        notebook.grid(row=1, column=0, sticky="nsew")
        self.notebook = notebook

        self.tab_target = ttk.Frame(notebook, padding=14)
        self.tab_look = ttk.Frame(notebook, padding=14)
        self.tab_connection = ttk.Frame(notebook, padding=14)
        notebook.add(self.tab_target, text="Watching")
        notebook.add(self.tab_look, text="Appearance")
        notebook.add(self.tab_connection, text="Connection")
        for tab in (self.tab_target, self.tab_look, self.tab_connection):
            tab.columnconfigure(0, weight=1)

        self._build_target(self.tab_target)
        self._build_appearance(self.tab_look)
        self._build_connection(self.tab_connection)

        footer = ttk.Frame(body)
        footer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="Changes are saved automatically.  Read-only: no Voicemeeter settings are modified.",
                  foreground="#5d6472").grid(row=0, column=0, sticky="w")
        ttk.Button(footer, text="Close", command=self.hide).grid(row=0, column=1, sticky="e")

        self._center_window()

    def _center_window(self) -> None:
        try:
            self.win.update_idletasks()
            width = self.win.winfo_width()
            height = self.win.winfo_height()
            from .winapi import primary_monitor

            monitor = primary_monitor()
            if monitor:
                left, top, right, bottom = monitor.work
                x = left + (right - left - width) // 2
                y = top + (bottom - top - height) // 2
            else:
                x = (self.win.winfo_screenwidth() - width) // 2
                y = (self.win.winfo_screenheight() - height) // 2
            self.win.geometry(f"{width}x{height}{x:+d}{y:+d}")
        except Exception:
            pass

    # ------------------------------------------------------------------ target
    def _build_target(self, tab: ttk.Frame) -> None:
        cfg = self.app.config
        watch = ttk.LabelFrame(tab, text="What to watch", padding=12)
        watch.grid(row=0, column=0, sticky="ew")
        watch.columnconfigure(1, weight=1)

        self.var_mode = tk.StringVar(value=cfg.monitor.mode)
        ttk.Radiobutton(watch, text="Any fader that moves in Voicemeeter (recommended)",
                        value="any", variable=self.var_mode,
                        command=self._apply_monitor).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(watch, text="Only the selected channel",
                        value="target", variable=self.var_mode,
                        command=self._apply_monitor).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(watch, text="Works with the Voicemeeter window, its MIDI/keyboard mapping and any control "
                             "surface — the popup follows whatever Voicemeeter reports.",
                  foreground="#5d6472", wraplength=640, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 10))

        sep = ttk.Separator(watch)
        sep.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(watch, text="Selected channel", font=tkfont.Font(size=10, weight="bold")).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.var_kind = tk.StringVar(value=cfg.target.kind)
        row = 5
        for kind in ("master", "bus", "strip"):
            ttk.Radiobutton(watch, text=KIND_LABELS[kind], value=kind,
                            variable=self.var_kind, command=self._on_kind_change).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=1)
            row += 1

        self.frame_master = ttk.Frame(watch)
        self.frame_master.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.frame_master.columnconfigure(1, weight=1)
        ttk.Label(self.frame_master, text="Master bus").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.cmb_master = ttk.Combobox(self.frame_master, state="readonly", width=46)
        self.cmb_master.grid(row=0, column=1, sticky="ew")
        self.cmb_master.bind("<<ComboboxSelected>>", lambda _e: self._on_master_change())
        row += 1

        self.frame_channel = ttk.Frame(watch)
        self.frame_channel.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.frame_channel.columnconfigure(1, weight=1)
        self.lbl_channel = ttk.Label(self.frame_channel, text="Channel")
        self.lbl_channel.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.cmb_channel = ttk.Combobox(self.frame_channel, state="readonly", width=46)
        self.cmb_channel.grid(row=0, column=1, sticky="ew")
        self.cmb_channel.bind("<<ComboboxSelected>>", lambda _e: self._on_channel_change())

        # ---- current level (read-only)
        level_frame = ttk.LabelFrame(tab, text="Current level of the selected channel", padding=12)
        level_frame.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        level_frame.columnconfigure(0, weight=1)

        self.var_now_name = tk.StringVar(value="—")
        ttk.Label(level_frame, textvariable=self.var_now_name,
                  font=tkfont.Font(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        self.var_now_value = tk.StringVar(value="—")
        ttk.Label(level_frame, textvariable=self.var_now_value).grid(row=0, column=1, sticky="e")

        self.progress = ttk.Progressbar(level_frame, maximum=100, value=0)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 10))

        buttons = ttk.Frame(level_frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Button(buttons, text="Show popup now", command=self.app.preview_osd).pack(side="left")
        self.var_show_mute = tk.BooleanVar(value=cfg.monitor.show_mute)
        ttk.Checkbutton(buttons, text="Also show mute changes", variable=self.var_show_mute,
                        command=self._apply_monitor).pack(side="left", padx=(14, 0))
        self.var_show_target = tk.BooleanVar(value=cfg.monitor.show_on_target_change)
        ttk.Checkbutton(buttons, text="Show the popup when the selection changes",
                        variable=self.var_show_target,
                        command=self._apply_monitor).pack(side="left", padx=(14, 0))

        # ---- polling
        poll = ttk.Frame(tab)
        poll.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        poll.columnconfigure(3, weight=1)
        ttk.Label(poll, text="Update interval").grid(row=0, column=0, sticky="w")
        self.var_poll = tk.StringVar(value=str(cfg.monitor.poll_ms))
        self.spn_poll = ttk.Spinbox(poll, from_=25, to=250, increment=5, width=6,
                                    textvariable=self.var_poll, command=self._apply_monitor)
        self.spn_poll.grid(row=0, column=1, padx=(10, 4))
        self.spn_poll.bind("<FocusOut>", lambda _e: self._apply_monitor())
        self.spn_poll.bind("<Return>", lambda _e: self._apply_monitor())
        ttk.Label(poll, text="ms").grid(row=0, column=2, sticky="w")
        ttk.Label(poll, text="50 ms feels instant. The app only polls IsParametersDirty; it does not read audio.",
                  foreground="#5d6472").grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        info = ttk.Label(
            tab,
            text="The app never changes Voicemeeter volume and never intercepts keyboard input. "
                 "It only reads Gain/Mute through Voicemeeter's official Remote API.",
            foreground="#5d6472", wraplength=650, justify="left",
        )
        info.grid(row=3, column=0, sticky="w", pady=(16, 0))

    # ------------------------------------------------------------------ appearance
    def _build_appearance(self, tab: ttk.Frame) -> None:
        cfg = self.app.config
        placement = ttk.LabelFrame(tab, text="Placement", padding=12)
        placement.grid(row=0, column=0, sticky="ew")
        placement.columnconfigure(1, weight=1)

        ttk.Label(placement, text="Position").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.var_position = tk.StringVar(value=POSITION_LABELS[cfg.osd.position])
        self.cmb_position = ttk.Combobox(placement, state="readonly", width=22,
                                         textvariable=self.var_position,
                                         values=[POSITION_LABELS[p] for p in POSITIONS])
        self.cmb_position.grid(row=0, column=1, sticky="w")
        self.cmb_position.bind("<<ComboboxSelected>>", lambda _e: self._apply_osd_now())

        ttk.Label(placement, text="Monitor").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.var_monitor = tk.StringVar(value="Active monitor (mouse)" if cfg.osd.monitor == "cursor" else "Primary monitor")
        self.cmb_monitor = ttk.Combobox(placement, state="readonly", width=22,
                                        textvariable=self.var_monitor,
                                        values=["Active monitor (mouse)", "Primary monitor"])
        self.cmb_monitor.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.cmb_monitor.bind("<<ComboboxSelected>>", lambda _e: self._apply_osd_now())

        offsets = ttk.Frame(placement)
        offsets.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(offsets, text="Offset").pack(side="left")
        self.var_offset_x = tk.StringVar(value=str(cfg.osd.offset_x))
        self.var_offset_y = tk.StringVar(value=str(cfg.osd.offset_y))
        for label, var in (("X", self.var_offset_x), ("Y", self.var_offset_y)):
            ttk.Label(offsets, text=f"  {label}").pack(side="left")
            spin = ttk.Spinbox(offsets, from_=-2000, to=2000, increment=5, width=6,
                               textvariable=var, command=self._apply_osd_now)
            spin.pack(side="left", padx=(4, 0))
            spin.bind("<FocusOut>", lambda _e: self._apply_osd_now())
            spin.bind("<Return>", lambda _e: self._apply_osd_now())
        ttk.Label(offsets, text="px", foreground="#5d6472").pack(side="left", padx=(6, 0))

        look = ttk.LabelFrame(tab, text="Look and timing", padding=12)
        look.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        look.columnconfigure(1, weight=1)

        self._scale(look, 0, "Size", "scale", 60, 200, int(cfg.osd.scale * 100), "%")
        self._scale(look, 1, "Opacity", "opacity", 25, 100, int(cfg.osd.opacity * 100), "%")
        self._scale(look, 2, "Hide after", "hold", 3, 100, int(cfg.osd.hold_seconds * 10), "s", divisor=10.0)
        self._scale(look, 3, "Fade time", "fade", 0, 800, int(cfg.osd.fade_ms), "ms")

        ref_row = ttk.Frame(look)
        ref_row.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(ref_row, text="100 % at").pack(side="left")
        self.var_ref = tk.StringVar(value=f"{cfg.osd.full_scale_db:g}")
        self.spn_ref = ttk.Spinbox(ref_row, from_=-40, to=12, increment=0.5, width=6,
                                   textvariable=self.var_ref, command=self._apply_osd_now)
        self.spn_ref.pack(side="left", padx=(10, 4))
        self.spn_ref.bind("<FocusOut>", lambda _e: self._apply_osd_now())
        self.spn_ref.bind("<Return>", lambda _e: self._apply_osd_now())
        ttk.Label(ref_row, text="dB").pack(side="left")
        ttk.Label(ref_row, text="  Above this value the popup reads over 100 %.",
                  foreground="#5d6472").pack(side="left")

        self.var_show_db = tk.BooleanVar(value=cfg.osd.show_db)
        ttk.Checkbutton(look, text="Show the dB value in the popup", variable=self.var_show_db,
                        command=self._apply_osd_now).grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.var_enabled = tk.BooleanVar(value=cfg.osd.enabled)
        ttk.Checkbutton(look, text="Show the popup at all", variable=self.var_enabled,
                        command=self._apply_osd_now).grid(row=6, column=0, columnspan=2, sticky="w", pady=(6, 0))

        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, sticky="w", pady=(14, 0))
        ttk.Button(actions, text="Preview on screen", command=self.app.preview_osd).pack(side="left")
        ttk.Label(actions, text="  Preview appears on the active monitor and hides by itself.",
                  foreground="#5d6472").pack(side="left")

    def _scale(self, parent, row, label, key, low, high, initial, unit, divisor: float = 1.0) -> None:
        ttk.Label(parent, text=label, width=12).grid(row=row, column=0, sticky="w", pady=3)
        var = tk.DoubleVar(value=initial)
        self._value_vars[key] = var
        holder = ttk.Frame(parent)
        holder.grid(row=row, column=1, sticky="ew", pady=3)
        holder.columnconfigure(0, weight=1)
        value_label = tk.Label(holder, width=8, anchor="e")
        value_label.grid(row=0, column=1, padx=(8, 0))

        def format_value(value: float) -> str:
            if key == "hold":
                return f"{value / divisor:.1f} {unit}"
            if key == "fade":
                return f"{int(value)} {unit}"
            return f"{int(value)} {unit}"

        def on_change(raw):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return
            value_label.configure(text=format_value(value))

        scale = ttk.Scale(holder, from_=low, to=high, variable=var, command=on_change)
        scale.grid(row=0, column=0, sticky="ew")
        scale.bind("<ButtonRelease-1>", lambda _e: self._apply_osd_now())
        scale.bind("<KeyRelease>", lambda _e: self._apply_osd_now())
        value_label.configure(text=format_value(initial))

    # ------------------------------------------------------------------ connection
    def _build_connection(self, tab: ttk.Frame) -> None:
        cfg = self.app.config
        status = ttk.LabelFrame(tab, text="Voicemeeter", padding=12)
        status.grid(row=0, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)

        self.var_conn_status = tk.StringVar(value="—")
        ttk.Label(status, textvariable=self.var_conn_status,
                  font=tkfont.Font(size=11, weight="bold")).grid(row=0, column=0, sticky="w")
        self.var_conn_detail = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.var_conn_detail, foreground="#5d6472",
                  wraplength=640, justify="left").grid(row=1, column=0, sticky="w", pady=(4, 8))

        buttons = ttk.Frame(status)
        buttons.grid(row=2, column=0, sticky="w")
        ttk.Button(buttons, text="Test connection", command=self.app.test_connection).pack(side="left")
        ttk.Button(buttons, text="Reconnect", command=self.app.reconnect).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Launch Voicemeeter", command=self._launch).pack(side="left", padx=(16, 0))
        self.var_edition = tk.StringVar(value=EDITION_LABELS.get(cfg.connection.launch_edition,
                                                                "Voicemeeter Banana"))
        ttk.Combobox(buttons, state="readonly", width=21, textvariable=self.var_edition,
                     values=[EDITION_LABELS[k] for k in (1, 2, 3, 6)]).pack(side="left", padx=(8, 0))

        dll = ttk.LabelFrame(tab, text="Remote API library", padding=12)
        dll.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        dll.columnconfigure(0, weight=1)
        self.var_dll = tk.StringVar(value=cfg.connection.dll_path)
        self.ent_dll = ttk.Entry(dll, textvariable=self.var_dll)
        self.ent_dll.grid(row=0, column=0, sticky="ew")
        self.ent_dll.bind("<FocusOut>", lambda _e: self._apply_dll())
        self.ent_dll.bind("<Return>", lambda _e: self._apply_dll())
        ttk.Button(dll, text="Browse…", command=self._browse_dll).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(dll, text="Auto-detect", command=self._auto_dll).grid(row=0, column=2, padx=(4, 0))
        ttk.Label(dll, text=f"Leave empty to search the Voicemeeter installation. 64-bit Python needs {DLL_NAME}.",
                  foreground="#5d6472").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

        testing = ttk.LabelFrame(tab, text="Testing", padding=12)
        testing.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        testing.columnconfigure(0, weight=1)
        self.var_demo = tk.BooleanVar(value=cfg.connection.demo_backend)
        ttk.Checkbutton(testing, text="Use the demo backend (no Voicemeeter needed)",
                        variable=self.var_demo, command=self._apply_demo).grid(row=0, column=0, sticky="w")
        self.var_simulate = tk.BooleanVar(value=cfg.connection.simulate_unavailable)
        self.chk_simulate = ttk.Checkbutton(
            testing, text="Demo: pretend Voicemeeter is unavailable (tests reconnect handling)",
            variable=self.var_simulate, command=self._apply_demo)
        self.chk_simulate.grid(row=1, column=0, sticky="w", pady=(6, 0))
        demo_row = ttk.Frame(testing)
        demo_row.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.btn_bump = ttk.Button(demo_row, text="Simulate a fader move (demo)",
                                   command=self.app.demo_bump)
        self.btn_bump.pack(side="left")
        ttk.Label(demo_row, text="  Changes a demo value so the normal polling path shows the popup.",
                  foreground="#5d6472").pack(side="left")

        general = ttk.LabelFrame(tab, text="Windows", padding=12)
        general.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        self.var_autostart = tk.BooleanVar(value=cfg.general.start_with_windows)
        ttk.Checkbutton(general, text="Start with Windows (per-user, HKCU Run key)",
                        variable=self.var_autostart, command=self._apply_autostart).grid(row=0, column=0, sticky="w")
        links = ttk.Frame(general)
        links.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(links, text="Open config folder", command=self.app.open_config_folder).pack(side="left")
        ttk.Button(links, text="Open log file", command=self.app.open_log).pack(side="left", padx=(8, 0))
        ttk.Label(general, text="No administrator rights are required: the app reads Voicemeeter parameters "
                              "and stores its settings in your user profile.",
                  foreground="#5d6472", wraplength=640, justify="left").grid(row=2, column=0, sticky="w", pady=(8, 0))

    # ------------------------------------------------------------------ refresh
    def refresh(self, state: State, config: Config) -> None:
        if self.win is None or not self.win.winfo_exists():
            return
        self._updating = True
        try:
            self._refresh_header(state)
            self._refresh_channels(state, config)
            self._refresh_level(state, config)
            self._refresh_values(config)
            self._refresh_connection(state, config)
        except Exception:
            _LOGGER.exception("Settings refresh failed")
        finally:
            self._updating = False

    def _refresh_header(self, state: State) -> None:
        self.var_status.set(state.status)
        detail = ""
        if state.connected and state.info:
            detail = f"{state.info.name} {state.info.version}".strip()
        elif state.detail:
            detail = state.detail.splitlines()[0][:80]
        self.var_status_detail.set(detail)
        self.dot.itemconfig(self._dot_item, fill="#2f9e44" if state.connected else "#c9a227")

    def _refresh_channels(self, state: State, config: Config) -> None:
        info = state.info
        signature = ()
        if info is not None:
            signature = (info.type_id, tuple((c.kind, c.index, c.label, c.device) for c in info.channels()))
        rebuild = signature != self._info_signature
        self._info_signature = signature

        if rebuild or not self._channel_map:
            self._fill_master_combo(info)
            self._fill_channel_combo(info, "bus")

        if self.var_kind.get() != config.target.kind:
            self.var_kind.set(config.target.kind)
        self._show_kind_widgets(config.target.kind)

        if config.target.kind == "master":
            self._select_master(config.target.master_bus)
        else:
            self._fill_channel_combo(info, config.target.kind)
            self._select_channel(config.target.index)

    def _fill_master_combo(self, info: VmInfo | None) -> None:
        self._master_map = []
        if info is not None:
            for bus in info.buses:
                self._master_map.append((bus.display, bus.index))
        else:
            from .backend import bus_default_label

            for index in range(8):
                self._master_map.append((bus_default_label(6, index), index))
        self.cmb_master.configure(values=[label for label, _ in self._master_map])

    def _fill_channel_combo(self, info: VmInfo | None, kind: str) -> None:
        self._channel_map = []
        if info is not None:
            pool = info.buses if kind == "bus" else info.strips
            for channel in pool:
                self._channel_map.append((channel.display, channel.index))
        elif kind == "bus":
            from .backend import bus_default_label

            for index in range(8):
                self._channel_map.append((f"Bus {bus_default_label(6, index)}", index))
        else:
            for index in range(8):
                self._channel_map.append((f"Strip {index + 1}", index))
        self.cmb_channel.configure(values=[label for label, _ in self._channel_map])
        self.lbl_channel.configure(text="Bus" if kind == "bus" else "Strip")

    def _show_kind_widgets(self, kind: str) -> None:
        if kind == "master":
            self.frame_channel.grid_remove()
            self.frame_master.grid()
        else:
            self.frame_master.grid_remove()
            self.frame_channel.grid()

    def _select_master(self, index: int) -> None:
        for position, (_label, value) in enumerate(self._master_map):
            if value == index:
                self.cmb_master.current(position)
                return
        if self._master_map:
            self.cmb_master.current(0)

    def _select_channel(self, index: int) -> None:
        for position, (_label, value) in enumerate(self._channel_map):
            if value == index:
                self.cmb_channel.current(position)
                return
        if self._channel_map:
            self.cmb_channel.current(0)

    def _refresh_level(self, state: State, config: Config) -> None:
        target = state.target or resolve_target(config, state.info)
        name = target.short_name
        detail = channel_device_short(target.device)
        if detail and detail.lower() not in name.lower():
            name = f"{name}   ·   {detail}"
        self.var_now_name.set(name)
        percent = display_percent(state.gain, config.osd.full_scale_db)
        if state.muted:
            self.var_now_value.set("muted")
        else:
            self.var_now_value.set(f"{format_db(state.gain)}   ·   {percent} %")
        self.progress.configure(value=percent)

    def _refresh_values(self, config: Config) -> None:
        if self.win.focus_get() is not self.spn_poll:
            self.var_poll.set(str(config.monitor.poll_ms))
        if self.win.focus_get() is not self.spn_ref:
            self.var_ref.set(f"{config.osd.full_scale_db:g}")
        if self.var_mode.get() != config.monitor.mode:
            self.var_mode.set(config.monitor.mode)
        if self.var_show_mute.get() != config.monitor.show_mute:
            self.var_show_mute.set(config.monitor.show_mute)
        if self.var_show_target.get() != config.monitor.show_on_target_change:
            self.var_show_target.set(config.monitor.show_on_target_change)

    def _refresh_connection(self, state: State, config: Config) -> None:
        if state.connected and state.info:
            self.var_conn_status.set(f"Connected — {state.info.name}")
            text = (f"Version {state.info.version or 'unknown'}  ·  "
                    f"{len(state.info.strips)} strips, {len(state.info.buses)} buses")
            if state.info.demo:
                text += "  ·  demo backend"
            text += f"  ·  {'following any fader' if config.monitor.mode == 'any' else 'watching one channel'}"
            self.var_conn_detail.set(text)
        else:
            self.var_conn_status.set(state.status)
            self.var_conn_detail.set(state.detail or "—")
        self.var_autostart.set(config.general.start_with_windows)
        self.var_demo.set(config.connection.demo_backend)
        self.var_simulate.set(config.connection.simulate_unavailable)
        self.chk_simulate.configure(state="normal" if config.connection.demo_backend else "disabled")
        self.btn_bump.configure(state="normal" if config.connection.demo_backend else "disabled")
        if self.win.focus_get() is not self.ent_dll:
            self.var_dll.set(config.connection.dll_path)

    # ------------------------------------------------------------------ apply
    def _apply_monitor(self) -> None:
        if self._updating:
            return
        try:
            poll_ms = max(25, min(250, int(float(self.var_poll.get()))))
        except ValueError:
            poll_ms = self.app.config.monitor.poll_ms
            self.var_poll.set(str(poll_ms))
        self.app.set_config(self.app.config.with_section(
            "monitor",
            mode=self.var_mode.get() if self.var_mode.get() in ("any", "target") else "any",
            poll_ms=poll_ms,
            show_mute=bool(self.var_show_mute.get()),
            show_on_target_change=bool(self.var_show_target.get()),
        ))

    def _on_kind_change(self) -> None:
        if self._updating:
            return
        kind = self.var_kind.get()
        self._show_kind_widgets(kind)
        cfg = self.app.config
        target = dict(cfg.to_dict()["target"])
        target["kind"] = kind
        if kind == "master":
            target["master_bus"] = self._selected(self._master_map, self.cmb_master, cfg.target.master_bus)
        else:
            self._fill_channel_combo(self.app.state.info, kind)
            target["index"] = self._selected(self._channel_map, self.cmb_channel, cfg.target.index)
        self.app.set_config(cfg.replace(target=target))

    def _on_channel_change(self) -> None:
        if self._updating:
            return
        cfg = self.app.config
        target = dict(cfg.to_dict()["target"])
        target["kind"] = "bus" if self.var_kind.get() == "bus" else "strip"
        target["index"] = self._selected(self._channel_map, self.cmb_channel, cfg.target.index)
        self.app.set_config(cfg.replace(target=target))

    def _on_master_change(self) -> None:
        if self._updating:
            return
        cfg = self.app.config
        target = dict(cfg.to_dict()["target"])
        target["master_bus"] = self._selected(self._master_map, self.cmb_master, cfg.target.master_bus)
        self.app.set_config(cfg.replace(target=target))

    @staticmethod
    def _selected(mapping: list[tuple[str, int]], combo: ttk.Combobox, fallback: int) -> int:
        position = combo.current()
        if 0 <= position < len(mapping):
            return mapping[position][1]
        return fallback

    def _apply_osd_now(self) -> None:
        if self._updating:
            return
        cfg = self.app.config
        try:
            position = next((key for key, label in POSITION_LABELS.items()
                             if label == self.var_position.get()), cfg.osd.position)
            monitor = "cursor" if self.var_monitor.get().startswith("Active") else "primary"
            offset_x = int(float(self.var_offset_x.get() or 0))
            offset_y = int(float(self.var_offset_y.get() or 0))
            scale = float(self._value_vars["scale"].get()) / 100.0
            opacity = float(self._value_vars["opacity"].get()) / 100.0
            hold = float(self._value_vars["hold"].get()) / 10.0
            fade = int(float(self._value_vars["fade"].get()))
        except (KeyError, ValueError, AttributeError):
            return
        try:
            reference = float(self.var_ref.get().replace(",", "."))
        except (ValueError, AttributeError):
            reference = cfg.osd.full_scale_db
        self.app.set_config(cfg.with_section(
            "osd",
            position=position,
            monitor=monitor,
            offset_x=offset_x,
            offset_y=offset_y,
            scale=scale,
            opacity=opacity,
            hold_seconds=hold,
            fade_ms=fade,
            show_db=bool(self.var_show_db.get()),
            enabled=bool(self.var_enabled.get()),
            full_scale_db=reference,
        ))

    def _apply_demo(self) -> None:
        if self._updating:
            return
        self.chk_simulate.configure(state="normal" if self.var_demo.get() else "disabled")
        self.app.set_config(self.app.config.with_section(
            "connection",
            demo_backend=bool(self.var_demo.get()),
            simulate_unavailable=bool(self.var_simulate.get()),
        ))

    def _apply_dll(self) -> None:
        if self._updating:
            return
        path = self.var_dll.get().strip()
        if os.path.sep in path and not os.path.exists(path):
            messagebox.showwarning("DLL not found", f"{path}\n\nLeave the field empty to auto-detect.",
                                   parent=self.win)
            self.var_dll.set(self.app.config.connection.dll_path)
            return
        self.app.set_config(self.app.config.with_section("connection", dll_path=path))

    def _browse_dll(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.win, title="Select the Voicemeeter Remote API DLL",
            filetypes=[("Voicemeeter Remote API", "VoicemeeterRemote*.dll"), ("DLL files", "*.dll")],
        )
        if path:
            self.var_dll.set(path)
            self._apply_dll()

    def _auto_dll(self) -> None:
        self.var_dll.set("")
        self._apply_dll()

    def _apply_autostart(self) -> None:
        if self._updating:
            return
        self.app.set_autostart(bool(self.var_autostart.get()))

    def _launch(self) -> None:
        edition = next((key for key, label in EDITION_LABELS.items() if label == self.var_edition.get()), 2)
        self.app.set_config(self.app.config.with_section("connection", launch_edition=edition))
        self.app.launch_voicemeeter(edition)

    # ------------------------------------------------------------------ messages
    def update_level(self, state: State, config: Config) -> None:
        """Cheap refresh during a level change; does not rebuild widgets."""
        if self.win is None or not self.win.winfo_exists() or not self.visible:
            return
        self._refresh_level(state, config)

    def show_test_report(self, ok: bool, report: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Connection test")
        dialog.transient(self.win if self.win is not None else self.root)
        dialog.geometry("660x500")
        frame = ttk.Frame(dialog, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="PASSED" if ok else "FAILED",
                  font=tkfont.Font(size=12, weight="bold"),
                  foreground="#2f9e44" if ok else "#c92a2a").pack(anchor="w")
        text = tk.Text(frame, wrap="word", height=22, borderwidth=1, relief="solid")
        text.pack(fill="both", expand=True, pady=(8, 10))
        text.insert("1.0", report)
        text.configure(state="disabled")
        ttk.Button(frame, text="Close", command=dialog.destroy).pack(anchor="e")
        dialog.grab_set()
