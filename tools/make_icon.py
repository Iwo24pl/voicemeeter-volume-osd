"""Regenerate assets/icon.ico (run from the project root: python tools/make_icon.py)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tray import make_icon  # noqa: E402

OUT = ROOT / "assets" / "icon.ico"


def main() -> int:
    image = make_icon(True, False, 256)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    # A PNG copy is handy for README/docs.
    image.save(OUT.with_suffix(".png"))
    print(f"wrote {OUT} and {OUT.with_suffix('.png')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
