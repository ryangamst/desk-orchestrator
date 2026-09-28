"""Bounded raw IR capture and LIRC catalog access for the hardware editor."""
from __future__ import annotations

import os
import select
import stat
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .core import DeskError, exclusive, require
from .hardware import Hardware, NAME
from .config_store import identifier


def lirc_name(value):
    require(isinstance(value, str) and 0 < len(value) <= 240 and NAME.fullmatch(value),
            "Use a LIRC name containing only letters, numbers, dots, underscores, + or -.")
    return value


def catalog(config, remote=""):
    if remote:
        lirc_name(remote)
    try:
        output = Hardware(config).irsend("LIST", remote, "")
    except OSError as exc:
        raise DeskError("Cannot run irsend. Install LIRC and check the socket in Settings.") from exc
    # irsend prints '<hex> <key>' for keys and bare names for remotes.
    return sorted({line.split()[-1] for line in output.splitlines()
                   if line.split() and NAME.fullmatch(line.split()[-1])})


class Frame:
    """A single pulse/space frame, excluding its surrounding silence."""
    def __init__(self):
        self.timings = []

    def feed(self, line):
        fields = line.split()
        if len(fields) != 2 or fields[0] not in ("pulse", "space", "timeout", "overflow"):
            return False
        kind, raw = fields
        require(kind != "overflow", "Receiver overflowed. Move the remote farther away and retry.")
        try:
            duration = int(raw)
        except ValueError:
            raise DeskError("Receiver returned invalid timing data.") from None
        require(0 < duration <= 16777215, "Receiver returned invalid timing data.")
        if kind == "timeout" or (kind == "space" and duration >= 20000):
            return bool(self.timings)
        if not self.timings and kind == "space":
            return False
        require(kind == ("pulse" if len(self.timings) % 2 == 0 else "space"),
                "Incomplete IR signal. Retry with a brief button press.")
        self.timings.append(duration)
        require(len(self.timings) <= 1023 and sum(self.timings) <= 500000,
                "IR signal is too long for single-button learning.")
        return False

    def finish(self):
        # A receiver may report silence as the last space before going idle.
        timings = self.timings if len(self.timings) % 2 else self.timings[:-1]
        require(len(timings) >= 5, "No complete IR button received. Aim at the receiver and try again.")
        return timings


def receiver_path(value):
    require(isinstance(value, str) and value.startswith("/dev/") and
            "\x00" not in value and len(value) <= 240 and ".." not in Path(value).parts,
            "IR receiver must be a device path under /dev/.")
    try:
        resolved = Path(value).resolve()
    except (OSError, RuntimeError) as exc:
        raise DeskError("Cannot resolve the IR receiver path. Check its device symlink.") from exc
    require(not Path(value).is_relative_to("/dev/input") and not resolved.is_relative_to("/dev/input"),
            f"{value} is a decoded input device, not a raw IR receiver. "
            "Set Settings → IR receiver to /dev/desk-ir-rx or the receiving /dev/lircN device. "
            "The Numpad selection is separate from IR learning.")
    require(value != "/dev/desk-ir-tx", "Choose the IR receiver /dev/desk-ir-rx, not the transmitter.")
    return value


def check_receiver(receiver):
    receiver_path(receiver)
    try:
        mode = Path(receiver).stat().st_mode
    except FileNotFoundError as exc:
        raise DeskError(f"IR receiver {receiver} does not exist. Run the updated Pi installer to create "
                        "/dev/desk-ir-rx, or select the receiving /dev/lircN device in Settings.") from exc
    except OSError as exc:
        raise DeskError(f"Cannot access IR receiver {receiver}: {exc.strerror}.") from exc
    require(stat.S_ISCHR(mode), f"IR receiver {receiver} must be a character device.")
    require(os.access(receiver, os.R_OK | os.W_OK), f"IR receiver {receiver} is not readable and writable by the web service. "
            "Run the updated Pi installer to apply the desk user's receiver permissions.")


