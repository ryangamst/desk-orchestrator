"""Linux evdev numpad and directional controls; no queued hardware actions."""
import asyncio
import fcntl
import struct
import time
from contextlib import closing
from pathlib import Path

from .controls import ControlDecoder, run_control, active_scene
from .gmmk_slider import SliderDevice
from .key_commands import run_key_command
from . import numpad_profiles, key_learning
from .physical_numpad import KeyFeedback

from .core import DeskError, Runner, require
from .hardware import Hardware
from .diagnostics import trace, error_text


def evdev_module():
    try:
        import evdev
        return evdev
    except ImportError as exc:
        raise DeskError('Install keypad support: pip install ".[keypad]"') from exc


def devices():
    evdev = evdev_module()
    results = []
    for path in evdev.list_devices():
        with closing(evdev.InputDevice(path)) as device:
            results.append({"path": path, "name": device.name, "physical_path": device.phys})
    return results


class KeyGate:
    def __init__(self, bindings, debounce=0.35):
        self.bindings, self.debounce = bindings, debounce
        self.last = float("-inf")

    def accept(self, names, value, *, busy, now):
        self.reason = None
        if value != 1:
            self.reason = "release" if value == 0 else "repeat"
        elif busy:
            self.reason = "busy"
        elif now - self.last < self.debounce:
            self.reason = "debounce"
        if self.reason:
            return None
        if isinstance(names, str):
            names = [names]
        for name in names:
            if name in self.bindings:
                self.last = now
                return self.bindings[name]
        self.reason = "unmapped key"
        return None


class Reconfigure(Exception):
    pass


async def listen(config, runner, *, live=False, reload_config=None):
    while True:
        try:
            return await _listen(config, runner, live=live, reload_config=reload_config)
        except Reconfigure:
            config = reload_config()
            runner = Runner(config, Hardware(config), emit=runner.emit)


