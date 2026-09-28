from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path

from .diagnostics import EventLog, error_text, target, trace


class DeskError(Exception):
    """An actionable configuration or hardware error."""


def require(condition, message):
    if not condition:
        raise DeskError(message)


def number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".desk-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def exclusive(path: Path, *, blocking=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise DeskError("Another scene is running; this request was not queued.") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def load_config(path):
    path = Path(path).resolve()
    with path.open("rb") as stream:
        config = json.load(stream) if path.suffix == ".json" else tomllib.load(stream)
    return validate_config(config, path.parent)


def validate_config(config, base):
    from .controls import validate_mappings, validate_sources
    require(isinstance(config, dict), "Configuration must be an object.")
    require(config.get("version") == 1, "Configuration version must be 1.")
    require(isinstance(config.get("scenes"), dict), "Scenes must be a table.")
    if "kvm" in config:
        from .keyboard_transport import validate_settings
        validate_settings(config["kvm"])
    for name, scene in config["scenes"].items():
        require(isinstance(scene, dict), f"Invalid scene: {name}")
        require(type(scene.get("keep_active_task", False)) is bool,
                f"{name}: keep active task must be a checkbox.")
        if scene.get("keep_active_task", False):
            require(not scene.get("controls") and not scene.get("key_commands"),
                    f"{name}: tasks that keep the active task cannot have keyboard mappings or dial/slider controls.")
        require(isinstance(scene.get("steps"), list) and scene["steps"], f"{name}: no steps")
        for step in scene["steps"]:
            validate_step(step)
        validate_mappings(scene.get("controls", {}))
    validate_sources(config.get("keypad", {}).get("controls", {}))
    from .key_commands import validate_bindings
    for scene in config["scenes"].values():
        validate_bindings(scene.get("key_commands", {}), config)
    if "lighting" in config.get("keypad", {}):
        from .gmmk_rgb import validate as validate_lighting
        validate_lighting(config["keypad"]["lighting"])
    for key, scene in config.get("keypad", {}).get("bindings", {}).items():
        require(key.startswith("KEY_"), f"Invalid Linux key name: {key}")
        require(scene in config["scenes"], f"{key}: unknown scene {scene}")
    runtime = config.setdefault("runtime", {})
    root = Path(runtime.get("directory", "../.runtime")).expanduser()
    runtime["directory"] = str(root if root.is_absolute() else (Path(base) / root).resolve())
    return config


def validate_step(step):
    require(isinstance(step, dict), "Each step must be a table.")
    kind = step.get("kind")
    fields = {
        "ir": {"kind", "device", "command"},
        "volume": {"kind", "device", "db"},
        "kvm": {"kind", "port"},
        "monitor": {"kind", "device", "input"},
        "wait": {"kind", "seconds"},
    }
    require(isinstance(kind, str) and kind in fields, f"Unknown step kind: {kind}")
    require(set(step) == fields[kind], f"{kind}: expected fields {sorted(fields[kind])}")
    if kind == "wait":
        require(number(step["seconds"], 0, 60), "Wait must be 0–60 seconds.")
    elif kind == "kvm":
        require(type(step["port"]) is int and 1 <= step["port"] <= 4, "KVM port must be 1–4.")
    else:
        require(isinstance(step["device"], str) and step["device"], "Missing device name.")
        if kind == "volume":
            require(number(step["db"], -120, 20), "Invalid volume target.")
        else:
            value = step["command" if kind == "ir" else "input"]
            require(isinstance(value, str) and value, "Missing command/input.")


