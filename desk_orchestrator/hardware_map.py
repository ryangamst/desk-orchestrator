"""Hardware Map: diagram layout, typed ports and connections stored on each
inventory item's optional ``map`` field, plus the live actions the map offers.

There is no separate map store. Positions, ports and links live in
``controller.json`` next to the hardware they describe, so backups, restore and
revision checks cover them automatically.
"""
from __future__ import annotations

import copy
import re

from .core import DeskError, Runner, exclusive, number, require

CONTROLLER = "desk_controller"
CONTROLLER_TYPE = "Controller"
# The USB numpad is configured on the Numpad page, not in the inventory. It appears
# on the map once a device is selected there; its layout lives in keypad["map"].
NUMPAD = "desk_numpad"
NUMPAD_TYPE = "Numpad"
SIGNALS = {"video": "Video", "audio": "Audio", "usb": "USB / data", "ir": "Infrared",
           "network": "Network", "power": "Power", "other": "Other"}
DIRECTIONS = {"in": "Input", "out": "Output", "both": "Two-way"}
PORT_ID = re.compile(r"[a-z][a-z0-9_]{0,47}\Z")
MAX_PORTS, MAX_LINKS, MAX_COORD = 32, 64, 5000


def controller_item():
    """The Raspberry Pi running this app. Always present, never deletable."""
    ports = {
        "ir_out": dict(label="IR transmitter", signal="ir", direction="out"),
        "usb_keyboard": dict(label="USB keyboard to KVM", signal="usb", direction="out"),
        "numpad_in": dict(label="Numpad (USB host)", signal="usb", direction="in"),
        "network": dict(label="Network / SmartThings", signal="network", direction="both"),
    }
    return dict(name="Raspberry Pi controller", type=CONTROLLER_TYPE, method="none",
                notes="Runs Desk Orchestrator: numpad listener, IR transmitter, KVM keyboard and SmartThings.",
                settings={}, map=dict(ports=ports, links=[]))  # placed automatically until moved


def ensure_controller(config):
    """Add the controller node to an inventory that predates the map. Returns True if changed."""
    inventory = config.get("inventory")
    if not isinstance(inventory, dict) or CONTROLLER in inventory:
        return False
    inventory[CONTROLLER] = controller_item()
    return True


def action_step(name, item, action):
    """Translate a port action into the runner step it performs on this device."""
    require(isinstance(action, dict), "Invalid port action.")
    kind, method, settings = action.get("kind"), item["method"], item["settings"]
    if kind == "ir":
        require(method == "ir" and set(action) == {"kind", "command"}, "IR port actions need IR hardware.")
        require(action["command"] in settings.get("commands", {}),
                f"{item['name']}: port action uses unknown IR command {action['command']}.")
        return {"kind": "ir", "device": name, "command": action["command"]}
    if kind == "monitor":
        require(method == "smartthings" and set(action) == {"kind", "input"}, "Input port actions need SmartThings hardware.")
        require(action["input"] in settings.get("inputs", {}),
                f"{item['name']}: port action uses unknown input {action['input']}.")
        return {"kind": "monitor", "device": name, "input": action["input"]}
    if kind == "kvm":
        require(method == "usb_hid" and set(action) == {"kind", "port"}, "KVM port actions need USB keyboard / KVM hardware.")
        require(type(action["port"]) is int and 1 <= action["port"] <= 4, "KVM port must be 1–4.")
        return {"kind": "kvm", "port": action["port"]}
    raise DeskError("Choose a supported port action.")