async def _listen(config, runner, *, live=False, reload_config=None):
    keypad = config.get("keypad", {})
    log = runner.log
    try:
        evdev = evdev_module()
    except DeskError as exc:
        log.emit("listener", "Listener could not start", level="error", error=str(exc), live=live)
        raise
    gate = KeyGate(keypad.get("bindings", {}))
    active = None
    finished_at = 0.0
    dry_scene = None
    dry_targets = {}
    decoder = ControlDecoder()
    last_control = float("-inf")
    sources = keypad.get("controls", {})
    control_scene = None
    feedback = KeyFeedback(config) if live else None
    if feedback:
        feedback.clear()

    async def run_scene(name, scene_runner):
        nonlocal finished_at, dry_scene
        keep_active = scene_runner.scene(name).get("keep_active_task", False)
        if not keep_active:
            dry_scene = None
            dry_targets.clear()
        try:
            await asyncio.to_thread(scene_runner.run, name, live=live)
            if not keep_active:
                dry_scene = name
        except (DeskError, OSError) as exc:
            print(f"Scene failed: {exc}", flush=True)
        finally:
            decoder.reset()
            finished_at = time.monotonic()

    async def adjust(name, direction, current_runner):
        nonlocal finished_at
        try:
            current_runner._dry_control_targets = dry_targets
            await asyncio.to_thread(run_control, current_runner, name, direction, live=live, dry_scene=dry_scene)
        except (DeskError, OSError) as exc:
            print(f"Control ignored: {exc}", flush=True)
        finally:
            finished_at = time.monotonic()

    async def send_command(key, current_runner, scene):
        nonlocal finished_at
        try:
            await asyncio.to_thread(run_key_command, current_runner, key, live=live,
                                    dry_scene=dry_scene, expected_scene=scene)
        except (DeskError, OSError) as exc:
            print(f"Numpad command ignored: {exc}", flush=True)
        finally:
            finished_at = time.monotonic()

    # De-duplicate aliases: each physical interface is opened/grabbed only once.
    paths = list(dict.fromkeys([keypad.get("device", ""), *[s["device"] for s in sources.values() if s["mode"] != "gmmk_raw"]]))
    paths.extend(s["press_device"] for s in sources.values() if s.get("press_device"))
    unique = {}
    for path in filter(None, paths):
        unique.setdefault(str(Path(path).resolve()), path)

    states = {path: {"state": "connecting"} for path in unique.values()}

    def capture_finished():
        nonlocal finished_at
        finished_at = time.monotonic()
        decoder.reset()

    broker = key_learning.Broker(config.get("runtime", {}).get("directory", ".runtime"), evdev, list(unique.values()), states,
                                lambda: active is not None and not active.done(), capture_finished)

    def listener_state(path, state, **details):
        states[path] = dict(state=state, **details)
        log.listener(path, live=live, **states[path])
        log.emit("listener", f"Input {state}", level="warning" if state == "disconnected" else "info",
                 path=path, live=live, **details)

    async def heartbeat():
        while True:
            if feedback:
                feedback.publish()
            for path, state in states.items():
                log.listener(path, live=live, **state)
            await asyncio.sleep(1)
            if reload_config and broker.session is None:
                try:
                    fresh = reload_config().get("keypad", {})
                except (DeskError, OSError, ValueError):
                    continue
                if any(fresh.get(field) != keypad.get(field) for field in ("device", "grab", "active_profile", "controls")):
                    raise Reconfigure()

    def handle_event(event, path, main_input, controls):
        nonlocal active, last_control, control_scene
        if feedback and main_input and event.type == 1:
            feedback.event(event.code, evdev.ecodes.KEY.get(event.code, []), event.value)
        dial = sources.get("dial", {})
        click_input = ("press_code" in dial and str(Path(path).resolve()) ==
                       str(Path(dial.get("press_device", dial["device"])).resolve()))
        is_click = click_input and event.type == 1 and event.code == dial["press_code"]
        if feedback:
            for name, source in controls.items():
                feedback.control_event(name, source, event)
            if is_click:
                feedback.click(event.value)
        if broker.consume(event, path):
            return
        # Each input and all work dispatched from it share one trace across threads.
        with trace():
            names = evdev.ecodes.KEY.get(event.code, []) if event.type == 1 else []
            log.emit("input", "Input received", path=path, type=event.type, code=event.code,
                     names=names, value=event.value, action={0: "release", 1: "press", 2: "repeat"}.get(event.value)
                     if event.type == 1 else "movement", live=live)
            scene_now = active_scene(runner) if live else dry_scene
            if scene_now != control_scene:
                decoder.reset()
                control_scene = scene_now
            directions = [(name, decoder.decode(name, source, event, click=False)) for name, source in controls.items()]
            if is_click:
                directions.append(("dial", "switch" if event.value == 1 else None))
            # Decode first so movement during a task cannot build up.
            reason = "stale input from completed action" if event.timestamp() < finished_at else None
            if active is not None and not active.done():
                reason = "controller busy; input not queued"
            if reason:
                log.emit("input", "Input ignored", reason=reason, live=live)
                return
            current_runner = runner
            event_config = config
            if reload_config and (event.type != 1 or event.value == 1):
                try:
                    fresh = reload_config()
                    event_config = fresh
                    gate.bindings = fresh.get("keypad", {}).get("bindings", {})
                    current_runner = Runner(fresh, Hardware(fresh), emit=runner.emit)
                    if any(fresh.get("keypad", {}).get(field) != keypad.get(field)
                           for field in ("device", "grab", "active_profile", "controls")):
                        return
                except (DeskError, OSError, ValueError) as exc:
                    log.emit("input", "Configuration reload failed; input ignored", level="error", error=error_text(exc), live=live)
                    print(f"Cannot reload configuration; input ignored: {exc}", flush=True)
                    return
            is_control_key = is_click or event.type == 1 and any(source["mode"] == "keys" and event.code in
                (source["code"], source["down_code"]) for source in controls.values())
            if main_input and event.type == 1 and not is_control_key:
                scene_now = active_scene(current_runner) if live else dry_scene
                commands = event_config.get("scenes", {}).get(scene_now, {}).get("key_commands", {})
                gate.bindings = {key: ("command", key) for key in commands}
                gate.bindings.update({key: ("scene", scene) for key, scene in
                                      event_config.get("keypad", {}).get("bindings", {}).items()})
                input_keys = numpad_profiles.signal_keys(event_config, event.code, names)
                action = gate.accept(input_keys, event.value, busy=False, now=time.monotonic())
                if action and action[0] == "command":
                    log.emit("input", "Key mapped to numpad command", key=action[1], scene=scene_now, live=live)
                    active = asyncio.create_task(send_command(action[1], current_runner, scene_now))
                    return
                if action:
                    scene = action[1]
                    log.emit("input", "Key mapped to task", scene=scene, live=live, revision=current_runner.config.get("revision"))
                    active = asyncio.create_task(run_scene(scene, current_runner))
                    return
                reason = gate.reason
            for name, direction in directions:
                if reload_config and current_runner.config.get("keypad", {}).get("controls", {}).get(name) != sources[name]:
                    reason = "control input configuration changed; restart listener"
                    continue
                if direction:
                    if time.monotonic() - last_control < .1:
                        reason = "control rate limit"
                        continue
                    last_control = time.monotonic()
                    log.emit("input", "Input mapped to control", control=name, direction=direction, live=live)
                    active = asyncio.create_task(adjust(name, direction, current_runner))
                    return
            if (event.type == 1 and event.code in (114, 115) and event.value == 1
                    and reason in (None, "unmapped key") and not any(direction for _, direction in directions)):
                reason = "Volume key has no matching control input. In Numpad, choose Two keys with increase 115 and decrease 114 on this input device, then restart the numpad listener."
            log.emit("input", "Input ignored", reason=reason or "no matching direction (release, repeat, baseline, or below threshold)", live=live)

    async def read_input(path):
        nonlocal active, last_control
        canonical = str(Path(path).resolve())
        main_input = canonical == str(Path(keypad.get("device", "")).resolve())
        controls = {name: source for name, source in sources.items()
                    if source["mode"] != "gmmk_raw" and str(Path(source["device"]).resolve()) == canonical}
        while True:
            try:
                with closing(evdev.InputDevice(path)) as device:
                    fcntl.ioctl(device.fd, 0x400445A0, struct.pack("i", time.CLOCK_MONOTONIC))
                    if keypad.get("grab", True):
                        device.grab()
                    if feedback:
                        for name, source in controls.items():
                            if source["mode"] == "absolute":
                                try:
                                    info = device.absinfo(source["code"])
                                    if info and info.max > info.min:
                                        feedback.ranges[name] = (info.min, info.max)
                                except (OSError, AttributeError):
                                    pass  # Directional feedback still works without a range.
                    decoder.reset()
                    listener_state(path, "listening", name=device.name, grab=keypad.get("grab", True))
                    print(f"Listening to {device.name} ({'LIVE' if live else 'DRY RUN'})", flush=True)
                    async for event in device.async_read_loop():
                        # SYN_DROPPED means events were lost. Reopen to resync and
                        # establish fresh absolute baselines without transmitting.
                        if event.type == 0 and event.code == 3:
                            raise OSError("Input events lost; resetting input")
                        if event.type not in (1, 2, 3):
                            continue
                        handle_event(event, path, main_input, controls)
            except OSError as exc:
                if feedback:
                    if main_input:
                        feedback.keys.clear()
                    affected = set(controls)
                    dial = sources.get("dial", {})
                    if "press_code" in dial and canonical == str(Path(dial.get("press_device", dial["device"])).resolve()):
                        affected.add("dial")
                    feedback.clear_controls(affected)
                decoder.reset()
                listener_state(path, "disconnected", error=str(exc))
                print(f"Input {path} unavailable ({exc}); reconnecting in 2 seconds", flush=True)
                await asyncio.sleep(2)

    async def read_slider(name, source):
        # Separate status identity even when the source references the same
        # keyboard path as the main reader. Discovery is repeated on reconnect.
        identity = "gmmk-slider:" + source["device"]
        while True:
            try:
                with closing(SliderDevice(source["device"])) as device:
                    decoder.positions.pop(name, None)
                    listener_state(identity, "listening", name=device.name, device=device.path, grab=False)
                    async for event in device.async_read_loop():
                        handle_event(event, device.path, False, {name: source})
            except OSError as exc:
                if feedback:
                    feedback.clear_controls([name])
                decoder.positions.pop(name, None)
                listener_state(identity, "disconnected", error=str(exc))
                await asyncio.sleep(2)

    readers = [asyncio.create_task(read_input(path)) for path in unique.values()]
    readers.extend(asyncio.create_task(read_slider(name, source)) for name, source in sources.items()
                   if source["mode"] == "gmmk_raw")
    pulse = asyncio.create_task(heartbeat())
    learning = asyncio.create_task(broker.run())
    try:
        if readers:
            done, _ = await asyncio.wait([*readers, pulse, learning], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        else:
            await asyncio.gather(pulse, learning)
    finally:
        pulse.cancel()
        learning.cancel()
        await asyncio.gather(learning, return_exceptions=True)
        await asyncio.gather(pulse, return_exceptions=True)
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        if feedback:
            feedback.clear()
        if active is not None:
            await asyncio.shield(active)
        for path in states:
            listener_state(path, "stopped")


async def monitor_input(path, seconds=20):
    """Explicit, bounded diagnostic capture. Never grabs or sends commands."""
    from .usb_devices import valid_input_path
    require(valid_input_path(path), "Choose a /dev/input/eventN or by-id/by-path input.")
    evdev = evdev_module()
    with closing(evdev.InputDevice(path)) as device:
        print(f"Reading {device.name} for {seconds} seconds; no commands will be sent.", flush=True)
        print(f"Capabilities: {device.capabilities(verbose=True)}", flush=True)
        async def read():
            async for event in device.async_read_loop():
                if event.type in (1, 2, 3):
                    name = evdev.ecodes.bytype.get(event.type, {}).get(event.code, str(event.code))
                    print(f"type={event.type} code={event.code} ({name}) value={event.value}", flush=True)
        try:
            await asyncio.wait_for(read(), seconds)
        except asyncio.TimeoutError:
            pass
