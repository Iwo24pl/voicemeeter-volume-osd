# Test plan

Two layers: an automated suite that needs no Voicemeeter, and a manual pass that
verifies the real integration (and the parts that need a screen).

## Automated

```bat
python -m unittest discover -s tests -v
python main.py --selftest
```

`tests/test_levels.py` — dB ↔ fader-percent, clamping, formatting.
`tests/test_config.py` — defaults, atomic save/load, sanitising bad values, and
migration of a version-1 config that still contains `keys`/`step_db`.
`tests/test_backend.py` — target resolution (master/bus/strip), channel naming,
per-edition bus labels, and the read-only guarantee (no write methods exist).
`tests/test_osd.py` — render size, transparency (alpha 0 at the edges, 255 in
the card), scale, muted/warning/boost variants, long-name truncation,
premultiplied alpha and BGRA byte layout.
`tests/test_controller.py` — with an injected fake client:
change → one `level` event; idle → none; target mode reads only the selected
channel; any mode reads every channel; mute change reported; `-2` detected as a
disconnect and reconnected; changing the target announces the new channel.

`--selftest` additionally exercises:

| Check | What it proves |
|---|---|
| config read/write | the real data directory is writable |
| OSD renderer | Pillow + fonts produce a valid image |
| change detection pipeline | the real controller thread connects, receives a demo fader move and emits a level event |
| Voicemeeter connection | DLL found, `Login`, type/version, channel enumeration, read access (or an explicit WARN when Voicemeeter is not running) |

## Manual test plan

### 1. First run and tray

1. `python main.py --settings`.
2. Settings opens; the header shows **Connected — Voicemeeter …** with a green
   dot; the tray icon appears.
3. Close settings. Double-click the tray icon → settings return.
4. Tray → Exit → process exits, no tray icon remains.

### 2. The core behaviour (real Voicemeeter)

1. With the app running, open the Voicemeeter window.
2. Drag a strip fader, then a bus fader, then the master bus.
3. **Expected:** the popup appears within ~100 ms, names that specific channel,
   shows the exact dB and the matching percentage, follows the drag, and fades
   out after the configured hold time.
4. Move the fader in Voicemeeter with the mouse *while the cursor is on another
   monitor* → the popup appears on the monitor under the cursor (default).
5. Mute/unmute a channel → `MUTED` appears with the channel name.
6. Turn the physical knob that Voicemeeter is mapped to → the same popup
   appears. **The Windows volume flyout must not appear and the Windows
   endpoint volume must not change** (the app has no keyboard hook).
7. While the app is running, press the Windows volume keys directly: Windows'
   own OSD appears and the app shows nothing — correct, because Voicemeeter was
   not changed.
8. Load a Voicemeeter preset that moves many faders → at most no popup (bulk
   change is rebased), no flood of popups.

### 3. Target selection

1. Watching → *Only the selected channel*, choose a strip.
2. Move that strip → popup. Move another strip → no popup (still visible in the
   Voicemeeter window, which is fine).
3. Switch the selection → the popup announces the newly selected channel's
   current level (optional checkbox).

### 4. Percentage reference

1. Default (`100 % at = 0 dB`): with a channel at 0.0 dB the popup reads
   **100 %**; at +6.0 dB it reads **110 %** and the bar, dB text and accent are
   amber; at −6.0 dB it reads **90 %**.
2. Change *100 % at* to −6 dB: −6.0 dB now reads 100 %, 0.0 dB reads 111 %.
3. Set *100 % at* = +12 dB: +12.0 dB reads 100 %, 0.0 dB reads 83 %.
4. Set *100 % at* = −40 dB: the number grows well past 100 % without clipping
   or overlapping the card edge.
5. Restart the app: the chosen reference is still in effect.

### 5. Appearance

For each of: position (all nine), X/Y offsets, size 60/100/200 %, opacity
25/100 %, hold 0.3/5 s, fade 0 ms, show-dB off, primary monitor:
run **Preview on screen** and confirm the popup matches, stays on screen, does
not steal focus (type in Notepad while it shows), and does not block clicks
(click through the card onto the window behind it).

### 6. Screen/DPI edge cases

1. 125 %/150 % display scaling → the card is sharp and correctly sized.
2. Two monitors with different scaling → the card appears correctly on both.
3. 3440×1440 ultrawide → bottom-center is centred and above the taskbar.
4. Temporarily set the hold time very high, change the target, and confirm a
   second change replaces the content instead of stacking windows.

### 7. Failure handling

1. Start the app **without** Voicemeeter → status "Voicemeeter is not running",
   tray icon is dim, no crash, retry every ~1.2 s.
2. Launch Voicemeeter → within ~2 s the status becomes Connected and fader
   changes pop up.
3. Close Voicemeeter while connected → status changes, no crash; relaunch → it
   reconnects by itself.
4. Settings → Connection → *Test connection* → PASSED report with the DLL path,
   type, version, every channel's level.
5. Corrupt `config.json` → the app starts with defaults and logs a warning.
6. Try `python main.py` while it runs → the running instance's settings window
   comes forward; no second process.
7. (Demo) enable the demo backend, *Simulate a fader move* → the popup appears
   via the normal polling path; enable *pretend unavailable* → the status turns
   disconnected.

### 8. Permissions

1. Run from a normal (non-elevated) account: everything works.
2. Confirm that no UAC prompt appears and no service/driver was installed.
3. Enable *Start with Windows*, log out/in (or check the Run key), confirm it
   starts minimised; disable it and confirm the value is removed.

## Verified on this machine

* Windows 11, Python 3.10.11 x64, Voicemeeter Banana 2.1.2.2, 3440×1440.
* Real fader change (`Bus[0].Gain` 0 → +2 dB) captured on screen: popup showed
  `Master · A1 · Głośniki`, `+2.0 dB`, `103 %`, amber accent; restored to 0 dB
  afterwards.
* With the channel at 0.0 dB the popup reads exactly 100 %.
* `--selftest`: all checks PASS, including read access to 5 strips / 5 buses.
* 38 unit tests pass.
* Single-instance hand-off, settings tabs, tray startup and reconnect logic
  exercised manually.
