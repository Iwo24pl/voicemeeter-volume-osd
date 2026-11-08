"""Command line entry point and diagnostics."""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import queue
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import Config, config_path, data_dir, log_path
from .winapi import InstanceGuard, message_box, set_dpi_awareness, system_dpi

_LOG = logging.getLogger("voicemeeter_popup")
APP_ID = "VoiceMeeterPopup"


# ------------------------------------------------------------------ logging
def setup_logging(verbose: bool = False) -> None:
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.handlers.RotatingFileHandler(
            log_path(), maxBytes=512 * 1024, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    if verbose or sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers or [logging.NullHandler()],
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)


# ------------------------------------------------------------------ preview images
def write_previews(folder: str) -> int:
    from PIL import Image

    from .levels import display_percent
    from .osd import OsdModel, render_osd

    target = Path(folder)
    target.mkdir(parents=True, exist_ok=True)
    cases = {
        "osd-normal.png": OsdModel(name="Bus A1 · Głośniki (Realtek)", kicker="VOICEMEETER BANANA",
                                   percent=display_percent(-12.0), db=-12.0),
        "osd-boost.png": OsdModel(name="Master · A1 · Speakers", kicker="VOICEMEETER POTATO",
                                  percent=display_percent(6.0), db=6.0),
        "osd-muted.png": OsdModel(name="Mic1 · Wireless Microphone", kicker="VOICEMEETER BANANA",
                                  percent=display_percent(-6.0), db=-6.0, muted=True),
        "osd-warning.png": OsdModel(name="Voicemeeter is not running", kicker="VOICEMEETER",
                                    percent=None, db=None, warning=True, show_db=False),
    }
    for filename, model in cases.items():
        image = render_osd(model, 1.0)
        background = Image.new("RGBA", image.size, (30, 32, 38, 255))
        background.alpha_composite(image)
        background.convert("RGB").save(target / filename)
    print(f"Wrote {len(cases)} previews to {target}")
    return 0


# ------------------------------------------------------------------ self test
def _demo_pipeline_test() -> tuple[bool, str]:
    """Run the real controller against the demo backend and require a level event."""
    from .controller import Controller

    events: "queue.Queue" = queue.Queue()
    cfg = Config().with_section("connection", demo_backend=True)
    control = Controller(cfg, events)
    control.start()
    connected = False
    bumped = False
    level = None
    deadline = time.time() + 6.0
    try:
        while time.time() < deadline:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                event = None
            if event is not None:
                if event.kind == "state" and event.data["state"].connected and not connected:
                    connected = True
                    # The controller ignores changes for a moment after connecting
                    # while the Remote API read cache warms up.
                    time.sleep(0.7)
                    control.demo_bump()      # exactly what the settings button does
                    bumped = True
                if event.kind == "level":
                    level = event.data
                    break
            elif connected and not bumped:
                control.demo_bump()
                bumped = True
    finally:
        control.stop()
    if not connected:
        return False, "the controller never connected to the demo backend"
    if level is None:
        return False, "no level event arrived after a demo fader move"
    target = level["target"]
    return True, f"{target.short_name} {level['gain']:+.1f} dB ({level['percent']} %)"


