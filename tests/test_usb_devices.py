import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_orchestrator.usb_devices import NUMPAD_KEYS, bitmap, scan_usb, selected_input, valid_input_path


class USBDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / "sys"
        self.dev = self.root / "dev/input"
        self.usb = self.sys / "devices/usb1/1-2"
        self.usb.mkdir(parents=True)
        for field, value in {"idVendor": "320f", "idProduct": "5044", "product": "GMMK Numpad", "manufacturer": "Glorious", "serial": "test-serial"}.items():
            (self.usb / field).write_text(value)
        bus = self.sys / "bus/usb/devices"
        bus.mkdir(parents=True)
        (bus / "1-2").symlink_to(self.usb)
        (self.sys / "class/input").mkdir(parents=True)
        self.dev.mkdir(parents=True)

    def event(self, number, keys):
        source = self.usb / f"1-2:1.{number}/input/input{number}"
        (source / "capabilities").mkdir(parents=True)
        (source / "name").write_text(f"Keyboard interface {number}")
        (source / "capabilities/ev").write_text("2")
        mask = sum(1 << key for key in keys)
        words = []
        while mask:
            words.insert(0, f"{mask & ((1 << 64)-1):x}")
            mask >>= 64
        (source / "capabilities/key").write_text(" ".join(words))
        event = self.sys / f"class/input/event{number}"
        event.mkdir()
        (event / "device").symlink_to(source)
        (self.dev / f"event{number}").touch()

    def test_usb_input_association_and_stable_alias_preference(self):
        self.event(5, NUMPAD_KEYS)
        self.event(8, [113, 114])  # media interface, not a numpad
        for folder, label in (("by-id", "usb-Glorious-test-event-kbd"), ("by-path", "platform-usb-port-event-kbd")):
            (self.dev / folder).mkdir()
            (self.dev / folder / label).symlink_to("../event5")
        result = scan_usb(self.sys, self.dev)
        self.assertEqual(len(result["devices"]), 1)
        self.assertEqual(len(result["devices"][0]["inputs"]), 2)
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertIn("/by-id/", candidate["selection_path"])
        self.assertEqual(candidate["serial"], "test-serial")
        self.assertTrue(candidate["readable"])

    def test_peripherals_without_input_still_listed(self):
        result = scan_usb(self.sys, self.dev)
        self.assertEqual(result["devices"][0]["name"], "GMMK Numpad")
        self.assertEqual(result["candidates"], [])

    def test_permission_denied_is_visible_not_a_scan_failure(self):
        self.event(5, NUMPAD_KEYS)
        with patch("desk_orchestrator.usb_devices.os.access", return_value=False):
            result = scan_usb(self.sys, self.dev)
        self.assertFalse(result["candidates"][0]["readable"])
        state = selected_input({"device": str(self.dev / "event5")}, result)
        self.assertEqual(state["status"], "Connected · input access needed")

    def test_missing_sysfs_and_disconnected_saved_device(self):
        result = scan_usb(self.root / "missing", self.dev)
        self.assertTrue(result["warnings"])
        state = selected_input({"device": "/dev/input/by-id/usb-test", "identity": {"name": "My numpad"}}, result)
        self.assertEqual(state["status"], "Not detected")
        self.assertEqual(state["name"], "My numpad")

    def test_bitmap_native_word_widths(self):
        self.assertEqual(bitmap("1 0", 64), 1 << 64)
        self.assertEqual(bitmap("1 0", 32), 1 << 32)
        self.assertEqual(bitmap("bad data", 64), 0)

    def test_manual_paths_are_input_nodes_not_arbitrary_paths(self):
        for path in ["/dev/input/event5", "/dev/input/by-id/usb-keyboard-event-kbd", "/dev/input/by-path/platform-usb-event-kbd"]:
            self.assertTrue(valid_input_path(path))
        for path in ["/etc/passwd", "/dev/input/../../etc/passwd", "/dev/input/by-id/..", "/dev/input/", "/dev/input/event5\x00"]:
            self.assertFalse(valid_input_path(path))
