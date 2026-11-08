<p align="center">
  <img src="assets/icon.png" alt="" width="88">
</p>

<h1 align="center">VoiceMeeter Volume OSD</h1>

<p align="center">
  A clean, read-only on-screen display for Voicemeeter faders on Windows.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6?logo=windows&logoColor=white" alt="Windows 10 / 11">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-2ea44f" alt="MIT">
</p>

<p align="center">
  <img src="assets/preview/osd-normal.png" width="452" alt="The popup showing Bus A1 at -12 dB and 80 %">
</p>

VoiceMeeter Volume OSD shows a small, unobtrusive popup whenever a Voicemeeter
fader changes — whether it was moved in the Voicemeeter window, from its MIDI or
keyboard mapping, or by a physical control surface.

It is a **pure observer**: it reads the official Remote API, never writes a
parameter, never intercepts the keyboard and never touches Windows' endpoint
volume. There is no driver, no injection, no telemetry and no network access.

---

## Highlights

- **Follows Voicemeeter, not the keyboard.** No global hooks, no swallowed
  media keys, no interference with Windows volume.
- **Names the channel** that moved: `Master · A1`, `Bus B2`, `Mic1`, and the
  device behind it.
- **Configurable 100 % point** — 0 dB by default, so boosts read over 100 %
  (e.g. +6 dB → 110 %).
- **Native-feeling overlay**: per-pixel transparency, rounded corners, soft
  shadow, never takes focus, never blocks clicks.
- **Nine positions**, size, opacity, hold time, fade time, cursor or primary
  monitor.
- **Tray-first**: settings window, target shortcuts, autostart, single instance.
- **Resilient**: survives Voicemeeter restarts, reports missing DLLs clearly,
  and keeps running when the connection drops.
- **Testable without Voicemeeter** through the built-in demo backend.

---

## Screenshots

| Normal | Above the 0 dB reference |
|---|---|
| ![Normal](assets/preview/osd-normal.png) | ![Boost](assets/preview/osd-boost.png) |
| **Muted** | **Voicemeeter unavailable** |
| ![Muted](assets/preview/osd-muted.png) | ![Warning](assets/preview/osd-warning.png) |

| Settings — Watching | Settings — Appearance |
|---|---|
| ![Settings](assets/settings.png) | ![Appearance](assets/settings-appearance.png) |

---

## How it works

```
Voicemeeter (GUI + audio engine)
        │  shared-memory Remote API
        ▼
VoicemeeterRemote64.dll
        │  VBVMR_IsParametersDirty + VBVMR_GetParameterFloat
        ▼
monitor thread ── change detected ──► Tk thread ──► layered OSD window
        │                                  │
        └── reconnect loop                 └── tray icon + settings
```

1. Every **50 ms** (25–250 ms, configurable) the monitor calls
   `VBVMR_IsParametersDirty()`.
2. When Voicemeeter reports a change, it reads the watched gains and mutes.
3. Values are compared with the previous snapshot. A delta of **≥ 0.05 dB** or
   a mute toggle produces one event; the fader that moved most wins, so linked
   faders produce a single popup.
4. Changes to more than three channels at once (loading a preset) rebase the
   snapshot silently instead of flooding the screen.
5. The overlay redraws and restarts its hide timer. Dragging a fader is
   followed at up to 20 Hz.

Voicemeeter serves parameter reads from a cache that updates on its engine
cycle, so the app takes a baseline and ignores transient reads for 0.6 s after
connecting. The popup never shows a value that was not read from Voicemeeter.

### Read-only by design

`app/vmapi.py` binds only getters. The backends expose no write methods, and a
unit test enforces it. The app cannot change your mix, your mute states or your
Windows volume, even by accident.

### Why there is no keyboard hook

Intercepting the Windows volume keys requires a system-wide low-level hook and
suppressing the keys. That breaks the normal volume behaviour for every other
application. This project intentionally does not do it: configure the knob
inside Voicemeeter (MIDI mapping, keyboard shortcuts / MacroButtons, or a
control-surface app) and the OSD will report exactly what the mixer does.

---

## Requirements

| | |
|---|---|
| OS | Windows 10 or 11 (x64 or ARM64) |
| Mixer | Voicemeeter, Voicemeeter Banana or Voicemeeter Potato, **running** |
| Python | 3.10 or newer, same bitness as Voicemeeter (64-bit is typical) |
| Packages | `pystray`, `Pillow` (installed from `requirements.txt`) |
| UI | tkinter, bundled with the python.org installer |