def selftest(demo: bool) -> int:
    from .backend import MockClient, RealClient, ResolvedTarget, resolve_target
    from .osd import OsdModel, render_osd
    from .vmapi import VoicemeeterDllNotFound, VoicemeeterNotRunning, find_remote_dll

    results: list[tuple[str, str, str]] = []  # status, name, detail

    def record(name: str, status: str, detail: str = "") -> None:
        results.append((status, name, detail))
        print(f"[{status:<4}] {name}" + (f" — {detail}" if detail else ""))

    print(f"VoiceMeeter Volume OSD {__version__} — self test")
    print(f"Python {sys.version.split()[0]} ({'64' if sys.maxsize > 2**32 else '32'}-bit)"
          f"  ·  DPI {system_dpi()}")
    print(f"Config folder: {data_dir()}")
    print()

    # 1. config round trip
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config()
            assert cfg.save(path), "save returned False"
            again = Config.load(path)
            assert again.to_dict() == cfg.to_dict(), "round trip differs"
        record("Configuration read/write", "PASS")
    except Exception as exc:
        record("Configuration read/write", "FAIL", f"{type(exc).__name__}: {exc}")

    # 2. OSD renderer
    try:
        image = render_osd(OsdModel(name="Bus A1", percent=72, db=-6.0), 1.0)
        alpha = image.getchannel("A").getextrema()
        assert image.width > 400 and image.height > 100, image.size
        assert alpha[0] == 0 and alpha[1] == 255, alpha
        record("OSD renderer", "PASS", f"{image.width}×{image.height}, alpha {alpha[0]}…{alpha[1]}")
    except Exception as exc:
        record("OSD renderer", "FAIL", f"{type(exc).__name__}: {exc}")

    # 3. polling pipeline (connect -> dirty -> level event)
    try:
        ok, detail = _demo_pipeline_test()
        record("Change detection pipeline", "PASS" if ok else "FAIL", detail)
    except Exception as exc:
        record("Change detection pipeline", "FAIL", f"{type(exc).__name__}: {exc}")

    # 4. Voicemeeter connection (read-only)
    cfg = Config()
    client = None
    try:
        if demo:
            client = MockClient(cfg.connection)
            info = client.connect()
            record("Demo backend", "PASS", f"{info.name}, {len(info.strips)} strips / {len(info.buses)} buses")
        else:
            path, notes = find_remote_dll(cfg.connection.dll_path)
            if path is None:
                raise VoicemeeterDllNotFound(
                    "VoicemeeterRemote DLL not found. Notes:\n  " + "\n  ".join(notes[-6:])
                )
            print(f"Remote DLL: {path}")
            client = RealClient(cfg.connection)
            try:
                info = client.connect()
            except VoicemeeterNotRunning:
                record("Voicemeeter running", "WARN",
                       "DLL found, but Voicemeeter is not running (the app will retry)")
                info = None
            if info is not None:
                record("Voicemeeter connection", "PASS",
                       f"{info.name} {info.version}, {len(info.strips)} strips / {len(info.buses)} buses")
                for channel in info.channels():
                    target = ResolvedTarget.from_channel(channel)
                    try:
                        gain = client.read_gain(target)
                        muted = client.read_mute(target)
                        suffix = f"{gain:+6.1f} dB" + ("   [muted]" if muted else "")
                    except Exception:
                        suffix = "n/a"
                    print(f"    {channel.base:<10} {channel.display:<52} {suffix}")
                target = resolve_target(cfg, info)
                print(f"    selected {target.base}: {client.read_gain(target):+.1f} dB (read-only)")
                print("    the app performs no writes")
    except VoicemeeterDllNotFound as exc:
        record("Voicemeeter connection", "FAIL", str(exc).replace("\n", " "))
    except Exception as exc:
        record("Voicemeeter connection", "FAIL", f"{type(exc).__name__}: {exc}")
    finally:
        if client is not None:
            client.close()

    passed = sum(1 for status, _, _ in results if status == "PASS")
    failed = sum(1 for status, _, _ in results if status == "FAIL")
    warned = sum(1 for status, _, _ in results if status == "WARN")
    print()
    print(f"{passed} passed, {failed} failed, {warned} warning(s)")
    return 0 if failed == 0 else 1


# ------------------------------------------------------------------ args
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="VoiceMeeterPopup",
        description="Voicemeeter volume popup: shows Voicemeeter fader changes on screen.",
    )
    parser.add_argument("--config", metavar="PATH", help="use a specific config.json")
    parser.add_argument("--demo", action="store_true", help="use the built-in demo backend")
    parser.add_argument("--settings", action="store_true", help="open settings on start")
    parser.add_argument("--minimized", action="store_true", help="start in the tray without settings")
    parser.add_argument("--selftest", action="store_true", help="run environment/API checks and exit")
    parser.add_argument("--preview", metavar="DIR", help="write OSD preview PNGs and exit")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if sys.stdout is not None:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr is not None:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    set_dpi_awareness()
    setup_logging(args.verbose)
    logging.getLogger(__name__).info(
        "VoiceMeeterPopup %s, Python %s, config %s", __version__, sys.version.split()[0],
        args.config or config_path())

    if args.selftest:
        return selftest(args.demo)
    if args.preview:
        return write_previews(args.preview)

    config_file = Path(args.config) if args.config else config_path()
    first_run = not config_file.exists()

    guard = InstanceGuard(APP_ID)
    if guard.already_running:
        guard.signal_existing()
        guard.close()
        print("VoiceMeeter Volume OSD is already running (tray). Asked it to open settings.")
        return 0

    try:
        from .app import App

        app = App(config_file, demo=args.demo, guard=guard)
    except Exception as exc:
        logging.getLogger(__name__).exception("Startup failed")
        message_box(f"Could not start VoiceMeeter Volume OSD:\n\n{exc}", error=True)
        guard.close()
        return 1

    show_settings = args.settings or first_run or (
        not args.minimized and not app.config.general.start_minimized
    )
    if args.minimized and not args.settings:
        show_settings = False
    try:
        app.run(show_settings=show_settings)
    except KeyboardInterrupt:
        app.quit()
        app.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
