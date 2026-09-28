"""Task-local commands: persistence, routing, stale input and USB release safety."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from desk_orchestrator.config_store import ConfigStore
from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive, load_config
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.key_commands import command_report, run_key_command, validate_bindings
from desk_orchestrator.keypad import listen
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
BINDINGS = {'KEY_NUMLOCK': 'play_pause', 'KEY_KPSLASH': 'previous_track', 'KEY_KPASTERISK': 'next_track'}


class KeyCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, 'key-command-tests-only')
        self.app = create_app(self.temp.name, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['desk_store']
        def edit(cfg):
            cfg['scenes']['personal'].update(confirmed=False, steps=[dict(kind='wait', seconds=0)], key_commands=BINDINGS)
            cfg['inventory']['kvm']['settings']['confirmed'] = True
        self.cfg = self.store.update(self.store.read()['revision'], edit)
        self.runner = Runner(self.cfg, Hardware(self.cfg), emit=lambda _: None)
        self.status = Path(self.temp.name) / 'status.json'
        atomic_json(self.status, dict(scene='personal', status='commands_sent'))
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf='test')

    def data(self, **changes):
        return dict(csrf='test', revision=str(self.cfg['revision']), request_id=uuid.uuid4().hex,
                    kind='key', key='KEY_NUMLOCK', active_task='personal') | changes

    def test_save_reload_remove_and_legacy_edit_preserves_bindings(self):
        self.assertEqual(load_config(self.store.path)['scenes']['personal']['key_commands'], BINDINGS)
        response = self.client.get('/tasks/personal/edit')
        self.assertIn(b'Keyboard mapping', response.data)
        self.assertIn(b'command_KEY_NUMLOCK', response.data)
        data = dict(csrf='test', revision=self.cfg['revision'], label='Personal Computing',
                    key='KEY_KP1', steps=json.dumps([dict(kind='wait', seconds=0)]), confirmed='on')
        self.assertEqual(self.client.post('/tasks/personal/edit', data=data).status_code, 302)
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands'], BINDINGS)
        data.update(revision=self.store.read()['revision'], key_commands_present='1',
                    command_KEY_NUMLOCK='pause', command_KEY_KPSLASH='previous_track')
        self.assertEqual(self.client.post('/tasks/personal/edit', data=data).status_code, 302)
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands'],
                         {'KEY_NUMLOCK': 'pause', 'KEY_KPSLASH': 'previous_track'})
        data.update(revision=self.store.read()['revision'], command_KEY_NUMLOCK='', command_KEY_KPSLASH='')
        self.assertEqual(self.client.post('/tasks/personal/edit', data=data).status_code, 302)
        self.assertEqual(self.store.read()['scenes']['personal']['key_commands'], {})

    def test_rejects_reserved_unknown_and_input_control_keys(self):
        for bindings in ({'KEY_KP1': 'play_pause'}, {'KEY_A': 'play_pause'}, {'KEY_NUMLOCK': 'shell'},
                         {'KEY_NUMLOCK': []}, [], None):
            with self.subTest(bindings=bindings), self.assertRaises(DeskError):
                validate_bindings(bindings, self.cfg)
        cfg = copy.deepcopy(self.cfg)
        cfg['keypad']['controls'] = {'dial': dict(device=cfg['keypad']['device'], mode='keys', code=69, down_code=98)}
        with self.assertRaises(DeskError):
            validate_bindings(BINDINGS, cfg)
        cfg['keypad']['controls']['dial'].update(code=115, down_code=114, press_code=69)
        with self.assertRaises(DeskError):
            validate_bindings(BINDINGS, cfg)
        # A later launch-key assignment must not silently shadow existing commands.
        with self.assertRaises(DeskError):
            self.store.save_task(self.cfg['revision'], 'work', self.cfg['scenes']['work'], 'KEY_NUMLOCK')
        self.assertEqual(self.store.read()['revision'], self.cfg['revision'])

    def test_live_virtual_media_command_does_not_rerun_task(self):
        state = self.client.get('/api/numpad').json
        self.assertEqual(state['key_commands']['KEY_NUMLOCK'], 'Play / Pause')
        before = self.status.read_text()
        with patch.object(Hardware, 'check') as check, patch.object(Hardware, 'execute') as execute:
            data = self.data()
            response = self.client.post('/api/numpad/press', data=data)
            self.assertEqual(response.status_code, 200, response.json)
            execute.assert_called_once_with(dict(kind='keyboard', command='play_pause'))
            check.assert_not_called()
            self.assertEqual(self.client.post('/api/numpad/press', data=data).status_code, 409)
        self.assertEqual(self.status.read_text(), before)

    def test_requires_current_successful_task_and_lock_without_hardware_checks(self):
        with patch.object(Hardware, 'execute') as execute:
            for status in ('running', 'failed', 'dry_run'):
                atomic_json(self.status, dict(scene='personal', status=status))
                with self.assertRaises(DeskError):
                    run_key_command(self.runner, 'KEY_NUMLOCK', live=True)
            atomic_json(self.status, dict(scene='work', status='commands_sent'))
            self.assertEqual(self.client.post('/api/numpad/press', data=self.data()).status_code, 409)
            atomic_json(self.status, dict(scene='personal', status='commands_sent'))
            with exclusive(Path(self.temp.name) / 'scene.lock'), self.assertRaises(DeskError):
                run_key_command(self.runner, 'KEY_NUMLOCK', live=True)
            execute.assert_not_called()
            self.cfg['kvm']['confirmed'] = False
            run_key_command(self.runner, 'KEY_NUMLOCK', live=True)
            execute.assert_called_once()

    def test_commands_follow_task_and_dry_run_never_transmits(self):
        with patch.object(Hardware, 'execute') as execute:
            result = run_key_command(self.runner, 'KEY_NUMLOCK', dry_scene='personal')
            self.assertEqual(result['status'], 'dry_run')
            execute.assert_not_called()
            self.cfg['scenes']['work'].update(confirmed=True, key_commands={'KEY_NUMLOCK': 'mute'})
            atomic_json(self.status, dict(scene='work', status='commands_sent'))
            with patch.object(Hardware, 'check'):
                run_key_command(self.runner, 'KEY_NUMLOCK', live=True)
            execute.assert_called_once_with(dict(kind='keyboard', command='mute'))

    def test_usb_reports_paths_and_release_on_send_failure(self):
        self.assertEqual(command_report('play_pause'), b'\xcd\x00')
        self.assertEqual(command_report('previous_track'), b'\xb6\x00')
        self.assertEqual(command_report('next_track'), b'\xb5\x00')
        self.assertEqual(command_report('space'), bytes([0, 0, 0x2c, 0, 0, 0, 0, 0]))
        for command, path in [('play_pause', '/dev/hidg1'), ('space', '/dev/hidg0')]:
            with patch('desk_orchestrator.hardware.os.open', return_value=42) as opened, \
                 patch('desk_orchestrator.hardware.os.close') as close, \
                 patch('desk_orchestrator.hardware.time.sleep'), \
                 patch.object(Hardware, 'write_report', side_effect=[OSError('failed'), None]) as write:
                with self.assertRaises(OSError):
                    self.runner.hardware.send_key_command(command)
                self.assertEqual(opened.call_args.args[0], path)
                self.assertEqual(write.call_args_list, [call(42, command_report(command)), call(42, bytes(len(command_report(command))))])
                close.assert_called_once_with(42)
        with patch('desk_orchestrator.hardware.select.select', return_value=([], [42], [])), \
             patch('desk_orchestrator.hardware.os.write', return_value=2):
            Hardware.write_report(42, b'\xcd\x00')
        with patch('desk_orchestrator.hardware.select.select', return_value=([], [42], [])), \
             patch('desk_orchestrator.hardware.os.write', return_value=1), self.assertRaises(DeskError):
            Hardware.write_report(42, b'\xcd\x00')


class PhysicalCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_physical_press_reload_repeat_release_and_task_priority(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = ConfigStore(root, SEED).read()
            cfg['scenes']['personal'].update(confirmed=True, key_commands=BINDINGS)
            cfg['keypad']['controls'] = {}
            atomic_json(Path(root) / 'status.json', dict(scene='personal', status='commands_sent'))
            runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
            queue, sent = asyncio.Queue(), asyncio.Queue()
            loop = asyncio.get_running_loop()
            async def events():
                while True:
                    yield await queue.get()
            device = SimpleNamespace(fd=42, name='numpad', close=Mock(), grab=Mock(), async_read_loop=events)
            module = SimpleNamespace(InputDevice=lambda _: device, ecodes=SimpleNamespace(KEY={69: 'KEY_NUMLOCK', 79: 'KEY_KP1'}))
            def transmit(step):
                loop.call_soon_threadsafe(sent.put_nowait, step)
            with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
                 patch('desk_orchestrator.keypad.fcntl.ioctl'), patch.object(Hardware, 'check'), \
                 patch.object(Hardware, 'execute', side_effect=transmit), patch.object(Runner, 'run') as run, patch('builtins.print'):
                task = asyncio.create_task(listen(cfg, runner, live=True, reload_config=lambda: cfg))
                try:
                    def press(code, value):
                        queue.put_nowait(SimpleNamespace(type=1, code=code, value=value, timestamp=time.monotonic))
                    press(69, 1)
                    self.assertEqual(await asyncio.wait_for(sent.get(), 2), dict(kind='keyboard', command='play_pause'))
                    await asyncio.sleep(.4)
                    for value in (0, 2):
                        press(69, value)
                    await asyncio.sleep(.05)
                    self.assertTrue(sent.empty())
                    cfg['scenes']['personal']['key_commands'] = {'KEY_NUMLOCK': 'mute'}
                    press(69, 1)
                    self.assertEqual(await asyncio.wait_for(sent.get(), 2), dict(kind='keyboard', command='mute'))
                    await asyncio.sleep(.4)
                    ir = dict(kind='ir', device='oppo', command='volume_up')
                    cfg['scenes']['personal']['key_commands'] = {'KEY_NUMLOCK': ir}
                    press(69, 1)
                    self.assertEqual(await asyncio.wait_for(sent.get(), 2), ir)
                    await asyncio.sleep(.4)
                    for value in (0, 2):
                        press(69, value)
                    await asyncio.sleep(.05)
                    self.assertTrue(sent.empty())
                    press(79, 1)
                    async with asyncio.timeout(2):
                        while not run.called:
                            await asyncio.sleep(.01)
                    run.assert_called_once_with('personal', live=True)
                    self.assertTrue(sent.empty())
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                device.close.assert_called_once()
