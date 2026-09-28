"""CH9328 behavior tested with fake UARTs and a real pyserial pseudo-terminal."""
import copy
import json
import os
from pathlib import Path
import pty
import select
import tempfile
import unittest
from unittest.mock import Mock, patch

import serial

from desk_orchestrator.configure_keyboard import choose_device, save_configuration
from desk_orchestrator.config_store import ConfigStore
from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive, load_config
from desk_orchestrator.hardware import Hardware, RELEASE, kvm_reports
from desk_orchestrator.key_commands import run_key_command, shortcut_report
from desk_orchestrator.keyboard_transport import serial_devices, validate_settings
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
SETTINGS = dict(transport='ch9328', device='/dev/serial/by-id/test-uart', baudrate=9600,
                key_delay=.08, confirmed=True)


class SerialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = load_config(SEED)
        self.cfg['runtime']['directory'] = self.temp.name
        self.cfg['kvm'] = copy.deepcopy(SETTINGS)
        self.hw = Hardware(self.cfg)
        self.uart = Mock()
        self.uart.write.return_value = 8
        self.open = patch('serial.Serial')
        self.serial = self.open.start()
        self.addCleanup(self.open.stop)
        self.serial.return_value.__enter__.return_value = self.uart
        self.sleep = patch('desk_orchestrator.hardware.time.sleep').start()
        self.addCleanup(patch.stopall)

    def reports(self):
        return [call.args[0] for call in self.uart.write.call_args_list]

    def test_switch_all_ports_with_raw_reports_and_exclusive_session(self):
        for port in range(1, 5):
            self.uart.reset_mock()
            self.hw.switch_kvm(port)
            self.assertEqual(self.reports(), [RELEASE, *kvm_reports(port), RELEASE])
        self.assertTrue(self.serial.call_args.kwargs['exclusive'])
        self.assertEqual(self.serial.call_args.kwargs['baudrate'], 9600)
        self.assertEqual(self.serial.call_args.kwargs['write_timeout'], 2)
        self.assertFalse(self.serial.call_args.kwargs['rtscts'])

    def test_macro_single_connection_timing_and_releases(self):
        self.hw.send_key_command({'label': 'Copy paste', 'steps': [
            {'shortcut': 'Ctrl+C'}, {'wait': .25}, {'shortcut': 'Ctrl+V'}]})
        self.serial.assert_called_once()
        self.assertEqual(self.reports(), [RELEASE, shortcut_report('Ctrl+C'), RELEASE,
                                         shortcut_report('Ctrl+V'), RELEASE, RELEASE])
        self.sleep.assert_any_call(.25)

    def test_consumer_macro_rejected_before_opening_or_sending_any_prefix(self):
        for command in ('play_pause', {'label': 'Mixed', 'steps': [{'shortcut': 'Ctrl+C'}, {'command': 'play_pause'}]}):
            with self.assertRaisesRegex(DeskError, 'cannot send Consumer Control'):
                self.hw.send_key_command(command)
        self.serial.assert_not_called()
        self.uart.write.assert_not_called()

    def test_disconnect_timeout_short_write_and_interrupt_attempt_release_without_replay(self):
        for failure in (serial.SerialException('gone'), serial.SerialTimeoutException('timeout'), 3, KeyboardInterrupt()):
            self.uart.reset_mock()
            self.uart.write.side_effect = [8, failure, 8]
            with self.assertRaises((DeskError, KeyboardInterrupt)):
                self.hw.switch_kvm(3)
            self.assertEqual(self.reports(), [RELEASE, kvm_reports(3)[0], RELEASE])
        self.assertEqual(self.serial.return_value.__exit__.call_count, 4)

    def test_read_only_probe_and_dry_run_do_not_open_uart(self):
        self.cfg['kvm']['device'] = '/dev/null'
        self.assertIn('No port was opened', self.hw.check({'kind': 'kvm', 'port': 3}, probe=True))
        self.cfg['scenes']['bridge_test'] = dict(steps=[dict(kind='kvm', port=3)])
        Runner(self.cfg, self.hw, emit=lambda _: None).run('bridge_test')
        self.serial.assert_not_called()

    def test_key_commands_share_scene_lock_and_preserve_active_task(self):
        self.cfg['scenes']['personal']['key_commands'] = {'KEY_NUMLOCK': 'space'}
        state = Path(self.temp.name) / 'status.json'
        atomic_json(state, dict(scene='personal', status='commands_sent'))
        before = state.read_bytes()
        runner = Runner(self.cfg, self.hw, emit=lambda _: None)
        with exclusive(Path(self.temp.name) / 'scene.lock'), self.assertRaises(DeskError):
            run_key_command(runner, 'KEY_NUMLOCK', live=True)
        self.serial.assert_not_called()
        run_key_command(runner, 'KEY_NUMLOCK', live=True)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(self.reports(), [RELEASE, shortcut_report('Space'), RELEASE, RELEASE])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_real_pyserial_writes_expected_bytes_and_reopens_after_disconnect(self):
        cfg = load_config(SEED)
        cfg['runtime']['directory'] = self.temp.name
        cfg['kvm'] = copy.deepcopy(SETTINGS)
        link = self.root / 'serial-adapter'
        for _ in range(2):
            master, slave = pty.openpty()
            try:
                link.symlink_to(os.ttyname(slave))
                # The /dev alias used on the real Pi is replaced by this PTY node.
                cfg['kvm']['device'] = str(link.resolve())
                with patch('desk_orchestrator.hardware.time.sleep'):
                    Hardware(cfg).switch_kvm(3)
                expected = b''.join([RELEASE, *kvm_reports(3), RELEASE])
                result = b''
                while len(result) < len(expected):
                    self.assertTrue(select.select([master], [], [], 2)[0])
                    result += os.read(master, 4096)
                self.assertEqual(result, expected)
            finally:
                link.unlink()
                os.close(master)
                os.close(slave)

    def test_discovery_prefers_by_id_then_by_path_and_never_guesses_multiple(self):
        for directory in ('serial/by-id', 'serial/by-path'):
            (self.root / directory).mkdir(parents=True)
        port = self.root / 'ttyUSB0'
        port.touch()
        identity = self.root / 'serial/by-id/adapter'
        location = self.root / 'serial/by-path/usb-port'
        identity.symlink_to(port)
        location.symlink_to(port)
        devices = serial_devices(self.root)
        self.assertEqual(choose_device(None, devices), str(identity))
        self.assertEqual(choose_device(str(port), devices), str(identity))
        identity.unlink()
        self.assertEqual(serial_devices(self.root)[0]['path'], str(location))
        (self.root / 'ttyUSB1').touch()
        with self.assertRaisesRegex(DeskError, 'exactly one'):
            choose_device(None, serial_devices(self.root))
        location.unlink()
        (self.root / 'ttyUSB1').unlink()
        with self.assertRaisesRegex(DeskError, 'stable'):
            choose_device(None, serial_devices(self.root))

    def test_config_validation_and_legacy_default(self):
        validate_settings(dict(device='/dev/hidg0'))
        validate_settings(SETTINGS)
        for change in (dict(transport='other'), dict(device='relative'), dict(baudrate=115200),
                       dict(baudrate=True), dict(key_delay=0), dict(confirmed='yes')):
            with self.assertRaises(DeskError):
                validate_settings(SETTINGS | change)

    def test_commissioning_backs_up_and_preserves_scenes_and_media_bindings(self):
        store = ConfigStore(self.root, SEED)
        cfg = store.update(1, lambda cfg: cfg['scenes']['personal'].update(key_commands={'KEY_NUMLOCK': 'play_pause'}))
        with patch('serial.Serial') as opened:
            save_configuration(SETTINGS['device'], self.root, SEED)
            opened.assert_not_called()
        updated = store.read()
        self.assertEqual(updated['kvm'], SETTINGS)
        self.assertEqual(updated['scenes'], cfg['scenes'])
        self.assertEqual(updated['keypad'], cfg['keypad'])
        self.assertEqual(json.loads(next(self.root.glob('controller.before-ch9328-*.json')).read_text()), cfg)

    def test_hardware_editor_roundtrip_and_task_notice(self):
        set_password(self.root, 'bridge-test-password')
        app = create_app(self.root, SEED)
        app.testing = True
        client = app.test_client()
        store = app.extensions['desk_store']
        with client.session_transaction() as session:
            session.update(authenticated=True, csrf='test')
        cfg = store.read()
        item = cfg['inventory']['kvm']
        data = dict(csrf='test', revision=cfg['revision'], name=item['name'], type=item['type'],
                    method='usb_hid', notes=item['notes'], settings=json.dumps(SETTINGS))
        self.assertEqual(client.post('/hardware/kvm/edit', data=data).status_code, 302)
        self.assertEqual(store.read()['kvm'], SETTINGS)
        with patch('desk_orchestrator.keyboard_transport.serial_devices', return_value=[
                dict(path=SETTINGS['device'], node='/dev/ttyUSB0', stable=True, writable=True)]):
            response = client.get('/hardware/kvm/edit')
        self.assertEqual(response.status_code, 200)
        self.assertIn(SETTINGS['device'].encode(), response.data)
        self.assertIn(b'Your CH9328 connection', client.get('/tasks/personal/edit').data)
        data.update(revision=store.read()['revision'], settings=json.dumps(SETTINGS | dict(baudrate=115200)))
        self.assertEqual(client.post('/hardware/kvm/edit', data=data).status_code, 422)
        self.assertEqual(store.read()['kvm'], SETTINGS)
