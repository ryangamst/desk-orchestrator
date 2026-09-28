"""Per-task directional controls, with bounded IR actions and no inferred volume."""
import copy
import json
import math
from pathlib import Path

from .core import DeskError, atomic_json, exclusive, number, require
from .diagnostics import trace, error_text

CONTROL_NAMES = {"dial": "Rotary dial", "slider": "Slider"}


def validate_sources(sources, *, check_signal_codes=False):
    from .usb_devices import valid_input_path
    require(isinstance(sources, dict) and set(sources) <= set(CONTROL_NAMES), "Unknown numpad control.")
    signatures = set()
    for name, source in sources.items():
        require(isinstance(source, dict) and set(source) <= {"device", "mode", "code", "down_code", "deadband", "invert", "press_code", "press_device"}, "Invalid control input fields.")
        require(valid_input_path(source.get("device")), f"{name}: choose a Linux input device path.")
        mode = source.get("mode")
        require(mode in ("keys", "relative", "absolute", "gmmk_raw"), f"{name}: invalid input signal type.")
        code = source.get("code")
        require(type(code) is int and 0 <= code <= 767, f"{name}: invalid input code.")
        if mode == "gmmk_raw":
            require(name == "slider" and code == 32, "GMMK raw HID is a slider input with fixed position code 32.")
        # Enforce Linux axis ranges on form saves, but keep older configurations
        # readable so their input settings can be corrected in the web app.
        if check_signal_codes and mode in ("relative", "absolute"):
            maximum = 15 if mode == "relative" else 63  # REL_MAX / ABS_MAX
            require(code <= maximum,
                    f"{CONTROL_NAMES[name]}: {mode} axis codes must be 0–{maximum}. "
                    "For KEY_VOLUMEUP (115) and KEY_VOLUMEDOWN (114), choose Two keys, "
                    "even when the physical control is a wheel.")
        require(type(source.get("invert", False)) is bool, "Reverse direction must be a checkbox.")
        require(number(source.get("deadband", 4), 1, 65535), "Slider movement threshold must be 1–65535.")
        codes = [code]
        if mode == "keys":
            down = source.get("down_code")
            require(type(down) is int and 0 <= down <= 767 and down != code, f"{name}: choose different increase and decrease keys.")
            codes.append(down)
        for code in codes:
            signature = (str(Path(source["device"]).resolve()), mode, code)
            require(signature not in signatures, "Dial and slider inputs overlap; choose separate signals or leave one disabled.")
            signatures.add(signature)
    # A click is always an EV_KEY signal, even when rotation uses an axis.
    for name, source in sources.items():
        if "press_code" not in source:
            require("press_device" not in source, "Configure a click key code before selecting its input device.")
            continue
        code = source["press_code"]
        require(name == "dial" and type(code) is int and 0 <= code <= 767, "Choose a valid dial click key code (0–767).")
        device = source.get("press_device", source["device"])
        require(valid_input_path(device), "Choose a Linux input device for the dial click.")
        signature = (str(Path(device).resolve()), "keys", code)
        require(signature not in signatures, "Dial click overlaps a directional input; choose a separate key.")
        signatures.add(signature)


def validate_mappings(mappings):
    require(isinstance(mappings, dict) and set(mappings) <= set(CONTROL_NAMES), "Unknown task control.")
    for name, mapping in mappings.items():
        require(isinstance(mapping, dict) and {"mode", "device", "increase", "decrease"} <= set(mapping)
                <= {"mode", "device", "increase", "decrease", "click"}, f"{name}: invalid control mapping.")
        require(mapping["mode"] in ("volume", "commands"), "Choose volume control or directional IR commands.")
        for key in ("device", "increase", "decrease"):
            require(isinstance(mapping[key], str) and mapping[key], f"{name}: select a device and both commands.")
        require(mapping["increase"] != mapping["decrease"], "Increase and decrease must use different commands.")
        if "click" in mapping:
            click = mapping["click"]
            require(name == "dial" and mapping["mode"] == "volume", "Click cycling is available for dial volume control only.")
            require(isinstance(click, dict) and set(click) == {"action", "sources"}
                    and click["action"] == "cycle_volume", "Choose a valid wheel click action.")
            require(isinstance(click["sources"], list) and 1 <= len(click["sources"]) <= 100,
                    "Add 1–100 volume sources for wheel clicks.")
            for source in click["sources"]:
                require(isinstance(source, dict) and set(source) == {"device", "increase", "decrease"},
                        "Select a device and both commands for every volume source.")
                validate_mappings({"dial": dict(mode="volume", **source)})


def control_mappings(mapping):
    sources = mapping.get("click", {}).get("sources", [])
    if sources:
        return [dict(mode="volume", **source) for source in sources]
    primary = {key: value for key, value in mapping.items() if key != "click"}
    return [primary]


def control_steps(mapping):
    return [{"kind": "ir", "device": target["device"], "command": target[direction]}
            for target in control_mappings(mapping) for direction in ("increase", "decrease")]


def check_mapping(mapping, hardware, *, probe=False):
    for step in control_steps(mapping):
        hardware.check(step, probe=probe)
        # Continuous controls must never launch long macros or volume resets.
        _, macro = hardware.macro(step)
        sequence = macro["sequence"]
        require(len(sequence) == 1 and sequence[0].get("count", 1) == 1,
                "Dial/slider commands must each contain one IR pulse with count 1.")
        require(sequence[0].get("gap", .15) <= .5, "Dial/slider pulse gap must be at most 0.5 seconds.")