def validate_item_map(name, item):
    """Structure of one item's map. Links are checked against the catalog separately."""
    layout = item.get("map")
    if layout is None:
        return
    require(isinstance(layout, dict) and set(layout) <= {"x", "y", "ports", "links"}, f"{item['name']}: invalid map fields.")
    require(number(layout.get("x", 0), 0, MAX_COORD) and number(layout.get("y", 0), 0, MAX_COORD),
            f"{item['name']}: map position is out of range.")
    ports = layout.get("ports", {})
    require(isinstance(ports, dict) and len(ports) <= MAX_PORTS, f"{item['name']}: at most {MAX_PORTS} ports.")
    for port_id, port in ports.items():
        require(isinstance(port_id, str) and PORT_ID.fullmatch(port_id), f"{item['name']}: invalid port ID.")
        require(isinstance(port, dict) and {"label", "signal", "direction"} <= set(port) <= {"label", "signal", "direction", "action"},
                f"{item['name']}: invalid port fields.")
        label = port["label"]
        require(isinstance(label, str) and label.strip() and len(label) <= 60 and "\x00" not in label,
                f"{item['name']}: each port needs a name of 60 characters or fewer.")
        require(port["signal"] in SIGNALS, f"{item['name']}: choose a supported signal type for {label}.")
        require(port["direction"] in DIRECTIONS, f"{item['name']}: choose a direction for {label}.")
        if "action" in port:
            action_step(name, item, port["action"])
    links = layout.get("links", [])
    require(isinstance(links, list) and len(links) <= MAX_LINKS, f"{item['name']}: at most {MAX_LINKS} connections.")
    for link in links:
        require(isinstance(link, dict) and set(link) == {"port", "to", "to_port"}, f"{item['name']}: invalid connection fields.")
        require(link["port"] in ports, f"{item['name']}: a connection uses a port that no longer exists.")


def validate_links(inventory):
    seen = set()
    for name, item in inventory.items():
        for link in item.get("map", {}).get("links", []):
            other = inventory.get(link["to"])
            require(isinstance(link["to"], str) and other is not None,
                    f"{item['name']}: a connection points to hardware that no longer exists.")
            require(link["to_port"] in other.get("map", {}).get("ports", {}),
                    f"{item['name']}: a connection points to a port missing on {other['name']}.")
            ends = frozenset([(name, link["port"]), (link["to"], link["to_port"])])
            require(len(ends) == 2, f"{item['name']}: a port cannot connect to itself.")
            require(ends not in seen, f"{item['name']}: duplicate connection.")
            seen.add(ends)


def drop_links_to(config, name):
    for layout in [item.get("map") for item in config["inventory"].values()] + [config.get("keypad", {}).get("map")]:
        if layout:
            layout["links"] = [link for link in layout.get("links", []) if link["to"] != name]


def numpad_configured(config):
    device = config.get("keypad", {}).get("device", "")
    return isinstance(device, str) and device.startswith("/dev/") and "REPLACE_WITH" not in device


def default_numpad_map(config):
    """Until the numpad is moved and saved: one USB port wired to the controller."""
    ports = {"usb": dict(label="USB", signal="usb", direction="out")}
    controller = config.get("inventory", {}).get(CONTROLLER, {})
    links = ([dict(port="usb", to=CONTROLLER, to_port="numpad_in")]
             if "numpad_in" in controller.get("map", {}).get("ports", {}) else [])
    return dict(ports=ports, links=links)


def numpad_item(config):
    keypad = config.get("keypad", {})
    return dict(name=keypad.get("identity", {}).get("name") or "USB numpad", type=NUMPAD_TYPE, method="none",
                notes=keypad.get("device", ""), settings={},
                map=copy.deepcopy(keypad["map"]) if "map" in keypad else default_numpad_map(config))


def map_nodes(config):
    """Everything drawn on the map: the inventory plus the configured numpad."""
    nodes = dict(config["inventory"])
    if numpad_configured(config):
        nodes[NUMPAD] = numpad_item(config)
    return nodes


def validate_numpad(config):
    require(NUMPAD not in config["inventory"], f"The ID {NUMPAD} is reserved for the numpad.")
    if "map" in config.get("keypad", {}):
        # A numpad port has no command: key presses are configured per task.
        validate_item_map(NUMPAD, dict(numpad_item(config), map=config["keypad"]["map"]))


