"""Read USB inventory and input capabilities without opening or grabbing devices."""
import os
import platform
import re
from pathlib import Path

from .core import require

# Linux input-event-codes.h: KP1, KP2, KP3, KPENTER, KPPLUS.
NUMPAD_KEYS = (79, 80, 81, 96, 78)


def read_text(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return ""


def bitmap(value, word_bits=None):
    # sysfs prints native *kernel* unsigned-long words, most significant first.
    # uname reports the kernel architecture even with a 32-bit Python userspace.
    if word_bits is None:
        word_bits = 64 if "64" in platform.machine() else 32
    try:
        return sum(int(word, 16) << (index * word_bits) for index, word in enumerate(reversed(value.split())))
    except ValueError:
        return 0


def valid_input_path(value):
    return isinstance(value, str) and len(value) <= 512 and bool(re.fullmatch(
        r"/dev/input/(?:event[0-9]+|by-(?:id|path)/[^/\x00]+)", value)) and Path(value).name not in (".", "..")


def scan_usb(sys_root=Path("/sys"), input_root=Path("/dev/input")):
    sys_root, input_root = Path(sys_root), Path(input_root)
    warnings, devices, usb_paths = [], [], {}
    try:
        entries = sorted((sys_root / "bus/usb/devices").iterdir())
    except OSError:
        entries = []
        warnings.append("USB discovery is unavailable. The web service needs access to Linux /sys/bus/usb/devices.")
    for path in entries:
        vendor, product_id = read_text(path / "idVendor"), read_text(path / "idProduct")
        # Interfaces have no idVendor; root hubs are not plugged-in peripherals.
        if not vendor or not product_id or path.name.startswith("usb"):
            continue
        device = {"port": path.name, "name": read_text(path / "product") or f"USB device {vendor}:{product_id}",
                  "manufacturer": read_text(path / "manufacturer"), "vendor_id": vendor,
                  "product_id": product_id, "serial": read_text(path / "serial"), "inputs": []}
        devices.append(device)
        usb_paths[path.resolve()] = device

    aliases = {}
    for folder in ("by-id", "by-path"):
        try:
            links = sorted((input_root / folder).iterdir())
        except OSError:
            continue
        for link in links:
            if link.is_symlink() and link.exists():
                aliases.setdefault(link.resolve(), []).append(str(link))
    try:
        events = sorted((sys_root / "class/input").iterdir())
    except OSError:
        events = []
        warnings.append("Input discovery is unavailable. Check access to /sys/class/input.")
    candidates, keyboards = [], []
    for event in events:
        if not re.fullmatch(r"event[0-9]+", event.name):
            continue
        source = (event / "device").resolve()
        owner = next((usb_paths[parent] for parent in (source, *source.parents) if parent in usb_paths), None)
        identity = owner or {"name": read_text(source / "name") or event.name,
                             "vendor_id": read_text(source / "id/vendor"), "product_id": read_text(source / "id/product"),
                             "serial": read_text(source / "uniq"), "port": "non-USB"}
        path = input_root / event.name
        key_mask = bitmap(read_text(source / "capabilities/key"))
        event_mask = bitmap(read_text(source / "capabilities/ev"))
        capable = bool(event_mask & (1 << 1)) and all(key_mask & (1 << key) for key in NUMPAD_KEYS)
        stable_paths = aliases.get(path.resolve(), [])
        choice = {"name": read_text(source / "name") or identity["name"], "path": str(path),
                  "selection_path": stable_paths[0] if stable_paths else str(path), "aliases": stable_paths,
                  "numpad_capable": capable, "readable": path.exists() and os.access(path, os.R_OK),
                  "key_capable": bool(event_mask & (1 << 1) and key_mask),
                  "vendor_id": identity["vendor_id"], "product_id": identity["product_id"],
                  "serial": identity["serial"], "port": identity["port"]}
        rel_mask = bitmap(read_text(source / "capabilities/rel"))
        abs_mask = bitmap(read_text(source / "capabilities/abs"))
        choice["control_signals"] = [label for mask, code, label in (
            (key_mask, 115, "KEY_VOLUMEUP (115)"), (key_mask, 114, "KEY_VOLUMEDOWN (114)"),
            (key_mask, 113, "KEY_MUTE (113)"), (rel_mask, 8, "REL_WHEEL (8)"),
            (rel_mask, 7, "REL_DIAL (7)"), (abs_mask, 32, "ABS_VOLUME (32)")) if mask & (1 << code)]
        if choice["key_capable"]:
            keyboards.append(choice)
        if owner is not None:
            owner["inputs"].append(choice)
        if capable and owner is not None:
            candidates.append(choice)
    return {"host": platform.node(), "devices": devices, "candidates": candidates, "keyboards": keyboards, "warnings": warnings}


def match_input(snapshot, path):
    entries = snapshot.get("keyboards", []) + [entry for device in snapshot["devices"] for entry in device["inputs"]]
    return next((entry for entry in entries
                 if path in (entry["path"], entry["selection_path"], *entry["aliases"])), None)


def keyboard_inputs(snapshot):
    return snapshot.get("keyboards", [item for device in snapshot["devices"] for item in device["inputs"]
                                      if item.get("key_capable", item.get("numpad_capable"))])


def selected_input(keypad, snapshot):
    path = keypad.get("device", "")
    current = match_input(snapshot, path)
    identity = keypad.get("identity", {})
    name = current["name"] if current else identity.get("name", "No USB numpad selected")
    if current and identity:
        # An unstable event number must not make a different peripheral look selected.
        if any(identity.get(key) and identity[key] != current.get(key) for key in ("vendor_id", "product_id", "serial")):
            return {"name": identity.get("name", name), "path": path, "status": "Device identity changed"}
    status = ("Connected" if current["readable"] else "Connected · input access needed") if current else "Not detected"
    return {"name": name, "path": path, "status": status}


def save_selection(config, choice, manual_path, grab, snapshot):
    if choice == "manual":
        require(valid_input_path(manual_path), "Use /dev/input/eventN or a /dev/input/by-id/ or by-path/ device path.")
        selected = match_input(snapshot, manual_path)
        path = manual_path
    else:
        selected = next((item for item in snapshot["candidates"] if item["selection_path"] == choice), None)
        require(selected is not None, "That USB input is no longer available. Refresh the device list and choose again.")
        path = selected["selection_path"]
    keypad = config.setdefault("keypad", {})
    keypad.update(device=path, grab=grab)
    for source in keypad.get("controls", {}).values():
        if source["mode"] == "gmmk_raw":
            source["device"] = path
    if selected:
        keypad["identity"] = {key: selected[key] for key in ("name", "vendor_id", "product_id", "serial", "port")}
    else:
        keypad.pop("identity", None)
