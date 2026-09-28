"""Browser input adapter using the same live runner and control path as evdev."""
import json
import re
import time
from contextlib import contextmanager

from .config_store import KEYS
from .controls import CONTROL_NAMES, active_scene, run_control, selected_mapping, selected_index
from .core import DeskError, Runner, atomic_json, exclusive, require
from .diagnostics import trace
from .hardware import Hardware
from .key_commands import command_label, reserved_keys, run_key_command


def locked(path):
    try:
        with exclusive(path):
            return False
    except DeskError:
        return True


def key_commands(cfg, active):
    reserved = reserved_keys(cfg)
    return {key: command_label(command, cfg) for key, command in
            cfg["scenes"].get(active, {}).get("key_commands", {}).items()
            if key not in reserved}


def state(cfg):
    runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
    active = active_scene(runner)
    controls = {}
    for name in CONTROL_NAMES:
        source = cfg["keypad"].get("controls", {}).get(name)
        configured = cfg["scenes"].get(active, {}).get("controls", {}).get(name, {})
        mapping = selected_mapping(runner, active, name)
        controls[name] = {"enabled": bool(source and mapping),
                          "switch_enabled": bool(source and "press_code" in source and
                              configured.get("click")),
                          "source_index": selected_index(runner, active, name, configured) if configured else 0,
                          "source_count": len(configured.get("click", {}).get("sources", [])),
                          "target": cfg.get("inventory", {}).get(mapping["device"], {}).get("name", mapping["device"]) if mapping else "Unassigned"}
    busy = locked(runner.runtime / "virtual-input.lock") or locked(runner.runtime / "scene.lock")
    return {"revision": cfg["revision"], "active_task": active,
            "active_label": cfg["scenes"][active]["label"] if active else "None",
            "key_commands": key_commands(cfg, active),
            "busy": busy, "controls": controls}


@contextmanager
def input_session(runner, request_id, kind, *, source, **details):
    """Accept one live browser input: rejected (never queued) when another is in
    progress, repeated, or too fast. Shared by the virtual numpad and the map."""
    require(bool(re.fullmatch(r"[a-f0-9]{32}", request_id or "")), "Invalid input request ID.")
    # Shared by HTTP/HTTPS workers. A second request is rejected, never queued.
    with exclusive(runner.runtime / "virtual-input.lock"), trace(request_id):
        receipt_path = runner.runtime / "virtual-input.json"
        receipts = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        seen = receipts.get("requests", [])
        require(request_id not in seen, "This input was already received and will not be repeated.")
        now = time.time()
        interval = .1 if kind == "control" else .35
        require(not 0 <= now - receipts.get(kind, 0) < interval, "Input ignored: pressed too quickly; not queued.")
        receipts.update(requests=(seen + [request_id])[-128:])
        receipts[kind] = now
        # Record before dispatch, including failures: uncertain input is never retried.
        atomic_json(receipt_path, receipts)
        runner.log.emit("input", "Virtual numpad input received" if source == "virtual_numpad" else "Hardware map input received",
                        source=source, live=True, **details)
        yield


def press(cfg, data):
    require(data.get("revision") == str(cfg["revision"]), "Mappings changed. Wait for the background update before pressing again.")
    request_id = data.get("request_id", "")
    require(bool(re.fullmatch(r"[a-f0-9]{32}", request_id)), "Invalid input request ID.")
    kind = data.get("kind")
    scene = None
    if kind == "key":
        key = data.get("key", "")
        require(key in KEYS, "Unknown numpad key.")
        scene = cfg["keypad"]["bindings"].get(key)
        if scene is None:
            scene = data.get("active_task")
            require(scene in cfg["scenes"] and key in cfg["scenes"][scene].get("key_commands", {}),
                    "This key has no task or active command assigned.")
    elif kind == "control":
        name, direction = data.get("control"), data.get("direction")
        require(name in CONTROL_NAMES and (direction in ("increase", "decrease") or
                name == "dial" and direction == "switch"), "Invalid control input.")
        source = cfg["keypad"].get("controls", {}).get(name)
        require(source, "This control is disabled in Numpad settings.")
        if direction == "switch":
            require("press_code" in source, "Configure the dial click input in Numpad settings first.")
        scene = data.get("active_task")
        require(scene in cfg["scenes"] and name in cfg["scenes"][scene].get("controls", {}),
                "No active task mapping for this control.")
        if direction != "switch" and source.get("invert", False):
            direction = "decrease" if direction == "increase" else "increase"
    else:
        raise DeskError("Unknown virtual input type.")

    runner = Runner(cfg, Hardware(cfg), emit=lambda _: None)
    with input_session(runner, request_id, kind, source="virtual_numpad",
                       key=data.get("key") if kind == "key" else None,
                       control=data.get("control") if kind == "control" else None,
                       direction=data.get("direction") if kind == "control" else None):
        if kind == "key":
            if key not in cfg["keypad"]["bindings"]:
                return run_key_command(runner, key, live=True, expected_scene=scene)
            runner.log.emit("input", "Key mapped to task", source="virtual_numpad", scene=scene, live=True,
                            revision=cfg["revision"])
            return runner.run(scene, live=True)
        result = run_control(runner, name, direction, live=True, expected_scene=scene)
        require(result, "No active task mapping for this control.")
        return result
