import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_orchestrator.core import DeskError, Runner, atomic_json
from desk_orchestrator.diagnostics import EventLog, target, trace
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.keypad import listen
from test_controls import config, event


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name)
        self.log = EventLog(self.temp.name)

    def test_shared_bounded_history_incremental_cursor_and_gap(self):
        self.assertEqual(self.log.read()['events'], [])
        with patch('desk_orchestrator.diagnostics.MAX_EVENTS', 3):
            for i in range(5):
                EventLog(self.temp.name).emit('input', str(i))
        data = self.log.read(1)
        self.assertTrue(data['gap'])
        self.assertEqual([e['message'] for e in data['events']], ['2', '3', '4'])
        self.assertEqual(self.log.read(data['cursor'])['events'], [])
        self.assertTrue(self.log.read(999)['gap'])

    def test_concurrent_writers_have_unique_ordered_ids(self):
        def write(i):
            EventLog(self.temp.name).emit('input', str(i))
        # Initialize schema before racing independent writer connections.
        self.log.emit('listener', 'start')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(40)))
        ids = [e['id'] for e in self.log.read()['events']]
        self.assertEqual(len(ids), 41)
        self.assertEqual(ids, sorted(set(ids)))

    def test_failed_log_write_does_not_stop_execution(self):
        hw = Mock()
        runner = Runner(self.cfg, hw, emit=lambda _: None)
        with patch.object(runner.log, 'connect', side_effect=sqlite3.OperationalError('disk full')), \
             patch.object(runner, 'issues', return_value=[]), self.assertLogs('desk_orchestrator.diagnostics'):
            state = runner.run('personal', live=True)
        self.assertEqual(state['status'], 'commands_sent')
        hw.execute.assert_called_once()

    def test_command_failure_is_correlated_and_stops_later_commands(self):
        self.cfg['scenes']['personal']['steps'] *= 2
        hw = Mock()
        hw.execute.side_effect = DeskError('USB unavailable')
        runner = Runner(self.cfg, hw, emit=lambda _: None)
        with trace('test-trace'), patch.object(runner, 'issues', return_value=[]), self.assertRaises(DeskError):
            runner.run('personal', live=True)
        entries = self.log.read()['events']
        self.assertTrue(all(e['trace'] == 'test-trace' for e in entries))
        self.assertEqual([e['category'] for e in entries if e['level'] == 'error'], ['command', 'task'])
        self.assertEqual(hw.execute.call_count, 1)
        self.assertFalse(any(e['message'] == 'Task completed' for e in entries))

    def test_optional_preflight_does_not_block_execution_or_report_a_false_rejection(self):
        hw = Mock()
        runner = Runner(self.cfg, hw, emit=lambda _: None)
        with patch.object(runner, 'issues', return_value=['Unverified remote']) as issues:
            runner.run('personal', live=True)
        issues.assert_not_called()
        hw.execute.assert_called_once()
        self.assertEqual(self.log.read()['events'][-1]['message'], 'Task completed')

    def test_dry_run_targets_are_resolved_without_commands_or_credentials(self):
        self.cfg['scenes']['personal']['steps'] = [{'kind': 'ir', 'device': 'oppo', 'command': 'volume_up'}]
        self.cfg['smartthings'] = {'access_token': 'never-log-this'}
        hw = Hardware(self.cfg)
        with patch.object(hw, 'execute') as execute:
            Runner(self.cfg, hw, emit=lambda _: None).run('personal')
        execute.assert_not_called()
        entries = self.log.read()['events']
        planned = next(e for e in entries if e['category'] == 'command')
        self.assertEqual(planned['details']['target']['remote'], 'oppo')
        self.assertFalse(planned['details']['live'])
        self.assertNotIn('never-log-this', json.dumps(entries))
        self.assertFalse((Path(self.temp.name) / 'status.json').exists())

    def test_monitor_and_kvm_destinations_are_explicit(self):
        self.cfg['monitors'] = {'screen': {'device_id': 'st-device', 'component': 'main', 'capability': 'input',
            'command': 'setInput', 'attribute': 'source', 'inputs': {'hdmi1': 'HDMI1'}, 'token': 'secret'}}
        resolved = target(self.cfg, {'kind': 'monitor', 'device': 'screen', 'input': 'hdmi1'})
        self.assertEqual(resolved['device_id'], 'st-device')
        self.assertEqual(resolved['value'], 'HDMI1')
        self.assertNotIn('token', resolved)
        self.cfg['kvm'] = {'device': '/dev/hidg1'}
        self.assertEqual(target(self.cfg, {'kind': 'kvm', 'port': 3})['path'], '/dev/hidg1')


