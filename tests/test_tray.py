"""Tray menu wiring tests.

These caught a real bug once: the "Settings…" item called the ``settings``
attribute (a window object) instead of ``App.show_settings``.  The menu is built
without creating a real tray icon, and every action is invoked against a fake
app so wrong names fail here instead of on the user's desktop.
"""
from __future__ import annotations

import unittest

from app.backend import Channel, VmInfo, bus_default_label
from app.config import Config
from app.controller import State
from app.tray import TrayIcon


def info() -> VmInfo:
    strips = tuple(Channel("strip", i, f"Mic{i + 1}", "", f"Strip {i + 1}") for i in range(2))
    buses = tuple(Channel("bus", i, "", f"Device {i}", bus_default_label(2, i)) for i in range(2))
    return VmInfo(2, "test", strips, buses, "fake")


class FakeActions:
    def __init__(self):
        self.calls = []

    def show_settings(self):
        self.calls.append("show_settings")

    def preview_osd(self):
        self.calls.append("preview_osd")

    def reconnect(self):
        self.calls.append("reconnect")

    def set_target(self, kind, index):
        self.calls.append(("set_target", kind, index))

    def set_autostart(self, enabled):
        self.calls.append(("set_autostart", enabled))

    def open_config_folder(self):
        self.calls.append("open_config_folder")

    def open_log(self):
        self.calls.append("open_log")

    def quit(self):
        self.calls.append("quit")


class TrayMenuTests(unittest.TestCase):
    def make_tray(self):
        actions = FakeActions()
        posted = []
        tray = TrayIcon(lambda job: posted.append(job), actions)
        tray._state = State(connected=True, info=info(), target=None, gain=-6.0)
        tray._config = Config()
        return tray, actions, posted

    def test_settings_item_opens_settings(self):
        tray, actions, posted = self.make_tray()
        menu = tray._build_menu()
        item = next(entry for entry in menu.items if entry.text == "Settings…")
        item(None)
        self.assertEqual(len(posted), 1)
        posted[0]()
        self.assertEqual(actions.calls, ["show_settings"])

    def test_every_enabled_action_is_wired(self):
        """Invoking every action must not raise (no wrong attribute names)."""
        tray, actions, posted = self.make_tray()
        menu = tray._build_menu()
        invoked = 0
        for entry in menu.items:
            if entry.submenu is None and entry.enabled:
                entry(None)
                invoked += 1
        for job in posted:
            job()
        self.assertGreaterEqual(invoked, 5)
        self.assertIn("show_settings", actions.calls)
        self.assertIn("preview_osd", actions.calls)
        self.assertIn("reconnect", actions.calls)
        self.assertIn("quit", actions.calls)

    def test_target_submenu_selects_a_channel(self):
        tray, actions, posted = self.make_tray()
        menu = tray._build_menu()
        target_menu = next(entry for entry in menu.items if entry.text == "Target")
        first = next(entry for entry in target_menu.submenu.items if entry.text.startswith("Bus A1"))
        first(None)
        for job in posted:
            job()
        self.assertIn(("set_target", "bus", 0), actions.calls)


if __name__ == "__main__":
    unittest.main()