def capture(receiver, *, timeout=12):
    check_receiver(receiver)
    try:
        # LIRC 0.10.x --raw skips driver initialization and can format MODE2
        # timing bytes as "code:" lines. Initialize the kernel LIRC driver and
        # isolate output formatting from host mode2/lircd option-file settings.
        process = subprocess.Popen(["stdbuf", "-oL", "mode2", "--driver", "default", "--device", receiver],
                                   env={**os.environ, "LIRC_OPTIONS_PATH": "/dev/null"},
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    except OSError as exc:
        raise DeskError("Install LIRC (mode2) and coreutils (stdbuf) to learn buttons.") from exc
    frame, pending, diagnostic = Frame(), b"", ""
    deadline, last_signal = time.monotonic() + timeout, None
    try:
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], .1)[0]:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    detail = (diagnostic + pending.decode("utf-8", errors="replace"))[-2000:].strip()
                    raise DeskError(f"IR receiver {receiver} stopped. "
                                    + (f"mode2 reported: {detail}" if detail else
                                       "Check receiver permissions and whether another program is using it."))
                pending += chunk
                require(len(pending) <= 65536, "Receiver output exceeded the capture limit.")
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    decoded = line.decode("utf-8", errors="replace")
                    require(not decoded.lstrip().startswith("code:"),
                            "mode2 returned code data instead of pulse/space timings. "
                            "IR data arrived, but this output format cannot be learned. "
                            "Use mode2 with --driver default and no --raw flag.")
                    if decoded.split()[:1] not in (["pulse"], ["space"], ["timeout"], ["overflow"]):
                        diagnostic = (diagnostic + decoded + "\n")[-2000:]
                    before = len(frame.timings)
                    if frame.feed(decoded):
                        return frame.finish()
                    if len(frame.timings) != before:
                        last_signal = time.monotonic()
            if last_signal is not None and time.monotonic() - last_signal >= .15:
                return frame.finish()
        raise DeskError("No IR signal received within 12 seconds. Check the receiver and try again.")
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


def write_capture(directory, timings, frequency):
    require(type(frequency) is int and 20000 <= frequency <= 60000, "Carrier frequency must be 20000–60000 Hz.")
    require(5 <= len(timings) <= 1023 and len(timings) % 2 == 1 and
            all(type(n) is int and 0 < n < 20000 for n in timings) and sum(timings) <= 500000,
            "Invalid captured waveform.")
    remote = "desk_learned_" + uuid.uuid4().hex
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (remote + ".conf")
    text = (f"# Captured by Desk Orchestrator; physical response requires verification.\n"
            f"begin remote\n  name {remote}\n  flags RAW_CODES\n  eps 30\n  aeps 100\n"
            f"  gap 40000\n  frequency {frequency}\n  begin raw_codes\n    name KEY_CAPTURED\n")
    text += "".join("      " + " ".join(map(str, timings[i:i+8])) + "\n" for i in range(0, len(timings), 8))
    text += "  end raw_codes\nend remote\n"
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".capture-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path, remote


def device(config, name):
    item = config["inventory"].get(name)
    require(item and item["method"] == "ir", "Choose saved IR hardware first.")
    return item


def add_command(store, revision, name, command, *, remote="", key="", learn=False,
                frequency=38000, replace=False):
    identifier(command)
    config = store.read()
    require(config["revision"] == revision, "Configuration changed. Reload before adding a command.")
    item = device(config, name)
    require(replace or command not in item["settings"].get("commands", {}),
            "This command exists. Select Replace existing command to change it.")
    path = None
    with exclusive(store.directory / "scene.lock"):
        try:
            if learn:
                timings = capture(config.get("ir", {}).get("receiver", "/dev/desk-ir-rx"))
                path, remote = write_capture(store.directory / "ir-codes", timings, frequency)
                key = "KEY_CAPTURED"
            else:
                lirc_name(remote)
                lirc_name(key)
                require(key in catalog(config, remote), "LIRC does not have that key. Refresh the remote's keys.")
            def edit(current):
                settings = device(current, name)["settings"]
                settings.setdefault("commands", {})[command] = {
                    "verified": False, "discrete": False,
                    "sequence": [{"remote": remote, "key": key, "count": 1, "gap": .15}]}
            store.update(revision, edit)
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
