"""Offline checks for stock lighting; no real device is opened."""
import copy
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk_orchestrator import gmmk_rgb as rgb
from desk_orchestrator.core import DeskError

DESCRIPTOR = bytes.fromhex("06 01 ff 09 01 a1 01 85 07 15 00 26 ff 00 09 20 75 08 96 07 01 b1 02 c0")


class ReportTests(unittest.TestCase):
    def test_lighting_only_packet_and_hardware_matrix(self):
        settings = dict(rgb.DEFAULTS, profile=2, layer=3, brightness=75,
                        keys_color="#123456", sides_color="#abcdef", sides_mode="breathing")
        packet = rgb.lighting_report(settings)
        self.assertEqual(len(packet), 264)
        self.assertEqual(packet[:8], bytes([7, 6, 2, 3, 42, 0, 15, 0]))
        # Independently selected matrix positions: Num Lock, Num+, Enter, 0, dot.
        for index in [1, 10, 28, 38, 39]:
            self.assertEqual(packet[8 + index*5:13 + index*5], bytes([255, index, 0x12, 0x34, 0x56]))
        for index in [0, 5, 36, 41]:
            self.assertEqual(packet[8 + index*5:13 + index*5], bytes([254, index, 0xab, 0xcd, 0xef]))
        # Unused slots and duplicate plus retain CORE's zero padding.
        for index in [13, 14, 15, 16, 22, 31, 32, 33, 34, 37, 40]:
            self.assertEqual(packet[8 + index*5:13 + index*5], bytes(5))
        self.assertEqual(packet[218:], bytes(46))
        self.assertEqual(rgb.lighting_report(settings, 256), packet[:256])

    def test_off_is_black_overlay_and_base_releases_overlay(self):
        packet = rgb.lighting_report(dict(rgb.DEFAULTS, keys_mode="off", sides_mode="base"))
        self.assertEqual(packet[13:18], bytes([255, 1, 0, 0, 0]))
        self.assertEqual(packet[8], 0)
        self.assertEqual(rgb.lighting_report(dict(rgb.DEFAULTS, brightness=0))[6], 0)

    def test_invalid_settings_never_open_hardware(self):
        for field, value in [("profile", 0), ("profile", True), ("layer", 4), ("brightness", -1),
                             ("brightness", 101), ("brightness", 10.5), ("keys_color", "red"),
                             ("sides_color", "#123"), ("keys_mode", "rainbow"), ("keys_mode", [])]:
            with self.subTest(field=field, value=value), patch.object(rgb.os, "open") as opened:
                with self.assertRaises(DeskError):
                    rgb.apply("/dev/input/event0", dict(rgb.DEFAULTS, **{field: value}))
                opened.assert_not_called()
        with self.assertRaises(DeskError):
            rgb.from_form({})

    def test_descriptor_fails_closed_on_custom_firmware(self):
        self.assertEqual(rgb.feature_length(DESCRIPTOR), 264)
        self.assertEqual(rgb.feature_length(DESCRIPTOR[:19] + b"\xff\x00" + DESCRIPTOR[21:]), 256)
        for invalid in [b"", DESCRIPTOR[:-1], DESCRIPTOR.replace(b"\x85\x07", b"\x85\x08"),
                        DESCRIPTOR[:19] + b"\xff\xff" + DESCRIPTOR[21:]]:
            with self.assertRaises(OSError):
                rgb.feature_length(invalid)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sys = self.root / "sys"
        self.dev = self.root / "dev"
        (self.sys / "class/input/event8").mkdir(parents=True)
        (self.sys / "class/hidraw").mkdir()
        self.dev.mkdir()
        self.input = self.dev / "event8"
        self.input.touch()
        self.alias = self.dev / "selected"
        self.alias.symlink_to(self.input)
        self.usb = self.usb_device("selected")
        (self.sys / "class/input/event8/device").symlink_to(self.usb)

    def usb_device(self, name):
        usb = self.sys / "devices" / name
        usb.mkdir(parents=True)
        (usb / "idVendor").write_text("320f")
        (usb / "idProduct").write_text("5088")
        return usb

    def raw(self, usb, number, interface="02", descriptor=DESCRIPTOR):
        target = usb / ("interface" + str(number))
        target.mkdir()
        (target / "bInterfaceNumber").write_text(interface)
        (target / "report_descriptor").write_bytes(descriptor)
        node = self.sys / "class/hidraw" / ("hidraw" + str(number))
        node.mkdir()
        (node / "device").symlink_to(target)
        return node

    def find(self):
        return rgb.find_device(self.alias, self.sys, self.dev)

    def test_selected_device_interface_and_reconnection(self):
        self.raw(self.usb_device("other"), 0)
        self.raw(self.usb, 1, "01")
        node = self.raw(self.usb, 7)
        self.assertEqual(self.find(), (str(self.dev / "hidraw7"), 264))
        node.rename(node.with_name("hidraw9"))
        self.assertEqual(self.find()[0], str(self.dev / "hidraw9"))

    def test_wrong_missing_ambiguous_and_custom_firmware(self):
        with self.assertRaises(OSError):
            self.find()
        node = self.raw(self.usb, 7, descriptor=b"QMK")
        with self.assertRaises(OSError):
            self.find()
        (node / "device/report_descriptor").write_bytes(DESCRIPTOR)
        (self.usb / "idProduct").write_text("5044")
        with self.assertRaisesRegex(OSError, "320f:5088"):
            self.find()
        (self.usb / "idProduct").write_text("5088")
        self.raw(self.usb, 8)
        with self.assertRaises(OSError):
            self.find()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.find = patch.object(rgb, "find_device", return_value=("/dev/hidraw7", 264)).start()
        self.open = patch.object(rgb.os, "open", return_value=42).start()
        self.close = patch.object(rgb.os, "close").start()
        self.ioctl = patch.object(rgb.fcntl, "ioctl", return_value=264).start()
        patch.object(rgb.fcntl, "flock").start()
        patch.object(rgb.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=12)).start()
        patch.object(rgb.os, "stat", return_value=SimpleNamespace(st_rdev=12)).start()

    def test_one_feature_report_and_no_input_reads(self):
        with patch.object(rgb.os, "read") as read:
            rgb.apply("selected", rgb.DEFAULTS)
        self.ioctl.assert_called_once_with(42, 0xc1084806, rgb.lighting_report(rgb.DEFAULTS), True)
        read.assert_not_called()
        self.close.assert_called_once_with(42)

    def test_disconnect_and_short_write_are_not_retried(self):
        for response in [OSError("disconnected"), 0, 100]:
            self.ioctl.reset_mock()
            self.ioctl.side_effect = response if isinstance(response, Exception) else None
            self.ioctl.return_value = response
            with self.assertRaises(DeskError):
                rgb.apply("selected", rgb.DEFAULTS)
            self.ioctl.assert_called_once()

    def test_identity_change_does_not_write(self):
        self.find.side_effect = [("/dev/hidraw7", 264), ("/dev/hidraw8", 264)]
        with self.assertRaisesRegex(DeskError, "connection changed"):
            rgb.apply("selected", rgb.DEFAULTS)
        self.ioctl.assert_not_called()
        self.close.assert_called_once()

    def test_permission_and_status_never_send(self):
        self.open.side_effect = PermissionError()
        with self.assertRaisesRegex(DeskError, "permission denied"):
            rgb.apply("selected", rgb.DEFAULTS)
        self.ioctl.assert_not_called()
        self.open.reset_mock()
        with patch.object(rgb.os, "access", return_value=False):
            self.assertFalse(rgb.status("selected")["available"])
        self.open.assert_not_called()
