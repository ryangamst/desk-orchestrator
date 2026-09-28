import asyncio
import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_orchestrator.controls import (ControlDecoder, active_scene, check_mapping, run_control,
                                         validate_mappings, validate_sources, selected_mapping)
from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.keypad import listen

MAPPING = dict(mode="volume", device="oppo", increase="volume_up", decrease="volume_down")
SOURCE = dict(device="/dev/input/event7", mode="keys", code=115, down_code=114)


def event(kind, code, value):
    return SimpleNamespace(type=kind, code=code, value=value, timestamp=time.monotonic)


def config(root):
    commands = {name: {"verified": True, "sequence": [{"key": key, "count": 1, "gap": .02}]}
                for name, key in (("volume_up", "KEY_VOLUMEUP"), ("volume_down", "KEY_VOLUMEDOWN"))}
    return {"runtime": {"directory": str(root)}, "ir": {"devices": {"oppo": {"remote": "oppo", "commands": commands}}},
            "keypad": {"device": "/dev/input/event5", "bindings": {"KEY_KP1": "personal"}, "controls": {"dial": SOURCE}},
            "scenes": {"personal": {"confirmed": True, "steps": [{"kind": "wait", "seconds": 0}], "controls": {"dial": MAPPING}}}}


class ControlTests(unittest.TestCase):
    def test_key_press_only_and_relative_direction(self):
        decoder = ControlDecoder()
        self.assertEqual(decoder.decode("dial", SOURCE, event(1, 115, 1)), "increase")
        self.assertEqual(decoder.decode("dial", SOURCE, event(1, 114, 1)), "decrease")
        for e in (event(1, 115, 0), event(1, 115, 2), event(1, 116, 1), event(2, 115, 1)):
            self.assertIsNone(decoder.decode("dial", SOURCE, e))
        relative = dict(mode="relative", code=8, invert=True)
        self.assertEqual(decoder.decode("dial", relative, event(2, 8, 1000)), "decrease")
        self.assertIsNone(decoder.decode("dial", relative, event(2, 8, 0)))

    def test_click_is_press_only_and_never_reversed(self):
        decoder = ControlDecoder()
        for mode in ('keys', 'relative'):
            source = dict(SOURCE, mode=mode, press_code=113, invert=True)
            self.assertEqual(decoder.decode('dial', source, event(1, 113, 1)), 'switch')
            for value in (0, 2):
                self.assertIsNone(decoder.decode('dial', source, event(1, 113, value)))
            self.assertIsNone(decoder.decode('dial', source, event(1, 113, 1), click=False))

    def test_click_input_and_source_validation(self):
        validate_sources({'dial': dict(SOURCE, press_code=113)})
        validate_sources({'dial': dict(SOURCE, press_code=115, press_device='/dev/input/event8')})
        for fields in ({'press_code': 115}, {'press_code': True}, {'press_code': -1},
                       {'press_code': 113, 'press_device': '/etc/passwd'}, {'press_device': '/dev/input/event8'}):
            with self.subTest(fields=fields), self.assertRaises(DeskError):
                validate_sources({'dial': dict(SOURCE, **fields)})
        with self.assertRaises(DeskError):
            validate_sources({'dial': dict(SOURCE, press_code=116),
                              'slider': dict(SOURCE, code=116, down_code=117)})
        source = dict(device='amp', increase='up', decrease='down')
        click = dict(action='cycle_volume', sources=[source])
        validate_mappings({'dial': dict(MAPPING, click=click)})
        for name, mapping in [('slider', dict(MAPPING, click=click)),
                              ('dial', dict(MAPPING, mode='commands', click=click)),
                              ('dial', dict(MAPPING, click=dict(action='bad', sources=[source]))),
                              ('dial', dict(MAPPING, click=dict(action='cycle_volume', sources=[]))),
                              ('dial', dict(MAPPING, click=dict(action='cycle_volume', sources=[dict(source, decrease='up')])) )]:
            with self.subTest(name=name, mapping=mapping), self.assertRaises(DeskError):
                validate_mappings({name: mapping})

    def test_click_switches_shared_target_without_ir_and_task_run_resets_it(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = copy.deepcopy(config(root))
            mapping = cfg['scenes']['personal']['controls']['dial']
            devices = ('amp', 'source2', 'source3', 'source4')
            mapping['click'] = dict(action='cycle_volume', sources=[
                dict(device=device, increase='volume_up', decrease='volume_down') for device in devices])
            for device in devices:
                cfg['ir']['devices'][device] = copy.deepcopy(cfg['ir']['devices']['oppo'])
            cfg['scenes']['personal']['controls']['slider'] = dict(MAPPING)
            hardware = Hardware(cfg)
            runner = Runner(cfg, hardware, emit=lambda _: None)
            status_path = Path(root) / 'status.json'
            atomic_json(status_path, dict(scene='personal', status='commands_sent'))
            with patch.object(hardware, 'check'), patch.object(hardware, 'execute') as execute:
                self.assertEqual(selected_mapping(runner, 'personal', 'dial')['device'], 'amp')
                run_control(runner, 'dial', 'increase', live=True)
                execute.assert_called_once_with(dict(kind='ir', device='amp', command='volume_up'))
                for target in (*devices[1:], 'amp', 'source2'):
                    execute.reset_mock()
                    result = run_control(runner, 'dial', 'switch', live=True)
                    self.assertEqual(result['device'], target)
                    execute.assert_not_called()
                    # A fresh process/runner sees the same target.
                    other = Runner(cfg, hardware, emit=lambda _: None)
                    run_control(other, 'dial', 'increase', live=True)
                    execute.assert_called_once_with(dict(kind='ir', device=target, command='volume_up'))
                    self.assertEqual(selected_mapping(other, 'personal', 'slider')['device'], 'oppo')
                with exclusive(Path(root) / 'scene.lock'), self.assertRaises(DeskError):
                    run_control(runner, 'dial', 'switch', live=True)
                with self.assertRaises(DeskError):
                    run_control(runner, 'dial', 'switch', live=True, expected_scene='another')
                # Editing the mapping also discards a saved selection.
                mapping['increase'] = 'new_up'
                self.assertEqual(selected_mapping(runner, 'personal', 'dial')['device'], 'amp')
                mapping['increase'] = 'volume_up'
                with patch.object(runner, 'issues', return_value=[]):
                    runner.run('personal', live=True)
                self.assertEqual(selected_mapping(runner, 'personal', 'dial')['device'], 'amp')
                cfg['scenes']['personal']['confirmed'] = False
                execute.reset_mock()
                run_control(runner, 'dial', 'switch', live=True)
                execute.assert_not_called()

    def test_dry_switch_does_not_change_live_selection_or_send_commands(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = copy.deepcopy(config(root))
            cfg['scenes']['personal']['controls']['dial']['click'] = dict(action='cycle_volume', sources=[
                dict(device=device, increase='up', decrease='down') for device in ('amp', 'other')])
            runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
            with patch.object(runner.hardware, 'execute') as execute:
                run_control(runner, 'dial', 'switch', dry_scene='personal')
                self.assertEqual(selected_mapping(runner, 'personal', 'dial', live=False)['device'], 'other')
                self.assertEqual(selected_mapping(runner, 'personal', 'dial')['device'], 'amp')
                execute.assert_not_called()
                self.assertFalse((Path(root) / 'status.json').exists())

    def test_single_source_is_default_and_clicks_keep_it_selected(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = copy.deepcopy(config(root))
            cfg['scenes']['personal']['controls']['dial']['click'] = dict(action='cycle_volume', sources=[
                dict(device='amp', increase='up', decrease='down')])
            runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
            atomic_json(Path(root) / 'status.json', dict(scene='personal', status='commands_sent'))
            with patch.object(runner.hardware, 'execute') as execute:
                self.assertEqual(selected_mapping(runner, 'personal', 'dial')['device'], 'amp')
                for _ in range(3):
                    result = run_control(runner, 'dial', 'switch', live=True)
                    self.assertEqual(result['device'], 'amp')
                    self.assertEqual(result['source_index'], 1)
                execute.assert_not_called()

    def test_absolute_baseline_jitter_direction_and_reconnect(self):
        decoder = ControlDecoder()
        source = dict(mode="absolute", code=32, deadband=4)
        self.assertIsNone(decoder.decode("slider", source, event(3, 32, 200)))
        self.assertIsNone(decoder.decode("slider", source, event(3, 32, 202)))
        self.assertEqual(decoder.decode("slider", source, event(3, 32, 204)), "increase")
        self.assertEqual(decoder.decode("slider", source, event(3, 32, 0)), "decrease")
        decoder.reset()
        self.assertIsNone(decoder.decode("slider", source, event(3, 32, 255)))

    def test_rejects_overlapping_inputs_and_invalid_mappings(self):
        with self.assertRaises(DeskError):
            validate_sources({"dial": SOURCE, "slider": SOURCE})
        with self.assertRaises(DeskError):
            validate_sources({"dial": dict(SOURCE, device="/etc/passwd")})
        with self.assertRaises(DeskError):
            validate_sources({"dial": dict(SOURCE, down_code=115)})
        with self.assertRaises(DeskError):
            validate_mappings({"dial": dict(MAPPING, decrease="volume_up")})
        validate_sources({"dial": SOURCE, "slider": dict(SOURCE, mode="absolute", code=32)})

    def test_only_successful_task_owns_controls_and_lock_is_shared(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(root)
            hardware = Hardware(cfg)
            runner = Runner(cfg, hardware, emit=lambda _: None)
            with patch.object(hardware, "irsend", return_value="KEY_VOLUMEUP\nKEY_VOLUMEDOWN") as irsend, patch.object(hardware, "execute") as execute, patch("desk_orchestrator.hardware.shutil.which", return_value="irsend"):
                for status in (None, "running", "failed", "dry_run"):
                    if status:
                        atomic_json(Path(root)/"status.json", dict(scene="personal", status=status))
                    run_control(runner, "dial", "increase", live=True)
                execute.assert_not_called()
                atomic_json(Path(root)/"status.json", dict(scene="personal", status="commands_sent"))
                run_control(runner, "dial", "decrease", live=True)
                execute.assert_called_once_with(dict(kind="ir", device="oppo", command="volume_down"))
                with exclusive(Path(root)/"scene.lock"), self.assertRaises(DeskError):
                    run_control(runner, "dial", "increase", live=True)
                cfg["scenes"]["personal"]["confirmed"] = False
                execute.reset_mock()
                run_control(runner, "dial", "increase", live=True)
                execute.assert_called_once_with(dict(kind="ir", device="oppo", command="volume_up"))
                execute.reset_mock()
                cfg["ir"]["devices"]["oppo"]["commands"]["volume_up"]["verified"] = False
                run_control(runner, "dial", "increase", live=True)
                execute.assert_called_once_with(dict(kind="ir", device="oppo", command="volume_up"))

    def test_volume_target_follows_active_task_and_dry_run_is_no_io(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(root)
            cfg["scenes"]["work"] = copy.deepcopy(cfg["scenes"]["personal"])
            cfg["scenes"]["work"]["controls"]["dial"]["device"] = "amp"
            cfg["ir"]["devices"]["amp"] = copy.deepcopy(cfg["ir"]["devices"]["oppo"])
            hardware = Hardware(cfg); output = []
            runner = Runner(cfg, hardware, emit=output.append)
            with patch.object(hardware, "check") as check, patch.object(hardware, "execute") as execute:
                run_control(runner, "dial", "increase", dry_scene="work")
                check.assert_not_called(); execute.assert_not_called()
                self.assertIn('"device": "amp"', output[0])
                self.assertFalse((Path(root) / "status.json").exists())
                self.assertFalse((Path(root) / "scene.lock").exists())
                atomic_json(Path(root)/"status.json", dict(scene="work", status="commands_sent"))
                run_control(runner, "dial", "increase", live=True)
                execute.assert_called_once_with(dict(kind="ir", device="amp", command="volume_up"))

    def test_unverified_and_multi_pulse_commands_block_controls(self):
        cfg = config("/tmp/unused"); hardware = Hardware(cfg)
        macro = cfg["ir"]["devices"]["oppo"]["commands"]["volume_up"]
        macro["verified"] = False
        with self.assertRaises(DeskError): check_mapping(MAPPING, hardware)
        macro["verified"] = True; macro["sequence"][0]["count"] = 2
        with self.assertRaises(DeskError): check_mapping(MAPPING, hardware)
        macro["sequence"][0]["count"] = 1; macro["sequence"][0]["gap"] = 1
        with self.assertRaises(DeskError): check_mapping(MAPPING, hardware)

    def test_partial_scene_failure_disarms_controls(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config(root); hardware = Hardware(cfg); runner = Runner(cfg, hardware, emit=lambda _: None)
            atomic_json(Path(root)/"status.json", dict(scene="personal", status="commands_sent"))
            with patch.object(runner, "issues", return_value=[]), patch.object(hardware, "execute", side_effect=DeskError("IR failure")):
                with self.assertRaises(DeskError): runner.run("personal", live=True)
            self.assertIsNone(active_scene(runner))


class MultipleInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_dispatches_once_on_shared_or_separate_interface(self):
        for click_path in ('/dev/input/event5', '/dev/input/event7', '/dev/input/event8'):
            with self.subTest(click_path=click_path):
                cfg = copy.deepcopy(config('/tmp/unused'))
                cfg['keypad']['controls']['dial'].update(press_code=113, press_device=click_path)
                queues = {path: asyncio.Queue() for path in {cfg['keypad']['device'], SOURCE['device'], click_path}}
                sent = asyncio.Queue()
                loop = asyncio.get_running_loop()
                async def read(path):
                    while True:
                        yield await queues[path].get()
                devices = {path: SimpleNamespace(fd=42, name=path, close=Mock(), grab=Mock(),
                           async_read_loop=lambda path=path: read(path)) for path in queues}
                module = SimpleNamespace(InputDevice=devices.__getitem__, ecodes=SimpleNamespace(KEY={113: 'KEY_KP1'}))
                runner = Mock()
                def adjusted(*args, **kwargs):
                    loop.call_soon_threadsafe(sent.put_nowait, args[1:])
                with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
                     patch('desk_orchestrator.keypad.fcntl.ioctl'), \
                     patch('desk_orchestrator.keypad.run_control', side_effect=adjusted), patch('builtins.print'):
                    task = asyncio.create_task(listen(cfg, runner))
                    try:
                        queues[click_path].put_nowait(event(1, 113, 1))
                        self.assertEqual(await asyncio.wait_for(sent.get(), 2), ('dial', 'switch'))
                        await asyncio.sleep(.12)
                        for value in (0, 2):
                            queues[click_path].put_nowait(event(1, 113, value))
                        queues[SOURCE['device']].put_nowait(event(1, 115, 1))
                        self.assertEqual(await asyncio.wait_for(sent.get(), 2), ('dial', 'increase'))
                        self.assertTrue(sent.empty())
                        runner.run.assert_not_called()
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                for device in devices.values():
                    device.close.assert_called_once()

    async def test_raw_slider_is_separate_from_evdev_and_reconnect_resets_baseline(self):
        loop = asyncio.get_running_loop()
        sent = asyncio.Queue()
        parked = asyncio.Event()
        opened = []
        connections = []
        cfg = config('/tmp/unused')
        cfg['keypad']['controls']['slider'] = dict(device='/dev/input/event5', mode='gmmk_raw', code=32, deadband=4)
        async def keys():
            await parked.wait()
            if False: yield None
        device = SimpleNamespace(fd=42, name='keys', close=Mock(), grab=Mock(), async_read_loop=keys)
        def input_device(path):
            opened.append(path)
            return device
        class Raw:
            name = 'GMMK slider'
            def __init__(self, source):
                self.path = '/dev/hidraw' + str(len(connections) + 1)
                self.queue = asyncio.Queue()
                self.close = Mock()
                connections.append(self)
            async def async_read_loop(self):
                while True:
                    value = await self.queue.get()
                    if value is None:
                        raise OSError('unplugged')
                    yield event(3, 32, value)
        async def wait_for_count(count):
            async with asyncio.timeout(4):
                while len(connections) < count:
                    await asyncio.sleep(.01)
        def adjusted(*args, **kwargs):
            loop.call_soon_threadsafe(sent.put_nowait, args[1:])
        module = SimpleNamespace(InputDevice=input_device, ecodes=SimpleNamespace(KEY={}))
        with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
             patch('desk_orchestrator.keypad.fcntl.ioctl'), \
             patch('desk_orchestrator.keypad.SliderDevice', Raw), \
             patch('desk_orchestrator.keypad.run_control', side_effect=adjusted), patch('builtins.print'):
            task = asyncio.create_task(listen(cfg, Mock()))
            try:
                await wait_for_count(1)
                connections[0].queue.put_nowait(266)  # baseline
                connections[0].queue.put_nowait(268)  # jitter
                connections[0].queue.put_nowait(298)
                self.assertEqual(await asyncio.wait_for(sent.get(), 1), ('slider', 'increase'))
                connections[0].queue.put_nowait(None)
                await wait_for_count(2)
                connections[1].queue.put_nowait(2000)  # new baseline, not increase
                connections[1].queue.put_nowait(1900)
                self.assertEqual(await asyncio.wait_for(sent.get(), 1), ('slider', 'decrease'))
                self.assertTrue(sent.empty())
                self.assertEqual(opened, ['/dev/input/event5', '/dev/input/event7'])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            for connection in connections:
                connection.close.assert_called_once()

    async def test_raw_slider_motion_while_task_runs_is_not_replayed(self):
        started, consumed, parked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def run(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            release.wait(2)
        async def keys():
            yield event(1, 79, 1)
            await parked.wait()
        async def raw():
            await started.wait()
            for value in (266, 298, 500, 1000):
                yield event(3, 32, value)
            consumed.set()
            await parked.wait()
        cfg = config('/tmp/unused')
        cfg['keypad']['controls'] = {'slider': dict(device='/dev/input/event5', mode='gmmk_raw', code=32)}
        device = SimpleNamespace(fd=42, name='keys', close=Mock(), grab=Mock(), async_read_loop=keys)
        raw_device = SimpleNamespace(path='/dev/hidraw8', name='slider', close=Mock(), async_read_loop=raw)
        module = SimpleNamespace(InputDevice=lambda _: device, ecodes=SimpleNamespace(KEY={79: 'KEY_KP1'}))
        runner = Mock(); runner.run.side_effect = run
        with patch('desk_orchestrator.keypad.evdev_module', return_value=module), \
             patch('desk_orchestrator.keypad.fcntl.ioctl'), \
             patch('desk_orchestrator.keypad.SliderDevice', return_value=raw_device), \
             patch('desk_orchestrator.keypad.run_control') as control, patch('builtins.print'):
            task = asyncio.create_task(listen(cfg, runner))
            try:
                await asyncio.wait_for(consumed.wait(), 2)
                release.set()
                await asyncio.sleep(.05)
                control.assert_not_called()
            finally:
                release.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            raw_device.close.assert_called_once()

    async def test_control_motion_during_task_is_discarded_not_queued(self):
        started, consumed, parked = asyncio.Event(), asyncio.Event(), asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        def run(*args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            release.wait(2)
        async def keys():
            yield event(1, 79, 1)
            await parked.wait()
        async def media():
            await started.wait()
            for value in (1, 2, 0, 1):
                yield event(1, 115, value)
            consumed.set()
            await parked.wait()
        devices = {path: SimpleNamespace(fd=42, name=path, close=Mock(), grab=Mock(), async_read_loop=events)
                   for path, events in (("/dev/input/event5", keys), ("/dev/input/event7", media))}
        module = SimpleNamespace(InputDevice=lambda path: devices[path], ecodes=SimpleNamespace(KEY={79: "KEY_KP1"}))
        runner = Mock(); runner.run.side_effect = run
        with patch("desk_orchestrator.keypad.evdev_module", return_value=module), patch("desk_orchestrator.keypad.fcntl.ioctl"), patch("desk_orchestrator.keypad.run_control") as control, patch("builtins.print"):
            task = asyncio.create_task(listen(config("/tmp/unused"), runner))
            try:
                await asyncio.wait_for(consumed.wait(), 2)
            finally:
                release.set()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            runner.run.assert_called_once_with("personal", live=False)
            control.assert_not_called()

    async def test_media_interface_dispatches_controls_and_closes_all_handles(self):
        done = asyncio.Event(); parked = asyncio.Event(); loop = asyncio.get_running_loop()
        async def keys():
            await parked.wait()
            if False: yield None
        async def media():
            yield event(1, 115, 1)
            await parked.wait()
        devices = {path: SimpleNamespace(fd=42, name=path, close=Mock(), grab=Mock(), async_read_loop=events)
                   for path, events in (("/dev/input/event5", keys), ("/dev/input/event7", media))}
        module = SimpleNamespace(InputDevice=lambda path: devices[path], ecodes=SimpleNamespace(KEY={}))
        runner = Mock()
        with patch("desk_orchestrator.keypad.evdev_module", return_value=module), patch("desk_orchestrator.keypad.fcntl.ioctl"), patch("desk_orchestrator.keypad.run_control", side_effect=lambda *a, **k: loop.call_soon_threadsafe(done.set)) as control, patch("builtins.print"):
            task = asyncio.create_task(listen(config("/tmp/unused"), runner))
            try:
                await asyncio.wait_for(done.wait(), 2)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(control.call_args.args[1:], ("dial", "increase"))
            runner.run.assert_not_called()
            for device in devices.values():
                device.close.assert_called_once(); device.grab.assert_called_once()


if __name__ == '__main__': unittest.main()
