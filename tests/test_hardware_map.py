"""Hardware Map: layout, ports and links live on inventory items in controller.json."""
import copy
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk_orchestrator.config_store import ConfigStore
from desk_orchestrator.core import DeskError, atomic_json, exclusive
from desk_orchestrator.hardware_map import CONTROLLER, NUMPAD, model
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / "config/desk.example.toml"


def g8_map():
    return {"x": 700, "y": 40, "ports": {
        "dp": {"label": "DisplayPort", "signal": "video", "direction": "in", "action": {"kind": "monitor", "input": "displayport"}},
        "hdmi1": {"label": "HDMI 1", "signal": "video", "direction": "in", "action": {"kind": "monitor", "input": "hdmi1"}}},
        "links": []}


def payload(cfg, maps):
    """The edit-mode save: every device's details plus its map."""
    return {name: dict(name=item["name"], type=item["type"], notes=item["notes"], method=item["method"],
                       map=maps.get(name)) for name, item in cfg["inventory"].items()}


def macbook_map():
    return {"x": 40, "y": 40, "ports": {"hdmi": {"label": "HDMI out", "signal": "video", "direction": "out"}},
            "links": [{"port": "hdmi", "to": "g8", "to_port": "hdmi1"}]}


class MapStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(self.temp.name, SEED)

    def maps(self, **changes):
        cfg = self.store.read()
        maps = {name: copy.deepcopy(item.get("map")) for name, item in cfg["inventory"].items()}
        maps.update(g8=g8_map(), macbook=macbook_map())
        maps.update(changes)
        return maps

    def save(self, revision, maps):
        return self.store.save_hardware_map(revision, payload(self.store.read(), maps))

    def test_seed_import_adds_controller_node_with_control_ports(self):
        item = self.store.read()["inventory"][CONTROLLER]
        self.assertEqual((item["type"], item["method"], item["settings"]), ("Controller", "none", {}))
        self.assertEqual(set(item["map"]["ports"]), {"ir_out", "usb_keyboard", "numpad_in", "network"})

    def test_map_is_saved_on_hardware_items_in_the_single_config_file(self):
        cfg = self.save(self.store.read()["revision"], self.maps())
        raw = json.loads(self.store.path.read_text())
        self.assertEqual(raw["inventory"]["macbook"]["map"]["links"], [{"port": "hdmi", "to": "g8", "to_port": "hdmi1"}])
        self.assertEqual(raw["inventory"]["g8"]["map"]["ports"]["hdmi1"]["action"], {"kind": "monitor", "input": "hdmi1"})
        self.assertEqual(cfg["revision"], raw["revision"])
        # The compiled runtime sections are unchanged: the map never alters control settings.
        self.assertNotIn("map", raw["monitors"]["g8"])
        self.assertEqual(sorted(path.name for path in Path(self.temp.name).glob("*.json")), ["controller.json"])

    def test_invalid_maps_are_rejected_without_changes(self):
        before = self.store.read()
        bad_link = macbook_map()
        bad_link["links"][0]["to_port"] = "missing"
        bad_action = g8_map()
        bad_action["ports"]["dp"]["action"] = {"kind": "monitor", "input": "hdmi9"}
        wrong_kind = g8_map()
        wrong_kind["ports"]["dp"]["action"] = {"kind": "ir", "command": "usb"}
        bad_signal = g8_map()
        bad_signal["ports"]["dp"]["signal"] = "laser"
        self_link = macbook_map()
        self_link["links"] = [{"port": "hdmi", "to": "macbook", "to_port": "hdmi"}]
        duplicate = macbook_map()
        duplicate["links"].append(dict(duplicate["links"][0]))
        for maps in (self.maps(macbook=bad_link), self.maps(g8=bad_action), self.maps(g8=wrong_kind),
                     self.maps(g8=bad_signal), self.maps(macbook=self_link), self.maps(macbook=duplicate),
                     self.maps(g8={"x": -5, "y": 0, "ports": {}, "links": []})):
            with self.subTest(maps=list(maps)), self.assertRaises(DeskError):
                self.save(before["revision"], maps)
        new_without_method = payload(before, self.maps())
        new_without_method["unknown_device"] = dict(name="New", type="Audio", notes="")
        for devices in (new_without_method, {"g8": "not a device"}, {"g8": dict(name="G8", type="Monitor")}):
            with self.assertRaises(DeskError):
                self.store.save_hardware_map(before["revision"], devices)
        self.assertEqual(self.store.read(), before)

    def test_stale_revision_cannot_overwrite_newer_map(self):
        revision = self.store.read()["revision"]
        self.save(revision, self.maps())
        with self.assertRaisesRegex(DeskError, "another tab"):
            self.save(revision, self.maps(macbook=None))
        self.assertIn("map", self.store.read()["inventory"]["macbook"])

    def test_hardware_form_saves_keep_the_map_and_delete_drops_incoming_links(self):
        cfg = self.save(self.store.read()["revision"], self.maps())
        item = {key: value for key, value in cfg["inventory"]["g8"].items() if key != "map"}
        item["notes"] = "Desk left"
        cfg = self.store.save_hardware(cfg["revision"], "g8", item)
        self.assertEqual(cfg["inventory"]["g8"]["map"], g8_map())
        # A port action must keep pointing at a saved input.
        item["settings"] = dict(item["settings"], inputs={"displayport": "DP"})
        with self.assertRaisesRegex(DeskError, "unknown input hdmi1"):
            self.store.save_hardware(cfg["revision"], "g8", item)
        # Unreferenced hardware can be deleted; connections into it go with it.
        extra = {"x": 0, "y": 0, "ports": {"in": {"label": "In", "signal": "usb", "direction": "in"}}, "links": []}
        cfg = self.store.save_hardware(cfg["revision"], "hub", dict(name="Hub", type="Peripheral", method="none",
                                                                    notes="", settings={}, map=extra), creating=True)
        maps = self.maps(hub=extra)
        maps["macbook"] = macbook_map()
        maps["macbook"]["ports"]["usb"] = {"label": "USB", "signal": "usb", "direction": "out"}
        maps["macbook"]["links"].append({"port": "usb", "to": "hub", "to_port": "in"})
        cfg = self.save(cfg["revision"], maps)
        cfg = self.store.delete_hardware(cfg["revision"], "hub")
        self.assertEqual(cfg["inventory"]["macbook"]["map"]["links"], [{"port": "hdmi", "to": "g8", "to_port": "hdmi1"}])

    def test_edit_mode_adds_renames_and_deletes_hardware_in_one_save(self):
        cfg = self.store.read()
        devices = payload(cfg, self.maps())
        devices["hub"] = dict(name="USB hub", type="Peripheral", notes="Under desk", method="none",
                              map={"x": 40, "y": 600, "ports": {"up": {"label": "Upstream", "signal": "usb", "direction": "in"}}, "links": []})
        devices["macbook"]["map"]["ports"]["usb"] = {"label": "USB-C", "signal": "usb", "direction": "out"}
        devices["macbook"]["map"]["links"].append({"port": "usb", "to": "hub", "to_port": "up"})
        devices["g9"].update(name="Samsung G9 (left)", notes="KVM DisplayPort")
        del devices["steam_deck"]
        saved = self.store.save_hardware_map(cfg["revision"], devices)
        inventory = saved["inventory"]
        self.assertEqual(inventory["hub"], dict(name="USB hub", type="Peripheral", method="none", notes="Under desk",
                                                settings={}, map=devices["hub"]["map"]))
        self.assertEqual((inventory["g9"]["name"], inventory["g9"]["notes"]), ("Samsung G9 (left)", "KVM DisplayPort"))
        self.assertEqual(inventory["g9"]["settings"], cfg["inventory"]["g9"]["settings"])
        self.assertNotIn("steam_deck", inventory)
        self.assertEqual(saved["revision"], cfg["revision"] + 1)
        # A new IR device starts with empty settings and compiles into the runtime config.
        devices = payload(saved, {name: item.get("map") for name, item in saved["inventory"].items()})
        devices["amp2"] = dict(name="Zone amp", type="Audio", notes="", method="ir", map=None)
        saved = self.store.save_hardware_map(saved["revision"], devices)
        self.assertEqual(saved["ir"]["devices"]["amp2"], {})

    def test_edit_mode_save_rejects_unsafe_changes_without_side_effects(self):
        before = self.store.read()
        cases = []
        in_use = payload(before, self.maps())
        del in_use["oppo"]
        cases.append((in_use, "oppo"))
        controller = payload(before, self.maps())
        del controller[CONTROLLER]
        cases.append((controller, "cannot be deleted"))
        method = payload(before, self.maps())
        method["g8"]["method"] = "ir"
        cases.append((method, "Control settings"))
        second_kvm = payload(before, self.maps())
        second_kvm["kvm2"] = dict(name="KVM 2", type="KVM", notes="", method="usb_hid", map=None)
        cases.append((second_kvm, "one USB KVM"))
        bad_id = payload(before, self.maps())
        bad_id["Bad ID"] = dict(name="Bad", type="Audio", notes="", method="none", map=None)
        cases.append((bad_id, "ID must start"))
        blank = payload(before, self.maps())
        blank["g8"]["name"] = "  "
        cases.append((blank, "needs a name"))
        dangling = payload(before, self.maps())
        del dangling["g8"]
        dangling["monitor_stand"] = dict(name="Stand", type="Other", notes="", method="none", map=None)
        cases.append((dangling, ""))
        for devices, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(DeskError, message):
                self.store.save_hardware_map(before["revision"], devices)
        self.assertEqual(self.store.read(), before)

    def test_controller_is_fixed_and_type_is_reserved(self):
        cfg = self.store.read()
        with self.assertRaisesRegex(DeskError, "cannot be deleted"):
            self.store.delete_hardware(cfg["revision"], CONTROLLER)
        with self.assertRaisesRegex(DeskError, "reserved"):
            self.store.save_hardware(cfg["revision"], "fake", dict(name="Fake", type="Controller", method="none",
                                                                    notes="", settings={}), creating=True)
        item = copy.deepcopy(cfg["inventory"][CONTROLLER])
        item.update(type="Other")
        with self.assertRaisesRegex(DeskError, "reserved"):
            self.store.save_hardware(cfg["revision"], CONTROLLER, item)

    def test_existing_configuration_is_upgraded_once_with_controller(self):
        def edit(cfg):
            del cfg["inventory"][CONTROLLER]
        # Simulate a controller.json saved before the Hardware Map existed.
        cfg = self.store.read()
        edit(cfg)
        atomic_json(self.store.path, cfg)
        upgraded = ConfigStore(self.temp.name, SEED).read()
        self.assertIn(CONTROLLER, upgraded["inventory"])
        self.assertEqual(upgraded["revision"], cfg["revision"] + 1)
        self.assertEqual(ConfigStore(self.temp.name, SEED).read()["revision"], upgraded["revision"])

    def test_restore_keeps_controller_when_backup_predates_map(self):
        cfg = self.save(self.store.read()["revision"], self.maps())
        backup = copy.deepcopy(cfg)
        del backup["inventory"][CONTROLLER]
        restored = self.store.restore(cfg["revision"], {"format": "desk-backup-1", "config": backup})
        self.assertEqual(restored["inventory"][CONTROLLER], cfg["inventory"][CONTROLLER])
        self.assertEqual(restored["inventory"]["macbook"]["map"], macbook_map())

    def test_model_traces_task_routes_to_ports(self):
        cfg = self.save(self.store.read()["revision"], self.maps())
        data = model(cfg)
        self.assertEqual(data["tasks"]["work"]["touches"]["g8"], ["hdmi1"])
        self.assertEqual(data["tasks"]["personal"]["touches"]["g8"], ["dp"])
        self.assertIn("kvm", data["tasks"]["work"]["touches"])
        self.assertEqual(data["tasks"]["work"]["keys"], [{"key": "KEY_KP3", "label": "3"}])
        commands = {c["id"]: c["label"] for c in data["devices"]["g8"]["commands"]}
        self.assertEqual(commands["input:hdmi1"], "Switch to HDMI 1")
        self.assertIn("kvm:4", {c["id"] for c in data["devices"]["kvm"]["commands"]})
        self.assertIn("ir:power_on", {c["id"] for c in data["devices"]["oppo"]["commands"]})


class MapWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        set_password(self.temp.name, "hardware-map-tests-only")
        self.app = create_app(self.temp.name, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["desk_store"]
        self.execute = patch("desk_orchestrator.hardware.Hardware.execute").start()
        self.addCleanup(patch.stopall)
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf="test-csrf")

    def post(self, url, **fields):
        data = dict(csrf="test-csrf", revision=str(self.store.read()["revision"]), request_id=uuid.uuid4().hex)
        data.update(fields)
        return self.client.post(url, data=data)

    def test_map_page_and_model_render(self):
        page = self.client.get("/hardware")
        self.assertEqual(page.status_code, 200)
        moved = self.client.get("/map")
        self.assertEqual((moved.status_code, moved.headers["Location"]), (301, "/hardware"))
        text = page.get_data(as_text=True)
        self.assertIn('id="map-model"', text)
        self.assertIn("hardware_map.js", text)
        self.assertIn("hardware_map.css", text)
        self.assertIn('id="map-edit"', text)
        self.assertIn('id="map-exit"', text)
        self.assertNotIn("data-map-mode", text)
        self.assertEqual(text.count('href="/hardware"'), 1)  # one sidebar entry, no separate Map page
        selected = self.client.get("/hardware?device=g8").get_data(as_text=True)
        self.assertIn('data-select="g8"', selected)
        self.assertIn('data-select=""', self.client.get("/hardware?device=nope").get_data(as_text=True))
        data = self.client.get("/api/map").json
        self.assertIn(CONTROLLER, data["devices"])
        self.assertEqual(data["revision"], self.store.read()["revision"])
        script = self.client.get("/static/hardware_map.js")
        self.assertEqual(script.status_code, 200)
        script.close()

    def test_save_via_web_is_revision_checked(self):
        maps = {name: item.get("map") for name, item in self.store.read()["inventory"].items()}
        maps.update(g8=g8_map(), macbook=macbook_map())
        devices = payload(self.store.read(), maps)
        devices["desk_lamp"] = dict(name="Desk lamp", type="Other", notes="", method="none", map=None)
        response = self.post("/map/save", devices=json.dumps(devices))
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["revision"], self.store.read()["revision"])
        self.assertEqual(self.store.read()["inventory"]["g8"]["map"], g8_map())
        self.assertIn("desk_lamp", response.json["model"]["devices"])
        stale = self.post("/map/save", devices=json.dumps(devices), revision="1")
        self.assertEqual(stale.status_code, 409)
        self.assertIn("Nothing was saved", stale.json["error"])
        malformed = self.post("/map/save", devices="{")
        self.assertEqual(malformed.status_code, 409)
        self.assertEqual(self.client.post("/map/save", data=dict(devices="{}")).status_code, 400)

    def test_run_task_from_map_uses_live_runner(self):
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["scenes"]["personal"].update(
            steps=[{"kind": "wait", "seconds": 0}]))
        response = self.post("/api/map/run", kind="task", task="personal")
        self.assertEqual(response.status_code, 200, response.json)
        self.execute.assert_called_once_with({"kind": "wait", "seconds": 0})
        self.assertEqual(self.client.get("/api/numpad").json["active_task"], "personal")

    def test_device_command_sends_one_step_without_changing_active_task(self):
        atomic_json(Path(self.temp.name) / "status.json", dict(scene="work", status="commands_sent"))
        response = self.post("/api/map/run", kind="command", device="kvm", command="kvm:2")
        self.assertEqual(response.status_code, 200, response.json)
        self.execute.assert_called_once_with({"kind": "kvm", "port": 2})
        self.assertEqual(self.client.get("/api/numpad").json["active_task"], "work")
        self.execute.reset_mock()
        with patch("desk_orchestrator.virtual_numpad.time", SimpleNamespace(time=lambda: 10**10)):
            response = self.post("/api/map/run", kind="command", device="oppo", command="volume:-32.0")
        self.assertEqual(response.status_code, 200, response.json)
        self.execute.assert_called_once_with({"kind": "volume", "device": "oppo", "db": -32.0})

    def test_invalid_busy_and_repeated_inputs_are_rejected_not_queued(self):
        for fields in (dict(kind="command", device="kvm", command="kvm:9"), dict(kind="command", device="nope", command="x"),
                       dict(kind="task", task="missing"), dict(kind="other"), dict(kind="task", task="work", request_id="bad"),
                       dict(kind="task", task="work", revision="0")):
            with self.subTest(fields=fields):
                self.assertEqual(self.post("/api/map/run", **fields).status_code, 409)
        with exclusive(Path(self.temp.name) / "scene.lock"):
            busy = self.post("/api/map/run", kind="command", device="kvm", command="kvm:1")
        self.assertEqual(busy.status_code, 409)
        request_id = uuid.uuid4().hex
        with patch("desk_orchestrator.virtual_numpad.time", SimpleNamespace(time=lambda: 10**10)):
            self.post("/api/map/run", kind="command", device="kvm", command="kvm:1", request_id=request_id)
        with patch("desk_orchestrator.virtual_numpad.time", SimpleNamespace(time=lambda: 10**10 + 5)):
            repeat = self.post("/api/map/run", kind="command", device="kvm", command="kvm:1", request_id=request_id)
        self.assertEqual(repeat.status_code, 409)
        self.assertIn("already received", repeat.json["error"])
        self.execute.assert_called_once_with({"kind": "kvm", "port": 1})

    def test_controller_hardware_page_only_edits_name_and_notes(self):
        page = self.client.get(f"/hardware/{CONTROLLER}/edit").get_data(as_text=True)
        self.assertIn("fixed node on the Hardware Map", page)
        response = self.post(f"/hardware/{CONTROLLER}/edit", name="Pi 4", type="Audio", method="ir",
                             notes="Rack shelf", settings=json.dumps({"remote": "x"}))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], f"/hardware?device={CONTROLLER}")
        item = self.store.read()["inventory"][CONTROLLER]
        self.assertEqual((item["name"], item["type"], item["method"], item["settings"]), ("Pi 4", "Controller", "none", {}))
        self.assertIn("ports", item["map"])
        deleted = self.post(f"/hardware/{CONTROLLER}/delete")
        self.assertEqual(deleted.status_code, 409)
        self.assertIn(CONTROLLER, self.store.read()["inventory"])


