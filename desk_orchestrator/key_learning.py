"""Bounded, authenticated key-learning leases shared with the input service."""
import asyncio
import json
import time
import uuid
from contextlib import ExitStack, closing
from pathlib import Path

from .core import DeskError, atomic_json, exclusive, require
from .usb_devices import valid_input_path


def read(root):
    try:
        return json.loads((Path(root) / "key-learning.json").read_text())
    except (OSError, ValueError):
        return {}


def alive(state):
    return state.get("expires", 0) > time.time() and state.get("status") not in ("cancelled", "error")


def request(root, owner, action, *, token="", device="", revision=None, signal="key"):
    root = Path(root)
    with exclusive(root / "key-learning.lock", blocking=True):
        state = read(root)
        if action == "start":
            require(not alive(state), "Another key-learning session is active.")
            require(valid_input_path(device), "Choose a connected /dev/input device for learning.")
            require(signal in ("key", "relative", "absolute"), "Choose a supported input signal type.")
            state = dict(token=uuid.uuid4().hex, owner=owner, device=device, revision=revision,
                         signal=signal,
                         status="pending", expires=time.time() + 10, ready_by=time.time() + 5, deadline=time.time() + 60)
        else:
            require(token and state.get("token") == token and state.get("owner") == owner, "Key-learning session expired.")
            require(action in ("poll", "cancel"), "Invalid key-learning action.")
            if action == "cancel":
                state.update(status="cancelled", expires=0)
            elif alive(state):
                if state.get("status") == "pending" and time.time() > state["ready_by"]:
                    state.update(status="error", expires=0, error="The numpad listener did not respond. Start the updated listener on this controller, then retry.")
                else:
                    state["expires"] = min(time.time() + 10, state["deadline"])
        atomic_json(root / "key-learning.json", state)
        public = {key: value for key, value in state.items() if key not in ("owner", "device")}
        if not alive(state) and state.get("status") not in ("cancelled", "error"):
            public.update(status="error", error="Learning timed out. Check that the numpad listener is running, then retry.")
        return public


def update(root, token, **changes):
    with exclusive(Path(root) / "key-learning.lock", blocking=True):
        state = read(root)
        if state.get("token") == token and alive(state):
            state.update(changes)
            atomic_json(Path(root) / "key-learning.json", state)


class Broker:
    """The listener owns all reads, including its already exclusively grabbed input."""
    def __init__(self, root, evdev, paths, states, is_busy, finished):
        self.root, self.evdev = Path(root), evdev
        self.paths, self.states = paths, states
        self.is_busy, self.finished = is_busy, finished
        self.session = None
        self.started = 0

    def consume(self, event, path):
        if self.session is None:
            return False
        expected_type = {"key": 1, "relative": 2, "absolute": 3}[self.session.get("signal", "key")]
        matches = event.type == expected_type and (event.value == 1 if expected_type == 1 else event.value != 0 if expected_type == 2 else True)
        if (str(Path(path).resolve()) == str(Path(self.session["device"]).resolve()) and
                matches and event.timestamp() >= self.started and
                not self.session.get("captured")):
            names = (self.evdev.ecodes.KEY if expected_type == 1 else self.evdev.ecodes.bytype.get(expected_type, {})).get(event.code, str(event.code))
            self.session["captured"] = True
            update(self.root, self.session["token"], status="captured", code=event.code, name=names, event_type=event.type, value=event.value)
        return True

    async def run(self):
        while True:
            state = read(self.root)
            if not alive(state) or state.get("status") != "pending":
                await asyncio.sleep(.05)
                continue
            token = state["token"]
            reader = None
            try:
                require(not self.is_busy(), "Controller busy. Wait for the current action to finish, then retry.")
                with ExitStack() as stack:
                    stack.enter_context(exclusive(self.root / "scene.lock"))
                    self.session = state
                    self.started = time.monotonic()
                    canonical = str(Path(state["device"]).resolve())
                    existing = next((p for p in self.paths if str(Path(p).resolve()) == canonical), None)
                    if existing:
                        require(self.states.get(existing, {}).get("state") == "listening", "Selected input is disconnected or unreadable.")
                    else:
                        device = stack.enter_context(closing(self.evdev.InputDevice(state["device"])))
                        import fcntl
                        import struct
                        fcntl.ioctl(device.fd, 0x400445A0, struct.pack("i", time.CLOCK_MONOTONIC))
                        device.grab()
                        async def capture():
                            async for event in device.async_read_loop():
                                self.consume(event, state["device"])
                        reader = asyncio.create_task(capture())
                    update(self.root, token, status="listening")
                    try:
                        while alive(read(self.root)) and read(self.root).get("token") == token:
                            if reader and reader.done():
                                reader.result()
                                raise DeskError("Selected input disconnected. Reconnect and retry.")
                            if existing and self.states.get(existing, {}).get("state") != "listening":
                                raise DeskError("Selected input disconnected. Reconnect and retry.")
                            await asyncio.sleep(.05)
                    finally:
                        if reader:
                            reader.cancel()
                            await asyncio.gather(reader, return_exceptions=True)
                        # Discard buffered input before releasing the action lock.
                        self.finished()
            except (DeskError, OSError) as exc:
                update(self.root, token, status="error", error=str(exc))
            finally:
                self.session = None
            await asyncio.sleep(.05)
