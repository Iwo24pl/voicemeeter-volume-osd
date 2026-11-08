"""Gain <-> percentage helpers.

The popup shows the Voicemeeter fader as a percentage, with a configurable
"100 %" reference (default 0.0 dB).  The mapping is linear in dB from the bottom
of the fader (-60 dB) to the reference; above the reference it keeps going, so
boosting +6 dB with a 0 dB reference reads 110 %.

    percent = (dB + 60) / (reference + 60) * 100

0 dB → 100 %, -60 dB → 0 %, +12 dB → 120 % (for reference = 0 dB).
"""
from __future__ import annotations

MIN_DB = -60.0
MAX_DB = 12.0
MIN_REFERENCE_DB = -40.0
MINUS = "\u2212"  # typographic minus, nicer than a hyphen in the OSD


def clamp_db(value: float) -> float:
    """Clamp to the documented Voicemeeter fader range."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return round(max(MIN_DB, min(MAX_DB, number)), 2)


def clamp_reference(value: float) -> float:
    """Clamp the '100 %' reference to a usable range above the fader bottom."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    return round(max(MIN_REFERENCE_DB, min(MAX_DB, number)), 2)


def db_to_percent(db: float, reference_db: float = 0.0) -> float:
    """Fader percentage; values above ``reference_db`` exceed 100 %."""
    reference = clamp_reference(reference_db)
    span = max(1.0, reference - MIN_DB)
    return round(max(0.0, (clamp_db(db) - MIN_DB) / span * 100.0), 1)


def display_percent(db: float, reference_db: float = 0.0) -> int:
    """Whole percentage for the big OSD number (can be over 100)."""
    return int(round(db_to_percent(db, reference_db)))


def percent_to_db(percent: float, reference_db: float = 0.0) -> float:
    """Inverse mapping, clamped to the fader range (used by tests/tools)."""
    reference = clamp_reference(reference_db)
    span = max(1.0, reference - MIN_DB)
    return clamp_db(MIN_DB + (max(0.0, float(percent)) / 100.0) * span)


def format_db(db: float) -> str:
    """'+1.5 dB' / '−6.0 dB' / '−60.0 dB'."""
    value = clamp_db(db)
    sign = "+" if value > 0 else (MINUS if value < 0 else "")
    return f"{sign}{abs(value):.1f} dB"