class NumpadNodeTests(unittest.TestCase):
    PATH = "/dev/input/by-id/usb-Glorious_GMMK_Numpad-event-kbd"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(self.temp.name, SEED)

    def configure(self):
        def edit(cfg):
            cfg["keypad"].update(device=self.PATH, identity=dict(name="Glorious GMMK Numpad", vendor_id="320f",
                                                                 product_id="5088", serial="", port="1-1"))
        return self.store.update(self.store.read()["revision"], edit)

    def devices(self, cfg):
        data = model(cfg)["devices"]
        return {name: dict(name=d["name"], type=d["type"], notes=d["notes"], method=d["method"], map=d["map"])
                for name, d in data.items()}

    def test_numpad_appears_only_when_configured(self):
        self.assertNotIn(NUMPAD, model(self.store.read())["devices"])  # seed path is a placeholder
        node = model(self.configure())["devices"][NUMPAD]
        self.assertEqual((node["name"], node["type"], node["fixed"]), ("Glorious GMMK Numpad", "Numpad", "numpad"))
        self.assertEqual(node["summary"], "5 task keys")
        # Until it is saved, it has one USB port wired to the controller's numpad input.
        self.assertEqual(node["map"], {"ports": {"usb": {"label": "USB", "signal": "usb", "direction": "out"}},
                                       "links": [{"port": "usb", "to": CONTROLLER, "to_port": "numpad_in"}]})
        self.assertNotIn(NUMPAD, self.store.read()["inventory"])

    def test_numpad_map_is_saved_in_keypad_section_and_can_be_linked(self):
        cfg = self.configure()
        devices = self.devices(cfg)
        devices[NUMPAD]["map"] = dict(devices[NUMPAD]["map"], x=340, y=40)
        devices["macbook"]["map"] = {"x": 40, "y": 40, "ports": {"kb": {"label": "Keyboard", "signal": "usb", "direction": "in"}},
                                     "links": [{"port": "kb", "to": NUMPAD, "to_port": "usb"}]}
        saved = self.store.save_hardware_map(cfg["revision"], devices)
        self.assertEqual(saved["keypad"]["map"]["x"], 340)
        self.assertEqual(saved["keypad"]["device"], self.PATH)
        self.assertNotIn(NUMPAD, saved["inventory"])
        self.assertEqual(model(saved)["devices"][NUMPAD]["map"], saved["keypad"]["map"])
        # Selecting the numpad again on the Numpad page keeps its map.
        from desk_orchestrator.usb_devices import save_selection
        saved = self.store.update(saved["revision"], lambda c: save_selection(c, "manual", "/dev/input/event9", True, {"candidates": [], "devices": []}))
        self.assertEqual(saved["keypad"]["map"]["x"], 340)
        # Deleting hardware removes its connections from the numpad map too.
        devices = self.devices(saved)
        devices["hub"] = dict(name="Hub", type="Peripheral", notes="", method="none",
                              map={"x": 0, "y": 600, "ports": {"in": {"label": "In", "signal": "usb", "direction": "in"}}, "links": []})
        devices[NUMPAD]["map"]["links"].append({"port": "usb", "to": "hub", "to_port": "in"})
        saved = self.store.save_hardware_map(saved["revision"], devices)
        saved = self.store.delete_hardware(saved["revision"], "hub")
        self.assertEqual([link["to"] for link in saved["keypad"]["map"]["links"]], [CONTROLLER])

    def test_numpad_map_rules(self):
        before = self.store.read()
        unconfigured = self.devices(before)
        unconfigured[NUMPAD] = dict(name="Numpad", type="Numpad", notes="", method="numpad", map={"ports": {}, "links": []})
        with self.assertRaisesRegex(DeskError, "Numpad page"):
            self.store.save_hardware_map(before["revision"], unconfigured)
        cfg = self.configure()
        action = self.devices(cfg)
        action[NUMPAD]["map"]["ports"]["usb"]["action"] = {"kind": "kvm", "port": 1}
        with self.assertRaisesRegex(DeskError, "port action"):
            self.store.save_hardware_map(cfg["revision"], action)
        with self.assertRaisesRegex(DeskError, "reserved"):
            self.store.save_hardware(cfg["revision"], NUMPAD, dict(name="Fake", type="Other", method="none",
                                                                   notes="", settings={}), creating=True)
        self.assertEqual(self.store.read(), cfg)

    def test_restore_drops_links_to_a_numpad_this_host_does_not_have(self):
        cfg = self.configure()
        devices = self.devices(cfg)
        devices["macbook"]["map"] = {"x": 40, "y": 40, "ports": {"kb": {"label": "Keyboard", "signal": "usb", "direction": "in"}},
                                     "links": [{"port": "kb", "to": NUMPAD, "to_port": "usb"}]}
        backup = self.store.save_hardware_map(cfg["revision"], devices)
        other = ConfigStore(tempfile.mkdtemp(dir=self.temp.name), SEED)
        restored = other.restore(other.read()["revision"], {"format": "desk-backup-1", "config": backup})
        self.assertEqual(restored["inventory"]["macbook"]["map"]["links"], [])


if __name__ == "__main__":
    unittest.main()