The Remote API is provided by the Voicemeeter **GUI** process. If the GUI is not
running there is nothing to read, and the app says so and keeps retrying.

---

## Installation

### From source

```powershell
git clone https://github.com/Iwo24pl/voicemeeter-volume-osd.git
cd voicemeeter-volume-osd
python -m pip install -r requirements.txt
python main.py --settings
```

`run.bat` starts it with a console; `run-hidden.vbs` starts it minimised in the
tray.

### Prebuilt executable

Download `VoiceMeeterPopup.exe` from
[Releases](https://github.com/Iwo24pl/voicemeeter-volume-osd/releases) (published
by the tag workflow) or build it yourself with `build_exe.bat`.
The executable is unsigned, so SmartScreen may ask once.

### First run

The settings window opens. Pick **Any fader that moves** (default) or a fixed
channel, then move a fader in Voicemeeter. The app stays in the notification
area afterwards; double-click the tray icon for settings.

---

## Settings

### Watching

| Option | Meaning |
|---|---|
| Any fader that moves | Show whichever strip/bus changed, with its name. Recommended. |
| Only the selected channel | Watch one Master/bus/strip. |
| Master | A bus you choose (A1 by default), displayed as `Master · A1`. |
| Show mute changes | Announce mute/unmute with `MUTED`. |
| Show on selection change | Briefly preview the newly selected channel. |
| Update interval | `VBVMR_IsParametersDirty` polling period, 25–250 ms. |

### Appearance

Position (nine anchors) plus X/Y offsets, size 60–200 %, opacity 25–100 %,
hold 0.3–15 s, fade 0–800 ms, active or primary monitor, optional dB line, and
the **100 % at** reference (−40…+12 dB).

### Connection

Live status, connection test (read-only), reconnect, optional Voicemeeter
launch, explicit `VoicemeeterRemote*.dll` path with auto-detection, demo
backend, and the per-user autostart toggle.

---

## Percentage reference

The percentage is linear in dB and anchored at a configurable reference
(`osd.full_scale_db`, default **0 dB**):

```
percent = (dB + 60) / (reference + 60) × 100
```

| dB | with 0 dB reference |
|---:|---:|
| +12.0 | 120 % |
| +6.0 | 110 % |
| **0.0** | **100 %** |
| −6.0 | 90 % |
| −24.0 | 60 % |
| −60.0 | 0 % |

Above the reference the bar stays full, the number keeps growing and the accent
turns amber. Setting the reference to −6 dB makes −6 dB read 100 % and 0 dB read
111 %; setting it to +12 dB makes 0 dB read 83 %. The exact dB value is always
shown next to the percentage.

---

## Configuration file

`%LOCALAPPDATA%\VoiceMeeterPopup\config.json` — created on first launch, written
atomically, and validated on load. Unknown or invalid values are clamped or
ignored, so the app always starts.

| Section | Keys |
|---|---|
| `target` | `kind` (`master`/`bus`/`strip`), `index`, `master_bus` |
| `monitor` | `mode` (`any`/`target`), `poll_ms`, `show_mute`, `show_on_target_change` |
| `osd` | `enabled`, `position`, `offset_x`, `offset_y`, `scale`, `opacity`, `hold_seconds`, `fade_ms`, `show_db`, `monitor`, `full_scale_db` |
| `connection` | `dll_path`, `demo_backend`, `simulate_unavailable`, `launch_edition` |
| `general` | `start_with_windows`, `start_minimized` |

Logs: `%LOCALAPPDATA%\VoiceMeeterPopup\app.log` (512 KB × 2, rotating).

---

## Remote API reference

All calls run on one monitor thread, as the API documentation requires.

| Function | Use |
|---|---|
| `VBVMR_Login` / `VBVMR_Logout` | connect / disconnect (`0` OK, `1` OK but app not started) |
| `VBVMR_GetVoicemeeterType` | Basic / Banana / Potato, strip and bus counts |
| `VBVMR_GetVoicemeeterVersion` | shown in the UI |
| `VBVMR_IsParametersDirty` | poll: `1` changed, `−2` Voicemeeter gone |
| `VBVMR_GetParameterFloat` | read `Gain` and `Mute` |
| `VBVMR_GetParameterStringA` | read `Label` and `device.name` |
| `VBVMR_RunVoicemeeter` | optional “Launch Voicemeeter” button |

