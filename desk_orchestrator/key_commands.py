"""Task-local numpad commands routed to the keyboard transport or IR hardware."""
from pathlib import Path
import re

from .core import require, exclusive, number, validate_step
from .controls import active_scene
from .diagnostics import trace, error_text

KEYS = {**{f"KEY_KP{i}": str(i) for i in range(10)}, "KEY_KPENTER": "Enter", "KEY_KPPLUS": "+",
        "KEY_KPMINUS": "−", "KEY_KPASTERISK": "×", "KEY_KPSLASH": "/", "KEY_KPDOT": ".",
        "KEY_NUMLOCK": "Num Lock"}
KEY_CODES = dict(zip([f"KEY_KP{i}" for i in range(10)], [82, 79, 80, 81, 75, 76, 77, 71, 72, 73]))
KEY_CODES.update(KEY_KPENTER=96, KEY_KPPLUS=78, KEY_KPMINUS=74, KEY_KPASTERISK=55,
                 KEY_KPSLASH=98, KEY_KPDOT=83, KEY_NUMLOCK=69)
# USB-IF HID Usage Tables, Consumer page 0x0c and Keyboard page 0x07.
# Consumer reports contain one little-endian 16-bit usage, without a report ID.
COMMANDS = {
    "play_pause": ("Play / Pause", "consumer", 0xCD),
    "play": ("Play", "consumer", 0xB0), "pause": ("Pause", "consumer", 0xB1),
    "previous_track": ("Previous track", "consumer", 0xB6),
    "next_track": ("Next track", "consumer", 0xB5),
    "stop": ("Stop playback", "consumer", 0xB7),
    "mute": ("Mute", "consumer", 0xE2),
    "volume_down": ("Volume down", "consumer", 0xEA),
    "volume_up": ("Volume up", "consumer", 0xE9),
    "enter": ("Enter", "keyboard", 0x28), "escape": ("Escape", "keyboard", 0x29),
    "backspace": ("Backspace", "keyboard", 0x2A), "tab": ("Tab", "keyboard", 0x2B),
    "space": ("Space", "keyboard", 0x2C), "delete": ("Delete", "keyboard", 0x4C),
    "home": ("Home", "keyboard", 0x4A), "end": ("End", "keyboard", 0x4D),
    "page_up": ("Page Up", "keyboard", 0x4B), "page_down": ("Page Down", "keyboard", 0x4E),
    "right": ("Right arrow", "keyboard", 0x4F), "left": ("Left arrow", "keyboard", 0x50),
    "down": ("Down arrow", "keyboard", 0x51), "up": ("Up arrow", "keyboard", 0x52),
    **{f"f{i}": (f"F{i}", "keyboard", 0x39 + i) for i in range(1, 13)},
}

MEDIA_COMMANDS = {name: details for name, details in COMMANDS.items() if details[1] == "consumer"}

SHORTCUT_KEYS = {
    **{chr(97 + i): 4 + i for i in range(26)},
    **{str(i): 0x1D + i for i in range(1, 10)}, "0": 0x27,
    **{name: usage for name, (_, kind, usage) in COMMANDS.items() if kind == "keyboard"},
    **{f"f{i}": 0x68 + i - 13 for i in range(13, 25)},
    "minus": 0x2D, "equal": 0x2E, "leftbracket": 0x2F, "rightbracket": 0x30,
    "backslash": 0x31, "nonushash": 0x32, "semicolon": 0x33, "quote": 0x34,
    "backquote": 0x35, "comma": 0x36, "period": 0x37, "slash": 0x38,
    "capslock": 0x39, "printscreen": 0x46, "scrolllock": 0x47, "pause": 0x48,
    "insert": 0x49, "numlock": 0x53, "numpaddivide": 0x54, "numpadmultiply": 0x55,
    "numpadsubtract": 0x56, "numpadadd": 0x57, "numpadenter": 0x58,
    **{f"numpad{i}": 0x58 + i for i in range(1, 10)},
    "numpad0": 0x62, "numpaddecimal": 0x63, "nonusbackslash": 0x64, "menu": 0x65,
    "numpadequal": 0x67,
}
MODIFIERS = {"ctrl": 1, "shift": 2, "alt": 4, "meta": 8,
             "rightctrl": 16, "rightshift": 32, "rightalt": 64, "rightmeta": 128}
