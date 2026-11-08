"""Controller tests: change detection, watched channels, reconnect.

A fake client is injected so these tests are deterministic and never depend on
a running Voicemeeter.  They exercise the real polling logic and threading.
"""
from __future__ import annotations

import queue
import time
import unittest

from app.backend import Channel, VmInfo, bus_default_label
from app.config import Config
from app.controller import Controller, Event
from app.vmapi import VoicemeeterNotRunning


def fake_info() -> VmInfo:
    strips = tuple(Channel("strip", i, f"Mic{i + 1}", "", f"Strip {i + 1}") for i in range(2))
    buses = tuple(Channel("bus", i, "", f"Device {i}", bus_default_label(2, i)) for i in range(2))
    return VmInfo(2, "test", strips, buses, "fake")


class FakeClient:
    demo = True
    writable = False

    def __init__(self):
        self.available = True
        self.dirty = False
        self.gains = {("bus", 0): -6.0, ("bus", 1): -3.0, ("strip", 0): 0.0, ("strip", 1): -9.0}
        self.mutes = {}
        self.reads: list[tuple[str, int]] = []
        self.connects = 0
        self.dll_notes = ["fake"]
        self.dll_path = "fake"
        self.last_login_code = 0

    # -- interface used by Controller -------------------------------------
    def connect(self):
        self.connects += 1
        if not self.available:
            raise VoicemeeterNotRunning(-2, "fake")
        return fake_info()

    def close(self):
        pass

    def apply_config(self, cfg):
        pass

    def launch(self, edition):
        self.available = True

    def poll_dirty(self):
        if not self.available:
            raise VoicemeeterNotRunning(-2, "fake")
        return self.dirty

    def read_gain(self, target):
        if not self.available:
            raise VoicemeeterNotRunning(-2, "fake")
        self.reads.append((target.kind, target.index))
        return self.gains.get((target.kind, target.index), 0.0)

    def read_mute(self, target):
        if not self.available:
            raise VoicemeeterNotRunning(-2, "fake")
        return self.mutes.get((target.kind, target.index), False)

    # -- test helpers ------------------------------------------------------
    def set_gain(self, kind, index, value):
        self.gains[(kind, index)] = value
        self.dirty = True


def wait_event(events: queue.Queue, predicate, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.05)
        except queue.Empty:
            continue
        if predicate(event):
            return event
    return None


def is_connected(event: Event) -> bool:
    return event.kind == "state" and event.data["state"].connected


class ControllerTests(unittest.TestCase):
    def make(self, mode="any", target=None, osd=None) -> tuple[Controller, FakeClient, queue.Queue]:
        cfg = Config().with_section("monitor", mode=mode, poll_ms=25)
        if target:
            cfg = cfg.replace(target=target)
        if osd:
            cfg = cfg.with_section("osd", **osd)
        events: queue.Queue = queue.Queue()
        client = FakeClient()
        controller = Controller(cfg, events, client=client)
        controller.start()
        connected = wait_event(events, is_connected)
        self.assertIsNotNone(connected, "controller did not connect")
        # The controller ignores changes for a moment after connecting while the
        # Remote API's read cache warms up.
        time.sleep(0.7)
        return controller, client, events

    def test_reports_a_fader_change(self):
        controller, client, events = self.make()
        try:
            client.set_gain("bus", 0, -3.0)
            event = wait_event(events, lambda e: e.kind == "level")
            self.assertIsNotNone(event)
            data = event.data
            self.assertEqual((data["target"].kind, data["target"].index), ("bus", 0))
            self.assertAlmostEqual(data["gain"], -3.0)
            self.assertEqual(data["source"], "change")
        finally:
            controller.stop()

    def test_no_event_without_a_voicemeeter_change(self):
        controller, client, events = self.make()
        try:
            client.dirty = False
            # Drain anything queued during startup, then keep the connection idle.
            time.sleep(0.25)
            while not events.empty():
                events.get_nowait()
            time.sleep(0.25)
            self.assertTrue(events.empty(), "an idle connection must not show a popup")
        finally:
            controller.stop()

    def test_target_mode_only_reads_the_selected_channel(self):
        controller, client, _events = self.make(
            mode="target", target={"kind": "bus", "index": 1, "master_bus": 0})
        try:
            client.dirty = True          # make the poll read every tick
            time.sleep(0.3)
            watched = set(client.reads)
            self.assertIn(("bus", 1), watched)
            self.assertNotIn(("strip", 0), watched)
            self.assertNotIn(("bus", 0), watched)
        finally:
            controller.stop()

    def test_any_mode_reads_every_channel(self):
        controller, client, _events = self.make(mode="any")
        try:
            client.dirty = True
            time.sleep(0.3)
            watched = set(client.reads)
            self.assertIn(("strip", 0), watched)
            self.assertIn(("bus", 0), watched)
            self.assertIn(("bus", 1), watched)
        finally:
            controller.stop()

    def test_mute_change_is_reported(self):
        controller, client, events = self.make()
        try:
            client.mutes[("strip", 1)] = True
            client.dirty = True
            event = wait_event(events, lambda e: e.kind == "level")
            self.assertIsNotNone(event)
            self.assertTrue(event.data["muted"])
            self.assertEqual(event.data["target"].index, 1)
        finally:
            controller.stop()

    def test_losing_and_regaining_the_connection(self):
        controller, client, events = self.make()
        try:
            client.available = False
            client.dirty = True
            lost = wait_event(events, lambda e: e.kind == "state" and not e.data["state"].connected, 3.0)
            self.assertIsNotNone(lost, "disconnect was not detected")
            client.available = True
            back = wait_event(events, is_connected, 4.0)
            self.assertIsNotNone(back, "the controller did not reconnect")
        finally:
            controller.stop()

    def test_target_change_shows_the_new_channel(self):
        controller, client, events = self.make(mode="target", target={"kind": "bus", "index": 0, "master_bus": 0})
        try:
            new_cfg = controller.config.replace(target={"kind": "strip", "index": 0, "master_bus": 0})
            controller.apply_config(new_cfg)
            event = wait_event(events, lambda e: e.kind == "level" and e.data["source"] == "target")
            self.assertIsNotNone(event)
            self.assertEqual(event.data["target"].kind, "strip")
        finally:
            controller.stop()

    def test_percentage_follows_the_configured_reference(self):
        controller, client, events = self.make(
            mode="target", target={"kind": "bus", "index": 0, "master_bus": 0},
            osd={"full_scale_db": 0.0})
        try:
            client.set_gain("bus", 0, 3.0)      # 3 dB above the 0 dB reference
            event = wait_event(events, lambda e: e.kind == "level")
            self.assertIsNotNone(event)
            self.assertEqual(event.data["percent"], 105)
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
