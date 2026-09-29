"""Device layouts and stable virtual key identities, with an implicit GMMK profile."""
import copy
import math
import re

from .core import require

CONNECTION_FIELDS = ("device", "identity", "grab", "controls", "lighting")


def active_id(config):
    return config.get("keypad", {}).get("active_profile", "gmmk")


def layout(config):
    return config.get("keypad", {}).get("profiles", {}).get(active_id(config)) if active_id(config) != "gmmk" else None


def labels(config, *, all_profiles=False):
    from .key_commands import KEYS
    result = dict(KEYS) if all_profiles or active_id(config) == "gmmk" else {}
    profiles = config.get("keypad", {}).get("profiles", {})
    for name, profile in profiles.items():
        if all_profiles or name == active_id(config):
            result.update((key["id"], key["label"]) for key in profile["keys"])
    return result


def signal_keys(config, code, names):
    custom = layout(config)
    if custom is None:
        return names
    return [key["id"] for key in custom["keys"] if key.get("code") == code]


def geometry_signature(config):
    import hashlib
    import json
    return hashlib.sha256(json.dumps([active_id(config), layout(config)], sort_keys=True).encode()).hexdigest()[:16]


def validate_profile(profile):
    require(isinstance(profile, dict) and {"name", "width", "height", "keys"} <= set(profile) <= {"name", "width", "height", "keys", "controls"}, "Invalid layout fields.")
    require(isinstance(profile["name"], str) and 0 < len(profile["name"].strip()) <= 80, "Name the numpad (80 characters maximum).")
    def measure(value, low, high):
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high and value * 4 == round(value * 4)
    require(measure(profile["width"], 1, 32) and measure(profile["height"], 1, 16), "Container dimensions must be 1-32 by 1-16 key units, in quarter units.")
    require(isinstance(profile["keys"], list) and len(profile["keys"]) <= 200, "Maximum 200 keys per layout.")
    ids, codes, placed = set(), set(), []
    for key in profile["keys"]:
        require(isinstance(key, dict) and set(key) == {"id", "label", "x", "y", "w", "h", "code"}, "Invalid key fields.")
        require(isinstance(key["id"], str) and re.fullmatch(r"KEY_CUSTOM_[a-f0-9]{32}", key["id"]), "Invalid virtual key ID.")
        require(key["id"] not in ids, "Each key needs a unique ID.")
        ids.add(key["id"])
        require(isinstance(key["label"], str) and 0 < len(key["label"].strip()) <= 40 and "\x00" not in key["label"], "Key labels must contain 1-40 characters.")
        require(all(measure(key[k], 0 if k in ("x", "y") else .25, 32) for k in ("x", "y", "w", "h")), "Invalid key geometry.")
        require(key["x"] + key["w"] <= profile["width"] and key["y"] + key["h"] <= profile["height"], "Keys must fit inside the container.")
        require(not any(key["x"] < p["x"] + p["w"] and p["x"] < key["x"] + key["w"] and
                        key["y"] < p["y"] + p["h"] and p["y"] < key["y"] + key["h"] for p in placed), "Keys cannot overlap.")
        placed.append(key)
        code = key["code"]
        require(code is None or type(code) is int and 1 <= code <= 767, "Invalid hardware key code.")
        require(code is None or code not in codes, "Two virtual keys cannot use the same hardware signal.")
        codes.add(code)
    controls = profile.get("controls", [])
    require(isinstance(controls, list) and len(controls) <= 2, "A layout supports one rotary dial and one slider.")
    control_ids = set()
    for control in controls:
        require(isinstance(control, dict) and {"id", "label", "x", "y", "w", "h"} <= set(control)
                <= {"id", "label", "x", "y", "w", "h", "orientation"}, "Invalid layout control fields.")
        require(control["id"] in ("dial", "slider") and control["id"] not in control_ids, "A layout supports one rotary dial and one slider.")
        control_ids.add(control["id"])
        require(isinstance(control["label"], str) and 0 < len(control["label"].strip()) <= 40 and "\x00" not in control["label"], "Control labels must contain 1-40 characters.")
        require(all(measure(control[k], 0 if k in ("x", "y") else 1, 32) for k in ("x", "y", "w", "h")), "Controls need at least one key unit of width and height.")
        require(control["x"] + control["w"] <= profile["width"] and control["y"] + control["h"] <= profile["height"], "Controls must fit inside the container.")
        require(control.get("orientation", "vertical") in ("horizontal", "vertical"), "Invalid slider orientation.")
        require(control["id"] != "dial" or control["w"] == control["h"], "Rotary dial width and height must match.")
        require(not any(control["x"] < p["x"] + p["w"] and p["x"] < control["x"] + control["w"] and
                        control["y"] < p["y"] + p["h"] and p["y"] < control["y"] + control["h"] for p in placed), "Controls and keys cannot overlap.")
        placed.append(control)


