"""Media presets keep their native HID meaning; CH9328 must never substitute shortcuts."""
import json
import os
from pathlib import Path
import pty
import select
import tempfile
import unittest
import uuid
from unittest.mock import call, patch

from desk_orchestrator.core import DeskError, atomic_json
from desk_orchestrator.hardware import Hardware, RELEASE
from desk_orchestrator.key_commands import command_actions, serial_command_actions, shortcut_report
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
# USB HID Consumer page usages, independent of application and operating system.
MEDIA = {'play_pause': 0xCD, 'play': 0xB0, 'pause': 0xB1, 'previous_track': 0xB6,
         'next_track': 0xB5, 'stop': 0xB7, 'mute': 0xE2, 'volume_down': 0xEA, 'volume_up': 0xE9}
MACRO = {'label': 'Mixed', 'steps': [{'shortcut': 'Win+R'}, {'command': 'previous_track'}]}


class MediaSemanticsTests(unittest.TestCase):
    def test_presets_are_native_consumer_reports_and_ch9328_rejects_them_before_open(self):
        hardware = Hardware({'kvm': {'transport': 'ch9328', 'device': '/dev/ttyUSB0'}})
        for name, usage in MEDIA.items():
            with self.subTest(command=name):
                self.assertEqual(command_actions(name), [('consumer', usage.to_bytes(2, 'little'))])
                with patch('serial.Serial') as opened, self.assertRaisesRegex(DeskError, 'cannot send Consumer Control'):
                    hardware.send_key_command(name)
                opened.assert_not_called()
        with patch('serial.Serial') as opened, self.assertRaisesRegex(DeskError, 'Previous track'):
            hardware.send_key_command(MACRO)
        opened.assert_not_called()

    def test_ordinary_shortcuts_and_delays_still_resolve_unchanged(self):
        command = {'label': 'Run', 'steps': [{'shortcut': 'Super+R'}, {'wait': .25}, {'command': 'enter'}]}
        self.assertEqual(serial_command_actions(command), [
            ('keyboard', bytes([8, 0, 0x15, 0, 0, 0, 0, 0])), ('wait', .25),
            ('keyboard', shortcut_report('Enter'))])

    def test_gadget_sends_native_media_press_and_release_for_every_preset(self):
        hardware = Hardware({'kvm': {}})
        for name, usage in MEDIA.items():
            with self.subTest(command=name), patch('desk_orchestrator.hardware.os.open', return_value=42) as opened, \
                 patch('desk_orchestrator.hardware.os.close') as closed, \
                 patch('desk_orchestrator.hardware.time.sleep'), patch.object(Hardware, 'write_report') as write:
                hardware.send_key_command(name)
                self.assertEqual(opened.call_args.args[0], '/dev/hidg1')
                self.assertEqual(write.call_args_list, [call(42, usage.to_bytes(2, 'little')), call(42, b'\0\0')])
                closed.assert_called_once_with(42)


class SavedMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        set_password(self.root, 'native-media-tests')
        self.app = create_app(self.root, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['desk_store']
        def edit(cfg):
            cfg['inventory']['kvm']['settings'] = dict(transport='ch9328', device='/dev/ttyUSB0', confirmed=True)
            cfg['scenes']['personal'].update(steps=[dict(kind='wait', seconds=0)], controls={},
                key_commands={'KEY_NUMLOCK': 'previous_track'},
                media_shortcuts={'previous_track': 'Ctrl+Alt+P'})
        self.cfg = self.store.update(self.store.read()['revision'], edit)
        self.status = self.root / 'status.json'
        atomic_json(self.status, dict(scene='personal', status='commands_sent'))
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf='test')

    def press(self):
        return self.client.post('/api/numpad/press', data=dict(csrf='test', revision=str(self.store.read()['revision']),
            request_id=uuid.uuid4().hex, kind='key', key='KEY_NUMLOCK', active_task='personal'))

    def test_existing_shortcut_data_cannot_turn_a_media_command_into_ordinary_keys(self):
        before = self.status.read_bytes()
        with patch('serial.Serial') as opened:
            response = self.press()
            self.assertEqual(response.status_code, 409)
            self.assertIn('cannot send Consumer Control', str(response.json))
            opened.assert_not_called()
        self.assertEqual(self.status.read_bytes(), before)

    def test_editor_preserves_legacy_mapping_without_offering_new_media_presets(self):
        for url in ('/tasks/personal/edit', '/tasks/personal/preview'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Previous track', response.data)
            self.assertNotIn(b'<option value="next_track"', response.data)
            self.assertNotIn(b'<option value="play_pause"', response.data)
            self.assertNotIn(b'Media shortcuts on this computer', response.data)
            self.assertNotIn(b'media_shortcut_previous_track', response.data)
        data = dict(csrf='test', revision=self.cfg['revision'], label='Personal', key='KEY_KP1',
                    steps='[{"kind":"wait","seconds":0}]', key_commands_present='1',
                    command_KEY_NUMLOCK='previous_track')
        self.assertEqual(self.client.post('/tasks/personal/edit', data=data).status_code, 302)
        task = self.store.read()['scenes']['personal']
        self.assertEqual(task['key_commands'], {'KEY_NUMLOCK': 'previous_track'})
        self.assertEqual(task['media_shortcuts'], self.cfg['scenes']['personal']['media_shortcuts'])
        cfg = self.store.read()
        self.store.restore(cfg['revision'], {'format': 'desk-backup-1', 'config': json.loads(json.dumps(cfg))})
        self.assertEqual(self.store.read()['scenes']['personal'], task)

    def test_virtual_ordinary_macro_still_reaches_real_pyserial(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        def edit(cfg):
            cfg['inventory']['kvm']['settings']['device'] = os.ttyname(slave)
            cfg['scenes']['personal']['key_commands']['KEY_NUMLOCK'] = {
                'label': 'Run', 'steps': [{'shortcut': 'Win+R'}, {'wait': .25}, {'command': 'enter'}]}
        self.store.update(self.cfg['revision'], edit)
        with patch('desk_orchestrator.hardware.time.sleep'):
            response = self.press()
        self.assertEqual(response.status_code, 200, response.json)
        expected = b''.join([RELEASE, shortcut_report('Win+R'), RELEASE,
                             shortcut_report('Enter'), RELEASE, RELEASE])
        received = b''
        while len(received) < len(expected):
            self.assertTrue(select.select([master], [], [], 2)[0])
            received += os.read(master, 4096)
        self.assertEqual(received, expected)

    def test_saved_sequences_follow_active_task_and_reach_serial_unchanged(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        self.store.update(self.cfg['revision'], lambda cfg:
                          cfg['inventory']['kvm']['settings'].update(device=os.ttyname(slave)))
        mappings = {'personal': ('KEY_KP1', 'Win+R'), 'work': ('KEY_KP3', 'Cmd+Space'),
                    'steam_deck': ('KEY_KP2', 'Ctrl+Alt+T')}
        for scene, (launch_key, shortcut) in mappings.items():
            sequence = {'label': '', 'steps': [{'shortcut': shortcut}, {'wait': .25}, {'shortcut': 'Escape'}]}
            response = self.client.post(f'/tasks/{scene}/edit', data=dict(
                csrf='test', revision=self.store.read()['revision'], label=scene, key=launch_key,
                steps='[{"kind":"wait","seconds":0}]', controls_present='1', key_commands_present='1',
                command_KEY_NUMLOCK='custom', macro_KEY_NUMLOCK=json.dumps(sequence)))
            self.assertEqual(response.status_code, 302, response.data)
            self.assertEqual(self.store.read()['scenes'][scene]['key_commands']['KEY_NUMLOCK'], sequence)
        for index, (scene, (_, shortcut)) in enumerate(mappings.items()):
            with self.subTest(scene=scene):
                atomic_json(self.status, dict(scene=scene, status='commands_sent'))
                with patch('desk_orchestrator.hardware.time.sleep'), \
                     patch('desk_orchestrator.virtual_numpad.time') as clock:
                    clock.time.return_value = 1000 + index
                    response = self.client.post('/api/numpad/press', data=dict(
                        csrf='test', revision=self.store.read()['revision'], request_id=uuid.uuid4().hex,
                        kind='key', key='KEY_NUMLOCK', active_task=scene))
                self.assertEqual(response.status_code, 200, response.json)
                expected = b''.join([RELEASE, shortcut_report(shortcut), RELEASE,
                                     shortcut_report('Escape'), RELEASE, RELEASE])
                received = b''
                while len(received) < len(expected):
                    self.assertTrue(select.select([master], [], [], 2)[0])
                    received += os.read(master, 4096)
                self.assertEqual(received, expected)
        with patch('serial.Serial') as opened:
            response = self.client.post('/tasks/personal/edit', data=dict(
                csrf='test', revision=self.store.read()['revision'], label='Personal', key='KEY_KP1',
                steps='[{"kind":"wait","seconds":0}]', key_commands_present='1',
                command_KEY_NUMLOCK='custom', macro_KEY_NUMLOCK=json.dumps(
                    {'label': '', 'steps': [{'shortcut': 'Win+R'}, {'shortcut': 'InvalidKey'}]})))
            self.assertEqual(response.status_code, 422)
            opened.assert_not_called()
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands']['KEY_NUMLOCK']['steps'][0],
                         {'shortcut': 'Win+R'})

    def test_same_saved_previous_track_mapping_sends_native_report_in_gadget_mode(self):
        def edit(cfg):
            cfg['inventory']['kvm']['settings'] = dict(transport='gadget', device='/dev/hidg0', consumer_device='/dev/hidg1')
        self.store.update(self.cfg['revision'], edit)
        with patch.object(Hardware, 'send_key_report') as report:
            response = self.press()
        self.assertEqual(response.status_code, 200, response.json)
        report.assert_called_once_with('/dev/hidg1', b'\xb6\x00')
