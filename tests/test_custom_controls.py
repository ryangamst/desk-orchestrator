import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk_orchestrator import key_learning, numpad_profiles
from desk_orchestrator.core import DeskError, atomic_json
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'


def layout():
    return dict(name='Controls pad', width=5, height=4, keys=[], controls=[
        dict(id='dial', label='Volume', x=0, y=0, w=2, h=2),
        dict(id='slider', label='Level', x=3, y=0, w=1, h=3, orientation='vertical')])


def sources():
    return dict(dial=dict(mode='relative', device='/dev/input/event9', code=8, press_code=113),
                slider=dict(mode='absolute', device='/dev/input/event9', code=32, deadband=4))


class ControlLayoutValidationTests(unittest.TestCase):
    def test_layouts_without_controls_remain_valid(self):
        value = layout(); value.pop('controls')
        numpad_profiles.validate_profile(value)

    def test_control_geometry_and_identity_rules(self):
        numpad_profiles.validate_profile(layout())
        for change in [lambda p: p['controls'].append(copy.deepcopy(p['controls'][0])),
                       lambda p: p['controls'][0].update(id='second_dial'),
                       lambda p: p['controls'][0].update(h=1),
                       lambda p: p['controls'][1].update(x=1),
                       lambda p: p['controls'][1].update(x=5),
                       lambda p: p['controls'][1].update(orientation='diagonal'),
                       lambda p: p['controls'][1].update(w=.5),
                       lambda p: p['keys'].append(dict(id='KEY_CUSTOM_' + 'a'*32, label='Key', x=0, y=0, w=1, h=1, code=None))]:
            value = layout(); change(value)
            with self.subTest(value=value), self.assertRaises(DeskError):
                numpad_profiles.validate_profile(value)


class CustomControlWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, 'custom-control-test')
        self.app = create_app(self.temp.name, SEED); self.app.testing = True
        self.client = self.app.test_client(); self.store = self.app.extensions['desk_store']
        with self.client.session_transaction() as session: session.update(authenticated=True, csrf='test')

    def post(self, url, **data):
        return self.client.post(url, data=dict(csrf='test', revision=self.store.read()['revision'], **data))

    def save(self, value=None, signals=None):
        return self.post('/numpad/profiles/custom/save', profile=json.dumps(layout() if value is None else value),
                         controls=json.dumps(sources() if signals is None else signals), device='/dev/input/event9', grab='yes')

    def test_save_switch_and_render_unique_live_controls(self):
        original = self.store.read()['keypad']
        self.assertEqual(self.save().status_code, 200)
        self.post('/numpad/profiles/activate', profile='custom')
        self.assertEqual(self.store.read()['keypad']['controls'], sources())
        page = self.client.get('/').text
        self.assertEqual(page.count('id="virtual-dial"'), 1)
        self.assertEqual(page.count('id="virtual-slider"'), 1)
        self.assertIn('data-layout-control="slider"', page)
        self.assertIn('aria-orientation="vertical"', page)
        self.assertNotIn('custom-control-label', page)
        self.assertIn('aria-label="Volume"', page)
        self.assertIn('aria-label="Level"', page)
        page = self.client.get('/tasks/work/edit').text
        self.assertIn('href="#dial-controls"', page)
        self.assertIn('href="#slider-controls"', page)
        self.assertNotIn('custom-control-label', page)
        self.assertIn('aria-label="Configure Volume"', page)
        self.assertIn('aria-label="Configure Level"', page)
        self.post('/numpad/profiles/activate', profile='gmmk')
        self.assertEqual(self.store.read()['keypad'].get('controls'), original.get('controls'))
        self.post('/numpad/profiles/activate', profile='custom')
        self.assertEqual(self.store.read()['keypad']['controls'], sources())

    def test_invalid_axis_and_stale_save_leave_configuration_intact(self):
        before = self.store.read()
        signals = sources(); signals['dial']['code'] = 115
        self.assertEqual(self.save(signals=signals).status_code, 409)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(self.save().status_code, 200)
        saved = self.store.read()
        response = self.client.post('/numpad/profiles/custom/save', data=dict(csrf='test', revision=before['revision'], profile=json.dumps(layout()), controls='{}'))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read(), saved)

    def test_removing_control_disables_input_but_keeps_task_actions(self):
        self.save(); self.post('/numpad/profiles/activate', profile='custom')
        def edit(cfg):
            cfg['scenes']['work']['controls'] = dict(dial=dict(mode='volume', device='oppo', increase='volume_up', decrease='volume_down'))
            cfg['inventory']['oppo']['settings']['commands'].update({name:dict(sequence=[dict(key=name, count=1, gap=.02)]) for name in ('volume_up','volume_down')})
        self.store.update(self.store.read()['revision'], edit)
        value = layout(); value['controls'].pop(0)
        self.assertEqual(self.save(value).status_code, 200)
        cfg = self.store.read()
        self.assertNotIn('dial', cfg['keypad']['controls'])
        self.assertIn('slider', cfg['keypad']['controls'])
        self.assertIn('dial', cfg['scenes']['work']['controls'])

    def test_live_custom_controls_dispatch_existing_task_actions(self):
        self.save(); self.post('/numpad/profiles/activate', profile='custom')
        def edit(cfg):
            cfg['scenes']['work']['controls'] = {name:dict(mode='volume', device='oppo', increase='volume_up', decrease='volume_down') for name in ('dial','slider')}
            cfg['inventory']['oppo']['settings']['commands'].update({name:dict(sequence=[dict(key=name, count=1, gap=.02)]) for name in ('volume_up','volume_down')})
        self.store.update(self.store.read()['revision'], edit)
        atomic_json(Path(self.temp.name) / 'status.json', dict(scene='work', status='commands_sent'))
        with patch.object(Hardware, 'execute') as execute:
            for i, name in enumerate(('dial','slider')):
                with patch('desk_orchestrator.virtual_numpad.time', SimpleNamespace(time=lambda: 100+i)):
                    response = self.post('/api/numpad/press', kind='control', control=name, direction='increase', active_task='work', request_id=str(i)*32)
                    self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(execute.call_count, 2)
            execute.assert_called_with(dict(kind='ir', device='oppo', command='volume_up'))


class AxisLearningTests(unittest.TestCase):
    def test_relative_and_absolute_capture_filter_types_and_allow_zero_axis(self):
        for signal, event_type, value in [('relative',2,-1), ('absolute',3,0)]:
            with self.subTest(signal=signal), tempfile.TemporaryDirectory() as root:
                state = key_learning.request(root, 'owner', 'start', device='/dev/input/event9', signal=signal)
                module = SimpleNamespace(ecodes=SimpleNamespace(KEY={30:'KEY_A'}, bytype={2:{0:'REL_X'},3:{0:'ABS_X'}}))
                broker = key_learning.Broker(root, module, [], {}, lambda:False, lambda:None)
                broker.session = dict(state, device='/dev/input/event9'); broker.started = time.monotonic()
                event = lambda type, value: SimpleNamespace(type=type, code=0, value=value, timestamp=time.monotonic)
                self.assertTrue(broker.consume(event(1,1), '/dev/input/event9'))
                self.assertNotIn('code', key_learning.read(root))
                if signal == 'relative':
                    broker.consume(event(2,0), '/dev/input/event9')
                    self.assertNotIn('code', key_learning.read(root))
                broker.consume(event(event_type,value), '/dev/input/event8')
                self.assertNotIn('code', key_learning.read(root))
                broker.consume(event(event_type,value), '/dev/input/event9')
                result = key_learning.read(root)
                self.assertEqual((result['code'],result['event_type'],result['value']), (0,event_type,value))
                self.assertEqual(result['status'], 'captured')

    def test_unsupported_signal_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(DeskError):
            key_learning.request(root, 'owner', 'start', device='/dev/input/event9', signal='raw')