ALIASES = {"control": "ctrl", "leftctrl": "ctrl", "leftcontrol": "ctrl", "leftshift": "shift",
           "leftalt": "alt", "option": "alt", "cmd": "meta", "command": "meta", "win": "meta",
           "windows": "meta", "super": "meta", "superkey": "meta", "leftmeta": "meta", "rightcontrol": "rightctrl",
           "altgr": "rightalt", "rightoption": "rightalt", "rightcmd": "rightmeta", "rightwin": "rightmeta",
           "esc": "escape", "return": "enter", "del": "delete", "pgup": "pageup", "pgdn": "pagedown",
           "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
           "-": "minus", "=": "equal", "[": "leftbracket", "]": "rightbracket", "\\": "backslash",
           ";": "semicolon", "'": "quote", "`": "backquote", ",": "comma", ".": "period", "/": "slash"}
# Accept spaced, underscored and hyphenated names such as Page Down / page_down.
SHORTCUT_KEYS = {name.replace("_", ""): code for name, code in SHORTCUT_KEYS.items()}


def shortcut_report(shortcut):
    require(isinstance(shortcut, str) and 0 < len(shortcut) <= 200, "Enter a shortcut such as Ctrl+Shift+S.")
    modifier, usages, seen = 0, [], set()
    for token in shortcut.split("+"):
        token = token.strip().lower()
        token = ALIASES.get(token, token)
        token = re.sub(r"[\s_-]", "", token)
        token = ALIASES.get(token, token)
        require(bool(token), "Separate shortcut keys with +; use Shift+Equal for the + key.")
        if token in MODIFIERS:
            identity = ("modifier", MODIFIERS[token])
            modifier |= MODIFIERS[token]
        else:
            usage = SHORTCUT_KEYS.get(token)
            # Advanced physical key on the USB Keyboard/Keypad usage page.
            if re.fullmatch(r"0x[0-9a-f]{2}", token):
                usage = int(token, 16)
            require(type(usage) is int and 4 <= usage <= 0xDF, f"Unknown shortcut key: {token}. Use a key name or keyboard usage 0x04–0xDF.")
            identity = ("key", usage)
            usages.append(usage)
        require(identity not in seen, f"Duplicate shortcut key: {token}.")
        seen.add(identity)
    require(len(usages) <= 6, "A shortcut supports at most six keys plus modifiers.")
    return bytes([modifier, 0, *usages, *([0] * (6 - len(usages)))])


def command_actions(command):
    """Validate the entire macro before returning (interface, report / seconds)."""
    if isinstance(command, str):
        require(command in COMMANDS, "Choose a supported keyboard command.")
        return [(COMMANDS[command][1], command_report(command))]
    require(isinstance(command, dict) and set(command) == {"label", "steps"}, "Custom commands need a name and a list of steps.")
    require(isinstance(command["label"], str) and len(command["label"]) <= 80 and "\x00" not in command["label"], "Macro name must be at most 80 characters.")
    steps = command["steps"]
    require(isinstance(steps, list) and 1 <= len(steps) <= 100, "Add 1–100 steps to the custom command.")
    actions, total_wait = [], 0
    for index, step in enumerate(steps, 1):
        require(isinstance(step, dict) and len(step) == 1, f"Macro step {index}: choose a shortcut, command, or delay.")
        if "shortcut" in step:
            actions.append(("keyboard", shortcut_report(step["shortcut"])))
        elif "command" in step:
            require(isinstance(step["command"], str) and step["command"] in COMMANDS, f"Macro step {index}: choose a supported command.")
            actions.extend(command_actions(step["command"]))
        else:
            require(set(step) == {"wait"} and number(step["wait"], 0, 60), f"Macro step {index}: delay must be 0–60 seconds.")
            total_wait += step["wait"]
            actions.append(("wait", step["wait"]))
    require(total_wait <= 60, "A macro may contain at most 60 seconds of delays.")
    require(any(kind != "wait" for kind, _ in actions), "Add at least one keyboard shortcut or command.")
    return actions


def serial_command_actions(command):
    """CH9328 Mode 3 only accepts eight-byte keyboard reports, not Consumer Control."""
    actions = command_actions(command)
    for interface, value in actions:
        if interface == "consumer":
            label = next(details[0] for name, details in MEDIA_COMMANDS.items()
                         if command_report(name) == value)
            require(False, f"{label} is a standard USB HID Consumer Control command. "
                    "The CH9328 Mode 3 bridge cannot send Consumer Control reports. "
                    "Use a media-capable USB HID bridge or the Pi Consumer Control gadget. "
                    "No keys were sent; application shortcuts are not substituted.")
    return actions


def is_ir_binding(command):
    return isinstance(command, dict) and command.get("kind") == "ir"


def binding_step(command):
    return dict(command) if is_ir_binding(command) else {"kind": "keyboard", "command": command}


