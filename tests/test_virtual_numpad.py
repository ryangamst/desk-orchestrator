"""Live adapter tests: real execution pipeline, mocked physical hardware."""
import copy
import json
import tempfile
import unittest
import uuid
from html.parser import HTMLParser
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from desk_orchestrator.core import DeskError, atomic_json, exclusive
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'


class VirtualNumpadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, 'virtual-input-tests-only')
        self.app = create_app(self.temp.name, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['desk_store']
        self.check = patch('desk_orchestrator.hardware.Hardware.check', return_value=None).start()
        self.execute = patch('desk_orchestrator.hardware.Hardware.execute').start()
        self.addCleanup(patch.stopall)
        self.configure()
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf='test-csrf')

    def configure(self):
        def edit(cfg):
            cfg['scenes']['personal'].update(confirmed=False, steps=[{'kind':'wait', 'seconds':0}], controls={})
            cfg['keypad']['bindings']['KEY_KP1'] = 'personal'
            cfg['keypad']['controls'] = {}
        self.store.update(self.store.read()['revision'], edit)

    def controls(self, invert=False):
        def edit(cfg):
            for control in ('dial', 'slider'):
                cfg['keypad']['controls'][control] = dict(device='/dev/input/event7', mode='keys',
                    code=115 if control == 'dial' else 116, down_code=114 if control == 'dial' else 117, invert=invert)
                cfg['scenes']['personal']['controls'][control] = dict(mode='volume', device='oppo', increase='volume_up', decrease='volume_down')
            cfg['inventory']['oppo']['settings']['commands'].update({
                name: dict(verified=True, sequence=[dict(key=key, count=1, gap=.02)])
                for name, key in [('volume_up', 'KEY_VOLUMEUP'), ('volume_down', 'KEY_VOLUMEDOWN')]})
        self.store.update(self.store.read()['revision'], edit)
        atomic_json(Path(self.temp.name)/'status.json', dict(scene='personal', status='commands_sent'))

    def data(self, **changes):
        return dict(csrf='test-csrf', revision=str(self.store.read()['revision']),
                    request_id=uuid.uuid4().hex, kind='key', key='KEY_KP1', **changes)

    def press(self, data=None):
        return self.client.post('/api/numpad/press', data=data or self.data())

    def control_data(self, name='dial', direction='increase'):
        data = self.data()
        data.update(kind='control', control=name, direction=direction, active_task='personal')
        return data

    def test_one_shot_keeps_active_keyboard_and_selected_volume_source(self):
        self.controls()
        def edit(cfg):
            cfg['scenes']['audio_power'] = dict(label='Audio Power', keep_active_task=True,
                steps=[dict(kind='ir', device='amplifier', command='power_on')])
            cfg['keypad']['bindings']['KEY_KP9'] = 'audio_power'
            cfg['inventory']['amplifier']['settings']['commands'].update(
                copy.deepcopy(cfg['inventory']['oppo']['settings']['commands']))
            cfg['scenes']['personal']['key_commands'] = {'KEY_KP0': dict(label='Copy', steps=[dict(shortcut='Ctrl+C')])}
            cfg['scenes']['personal']['controls']['dial']['click'] = dict(action='cycle_volume', sources=[
                dict(device=device, increase='volume_up', decrease='volume_down') for device in ('oppo', 'amplifier')])
        self.store.update(self.store.read()['revision'], edit)
        mapping = self.store.read()['scenes']['personal']['controls']['dial']
        atomic_json(Path(self.temp.name)/'status.json', dict(scene='personal', status='commands_sent',
            control_targets={'dial': dict(mapping=mapping, index=2)}))
        before = self.client.get('/api/numpad').json
        data = self.data()
        data['key'] = 'KEY_KP9'
        response = self.press(data)
        self.assertEqual(response.status_code, 200, response.json)
        self.execute.assert_called_once_with(dict(kind='ir', device='amplifier', command='power_on'))
        after = self.client.get('/api/numpad').json
        for field in ('active_task', 'key_commands', 'controls'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after['controls']['dial']['source_index'], 2)
        self.execute.reset_mock()
        response = self.press(self.control_data())
        self.assertEqual(response.status_code, 200, response.json)
        self.execute.assert_called_once_with(dict(kind='ir', device='amplifier', command='volume_up'))

    def test_click_switches_target_shared_with_physical_controls(self):
        self.controls(invert=True)
        def edit(cfg):
            cfg['keypad']['controls']['dial']['press_code'] = 113
            cfg['inventory']['amplifier']['settings']['commands'].update(
                copy.deepcopy(cfg['inventory']['oppo']['settings']['commands']))
            cfg['scenes']['personal']['controls']['dial']['click'] = dict(action='cycle_volume', sources=[
                dict(device=device, increase='volume_up', decrease='volume_down') for device in ('oppo', 'amplifier')])
        self.store.update(self.store.read()['revision'], edit)
        initial = self.client.get('/api/numpad').json['controls']['dial']
        self.assertTrue(initial['switch_enabled'])
        self.assertEqual(initial['source_index'], 1)
        self.assertEqual(initial['source_count'], 2)
        self.assertEqual(initial['target'], 'OPPO HA-1')
        with patch('desk_orchestrator.virtual_numpad.time', SimpleNamespace(time=lambda: 100)):
            response = self.press(self.control_data(direction='switch'))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json['result']['device'], 'amplifier')
        self.assertEqual(response.json['message'], 'Wheel volume target switched.')
        self.execute.assert_not_called()
        state = self.client.get('/api/numpad').json
        self.assertEqual(state['controls']['dial']['target'], 'S.M.S.L DA-9')
        self.assertEqual(state['controls']['slider']['target'], 'OPPO HA-1')
        with patch('desk_orchestrator.virtual_numpad.time', SimpleNamespace(time=lambda: 101)):
            self.assertEqual(self.press(self.control_data()).status_code, 200)
        self.execute.assert_called_once_with(dict(kind='ir', device='amplifier', command='volume_down'))
        from desk_orchestrator.controls import run_control
        from desk_orchestrator.core import Runner
        from desk_orchestrator.hardware import Hardware
        cfg = self.store.read()
        run_control(Runner(cfg, Hardware(cfg), emit=lambda _: None), 'dial', 'switch', live=True)
        self.assertEqual(self.client.get('/api/numpad').json['controls']['dial']['target'], 'OPPO HA-1')

    def test_click_requires_configuration_and_only_dial_can_switch(self):
        self.controls()
        for data in (self.control_data(direction='switch'), self.control_data('slider', 'switch')):
            self.assertEqual(self.press(data).status_code, 409)
        self.execute.assert_not_called()

    def test_press_executes_current_binding_live_and_updates_shared_active_task(self):
        response = self.press()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['result']['status'], 'commands_sent')
        self.execute.assert_called_once_with(dict(kind='wait', seconds=0))
        self.check.assert_not_called()
        status = self.client.get('/api/numpad').json
        self.assertEqual(status['active_task'], 'personal')
        self.assertFalse(status['busy'])
        from desk_orchestrator.diagnostics import EventLog
        events = EventLog(self.temp.name).read()['events']
        self.assertTrue(any(e['message'] == 'Virtual numpad input received' for e in events))
        self.assertTrue(any(e['message'] == 'Task completed' for e in events))
        self.assertEqual(len({e['trace'] for e in events}), 1)

    def test_requires_authentication_csrf_post_and_current_revision(self):
        self.assertEqual(self.client.get('/api/numpad/press').status_code, 405)
        data = self.data(); data['csrf'] = 'wrong'
        self.assertEqual(self.press(data).status_code, 400)
        data = self.data(); data['revision'] = '0'
        self.assertEqual(self.press(data).status_code, 409)
        with self.client.session_transaction() as session:
            session.pop('authenticated')
        self.assertEqual(self.press().status_code, 302)
        self.assertEqual(self.client.get('/api/numpad').status_code, 302)
        self.execute.assert_not_called()

    def test_unmapped_invalid_inputs_and_stale_binding_never_execute(self):
        for updates in [dict(key='KEY_KP9'), dict(key='KEY_POWER'), dict(kind='other'),
                        dict(kind='control', control='dial', direction='increase'), dict(request_id='bad')]:
            data=self.data(); data.update(updates)
            self.assertEqual(self.press(data).status_code, 409)
        old = self.data()
        self.store.update(self.store.read()['revision'], lambda cfg: cfg['keypad']['bindings'].update(KEY_KP1='work'))
        self.assertEqual(self.press(old).status_code, 409)
        self.execute.assert_not_called()

    def test_hardware_preflight_is_not_required(self):
        self.check.side_effect = DeskError('IR command must be verified')
        response = self.press()
        self.assertEqual(response.status_code, 200)
        self.check.assert_not_called()
        self.execute.assert_called_once()

    def test_partial_failure_stops_task_and_disarms_controls(self):
        self.store.update(self.store.read()['revision'], lambda cfg: cfg['scenes']['personal'].update(
            steps=[dict(kind='wait', seconds=i) for i in (0, 1, 2)]))
        self.execute.side_effect = [None, DeskError('adapter failed')]
        self.assertEqual(self.press().status_code, 409)
        self.assertEqual(self.execute.call_count, 2)
        state = json.loads((Path(self.temp.name)/'status.json').read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['completed_steps'], 1)
        self.assertIsNone(self.client.get('/api/numpad').json['active_task'])

    def test_busy_requests_are_not_queued(self):
        for lock in ('scene.lock', 'virtual-input.lock'):
            with self.subTest(lock=lock), exclusive(Path(self.temp.name)/lock):
                self.assertTrue(self.client.get('/api/numpad').json['busy'])
                self.assertEqual(self.press().status_code, 409)
        self.execute.assert_not_called()

    def test_request_deduplication_is_shared_across_app_instances(self):
        data = self.data()
        self.assertEqual(self.press(data).status_code, 200)
        other = create_app(self.temp.name, SEED).test_client()
        with other.session_transaction() as session:
            session.update(authenticated=True, csrf='test-csrf')
        response = other.post('/api/numpad/press', data=data)
        self.assertEqual(response.status_code, 409)
        self.assertIn('already received', response.json['error'])
        self.execute.assert_called_once()

    def test_key_debounce_and_control_rate_limit(self):
        with patch('desk_orchestrator.virtual_numpad.time', SimpleNamespace(time=lambda: 1000)):
            self.assertEqual(self.press().status_code, 200)
            self.assertEqual(self.press().status_code, 409)
        self.execute.assert_called_once()
        self.controls()
        with patch('desk_orchestrator.virtual_numpad.time', SimpleNamespace(time=lambda: 1001)):
            self.assertEqual(self.press(self.control_data()).status_code, 200)
            self.assertEqual(self.press(self.control_data()).status_code, 409)
        self.assertEqual(self.execute.call_count, 2)

    def test_dial_slider_and_reversed_direction_use_active_task_ir_commands(self):
        for invert in (False, True):
            self.controls(invert)
            for name in ('dial', 'slider'):
                for direction in ('increase', 'decrease'):
                    with self.subTest(invert=invert, name=name, direction=direction):
                        with patch('desk_orchestrator.virtual_numpad.time', SimpleNamespace(time=lambda: 10000 + self.execute.call_count)):
                            response = self.press(self.control_data(name, direction))
                        self.assertEqual(response.status_code, 200, response.json)
                        up = (direction == 'increase') != invert
                        self.execute.assert_called_with(dict(kind='ir', device='oppo', command='volume_up' if up else 'volume_down'))

    def test_control_cannot_switch_targets_after_physical_task_change(self):
        self.controls()
        atomic_json(Path(self.temp.name)/'status.json', dict(scene='work', status='commands_sent'))
        response = self.press(self.control_data())
        self.assertEqual(response.status_code, 409)
        self.assertIn('active task changed', response.json['error'])
        self.execute.assert_not_called()

    def test_state_poll_is_read_only_and_markup_has_live_buttons(self):
        response = self.client.get('/api/numpad')
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['active_task'])
        self.assertFalse(response.json['controls']['dial']['enabled'])
        self.check.assert_not_called()
        self.execute.assert_not_called()
        with patch('desk_orchestrator.web.scan_usb', return_value=dict(devices=[], candidates=[], warnings=[])):
            page = self.client.get('/').text
        self.assertIn('Virtual numpad', page)
        self.assertIn('data-virtual-key="KEY_KP1"', page)
        self.assertNotIn('Numpad mappings</h2>', page)
        with self.client.get('/static/virtual_numpad.js') as response:
            self.assertEqual(response.status_code, 200)

    def test_background_refresh_only_includes_overview_when_revision_changes(self):
        revision = self.store.read()['revision']
        response = self.client.get(f'/api/numpad?revision={revision}')
        self.assertNotIn('overview', response.json)
        self.check.assert_not_called()
        def edit(cfg):
            cfg['keypad']['bindings']['KEY_KP1'] = 'work'
            cfg['scenes']['work']['label'] = 'Work <updated>'
        self.store.update(revision, edit)
        response = self.client.get(f'/api/numpad?revision={revision}')
        self.assertEqual(response.status_code, 200)
        view = response.json['overview']
        self.assertEqual(view['mappings']['KEY_KP1']['task'], 'work')
        self.assertEqual(view['mappings']['KEY_KP1']['label'], 'Work <updated>')
        self.assertIn('Work &lt;updated&gt;', view['tasks'])
        self.assertNotIn('<script', view['tasks'])
        self.assertIn('keys mapped', view['stats'])
        self.assertTrue(all(call.kwargs.get('probe') is False for call in self.check.call_args_list))
        self.execute.assert_not_called()
        self.assertNotIn('overview', self.client.get(
            f'/api/numpad?revision={response.json["revision"]}').json)

    def test_keyboard_mappings_have_active_task_color_and_initial_markup(self):
        macro = dict(label='Save <document>', steps=[dict(shortcut='Ctrl+S')])
        def edit(cfg):
            cfg['scenes']['personal']['key_commands'] = dict(KEY_NUMLOCK='play_pause', KEY_KP9=macro)
            cfg['scenes']['work']['key_commands'] = dict(KEY_NUMLOCK='mute')
        self.store.update(self.store.read()['revision'], edit)

        class KeyParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.keys = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button' and 'data-virtual-key' in attrs:
                    self.keys[attrs['data-virtual-key']] = attrs

        for active, color, command in [('personal', 'lavender', 'Play / Pause'),
                                       ('work', 'blue', 'Mute'), (None, None, 'Unassigned')]:
            atomic_json(Path(self.temp.name)/'status.json', dict(scene=active, status='commands_sent'))
            state = self.client.get('/api/numpad').json
            self.assertEqual(state['active_color'], color)
            self.assertEqual(state['key_commands'].get('KEY_NUMLOCK', 'Unassigned'), command)
            page = self.client.get('/').text
            parser = KeyParser()
            parser.feed(page)
            key = parser.keys['KEY_NUMLOCK']
            self.assertEqual(key['title'], command)
            self.assertEqual(key.get('data-command-task'), active)
            self.assertNotIn('data-linked-task', key)
            self.assertEqual('mapped' in key['class'].split(), bool(active))
            if active:
                self.assertIn(f'tone-{color}', key['class'])
            if active == 'personal':
                self.assertEqual(parser.keys['KEY_KP9']['data-key-command'], 'Save <document>')
                self.assertIn('Save &lt;document&gt;', page)
            self.assertEqual(parser.keys['KEY_KP1']['data-linked-task'], 'personal')
            self.assertNotIn('data-key-command', parser.keys['KEY_KP1'])
        self.execute.assert_not_called()
