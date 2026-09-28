"""Per-task IR numpad mappings share the existing task IR transmission path."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import call, patch

from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.key_commands import run_key_command, validate_bindings
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
IR = dict(kind='ir', device='oppo', command='volume_up')
KEYBOARD = {'label': '', 'steps': [{'shortcut': 'Win+R'}]}


class IRMappingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        set_password(self.root, 'ir-mapping-tests-only')
        self.app = create_app(self.root, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['desk_store']
        def edit(cfg):
            cfg['scenes'] = {
                'personal': dict(label='Personal', steps=[dict(kind='wait', seconds=0)],
                                 key_commands={'KEY_NUMLOCK': copy.deepcopy(IR)}),
                'work': dict(label='Work', steps=[dict(kind='wait', seconds=0)],
                             key_commands={'KEY_NUMLOCK': copy.deepcopy(KEYBOARD)})}
            cfg['keypad'].update(bindings={'KEY_KP1': 'personal', 'KEY_KP3': 'work'}, controls={})
            cfg['inventory']['oppo']['settings']['commands']['volume_up'] = dict(verified=True,
                sequence=[dict(remote='learned_oppo_up', key='captured', count=1, gap=.02)])
        self.cfg = self.store.update(self.store.read()['revision'], edit)
        self.status = self.root / 'status.json'
        atomic_json(self.status, dict(scene='personal', status='commands_sent'))
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf='test')

    def form(self, **changes):
        return dict(csrf='test', revision=self.store.read()['revision'], label='Personal', key='KEY_KP1',
                    steps='[{"kind":"wait","seconds":0}]', key_commands_present='1',
                    command_KEY_NUMLOCK='ir', ir_device_KEY_NUMLOCK='oppo', ir_command_KEY_NUMLOCK='volume_up',
                    command_KEY_KPSLASH='custom', macro_KEY_KPSLASH=json.dumps(KEYBOARD)) | changes

    def press(self, **changes):
        return self.client.post('/api/numpad/press', data=dict(csrf='test', revision=self.store.read()['revision'],
            request_id=uuid.uuid4().hex, kind='key', key='KEY_NUMLOCK', active_task='personal') | changes)

    def test_save_preview_backup_and_clear_preserve_keyboard_mapping(self):
        self.assertEqual(self.client.post('/tasks/personal/edit', data=self.form()).status_code, 302)
        mappings = self.store.read()['scenes']['personal']['key_commands']
        self.assertEqual(mappings, {'KEY_NUMLOCK': IR, 'KEY_KPSLASH': KEYBOARD})
        for page in ('edit', 'preview'):
            response = self.client.get(f'/tasks/personal/{page}')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Keyboard mapping', response.data)
            self.assertIn(b'OPPO HA-1', response.data)
            self.assertIn(b'volume_up', response.data)
        self.assertIn('IR · OPPO HA-1: volume_up', self.client.get('/api/numpad').json['key_commands']['KEY_NUMLOCK'])
        cfg = self.store.read()
        self.store.restore(cfg['revision'], {'format': 'desk-backup-1', 'config': cfg})
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands'], mappings)
        self.assertEqual(self.client.post('/tasks/personal/edit', data=self.form(command_KEY_NUMLOCK='')).status_code, 302)
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands'], {'KEY_KPSLASH': KEYBOARD})

    def test_invalid_references_and_reserved_keys_do_not_change_saved_config(self):
        for changes in ({'ir_device_KEY_NUMLOCK': ''}, {'ir_device_KEY_NUMLOCK': 'missing'},
                        {'ir_device_KEY_NUMLOCK': 'kvm'}, {'ir_command_KEY_NUMLOCK': ''},
                        {'ir_command_KEY_NUMLOCK': 'unknown'}, {'key': 'KEY_NUMLOCK'}):
            with self.subTest(changes=changes):
                response = self.client.post('/tasks/personal/edit', data=self.form(**changes))
                self.assertEqual(response.status_code, 422, response.data)
                self.assertEqual(self.store.read()['revision'], self.cfg['revision'])
        for value in (IR | {'count': 2}, IR | {'device': []}, IR | {'command': {}}):
            with self.assertRaises(DeskError):
                validate_bindings({'KEY_NUMLOCK': value}, self.cfg)
        with self.assertRaises(DeskError):
            self.store.update(self.cfg['revision'], lambda cfg:
                cfg['inventory']['oppo']['settings']['commands'].pop('volume_up'))
        self.assertEqual(self.store.read()['revision'], self.cfg['revision'])

    def test_ir_only_task_does_not_need_keyboard_hardware(self):
        def remove_keyboard(cfg):
            cfg['scenes']['work']['key_commands'] = {}
            cfg['inventory'].pop('kvm')
        cfg = self.store.update(self.cfg['revision'], remove_keyboard)
        self.assertNotIn('kvm', cfg)
        with patch.object(Hardware, 'irsend') as send, patch('desk_orchestrator.hardware.time.sleep'), \
             patch.object(Hardware, 'send_key_command') as keyboard, patch.object(Hardware, 'check') as check:
            response = self.press()
            self.assertEqual(response.status_code, 200, response.json)
            send.assert_called_once_with('SEND_ONCE', 'learned_oppo_up', 'captured')
            keyboard.assert_not_called()
            check.assert_not_called()

    def test_live_routing_lock_status_and_duplicate_protection(self):
        before = self.status.read_bytes()
        runner = Runner(self.cfg, Hardware(self.cfg), emit=lambda _: None)
        with exclusive(self.root / 'scene.lock'), patch.object(Hardware, 'execute') as execute:
            with self.assertRaises(DeskError):
                run_key_command(runner, 'KEY_NUMLOCK', live=True)
            execute.assert_not_called()
        with patch.object(Hardware, 'irsend') as send, patch('desk_orchestrator.hardware.time.sleep'), \
             patch.object(Hardware, 'send_key_command') as keyboard:
            request_id = uuid.uuid4().hex
            self.assertEqual(self.press(request_id=request_id).status_code, 200)
            self.assertEqual(self.press(request_id=request_id).status_code, 409)
            send.assert_called_once_with('SEND_ONCE', 'learned_oppo_up', 'captured')
            keyboard.assert_not_called()
            self.assertEqual(self.status.read_bytes(), before)
            atomic_json(self.status, dict(scene='work', status='commands_sent'))
            with self.assertRaises(DeskError):
                run_key_command(runner, 'KEY_NUMLOCK', live=True, expected_scene='personal')
            run_key_command(runner, 'KEY_NUMLOCK', live=True, expected_scene='work')
            keyboard.assert_called_once_with(KEYBOARD)
            self.assertEqual(send.call_count, 1)

    def test_dry_run_and_diagnostics_use_ir_without_sending(self):
        runner = Runner(self.cfg, Hardware(self.cfg), emit=lambda _: None)
        with patch.object(Hardware, 'execute') as execute:
            self.assertEqual(run_key_command(runner, 'KEY_NUMLOCK', dry_scene='personal')['status'], 'dry_run')
            execute.assert_not_called()
        with patch.object(Hardware, 'check') as check:
            passed = []
            self.assertEqual(runner.issues('personal', probe=True, passed=passed), [])
            self.assertIn(call(IR, probe=True), check.call_args_list)
            self.assertIn('No IR command was sent', passed[0]['message'])
            self.assertEqual(passed[0]['device'], 'oppo')

    def test_send_failure_keeps_task_and_releases_lock(self):
        before = self.status.read_bytes()
        with patch.object(Hardware, 'irsend', side_effect=DeskError('LIRC unavailable')) as send:
            response = self.press()
            self.assertEqual(response.status_code, 409)
            self.assertIn('LIRC unavailable', str(response.json))
            self.assertEqual(send.call_count, 1)
        self.assertEqual(self.status.read_bytes(), before)
        with exclusive(self.root / 'scene.lock'):
            pass
