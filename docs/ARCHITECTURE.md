# Architecture

## Goals and the one big decision

The app exists to answer one question at a glance: *what did Voicemeeter just
set that fader to?* It therefore **observes** Voicemeeter and does nothing else.

An earlier iteration installed a `WH_KEYBOARD_LL` hook to capture the Windows
volume keys and wrote the gain itself. That worked, but it meant:

* the app had to swallow keys globally (a system-wide hook),
* Windows' volume keys stopped doing anything else while the app ran,
* and the app needed a write path into the user's mixer.

That is not what this app is for. The hook, the hotkeys and every
`VBVMR_SetParameterFloat` call were removed. The result is smaller, safer and
cannot disturb the audio setup: no keys are consumed, no state is changed.

## Runtime structure

```
┌──────────────────────── Tk main thread ────────────────────────┐
│  Tk root (withdrawn)                                            │
│    ├── SettingsWindow (lazy Toplevel)                           │
│    ├── OsdWindow (layered Toplevel, UpdateLayeredWindow)        │
│    └── event pump: queue.Queue[Event] drained every 30 ms       │
│                                                                 │
│  pystray thread ── posts callables to a UI queue ───────────────┘
└────────────────────────────────────────────────────────────────┘
                  ▲ level / state events
┌─────────────────┴──────── monitor thread ──────────────────────┐
│  command queue (reconnect, config, test, demo bump)            │
│  poll loop: IsParametersDirty → read gains/mutes → diff        │
│  reconnect loop: Login / GetVoicemeeterType                    │
└────────────────────────────────────────────────────────────────┘
                  ▲ ctypes
        VoicemeeterRemote64.dll
```

Why threads:

* `VBVMR_IsParametersDirty` and `GetLevel` are documented as single-thread-only;
  keeping every API call on the monitor thread satisfies that and removes a
  whole class of races.
* The UI never blocks on a DLL call or a reconnect.
* The tray library runs its own message loop; it only posts callables into the
  Tk thread.

## Change detection

`Controller._poll()` runs at `monitor.poll_ms` (default 50 ms):

1. `VBVMR_IsParametersDirty()` → `0` means nothing new, `-2` means Voicemeeter is
   gone (disconnect and retry).
2. Read the watched channels: every strip + bus, or just the selected one.
   Read `Gain` and (optionally) `Mute`.
3. Compare with the previous snapshot, keyed by `(kind, index)`.
4. A delta ≥ 0.05 dB or a mute change is a candidate.
5. If more than three channels changed in one cycle, treat it as a preset load
   and only rebase the snapshot.
6. Otherwise report the largest delta as one `level` event.
7. If the changed channel is the selected target, also refresh the `state`.

Two subtleties handled explicitly:

* **Voicemeeter's read cache lags one engine cycle.** Right after `Login`,
  `GetParameterFloat` can still return the previous/zero values. The app
  therefore ignores detected changes for 0.6 s after connecting and takes those
  reads as the baseline, and it never displays a value it has not read.
* **Continuous drags.** Every poll that shows a different value posts an event,
  so the popup follows the fader at up to 20 Hz; the OSD only re-renders when
  the rounded percentage, name, mute or warning state actually changes, and each
  event restarts the hide timer.

## Percentage mapping

Voicemeeter exposes dB, not a 0–100 volume. The fader range is −60 … +12 dB
(documentation and `voicemeeterlib` agree). The 100 % point is configurable
(`osd.full_scale_db`, default 0 dB); the scale is linear in dB from the fader
bottom to that reference and continues above it:

```
percent = (dB + 60) / (reference + 60) * 100
```

With the default reference: −60 dB = 0 %, −6 dB = 90 %, 0 dB = 100 %,
+6 dB = 110 %, +12 dB = 120 %. The bar caps at 100 %, but the number keeps
growing, and above the reference the bar, dB text and accent turn amber.

This is linear in dB, like the motion of the Voicemeeter fader and like human
loudness perception. The precise dB is drawn as well, so the number is never
ambiguous — and it is never a Windows endpoint volume.

## The OSD window

A normal Tk window cannot do per-pixel transparency, so the OSD is a Tk
`Toplevel` used only for its HWND:

* `overrideredirect(True)`, `WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE |
  WS_EX_TRANSPARENT` (no focus, no taskbar, no Alt-Tab, click-through).
* The image is rendered with Pillow at 2× and downsampled with Lanczos:
  rounded card, blurred shadow, Segoe UI Semibold value, fader bar.
* Alpha is premultiplied (`ImageChops.multiply` with the alpha channel), uploaded
  to a 32-bit top-down DIB, and displayed with `UpdateLayeredWindow`.
* Animation is a small state machine (`in → hold → out`) driven by
  `Tk.after(16)`; fade-out scales the already premultiplied bytes (linear in
  premultiplied alpha, so it is correct and cheap).
* Fallback: if `UpdateLayeredWindow` fails, a plain bordered Toplevel with a
  label is used, so the app still works on unusual systems.

The window is positioned relative to the work area (taskbar excluded) of the
monitor under the cursor or the primary monitor, clamped so extreme offsets
cannot push it off screen.

## Failure handling

| Failure | Behaviour |
|---|---|
| DLL missing | clear status and the searched paths in Settings; retry every 6 s |
| Voicemeeter not running | "Waiting for Voicemeeter to start…"; retry every 1.2 s |
| Voicemeeter closed while running | `-2` from the API → logout, status change, retry until it returns |
| one parameter unreadable | log and skip that channel, keep monitoring |
| preset load (many changes) | rebase without popping up |
| tray unavailable | settings window opens; the app remains controllable |
| config unreadable/corrupt | defaults; a warning in the log |
| OSD upload failure | plain-window fallback |

No exception from the DLL or the UI can reach `main()`: the monitor loop, the
event pump and the Tk callback handler all log and continue.

## Read-only guarantee

`RealClient` and `MockClient` have no write methods at all. The ctypes wrapper in
`app/vmapi.py` deliberately exposes only getters, plus `VBVMR_RunVoicemeeter`
(which starts the Voicemeeter GUI, an explicit user action from Settings). A
unit test asserts that neither client exposes `write_gain` or `write_mute`.

## Configuration

`config.py` is a dataclass tree with a tolerant parser: missing keys get
defaults, unknown keys (including the old `keys`/`step_db` sections) are
ignored, numbers are clamped. Writes are atomic (`mkstemp` + `os.replace`) so a
crash mid-save cannot leave a broken file.

## Packaging

`build_exe.bat` uses PyInstaller `--onefile --windowed`, includes the icon and
assets, and adds `pystray._win32` as a hidden import (pystray loads its backend
dynamically). The frozen app resolves its data directory and log through the
same `config.py` helpers.