class ControlDecoder:
    """Normalize key presses, relative axes, and absolute slider movement."""
    def __init__(self):
        self.positions = {}

    def reset(self):
        self.positions.clear()

    def decode(self, name, source, event, *, click=True):
        if click and name == "dial" and event.type == 1 and event.code == source.get("press_code"):
            return "switch" if event.value == 1 else None
        mode = source["mode"]
        direction = None
        if mode == "keys" and event.type == 1 and event.value == 1:
            if event.code == source["code"]:
                direction = "increase"
            elif event.code == source["down_code"]:
                direction = "decrease"
        elif mode == "relative" and event.type == 2 and event.code == source["code"] and event.value:
            direction = "increase" if event.value > 0 else "decrease"
        elif mode in ("absolute", "gmmk_raw") and event.type == 3 and event.code == source["code"]:
            if not isinstance(event.value, (int, float)) or not math.isfinite(event.value):
                return None
            previous = self.positions.get(name)
            if previous is None:
                self.positions[name] = event.value
                return None  # Establish a baseline; reconnects must not change volume.
            delta = event.value - previous
            if abs(delta) >= source.get("deadband", 4):
                self.positions[name] = event.value
                direction = "increase" if delta > 0 else "decrease"
        if direction and source.get("invert", False):
            direction = "decrease" if direction == "increase" else "increase"
        return direction


def active_scene(runner):
    """Only a completely sent task can own controls; failures disarm them."""
    try:
        status = json.loads((runner.runtime / "status.json").read_text())
        if isinstance(status, dict) and status.get("status") == "commands_sent":
            name = status.get("scene")
            if isinstance(name, str) and name in runner.config["scenes"]:
                return None if runner.config["scenes"][name].get("keep_active_task", False) else name
    except (OSError, ValueError):
        pass
    return None


def selected_index(runner, scene, name, mapping, *, live=True):
    selection = {}
    if live:
        try:
            status = json.loads((runner.runtime / "status.json").read_text())
            if isinstance(status, dict) and status.get("scene") == scene and status.get("status") == "commands_sent":
                selection = status.get("control_targets", {}).get(name, {})
        except (OSError, ValueError):
            pass
    else:
        selection = getattr(runner, "_dry_control_targets", {}).get((scene, name), {})
    default = 1 if mapping.get("click", {}).get("sources") else 0
    index = selection.get("index", default)
    # Click sources are numbered 1..N; source 1 is also the initial target.
    # Config edits invalidate the selection; task runs replace status entirely.
    return index if (selection.get("mapping") == mapping and type(index) is int
                     and default <= index < len(control_mappings(mapping)) + default) else default


def selected_mapping(runner, scene, name, *, live=True):
    mapping = runner.config.get("scenes", {}).get(scene, {}).get("controls", {}).get(name)
    if not mapping:
        return None
    index = selected_index(runner, scene, name, mapping, live=live)
    return control_mappings(mapping)[index - 1 if mapping.get("click", {}).get("sources") else index]


def run_control(runner, name, direction, *, live=False, dry_scene=None, expected_scene=None):
    require(name in CONTROL_NAMES and (direction in ("increase", "decrease") or
            name == "dial" and direction == "switch"), "Invalid control event.")
    def run():
        scene = active_scene(runner) if live else dry_scene
        if expected_scene is not None:
            require(scene == expected_scene, "The active task changed. Refresh the virtual numpad before adjusting it.")
        mapping = runner.config.get("scenes", {}).get(scene, {}).get("controls", {}).get(name)
        if not mapping:
            runner.log.emit("control", "Control ignored: no active task or no mapping", level="warning",
                            scene=scene, control=name, direction=direction, live=live)
            return
        selected = selected_mapping(runner, scene, name, live=live)
        if direction == "switch":
            require(mapping.get("click", {}).get("action") == "cycle_volume",
                    "Configure the wheel click action and volume sources in this task first.")
            targets = control_mappings(mapping)
            index = selected_index(runner, scene, name, mapping, live=live) % len(targets) + 1
            target = targets[index - 1]
            selection = {"mapping": copy.deepcopy(mapping), "index": index}
            if live:
                status_path = runner.runtime / "status.json"
                status = json.loads(status_path.read_text())
                status.setdefault("control_targets", {})[name] = selection
                atomic_json(status_path, status)
            else:
                if not hasattr(runner, "_dry_control_targets"):
                    runner._dry_control_targets = {}
                runner._dry_control_targets[scene, name] = selection
            runner.log.emit("control", "Dial volume target switched", scene=scene, control=name,
                            device=target["device"], source_index=index, live=live)
            runner.emit(f"{'LIVE' if live else 'DRY RUN'} {scene} dial target: {target['device']}")
            return {"scene": scene, "control": name, "direction": direction,
                    "device": target["device"], "source_index": index, "status": "target_switched" if live else "dry_run"}
        mapping = selected
        step = {"kind": "ir", "device": mapping["device"], "command": mapping[direction]}
        runner.log.emit("control", "Control mapped to task command", scene=scene, control=name,
                        direction=direction, step=step, live=live)
        runner.execute_step(step, live=live, scene=scene, control=name, direction=direction)
        runner.emit(f"{'LIVE' if live else 'DRY RUN'} {scene} {name} {direction}: {json.dumps(step)}")
        return {"scene": scene, "control": name, "direction": direction, "status": "commands_sent" if live else "dry_run"}
    with trace():
        try:
            if live:
                with exclusive(runner.runtime / "scene.lock"):
                    return run()
            else:
                return run()
        except BaseException as exc:
            runner.log.emit("control", "Control failed or rejected", level="error", control=name,
                            direction=direction, live=live, error=error_text(exc))
            raise