Parameters:

```
Strip[i].Gain   Strip[i].Mute   Strip[i].Label   Strip[i].device.name
Bus[i].Gain     Bus[i].Mute     Bus[i].Label     Bus[i].device.name
```

Indexes: Basic 3 strips / 2 buses, Banana 5 / 5, Potato 8 / 8.

### DLL discovery order

1. Settings → Connection
2. `VOICEMEETER_REMOTE_DLL`
3. Next to the app
4. Uninstall registry entries (`InstallLocation`, `DisplayIcon`)
5. `C:\Program Files\VB\Voicemeeter*` and `C:\Program Files (x86)\VB\Voicemeeter*`
6. `PATH`

The connection test prints every path it tried.

---

## Build

```bat
build_exe.bat
```

Produces `dist\VoiceMeeterPopup.exe` with PyInstaller (`--onefile --windowed`,
icon and assets included). CI builds the same artifact for every `v*` tag and
attaches it to the release, with a SHA-256 checksum.

---

## Permissions and privacy

- **No administrator rights.** The Remote API is an ordinary user-mode DLL; the
  app does not inject into Voicemeeter or modify its files.
- **No drivers, services or scheduled tasks.** “Start with Windows” writes one
  `REG_SZ` value under `HKCU\...\Run`.
- **No network access, no telemetry, no analytics.** Settings and logs stay in
  `%LOCALAPPDATA%`.
- The overlay is a normal layered window: `WS_EX_NOACTIVATE | WS_EX_TRANSPARENT`,
  so it cannot take focus or intercept clicks.

---

## Limitations

- Windows only; requires the Voicemeeter GUI to be running.
- Shows **gain**, not audio levels or peak meters.
- One poll interval of latency (default 50 ms).
- Python and Voicemeeter must share bitness.
- Voicemeeter allows four Remote API clients; other tools can occupy them.
- Labels and device names are 8-bit ANSI strings from Voicemeeter.
- Display-only: no volume control, by design.

---

## Troubleshooting

| Symptom | Resolution |
|---|---|
| “Remote API not found” | Install Voicemeeter or set the DLL path in Settings → Connection. |
| “Voicemeeter is not running” | Start the Voicemeeter GUI (not just the audio engine). |
| `ImportError: DLL load failed` | 32/64-bit mismatch; use 64-bit Python with 64-bit Voicemeeter. |
| No popup | Check the connection test, “Show the popup at all”, and the watching mode. |
| One step behind | Use a 25–35 ms update interval; reads are cached for one engine cycle. |
| Tray icon missing | Settings opens automatically when the tray fails; see the log. |
| Second launch exits | It asks the running instance to open settings — one instance by design. |

---

## Development

```powershell
python -m unittest discover -s tests -v   # 41 tests, no Voicemeeter required
python -m app --selftest                  # environment + real API checks
python -m app --preview assets\preview    # regenerate the OSD previews
```

Tests cover the dB/percentage mapping, config validation and migration, target
resolution, change detection and reconnect logic (with an injected fake client),
the OSD renderer, and the tray menu wiring. `--selftest` additionally exercises
the real DLL, login, channel enumeration and read access when Voicemeeter is
installed.

```
app/
  cli.py           CLI, logging, selftest, diagnostics
  config.py        validated settings, atomic writes
  vmapi.py         ctypes binding + DLL discovery
  backend.py       read-only real/mock clients, channel model
  controller.py    polling thread, change detection, reconnect
  levels.py        dB ↔ fader-percentage mapping
  osd.py           renderer, layered window, animation
  tray.py          tray icon and menu
  settings_ui.py   settings window
  startup.py       per-user autostart
  winapi.py        DPI, monitors, single instance
tests/             unittest suite
tools/             icon generator
docs/              architecture and test plan
```

Deeper documentation: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) ·
[docs/TEST_PLAN.md](docs/TEST_PLAN.md) ·
[CHANGELOG.md](CHANGELOG.md).

---

## Contributing

Issues and pull requests are welcome. Please keep the read-only guarantee and
run the test suite before opening a PR. The app targets Python 3.10+ and has no
network dependencies.

---

## License

[MIT](LICENSE).

Voicemeeter is donationware by VB-Audio. This project only uses its published
Remote API and is not affiliated with or endorsed by VB-Audio.