def device_commands(cfg, name):
    """Individual actions a device supports, each with a stable ID and a runner step."""
    item = cfg["inventory"].get(name)
    if not item:
        return []
    settings, commands = item["settings"], []
    ports = item.get("map", {}).get("ports", {})

    def port_label(action):
        return next((port["label"] for port in ports.values() if port.get("action") == action), None)

    if item["method"] == "ir":
        for command in settings.get("commands", {}):
            label = port_label({"kind": "ir", "command": command})
            commands.append(dict(id=f"ir:{command}", label=command.replace("_", " ") + (f" · {label}" if label else ""),
                                 step={"kind": "ir", "device": name, "command": command}))
        for preset in settings.get("volume_presets", []):
            commands.append(dict(id=f"volume:{preset['db']}", label=f"Volume preset {preset['db']:+g} dB",
                                 step={"kind": "volume", "device": name, "db": preset["db"]}))
    elif item["method"] == "smartthings":
        for input_name in settings.get("inputs", {}):
            label = port_label({"kind": "monitor", "input": input_name})
            commands.append(dict(id=f"input:{input_name}", label=f"Switch to {label or input_name}",
                                 step={"kind": "monitor", "device": name, "input": input_name}))
    elif item["method"] == "usb_hid":
        for port in range(1, 5):
            label = port_label({"kind": "kvm", "port": port})
            commands.append(dict(id=f"kvm:{port}", label=f"Switch to port {port}" + (f" · {label}" if label else ""),
                                 step={"kind": "kvm", "port": port}))
    return commands


def kvm_name(cfg):
    return next((name for name, item in cfg["inventory"].items() if item["method"] == "usb_hid"), None)


def step_touches(cfg, step):
    """Device and matching ports a task step acts on, for route highlighting."""
    if step["kind"] == "wait":
        return None
    name = kvm_name(cfg) if step["kind"] == "kvm" else step["device"]
    item = cfg["inventory"].get(name)
    if not item:
        return None
    ports = []
    for port_id, port in item.get("map", {}).get("ports", {}).items():
        if "action" in port:
            try:
                target = action_step(name, item, port["action"])
            except DeskError:
                continue
            if target == step:
                ports.append(port_id)
    return dict(device=name, ports=ports)


def model(cfg):
    """Everything the map page draws. The browser never writes anything but maps."""
    from .appearance import task_appearance
    from .key_commands import KEYS, command_label
    keys_by_task = {}
    for key, task in cfg["keypad"].get("bindings", {}).items():
        keys_by_task.setdefault(task, []).append(dict(key=key, label=KEYS.get(key, key)))
    devices = {}
    for name, item in cfg["inventory"].items():
        choices = []
        if item["method"] == "ir":
            choices = [dict(value={"kind": "ir", "command": c}, label=f"IR: {c}") for c in item["settings"].get("commands", {})]
        elif item["method"] == "smartthings":
            choices = [dict(value={"kind": "monitor", "input": i}, label=f"Input: {i}") for i in item["settings"].get("inputs", {})]
        elif item["method"] == "usb_hid":
            choices = [dict(value={"kind": "kvm", "port": p}, label=f"KVM port {p}") for p in range(1, 5)]
        devices[name] = dict(name=item["name"], type=item["type"], method=item["method"], notes=item["notes"],
                             summary=summary(item),
                             map=copy.deepcopy(item.get("map")), commands=[dict(id=c["id"], label=c["label"])
                             for c in device_commands(cfg, name)], action_choices=choices)
    if numpad_configured(cfg):
        item = numpad_item(cfg)
        controls = [name for name in ("dial", "slider") if name in cfg["keypad"].get("controls", {})]
        devices[NUMPAD] = dict(name=item["name"], type=NUMPAD_TYPE, method="numpad", notes=item["notes"],
                               summary=f"{len(cfg['keypad'].get('bindings', {}))} task keys"
                                       + (f" · {' and '.join(controls)} configured" if controls else ""),
                               map=item["map"], commands=[], action_choices=[], fixed="numpad")
    from .controls import control_mappings
    from .key_commands import is_ir_binding
    tasks = {}
    for name, task in cfg["scenes"].items():
        touches = {}
        for step in task["steps"]:
            hit = step_touches(cfg, step)
            if hit:
                touches.setdefault(hit["device"], set()).update(hit["ports"])
        # Every device the task depends on, so the editor can explain why one can't be deleted.
        uses = set(touches)
        for mapping in task.get("controls", {}).values():
            uses.update(target["device"] for target in control_mappings(mapping))
        for command in task.get("key_commands", {}).values():
            uses.add(command["device"] if is_ir_binding(command) else kvm_name(cfg))
        tasks[name] = dict(label=task.get("label", name), keys=keys_by_task.get(name, []),
                           keep_active_task=task.get("keep_active_task", False),
                           touches={device: sorted(ports) for device, ports in touches.items()},
                           uses=sorted(device for device in uses if device),
                           key_commands={key: command_label(command, cfg) for key, command in task.get("key_commands", {}).items()},
                           **task_appearance(name, task))
    from .config_store import METHODS, TYPES
    return dict(revision=cfg["revision"], controller=CONTROLLER, numpad=NUMPAD, devices=devices, tasks=tasks,
                signals=SIGNALS, directions=DIRECTIONS, keys=KEYS, types=list(TYPES), methods=METHODS,
                bindings=dict(cfg["keypad"].get("bindings", {})))


