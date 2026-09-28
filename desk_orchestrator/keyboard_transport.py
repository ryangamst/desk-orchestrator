"""CH9328 Mode 3 serial output and read-only USB serial discovery."""
from contextlib import contextmanager
import os
from pathlib import Path

from .core import DeskError, number, require


def validate_settings(config):
    require(isinstance(config, dict), "KVM settings must be an object.")
    require(set(config) <= {"transport", "device", "consumer_device", "confirmed", "key_delay", "baudrate"},
            "Invalid KVM settings.")
    transport = config.get("transport", "gadget")
    require(transport in ("gadget", "ch9328"), "Choose USB gadget or CH9328 serial output.")
    path = config.get("device", "/dev/hidg0" if transport == "gadget" else "")
    require(isinstance(path, str) and path.startswith("/dev/") and len(path) <= 240 and "\0" not in path,
            "Keyboard device path must start with /dev/.")
    if transport == "ch9328":
        require(type(config.get("baudrate", 9600)) is int and config.get("baudrate", 9600) == 9600,
                "CH9328 Mode 3 uses 9600 baud.")
    if "consumer_device" in config:
        consumer = config["consumer_device"]
        require(isinstance(consumer, str) and consumer.startswith("/dev/") and len(consumer) <= 240 and "\0" not in consumer,
                "Media HID path must start with /dev/.")
        require(consumer != path, "Keyboard and media HID devices must be different.")
    require(type(config.get("confirmed", False)) is bool and number(config.get("key_delay", .08), .02, .5),
            "Invalid KVM confirmation or timing.")


def serial_module():
    try:
        import serial
    except ImportError as exc:
        raise DeskError("CH9328 requires pyserial. Install the project's serial extra or update the Pi installation.") from exc
    return serial


@contextmanager
def serial_connection(config):
    validate_settings(config)
    serial = serial_module()
    try:
        with serial.Serial(config["device"], baudrate=9600, bytesize=8, parity="N", stopbits=1,
                           timeout=1, write_timeout=2, xonxoff=False, rtscts=False,
                           dsrdtr=False, exclusive=True) as connection:
            yield connection
    except (serial.SerialException, OSError) as exc:
        raise DeskError(f"CH9328 serial output failed on {config['device']}: {exc}. "
                        "Check the adapter, cable, and service serial permissions. Command outcome may be partial.") from exc


def write_serial_report(connection, report):
    require(len(report) == 8, "CH9328 Mode 3 accepts only eight-byte keyboard reports; media commands are unsupported.")
    require(connection.write(report) == len(report), "Incomplete CH9328 keyboard report; command outcome is unknown.")


def serial_devices(root=Path("/dev")):
    """Never open a port. Prefer by-id, then by-path, then an ephemeral node."""
    aliases = {}
    for directory in ("serial/by-id", "serial/by-path"):
        for path in sorted((root / directory).glob("*")):
            if path.exists():
                aliases.setdefault(path.resolve(), path)
    devices = []
    for path in sorted([*root.glob("ttyUSB*"), *root.glob("ttyACM*")]):
        preferred = aliases.get(path.resolve(), path)
        devices.append(dict(path=str(preferred), node=str(path), stable=preferred != path,
                            writable=os.access(path, os.R_OK | os.W_OK)))
    return devices
