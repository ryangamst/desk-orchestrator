"""Read-only key and control feedback shared by the listener and web workers."""
import json
import math
import time
import uuid
from pathlib import Path

from .core import atomic_json
from .controls import ControlDecoder
from .numpad_profiles import labels, signal_keys

STALE_AFTER = 3
TAP_DURATION = .35


class KeyFeedback:
    def __init__(self, config):
        self.path = Path(config.get("runtime", {}).get("directory", ".runtime")) / "numpad-keys.json"
        self.device = config["keypad"]["device"]
        self.keys = {}
        self.sources = config["keypad"].get("controls", {})
        self.controls = {}
        self.decoder = ControlDecoder()
        self.ranges = {}

    def control(self, name):
        if name not in self.controls:
            self.controls[name] = dict(token=uuid.uuid4().hex, steps=0,
                sequence=0, direction=0, moved_at=-1, held=False, pressed_at=-1, position=None)
        return self.controls[name]

    def control_event(self, name, source, event):
        # Observe physical direction independently of command inversion and task
        # baselines. Visual feedback also works during learning and busy actions.
        mode = source["mode"]
        matches = (mode == "keys" and event.type == 1 and event.value == 1
                   and event.code in (source["code"], source["down_code"])) or (
                   mode == "relative" and event.type == 2 and event.code == source["code"] and event.value) or (
                   mode in ("absolute", "gmmk_raw") and event.type == 3 and event.code == source["code"])
        if not matches or not isinstance(event.value, (int, float)) or not math.isfinite(event.value):
            return
        control = self.control(name)
        direction = self.decoder.decode(name, dict(source, invert=False), event, click=False)
        if direction:
            delta = 1 if direction == "increase" else -1
            # Relative axes can report several detents in one event.
            if mode == "relative":
                delta = event.value
            control.update(steps=control["steps"] + delta, direction=1 if delta > 0 else -1,
                           moved_at=time.monotonic())
        if name == "slider" and mode == "absolute" and name in self.ranges:
            low, high = self.ranges[name]
            control["position"] = max(0, min(100, (event.value - low) * 100 / (high - low)))
        control["sequence"] += 1
        self.publish()

    def click(self, value):
        if value not in (0, 1):
            return
        control = self.control("dial")
        control["held"] = value == 1
        if value == 1:
            control["pressed_at"] = time.monotonic()
        self.publish()

    def clear_controls(self, names):
        for name in names:
            self.controls.pop(name, None)
            self.decoder.positions.pop(name, None)
            self.ranges.pop(name, None)
        self.publish()

    def event(self, code, names, value):
        if value not in (0, 1, 2):
            return
        if value == 1:
            self.keys[str(code)] = dict(names=names, held=True, pressed_at=time.monotonic())
        elif str(code) in self.keys:
            if value == 2:
                return  # A held key already has feedback; repeats add no flashes.
            self.keys[str(code)]["held"] = False
        else:
            return
        self.publish()

    def clear(self):
        self.keys.clear()
        self.controls.clear()
        self.decoder.reset()
        self.ranges.clear()
        self.publish()

    def publish(self):
        now = time.monotonic()
        self.keys = {code: key for code, key in self.keys.items()
                     if key["held"] or now - key["pressed_at"] < TAP_DURATION}
        try:
            atomic_json(self.path, dict(device=self.device, updated=now, keys=self.keys,
                                       sources=self.sources, controls=self.controls))
        except OSError:
            # Feedback must never prevent a physical key from running its action.
            pass


def state(config):
    empty = {"pressed_keys": [], "controls": {}}
    path = Path(config.get("runtime", {}).get("directory", ".runtime")) / "numpad-keys.json"
    try:
        data = json.loads(path.read_text())
        now = time.monotonic()
        if data["device"] != config["keypad"]["device"] or not 0 <= now - data["updated"] < STALE_AFTER:
            return empty
        visible = labels(config)
        pressed = set()
        for code, key in data["keys"].items():
            if not key["held"] and not 0 <= now - key["pressed_at"] < TAP_DURATION:
                continue
            names = signal_keys(config, int(code), key["names"])
            if isinstance(names, str):
                names = [names]
            pressed.update(name for name in names if name in visible)
        controls = {}
        for name, control in data.get("controls", {}).items():
            if data.get("sources", {}).get(name) != config["keypad"].get("controls", {}).get(name):
                continue
            controls[name] = dict(token=control["token"], steps=control["steps"],
                sequence=control["sequence"], direction=control["direction"], position=control["position"],
                active=0 <= now - control["moved_at"] < TAP_DURATION,
                pressed=control["held"] or 0 <= now - control["pressed_at"] < TAP_DURATION)
        return {"pressed_keys": sorted(pressed), "controls": controls}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return empty


def pressed_keys(config):
    return state(config)["pressed_keys"]
