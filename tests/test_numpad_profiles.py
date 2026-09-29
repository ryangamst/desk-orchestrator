import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_orchestrator import key_learning, numpad_profiles as profiles
from desk_orchestrator.config_store import ConfigStore
from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.keypad import listen
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
KEY = 'KEY_CUSTOM_' + 'a' * 32
OTHER = 'KEY_CUSTOM_' + 'b' * 32


def layout():
    return dict(name='Macro pad', width=3, height=2, keys=[
        dict(id=KEY, label='Work', x=0, y=0, w=1, h=1, code=30),
        dict(id=OTHER, label='Copy', x=1, y=0, w=2, h=1, code=31)])


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(self.temp.name, SEED)

    def edit(self, callback):
        return self.store.update(self.store.read()['revision'], callback)

    def install(self):
        return self.edit(lambda cfg: profiles.save(cfg, 'macro', layout()))

    def test_switch_preserves_gmmk_controls_lighting_and_mappings(self):
        from desk_orchestrator.gmmk_rgb import DEFAULTS
        self.edit(lambda cfg: cfg['keypad'].update(lighting=copy.deepcopy(DEFAULTS)))
        original = self.install()
        self.edit(lambda cfg: profiles.activate(cfg, 'macro'))
        self.edit(lambda cfg: cfg['keypad'].update(device='/dev/input/event9', grab=False))
        self.edit(lambda cfg: cfg['keypad']['bindings'].update({KEY: 'work'}))
        restored = self.edit(lambda cfg: profiles.activate(cfg, 'gmmk'))
        for field in profiles.CONNECTION_FIELDS:
            self.assertEqual(restored['keypad'].get(field), original['keypad'].get(field))
        self.assertEqual(restored['keypad']['bindings'][KEY], 'work')
        custom = self.edit(lambda cfg: profiles.activate(cfg, 'macro'))
        self.assertEqual(custom['keypad']['device'], '/dev/input/event9')
        self.assertFalse(custom['keypad']['grab'])
        self.assertEqual(set(profiles.labels(custom)), {KEY, OTHER})

    def test_task_save_keeps_inactive_launch_and_command_assignments(self):
        self.install()
        self.edit(lambda cfg: cfg['scenes']['work'].update(key_commands={'KEY_KP9': 'enter'}))
        cfg = self.edit(lambda cfg: profiles.activate(cfg, 'macro'))
        task = copy.deepcopy(cfg['scenes']['work'])
        task['key_commands'] = {OTHER: 'enter'}
        cfg = self.store.save_task(cfg['revision'], 'work', task, KEY)
        self.assertEqual(cfg['keypad']['bindings']['KEY_KP3'], 'work')
        self.assertEqual(cfg['scenes']['work']['key_commands'], {'KEY_KP9': 'enter', OTHER: 'enter'})
        cfg = self.edit(lambda cfg: profiles.activate(cfg, 'gmmk'))
        task = copy.deepcopy(cfg['scenes']['work']); task['key_commands'] = {}
        cfg = self.store.save_task(cfg['revision'], 'work', task, '')
        self.assertEqual(cfg['keypad']['bindings'][KEY], 'work')
        self.assertEqual(cfg['scenes']['work']['key_commands'], {OTHER: 'enter'})

    def test_deletion_requires_explicit_assignment_removal_and_is_atomic(self):
        self.install()
        self.edit(lambda cfg: cfg['keypad']['bindings'].update({KEY: 'work'}))
        before = self.store.read()
        changed = layout(); changed['keys'].pop(0)
        with self.assertRaisesRegex(DeskError, 'assignments'):
            self.edit(lambda cfg: profiles.save(cfg, 'macro', changed))
        self.assertEqual(self.store.read(), before)
        cfg = self.edit(lambda cfg: profiles.save(cfg, 'macro', changed, remove_assignments=True))
        self.assertNotIn(KEY, cfg['keypad']['bindings'])

    def test_invalid_geometry_and_duplicate_signals_rejected(self):
        for change in [lambda p: p['keys'][0].update(x=-1), lambda p: p['keys'][0].update(w=4),
                       lambda p: p['keys'][1].update(x=0), lambda p: p['keys'][1].update(code=30),
                       lambda p: p.update(width=float('nan')), lambda p: p['keys'][0].update(code=True)]:
            value = layout(); change(value)
            with self.assertRaises(DeskError): profiles.validate_profile(value)

    def test_control_input_conflict_is_rejected_without_losing_profile(self):
        self.install()
        cfg = self.edit(lambda cfg: profiles.activate(cfg, 'macro'))
        with self.assertRaisesRegex(DeskError, 'conflicts'):
            self.edit(lambda cfg: cfg['keypad'].update(device='/dev/input/event9', controls={
                'dial': dict(mode='keys', device='/dev/input/event9', code=30, down_code=31)}))
        self.assertEqual(self.store.read(), cfg)

    def test_backup_imports_profiles_but_keeps_local_device(self):
        self.install()
        backup = {'format': 'desk-backup-1', 'config': self.store.read()}
        backup['config']['keypad']['profile_connections'] = {'macro': {'device': '/dev/input/event99'}}
        self.edit(lambda cfg: cfg['keypad'].setdefault('profile_connections', {}).update(macro={'device':'/dev/input/event2'}))
        cfg = self.store.restore(self.store.read()['revision'], backup)
        self.assertEqual(cfg['keypad']['profile_connections']['macro']['device'], '/dev/input/event2')


class ProfileWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, 'custom-numpad-tests')
        self.app = create_app(self.temp.name, SEED); self.app.testing = True
        self.store = self.app.extensions['desk_store']; self.client = self.app.test_client()
        with self.client.session_transaction() as session: session.update(authenticated=True, csrf='test')

    def post(self, url, **data):
        return self.client.post(url, data=dict(csrf='test', revision=self.store.read()['revision'], **data))

    def test_editor_save_switch_and_all_views(self):
        self.assertEqual(self.client.get('/numpad/profiles/new').status_code, 200)
        result = self.post('/numpad/profiles/macro/save', profile=json.dumps(layout()), device='/dev/input/event9', grab='yes')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.post('/numpad/profiles/activate', profile='macro').status_code, 302)
        for path in ('/', '/numpad', '/tasks/work/edit', '/tasks/work/preview', '/api/numpad', '/api/map'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, (path, response.text))
        self.assertIn('data-virtual-key="' + KEY + '"', self.client.get('/').text)
        self.assertNotIn('data-virtual-key="KEY_KP1"', self.client.get('/').text)
        self.assertEqual(set(self.client.get('/api/map').json['keys']), {KEY, OTHER})
        with patch.object(Hardware, 'execute') as execute:
            response = self.post('/api/numpad/press', kind='key', key='KEY_KP1', request_id='c' * 32)
            self.assertEqual(response.status_code, 409)
            execute.assert_not_called()

    def test_learning_lease_owner_expiry_and_revision(self):
        response = self.post('/api/numpad/learn', action='start', device='/dev/input/event9')
        self.assertEqual(response.status_code, 200)
        token = response.json['token']
        self.assertNotIn('owner', response.json)
        self.assertEqual(self.post('/api/numpad/learn', action='start', device='/dev/input/event8').status_code, 409)
        self.assertEqual(self.post('/api/numpad/learn', action='cancel', token='wrong').status_code, 409)
        self.assertEqual(self.post('/api/numpad/learn', action='cancel', token=token).status_code, 200)
        self.assertFalse(key_learning.alive(key_learning.read(self.temp.name)))
        self.assertEqual(self.post('/api/numpad/learn', action='start', device='/etc/passwd').status_code, 409)

    def test_capture_owner_authentication_and_cancellation_after_config_change(self):
        lease = self.post('/api/numpad/learn', action='start', device='/dev/input/event9').json
        with self.assertRaises(DeskError):
            key_learning.request(self.temp.name, 'different-owner', 'poll', token=lease['token'])
        original_revision = self.store.read()['revision']
        self.store.update(original_revision, lambda cfg: cfg['scenes']['work'].update(label='Changed'))
        response = self.client.post('/api/numpad/learn', data=dict(csrf='test', revision=original_revision, action='cancel', token=lease['token']))
        self.assertEqual(response.status_code, 200)
        response = self.client.post('/numpad/profiles/macro/save', data=dict(csrf='wrong', revision=self.store.read()['revision'], profile=json.dumps(layout())))
        self.assertEqual(response.status_code, 400)
        with self.client.session_transaction() as session: session.pop('authenticated')
        self.assertEqual(self.post('/api/numpad/learn', action='start', device='/dev/input/event9').status_code, 302)


