"""Atomic, versioned configuration shared by the web app and keypad service."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .core import DeskError, atomic_json, exclusive, load_config, number, require, validate_config
from .key_commands import KEYS, is_ir_binding
from .appearance import COLORS, ICONS
from . import hardware_map
from .hardware_map import CONTROLLER, CONTROLLER_TYPE, NUMPAD

TYPES = ("Computing", "Monitor", "Audio", "KVM", "Peripheral", "Other")
METHODS = {"none": "Inventory only", "ir": "Infrared (LIRC)", "smartthings": "SmartThings", "usb_hid": "USB keyboard / KVM"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")


def identifier(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value), "ID must start with a lowercase letter and use only lowercase letters, numbers, or underscores (48 characters max).")
    return value


def text(value, label, maxlen=240):
    require(isinstance(value, str) and len(value) <= maxlen and "\x00" not in value, f"Invalid {label}.")
    return value


def sequence(value):
    require(isinstance(value, list) and len(value) <= 50, "An IR sequence must be a list of at most 50 pulses.")
    total, duration = 0, 0
    for pulse in value:
        require(isinstance(pulse, dict) and set(pulse) <= {"key", "count", "gap", "remote"}, "Invalid IR pulse fields.")
        require(re.fullmatch(r"[A-Za-z0-9_.+-]+", text(pulse.get("key"), "IR key")), "IR key names cannot contain spaces.")
        if "remote" in pulse:
            require(re.fullmatch(r"[A-Za-z0-9_.+-]+", text(pulse["remote"], "LIRC remote")), "Invalid LIRC remote name.")
        count, gap = pulse.get("count", 1), pulse.get("gap", .15)
        require(type(count) is int and 1 <= count <= 300, "Pulse count must be 1–300.")
        require(number(gap, .02, 5), "Pulse gap must be 0.02–5 seconds.")
        total += count
        duration += count * gap
    require(total <= 600 and duration <= 120, "IR sequence exceeds transmission or duration limits.")


def validate_hardware(item):
    require(isinstance(item, dict), "Hardware must be an object.")
    # "map" is optional: the Hardware Map layout, ports and connections for this device.
    require({"name", "type", "method", "notes", "settings"} <= set(item) <= {"name", "type", "method", "notes", "settings", "map"},
            "Invalid hardware fields.")
    require(text(item["name"], "hardware name", 100).strip(), "Hardware needs a name.")
    require(item["type"] in (*TYPES, CONTROLLER_TYPE) and item["method"] in METHODS, "Choose a supported hardware type and control method.")
    text(item["notes"], "notes", 2000)
    cfg = item["settings"]
    require(isinstance(cfg, dict), "Hardware settings must be an object.")
    method = item["method"]
    if method == "none":
        require(not cfg, "Inventory-only hardware has no controller settings.")
    elif method == "ir":
        require(set(cfg) <= {"remote", "commands", "volume_presets", "allow_approximate_volume"}, "Invalid IR settings.")
        text(cfg.get("remote", ""), "remote name")
        require(type(cfg.get("allow_approximate_volume", False)) is bool, "Approximate volume must be a checkbox.")
        commands = cfg.get("commands", {})
        require(isinstance(commands, dict) and len(commands) <= 100, "Invalid command list.")
        for name, macro in commands.items():
            identifier(name)
            require(isinstance(macro, dict) and set(macro) <= {"verified", "discrete", "sequence"}, "Invalid command settings.")
            require(type(macro.get("verified", False)) is bool and type(macro.get("discrete", False)) is bool, "Command flags must be checkboxes.")
            sequence(macro.get("sequence", []))
        presets, targets = cfg.get("volume_presets", []), set()
        require(isinstance(presets, list) and len(presets) <= 50, "Invalid volume presets.")
        for preset in presets:
            require(isinstance(preset, dict) and set(preset) <= {"db", "calibrated", "starts_from_known_reference", "sequence"}, "Invalid volume preset fields.")
            require(number(preset.get("db"), -120, 20) and preset["db"] not in targets, "Volume presets need unique numeric dB targets.")
            targets.add(preset["db"])
            require(type(preset.get("calibrated", False)) is bool and type(preset.get("starts_from_known_reference", False)) is bool, "Volume flags must be checkboxes.")
            sequence(preset.get("sequence", []))
    elif method == "smartthings":
        require(set(cfg) <= {"device_id", "component", "capability", "command", "attribute", "confirmed", "inputs", "verify_timeout"}, "Invalid SmartThings fields.")
        for field in ("device_id", "component", "capability", "command", "attribute"):
            text(cfg.get(field, ""), field)
        require(type(cfg.get("confirmed", False)) is bool, "Confirmed must be a checkbox.")
        require(number(cfg.get("verify_timeout", 15), 1, 60), "Verification timeout must be 1–60 seconds.")
        inputs = cfg.get("inputs", {})
        require(isinstance(inputs, dict) and len(inputs) <= 50, "Invalid input mappings.")
        for name, value in inputs.items():
            identifier(name)
            require(text(value, "input value"), "Input values cannot be empty.")
    else:
        from .keyboard_transport import validate_settings
        validate_settings(cfg)


def validate_catalog(config):
    validate_config(config, ".")
    inventory = config.get("inventory")
    require(isinstance(inventory, dict) and len(inventory) <= 100, "Invalid hardware inventory.")
    kvms = []
    for name, item in inventory.items():
        identifier(name)
        validate_hardware(item)
        require((item["type"] == CONTROLLER_TYPE) == (name == CONTROLLER), "The Controller type is reserved for the Raspberry Pi controller.")
        if name == CONTROLLER:
            require(item["method"] == "none" and not item["settings"], "The Raspberry Pi controller is inventory only.")
        hardware_map.validate_item_map(name, item)
        if item["method"] == "usb_hid":
            kvms.append(name)
    require(len(kvms) <= 1, "This controller supports one USB KVM. Edit the existing KVM instead.")
    hardware_map.validate_numpad(config)
    hardware_map.validate_links(hardware_map.map_nodes(config))
    require(len(config["scenes"]) <= 100, "Maximum 100 tasks.")
    for name, task in config["scenes"].items():
        identifier(name)
        for field, choices in (("color", COLORS), ("icon", ICONS)):
            if field in task:
                require(isinstance(task[field], str) and task[field] in choices,
                        f"Choose a supported task {field}.")
        require(text(task.get("label", name), "task name", 100).strip(), "Task needs a name.")
        require(len(task["steps"]) <= 100, "Maximum 100 actions per task.")
        bindings = task.get("key_commands", {}).values()
        require(not any(not is_ir_binding(command) for command in bindings) or kvms,
                f"{name}: add USB keyboard / KVM hardware before binding keyboard commands.")
        for control, mapping in task.get("controls", {}).items():
            from .controls import control_mappings
            for target in control_mappings(mapping):
                device = inventory.get(target["device"])
                require(device and device["method"] == "ir", f"{name}/{control}: select IR hardware for directional control.")
                commands = device["settings"].get("commands", {})
                require(all(target[key] in commands for key in ("increase", "decrease")),
                        f"{name}/{control}: configure both IR commands on the selected hardware first.")
        for step in [*task["steps"], *(command for command in bindings if is_ir_binding(command))]:
            kind = step["kind"]
            if kind == "wait":
                continue
            if kind == "kvm":
                require(kvms, f"{name}: add a KVM before selecting a port.")
                continue
            device = inventory.get(step["device"])
            expected = "smartthings" if kind == "monitor" else "ir"
            require(device and device["method"] == expected, f"{name}: action references missing or incompatible hardware {step['device']}.")
            settings = device["settings"]
            if kind == "ir":
                require(step["command"] in settings.get("commands", {}), f"{name}: unknown command {step['command']}.")
            elif kind == "monitor":
                require(step["input"] in settings.get("inputs", {}), f"{name}: unknown input {step['input']}.")
            else:
                require(any(p["db"] == step["db"] for p in settings.get("volume_presets", [])), f"{name}: add a volume preset for {step['db']} dB first.")
    require(set(config.get("keypad", {}).get("bindings", {})) <= set(KEYS), "Unsupported numpad key.")
    return config


def compile_inventory(config):
    config["monitors"] = {}
    config.setdefault("ir", {})["devices"] = {}
    config.pop("kvm", None)
    for name, item in config["inventory"].items():
        settings = copy.deepcopy(item["settings"])
        if item["method"] == "ir":
            config["ir"]["devices"][name] = settings
        elif item["method"] == "smartthings":
            config["monitors"][name] = settings
        elif item["method"] == "usb_hid":
            config["kvm"] = settings
    return config


def migrate(config):
    config = copy.deepcopy(config)
    if "inventory" in config:
        return config
    inventory = {}
    for name, settings in config.get("ir", {}).get("devices", {}).items():
        label = {"oppo": "OPPO HA-1", "amplifier": "S.M.S.L DA-9"}.get(name, name)
        inventory[name] = dict(name=label, type="Audio", method="ir", notes="", settings=settings)
    for name, settings in config.get("monitors", {}).items():
        label = {"g8": "Samsung G8 OLED", "g9": "Samsung G9 OLED"}.get(name, name)
        inventory[name] = dict(name=label, type="Monitor", method="smartthings", notes="", settings=settings)
    if config.get("kvm"):
        inventory["kvm"] = dict(name="Level1Techs KVM", type="KVM", method="usb_hid", notes="Port 1: Linux PC · Port 2: Steam Deck · Port 3: MacBook", settings=config["kvm"])
        if {"personal", "work", "steam_deck"} <= set(config["scenes"]):
            for name, label, note in [("linux_pc", "Custom Linux PC", "CachyOS · KVM port 1 · G8 DisplayPort"),
                                      ("steam_deck", "Valve Steam Deck", "KVM port 2"),
                                      ("macbook", "Apple MacBook Pro", "KVM port 3 · G8 HDMI 1")]:
                if name not in inventory:
                    inventory[name] = dict(name=label, type="Computing", method="none", notes=note, settings={})
    config["inventory"] = inventory
    hardware_map.ensure_controller(config)
    return config


class ConfigStore:
    def __init__(self, directory, seed):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "controller.json"
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive(self.directory / "config.lock", blocking=True):
            if not self.path.exists():
                config = migrate(load_config(seed))
                config["runtime"]["directory"] = str(self.directory)
                config["revision"] = 1
                validate_catalog(config)
                atomic_json(self.path, config)
            else:
                self._add_controller()

    def _add_controller(self):
        """One-time upgrade: saved configurations from before the Hardware Map
        gain the controller node. An invalid file is left for read() to report."""
        try:
            config = self.read()
            if hardware_map.ensure_controller(config):
                validate_catalog(config)
                compile_inventory(config)
                config["revision"] += 1
                atomic_json(self.path, config)
        except (DeskError, OSError, ValueError, KeyError, TypeError):
            pass

    def read(self):
        return load_config(self.path)

    def update(self, revision, edit):
        with exclusive(self.directory / "config.lock", blocking=True):
            config = self.read()
            require(type(revision) is int and revision == config["revision"], "Configuration changed in another tab. Reload before saving; your changes were not applied.")
            edit(config)
            validate_catalog(config)
            compile_inventory(config)
            config["revision"] += 1
            atomic_json(self.path, config)
            return config

    def save_hardware(self, revision, name, item, *, creating=False):
        identifier(name)
        validate_hardware(item)
        def edit(config):
            require((name not in config["inventory"]) if creating else (name in config["inventory"]), "Hardware ID already exists or was deleted.")
            saved = copy.deepcopy(item)
            # The hardware form edits settings only; keep the device's map layout.
            if "map" not in saved and "map" in config["inventory"].get(name, {}):
                saved["map"] = copy.deepcopy(config["inventory"][name]["map"])
            config["inventory"][name] = saved
        return self.update(revision, edit)

    def delete_hardware(self, revision, name):
        def edit(config):
            require(name in config["inventory"], "Hardware no longer exists.")
            require(name != CONTROLLER, "The Raspberry Pi controller is part of the Hardware Map and cannot be deleted.")
            del config["inventory"][name]
            hardware_map.drop_links_to(config, name)
        return self.update(revision, edit)

    def save_task(self, revision, name, task, key, *, creating=False):
        identifier(name)
        require(key == "" or key in KEYS, "Choose a valid numpad key.")
        def edit(config):
            require((name not in config["scenes"]) if creating else (name in config["scenes"]), "Task ID already exists or was deleted.")
            bindings = config.setdefault("keypad", {}).setdefault("bindings", {})
            require(not key or bindings.get(key, name) == name, f"Numpad key is already assigned to {bindings.get(key)}.")
            config["keypad"]["bindings"] = {k: v for k, v in bindings.items() if v != name}
            if key:
                config["keypad"]["bindings"][key] = name
            config["scenes"][name] = copy.deepcopy(task)
        return self.update(revision, edit)

    def delete_task(self, revision, name):
        def edit(config):
            require(name in config["scenes"], "Task no longer exists.")
            del config["scenes"][name]
            config["keypad"]["bindings"] = {k: v for k, v in config["keypad"]["bindings"].items() if v != name}
        return self.update(revision, edit)

    def save_hardware_map(self, revision, devices):
        """Save the Hardware page's edit mode in one step: hardware added, renamed or
        removed there, plus every device's position, ports and connections.
        Control settings are edited separately; a new device starts with none."""
        hardware_map.parse_devices(devices)
        numpad = devices.get(NUMPAD)
        devices = {name: entry for name, entry in devices.items() if name != NUMPAD}
        def edit(config):
            if numpad is not None:
                # Only the numpad's map is edited here; the device itself is chosen on the Numpad page.
                require(hardware_map.numpad_configured(config), "Select the numpad on the Numpad page before mapping it.")
                if numpad.get("map") is None:
                    config["keypad"].pop("map", None)
                else:
                    config["keypad"]["map"] = copy.deepcopy(numpad["map"])
            inventory = config["inventory"]
            for name in [name for name in inventory if name not in devices]:
                require(name != CONTROLLER, "The Raspberry Pi controller cannot be deleted.")
                # Hardware still used by a task is rejected by catalog validation.
                del inventory[name]
            for name, entry in devices.items():
                identifier(name)
                if name in inventory:
                    item = inventory[name]
                    require(entry.get("method", item["method"]) == item["method"],
                            f"{item['name']}: change the control method in Control settings.")
                else:
                    require(entry.get("method") in METHODS, "Choose a control method for new hardware.")
                    item = inventory[name] = dict(name="", type="Other", method=entry["method"], notes="", settings={})
                item.update(name=entry["name"].strip(), notes=entry["notes"],
                            type=CONTROLLER_TYPE if name == CONTROLLER else entry["type"])
                if entry.get("map") is None:
                    item.pop("map", None)
                else:
                    item["map"] = copy.deepcopy(entry["map"])
        return self.update(revision, edit)

    def restore(self, revision, incoming):
        require(isinstance(incoming, dict) and incoming.get("format") == "desk-backup-1", "Choose a Desk Orchestrator JSON backup.")
        backup = incoming.get("config")
        require(isinstance(backup, dict), "Backup configuration is missing.")
        validate_catalog(copy.deepcopy(backup))
        def edit(config):
            # Restore editable data only. Local executable, credential references,
            # runtime paths and numpad node belong to this Pi, not the upload.
            controller = config["inventory"].get(CONTROLLER)
            for section in ("inventory", "scenes"):
                config[section] = copy.deepcopy(backup[section])
            # Backups from before the Hardware Map keep this Pi's controller node.
            if CONTROLLER not in config["inventory"] and controller:
                config["inventory"][CONTROLLER] = controller
            # The numpad belongs to this Pi; drop connections to one that isn't configured here.
            if not hardware_map.numpad_configured(config):
                hardware_map.drop_links_to(config, NUMPAD)
            config["keypad"]["bindings"] = copy.deepcopy(backup["keypad"]["bindings"])
        return self.update(revision, edit)
