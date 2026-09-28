import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_orchestrator.controls import ControlDecoder, validate_sources
from desk_orchestrator.core import DeskError
from desk_orchestrator.gmmk_slider import SliderDevice, find_device, slider_position


class ProtocolTests(unittest.TestCase):
    def test_captured_slider_reports_decode_across_byte_boundaries(self):
        capture = Path(__file__).parent / 'fixtures/gmmk_slider.txt'
        reports = [bytes.fromhex(line.split(' ', 1)[1]) for line in capture.read_text().splitlines()]
        positions = [slider_position(report) for report in reports]
        self.assertEqual(len(positions), 132)
        self.assertEqual(positions[:6], [266, 298, 340, 391, 448, 514])
        self.assertEqual((min(positions), max(positions)), (91, 2116))
        self.assertEqual(positions[-1], 109)

    def test_other_reports_and_malformed_reports_are_ignored(self):
        valid = bytes.fromhex('04 f8 32 00 05 01 0a 00 00')
        for data in (b'', valid[:8], valid + b'\x00', b'\x01' + valid[1:],
                     valid[:2] + b'\x33' + valid[3:], valid[:-1] + b'\x01'):
            self.assertIsNone(slider_position(data))

    def test_raw_source_validates_and_keeps_baseline_jitter_and_inversion(self):
        from types import SimpleNamespace
        source = dict(mode='gmmk_raw', device='/dev/input/event0', code=32, deadband=4, invert=True)
        validate_sources({'slider': source}, check_signal_codes=True)
        for config in ({'dial': source}, {'slider': dict(source, code=115)},
                       {'slider': dict(source, device='/dev/hidraw1')}):
            with self.assertRaises(DeskError):
                validate_sources(config)
        decoder = ControlDecoder()
        def decode(value):
            return decoder.decode('slider', source, SimpleNamespace(type=3, code=32, value=value))
        self.assertIsNone(decode(266))
        self.assertIsNone(decode(268))
        self.assertEqual(decode(298), 'decrease')
        self.assertEqual(decode(250), 'increase')
        decoder.reset()
        self.assertIsNone(decode(2000))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sys = self.root / 'sys'
        self.dev = self.root / 'dev'
        self.dev.mkdir()
        (self.sys / 'class/input/event8').mkdir(parents=True)
        (self.sys / 'class/hidraw').mkdir(parents=True)
        self.usb = self.make_usb('1-2')
        keyboard = self.usb / '1-2:1.0/input/input8'
        keyboard.mkdir(parents=True)
        (self.sys / 'class/input/event8/device').symlink_to(keyboard)
        (self.dev / 'event8').touch()
        self.alias = self.dev / 'stable-keyboard'
        self.alias.symlink_to('event8')

    def make_usb(self, name):
        usb = self.sys / 'devices' / name
        usb.mkdir(parents=True)
        (usb / 'idVendor').write_text('320f\n')
        (usb / 'idProduct').write_text('5088\n')
        return usb

    def raw(self, usb, number, interface='01'):
        parent = usb / ('interface-' + interface)
        parent.mkdir(exist_ok=True)
        (parent / 'bInterfaceNumber').write_text(interface + '\n')
        hid = parent / ('hid-' + str(number))
        hid.mkdir()
        node = self.sys / f'class/hidraw/hidraw{number}'
        node.mkdir()
        (node / 'device').symlink_to(hid)
        return node

    def test_matches_selected_usb_not_first_same_model_or_keyboard_interface(self):
        self.raw(self.make_usb('1-3'), 0)
        self.raw(self.usb, 1, '00')
        node = self.raw(self.usb, 7)
        self.assertEqual(find_device(self.alias, self.sys, self.dev), str(self.dev / 'hidraw7'))
        node.rename(node.with_name('hidraw9'))
        self.assertEqual(find_device(self.alias, self.sys, self.dev), str(self.dev / 'hidraw9'))

    def test_missing_wrong_model_and_ambiguous_interfaces_fail_closed(self):
        with self.assertRaises(OSError):
            find_device(self.alias, self.sys, self.dev)
        self.raw(self.usb, 1)
        (self.usb / 'idProduct').write_text('5044')
        with self.assertRaisesRegex(OSError, '320f:5088'):
            find_device(self.alias, self.sys, self.dev)
        (self.usb / 'idProduct').write_text('5088')
        self.raw(self.usb, 2)
        with self.assertRaisesRegex(OSError, 'Expected one'):
            find_device(self.alias, self.sys, self.dev)


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_burst_coalescing_and_cancellation(self):
        rx, tx = socket.socketpair(type=socket.SOCK_DGRAM)
        rx.setblocking(False)
        fd = rx.detach()
        self.addCleanup(tx.close)
        with patch('desk_orchestrator.gmmk_slider.find_device', return_value='/dev/hidraw9'), \
             patch('desk_orchestrator.gmmk_slider.os.open', return_value=fd) as opened:
            device = SliderDevice('/dev/input/event0')
        self.assertEqual(opened.call_args.args[1] & os.O_ACCMODE, os.O_RDONLY)
        stream = device.async_read_loop()
        try:
            tx.send(bytes.fromhex('04 f8 32 00 05 01 0a 00 00'))
            tx.send(b'unrelated')
            tx.send(bytes.fromhex('04 f8 32 00 06 01 2a 00 00'))
            event = await asyncio.wait_for(anext(stream), 1)
            self.assertEqual((event.type, event.code, event.value), (3, 32, 298))
            waiting = asyncio.create_task(anext(stream))
            await asyncio.sleep(.01)
            waiting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiting
            self.assertFalse(asyncio.get_running_loop().remove_reader(fd))
        finally:
            await stream.aclose()
            device.close()


if __name__ == '__main__':
    unittest.main()
