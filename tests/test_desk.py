import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_orchestrator.core import DeskError, Runner, atomic_json, exclusive, load_config, validate_step, validate_config
from desk_orchestrator.controls import active_scene
from desk_orchestrator.hardware import Hardware, RELEASE, kvm_reports
from desk_orchestrator.keypad import KeyGate, devices, listen
from desk_orchestrator.smartthings import HTTPFailure, SmartThings, Tokens

ROOT = Path(__file__).resolve().parents[1]


class FakeHardware:
    def __init__(self, *, invalid=None, fail_at=None):
        self.invalid, self.fail_at = invalid, fail_at
        self.sent = []
        self.checked = []

    def check(self, step, *, probe=False):
        self.checked.append((step, probe))
        if step == self.invalid:
            raise DeskError("not ready")

    def execute(self, step):
        if len(self.sent) == self.fail_at:
            raise DeskError("device failed")
        self.sent.append(step)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / "runtime"
        self.steps = [{"kind": "kvm", "port": 3}, {"kind": "wait", "seconds": 1},
                      {"kind": "ir", "device": "amplifier", "command": "power_on"}]
        self.config = {"runtime": {"directory": str(self.runtime)},
                       "scenes": {"work": {"confirmed": True, "steps": self.steps}}}

    def runner(self, hardware):
        return Runner(self.config, hardware, emit=lambda *_: None)

    def test_dry_run_has_no_execution_or_state(self):
        hardware = FakeHardware()
        self.runner(hardware).run("work")
        self.assertFalse(hardware.sent)
        self.assertFalse((self.runtime / "status.json").exists())
        self.assertFalse((self.runtime / "scene.lock").exists())
        self.assertEqual(hardware.checked, [])

    def test_scene_runs_without_hardware_preflight(self):
        hardware = FakeHardware(invalid=self.steps[-1])
        self.runner(hardware).run("work", live=True)
        self.assertEqual(hardware.sent, self.steps)
        self.assertEqual(hardware.checked, [])

    def test_one_shot_preserves_active_state_on_success_failure_and_dry_run(self):
        self.config["scenes"]["power"] = dict(keep_active_task=True, steps=self.steps)
        original = dict(scene="work", status="commands_sent", control_targets={"dial": {"index": 2}})
        for live, fail_at in ((False, None), (True, None), (True, 1)):
            with self.subTest(live=live, fail_at=fail_at):
                atomic_json(self.runtime / "status.json", original)
                hardware = FakeHardware(fail_at=fail_at)
                runner = self.runner(hardware)
                if fail_at is None:
                    result = runner.run("power", live=live)
                    self.assertEqual(result["status"], "commands_sent" if live else "dry_run")
                else:
                    with self.assertRaisesRegex(DeskError, "device failed"):
                        runner.run("power", live=live)
                self.assertEqual(hardware.sent, self.steps[:fail_at] if live else [])
                self.assertEqual(json.loads((self.runtime / "status.json").read_text()), original)
                self.assertEqual(active_scene(self.runner(hardware)), "work")
                if live:
                    result = json.loads((self.runtime / "action-status.json").read_text())
                    self.assertEqual(result["scene"], "power")
                    self.assertEqual(result["status"], "failed" if fail_at else "commands_sent")

    def test_one_shot_never_activates_and_uses_shared_lock(self):
        self.config["scenes"]["power"] = dict(keep_active_task=True, steps=self.steps)
        hardware = FakeHardware()
        runner = self.runner(hardware)
        runner.run("power", live=True)
        self.assertFalse((self.runtime / "status.json").exists())
        self.assertIsNone(active_scene(runner))
        hardware.sent.clear()
        with exclusive(self.runtime / "scene.lock"), self.assertRaises(DeskError):
            runner.run("power", live=True)
        self.assertEqual(hardware.sent, [])
        runner.run("work", live=True)
        self.assertEqual(active_scene(runner), "work")
        self.config["scenes"]["work"]["keep_active_task"] = True
        self.assertIsNone(active_scene(runner))

    def test_one_shot_configuration_rejects_mappings_and_non_boolean_flag(self):
        self.config["version"] = 1
        for fields in ({"keep_active_task": "false"},
                       {"keep_active_task": True, "key_commands": {"KEY_KP0": "enter"}},
                       {"keep_active_task": True, "controls": {"dial": {}}}):
            with self.subTest(fields=fields), self.assertRaises(DeskError):
                validate_config(dict(self.config, scenes={"power": dict(steps=self.steps, **fields)}), self.runtime)

    def test_order_and_unverified_completion(self):
        hardware = FakeHardware()
        state = self.runner(hardware).run("work", live=True)
        self.assertEqual(hardware.sent, self.steps)
        self.assertEqual(state["status"], "commands_sent")
        self.assertFalse(state["physical_state_verified"])
        self.assertEqual(json.loads((self.runtime / "status.json").read_text()), state)

    def test_failure_does_not_enable_amplifier(self):
        hardware = FakeHardware(fail_at=1)
        with self.assertRaises(DeskError):
            self.runner(hardware).run("work", live=True)
        self.assertEqual(hardware.sent, self.steps[:1])
        state = json.loads((self.runtime / "status.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["completed_steps"], 1)
        self.assertEqual(state["active_step"], 2)

    def test_concurrent_process_lock_rejects_new_scene(self):
        hardware = FakeHardware()
        with exclusive(self.runtime / "scene.lock"):
            with self.assertRaisesRegex(DeskError, "Another scene"):
                self.runner(hardware).run("work", live=True)
        self.assertFalse(hardware.sent)

    def test_task_review_and_hardware_checks_are_not_required(self):
        for legacy_confirmation in (False, None):
            with self.subTest(confirmed=legacy_confirmation):
                scene = self.config["scenes"]["work"]
                if legacy_confirmation is None:
                    scene.pop("confirmed", None)
                else:
                    scene["confirmed"] = legacy_confirmation
                hardware = FakeHardware()
                self.runner(hardware).run("work", live=True)
                self.assertEqual(hardware.sent, self.steps)
                self.assertEqual(hardware.checked, [])

    def test_lock_released_after_failure(self):
        with self.assertRaises(DeskError):
            self.runner(FakeHardware(fail_at=0)).run("work", live=True)
        self.runner(FakeHardware()).run("work", live=True)


class ConfigAndInputTests(unittest.TestCase):
    def test_user_corrections_and_all_bindings(self):
        config = load_config(ROOT / "config/desk.example.toml")
        scenes = config["scenes"]
        self.assertIn({"kind": "kvm", "port": 3}, scenes["work"]["steps"])
        self.assertIn({"kind": "monitor", "device": "g8", "input": "hdmi1"}, scenes["work"]["steps"])
        self.assertIn({"kind": "ir", "device": "oppo", "command": "rca"}, scenes["lps"]["steps"])
        self.assertIn({"kind": "volume", "device": "oppo", "db": 6.0}, scenes["lps"]["steps"])
        self.assertIn({"kind": "kvm", "port": 2}, scenes["steam_deck"]["steps"])
        self.assertFalse(any(s["kind"] == "monitor" for s in scenes["steam_deck"]["steps"]))
        self.assertEqual(set(config["keypad"]["bindings"]), {"KEY_KP1", "KEY_KP2", "KEY_KP3", "KEY_KPENTER", "KEY_KPPLUS"})

    def test_invalid_steps_rejected(self):
        invalid = [{"kind": "kvm", "port": True}, {"kind": "kvm", "port": 5},
                   {"kind": "wait", "seconds": float("nan")}, {"kind": "shell", "cmd": "true"},
                   {"kind": "volume", "device": "oppo", "db": "6"},
                   {"kind": "kvm", "port": 1, "typo": 1}]
        for step in invalid:
            with self.subTest(step=step), self.assertRaises(DeskError):
                validate_step(step)

    def test_key_repeats_releases_busy_and_debounce_are_ignored(self):
        gate = KeyGate({"KEY_KP1": "personal", "KEY_KPPLUS": "cds"})
        self.assertEqual(gate.accept("KEY_KP1", 1, busy=False, now=0), "personal")
        for value, busy, now in [(0, False, 1), (2, False, 2), (1, True, 3), (1, False, 0.1)]:
            self.assertIsNone(gate.accept("KEY_KP1", value, busy=busy, now=now))
        self.assertEqual(gate.accept(["KEY_KPPLUS"], 1, busy=False, now=4), "cds")

    def test_kvm_uses_ctrl_release_ctrl_release_top_row_release(self):
        for port in range(1, 5):
            reports = kvm_reports(port)
            self.assertEqual([len(r) for r in reports], [8] * 6)
            self.assertEqual(reports[0][0], 1)
            self.assertEqual(reports[0], reports[2])
            self.assertEqual(reports[4][2], 0x1D + port)
            self.assertEqual([reports[i] for i in [1, 3, 5]], [RELEASE] * 3)


class HardwareTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "config/desk.example.toml")
        self.hardware = Hardware(self.config)

    def test_execution_ignores_ir_verification_and_volume_calibration_flags(self):
        power = self.config["ir"]["devices"]["amplifier"]["commands"]["power_on"]
        power.update(verified=False, discrete=False)
        preset = self.config["ir"]["devices"]["oppo"]["volume_presets"][0]
        preset.update(calibrated=False, starts_from_known_reference=False,
                      sequence=[{"key": "KEY_VOLUMEDOWN", "count": 1}])
        with patch.object(self.hardware, "irsend") as send, patch("desk_orchestrator.hardware.time.sleep"):
            self.hardware.execute({"kind": "ir", "device": "amplifier", "command": "power_on"})
            self.hardware.execute({"kind": "volume", "device": "oppo", "db": preset["db"]})
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0], "SEND_ONCE")

    def test_default_volume_is_blocked(self):
        with self.assertRaisesRegex(DeskError, "exact dB"):
            self.hardware.check({"kind": "volume", "device": "oppo", "db": -32.0})

    def test_volume_requires_reference_and_explicit_opt_in(self):
        oppo = self.config["ir"]["devices"]["oppo"]
        oppo["allow_approximate_volume"] = True
        preset = oppo["volume_presets"][0]
        preset.update(calibrated=True, sequence=[{"key": "KEY_VOLUMEDOWN", "count": 3}])
        step = {"kind": "volume", "device": "oppo", "db": -32.0}
        with self.assertRaisesRegex(DeskError, "reference"):
            self.hardware.check(step)
        preset["starts_from_known_reference"] = True
        self.hardware.check(step)

    def test_power_toggle_rejected(self):
        macro = self.config["ir"]["devices"]["amplifier"]["commands"]["power_on"]
        macro.update(verified=True, discrete=False)
        with self.assertRaisesRegex(DeskError, "toggle"):
            self.hardware.check({"kind": "ir", "device": "amplifier", "command": "power_on"})

    def test_bounded_single_ir_transmissions(self):
        macro = self.config["ir"]["devices"]["oppo"]["commands"]["usb"]
        macro.update(verified=True, sequence=[{"key": "KEY_USB", "count": 2, "gap": 0.1}])
        step = {"kind": "ir", "device": "oppo", "command": "usb"}
        self.hardware.check(step)
        with patch.object(self.hardware, "irsend") as send, patch("desk_orchestrator.hardware.time.sleep"):
            self.hardware.execute(step)
        self.assertEqual(send.call_count, 2)
        send.assert_called_with("SEND_ONCE", "oppo_ha1", "KEY_USB")

    def test_missing_learned_ir_key_blocks_preflight(self):
        self.config["ir"]["devices"]["oppo"]["commands"]["usb"]["verified"] = True
        with patch("desk_orchestrator.hardware.shutil.which", return_value="/usr/bin/irsend"), \
             patch.object(self.hardware, "irsend", return_value="0000 KEY_OTHER"):
            with self.assertRaisesRegex(DeskError, "no oppo_ha1/KEY_USB"):
                self.hardware.check({"kind": "ir", "device": "oppo", "command": "usb"}, probe=True)

    def test_hid_release_attempted_and_fd_closed_after_write_failure(self):
        with patch("desk_orchestrator.hardware.os.open", return_value=42), \
             patch("desk_orchestrator.hardware.os.close") as close, \
             patch.object(self.hardware, "write_report", side_effect=[DeskError("disconnect"), None]) as write:
            with self.assertRaises(DeskError):
                self.hardware.switch_kvm(1)
            write.assert_called_with(42, RELEASE)
            close.assert_called_once_with(42)


