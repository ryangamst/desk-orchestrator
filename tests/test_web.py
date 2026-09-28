import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_orchestrator.config_store import ConfigStore
from desk_orchestrator.core import DeskError, exclusive, load_config
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / "config/desk.example.toml"
USB_INPUT = {"name": "Glorious GMMK Numpad", "path": "/dev/input/event5",
             "selection_path": "/dev/input/by-id/usb-Glorious-test-event-kbd",
             "aliases": ["/dev/input/by-id/usb-Glorious-test-event-kbd"],
             "numpad_capable": True, "readable": True, "vendor_id": "320f", "product_id": "5044",
             "serial": "unit-test", "port": "1-2"}
USB_SCAN = {"host": "test-pi", "warnings": [], "candidates": [USB_INPUT], "devices": [
    {"name": "Glorious GMMK Numpad", "manufacturer": "Glorious", "port": "1-2", "vendor_id": "320f",
     "product_id": "5044", "serial": "unit-test", "inputs": [USB_INPUT]},
    {"name": "USB Audio DAC", "manufacturer": "Test", "port": "1-3", "vendor_id": "1234",
     "product_id": "5678", "serial": "", "inputs": []}]}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(self.temp.name, SEED)

    def test_import_preserves_scene_behavior(self):
        original, imported = load_config(SEED), self.store.read()
        self.assertEqual(original["scenes"], imported["scenes"])
        self.assertEqual(original["keypad"]["bindings"], imported["keypad"]["bindings"])
        # Eight seeded devices plus the Raspberry Pi controller map node.
        self.assertEqual(len(imported["inventory"]), 9)
        self.assertEqual(imported["inventory"]["desk_controller"]["type"], "Controller")

    def test_hardware_crud_compiles_into_real_controller(self):
        cfg = self.store.read()
        item = dict(name="Test audio", type="Audio", method="ir", notes="", settings={"remote": "test_remote", "commands": {"on": {"verified": False, "sequence": [{"key": "KEY_ON"}]}}})
        cfg = self.store.save_hardware(cfg["revision"], "test", item, creating=True)
        self.assertEqual(load_config(self.store.path)["ir"]["devices"]["test"]["remote"], "test_remote")
        item["name"] = "Renamed"
        cfg = self.store.save_hardware(cfg["revision"], "test", item)
        self.assertEqual(cfg["inventory"]["test"]["name"], "Renamed")
        self.store.delete_hardware(cfg["revision"], "test")
        self.assertNotIn("test", self.store.read()["ir"]["devices"])

    def test_referenced_hardware_cannot_be_deleted_or_retyped(self):
        cfg = self.store.read()
        with self.assertRaises(DeskError):
            self.store.delete_hardware(cfg["revision"], "oppo")
        item = copy.deepcopy(cfg["inventory"]["oppo"])
        item.update(method="none", settings={})
        with self.assertRaises(DeskError):
            self.store.save_hardware(cfg["revision"], "oppo", item)
        self.assertEqual(self.store.read(), cfg)

    def test_conflicting_key_is_rejected_without_modifying_task(self):
        cfg = self.store.read()
        with self.assertRaisesRegex(DeskError, "already assigned"):
            self.store.save_task(cfg["revision"], "extra", {"label": "Extra", "confirmed": False, "steps": [{"kind": "wait", "seconds": 1}]}, "KEY_KP1", creating=True)
        self.assertEqual(self.store.read(), cfg)

    def test_reorder_and_reassign_then_delete_task(self):
        cfg = self.store.read()
        task = copy.deepcopy(cfg["scenes"]["personal"])
        task["steps"][1], task["steps"][2] = task["steps"][2], task["steps"][1]
        cfg = self.store.save_task(cfg["revision"], "personal", task, "KEY_KP9")
        self.assertNotIn("KEY_KP1", cfg["keypad"]["bindings"])
        self.assertEqual(load_config(self.store.path)["scenes"]["personal"]["steps"], task["steps"])
        cfg = self.store.delete_task(cfg["revision"], "personal")
        self.assertNotIn("KEY_KP9", cfg["keypad"]["bindings"])

    def test_stale_tab_cannot_overwrite_newer_save(self):
        cfg = self.store.read()
        self.store.delete_task(cfg["revision"], "personal")
        with self.assertRaisesRegex(DeskError, "another tab"):
            self.store.delete_task(cfg["revision"], "work")
        self.assertIn("work", self.store.read()["scenes"])

    def test_invalid_hardware_action_is_rejected(self):
        cfg = self.store.read()
        task = {"label": "Bad action", "confirmed": False, "steps": [{"kind": "ir", "device": "g8", "command": "power_on"}]}
        with self.assertRaisesRegex(DeskError, "incompatible"):
            self.store.save_task(cfg["revision"], "bad", task, "", creating=True)

    def test_deleting_every_task_remains_valid(self):
        cfg = self.store.read()
        for name in list(cfg["scenes"]):
            cfg = self.store.delete_task(cfg["revision"], name)
        self.assertEqual(load_config(self.store.path)["scenes"], {})
        self.assertEqual(cfg["keypad"]["bindings"], {})

    def test_backup_restore_keeps_local_executable_and_paths(self):
        cfg = self.store.read()
        incoming = copy.deepcopy(cfg)
        incoming["runtime"]["directory"] = "/tmp/other-runtime"
        incoming["ir"]["executable"] = "/tmp/untrusted-program"
        incoming["keypad"]["device"] = "/dev/input/untrusted"
        incoming["scenes"]["personal"]["label"] = "Restored task"
        self.store.restore(cfg["revision"], {"format": "desk-backup-1", "config": incoming})
        saved = self.store.read()
        self.assertEqual(saved["ir"]["executable"], cfg["ir"]["executable"])
        self.assertEqual(saved["runtime"], cfg["runtime"])
        self.assertEqual(saved["keypad"]["device"], cfg["keypad"]["device"])
        self.assertEqual(saved["scenes"]["personal"]["label"], "Restored task")


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A private local test credential, unrelated to the running UI.
        cls.password = "only-for-tests-1234"

    def setUp(self):
        self.scanner = patch("desk_orchestrator.web.scan_usb", return_value=copy.deepcopy(USB_SCAN)).start()
        self.addCleanup(patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, self.password)
        self.app = create_app(self.temp.name, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["desk_store"]

    def csrf(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            return session["csrf"]

    def login(self):
        response = self.client.post("/login", data={"csrf": self.csrf(), "password": self.password})
        self.assertEqual(response.status_code, 302)

    def form(self, **kwargs):
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        return {"csrf": csrf, "revision": str(self.store.read()["revision"]), **kwargs}

    def test_requires_authentication(self):
        self.assertEqual(self.client.get("/hardware").status_code, 302)
        self.assertEqual(self.client.get("/backup").status_code, 302)
        self.assertEqual(self.client.post("/tasks/work/delete").status_code, 400)

    def test_rgb_save_and_explicit_apply_preserve_existing_controls(self):
        from desk_orchestrator.gmmk_rgb import DEFAULTS
        self.login()
        before = self.store.read()
        fields = {"rgb_" + key: str(value) for key, value in DEFAULTS.items()}
        with patch("desk_orchestrator.web.gmmk_rgb.apply") as apply:
            page = self.client.get("/numpad")
            self.assertIn(b"RGB lighting", page.data)
            self.assertEqual(self.client.post("/numpad", data=self.form(section="lighting", **fields)).status_code, 302)
            saved = self.store.read()
            self.assertEqual(saved["keypad"]["lighting"], DEFAULTS)
            self.assertEqual({k: v for k, v in saved["keypad"].items() if k != "lighting"}, before["keypad"])
            self.assertEqual(saved["scenes"], before["scenes"])
            apply.assert_not_called()
            self.assertEqual(self.client.post("/numpad/lighting/apply", data=self.form()).status_code, 302)
            apply.assert_called_once_with(before["keypad"]["device"], DEFAULTS)
            self.assertEqual(self.store.read(), saved)

    def test_rgb_apply_requires_auth_csrf_saved_settings_and_current_revision(self):
        from desk_orchestrator.gmmk_rgb import DEFAULTS
        with patch("desk_orchestrator.web.gmmk_rgb.apply") as apply:
            self.assertEqual(self.client.post("/numpad/lighting/apply", data={"csrf": self.csrf()}).status_code, 302)
            self.login()
            self.assertEqual(self.client.post("/numpad/lighting/apply").status_code, 400)
            self.assertEqual(self.client.post("/numpad/lighting/apply", data=self.form()).status_code, 409)
            stale = self.form()
            self.store.update(self.store.read()["revision"], lambda cfg: cfg["keypad"].update(lighting=DEFAULTS.copy()))
            self.assertEqual(self.client.post("/numpad/lighting/apply", data=stale).status_code, 409)
            apply.assert_not_called()

    def test_rgb_invalid_save_and_hardware_failure(self):
        from desk_orchestrator.gmmk_rgb import DEFAULTS
        self.login()
        before = self.store.read()
        fields = {"rgb_" + key: str(value) for key, value in DEFAULTS.items()}
        with patch("desk_orchestrator.web.gmmk_rgb.apply", side_effect=DeskError("RGB permission denied")) as apply:
            invalid = dict(fields, rgb_profile="4")
            self.assertEqual(self.client.post("/numpad", data=self.form(section="lighting", **invalid)).status_code, 422)
            self.assertEqual(self.store.read(), before)
            apply.assert_not_called()
            self.client.post("/numpad", data=self.form(section="lighting", **fields))
            response = self.client.post("/numpad/lighting/apply", data=self.form())
            self.assertEqual(response.status_code, 409)
            self.assertIn(b"RGB permission denied", response.data)
            apply.assert_called_once()

    def test_rgb_apply_rejects_busy_configuration(self):
        from desk_orchestrator.gmmk_rgb import DEFAULTS
        self.login()
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["keypad"].update(lighting=DEFAULTS.copy()))
        with patch("desk_orchestrator.web.gmmk_rgb.apply") as apply:
            with exclusive(self.store.directory / "config.lock"):
                self.assertEqual(self.client.post("/numpad/lighting/apply", data=self.form()).status_code, 409)
            apply.assert_not_called()

    def configure_smartthings_test(self):
        self.login()
        def edit(cfg):
            settings = cfg["inventory"]["g8"]["settings"]
            settings["device_id"] = "test-g8"
            settings["inputs"]["displayport"] = "Display Port"
        self.store.update(self.store.read()["revision"], edit)

    def test_smartthings_input_test_uses_saved_mapping_without_verifying_hardware(self):
        self.configure_smartthings_test()
        before = self.store.read()
        with patch("desk_orchestrator.web.Hardware") as hardware:
            client = hardware.return_value.smartthings
            client.call.return_value = {"state": "ONLINE"}
            response = self.client.post("/hardware/g8/smartthings/test", data=self.form(
                input="displayport", value="HDMI2", device_id="other-device"))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ok"])
            client.call.assert_called_once_with("GET", "/v1/devices/test-g8/health")
            client.set_input.assert_called_once_with(before["inventory"]["g8"]["settings"], "Display Port")
        self.assertEqual(self.store.read(), before)
        page = self.client.get("/hardware/g8/edit").text
        self.assertIn("Test saved inputs", page)
        self.assertIn("Test displayport → Display Port", page)
        self.assertNotIn("smartthings-test-form", self.client.get("/hardware/oppo/edit").text)

    def test_smartthings_test_blocks_invalid_stale_and_busy_requests(self):
        self.configure_smartthings_test()
        with patch("desk_orchestrator.web.Hardware") as hardware:
            for device, data in [("g8", self.form(input="missing")),
                                 ("g8", self.form(input="hdmi1")),
                                 ("oppo", self.form(input="displayport")),
                                 ("missing", self.form(input="displayport")),
                                 ("g8", self.form(input="displayport", revision="0"))]:
                with self.subTest(device=device, data=data):
                    response = self.client.post(f"/hardware/{device}/smartthings/test", data=data)
                    self.assertEqual(response.status_code, 409)
                    self.assertIn("No input command was sent", response.json["message"])
            with exclusive(Path(self.temp.name) / "scene.lock"):
                response = self.client.post("/hardware/g8/smartthings/test", data=self.form(input="displayport"))
                self.assertEqual(response.status_code, 409)
                self.assertIn("not queued", response.json["message"])
            hardware.assert_not_called()

    def test_smartthings_test_offline_and_command_failure(self):
        self.configure_smartthings_test()
        with patch("desk_orchestrator.web.Hardware") as hardware:
            client = hardware.return_value.smartthings
            client.call.return_value = {"state": "OFFLINE"}
            response = self.client.post("/hardware/g8/smartthings/test", data=self.form(input="displayport"))
            self.assertEqual(response.status_code, 409)
            self.assertIn("offline", response.json["message"])
            client.set_input.assert_not_called()
            client.call.return_value = {"state": "ONLINE"}
            client.set_input.side_effect = DeskError("SmartThings input was not confirmed before timeout.")
            response = self.client.post("/hardware/g8/smartthings/test", data=self.form(input="displayport"))
            self.assertEqual(response.status_code, 409)
            self.assertIn("may have changed", response.json["message"])
            self.assertFalse(response.json["ok"])

    def test_smartthings_test_requires_login_csrf_and_post(self):
        with patch("desk_orchestrator.web.Hardware") as hardware:
            url = "/hardware/g8/smartthings/test"
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertEqual(self.client.post(url, data={"csrf": self.csrf()}).status_code, 302)
            self.login()
            self.assertEqual(self.client.post(url, data={"input": "displayport"}).status_code, 400)
            hardware.assert_not_called()

    def test_csrf_and_untrusted_host_rejected(self):
        self.login()
        self.assertEqual(self.client.post("/tasks/work/delete", data={"revision": 1}).status_code, 400)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        self.assertIn("work", self.store.read()["scenes"])

    def test_health_requires_session_and_does_not_contact_hardware(self):
        self.assertEqual(self.client.get("/health").status_code, 302)
        self.login()
        self.scanner.reset_mock()
        with patch("desk_orchestrator.web.Hardware") as hardware:
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "online"})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        hardware.assert_not_called()
        self.scanner.assert_not_called()

    def test_all_pages_render_and_static_assets_available(self):
        self.login()
        for path in ["/", "/console", "/numpad", "/hardware", "/hardware/new", "/hardware/oppo/edit", "/hardware/g8/edit", "/tasks", "/tasks/new", "/tasks/work/edit", "/tasks/work/preview", "/settings", "/static/app.css", "/static/studio.css", "/static/app.js", "/static/favicon.svg"]:
            with self.subTest(path=path):
                with self.client.get(path) as response:
                    self.assertEqual(response.status_code, 200)

    def test_overview_only_shows_active_task_for_completed_live_run(self):
        self.login()
        status_path = Path(self.temp.name) / "status.json"
        for state, scene, active in [(None, None, False), ("running", "personal", False),
                                     ("failed", "personal", False), ("dry_run", "personal", False),
                                     ("commands_sent", "deleted_task", False),
                                     ("commands_sent", "personal", True)]:
            with self.subTest(state=state, scene=scene):
                if state:
                    status_path.write_text(json.dumps(dict(scene=scene, status=state, completed_steps=3)))
                response = self.client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertEqual('is-active-task' in response.text, active)
                self.assertEqual(self.client.get('/api/numpad').json['active_task'], 'personal' if active else None)

    def test_console_is_authenticated_incremental_and_read_only(self):
        from desk_orchestrator.diagnostics import EventLog
        self.assertEqual(self.client.get('/console').status_code, 302)
        self.assertEqual(self.client.get('/api/events').status_code, 302)
        self.login()
        log = EventLog(self.store.read()['runtime']['directory'])
        log.emit('input', 'Input received', names='KEY_KP1', live=False)
        with patch('desk_orchestrator.web.Hardware') as hardware:
            response = self.client.get('/api/events')
        hardware.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.json['events'][0]['details']['names'], 'KEY_KP1')
        self.assertEqual(self.client.get('/api/events?after=' + str(response.json['cursor'])).json['events'], [])
        for cursor in ['-1', 'abc', str(2**64)]:
            self.assertEqual(self.client.get('/api/events?after=' + cursor).status_code, 400)

    def test_console_marks_stale_listener_and_reports_unreadable_log(self):
        import sqlite3
        from desk_orchestrator.diagnostics import EventLog
        self.login()
        cfg = self.store.read()
        log = EventLog(cfg['runtime']['directory'])
        with patch('desk_orchestrator.diagnostics.time.time', return_value=1):
            log.listener(cfg['keypad']['device'], 'listening', live=True)
        self.assertTrue(self.client.get('/api/events').json['listeners'][0]['stale'])
        with patch.object(EventLog, 'read', side_effect=sqlite3.DatabaseError('corrupt')):
            response = self.client.get('/api/events')
        self.assertEqual(response.status_code, 503)
        self.assertIn('unavailable', response.json['error'])

    def test_console_tracks_raw_slider_separately_from_keyboard(self):
        from desk_orchestrator.diagnostics import EventLog
        self.login()
        self.client.post('/numpad', data=self.form(
            section='controls', slider_input_mode='gmmk_raw', slider_deadband='4'))
        cfg = self.store.read()
        keyboard = cfg['keypad']['device']
        slider = 'gmmk-slider:' + keyboard
        log = EventLog(cfg['runtime']['directory'])
        log.listener(keyboard, 'listening', live=True)
        log.listener(slider, 'disconnected', live=True, error='Permission denied')
        data = self.client.get('/api/events').json
        self.assertEqual(data['expected_inputs'], [keyboard, slider])
        raw = next(item for item in data['listeners'] if item['path'] == slider)
        self.assertEqual(raw['state'], 'disconnected')
        self.assertEqual(raw['error'], 'Permission denied')
        self.assertFalse(raw['stale'])

    def test_task_crud_through_forms(self):
        self.login()
        steps = [{"kind": "wait", "seconds": .5}]
        response = self.client.post("/tasks/new", data=self.form(id="pause", label="Brief pause", key="KEY_KP9", steps=json.dumps(steps)))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("confirmed", self.store.read()["scenes"]["pause"])
        page = self.client.get("/tasks/pause/edit").get_data(as_text=True)
        self.assertNotIn('name="confirmed"', page)
        preview = self.client.get("/tasks/pause/preview").get_data(as_text=True)
        self.assertIn("Optional connection diagnostics", preview)
        self.assertNotIn("review and confirm", preview)
        response = self.client.post("/tasks/pause/edit", data=self.form(label="New name", key="KEY_KP8", steps=json.dumps(steps)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.read()["scenes"]["pause"]["label"], "New name")
        self.assertEqual(self.client.post("/tasks/pause/delete", data=self.form()).status_code, 302)
        self.assertNotIn("pause", self.store.read()["keypad"]["bindings"].values())

    def test_keep_active_task_editor_clears_and_disables_mappings(self):
        self.login()
        form = self.form(id="audio_power", label="Audio Power", key="KEY_KP9",
                         steps=json.dumps([dict(kind="wait", seconds=0)]), keep_active_task="on",
                         controls_present="1", dial_mode="volume", dial_click_sources="invalid json",
                         key_commands_present="1", command_KEY_KP0="custom", macro_KEY_KP0="invalid json")
        response = self.client.post("/tasks/new", data=form)
        self.assertEqual(response.status_code, 302)
        task = self.store.read()["scenes"]["audio_power"]
        self.assertTrue(task["keep_active_task"])
        self.assertEqual(task["controls"], {})
        self.assertEqual(task["key_commands"], {})
        self.assertEqual(self.store.read()["keypad"]["bindings"]["KEY_KP9"], "audio_power")
        page = self.client.get("/tasks/audio_power/edit").text
        self.assertIn('id="keep-active-task" checked', page)
        self.assertEqual(page.count('data-active-task-mappings hidden disabled'), 2)
        preview = self.client.get("/tasks/audio_power/preview").text
        self.assertIn("Runs without changing the active task", preview)
        self.assertNotIn("Keyboard mapping", preview)
        self.assertNotIn("<h2>Dial and slider</h2>", preview)
        # Switching back enables the editors, and the setting persists through edits.
        response = self.client.post("/tasks/audio_power/edit", data=self.form(
            label="Audio Power", key="KEY_KP9", steps=json.dumps(task["steps"]),
            key_commands_present="1", command_KEY_KP0="custom",
            macro_KEY_KP0=json.dumps(dict(label="Copy", steps=[dict(shortcut="Ctrl+C")]))))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.store.read()["scenes"]["audio_power"]["keep_active_task"])
        response = self.client.post("/tasks/audio_power/edit", data=self.form(
            label="Audio Power", key="KEY_KP9", steps=json.dumps(task["steps"]), keep_active_task="on"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.read()["scenes"]["audio_power"]["key_commands"], {})

    def test_task_appearance_create_edit_and_display(self):
        self.login()
        page = self.client.get("/tasks/new").get_data(as_text=True)
        self.assertIn('<legend>Task color</legend>', page)
        self.assertIn('<legend>Task icon</legend>', page)
        fields = dict(label="Custom task", key="KEY_KP9",
                      steps='[{"kind":"wait","seconds":1}]', color="rose", icon="audio")
        self.assertEqual(self.client.post("/tasks/new", data=self.form(id="custom", **fields)).status_code, 302)
        for color, icon in [("rose", "audio"), ("teal", "gamepad")]:
            fields.update(color=color, icon=icon)
            self.assertEqual(self.client.post("/tasks/custom/edit", data=self.form(**fields)).status_code, 302)
            task = self.store.read()["scenes"]["custom"]
            self.assertEqual((task["color"], task["icon"]), (color, icon))
            page = self.client.get("/tasks/custom/edit").get_data(as_text=True)
            self.assertIn(f'name="color" value="{color}" checked', page)
            self.assertIn(f'name="icon" value="{icon}" checked', page)
            state = self.client.get("/api/numpad?revision=0").json
            mapping = state["overview"]["mappings"]["KEY_KP9"]
            self.assertEqual((mapping["color"], mapping["icon"]), (color, icon))
            for url in ("/", "/tasks"):
                self.assertIn(f'task-card tone-{color} ', self.client.get(url).get_data(as_text=True))
        with patch("desk_orchestrator.web.virtual_numpad.state", return_value={"active_task": "custom"}):
            self.assertEqual(self.client.get("/api/numpad").json["active_color"], "teal")
        # Older forms must not discard saved choices.
        fields.pop("color")
        fields.pop("icon")
        self.assertEqual(self.client.post("/tasks/custom/edit", data=self.form(**fields)).status_code, 302)
        saved = self.store.read()["scenes"]["custom"]
        self.assertEqual((saved["color"], saved["icon"]), ("teal", "gamepad"))
        backup = self.client.get("/backup").json
        self.store.delete_task(self.store.read()["revision"], "custom")
        self.store.restore(self.store.read()["revision"], backup)
        self.assertEqual(self.store.read()["scenes"]["custom"], saved)

    def test_task_appearance_defaults_and_validation(self):
        self.login()
        page = self.client.get("/tasks/work/edit").get_data(as_text=True)
        self.assertIn('name="color" value="blue" checked', page)
        self.assertIn('name="icon" value="briefcase" checked', page)
        before = self.store.read()
        for field, invalid in [("color", "red"), ("icon", "missing")]:
            fields = dict(label="Work", key="KEY_KP3", color="rose", icon="disc",
                          steps='[{"kind":"wait","seconds":1}]')
            fields[field] = invalid
            response = self.client.post("/tasks/work/edit", data=self.form(**fields))
            self.assertEqual(response.status_code, 422)
            self.assertIn(f'Choose a supported task {field}.', response.get_data(as_text=True))
            other = "icon" if field == "color" else "color"
            self.assertIn(f'name="{other}" value="{fields[other]}" checked', response.get_data(as_text=True))
            self.assertEqual(self.store.read(), before)
        # Unrelated validation errors retain both choices for correction.
        fields.update(color="teal", icon="audio", steps='[{"kind":"wait","seconds":-1}]')
        response = self.client.post("/tasks/work/edit", data=self.form(**fields))
        self.assertEqual(response.status_code, 422)
        self.assertIn('name="color" value="teal" checked', response.get_data(as_text=True))
        self.assertIn('name="icon" value="audio" checked', response.get_data(as_text=True))
        self.assertEqual(self.store.read(), before)

    def test_numpad_lists_all_usb_and_saves_stable_path_and_identity(self):
        self.login()
        page = self.client.get("/numpad").get_data(as_text=True)
        self.assertIn("Glorious GMMK Numpad", page)
        self.assertIn("USB Audio DAC", page)
        self.assertIn("test-pi", page)
        before = self.store.read()["keypad"]["bindings"]
        response = self.client.post("/numpad", data=self.form(device_choice=USB_INPUT["selection_path"], grab="on"))
        self.assertEqual(response.status_code, 302)
        saved = load_config(self.store.path)["keypad"]
        self.assertEqual(saved["device"], USB_INPUT["selection_path"])
        self.assertEqual(saved["identity"]["vendor_id"], "320f")
        self.assertEqual(saved["bindings"], before)
        self.assertIn("Glorious GMMK Numpad", self.client.get("/numpad").get_data(as_text=True))

    def test_control_sources_saved_separately_and_overlap_rejected(self):
        self.login()
        original = self.store.read()["keypad"]
        fields = dict(section="controls", dial_input_mode="keys", dial_input_device="/dev/input/event7",
                      dial_input_code="115", dial_down_code="114", slider_input_mode="absolute",
                      slider_input_device="/dev/input/event7", slider_input_code="32", slider_deadband="4")
        self.assertEqual(self.client.post("/numpad", data=self.form(**fields)).status_code, 302)
        saved = self.store.read()
        self.assertEqual(saved["keypad"]["device"], original["device"])
        self.assertEqual(saved["keypad"]["bindings"], original["bindings"])
        self.assertEqual(saved["keypad"]["controls"]["slider"]["code"], 32)
        fields.update(slider_input_mode="keys", slider_input_code="115", slider_down_code="114")
        response = self.client.post("/numpad", data=self.form(**fields))
        self.assertEqual(response.status_code, 422)
        self.assertIn("overlap", response.get_data(as_text=True))
        self.assertEqual(self.store.read(), saved)

    def test_raw_slider_uses_selected_numpad_and_fixed_code(self):
        self.login()
        before = self.store.read()
        fields = dict(section="controls", slider_input_mode="gmmk_raw", slider_deadband="4")
        response = self.client.post("/numpad", data=self.form(**fields))
        self.assertEqual(response.status_code, 302)
        source = self.store.read()["keypad"]["controls"]["slider"]
        self.assertEqual(source["device"], before["keypad"]["device"])
        self.assertEqual(source["code"], 32)
        self.assertEqual(source["mode"], "gmmk_raw")
        page = self.client.get("/numpad").get_data(as_text=True)
        self.assertIn('value="gmmk_raw" selected', page)
        self.assertIn('GMMK Numpad slider (raw HID)', page)
        self.assertEqual(self.client.post("/numpad", data=self.form(
            device_choice="manual", manual_path="/dev/input/event19", grab="on")).status_code, 302)
        self.assertEqual(self.store.read()["keypad"]["controls"]["slider"]["device"], "/dev/input/event19")

    def test_volume_key_code_requires_key_signal_and_old_config_can_be_corrected(self):
        self.login()
        fields = dict(section="controls", dial_input_mode="relative", dial_input_device="/dev/input/event7",
                      dial_input_code="115", dial_down_code="114")
        # Older versions accepted this combination. Keep the app accessible to
        # repair it, while preventing the form from saving it again.
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["keypad"].update(
            controls={"dial": dict(device="/dev/input/event7", mode="relative", code=115, down_code=114)}))
        before = self.store.read()
        self.assertEqual(self.client.get("/numpad").status_code, 200)
        for mode in ("relative", "absolute"):
            fields["dial_input_mode"] = mode
            response = self.client.post("/numpad", data=self.form(**fields))
            self.assertEqual(response.status_code, 422)
            self.assertIn("choose Two keys", response.get_data(as_text=True))
            self.assertEqual(self.store.read(), before)
        fields["dial_input_mode"] = "keys"
        self.assertEqual(self.client.post("/numpad", data=self.form(**fields)).status_code, 302)
        self.assertEqual(self.store.read()["keypad"]["controls"]["dial"]["mode"], "keys")

    def test_ordered_click_sources_save_reorder_disable_and_validate(self):
        self.login()
        commands = {key: dict(verified=True, sequence=[dict(key='KEY_' + key.upper())])
                    for key in ('volume_up', 'volume_down')}
        for index in range(4):
            item = dict(name=f'Volume source {index + 1}', type='Audio', method='ir', notes='',
                        settings=dict(remote=f'source{index}', commands=copy.deepcopy(commands)))
            self.store.save_hardware(self.store.read()['revision'], f'source{index}', item, creating=True)
        sources = [dict(device=f'source{index}', increase='volume_up', decrease='volume_down') for index in range(4)]
        fields = dict(id='cycle_test', label='Cycle test', key='', confirmed='on',
                      steps=json.dumps([dict(kind='wait', seconds=0)]), controls_present='1',
                      dial_mode='volume', dial_click_sources=json.dumps(sources))
        response = self.client.post('/tasks/new', data=self.form(**fields))
        self.assertEqual(response.status_code, 302, response.text)
        self.assertEqual(self.store.read()['scenes']['cycle_test']['controls']['dial']['click']['sources'], sources)
        page = self.client.get('/tasks/cycle_test/edit').text
        self.assertIn('Source 1 is the default volume control', page)
        self.assertNotIn('Default target device', page)
        self.assertNotIn('name="dial_click_action"', page)
        self.assertIn('Add volume source', page)
        self.assertEqual(self.client.get('/tasks/cycle_test/preview').status_code, 200)
        sources.reverse()
        fields['dial_click_sources'] = json.dumps(sources)
        self.assertEqual(self.client.post('/tasks/cycle_test/edit', data=self.form(**fields)).status_code, 302)
        saved = self.store.read()
        self.assertEqual(saved['scenes']['cycle_test']['controls']['dial']['click']['sources'], sources)
        self.assertEqual(saved['scenes']['cycle_test']['controls']['dial']['device'], sources[0]['device'])
        self.assertEqual(self.client.get('/backup').json['config']['scenes']['cycle_test'], saved['scenes']['cycle_test'])
        with self.assertRaises(DeskError):
            self.store.delete_hardware(saved['revision'], 'source3')
        for bad in ([], [dict(sources[0], device='missing')], [dict(sources[0], increase='missing')]):
            fields['dial_click_sources'] = json.dumps(bad)
            self.assertEqual(self.client.post('/tasks/cycle_test/edit', data=self.form(**fields)).status_code, 422)
            self.assertEqual(self.store.read(), saved)
        fields['dial_click_sources'] = json.dumps(sources[:1])
        self.assertEqual(self.client.post('/tasks/cycle_test/edit', data=self.form(**fields)).status_code, 302)
        single = self.store.read()['scenes']['cycle_test']['controls']['dial']
        self.assertEqual(single['device'], sources[0]['device'])
        self.assertEqual(single['click']['sources'], sources[:1])
        fields['dial_mode'] = ''
        self.assertEqual(self.client.post('/tasks/cycle_test/edit', data=self.form(**fields)).status_code, 302)
        self.assertNotIn('dial', self.store.read()['scenes']['cycle_test']['controls'])

    def test_click_signal_roundtrip_with_separate_interface(self):
        self.login()
        fields = dict(section='controls', dial_input_mode='keys', dial_input_device='/dev/input/event7',
                      dial_input_code='115', dial_down_code='114', dial_press_code='113', dial_press_device='/dev/input/event8')
        self.assertEqual(self.client.post('/numpad', data=self.form(**fields)).status_code, 302)
        source = self.store.read()['keypad']['controls']['dial']
        self.assertEqual(source['press_code'], 113)
        self.assertEqual(source['press_device'], '/dev/input/event8')
        self.assertIn('/dev/input/event8', self.client.get('/api/events').json['expected_inputs'])
        fields.update(dial_press_code='', dial_press_device='')
        self.assertEqual(self.client.post('/numpad', data=self.form(**fields)).status_code, 302)
        self.assertNotIn('press_code', self.store.read()['keypad']['controls']['dial'])

    def test_task_control_roundtrip_references_and_backup(self):
        self.login()
        item = copy.deepcopy(self.store.read()["inventory"]["oppo"])
        for name in ("volume_up", "volume_down"):
            item["settings"]["commands"][name] = {"verified": False, "sequence": [{"key": "KEY_" + name.upper()}]}
        self.store.save_hardware(self.store.read()["revision"], "oppo", item)
        fields = dict(id="dial_test", label="Dial test", key="", confirmed="on", steps=json.dumps([{"kind": "wait", "seconds": 1}]),
                      controls_present="1", dial_mode="volume", dial_device="oppo", dial_increase="volume_up", dial_decrease="volume_down",
                      slider_mode="volume", slider_device="oppo", slider_increase="volume_up", slider_decrease="volume_down")
        self.assertEqual(self.client.post("/tasks/new", data=self.form(**fields)).status_code, 302)
        task = self.store.read()["scenes"]["dial_test"]
        self.assertEqual(task["controls"]["dial"]["device"], "oppo")
        self.assertEqual(task["controls"]["slider"]["increase"], "volume_up")
        page = self.client.get("/tasks/dial_test/edit").get_data(as_text=True)
        self.assertIn("Dial and slider", page)
        backup = self.client.get("/backup").json
        self.assertEqual(backup["config"]["scenes"]["dial_test"]["controls"], task["controls"])
        item["settings"]["commands"].pop("volume_up")
        with self.assertRaises(DeskError):
            self.store.save_hardware(self.store.read()["revision"], "oppo", item)
        fields.update(dial_increase="nonexistent")
        self.assertEqual(self.client.post("/tasks/dial_test/edit", data=self.form(**fields)).status_code, 422)
        self.assertEqual(self.store.read()["scenes"]["dial_test"], task)
        fields.update(dial_mode="", slider_mode="")
        self.assertEqual(self.client.post("/tasks/dial_test/edit", data=self.form(**fields)).status_code, 302)
        self.assertEqual(self.store.read()["scenes"]["dial_test"]["controls"], {})

    def test_unplugged_choice_rejected_without_changing_config(self):
        self.login()
        before = self.store.read()
        self.scanner.return_value = {"host": "test-pi", "warnings": [], "devices": [], "candidates": []}
        response = self.client.post("/numpad", data=self.form(device_choice=USB_INPUT["selection_path"]))
        self.assertEqual(response.status_code, 422)
        self.assertIn("no longer available", response.get_data(as_text=True))
        self.assertEqual(self.store.read(), before)

    def test_manual_disconnected_selection_and_path_validation(self):
        self.login()
        path = "/dev/input/by-id/usb-offline-event-kbd"
        response = self.client.post("/numpad", data=self.form(device_choice="manual", manual_path=path))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.read()["keypad"]["device"], path)
        self.assertFalse(self.store.read()["keypad"]["grab"])
        before = self.store.read()
        response = self.client.post("/numpad", data=self.form(device_choice="manual", manual_path="/dev/input/../../etc/passwd"))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.read(), before)

    def test_saving_other_connections_keeps_numpad_selection(self):
        self.login()
        before = self.store.read()["keypad"]
        response = self.client.post("/settings", data=self.form(ir_socket="/run/lirc/lircd", token_env="SMARTTHINGS_TOKEN", token_file=""))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.read()["keypad"], before)

    def test_hardware_form_and_html_escaping(self):
        self.login()
        payload = self.form(id="test", name='<script>alert("x")</script>', type="Computing", method="none", notes="desk computer", settings="{}")
        self.assertEqual(self.client.post("/hardware/new", data=payload).status_code, 302)
        page = self.client.get("/hardware").get_data(as_text=True)
        self.assertNotIn('<script>alert("x")</script>', page)
        # The Hardware page embeds names as JSON, with markup characters escaped.
        self.assertIn("\\u003cscript\\u003e", page)
        self.assertIn("&lt;script&gt;", self.client.get("/hardware/test/edit").get_data(as_text=True))
        self.assertEqual(self.client.post("/hardware/test/delete", data=self.form()).status_code, 302)

    def test_invalid_changes_preserve_existing_config(self):
        self.login()
        before = self.store.read()
        response = self.client.post("/tasks/new", data=self.form(id="bad", label="Bad", key="KEY_KP1", steps='[{"kind":"wait","seconds":-1}]'))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.read(), before)

    def test_task_pages_do_not_run_hardware_checks(self):
        self.login()
        with patch("desk_orchestrator.web.Hardware.check", side_effect=AssertionError("Unexpected hardware check")):
            for path in ("/", "/tasks", "/tasks/work/preview", "/api/numpad?revision=0"):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_preview_does_not_execute_or_contact_hardware(self):
        self.login()
        with patch("desk_orchestrator.web.Hardware.execute") as execute, patch("desk_orchestrator.smartthings.SmartThings.call") as network:
            self.assertEqual(self.client.get("/tasks/work/preview").status_code, 200)
            execute.assert_not_called()
            network.assert_not_called()

    def test_connection_check_shows_smartthings_success_amid_other_failures(self):
        self.configure_smartthings_test()
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["inventory"]["g8"]["settings"].update(confirmed=True))
        monitor = self.store.read()["monitors"]["g8"]
        status = {"components": {monitor["component"]: {monitor["capability"]: {monitor["attribute"]: {"value": "HDMI2"}}}}}
        with patch("desk_orchestrator.smartthings.Tokens.available"), \
             patch("desk_orchestrator.smartthings.SmartThings.status", return_value=status) as read, \
             patch("desk_orchestrator.web.Hardware.execute") as execute, \
             patch("desk_orchestrator.smartthings.SmartThings.set_input") as switch:
            response = self.client.post("/tasks/personal/preview", data=self.form())
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Connection check completed", response.data)
        self.assertIn(b"items need attention", response.data)
        self.assertIn(b"Passed checks", response.data)
        self.assertIn(b"Samsung G8", response.data)
        self.assertIn(b"SmartThings API responded. Current input: HDMI2", response.data)
        self.assertIn(b"No input switch was requested", response.data)
        read.assert_called_once_with("test-g8")
        execute.assert_not_called()
        switch.assert_not_called()

    def test_connection_check_reports_api_failure_and_missing_feedback(self):
        self.configure_smartthings_test()
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["inventory"]["g8"]["settings"].update(confirmed=True))
        for reply in (DeskError("SmartThings network request failed or timed out."), {}):
            with self.subTest(reply=reply), patch("desk_orchestrator.smartthings.Tokens.available"), \
                 patch("desk_orchestrator.smartthings.SmartThings.status") as read:
                if isinstance(reply, Exception):
                    read.side_effect = reply
                else:
                    read.return_value = reply
                response = self.client.post("/tasks/personal/preview", data=self.form())
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b"SmartThings API responded", response.data)
                self.assertIn(b"timed out" if isinstance(reply, Exception) else b"feedback attribute is unavailable", response.data)

    def test_unconfirmed_monitor_does_not_claim_a_successful_connection(self):
        self.configure_smartthings_test()
        with patch("desk_orchestrator.smartthings.SmartThings.status") as read:
            response = self.client.post("/tasks/personal/preview", data=self.form())
        self.assertIn(b"discover and confirm SmartThings input capability", response.data)
        self.assertNotIn(b"SmartThings API responded", response.data)
        read.assert_not_called()

    def test_backup_restore_and_invalid_upload(self):
        self.login()
        backup = self.client.get("/backup")
        self.assertEqual(backup.status_code, 200)
        document = backup.get_json()
        document["config"]["scenes"]["work"]["label"] = "Restored work"
        response = self.client.post("/restore", data=self.form(backup=(io.BytesIO(json.dumps(document).encode()), "desk.json")))
        self.assertEqual(response.status_code, 302)
        before = self.store.read()
        self.assertEqual(before["scenes"]["work"]["label"], "Restored work")
        response = self.client.post("/restore", data=self.form(backup=(io.BytesIO(b"not-json"), "broken.json")))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.read(), before)


if __name__ == "__main__":
    unittest.main()
