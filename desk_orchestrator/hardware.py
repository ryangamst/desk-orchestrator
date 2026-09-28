from __future__ import annotations

import os
import re
import select
import shutil
import stat
import subprocess
import time
from pathlib import Path

from .core import DeskError, number, require
from .smartthings import SmartThings
from .diagnostics import EventLog
from .key_commands import command_actions, serial_command_actions
from .keyboard_transport import validate_settings, serial_connection, serial_module, write_serial_report

NAME = re.compile(r"[A-Za-z0-9_.+-]+\Z")
RELEASE = bytes(8)


def kvm_reports(port):
    """Two separate left-control taps, then a TOP ROW number, all released."""
    ctrl = bytes([1, 0, 0, 0, 0, 0, 0, 0])
    digit = bytes([0, 0, 0x1D + port, 0, 0, 0, 0, 0])
    return [ctrl, RELEASE, ctrl, RELEASE, digit, RELEASE]


class Hardware:
    def __init__(self, config):
        self.config = config
        self.smartthings = SmartThings(config.get("smartthings", {}))
        self.log = EventLog(config.get("runtime", {}).get("directory", ".runtime"))

    def ir_device(self, name):
        require(name in self.config.get("ir", {}).get("devices", {}), f"Unknown IR device: {name}")
        return self.config["ir"]["devices"][name]

    def macro(self, step):
        device = self.ir_device(step["device"])
        if step["kind"] == "volume":
            presets = device.get("volume_presets", [])
            matches = [p for p in presets if p.get("db") == step["db"]]
            require(len(matches) == 1, f"{step['device']}: no unique volume preset for {step['db']} dB")
            preset = matches[0]
            return device, preset
        command = step["command"]
        require(command in device.get("commands", {}), f"{step['device']}: configure IR command {command}")
        macro = device["commands"][command]
        return device, macro

    def irsend(self, *args):
        ir = self.config.get("ir", {})
        argv = [ir.get("executable", "irsend"), "--device", ir.get("socket", "/run/lirc/lircd"), *args]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise DeskError("LIRC timed out; command outcome is unknown.") from exc
        if result.returncode != 0:
            detail = (result.stderr + "\n" + result.stdout).strip()[-2000:]
            operation = args[0] if args else "request"
            socket = ir.get("socket", "/run/lirc/lircd")
            raise DeskError(f"LIRC {operation} failed on {socket} (exit {result.returncode}): "
                            + (detail or "irsend returned no diagnostic output. Check lircd's journal."))
        return result.stdout + result.stderr

    def check(self, step, *, probe=False):
        kind = step["kind"]
        if kind == "wait":
            return
        if kind in ("ir", "volume"):
            device, macro = self.macro(step)
            if kind == "volume":
                require(device.get("allow_approximate_volume") is True,
                        f"{step['device']}: exact dB cannot be guaranteed by IR; calibrated approximation is not enabled")
                require(macro.get("calibrated") is True and macro.get("starts_from_known_reference") is True,
                        "Volume preset must be calibrated and establish its own reference every run.")
            else:
                require(macro.get("verified") is True, f"{step['device']}.{step['command']}: IR command is not verified")
                if step["command"] in ("power_on", "power_off"):
                    require(macro.get("discrete") is True, f"{step['device']}.{step['command']}: a toggle cannot ensure power state")
            remote = device.get("remote", "")
            sequence = macro.get("sequence", [])
            require(isinstance(sequence, list) and sequence, "IR macro sequence is empty.")
            total = 0
            duration = 0
            for pulse in sequence:
                require(isinstance(pulse, dict) and set(pulse) <= {"key", "count", "gap", "remote"}, "Invalid IR pulse fields.")
                pulse_remote = pulse.get("remote", remote)
                require(isinstance(pulse_remote, str) and NAME.fullmatch(pulse_remote), "Invalid LIRC remote name.")
                key, count, gap = pulse.get("key", ""), pulse.get("count", 1), pulse.get("gap", 0.15)
                require(isinstance(key, str) and NAME.fullmatch(key), "Invalid LIRC key name.")
                require(type(count) is int and 1 <= count <= 300, "IR count must be 1–300.")
                require(number(gap, 0.02, 5), "IR gap must be 0.02–5 seconds.")
                total += count
                duration += count * gap
            require(total <= 600, "IR macro exceeds 600 transmissions.")
            require(duration <= 120, "IR macro exceeds 120 seconds of delays.")
            if probe:
                require(shutil.which(self.config["ir"].get("executable", "irsend")), "Install irsend (LIRC).")
                listings = {}
                for pulse in sequence:
                    destination = pulse.get("remote", remote)
                    if destination not in listings:
                        listing = self.irsend("LIST", destination, "")
                        listings[destination] = {line.split()[-1] for line in listing.splitlines() if line.split()}
                    require(pulse["key"] in listings[destination], f"LIRC has no {destination}/{pulse['key']}")
        elif kind in ("kvm", "keyboard"):
            kvm = self.config.get("kvm", {})
            validate_settings(kvm)
            require(kvm.get("confirmed") is True, "Confirm USB keyboard wiring and double-left-control KVM hotkey.")
            require(number(kvm.get("key_delay", 0.08), 0.02, 0.5), "KVM key_delay must be 0.02–0.5 seconds.")
            paths = {kvm.get("device", "/dev/hidg0")}
            if kvm.get("transport", "gadget") == "ch9328":
                if kind == "keyboard":
                    serial_command_actions(step["command"])
                if probe:
                    serial_module()
                    path = Path(kvm["device"])
                    require(path.exists() and stat.S_ISCHR(path.stat().st_mode) and os.access(path, os.R_OK | os.W_OK),
                            f"CH9328 serial adapter is not accessible: {path}. Check the cable and desk service's dialout group.")
                    return "CH9328 serial adapter is accessible. No port was opened or keypress sent; KVM state is unverified."
                return
            if kind == "keyboard":
                interfaces = {interface for interface, _ in command_actions(step["command"]) if interface != "wait"}
                paths = {self.command_device(interface) for interface in interfaces}
                if "consumer" in interfaces:
                    require(self.command_device("consumer") != self.command_device("keyboard"), "Media commands need a separate consumer HID interface.")
            if probe:
                for path in sorted(paths):
                    path = Path(path)
                    require(path.exists() and stat.S_ISCHR(path.stat().st_mode) and os.access(path, os.W_OK),
                            f"USB HID gadget is not writable: {path}. Update the gadget setup and check its permissions.")
        elif kind == "monitor":
            monitor = self.config.get("monitors", {}).get(step["device"], {})
            require(monitor.get("confirmed") is True, f"{step['device']}: discover and confirm SmartThings input capability")
            for field in ("device_id", "component", "capability", "command", "attribute"):
                require(isinstance(monitor.get(field), str) and monitor[field], f"Monitor missing {field}")
            require(step["input"] in monitor.get("inputs", {}), f"Monitor has no input mapping: {step['input']}")
            require(number(monitor.get("verify_timeout", 15), 1, 60), "Monitor verify_timeout must be 1–60 seconds.")
            if probe:
                self.smartthings.tokens.available()
                status = self.smartthings.status(monitor["device_id"])
                attrs = status.get("components", {}).get(monitor["component"], {}).get(monitor["capability"], {})
                require(monitor["attribute"] in attrs, "Monitor input feedback attribute is unavailable.")
                feedback = attrs[monitor["attribute"]]
                value = feedback.get("value") if isinstance(feedback, dict) else None
                require(isinstance(value, str) and value, "SmartThings returned no current monitor input.")
                return f"SmartThings API responded. Current input: {value}. No input switch was requested."

    def execute(self, step):
        kind = step["kind"]
        if kind == "wait":
            time.sleep(step["seconds"])
        elif kind in ("ir", "volume"):
            device, macro = self.macro(step)
            for pulse in macro["sequence"]:
                for _ in range(pulse.get("count", 1)):
                    # Bounded single presses: never leave a repeating SEND_START active.
                    remote = pulse.get("remote", device.get("remote", ""))
                    self.irsend("SEND_ONCE", remote, pulse["key"])
                    self.log.emit("transport", "LIRC accepted SEND_ONCE; physical response unverified",
                                  remote=remote, key=pulse["key"], live=True)
                    time.sleep(pulse.get("gap", 0.15))
        elif kind == "monitor":
            monitor = self.config["monitors"][step["device"]]
            self.smartthings.set_input(monitor, monitor["inputs"][step["input"]])
        elif kind == "kvm":
            self.switch_kvm(step["port"])
        elif kind == "keyboard":
            self.send_key_command(step["command"])

    def command_device(self, interface):
        config = self.config["kvm"]
        return config.get("consumer_device", "/dev/hidg1") if interface == "consumer" else config.get("device", "/dev/hidg0")

    def send_key_command(self, command):
        validate_settings(self.config["kvm"])
        if self.config["kvm"].get("transport", "gadget") == "ch9328":
            actions = serial_command_actions(command)
            self.send_serial_actions(actions)
            return
        actions = command_actions(command)
        for index, (interface, value) in enumerate(actions):
            if interface == "wait":
                time.sleep(value)
                continue
            self.send_key_report(self.command_device(interface), value)
            # Give the host time to observe release, including repeated same-key steps.
            if index + 1 < len(actions):
                time.sleep(self.config["kvm"].get("key_delay", .08))

    def send_key_report(self, path, report):
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        try:
            self.write_report(fd, report)
            self.log.emit("transport", "USB keyboard command written", path=path, report=report.hex(), live=True)
            time.sleep(self.config["kvm"].get("key_delay", .08))
        finally:
            try:
                self.write_report(fd, bytes(len(report)))
            finally:
                os.close(fd)

    def switch_kvm(self, port):
        config = self.config["kvm"]
        validate_settings(config)
        require(type(port) is int and 1 <= port <= 4, "KVM port must be 1–4.")
        if config.get("transport", "gadget") == "ch9328":
            self.send_serial_actions([("keyboard", report) for report in kvm_reports(port) if report != RELEASE])
            return
        fd = os.open(config.get("device", "/dev/hidg0"), os.O_WRONLY | os.O_NONBLOCK)
        try:
            for report in kvm_reports(port):
                self.write_report(fd, report)
                self.log.emit("transport", "USB HID report written", path=config.get("device", "/dev/hidg0"),
                              report=report.hex(), port=port, live=True)
                time.sleep(config.get("key_delay", 0.08))
        finally:
            try:
                self.write_report(fd, RELEASE)
                self.log.emit("transport", "USB HID keys released", path=config.get("device", "/dev/hidg0"), live=True)
            finally:
                os.close(fd)

    def send_serial_actions(self, actions):
        config = self.config["kvm"]
        delay = config.get("key_delay", .08)
        # One exclusive port session for the entire macro. Runner's scene lock also
        # serializes web, CLI, and physical numpad requests across processes.
        with serial_connection(config) as connection:
            def report(value):
                write_serial_report(connection, value)
                self.log.emit("transport", "CH9328 keyboard report sent; physical response unverified",
                              transport="ch9328", path=config["device"], report=value.hex(), live=True)
                time.sleep(delay)

            try:
                report(RELEASE)
                for interface, value in actions:
                    if interface == "wait":
                        time.sleep(value)
                    else:
                        report(value)
                        report(RELEASE)
            finally:
                # Best effort even on disconnect/timeout/interruption; never retry
                # a key press or replay a partially completed sequence.
                report(RELEASE)

    @staticmethod
    def write_report(fd, report):
        deadline = time.monotonic() + 2
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0 and select.select([], [fd], [], max(0, remaining))[1],
                    "USB HID write timed out; check the KVM cable and gadget connection.")
            try:
                require(os.write(fd, report) == len(report), "Incomplete USB HID report.")
                return
            except BlockingIOError:
                continue
