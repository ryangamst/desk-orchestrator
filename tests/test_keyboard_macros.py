"""Custom keyboard macros use the same task ownership and USB safety path."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.key_commands import COMMANDS, command_actions, command_label, editable_sequence, run_key_command, shortcut_report
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
MACRO = {'label': 'Copy then paste', 'steps': [
    {'shortcut': 'Ctrl+C'}, {'wait': .25}, {'shortcut': 'Ctrl+V'}, {'command': 'play_pause'}]}


class ShortcutTests(unittest.TestCase):
    def test_existing_presets_become_editable_without_changing_reports(self):
        for name, (_, kind, _) in COMMANDS.items():
            with self.subTest(command=name):
                sequence = editable_sequence(name)
                self.assertEqual(command_actions(sequence), command_actions(name))
                if kind == 'keyboard':
                    self.assertIsInstance(sequence, dict)
                else:
                    self.assertEqual(sequence, name)
        mixed = {'label': 'Saved', 'steps': [{'command': 'page_down'}, {'wait': .3},
                                          {'command': 'pause'}, {'shortcut': 'Cmd+Space'}]}
        original = copy.deepcopy(mixed)
        converted = editable_sequence(mixed)
        self.assertEqual(converted['steps'][0], {'shortcut': 'pagedown'})
        self.assertEqual(converted['steps'][2], {'command': 'pause'})
        self.assertEqual(command_actions(converted), command_actions(original))
        self.assertEqual(mixed, original)
        self.assertEqual(shortcut_report('SuperKey+R'), shortcut_report('Win+R'))

    def test_unnamed_sequence_label_and_invalid_drafts(self):
        self.assertEqual(command_label({'label': '', 'steps': [{'shortcut': 'Win+R'}, {'wait': .5},
                                                            {'shortcut': 'Enter'}]}), 'Win+R → Wait 0.5s → Enter')
        for draft in ({'steps': None}, {'steps': [None]}, {'steps': [{'command': []}]}):
            self.assertEqual(command_label(draft), 'Custom shortcut / macro')

    def test_modifier_aliases_extended_and_simultaneous_keys(self):
        for text, expected in [('Ctrl+Shift+S', [3, 0, 0x16]), ('Cmd+C', [8, 0, 6]),
                               ('RightAlt+Enter', [64, 0, 0x28]), ('AltGr+Q', [64, 0, 0x14]),
                               ('F24', [0, 0, 0x73]), ('0x87', [0, 0, 0x87]),
                               ('Ctrl+Page Down', [1, 0, 0x4e]), ('Shift+Equal', [2, 0, 0x2e]),
                               ('Ctrl+A+B', [1, 0, 4, 5]), ('Win', [8, 0]), ('Numpad0', [0, 0, 0x62])]:
            with self.subTest(text=text):
                self.assertEqual(shortcut_report(text), bytes(expected + [0] * (8 - len(expected))))
        self.assertEqual(shortcut_report('a'), shortcut_report('A'))
        self.assertEqual(shortcut_report('left_control+page_down'), shortcut_report('Ctrl+PageDown'))

    def test_bad_shortcuts_and_rollover_are_rejected(self):
        for text in ['', None, [], 'Ctrl++', 'Fn+A', '0x00', '0xe0', '0x100', 'Ctrl+Control+A',
                     'A+0x04', 'A+B+C+D+E+F+G', 'Ctrl+unknown', 'A' * 201]:
            with self.subTest(text=text), self.assertRaises(DeskError):
                shortcut_report(text)

    def test_macro_validation_is_complete_and_bounded(self):
        self.assertEqual(command_actions(MACRO), [('keyboard', shortcut_report('Ctrl+C')),
            ('wait', .25), ('keyboard', shortcut_report('Ctrl+V')), ('consumer', b'\xcd\x00')])
        invalid = [None, [], {}, {'label': 'test', 'steps': []},
                   {'label': 'x' * 81, 'steps': [{'shortcut': 'A'}]},
                   {'label': 'test', 'steps': [{'wait': 1}]},
                   {'label': 'test', 'steps': [{'shortcut': 'A'}] * 101},
                   {'label': 'test', 'steps': [{'shortcut': 'A'}, {'wait': 40}, {'wait': 21}]},
                   {'label': 'test', 'steps': [{'shortcut': 'A', 'wait': 1}]},
                   {'label': 'test', 'steps': [{'command': MACRO}]},
                   {'label': 'test', 'steps': [{'shell': 'anything'}]}]
        invalid += [{'label': '', 'steps': [{'shortcut': 'A'}, {'wait': value}]} for value in
                    [-1, 61, True, '2', float('nan'), float('inf')]]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(DeskError):
                command_actions(value)

    def test_gadget_descriptor_advertises_extended_keyboard_range(self):
        script = (SEED.parent.parent / 'deploy/setup-gadget.sh').read_text()
        self.assertIn(r'\x26\xdf\x00\x05\x07\x19\x00\x2a\xdf\x00', script)
        self.assertIn('== 0x0102', script)


class MacroIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, 'macro-tests-only')
        self.app = create_app(self.temp.name, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['desk_store']
        def edit(cfg):
            cfg['scenes']['personal'].update(confirmed=True, steps=[{'kind': 'wait', 'seconds': 0}],
                                            key_commands={'KEY_NUMLOCK': copy.deepcopy(MACRO)})
            cfg['inventory']['kvm']['settings']['confirmed'] = True
        self.cfg = self.store.update(self.store.read()['revision'], edit)
        atomic_json(Path(self.temp.name) / 'status.json', dict(scene='personal', status='commands_sent'))
        self.hardware = Hardware(self.cfg)
        self.runner = Runner(self.cfg, self.hardware, emit=lambda _: None)
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf='macro-test')

    def form(self, **changes):
        return dict(csrf='macro-test', revision=self.cfg['revision'], label='Personal Computing', key='KEY_KP1',
                    steps='[{"kind":"wait","seconds":0}]', confirmed='on', key_commands_present='1',
                    command_KEY_NUMLOCK='custom', macro_KEY_NUMLOCK=json.dumps(MACRO)) | changes

    def test_roundtrip_editor_preview_virtual_state_and_preset_conversion(self):
        self.assertEqual(self.client.post('/tasks/personal/edit', data=self.form()).status_code, 302)
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands']['KEY_NUMLOCK'], MACRO)
        for url in ['/tasks/personal/edit', '/tasks/personal/preview']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Copy then paste', response.data)
        self.assertEqual(self.client.get('/api/numpad').json['key_commands']['KEY_NUMLOCK'], 'Copy then paste')
        self.assertEqual(self.client.post('/tasks/personal/edit', data=self.form(
            revision=self.store.read()['revision'], command_KEY_NUMLOCK='mute', macro_KEY_NUMLOCK='bad stale draft')).status_code, 302)
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands']['KEY_NUMLOCK'], 'mute')

    def test_invalid_macro_returns_form_and_retains_existing_configuration(self):
        for macro in ['{', json.dumps({'label': 'My draft', 'steps': [{'shortcut': 'Ctrl+NotAKey'}]}),
                      json.dumps({'label': 'My draft', 'steps': []})]:
            response = self.client.post('/tasks/personal/edit', data=self.form(macro_KEY_NUMLOCK=macro))
            self.assertEqual(response.status_code, 422, response.data)
            self.assertEqual(self.store.read()['revision'], self.cfg['revision'])
            self.assertEqual(self.store.read()['scenes']['personal']['key_commands']['KEY_NUMLOCK'], MACRO)
        response = self.client.post('/tasks/personal/edit', data=self.form(key='KEY_NUMLOCK'))
        self.assertEqual(response.status_code, 422)

    def test_mixed_macro_reports_delays_and_releases_in_order(self):
        events = []
        with patch('desk_orchestrator.hardware.os.open', side_effect=[41, 42, 43]) as opened, \
             patch('desk_orchestrator.hardware.os.close') as close, \
             patch.object(Hardware, 'write_report', side_effect=lambda fd, report: events.append((fd, report))), \
             patch('desk_orchestrator.hardware.time.sleep', side_effect=lambda seconds: events.append(('wait', seconds))):
            self.hardware.send_key_command(MACRO)
        self.assertEqual([args.args[0] for args in opened.call_args_list], ['/dev/hidg0', '/dev/hidg0', '/dev/hidg1'])
        self.assertEqual([e for e in events if e[0] != 'wait'], [
            (41, shortcut_report('Ctrl+C')), (41, bytes(8)), (42, shortcut_report('Ctrl+V')), (42, bytes(8)),
            (43, b'\xcd\x00'), (43, bytes(2))])
        self.assertLess(events.index((41, bytes(8))), events.index(('wait', .25)))
        self.assertLess(events.index(('wait', .25)), events.index((42, shortcut_report('Ctrl+V'))))
        self.assertEqual(close.call_args_list, [call(41), call(42), call(43)])

    def test_failure_releases_keys_closes_device_and_stops_macro(self):
        with patch('desk_orchestrator.hardware.os.open', return_value=42), \
             patch('desk_orchestrator.hardware.os.close') as close, \
             patch('desk_orchestrator.hardware.time.sleep'), \
             patch.object(Hardware, 'write_report', side_effect=[None, None, OSError('failed'), None]) as write:
            with self.assertRaises(OSError):
                self.hardware.send_key_command(MACRO)
            self.assertEqual(write.call_args_list[-1], call(42, bytes(8)))
            self.assertEqual(write.call_count, 4)
            self.assertEqual(close.call_count, 2)

    def test_macro_skips_hardware_preflight_and_retains_dry_run_and_busy_lock(self):
        self.cfg['kvm'].update(device='/dev/null', consumer_device='/dev/missing-test-hid')
        with patch.object(Hardware, 'execute') as execute:
            run_key_command(self.runner, 'KEY_NUMLOCK', live=True)
            execute.assert_called_once()
            execute.reset_mock()
            self.assertEqual(run_key_command(self.runner, 'KEY_NUMLOCK', dry_scene='personal')['status'], 'dry_run')
            execute.assert_not_called()
            with exclusive(Path(self.temp.name) / 'scene.lock'), self.assertRaises(DeskError):
                run_key_command(self.runner, 'KEY_NUMLOCK', live=True)
        invalid = {'label': '', 'steps': [{'shortcut': 'A'}, {'shortcut': 'bogus'}]}
        with patch('desk_orchestrator.hardware.os.open') as opened, self.assertRaises(DeskError):
            self.hardware.send_key_command(invalid)
        opened.assert_not_called()

    def test_live_macro_owns_lock_without_changing_active_task(self):
        before = (Path(self.temp.name) / 'status.json').read_text()
        def execute(step):
            self.assertEqual(step, dict(kind='keyboard', command=MACRO))
            with self.assertRaises(DeskError), exclusive(Path(self.temp.name) / 'scene.lock'):
                pass
        with patch.object(Hardware, 'check') as check, patch.object(Hardware, 'execute', side_effect=execute):
            result = run_key_command(self.runner, 'KEY_NUMLOCK', live=True, expected_scene='personal')
            self.assertEqual(result['status'], 'commands_sent')
            check.assert_not_called()
        self.assertEqual((Path(self.temp.name) / 'status.json').read_text(), before)
        self.assertEqual(command_label({'label': '  ', 'steps': []}), 'Custom shortcut / macro')