def validate(config):
    keypad = config.get("keypad", {})
    profiles = keypad.get("profiles", {})
    require(isinstance(profiles, dict) and len(profiles) <= 20, "Maximum 20 custom profiles.")
    seen = set()
    for name, profile in profiles.items():
        require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name) and name != "gmmk", "Invalid profile ID.")
        validate_profile(profile)
        ids = {key["id"] for key in profile["keys"]}
        require(not seen & ids, "Key IDs must be unique across profiles.")
        seen.update(ids)
    require(active_id(config) == "gmmk" or active_id(config) in profiles, "Unknown active numpad profile.")
    from .controls import validate_sources
    from .usb_devices import valid_input_path
    connections = keypad.get("profile_connections", {})
    require(isinstance(connections, dict) and set(connections) <= {"gmmk", *profiles}, "Invalid profile connections.")
    for connection in connections.values():
        require(isinstance(connection, dict) and set(connection) <= set(CONNECTION_FIELDS), "Invalid profile connection.")
        require(not connection.get("device") or valid_input_path(connection["device"]), "Invalid profile input path.")
        require(type(connection.get("grab", True)) is bool, "Invalid exclusive-access setting.")
        validate_sources(connection.get("controls", {}))
        if "lighting" in connection:
            from .gmmk_rgb import validate as validate_lighting
            validate_lighting(connection["lighting"])
    for name, profile in profiles.items():
        connection = keypad if active_id(config) == name else connections.get(name, {})
        from pathlib import Path
        main = str(Path(connection.get("device", "")).resolve())
        reserved = set()
        for source in connection.get("controls", {}).values():
            if source["mode"] == "keys" and str(Path(source["device"]).resolve()) == main:
                reserved.update((source["code"], source["down_code"]))
            if str(Path(source.get("press_device", source["device"])).resolve()) == main:
                reserved.add(source.get("press_code"))
        require(not any(key["code"] is not None and key["code"] in reserved for key in profile["keys"]),
                "A layout key conflicts with a dial or slider input on the same device. Clear one mapping first.")


def save(config, name, profile, *, remove_assignments=False):
    validate_profile(profile)
    keypad = config["keypad"]
    profiles = keypad.setdefault("profiles", {})
    old_ids = {key["id"] for key in profiles.get(name, {}).get("keys", [])}
    removed = old_ids - {key["id"] for key in profile["keys"]}
    used = removed & set(keypad["bindings"])
    for task in config["scenes"].values():
        used.update(removed & set(task.get("key_commands", {})))
    require(not used or remove_assignments, "Deleted keys have task assignments. Confirm removal of their assignments before saving.")
    for key in removed:
        keypad["bindings"].pop(key, None)
        for task in config["scenes"].values():
            task.get("key_commands", {}).pop(key, None)
    profiles[name] = copy.deepcopy(profile)


def activate(config, name):
    keypad = config["keypad"]
    require(name == "gmmk" or name in keypad.get("profiles", {}), "Unknown numpad profile.")
    if active_id(config) == name:
        return
    connections = keypad.setdefault("profile_connections", {})
    connections[active_id(config)] = {field: copy.deepcopy(keypad[field]) for field in CONNECTION_FIELDS if field in keypad}
    for field in CONNECTION_FIELDS:
        keypad.pop(field, None)
    keypad.update(copy.deepcopy(connections.get(name, {"device": "", "grab": True})))
    keypad.setdefault("device", "")
    keypad["active_profile"] = name


def restore(config, backup):
    """Import portable profiles and mappings, retaining this host's connections."""
    keypad, incoming = config["keypad"], backup["keypad"]
    profiles = copy.deepcopy(incoming.get("profiles", {}))
    current = active_id(config)
    # Keep the active local layout if an older backup has no matching profile.
    if current != "gmmk" and current not in profiles:
        profiles[current] = copy.deepcopy(keypad["profiles"][current])
    keypad["profiles"] = profiles
    keypad["profile_connections"] = {name: value for name, value in keypad.get("profile_connections", {}).items()
                                     if name == "gmmk" or name in profiles}
