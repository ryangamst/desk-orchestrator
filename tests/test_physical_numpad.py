import asyncio
import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_orchestrator.core import Runner
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.keypad import listen
from desk_orchestrator.physical_numpad import KeyFeedback, pressed_keys, state


def config(root):
    return dict(runtime=dict(directory=root),
                keypad=dict(device='/dev/input/event5', bindings={'KEY_KP1': 'work'}),
                scenes=dict(work=dict(steps=[])))


class KeyFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = config(self.temp.name)
        self.feedback = KeyFeedback(self.cfg)
        self.clock = patch('desk_orchestrator.physical_numpad.time', SimpleNamespace(monotonic=lambda: self.now))
        self.now = 100
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def test_taps_holds_repeats_and_multiple_keys(self):
        self.feedback.event(79, 'KEY_KP1', 1)
        self.feedback.event(80, ['KEY_KP2', 'KEY_OTHER'], 1)
        self.feedback.event(79, 'KEY_KP1', 0)
        self.assertEqual(pressed_keys(self.cfg), ['KEY_KP1', 'KEY_KP2'])
        self.now += .4
        self.assertEqual(pressed_keys(self.cfg), ['KEY_KP2'])
        self.feedback.event(80, 'KEY_KP2', 2)
        self.feedback.event(80, 'KEY_KP2', 0)
        self.assertEqual(pressed_keys(self.cfg), [])

    def test_heartbeat_keeps_holds_and_stale_disconnect_restart_clear_them(self):
        self.feedback.event(79, 'KEY_KP1', 1)
        self.now += 2
        self.feedback.publish()
        self.now += 2
        self.assertEqual(pressed_keys(self.cfg), ['KEY_KP1'])
        self.now += 2
        self.assertEqual(pressed_keys(self.cfg), [])
        self.feedback.publish()
        self.feedback.clear()
        self.assertEqual(pressed_keys(self.cfg), [])
        self.feedback.event(79, 'KEY_KP1', 1)
        self.now = 0  # Monotonic clock reset after reboot.
        self.assertEqual(pressed_keys(self.cfg), [])

    def test_custom_layout_resolves_raw_codes_and_device_changes_clear_feedback(self):
        custom = copy.deepcopy(self.cfg)
        custom['keypad'].update(active_profile='macro', profiles={'macro': {'keys': [
            dict(id='KEY_CUSTOM_test', label='Macro', code=30)]}})
        self.feedback.event(30, 'KEY_A', 1)
        self.assertEqual(pressed_keys(custom), ['KEY_CUSTOM_test'])
        self.assertEqual(pressed_keys(self.cfg), [])
        custom['keypad']['device'] = '/dev/input/event9'
        self.assertEqual(pressed_keys(custom), [])

    def test_missing_corrupt_and_unwritable_feedback_is_harmless(self):
        self.assertEqual(pressed_keys(self.cfg), [])
        for content in ('broken', 'null', '{"keys": []}'):
            self.feedback.path.write_text(content)
            self.assertEqual(pressed_keys(self.cfg), [])
        with patch('desk_orchestrator.physical_numpad.atomic_json', side_effect=OSError):
            self.feedback.event(79, 'KEY_KP1', 1)

    def test_dial_keys_relative_detents_click_holds_and_inversion(self):
        source = dict(mode='keys', device='/dev/input/event7', code=115, down_code=114, invert=True)
        self.cfg['keypad']['controls'] = {'dial': source}
        feedback = KeyFeedback(self.cfg)
        def event(kind, code, value):
            feedback.control_event('dial', source, SimpleNamespace(type=kind, code=code, value=value))
        event(1, 115, 1)
        event(1, 115, 2)
        event(1, 115, 0)
        dial = state(self.cfg)['controls']['dial']
        self.assertEqual(dial['steps'], 1, 'physical feedback is independent of command inversion')
        self.assertTrue(dial['active'])
        self.assertEqual(dial['sequence'], 1)
        event(1, 114, 1)
        self.assertEqual(state(self.cfg)['controls']['dial']['steps'], 0)
        source.update(mode='relative', code=8)
        event(2, 8, -3)
        self.assertEqual(state(self.cfg)['controls']['dial']['steps'], -3)
        feedback.click(1)
        self.now += .5
        self.assertTrue(state(self.cfg)['controls']['dial']['pressed'])
        feedback.click(0)
        self.assertFalse(state(self.cfg)['controls']['dial']['pressed'])
        self.assertFalse(state(self.cfg)['controls']['dial']['active'])
        feedback.click(1)
        feedback.click(0)
        self.assertTrue(state(self.cfg)['controls']['dial']['pressed'], 'short clicks remain visible')
        self.now += 4
        self.assertEqual(state(self.cfg)['controls'], {})

    def test_slider_known_range_and_raw_direction_without_inferred_range(self):
        source = dict(mode='absolute', device='/dev/input/event7', code=2, deadband=4)
        self.cfg['keypad']['controls'] = {'slider': source}
        feedback = KeyFeedback(self.cfg)
        feedback.ranges['slider'] = (100, 1100)
        def move(value):
            feedback.control_event('slider', source, SimpleNamespace(type=3, code=source['code'], value=value))
        move(600)
        slider = state(self.cfg)['controls']['slider']
        self.assertEqual(slider['position'], 50)
        self.assertEqual(slider['steps'], 0, 'initial position establishes a baseline')
        move(900)
        self.assertEqual(state(self.cfg)['controls']['slider']['position'], 80)
        self.assertTrue(state(self.cfg)['controls']['slider']['active'])
        move(1200)
        self.assertEqual(state(self.cfg)['controls']['slider']['position'], 100)
        feedback.clear_controls(['slider'])
        source.update(mode='gmmk_raw', code=32)
        move(600)
        move(400)
        slider = state(self.cfg)['controls']['slider']
        self.assertIsNone(slider['position'])
        self.assertEqual(slider['steps'], -1)
        self.assertEqual(slider['direction'], -1)
        move(float('nan'))
        self.assertEqual(state(self.cfg)['controls']['slider'], slider)
        feedback.clear_controls(['slider'])
        move(1500)
        self.assertEqual(state(self.cfg)['controls']['slider']['steps'], 0)
        self.assertNotEqual(state(self.cfg)['controls']['slider']['token'], slider['token'])

    def test_control_config_changes_and_disconnect_discard_old_feedback(self):
        source = dict(mode='relative', device='/dev/input/event7', code=8)
        self.cfg['keypad']['controls'] = {'dial': source}
        feedback = KeyFeedback(self.cfg)
        feedback.control_event('dial', source, SimpleNamespace(type=2, code=8, value=1))
        changed = copy.deepcopy(self.cfg)
        changed['keypad']['controls']['dial']['device'] = '/dev/input/event9'
        self.assertEqual(state(changed)['controls'], {})
        feedback.event(79, 'KEY_KP1', 1)
        feedback.clear_controls(['dial'])
        self.assertEqual(state(self.cfg)['controls'], {})
        self.assertEqual(pressed_keys(self.cfg), ['KEY_KP1'])


class ListenerFeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_feedback_tracks_busy_keys_ignores_other_inputs_and_clears_on_disconnect(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(root)
            cfg['keypad']['controls'] = {
                'dial': dict(device='/dev/input/event7', mode='keys', code=115, down_code=114,
                             press_code=113, press_device='/dev/input/event8'),
                'slider': dict(device='/dev/input/event7', mode='absolute', code=2)}
            queues = {path: asyncio.Queue() for path in ('/dev/input/event5', '/dev/input/event7', '/dev/input/event8')}
            processed = asyncio.Queue()
            def open_input(path):
                async def events():
                    while True:
                        item = await queues[path].get()
                        if isinstance(item, Exception):
                            raise item
                        yield item
                        await processed.put(None)
                return SimpleNamespace(fd=42, name=path, close=Mock(), grab=Mock(), async_read_loop=events,
                                       absinfo=lambda code: SimpleNamespace(min=0, max=1000))
            module = SimpleNamespace(InputDevice=open_input, ecodes=SimpleNamespace(KEY={79:'KEY_KP1', 80:'KEY_KP2'}))
            async def send(code, value, path='/dev/input/event5', kind=1):
                await queues[path].put(SimpleNamespace(type=kind, code=code, value=value, timestamp=time.monotonic))
                await asyncio.wait_for(processed.get(), 2)
            started, finish = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            def run(*args, **kwargs):
                loop.call_soon_threadsafe(started.set)
                finish.wait(5)
            runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
            with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
                 patch('desk_orchestrator.keypad.fcntl.ioctl'), patch.object(runner, 'run', side_effect=run) as action, \
                 patch('builtins.print'):
                task = asyncio.create_task(listen(cfg, runner, live=True))
                try:
                    await send(79, 1)
                    await asyncio.wait_for(started.wait(), 2)
                    await send(80, 1, '/dev/input/event7')
                    self.assertEqual(pressed_keys(cfg), ['KEY_KP1'])
                    await send(80, 1)
                    self.assertEqual(pressed_keys(cfg), ['KEY_KP1', 'KEY_KP2'])
                    await send(115, 1, '/dev/input/event7')
                    await send(113, 1, '/dev/input/event8')
                    await send(2, 500, '/dev/input/event7', kind=3)
                    await send(2, 750, '/dev/input/event7', kind=3)
                    controls = state(cfg)['controls']
                    self.assertEqual(controls['dial']['steps'], 1)
                    self.assertTrue(controls['dial']['pressed'])
                    self.assertEqual(controls['slider']['position'], 75)
                    self.assertTrue(controls['slider']['active'])
                    await send(113, 0, '/dev/input/event8')
                    await queues['/dev/input/event7'].put(OSError('Control disconnected'))
                    async with asyncio.timeout(2):
                        while state(cfg)['controls']:
                            await asyncio.sleep(.01)
                    self.assertEqual(pressed_keys(cfg), ['KEY_KP1', 'KEY_KP2'])
                    await send(79, 0)
                    await send(80, 0)
                    # Both releases must be recorded even while the action is running.
                    import json
                    snapshot = json.loads((Path(root) / 'numpad-keys.json').read_text())
                    self.assertTrue(all(not key['held'] for key in snapshot['keys'].values()))
                    await send(80, 1)
                    await queues['/dev/input/event5'].put(OSError('Disconnected'))
                    async with asyncio.timeout(2):
                        while pressed_keys(cfg):
                            await asyncio.sleep(.01)
                    action.assert_called_once()
                finally:
                    finish.set()
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertEqual(pressed_keys(cfg), [])
