# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-08

### Added

- Read-only Voicemeeter monitor: polls `VBVMR_IsParametersDirty` and reads
  `Strip[i].Gain/Mute` and `Bus[i].Gain/Mute` through the official Remote API.
- "Any fader that moves" mode with channel naming, plus a fixed target mode
  (Master, bus or strip).
- Per-pixel-alpha OSD with nine positions, offsets, size, opacity, hold time,
  fade time, monitor selection, mute state and warning state.
- Configurable 100 % reference (`osd.full_scale_db`, default 0 dB); values above
  it read over 100 % and use the amber boost accent.
- Tray icon with target shortcuts, settings window, connection test, demo
  backend and per-user autostart.
- Automatic reconnection, clear missing-DLL/not-running status, and a rotating
  log in `%LOCALAPPDATA%\VoiceMeeterPopup`.
- 41 unit tests, a `--selftest` diagnostic mode, GitHub Actions test and
  release workflows.

### Fixed

- The tray menu's **Settings…** action called the settings window object instead
  of `App.show_settings`, so it did nothing. A tray wiring test now covers it.
- The single-instance check no longer relies on a possibly stale
  `GetLastError` value.
- Self-test output no longer depends on a console being attached (windowed
  builds log instead).

### Security

- No keyboard hook or key suppression, no write path into Voicemeeter, no
  network access, no telemetry, and no administrator rights.

[1.0.0]: https://github.com/Iwo24pl/voicemeeter-volume-osd/releases/tag/v1.0.0