def command_label(command, config=None):
    # Also render incomplete form submissions without masking validation errors.
    if is_ir_binding(command):
        device = command.get("device", "")
        name = (config or {}).get("inventory", {}).get(device, {}).get("name", device)
        return f"IR · {name or 'Choose device'}: {command.get('command') or 'Choose command'}"
    if isinstance(command, dict):
        label = command.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
        steps = command.get("steps", [])
        if isinstance(steps, list) and steps and all(isinstance(step, dict) for step in steps):
            descriptions = [step.get("shortcut") or (COMMANDS.get(step.get("command"), ("",))[0]
                            if isinstance(step.get("command"), str) else "")
                            or (f"Wait {step['wait']}s" if "wait" in step else "") for step in steps]
            if all(isinstance(text, str) and text for text in descriptions):
                return " → ".join(descriptions)
        return "Custom shortcut / macro"
    return COMMANDS.get(command, ("Custom shortcut / macro",))[0] if isinstance(command, str) else "Custom shortcut / macro"


def editable_sequence(command):
    """Expose saved keyboard presets as shortcuts without changing their HID usages.

    Consumer presets retain their meaning and can only be replaced explicitly.
    This also accepts incomplete drafts returned after form validation errors.
    """
    def shortcut(name):
        details = COMMANDS.get(name) if isinstance(name, str) else None
        if details and details[1] == "keyboard":
            return next(key for key, usage in SHORTCUT_KEYS.items() if usage == details[2])

    if isinstance(command, str):
        key = shortcut(command)
        return {"label": "", "steps": [{"shortcut": key}]} if key else command
    if isinstance(command, dict) and isinstance(command.get("steps"), list):
        steps = []
        for step in command["steps"]:
            key = shortcut(step.get("command")) if isinstance(step, dict) and set(step) == {"command"} else None
            steps.append({"shortcut": key} if key else step)
        return {**command, "steps": steps}
    return command


def reserved_keys(config):
    from .numpad_profiles import layout
    keypad = config.get("keypad", {})
    reserved = set(keypad.get("bindings", {}))
    main = str(Path(keypad.get("device", "")).resolve())
    for source in keypad.get("controls", {}).values():
        codes = set()
        if str(Path(source["device"]).resolve()) == main and source["mode"] == "keys":
            codes.update((source["code"], source["down_code"]))
        if str(Path(source.get("press_device", source["device"])).resolve()) == main:
            codes.add(source.get("press_code"))
        custom = layout(config)
        signals = {key["id"]: key.get("code") for key in custom["keys"]} if custom else KEY_CODES
        reserved.update(key for key, code in signals.items() if code is not None and code in codes)
    return reserved


def validate_bindings(bindings, config):
    from .numpad_profiles import labels
    require(isinstance(bindings, dict) and set(bindings) <= set(labels(config, all_profiles=True)), "Choose supported numpad keys for commands.")
    require(not set(bindings) & reserved_keys(config), "Command keys must be unbound: a key is reserved for a task or input control.")
    for command in bindings.values():
        if is_ir_binding(command):
            validate_step(command)
        else:
            command_actions(command)


def command_report(command):
    require(isinstance(command, str) and command in COMMANDS, "Unknown keyboard command.")
    _, kind, usage = COMMANDS[command]
    return usage.to_bytes(2, "little") if kind == "consumer" else bytes([0, 0, usage, 0, 0, 0, 0, 0])


def run_key_command(runner, key, *, live=False, dry_scene=None, expected_scene=None):
    from .numpad_profiles import labels
    require(key in labels(runner.config), "This key belongs to an inactive numpad profile.")
    def run():
        scene = active_scene(runner) if live else dry_scene
        if expected_scene is not None:
            require(scene == expected_scene, "The active task changed. Refresh the virtual numpad before pressing again.")
        task = runner.config.get("scenes", {}).get(scene, {})
        bindings = task.get("key_commands", {})
        validate_bindings(bindings, runner.config)
        require(key in bindings, "This key has no command in the active task.")
        step = binding_step(bindings[key])
        runner.execute_step(step, live=live, scene=scene, key=key)
        runner.emit(f"{'LIVE' if live else 'DRY RUN'} {scene} {labels(runner.config)[key]}: {command_label(bindings[key], runner.config)}")
        return dict(scene=scene, key=key, command=bindings[key], status="commands_sent" if live else "dry_run")

    with trace():
        try:
            if live:
                with exclusive(runner.runtime / "scene.lock"):
                    return run()
            return run()
        except BaseException as exc:
            runner.log.emit("input", "Numpad command failed or rejected", level="error",
                            key=key, live=live, error=error_text(exc))
            raise