class SmartThingsTests(unittest.TestCase):
    def setUp(self):
        self.client = SmartThings({})
        self.monitor = {"device_id": "monitor-1", "component": "main", "capability": "input",
                        "command": "setInput", "attribute": "source", "verify_timeout": 1}

    def test_accepted_command_waits_for_matching_status(self):
        old = {"components": {"main": {"input": {"source": {"value": "DP"}}}}}
        new = {"components": {"main": {"input": {"source": {"value": "HDMI1"}}}}}
        with patch.object(self.client, "call", return_value={"results": [{"status": "ACCEPTED"}]}) as call, \
             patch.object(self.client, "status", side_effect=[old, old, new]) as status, \
             patch("desk_orchestrator.smartthings.time.sleep"):
            self.client.set_input(self.monitor, "HDMI1")
        body = call.call_args.args[2]
        self.assertEqual(body["commands"][0]["arguments"], ["HDMI1"])
        self.assertEqual(status.call_count, 3)
        call.assert_called_once()

    def test_accepted_is_not_success_when_status_never_changes(self):
        with patch.object(self.client, "call", return_value={"results": [{"status": "ACCEPTED"}]}), \
             patch.object(self.client, "status", return_value={}), \
             patch("desk_orchestrator.smartthings.time.monotonic", side_effect=[0, 2]):
            with self.assertRaisesRegex(DeskError, "not confirmed"):
                self.client.set_input(self.monitor, "HDMI1")

    def test_rejected_command_fails_without_input_polling(self):
        with patch.object(self.client, "call", return_value={"results": [{"status": "FAILED"}]}), \
             patch.object(self.client, "status", return_value={}) as status:
            with self.assertRaises(DeskError):
                self.client.set_input(self.monitor, "HDMI1")
            status.assert_called_once_with("monitor-1")

    def test_off_monitor_wakes_before_input_command(self):
        # A non-default component must be used for both status and commands.
        self.monitor["component"] = "display"
        off = {"components": {"display": {"switch": {"switch": {"value": "off"}}}}}
        on = {"components": {"display": {"switch": {"switch": {"value": "on"}}}}}
        new = {"components": {"display": {"input": {"source": {"value": "HDMI1"}}}}}
        for result in ("ACCEPTED", "COMPLETED"):
            with self.subTest(result=result):
                events = []
                states = iter([off, off, on, new])

                def status(device_id):
                    self.assertEqual(device_id, "monitor-1")
                    events.append("status")
                    return next(states)

                def command(method, path, body):
                    self.assertEqual((method, path), ("POST", "/v1/devices/monitor-1/commands"))
                    events.append(body)
                    return {"results": [{"status": result}]}

                with patch.object(self.client, "call", side_effect=command), \
                     patch.object(self.client, "status", side_effect=status), \
                     patch("desk_orchestrator.smartthings.time.sleep"):
                    self.client.set_input(self.monitor, "HDMI1")
                self.assertEqual(events, [
                    "status",
                    {"commands": [{"component": "display", "capability": "switch",
                                   "command": "on", "arguments": []}]},
                    "status", "status",
                    {"commands": [{"component": "display", "capability": "input",
                                   "command": "setInput", "arguments": ["HDMI1"]}]},
                    "status",
                ])

    def test_on_or_unknown_power_does_not_send_wake_command(self):
        for power in ("on", None, "unknown"):
            with self.subTest(power=power):
                current = {"components": {"main": {
                    "switch": {"switch": {"value": power}},
                    "input": {"source": {"value": "HDMI1"}},
                }}}
                with patch.object(self.client, "status", return_value=current), \
                     patch.object(self.client, "call", return_value={"results": [{"status": "COMPLETED"}]}) as call:
                    self.client.set_input(self.monitor, "HDMI1")
                call.assert_called_once()
                self.assertEqual(call.call_args.args[2]["commands"][0]["command"], "setInput")

    def test_wake_failure_prevents_input_command(self):
        off = {"components": {"main": {"switch": {"switch": {"value": "off"}}}}}
        for reply in ({"results": [{"status": "FAILED"}]}, {"results": []}, HTTPFailure(409)):
            with self.subTest(reply=reply), \
                 patch.object(self.client, "status", return_value=off) as status, \
                 patch.object(self.client, "call", side_effect=[reply]) as call:
                with self.assertRaises(DeskError):
                    self.client.set_input(self.monitor, "HDMI1")
                status.assert_called_once()
                call.assert_called_once()
                self.assertEqual(call.call_args.args[2]["commands"][0]["command"], "on")

    def test_wake_timeout_prevents_input_command(self):
        off = {"components": {"main": {"switch": {"switch": {"value": "off"}}}}}
        with patch.object(self.client, "status", return_value=off), \
             patch.object(self.client, "call", return_value={"results": [{"status": "ACCEPTED"}]}) as call, \
             patch("desk_orchestrator.smartthings.time.monotonic", side_effect=[0, 0.5, 2]), \
             patch("desk_orchestrator.smartthings.time.sleep"):
            with self.assertRaisesRegex(DeskError, "power-on was not confirmed"):
                self.client.set_input(self.monitor, "HDMI1")
        call.assert_called_once()
        self.assertEqual(call.call_args.args[2]["commands"][0]["command"], "on")

    def test_power_status_error_prevents_commands(self):
        with patch.object(self.client, "status", side_effect=HTTPFailure(503)), \
             patch.object(self.client, "call") as call:
            with self.assertRaises(DeskError):
                self.client.set_input(self.monitor, "HDMI1")
        call.assert_not_called()

    def test_network_errors_are_not_retried_as_commands(self):
        with patch.object(self.client.tokens, "get", return_value="secret"), \
             patch("desk_orchestrator.smartthings.request", side_effect=HTTPFailure(503)) as request:
            with self.assertRaises(DeskError):
                self.client.call("POST", "/v1/devices/x/commands", {})
            self.assertEqual(request.call_count, 1)

    def test_401_refreshes_once_then_retries(self):
        with patch.object(self.client.tokens, "get", side_effect=["old", "new"]) as tokens, \
             patch("desk_orchestrator.smartthings.request", side_effect=[HTTPFailure(401), {"ok": True}]):
            self.assertEqual(self.client.call("GET", "/v1/devices"), {"ok": True})
            tokens.assert_called_with(rejected="old")

    def test_pagination_does_not_follow_untrusted_host(self):
        result = {"items": [], "_links": {"next": {"href": "https://example.org/steal"}}}
        with patch.object(self.client, "call", return_value=result):
            with self.assertRaisesRegex(DeskError, "host"):
                self.client.devices()

    def test_oauth_rotates_and_persists_both_tokens_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "tokens.json"
            path.write_text(json.dumps({"access_token": "old", "refresh_token": "refresh-old", "expires_at": 0}))
            provider = Tokens({"token_file": str(path)})
            fresh = {"access_token": "new", "refresh_token": "refresh-new", "expires_in": 3600}
            with patch.dict(os.environ, {"SMARTTHINGS_CLIENT_ID": "client", "SMARTTHINGS_CLIENT_SECRET": "secret"}), \
                 patch("desk_orchestrator.smartthings.request", return_value=fresh) as request:
                self.assertEqual(provider.get(), "new")
                self.assertEqual(provider.get(), "new")
                self.assertEqual(request.call_count, 1)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["refresh_token"], "refresh-new")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class InputLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_key_press_uses_latest_saved_task_and_mapping(self):
        finished, parked = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()

        async def events():
            yield SimpleNamespace(type=1, code=79, value=1, timestamp=time.monotonic)
            await parked.wait()

        device = SimpleNamespace(fd=42, name="Numpad", close=Mock(), grab=Mock(), async_read_loop=events)
        module = SimpleNamespace(InputDevice=Mock(return_value=device), ecodes=SimpleNamespace(EV_KEY=1, KEY={79: "KEY_KP1"}))
        original = Mock()
        current = Mock()
        current.run.side_effect = lambda *args, **kwargs: loop.call_soon_threadsafe(finished.set)
        config = {"keypad": {"device": "/fake/numpad", "bindings": {"KEY_KP1": "personal"}}}
        changed = {"keypad": {"device": "/fake/numpad", "bindings": {"KEY_KP1": "work"}}}
        reload_config = Mock(return_value=changed)
        with patch("desk_orchestrator.keypad.evdev_module", return_value=module), \
             patch("desk_orchestrator.keypad.fcntl.ioctl"), \
             patch("desk_orchestrator.keypad.Hardware"), \
             patch("desk_orchestrator.keypad.Runner", return_value=current) as factory, patch("builtins.print"):
            task = asyncio.create_task(listen(config, original, reload_config=reload_config))
            await asyncio.wait_for(finished.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            current.run.assert_called_once_with("work", live=False)
            original.run.assert_not_called()
            self.assertEqual(factory.call_args.args[0], changed)

    async def test_real_evdev_interface_dispatches_and_closes_on_shutdown(self):
        # The fake intentionally has no __enter__ or set_clockid; evdev has neither.
        finished = asyncio.Event()
        parked = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def events():
            yield SimpleNamespace(type=1, code=79, value=1, timestamp=time.monotonic)
            await parked.wait()

        device = SimpleNamespace(fd=42, name="Numpad", close=Mock(), grab=Mock(), async_read_loop=events)
        module = SimpleNamespace(InputDevice=Mock(return_value=device), ecodes=SimpleNamespace(EV_KEY=1, KEY={79: "KEY_KP1"}))
        runner = Mock()
        runner.run.side_effect = lambda *args, **kwargs: loop.call_soon_threadsafe(finished.set)
        config = {"keypad": {"device": "/fake/numpad", "bindings": {"KEY_KP1": "personal"}}}
        with patch("desk_orchestrator.keypad.evdev_module", return_value=module), \
             patch("desk_orchestrator.keypad.fcntl.ioctl") as ioctl, patch("builtins.print"):
            task = asyncio.create_task(listen(config, runner))
            await asyncio.wait_for(finished.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            runner.run.assert_called_once_with("personal", live=False)
            device.close.assert_called_once()
            device.grab.assert_called_once()
            self.assertEqual(ioctl.call_args.args[0], 42)

    async def test_device_discovery_closes_input_handles(self):
        device = SimpleNamespace(name="Numpad", phys="usb-1", close=Mock())
        module = SimpleNamespace(InputDevice=Mock(return_value=device), list_devices=lambda: ["/fake/numpad"])
        with patch("desk_orchestrator.keypad.evdev_module", return_value=module):
            found = devices()
        self.assertEqual(found[0]["name"], "Numpad")
        device.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