class Runner:
    def __init__(self, config, hardware, emit=print):
        self.config, self.hardware, self.emit = config, hardware, emit
        self.runtime = Path(config["runtime"]["directory"])
        self.log = EventLog(self.runtime)

    def scene(self, name):
        require(name in self.config["scenes"], f"Unknown scene: {name}")
        return self.config["scenes"][name]

    def issues(self, name, *, probe=False, passed=None):
        from .controls import check_mapping
        from .key_commands import binding_step
        scene = self.scene(name)
        problems = []
        for i, step in enumerate(scene["steps"], 1):
            try:
                detail = self.hardware.check(step, probe=probe)
                if probe and passed is not None and step["kind"] != "wait":
                    messages = {"ir": "LIRC codes are available. No IR command was sent.",
                                "volume": "LIRC volume codes are available. No volume command was sent.",
                                "kvm": "USB keyboard device is writable. No keypress was sent."}
                    passed.append(dict(label=f"Step {i}", device=step.get("device", ""),
                                       message=detail or messages.get(step["kind"], "Connection check passed.")))
            except (DeskError, OSError) as exc:
                problems.append(f"Step {i}: {exc}")
        for control, mapping in scene.get("controls", {}).items():
            try:
                require(control in self.config.get("keypad", {}).get("controls", {}),
                        "Configure this input on the Numpad page first")
                check_mapping(mapping, self.hardware, probe=probe)
                if probe and passed is not None:
                    passed.append(dict(label=control, device=mapping["device"],
                                       message="Control mapping check passed. No control command was sent."))
            except (DeskError, OSError) as exc:
                problems.append(f"{control}: {exc}")
        for key, command in scene.get("key_commands", {}).items():
            try:
                step = binding_step(command)
                self.hardware.check(step, probe=probe)
                if probe and passed is not None:
                    passed.append(dict(label=key, device=step.get("device", ""), message=
                        "LIRC codes are available. No IR command was sent." if step["kind"] == "ir" else
                        "USB command device is writable. No keypress was sent."))
            except (DeskError, OSError) as exc:
                problems.append(f"{key}: {exc}")
        return problems

    def run(self, name, *, live=False):
        with trace():
            self.log.emit("task", "Task requested", scene=name, live=live, revision=self.config.get("revision"))
            try:
                result = self._run(name, live=live)
            except BaseException as exc:
                self.log.emit("task", "Task failed or rejected", level="error", scene=name, live=live, error=error_text(exc))
                raise
            self.log.emit("task", "Task completed" if live else "Dry run completed; no commands sent",
                          scene=name, live=live, status=result["status"], physical_state_verified=False)
            return result

    def execute_step(self, step, *, live, **details):
        destination = target(self.config, step)
        self.log.emit("command", "Command started" if live else "Command planned; no transmission",
                      live=live, step=step, target=destination, **details)
        if not live:
            return
        try:
            self.hardware.execute(step)
        except BaseException as exc:
            self.log.emit("command", "Command failed; outcome may be partial", level="error", live=True,
                          step=step, target=destination, error=error_text(exc), **details)
            raise
        result = {"monitor": "Input confirmed by SmartThings feedback", "wait": "Wait completed"}.get(
            step["kind"], "Command sent; physical response unverified")
        self.log.emit("command", result, live=True, step=step, target=destination, **details)

    def _run(self, name, *, live=False):
        scene = self.scene(name)
        if not live:
            self.emit(f"DRY RUN: {scene.get('label', name)}")
            for i, step in enumerate(scene["steps"], 1):
                self.emit(f"  {i}. {json.dumps(step)}")
                self.execute_step(step, live=False, scene=name, index=i)
            return {"scene": name, "status": "dry_run"}
        with exclusive(self.runtime / "scene.lock"):
            # One-shot actions share the execution lock but never replace the
            # active task or its selected volume source, even if an action fails.
            status_path = self.runtime / ("action-status.json" if scene.get("keep_active_task", False) else "status.json")
            state = {"scene": name, "status": "running", "started_at": time.time(),
                     "completed_steps": 0, "physical_state_verified": False}
            atomic_json(status_path, state)
            try:
                for i, step in enumerate(scene["steps"], 1):
                    state["active_step"] = i
                    atomic_json(status_path, state)
                    self.emit(f"{name} [{i}/{len(scene['steps'])}]: {json.dumps(step)}")
                    self.execute_step(step, live=True, scene=name, index=i)
                    state["completed_steps"] = i
                    atomic_json(status_path, state)
                state["status"] = "commands_sent"
                self.emit("Commands sent. IR/KVM physical state is unverified.")
            except BaseException as exc:
                state["status"] = "failed"
                state["error"] = str(exc) if isinstance(exc, (DeskError, OSError)) else type(exc).__name__
                self.emit("Scene stopped; previous actions may have taken effect. No automatic rollback.")
                raise
            finally:
                state["finished_at"] = time.time()
                atomic_json(status_path, state)
            return state
