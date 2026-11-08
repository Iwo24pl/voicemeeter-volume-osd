"""Unit tests for the dB <-> percentage helpers."""
from __future__ import annotations

import unittest

from app.levels import (
    MAX_DB,
    MIN_DB,
    clamp_db,
    clamp_reference,
    db_to_percent,
    display_percent,
    format_db,
    percent_to_db,
)


class GainMathTests(unittest.TestCase):
    def test_default_reference_makes_zero_db_one_hundred_percent(self):
        self.assertEqual(display_percent(0.0), 100)
        self.assertEqual(display_percent(-60.0), 0)
        self.assertEqual(display_percent(6.0), 110)
        self.assertEqual(display_percent(12.0), 120)
        self.assertEqual(display_percent(-6.0), 90)
        self.assertEqual(display_percent(-24.0), 60)
        self.assertGreater(display_percent(0.5), 100)

    def test_custom_reference(self):
        self.assertEqual(display_percent(-6.0, -6.0), 100)
        self.assertEqual(display_percent(0.0, -6.0), 111)
        self.assertEqual(display_percent(-12.0, -6.0), 89)
        self.assertEqual(display_percent(12.0, 12.0), 100)
        self.assertEqual(display_percent(0.0, 12.0), 83)
        self.assertEqual(display_percent(-60.0, 6.0), 0)

    def test_round_trip(self):
        for db in (-60.0, -42.3, -6.0, 0.0, 6.4, 12.0):
            self.assertAlmostEqual(percent_to_db(db_to_percent(db)), db, places=1)
        for reference in (-24.0, -6.0, 0.0, 6.0):
            percent = db_to_percent(reference, reference)
            self.assertAlmostEqual(percent, 100.0, places=1)

    def test_reference_clamping(self):
        self.assertEqual(clamp_reference(-500), -40.0)
        self.assertEqual(clamp_reference(500), 12.0)
        self.assertEqual(clamp_reference("nope"), 0.0)
        # A very low reference expands the top of the scale.
        self.assertEqual(display_percent(0.0, -100), 300)

    def test_clamping(self):
        self.assertEqual(clamp_db(-500), MIN_DB)
        self.assertEqual(clamp_db(500), MAX_DB)
        self.assertEqual(clamp_db(float("nan")), 0.0)
        self.assertEqual(clamp_db("nope"), 0.0)

    def test_percent_to_db(self):
        self.assertAlmostEqual(percent_to_db(50), -30.0)
        self.assertEqual(percent_to_db(0), MIN_DB)
        self.assertEqual(percent_to_db(100), 0.0)
        self.assertEqual(percent_to_db(1000), MAX_DB)
        self.assertAlmostEqual(percent_to_db(100, -12.0), -12.0)

    def test_format(self):
        self.assertEqual(format_db(-6.0), "\u22126.0 dB")
        self.assertEqual(format_db(1.5), "+1.5 dB")
        self.assertEqual(format_db(0.0), "0.0 dB")


if __name__ == "__main__":
    unittest.main()
