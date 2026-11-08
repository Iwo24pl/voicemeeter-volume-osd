"""Backend tests: target resolution, channel metadata and the read-only promise."""
from __future__ import annotations

import unittest

from app.backend import (
    Channel,
    MockClient,
    RealClient,
    ResolvedTarget,
    VmInfo,
    bus_default_label,
    resolve_target,
)
from app.config import Config, ConnectionConfig


def demo_info() -> VmInfo:
    strips = (
        Channel("strip", 0, "Mic1", "Wireless Microphone", "Strip 1"),
        Channel("strip", 1, "", "", "Strip 2"),
    )
    buses = (
        Channel("bus", 0, "", "Głośniki (Realtek)", "A1"),
        Channel("bus", 1, "", "", "A2"),
    )
    return VmInfo(2, "3.0.3.4", strips, buses, "C:/fake/VoicemeeterRemote64.dll")


class TargetTests(unittest.TestCase):
    def test_master_resolves_to_selected_bus(self):
        cfg = Config().with_section("target", kind="master", master_bus=1)
        target = resolve_target(cfg, demo_info())
        self.assertTrue(target.is_master)
        self.assertEqual(target.kind, "bus")
        self.assertEqual(target.index, 1)
        self.assertEqual(target.base, "Bus[1]")
        self.assertEqual(target.osd_name, "Master · A2")

    def test_bus_and_strip(self):
        cfg = Config().with_section("target", kind="bus", index=0)
        target = resolve_target(cfg, demo_info())
        self.assertFalse(target.is_master)
        self.assertEqual(target.short_name, "Bus A1")
        self.assertIn("Głośniki", target.osd_name)

        cfg = Config().with_section("target", kind="strip", index=0)
        target = resolve_target(cfg, demo_info())
        self.assertEqual(target.gain_param, "Strip[0].Gain")
        self.assertEqual(target.mute_param, "Strip[0].Mute")
        self.assertIn("Mic1", target.short_name)
        self.assertIn("Wireless Microphone", target.osd_name)

    def test_offline_fallback(self):
        cfg = Config().with_section("target", kind="master", master_bus=2)
        target = resolve_target(cfg, None)
        self.assertEqual(target.base, "Bus[2]")
        self.assertEqual(target.label, "A3")

    def test_invalid_index_is_clamped(self):
        cfg = Config().with_section("target", kind="strip", index=99)
        target = resolve_target(cfg, demo_info())
        self.assertEqual(target.index, 1)

    def test_bus_labels_per_edition(self):
        self.assertEqual(bus_default_label(1, 0), "A")
        self.assertEqual(bus_default_label(2, 3), "B1")
        self.assertEqual(bus_default_label(3, 7), "B3")
        self.assertEqual(bus_default_label(6, 4), "A5")
        self.assertTrue(bus_default_label(3, 42).startswith("Bus"))


class ReadOnlyTests(unittest.TestCase):
    def test_no_write_api_on_clients(self):
        """The app must be unable to alter Voicemeeter state."""
        for cls in (RealClient, MockClient):
            self.assertFalse(hasattr(cls, "write_gain"), cls.__name__)
            self.assertFalse(hasattr(cls, "write_mute"), cls.__name__)
            self.assertFalse(hasattr(cls, "set_float"), cls.__name__)

    def test_mock_connects_and_reads(self):
        client = MockClient(ConnectionConfig())
        info = client.connect()
        self.assertTrue(info.demo)
        target = resolve_target(Config(), info)
        gain = client.read_gain(target)
        self.assertGreaterEqual(gain, -60.0)
        self.assertLessEqual(gain, 12.0)
        self.assertFalse(client.read_mute(target))

    def test_mock_unavailable_raises(self):
        from app.vmapi import VoicemeeterNotRunning

        client = MockClient(ConnectionConfig(simulate_unavailable=True))
        with self.assertRaises(VoicemeeterNotRunning):
            client.connect()


if __name__ == "__main__":
    unittest.main()