class InputTraceTests(unittest.IsolatedAsyncioTestCase):
    async def test_wheel_keys_route_to_configured_ir_pulses_and_report_wrong_signal_type(self):
        for mode in ("keys", "relative", None):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                cfg = config(root)
                # Learned pulses use their own remote and key names, independent
                # of the Linux KEY_VOLUMEUP / KEY_VOLUMEDOWN input names.
                for direction in ("up", "down"):
                    cfg["ir"]["devices"]["oppo"]["commands"][f"volume_{direction}"]["sequence"] = [
                        dict(remote=f"learned_oppo_{direction}", key="captured", gap=.02)]
                if mode:
                    cfg["keypad"]["controls"]["dial"] = dict(cfg["keypad"]["controls"]["dial"], mode=mode)
                else:
                    cfg["keypad"]["controls"] = {}
                    cfg["keypad"]["device"] = "/dev/input/event7"
                atomic_json(Path(root) / "status.json", dict(scene="personal", status="commands_sent"))
                queues = {path: asyncio.Queue() for path in ("/dev/input/event5", "/dev/input/event7")}
                def input_device(path):
                    async def events():
                        while True:
                            yield await queues[path].get()
                    return SimpleNamespace(fd=42, name=path, close=Mock(), grab=Mock(), async_read_loop=events)
                module = SimpleNamespace(InputDevice=input_device,
                    ecodes=SimpleNamespace(KEY={115: "KEY_VOLUMEUP", 114: "KEY_VOLUMEDOWN"}))
                runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
                with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
                     patch('desk_orchestrator.keypad.fcntl.ioctl'), \
                     patch('desk_orchestrator.hardware.shutil.which', return_value='irsend'), \
                     patch.object(Hardware, 'irsend', return_value='captured') as send, patch('builtins.print'):
                    task = asyncio.create_task(listen(cfg, runner, live=True, reload_config=lambda: cfg))
                    try:
                        for code, direction in ((115, "up"), (114, "down")):
                            await queues["/dev/input/event7"].put(event(1, code, 1))
                            async with asyncio.timeout(3):
                                while len([e for e in runner.log.read()['events'] if e['message'] == 'Input received']) < (1 if code == 115 else 2):
                                    await asyncio.sleep(.01)
                                if mode == "keys":
                                    while len([e for e in runner.log.read()['events'] if e['message'] == 'Command sent; physical response unverified']) < (1 if code == 115 else 2):
                                        await asyncio.sleep(.01)
                            if mode == "keys":
                                # Allow the bounded action and dispatch interval to end.
                                await asyncio.sleep(.12)
                        entries = runner.log.read()['events']
                        received = [e for e in entries if e['message'] == 'Input received']
                        self.assertEqual([e['details']['names'] for e in received], ['KEY_VOLUMEUP', 'KEY_VOLUMEDOWN'])
                        if mode == "keys":
                            transmissions = [c.args for c in send.call_args_list if c.args[0] == 'SEND_ONCE']
                            self.assertEqual(transmissions, [('SEND_ONCE', 'learned_oppo_up', 'captured'),
                                                             ('SEND_ONCE', 'learned_oppo_down', 'captured')])
                            for incoming, direction in zip(received, ('up', 'down')):
                                linked = [e for e in entries if e['trace'] == incoming['trace']]
                                mapped = next(e for e in linked if e['category'] == 'control')
                                self.assertEqual(mapped['details']['step'], dict(kind='ir', device='oppo', command=f'volume_{direction}'))
                                sent = next(e for e in linked if e['category'] == 'transport')
                                self.assertEqual(sent['details']['remote'], f'learned_oppo_{direction}')
                        else:
                            send.assert_not_called()
                            reasons = [e['details']['reason'] for e in entries if e['message'] == 'Input ignored']
                            self.assertEqual(len(reasons), 2)
                            self.assertTrue(all('choose Two keys' in reason for reason in reasons))
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task

    async def test_real_pipeline_links_key_to_resolved_remote_and_transmission(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(root)
            cfg['keypad']['controls'] = {}
            cfg['scenes']['personal']['controls'] = {}
            cfg['scenes']['personal']['steps'] = [{'kind': 'ir', 'device': 'oppo', 'command': 'volume_up'}]
            queue = asyncio.Queue()
            async def events():
                while True:
                    yield await queue.get()
            device = SimpleNamespace(fd=42, name='Test numpad', close=Mock(), grab=Mock(), async_read_loop=events)
            module = SimpleNamespace(InputDevice=lambda _: device, ecodes=SimpleNamespace(KEY={79: 'KEY_KP1', 80: 'KEY_KP2'}))
            hw = Hardware(cfg)
            runner = Runner(cfg, hw, emit=lambda _: None)
            with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
                 patch('desk_orchestrator.keypad.fcntl.ioctl'), patch.object(hw, 'check'), \
                 patch.object(hw, 'irsend') as send, patch('builtins.print'):
                task = asyncio.create_task(listen(cfg, runner, live=True))
                try:
                    await queue.put(event(1, 79, 1))
                    async with asyncio.timeout(3):
                        while not any(e['message'] == 'Task completed' for e in runner.log.read()['events']):
                            await asyncio.sleep(.01)
                    send.assert_called_once_with('SEND_ONCE', 'oppo', 'KEY_VOLUMEUP')
                    entries = runner.log.read()['events']
                    received = next(e for e in entries if e['message'] == 'Input received')
                    linked = [e for e in entries if e['trace'] == received['trace']]
                    self.assertEqual(received['details']['names'], 'KEY_KP1')
                    self.assertEqual({e['category'] for e in linked}, {'input', 'task', 'command', 'transport'})
                    self.assertEqual(next(e for e in linked if e['category'] == 'command')['details']['target']['remote'], 'oppo')
                    await queue.put(event(1, 79, 0))
                    await queue.put(event(1, 79, 2))
                    await queue.put(event(1, 79, 1))
                    await asyncio.sleep(.03)
                    reasons = [e['details'].get('reason') for e in runner.log.read()['events']]
                    self.assertIn('release', reasons)
                    self.assertIn('repeat', reasons)
                    self.assertIn('debounce', reasons)
                    self.assertEqual(send.call_count, 1)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
            self.assertEqual(runner.log.read()['listeners'][0]['state'], 'stopped')
            device.close.assert_called_once()
