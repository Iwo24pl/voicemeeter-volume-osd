"""The on-screen display.

It is a per-pixel-alpha layered window (``UpdateLayeredWindow``), so rounded
corners, the drop shadow and the text are anti-aliased and the card floats over
any application without a rectangular background.  The window is
``WS_EX_NOACTIVATE | WS_EX_TRANSPARENT``: it never takes focus and never
intercepts mouse clicks.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from typing import Callable

import tkinter as tk
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .config import Config
from .levels import format_db
from .winapi import cursor_position, monitor_for_point, primary_monitor

_LOGGER = logging.getLogger(__name__)

# ---- design metrics (1.0 scale, before DPI/scale multiplication) ----------
CARD_W = 400
CARD_H = 112
SHADOW = 26
CARD_RADIUS = 18

SS = 2  # supersampling factor for smooth corners/text

# ---- colours (RGBA) -------------------------------------------------------
CARD_BG = (15, 17, 22, 244)
CARD_BORDER = (255, 255, 255, 20)
TEXT = (242, 244, 248, 255)
TEXT_MUTED = (139, 147, 167, 255)
TRACK = (255, 255, 255, 26)
ACCENT = (122, 162, 247, 255)
ACCENT_BOOST = (242, 179, 107, 255)
ACCENT_WARNING = (240, 128, 128, 255)
MUTED_VALUE = (238, 158, 158, 255)


@dataclass(frozen=True)
class OsdModel:
    name: str = "Voicemeeter"
    kicker: str = "VOICEMEETER"
    percent: int | None = 0
    db: float | None = 0.0
    muted: bool = False
    show_db: bool = True
    warning: bool = False
    detail: str = ""


@dataclass(frozen=True)
class OsdPosition:
    x: int
    y: int
    width: int
    height: int


# ------------------------------------------------------------------ drawing
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_FONT_DIR = None


def _font_names() -> tuple[str, str]:
    import os

    global _FONT_DIR
    if _FONT_DIR is None:
        _FONT_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    return (
        os.path.join(_FONT_DIR, "seguisb.ttf"),  # Segoe UI Semibold
        os.path.join(_FONT_DIR, "segoeui.ttf"),   # Segoe UI
    )


def _font(semibold: bool, size: float):
    path_semi, path_regular = _font_names()
    path = path_semi if semibold else path_regular
    pixels = max(1, int(round(size * SS)))
    key = (path, pixels)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(path, pixels)
    except OSError:
        try:
            font = ImageFont.truetype("segoeui.ttf", pixels)
        except OSError:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _rounded(draw: ImageDraw.ImageDraw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(
        [value * SS for value in box],
        radius=max(0.0, radius * SS),
        fill=fill,
        outline=outline,
        width=max(1, int(round(width * SS))),
    )


def _spaced_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, spacing: float):
    x, y = xy[0] * SS, xy[1] * SS
    for char in text:
        draw.text((x, y), char, font=font, fill=fill, anchor="lt")
        x += draw.textlength(char, font=font) + spacing * SS


def _speaker_icon(draw: ImageDraw.ImageDraw, x: float, y: float, size: float, color):
    """Speaker body + cone + two sound arcs, all scaled."""
    s = size
    body_x = x + s * 0.11
    body_y = y + s * 0.36
    body_w = s * 0.17
    body_h = s * 0.28
    draw.rounded_rectangle(
        [body_x * SS, body_y * SS, (body_x + body_w) * SS, (body_y + body_h) * SS],
        radius=1.2 * SS,
        fill=color,
    )
    cone_x = body_x + body_w
    draw.polygon(
        [
            (cone_x * SS, body_y * SS),
            ((x + s * 0.52) * SS, (y + s * 0.20) * SS),
            ((x + s * 0.52) * SS, (y + s * 0.80) * SS),
            (cone_x * SS, (body_y + body_h) * SS),
        ],
        fill=color,
    )
    center_x, center_y = x + s * 0.54, y + s * 0.50
    for radius, width in ((s * 0.20, 1.7), (s * 0.32, 1.5)):
        draw.arc(
            [(center_x - radius) * SS, (center_y - radius) * SS,
             (center_x + radius) * SS, (center_y + radius) * SS],
            start=-52, end=52, fill=color, width=max(1, int(round(width * SS))),
        )


def render_osd(model: OsdModel, scale: float = 1.0) -> Image.Image:
    """Draw the whole card (shadow included) at ``scale``; returns RGBA pixels."""
    scale = max(0.5, min(2.5, float(scale)))
    card_w, card_h = CARD_W * scale, CARD_H * scale
    pad = SHADOW * scale
    width = int(round(card_w + pad * 2))
    height = int(round(card_h + pad * 2))
    image = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))

    # soft drop shadow
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    _rounded(shadow_draw, (pad, pad + 4 * scale, pad + card_w, pad + card_h + 4 * scale),
             CARD_RADIUS * scale, fill=(0, 0, 0, 150))
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(9 * scale * SS)))

    draw = ImageDraw.Draw(image)
    cx, cy = pad, pad
    _rounded(draw, (cx, cy, cx + card_w, cy + card_h), CARD_RADIUS * scale, fill=CARD_BG)
    _rounded(draw, (cx, cy, cx + card_w, cy + card_h), CARD_RADIUS * scale,
             outline=CARD_BORDER, width=1)

    inner = 20 * scale
    icon_size = 34 * scale
    icon_x = cx + inner
    icon_y = cy + card_h / 2 - icon_size / 2

    over_reference = model.percent is not None and model.percent > 100
    if model.warning:
        accent = ACCENT_WARNING
    elif model.muted:
        accent = TEXT_MUTED
    elif over_reference:
        accent = ACCENT_BOOST
    else:
        accent = ACCENT

    plate = accent[:3] + (34,)
    _rounded(draw, (icon_x, icon_y, icon_x + icon_size, icon_y + icon_size),
             10 * scale, fill=plate)
    _speaker_icon(draw, icon_x + 1, icon_y + 1, icon_size - 2,
                  TEXT if not (model.muted or model.warning) else TEXT_MUTED)

    text_x = icon_x + icon_size + 14 * scale
    kicker_font = _font(False, 9.5 * scale)
    _spaced_text(draw, (text_x, cy + 20 * scale), model.kicker.upper(),
                 kicker_font, TEXT_MUTED, 1.15 * scale)

    name_font = _font(True, 13.5 * scale)
    right_reserve = 86 * scale
    max_name = max(40.0, (cx + card_w - inner) - text_x - right_reserve)
    name = model.name
    if draw.textlength(name, font=name_font) > max_name * SS:
        while name and draw.textlength(name + "…", font=name_font) > max_name * SS:
            name = name[:-1]
        name = name.rstrip() + "…"
    draw.text((text_x * SS, (cy + 38 * scale) * SS), name, font=name_font,
              fill=TEXT_MUTED if (model.muted or model.warning) else TEXT, anchor="lt")

    right_x = cx + card_w - inner
    if model.warning:
        value, unit, unit_font, value_font = "—", "", None, _font(True, 26 * scale)
        value_color = ACCENT_WARNING
    elif model.muted:
        value, unit, unit_font, value_font = "MUTED", "", None, _font(True, 15 * scale)
        value_color = MUTED_VALUE
    else:
        value = str(model.percent if model.percent is not None else 0)
        unit = "%"
        unit_font = _font(False, 13 * scale)
        value_font = _font(True, 29 * scale)
        value_color = TEXT

    unit_width = draw.textlength(unit, font=unit_font) * SS if unit else 0
    baseline = cy + 76 * scale
    if unit:
        draw.text((right_x * SS, baseline * SS), unit, font=unit_font,
                  fill=TEXT_MUTED, anchor="rb")
    draw.text(((right_x - unit_width / SS - (3 * scale if unit else 0)) * SS, baseline * SS),
              value, font=value_font, fill=value_color, anchor="rb")

    if model.show_db and model.db is not None and not model.muted and not model.warning:
        db_font = _font(False, 10 * scale)
        draw.text((right_x * SS, (cy + 20 * scale) * SS), format_db(model.db),
                  font=db_font, fill=ACCENT_BOOST if over_reference else TEXT_MUTED, anchor="rt")

    # level bar
    bar_x0, bar_x1 = cx + inner, cx + card_w - inner
    bar_y = cy + card_h - 22 * scale
    bar_h = 5 * scale
    _rounded(draw, (bar_x0, bar_y, bar_x1, bar_y + bar_h), bar_h / 2, fill=TRACK)
    if not model.muted and not model.warning and model.percent is not None:
        # Over 100 % the bar is simply full; the number and colour carry the rest.
        fraction = max(0.0, min(1.0, model.percent / 100.0))
        fill_w = fraction * (bar_x1 - bar_x0)
        if fill_w > 0.4:
            fill_w = max(fill_w, bar_h)
            _rounded(draw, (bar_x0, bar_y, bar_x0 + fill_w, bar_y + bar_h), bar_h / 2, fill=accent)
            knob_x = bar_x0 + fill_w
            knob_r = 3.5 * scale
            draw.ellipse(
                [(knob_x - knob_r) * SS, (bar_y + bar_h / 2 - knob_r) * SS,
                 (knob_x + knob_r) * SS, (bar_y + bar_h / 2 + knob_r) * SS],
                fill=(255, 255, 255, 235),
            )

    return image.resize((width, height), Image.LANCZOS)


def premultiply(image: Image.Image) -> Image.Image:
    """Straight RGBA -> premultiplied RGBA, as UpdateLayeredWindow expects."""
    red, green, blue, alpha = image.convert("RGBA").split()
    return Image.merge("RGBA", (
        ImageChops.multiply(red, alpha),
        ImageChops.multiply(green, alpha),
        ImageChops.multiply(blue, alpha),
        alpha,
    ))


# ------------------------------------------------------------------ window
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
SW_SHOWNOACTIVATE = 4
SW_HIDE = 0
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
ULW_ALPHA = 2


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_user32.GetDC.restype = wt.HDC
_user32.GetDC.argtypes = [wt.HWND]
_user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p
_user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]
_user32.GetWindowLongPtrW.restype = ctypes.c_void_p
_user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
_user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE),
    wt.HDC, ctypes.POINTER(wt.POINT), wt.DWORD, ctypes.POINTER(_BLENDFUNCTION), wt.DWORD,
]
_user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, wt.UINT]
_gdi32.CreateCompatibleDC.restype = wt.HDC
_gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
_gdi32.CreateDIBSection.restype = ctypes.c_void_p
_gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint]
_gdi32.SelectObject.restype = ctypes.c_void_p
_gdi32.SelectObject.argtypes = [wt.HDC, ctypes.c_void_p]
_gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
_gdi32.DeleteDC.argtypes = [wt.HDC]


class OsdWindow:
    """Owns one hidden layered window and animates the card in/out."""

    def __init__(self, root: tk.Tk, get_config: Callable[[], Config]):
        self._root = root
        self._get_config = get_config
        self._top: tk.Toplevel | None = None
        self._hwnd = 0
        self._screen_dc = None
        self._mem_dc = None
        self._bitmap = None
        self._original_bitmap = None
        self._bits = None
        self._surface_size = (0, 0)
        self._image: Image.Image | None = None
        self._premul: Image.Image | None = None
        self._model: OsdModel | None = None
        self._position = OsdPosition(0, 0, 0, 0)
        self._timer = None
        self._state = "hidden"       # hidden | in | hold | out
        self._phase_start = 0.0
        self._hold_ms = 1800
        self._fallback = False
        self._fallback_frame = None
        self._fallback_labels = (None, None)
        self._first_upload_failed = False

    # -- public API --------------------------------------------------------
    @property
    def visible(self) -> bool:
        return self._state != "hidden"

    def show(self, model: OsdModel, sticky_ms: int | None = None) -> None:
        cfg = self._get_config().osd
        if not cfg.enabled:
            return
        try:
            self._prepare(model, cfg.scale)
            self._hold_ms = int(sticky_ms if sticky_ms is not None else cfg.hold_seconds * 1000)
            self._cancel()
            self._state = "in"
            self._phase_start = time.monotonic()
            if self._fallback:
                self._show_fallback(model, cfg)
                self._timer = self._root.after(max(250, self._hold_ms), self._finish_fallback)
                return
            self._upload(0.0, dy=10 * cfg.scale)
            self._show_window()
            self._timer = self._root.after(16, self._tick)
        except Exception:
            _LOGGER.exception("Could not show the OSD")

    def update(self, model: OsdModel) -> None:
        """Refresh content of a visible OSD (a fader moved in Voicemeeter)."""
        if self._state == "hidden" or self._model is None:
            self.show(model)
            return
        cfg = self._get_config().osd
        try:
            if model == self._model:
                # Same value on screen (e.g. repeated reads): only extend the hold.
                self._restart_hold()
                return
            self._prepare(model, cfg.scale)
            if self._fallback:
                self._show_fallback(model, cfg)
            else:
                self._upload(cfg.opacity, dy=0)
            self._restart_hold()
        except Exception:
            _LOGGER.exception("Could not update the OSD")

    def _restart_hold(self) -> None:
        self._cancel()
        self._state = "hold"
        self._phase_start = time.monotonic()
        self._timer = self._root.after(self._hold_ms, self._begin_fade)

    def hide(self) -> None:
        self._cancel()
        self._state = "hidden"
        if self._hwnd:
            try:
                _user32.ShowWindow(self._hwnd, SW_HIDE)
            except Exception:
                pass
        if self._fallback_frame is not None:
            try:
                self._top.withdraw()
            except Exception:
                pass

    def close(self) -> None:
        self.hide()
        if self._hwnd:
            try:
                _user32.SetWindowLongPtrW(self._hwnd, GWL_EXSTYLE, 0)
            except Exception:
                pass
        self._free_surface()
        if self._screen_dc:
            try:
                _user32.ReleaseDC(None, self._screen_dc)
            except Exception:
                pass
            self._screen_dc = None
        if self._top is not None:
            try:
                self._top.destroy()
            except Exception:
                pass
            self._top = None

    # -- preparation -------------------------------------------------------
    def _prepare(self, model: OsdModel, scale: float) -> None:
        image = render_osd(model, scale)
        self._image = image
        self._premul = premultiply(image)
        self._model = model
        if self._fallback:
            return
        if self._top is None or not self._hwnd:
            self._create_window()
        self._ensure_surface(image.size)
        self._position = self._compute_position(image.size)
        if self._top is not None:
            # Keep Tk's idea of the geometry in sync; UpdateLayeredWindow positions
            # the window itself, but Tk must not move it back on the next update.
            self._top.geometry(
                f"{image.width}x{image.height}{self._position.x:+d}{self._position.y:+d}"
            )

    def _create_window(self) -> None:
        self._top = tk.Toplevel(self._root)
        self._top.title("VMP-OSD-Overlay")
        self._top.overrideredirect(True)
        self._top.geometry("1x1+-4000+-4000")
        self._top.update_idletasks()
        self._hwnd = int(self._top.wm_frame(), 16)
        ex_style = _user32.GetWindowLongPtrW(self._hwnd, GWL_EXSTYLE) or 0
        _user32.SetWindowLongPtrW(
            self._hwnd,
            GWL_EXSTYLE,
            ex_style | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
        )
        self._screen_dc = _user32.GetDC(None)
        self._mem_dc = _gdi32.CreateCompatibleDC(self._screen_dc)

    def _ensure_surface(self, size: tuple[int, int]) -> None:
        if size == self._surface_size and self._bitmap:
            return
        width, height = size
        info = _BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height          # top-down rows
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0           # BI_RGB
        bits = ctypes.c_void_p()
        bitmap = _gdi32.CreateDIBSection(
            self._mem_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
        )
        if not bitmap:
            raise OSError(f"CreateDIBSection failed: {ctypes.WinError(ctypes.get_last_error())}")
        old = _gdi32.SelectObject(self._mem_dc, bitmap)
        if self._original_bitmap is None:
            self._original_bitmap = old
        if self._bitmap:
            _gdi32.DeleteObject(self._bitmap)
        self._bitmap, self._bits = bitmap, bits
        self._surface_size = size

    def _free_surface(self) -> None:
        if self._mem_dc and self._bitmap:
            try:
                _gdi32.SelectObject(self._mem_dc, self._original_bitmap)
            except Exception:
                pass
        if self._bitmap:
            try:
                _gdi32.DeleteObject(self._bitmap)
            except Exception:
                pass
        self._bitmap = None
        self._original_bitmap = None
        self._bits = None
        if self._mem_dc:
            try:
                _gdi32.DeleteDC(self._mem_dc)
            except Exception:
                pass
            self._mem_dc = None
        self._surface_size = (0, 0)

    def _compute_position(self, size: tuple[int, int]) -> OsdPosition:
        cfg = self._get_config().osd
        width, height = size
        monitor = None
        if cfg.monitor == "primary":
            monitor = primary_monitor()
        else:
            monitor = monitor_for_point(*cursor_position()) or primary_monitor()
        if monitor is None:
            monitor = type("M", (), {"work": (0, 0, 1920, 1080)})()
        left, top, right, bottom = monitor.work
        margin = int(round(26 * cfg.scale))

        horizontal = cfg.position.split("-")[-1]
        vertical = cfg.position.split("-")[0]
        if horizontal == "left":
            x = left + margin
        elif horizontal == "right":
            x = right - width - margin
        else:
            x = left + (right - left - width) // 2

        if vertical == "top":
            y = top + margin
        elif vertical == "bottom":
            y = bottom - height - margin
        else:
            y = top + (bottom - top - height) // 2

        x += int(cfg.offset_x)
        y += int(cfg.offset_y)
        # Keep the whole card on screen even with extreme offsets.
        x = max(left + 6, min(right - width - 6, x))
        y = max(top + 6, min(bottom - height - 6, y))
        return OsdPosition(int(x), int(y), width, height)

    # -- animation ---------------------------------------------------------
    def _cancel(self) -> None:
        if self._timer is not None:
            try:
                self._root.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def _tick(self) -> None:
        self._timer = None
        if self._state == "hidden":
            return
        cfg = self._get_config().osd
        now = time.monotonic()

        if self._state == "in":
            duration = 0.16
            progress = min(1.0, (now - self._phase_start) / duration)
            eased = 1.0 - (1.0 - progress) ** 3
            self._upload(cfg.opacity * eased, dy=(1.0 - eased) * 10 * cfg.scale)
            if progress < 1.0:
                self._timer = self._root.after(16, self._tick)
            else:
                self._state = "hold"
                self._phase_start = now
                self._timer = self._root.after(self._hold_ms, self._begin_fade)
            return

        if self._state == "hold":
            self._begin_fade()
            return

        if self._state == "out":
            fade = max(0.01, cfg.fade_ms / 1000.0)
            progress = min(1.0, (now - self._phase_start) / fade)
            self._upload(cfg.opacity * (1.0 - progress), dy=0)
            if progress < 1.0:
                self._timer = self._root.after(16, self._tick)
            else:
                self.hide()

    def _begin_fade(self) -> None:
        self._timer = None
        cfg = self._get_config().osd
        if cfg.fade_ms <= 0:
            self.hide()
            return
        self._state = "out"
        self._phase_start = time.monotonic()
        self._timer = self._root.after(16, self._tick)

    # -- GDI upload --------------------------------------------------------
    def _upload(self, opacity: float, dy: float) -> None:
        if self._premul is None or not self._hwnd or not self._bits:
            return
        opacity = max(0.0, min(1.0, float(opacity)))
        frame = self._premul if opacity >= 0.999 else self._premul.point(lambda v: int(v * opacity))
        data = frame.tobytes("raw", "BGRA")  # already premultiplied
        if len(data) != self._surface_size[0] * self._surface_size[1] * 4:
            return
        ctypes.memmove(self._bits, data, len(data))

        point = wt.POINT(int(self._position.x), int(self._position.y + dy))
        size = wt.SIZE(self._position.width, self._position.height)
        source = wt.POINT(0, 0)
        blend = _BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, AC_SRC_ALPHA
        ok = _user32.UpdateLayeredWindow(
            self._hwnd, self._screen_dc, ctypes.byref(point), ctypes.byref(size),
            self._mem_dc, ctypes.byref(source), 0, ctypes.byref(blend), ULW_ALPHA,
        )
        if not ok and not self._first_upload_failed:
            self._first_upload_failed = True
            _LOGGER.warning("UpdateLayeredWindow failed (%s); using the plain fallback",
                            ctypes.WinError(ctypes.get_last_error()))
            self._enable_fallback()

    def _show_window(self) -> None:
        try:
            _user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            _user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        except Exception:
            _LOGGER.exception("Could not show the OSD window")

    # -- fallback (very old/broken compositing) ----------------------------
    def _enable_fallback(self) -> None:
        self._fallback = True
        self._free_surface()
        if self._top is None:
            return
        try:
            ex_style = _user32.GetWindowLongPtrW(self._hwnd, GWL_EXSTYLE) or 0
            ex_style &= ~(WS_EX_LAYERED | WS_EX_TRANSPARENT)
            _user32.SetWindowLongPtrW(self._hwnd, GWL_EXSTYLE, ex_style)
        except Exception:
            pass
        try:
            self._top.deiconify()
            self._top.overrideredirect(True)
            self._top.attributes("-alpha", 0.96)
            self._top.configure(bg="#0F1116")
            self._fallback_frame = tk.Frame(self._top, bg="#0F1116")
            self._fallback_frame.pack(fill="both", expand=True)
            title = tk.Label(self._fallback_frame, bg="#0F1116", fg="#8B93A7",
                             font=("Segoe UI", 9))
            value = tk.Label(self._fallback_frame, bg="#0F1116", fg="#F2F4F8",
                             font=("Segoe UI Semibold", 26))
            title.pack(anchor="w", padx=16, pady=(14, 0))
            value.pack(anchor="e", padx=16)
            self._fallback_labels = (title, value)
        except Exception:
            _LOGGER.exception("Fallback OSD could not be created")

    def _show_fallback(self, model: OsdModel, cfg) -> None:
        if not self._fallback_labels or self._top is None:
            return
        title, value = self._fallback_labels
        title.configure(text=f"{model.kicker}  ·  {model.name}")
        if model.muted:
            text = "MUTED"
        elif model.percent is None:
            text = "—"
        else:
            text = f"{model.percent}%" + (f"   {format_db(model.db)}" if model.show_db and model.db is not None else "")
        value.configure(text=text)
        width, height = self._position.width, self._position.height
        self._top.geometry(f"{width}x{height}{self._position.x:+d}{self._position.y:+d}")
        self._top.deiconify()
        self._top.attributes("-topmost", True)
        try:
            self._top.attributes("-alpha", 0.96)
        except Exception:
            pass

    def _finish_fallback(self) -> None:
        self._timer = None
        self.hide()