class LearningBrokerTests(unittest.IsolatedAsyncioTestCase):
    async def wait_status(self, root, expected):
        for _ in range(80):
            state = key_learning.read(root)
            if state.get('status') == expected: return state
            await asyncio.sleep(.01)
        self.fail(f'Expected {expected}: {state}')

    async def test_candidate_interface_disconnect_cleanup_and_expiry(self):
        with tempfile.TemporaryDirectory() as root:
            queue = asyncio.Queue()
            async def events():
                while True:
                    event = await queue.get()
                    if event is None: raise OSError('Device disconnected')
                    yield event
            device = SimpleNamespace(fd=42, close=Mock(), grab=Mock(), async_read_loop=events)
            module = SimpleNamespace(InputDevice=Mock(return_value=device), ecodes=SimpleNamespace(KEY={30:'KEY_A'}))
            finished = Mock()
            broker = key_learning.Broker(root, module, [], {}, lambda: False, finished)
            lease = key_learning.request(root, 'owner', 'start', device='/dev/input/event9')
            with patch('fcntl.ioctl'):
                task = asyncio.create_task(broker.run())
                try:
                    await self.wait_status(root, 'listening')
                    for value in (0, 2): queue.put_nowait(SimpleNamespace(type=1, code=30, value=value, timestamp=time.monotonic))
                    await asyncio.sleep(.03)
                    self.assertEqual(key_learning.read(root)['status'], 'listening')
                    queue.put_nowait(None)
                    error = await self.wait_status(root, 'error')
                    self.assertIn('disconnected', error['error'])
                    device.close.assert_called_once(); device.grab.assert_called_once(); finished.assert_called_once()
                    with exclusive(Path(root) / 'scene.lock'): pass
                    key_learning.request(root, 'owner', 'start', device='/dev/input/event9')
                    await self.wait_status(root, 'listening')
                    with patch('desk_orchestrator.key_learning.time.time', return_value=time.time() + 100):
                        await asyncio.sleep(.08)
                        self.assertFalse(key_learning.alive(key_learning.read(root)))
                        self.assertIsNone(broker.session)
                    self.assertEqual(device.close.call_count, 2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task

    async def test_busy_capture_never_opens_an_input(self):
        with tempfile.TemporaryDirectory() as root:
            module = Mock()
            key_learning.request(root, 'owner', 'start', device='/dev/input/event9')
            broker = key_learning.Broker(root, module, [], {}, lambda: True, Mock())
            task = asyncio.create_task(broker.run())
            try:
                state = await self.wait_status(root, 'error')
                self.assertIn('busy', state['error'])
                module.InputDevice.assert_not_called()
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task


class LearningListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_switch_reopens_input_without_restart(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root, SEED)
            def setup(cfg):
                profiles.save(cfg, 'macro', layout())
                cfg['keypad']['profile_connections'] = {'macro': {'device':'/dev/input/event9'}}
                cfg['keypad']['bindings'][KEY] = 'work'
                cfg['scenes']['work']['steps'] = [dict(kind='wait', seconds=0)]
            cfg = store.update(store.read()['revision'], setup)
            queue, sent = asyncio.Queue(), asyncio.Queue()
            async def events():
                while True: yield await queue.get()
            devices = []
            def open_input(path):
                device = SimpleNamespace(fd=42, name=path, close=Mock(), grab=Mock(), async_read_loop=events)
                devices.append(device); return device
            module = SimpleNamespace(InputDevice=Mock(side_effect=open_input), ecodes=SimpleNamespace(KEY={30:'KEY_A'}))
            loop = asyncio.get_running_loop()
            def execute(step): loop.call_soon_threadsafe(sent.put_nowait, step)
            with patch('desk_orchestrator.keypad.evdev_module', return_value=module), patch('desk_orchestrator.keypad.fcntl.ioctl'), patch.object(Hardware, 'execute', side_effect=execute):
                task = asyncio.create_task(listen(cfg, Runner(cfg, Hardware(cfg), emit=lambda _: None), live=True, reload_config=store.read))
                try:
                    await asyncio.sleep(.05)
                    store.update(cfg['revision'], lambda current: profiles.activate(current, 'macro'))
                    for _ in range(80):
                        if module.InputDevice.call_count >= 2: break
                        await asyncio.sleep(.025)
                    self.assertEqual(module.InputDevice.call_args.args, ('/dev/input/event9',))
                    devices[0].close.assert_called_once()
                    queue.put_nowait(SimpleNamespace(type=1, code=30, value=1, timestamp=time.monotonic))
                    self.assertEqual(await asyncio.wait_for(sent.get(), 2), dict(kind='wait', seconds=0))
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError): await task

    async def test_capture_uses_grabbed_input_suppresses_actions_and_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConfigStore(root, SEED)
            def setup(cfg):
                profiles.save(cfg, 'macro', layout()); profiles.activate(cfg, 'macro')
                cfg['keypad']['device'] = '/dev/input/event9'
                cfg['keypad']['bindings'][KEY] = 'work'
                cfg['scenes']['work']['steps'] = [dict(kind='wait', seconds=0)]
            cfg = store.update(store.read()['revision'], setup)
            queue, sent = asyncio.Queue(), asyncio.Queue()
            async def events():
                while True: yield await queue.get()
            device = SimpleNamespace(fd=42, name='macro', close=Mock(), grab=Mock(), async_read_loop=events)
            module = SimpleNamespace(InputDevice=Mock(return_value=device), ecodes=SimpleNamespace(KEY={30:'KEY_A'}))
            loop = asyncio.get_running_loop()
            def execute(step): loop.call_soon_threadsafe(sent.put_nowait, step)
            with patch('desk_orchestrator.keypad.evdev_module', return_value=module), patch('desk_orchestrator.keypad.fcntl.ioctl'), patch.object(Hardware, 'execute', side_effect=execute):
                listener = asyncio.create_task(listen(cfg, Runner(cfg, Hardware(cfg), emit=lambda _: None), live=True, reload_config=store.read))
                try:
                    await asyncio.sleep(.05)
                    lease = key_learning.request(root, 'owner', 'start', device=cfg['keypad']['device'])
                    for _ in range(40):
                        if key_learning.read(root).get('status') == 'listening': break
                        await asyncio.sleep(.025)
                    self.assertEqual(key_learning.read(root)['status'], 'listening')
                    with self.assertRaises(DeskError):
                        with exclusive(Path(root) / 'scene.lock'): pass
                    queue.put_nowait(SimpleNamespace(type=1, code=30, value=1, timestamp=time.monotonic))
                    await asyncio.sleep(.08)
                    self.assertEqual(key_learning.read(root)['code'], 30)
                    self.assertTrue(sent.empty())
                    module.InputDevice.assert_called_once()
                    key_learning.request(root, 'owner', 'cancel', token=lease['token'])
                    await asyncio.sleep(.1)
                    queue.put_nowait(SimpleNamespace(type=1, code=30, value=1, timestamp=time.monotonic))
                    self.assertEqual(await asyncio.wait_for(sent.get(), 2), dict(kind='wait', seconds=0))
                finally:
                    listener.cancel()
                    with self.assertRaises(asyncio.CancelledError): await listener
                device.close.assert_called_once()
