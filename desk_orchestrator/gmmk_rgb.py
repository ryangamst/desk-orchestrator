"""Stock GMMK Numpad lighting-only USB reports. See docs/gmmk-rgb.md.

No profile switching, keymaps, combined LED/control settings or firmware writes.
The per-key overlay command also addresses the side LEDs.
"""
import fcntl
import os
import re
import stat
from pathlib import Path

from .core import DeskError, require
from .gmmk_slider import usb_parent


MODES = {"solid": "Solid", "breathing": "Breathing", "off": "Off",
         "base": "Use onboard effect"}
DEFAULTS = {"profile": 1, "layer": 1, "brightness": 50,
            "keys_mode": "solid", "keys_color": "#b58aff",
            "sides_mode": "solid", "sides_color": "#b58aff"}
# Slot numbers from CORE's GMMK Numpad LED matrix (42 slots, including gaps).
# CORE uses the first occurrence of Numplus (10); slot 22 is its duplicate.
KEY_SLOTS = (1, 2, 3, 4, 7, 8, 9, 10, 19, 20, 21, 25, 26, 27, 28, 38, 39)
SIDE_SLOTS = (0, 6, 12, 18, 24, 30, 36, 5, 11, 17, 23, 29, 35, 41)


def validate(settings):
    require(isinstance(settings, dict) and set(settings) == set(DEFAULTS), "Invalid RGB settings.")
    for name, low, high in (("profile", 1, 3), ("layer", 1, 3), ("brightness", 0, 100)):
        value = settings[name]
        require(type(value) is int and low <= value <= high, f"RGB {name} must be {low}–{high}.")
    require(settings["brightness"] % 5 == 0, "RGB brightness must use 5% increments.")
    for zone in ("keys", "sides"):
        require(isinstance(settings[zone + "_mode"], str) and settings[zone + "_mode"] in MODES,
                "Choose a supported RGB mode.")
        color = settings[zone + "_color"]
        require(isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color),
                "RGB colors must use six hexadecimal digits, such as #b58aff.")
    return settings


def from_form(form):
    values = {name: form.get("rgb_" + name, "") for name in DEFAULTS}
    try:
        for name in ("profile", "layer", "brightness"):
            values[name] = int(values[name])
    except (TypeError, ValueError):
        raise DeskError("RGB profile, layer and brightness must be whole numbers.") from None
    return validate(values)


def feature_length(descriptor):
    # Only the stock vendor feature collection, report 7, byte-sized fields.
    # Native CORE allocates 264 bytes; hidraw uses the descriptor's actual size.
    prefix = bytes.fromhex("06 01 ff 09 01 a1 01 85 07 15 00 26 ff 00 09 20 75 08 96")
    if len(descriptor) == 24 and descriptor.startswith(prefix) and descriptor[21:] == b"\xb1\x02\xc0":
        size = int.from_bytes(descriptor[19:21], "little") + 1
        if size in (256, 264):
            return size
    raise OSError("Unrecognized GMMK lighting report descriptor; no RGB command was sent")


def find_device(input_path, sysfs=Path("/sys"), dev=Path("/dev")):
    event = Path(input_path).resolve(strict=True).name
    if not re.fullmatch(r"event[0-9]+", event):
        raise OSError("Select the GMMK Numpad keyboard input before using RGB")
    usb = usb_parent((sysfs / "class/input" / event / "device").resolve(strict=True))
    ids = tuple((usb / field).read_text().strip().lower() for field in ("idVendor", "idProduct"))
    if ids != ("320f", "5088"):
        raise OSError("RGB requires the stock GMMK Numpad (320f:5088) connected by USB")
    matches = []
    for node in (sysfs / "class/hidraw").glob("hidraw*"):
        try:
            target = (node / "device").resolve(strict=True)
            if usb_parent(target) != usb:
                continue
            interface = next(p for p in (target, *target.parents) if (p / "bInterfaceNumber").is_file())
            if (interface / "bInterfaceNumber").read_text().strip() != "02":
                continue
            size = feature_length((target / "report_descriptor").read_bytes())
            matches.append((str(dev / node.name), size))
        except (OSError, StopIteration):
            continue
    if len(matches) != 1:
        raise OSError("Expected one stock GMMK Numpad lighting interface 02; check USB connection and firmware")
    return matches[0]


def status(input_path):
    """Inspect sysfs and permissions only; never open or write the device."""
    try:
        path, _ = find_device(input_path)
        if not os.access(path, os.R_OK | os.W_OK):
            return {"available": False, "message": "RGB access needed. Run the updated Pi setup/update and reconnect the numpad."}
        return {"available": True, "message": "Stock USB lighting interface available"}
    except FileNotFoundError:
        return {"available": False, "message": "The selected numpad is not connected. Select its USB keyboard input above, then refresh."}
    except OSError as exc:
        return {"available": False, "message": str(exc)}


def lighting_report(settings, length=264):
    validate(settings)
    require(length in (256, 264), "Unsupported RGB report length.")
    report = bytearray(length)
    report[:8] = bytes((7, 6, settings["profile"], settings["layer"], 42, 0,
                        settings["brightness"] // 5, 0))
    for zone, slots in (("keys", KEY_SLOTS), ("sides", SIDE_SLOTS)):
        mode = settings[zone + "_mode"]
        rgb = bytes.fromhex(settings[zone + "_color"][1:]) if mode != "off" else bytes(3)
        visibility = {"solid": 255, "breathing": 254, "off": 255, "base": 0}[mode]
        for slot in slots:
            offset = 8 + slot * 5
            report[offset:offset + 5] = bytes((visibility, slot)) + rgb
    return report


def apply(input_path, settings):
    validate(settings)
    try:
        path, size = find_device(input_path)
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            require(stat.S_ISCHR(os.fstat(fd).st_mode), "RGB target is not a HID device.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Recheck identity after opening, including the opened node's rdev,
            # so unplug/replug cannot redirect a stale hidraw number.
            require(find_device(input_path) == (path, size)
                    and os.fstat(fd).st_rdev == os.stat(path).st_rdev,
                    "Numpad connection changed. Refresh before applying RGB.")
            report = lighting_report(settings, size)
            # HIDIOCSFEATURE: _IOC(READ|WRITE, 'H', 0x06, report length).
            sent = fcntl.ioctl(fd, (3 << 30) | (size << 16) | (ord("H") << 8) | 6, report, True)
            require(sent == size, "RGB report was incomplete; inspect the numpad before trying again.")
        finally:
            os.close(fd)
    except PermissionError:
        raise DeskError("RGB permission denied. Run the updated Pi setup/update and reconnect the numpad.") from None
    except OSError as exc:
        raise DeskError(f"RGB could not complete: {exc}. No automatic retry was made.") from exc
