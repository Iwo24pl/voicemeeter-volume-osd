"""Configuration tests: defaults, validation, migration from the old format."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import Config, POSITIONS


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid(self):
        cfg = Config()
        self.assertEqual(cfg.monitor.mode, "any")
        self.assertEqual(cfg.monitor.poll_ms, 50)
        self.assertEqual(cfg.target.kind, "master")
        self.assertEqual(cfg.osd.position, "bottom-center")

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            cfg = Config().with_section("osd", scale=1.4, position="top-right")
            self.assertTrue(cfg.save(path))
            again = Config.load(path)
            self.assertEqual(again.to_dict(), cfg.to_dict())

    def test_unknown_and_wrong_values_are_sanitized(self):
        cfg = Config.from_dict({
            "target": {"kind": "nonsense", "index": "7", "master_bus": 99},
            "monitor": {"mode": "sideways", "poll_ms": 9999},
            "osd": {"scale": 99, "opacity": "0.1", "position": "middle-right",
                    "hold_seconds": "nope", "full_scale_db": 99},
            "connection": {"launch_edition": 42},
            "stale": {"ignored": True},
        })
        self.assertEqual(cfg.target.kind, "master")
        self.assertEqual(cfg.target.index, 7)
        self.assertEqual(cfg.target.master_bus, 7)
        self.assertEqual(cfg.monitor.mode, "any")
        self.assertEqual(cfg.monitor.poll_ms, 250)
        self.assertEqual(cfg.osd.scale, 2.0)
        self.assertEqual(cfg.osd.opacity, 0.25)
        self.assertIn(cfg.osd.position, POSITIONS)
        self.assertEqual(cfg.osd.hold_seconds, 1.8)
        self.assertEqual(cfg.osd.full_scale_db, 12.0)
        self.assertEqual(cfg.connection.launch_edition, 2)

    def test_reference_defaults_to_zero_db(self):
        self.assertEqual(Config().osd.full_scale_db, 0.0)
        cfg = Config.from_dict({"osd": {"full_scale_db": -6}})
        self.assertEqual(cfg.osd.full_scale_db, -6.0)
        self.assertEqual(cfg.to_dict()["osd"]["full_scale_db"], -6.0)

    def test_old_config_is_migrated(self):
        """Version 1 had a `keys` section and `step_db`; both must be ignored."""
        old = {
            "version": 1,
            "target": {"kind": "bus", "index": 2, "master_bus": 0},
            "step_db": 1.5,
            "keys": {"intercept_media_keys": True, "up_hotkey": "Ctrl+Alt+Up"},
            "osd": {"position": "bottom-left", "scale": 1.25},
        }
        cfg = Config.from_dict(old)
        self.assertEqual(cfg.target.kind, "bus")
        self.assertEqual(cfg.target.index, 2)
        self.assertEqual(cfg.osd.position, "bottom-left")
        self.assertEqual(cfg.osd.scale, 1.25)
        self.assertNotIn("keys", cfg.to_dict())
        self.assertNotIn("step_db", cfg.to_dict())

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            cfg = Config.load(path)
            self.assertEqual(cfg.to_dict(), Config().to_dict())

    def test_save_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "config.json"
            self.assertTrue(Config().save(path))
            self.assertTrue(path.exists())
            json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