def parse_devices(value):
    """Shape check for the edit-mode save; field rules are enforced by catalog validation."""
    invalid = "Invalid hardware data. Reload the page and try again."
    require(isinstance(value, dict) and len(value) <= 100, invalid)
    for name, entry in value.items():
        require(isinstance(name, str) and isinstance(entry, dict), invalid)
        require({"name", "type", "notes"} <= set(entry) <= {"name", "type", "notes", "method", "map"}, invalid)
        require(all(isinstance(entry[field], str) for field in ("name", "type", "notes")), invalid)
        require(entry.get("map") is None or isinstance(entry["map"], dict), invalid)
    return value


def summary(item):
    settings = item["settings"]
    if item["method"] == "ir":
        return f"{len(settings.get('commands', {}))} IR commands · {len(settings.get('volume_presets', []))} volume presets"
    if item["method"] == "smartthings":
        return f"{len(settings.get('inputs', {}))} input mappings"
    if item["method"] == "usb_hid":
        return "4 computer ports"
    return "Inventory reference"


def run(cfg, data):
    """Live map input: a whole task or one device command. Same lock, receipts and
    no-retry rules as the virtual numpad; the controller never queues input."""
    from .hardware import Hardware
    from .virtual_numpad import input_session
    require(data.get("revision") == str(cfg["revision"]), "The map changed. Wait for the background update before trying again.")
    kind = data.get("kind")
    runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
    if kind == "task":
        name = data.get("task", "")
        require(name in cfg["scenes"], "Unknown task.")
        with input_session(runner, data.get("request_id", ""), "map", source="hardware_map", task=name):
            runner.log.emit("input", "Map task run requested", source="hardware_map", scene=name, live=True,
                            revision=cfg["revision"])
            return runner.run(name, live=True)
    if kind == "command":
        device = data.get("device", "")
        command = next((c for c in device_commands(cfg, device) if c["id"] == data.get("command")), None)
        require(command is not None, "This device command no longer exists. Reload the map.")
        with input_session(runner, data.get("request_id", ""), "map", source="hardware_map",
                           device=device, command=command["id"]):
            # A single command shares the scene lock but never changes the active task.
            with exclusive(runner.runtime / "scene.lock"):
                runner.execute_step(command["step"], live=True, source="hardware_map", device=device)
            return dict(device=device, command=command["id"], status="commands_sent")
    raise DeskError("Unknown map input type.")
