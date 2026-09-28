"""Flask configuration UI and virtual numpad backed by the shared controller."""
from __future__ import annotations

import hmac
import hashlib
import io
import json
import os
import secrets
import socket
import sqlite3
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .config_store import ConfigStore, KEYS, METHODS, TYPES, text
from .core import DeskError, Runner, atomic_json, exclusive, require
from .hardware import Hardware
from .smartthings import segment
from .usb_devices import scan_usb, selected_input, save_selection, match_input
from .controls import CONTROL_NAMES, validate_sources, validate_mappings, active_scene
from .diagnostics import EventLog
from . import ir_learning
from . import oauth
from . import tls
from . import virtual_numpad
from . import hardware_map
from .key_commands import COMMANDS, SHORTCUT_KEYS, command_label, editable_sequence, is_ir_binding, reserved_keys
from . import gmmk_rgb
from .appearance import COLORS, ICONS, task_appearance


def set_password(directory, password):
    from pathlib import Path
    directory = Path(directory).resolve()
    require(len(password) >= 12, "Use a password of at least 12 characters.")
    with exclusive(directory / "auth.lock", blocking=True):
        atomic_json(directory / "web-auth.json", {"password_hash": generate_password_hash(password),
                                                  "secret": secrets.token_hex(32)})


def create_app(directory, seed, *, trusted_hosts=None, secure_cookie=False):
    store = ConfigStore(directory, seed)
    auth_path = store.directory / "web-auth.json"
    if not auth_path.exists():
        password = os.environ.get("DESK_WEB_PASSWORD", "")
        require(password, "Set the web password first with desk web-password --data-dir PATH.")
        set_password(store.directory, password)
    auth = json.loads(auth_path.read_text())
    app = Flask(__name__)
    app.config.update(SECRET_KEY=auth["secret"], MAX_CONTENT_LENGTH=1024 * 1024,
                      SESSION_COOKIE_NAME="desk_session", SESSION_COOKIE_HTTPONLY=True,
                      # The top-level GET redirect from Samsung must retain login.
                      # All local POST actions still require the session CSRF token.
                      SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=secure_cookie,
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      TRUSTED_HOSTS=trusted_hosts or ["localhost", "127.0.0.1", "[::1]"])
    app.extensions["desk_store"] = store
    base_trusted_hosts = list(app.config["TRUSTED_HOSTS"])

    def allow_oauth_host():
        # An authenticated setup explicitly names this host. This only permits
        # its Host header; it does not bind a new interface or trust proxy headers.
        hosts = list(base_trusted_hosts)
        uri = store.read().get("smartthings", {}).get("redirect_uri")
        if uri:
            try:
                hosts.append(oauth.validate_redirect(uri).hostname)
            except DeskError:
                pass
        app.config["TRUSTED_HOSTS"] = hosts

    allow_oauth_host()
    attempts = deque(maxlen=100)
    attempts_lock = threading.Lock()

    @app.before_request
    def protect():
        if request.routing_exception is not None:
            raise request.routing_exception
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.method == "POST":
            submitted = request.form.get("csrf", "")
            if not hmac.compare_digest(session["csrf"], submitted):
                abort(400, "This form expired. Reload the page before trying again.")
        if request.endpoint not in ("login", "static", "oauth_callback") and not session.get("authenticated"):
            return redirect(url_for("login"))

    @app.after_request
    def headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def context():
        return dict(keys=KEYS, methods=METHODS, types=TYPES, control_names=CONTROL_NAMES,
                    keyboard_commands=COMMANDS, command_label=command_label, shortcut_keys=SHORTCUT_KEYS,
                    editable_sequence=editable_sequence, is_ir_binding=is_ir_binding,
                    task_appearance=task_appearance, task_colors=COLORS, task_icons=ICONS,
                    controller_id=hardware_map.CONTROLLER, csrf=session.get("csrf"))

    @app.errorhandler(DeskError)
    def config_error(error):
        return render_template("error.html", error=str(error)), 409

    @app.errorhandler(413)
    def too_large(error):
        return render_template("error.html", error="This upload is too large. Maximum backup size is 1 MB."), 413

    def revision():
        try:
            return int(request.form["revision"])
        except (KeyError, ValueError):
            raise DeskError("Missing configuration revision. Reload the form.") from None

    def parse(field):
        try:
            return json.loads(request.form.get(field, ""))
        except ValueError:
            raise DeskError(f"Invalid {field}. Check the editor values and try again.") from None

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            now = time.monotonic()
            with attempts_lock:
                while attempts and attempts[0] < now - 60:
                    attempts.popleft()
                if len(attempts) >= 10:
                    return render_template("login.html", error="Too many attempts. Try again in one minute."), 429
                attempts.append(now)
            if check_password_hash(auth["password_hash"], request.form.get("password", "")):
                session.clear()
                session.update(authenticated=True, csrf=secrets.token_urlsafe(32))
                session.permanent = True
                return redirect(url_for("dashboard"))
            error = "That password didn’t match. Try again."
        return render_template("login.html", error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/health")
    def health():
        return {"status": "online"}

    @app.get("/console")
    def developer_console():
        return render_template("console.html", cfg=store.read(), host=socket.gethostname(), config_path=store.path)

    @app.get("/api/events")
    def console_events():
        try:
            after = int(request.args.get("after", "0"))
            if not 0 <= after <= 2**63 - 1:
                raise ValueError
        except ValueError:
            return {"error": "Invalid event cursor."}, 400
        cfg = store.read()
        try:
            data = EventLog(cfg["runtime"]["directory"]).read(after)
        except (OSError, sqlite3.Error, ValueError):
            return {"error": "Console log unavailable. Check runtime directory permissions and disk space."}, 503
        now = time.time()
        control_inputs = [
            ("gmmk-slider:" if source["mode"] == "gmmk_raw" else "") + source["device"]
            for source in cfg["keypad"].get("controls", {}).values()
        ]
        control_inputs.extend(source["press_device"] for source in cfg["keypad"].get("controls", {}).values()
                              if source.get("press_device"))
        expected = list(dict.fromkeys([cfg["keypad"]["device"], *control_inputs]))
        for listener in data["listeners"]:
            listener["age"] = max(0, now - listener["updated"])
            listener["stale"] = listener["age"] > 15
        return dict(data, expected_inputs=expected)

    @app.get("/")
    def dashboard():
        cfg = store.read()
        active = active_scene(Runner(cfg, Hardware(cfg)))
        return render_template("dashboard.html", cfg=cfg, active_task=active,
                               key_commands=virtual_numpad.key_commands(cfg, active))

    @app.get("/api/numpad")
    def virtual_numpad_state():
        try:
            cfg = store.read()
            state = virtual_numpad.state(cfg)
            state["active_color"] = task_appearance(state["active_task"], cfg["scenes"][state["active_task"]])["color"] if state["active_task"] else None
            if "revision" in request.args and request.args["revision"] != str(cfg["revision"]):
                values = dict(cfg=cfg, active_task=state["active_task"])
                state["overview"] = {
                    "stats": render_template("overview_stats.html", **values),
                    "tasks": render_template("overview_tasks.html", **values),
                    "mappings": {key: dict(task=name, label=cfg["scenes"][name]["label"],
                                            **task_appearance(name, cfg["scenes"][name]))
                                 for key, name in cfg["keypad"]["bindings"].items()},
                }
            return state
        except (DeskError, OSError, ValueError):
            return {"error": "Controller state unavailable. Check the developer console."}, 503

    @app.post("/api/numpad/press")
    def virtual_numpad_press():
        try:
            result = virtual_numpad.press(store.read(), request.form)
            return {"ok": True, "result": result,
                    "message": "Wheel volume target switched." if result.get("status") == "target_switched"
                    else "Commands sent. IR/KVM physical response is unverified."}
        except DeskError as exc:
            return {"ok": False, "error": str(exc)}, 409
        except Exception:
            return {"ok": False, "error": "Input could not complete. Earlier actions may have taken effect; check the developer console before trying again."}, 503

    def control_sources_from_form(cfg):
        sources = {}
        for name in CONTROL_NAMES:
            mode = request.form.get(f"{name}_input_mode", "")
            if not mode:
                continue
            try:
                sources[name] = {"device": request.form.get(f"{name}_input_device", "").strip(), "mode": mode,
                                 "code": 32 if mode == "gmmk_raw" else int(request.form.get(f"{name}_input_code", "")),
                                 "down_code": int(request.form.get(f"{name}_down_code", "114")),
                                 "deadband": int(request.form.get(f"{name}_deadband", "4")),
                                 "invert": f"{name}_invert" in request.form}
                if mode == "gmmk_raw":
                    sources[name]["device"] = cfg["keypad"]["device"]
                if name == "dial" and request.form.get("dial_press_code", "").strip():
                    sources[name]["press_code"] = int(request.form["dial_press_code"])
                    if request.form.get("dial_press_device", "").strip():
                        sources[name]["press_device"] = request.form["dial_press_device"].strip()
            except ValueError:
                raise DeskError("Input codes and movement threshold must be whole numbers.") from None
        validate_sources(sources, check_signal_codes=True)
        return sources

    @app.route("/numpad", methods=["GET", "POST"])
    def numpad():
        cfg = store.read()
        snapshot = scan_usb()
        error = None
        if request.method == "POST":
            try:
                if request.form.get("section") == "lighting":
                    lighting = gmmk_rgb.from_form(request.form)
                    store.update(revision(), lambda config: config["keypad"].update(lighting=lighting))
                    flash("RGB settings saved. Apply saved lighting to send them to the numpad.")
                elif request.form.get("section") == "controls":
                    sources = control_sources_from_form(cfg)
                    store.update(revision(), lambda config: config["keypad"].update(controls=sources))
                    flash("Dial and slider inputs saved. Restart the numpad service, then assign their actions in each task.")
                else:
                    store.update(revision(), lambda config: save_selection(
                        config, request.form.get("device_choice", ""), request.form.get("manual_path", "").strip(),
                        "grab" in request.form, snapshot))
                    flash("Numpad device saved. Restart the numpad service to use the selected USB input. Your key mappings are unchanged.")
                return redirect(url_for("numpad"))
            except DeskError as exc:
                error = str(exc)
        current = selected_input(cfg["keypad"], snapshot)
        selected = match_input(snapshot, cfg["keypad"]["device"])
        control_inputs = [item for device in snapshot["devices"] for item in device["inputs"]]
        suggested_input = next((item["selection_path"] for item in sorted(control_inputs, key=lambda entry: "Consumer Control" not in entry["name"])
                                if selected and item["port"] == selected["port"] and
                                "KEY_VOLUMEUP (115)" in item.get("control_signals", [])), "")
        chosen = next((item["selection_path"] for item in snapshot["candidates"]
                       if cfg["keypad"]["device"] in (item["path"], item["selection_path"], *item["aliases"])), "manual")
        return render_template("numpad.html", cfg=cfg, snapshot=snapshot, current=current, error=error,
                               lighting=cfg["keypad"].get("lighting", gmmk_rgb.DEFAULTS),
                               lighting_modes=gmmk_rgb.MODES, lighting_status=gmmk_rgb.status(cfg["keypad"]["device"]),
                               control_inputs=control_inputs, suggested_input=suggested_input,
                               chosen=request.form.get("device_choice", chosen),
                               form_revision=request.form.get("revision", cfg["revision"])), 422 if error else 200

    @app.post("/numpad/lighting/apply")
    def apply_numpad_lighting():
        # Hold the configuration lock through the single USB report so a device
        # selection or saved settings change cannot race this action.
        with exclusive(store.directory / "config.lock", blocking=False):
            cfg = store.read()
            require(revision() == cfg["revision"], "Configuration changed in another tab. Reload before applying RGB.")
            require("lighting" in cfg["keypad"], "Save RGB settings before applying them.")
            gmmk_rgb.apply(cfg["keypad"]["device"], cfg["keypad"]["lighting"])
        flash("Lighting report sent. Check the numpad: the chosen onboard profile and layer must be active. Physical lighting is not verified.")
        return redirect(url_for("numpad", _anchor="numpad-lighting"))

    @app.get("/hardware")
    def hardware_list():
        # The Hardware page is the Hardware Map: operate it, or Edit to add and wire devices.
        cfg = store.read()
        selected = request.args.get("device", "")
        return render_template("hardware_map.html", cfg=cfg, model=hardware_map.model(cfg),
                               selected=selected if selected in cfg["inventory"] else "")

    @app.get("/map")
    def map_page():
        return redirect(url_for("hardware_list"), code=301)

    @app.route("/hardware/new", methods=["GET", "POST"])
    @app.route("/hardware/<name>/edit", methods=["GET", "POST"])
    def hardware_edit(name=None):
        cfg = store.read()
        if name is not None and name not in cfg["inventory"]:
            abort(404)
        item = cfg["inventory"].get(name, dict(name="", type="Audio", method="ir", notes="", settings={}))
        error = None
        if request.method == "POST":
            try:
                item = {"name": request.form.get("name", "").strip(), "type": request.form.get("type", ""),
                        "method": request.form.get("method", ""), "notes": request.form.get("notes", ""),
                        "settings": parse("settings")}
                if name == hardware_map.CONTROLLER:
                    # The controller is a fixed map node: only its name and notes are editable.
                    item.update(type=hardware_map.CONTROLLER_TYPE, method="none", settings={})
                saved_name = name or request.form.get("id", "")
                store.save_hardware(revision(), saved_name, item, creating=name is None)
                flash("Hardware saved. New tasks can use it immediately.")
                return redirect(url_for("hardware_list", device=saved_name))
            except DeskError as exc:
                error = str(exc)
        from .keyboard_transport import serial_devices
        return render_template("hardware_edit.html", cfg=cfg, item=item, name=name, error=error,
                               serial_devices=serial_devices(),
                               form_revision=request.form.get("revision", cfg["revision"])), 422 if error else 200

    @app.post("/hardware/<name>/smartthings/test")
    def hardware_test_smartthings(name):
        attempted = False
        try:
            cfg = store.read()
            submitted_revision = revision()
            require(cfg["revision"] == submitted_revision, "Configuration changed. Reload before testing.")
            item = cfg["inventory"].get(name)
            require(item and item["method"] == "smartthings", "Choose saved SmartThings hardware.")
            settings = item["settings"]
            input_name = request.form.get("input", "")
            require(input_name in settings.get("inputs", {}), "Choose a saved input mapping.")
            value = settings["inputs"][input_name]
            for field in ("device_id", "component", "capability", "command", "attribute"):
                require(settings.get(field, "").strip(), f"Save the SmartThings {field} before testing.")
            require(value.strip() and not value.upper().startswith("REPLACE_"),
                    "Replace the example input value with a discovered SmartThings value and save first.")
            runtime = Path(cfg["runtime"]["directory"])
            with exclusive(runtime / "scene.lock"):
                require(store.read()["revision"] == submitted_revision, "Configuration changed. Reload before testing.")
                client = Hardware(cfg).smartthings
                health = client.call("GET", f"/v1/devices/{segment(settings['device_id'])}/health")
                require(health.get("state") == "ONLINE",
                        "SmartThings reports this device offline or its connection is unknown. Turn it on and check its connection in SmartThings.")
                log = EventLog(runtime)
                details = dict(device=name, input=input_name, value=value, live=True)
                log.emit("command", "SmartThings input test started", **details)
                attempted = True
                try:
                    client.set_input(settings, value)
                except (DeskError, OSError):
                    log.emit("command", "SmartThings input test failed; input change unconfirmed", level="error", **details)
                    raise
                log.emit("command", "SmartThings input test confirmed by API feedback", **details)
            return {"ok": True, "message": f"SmartThings confirmed {input_name} ({value}). Check the monitor to verify the picture changed. This does not mark the hardware verified."}
        except (DeskError, OSError) as exc:
            message = str(exc) if isinstance(exc, DeskError) else "The input test could not complete. Check server connectivity and runtime file permissions."
            if attempted:
                message += " The input may have changed; check the monitor before retrying."
            else:
                message += " No input command was sent."
            return {"ok": False, "message": message}, 409

    @app.route("/hardware/<name>/ir", methods=["GET", "POST"])
    def ir_commands(name):
        cfg = store.read()
        item = ir_learning.device(cfg, name)
        error, connection_error = None, None
        selected_remote = request.values.get("remote", item["settings"].get("remote", ""))
        if request.method == "POST":
            try:
                action = request.form.get("action")
                command = request.form.get("command", "")
                submitted_revision = revision()
                require(submitted_revision == cfg["revision"], "Configuration changed. Reload before continuing.")
                if action in ("learn", "add"):
                    try:
                        frequency = int(request.form.get("frequency", "38000"))
                    except ValueError:
                        raise DeskError("Carrier frequency must be a whole number in Hz.") from None
                    require(20000 <= frequency <= 60000, "Carrier frequency must be 20000–60000 Hz.")
                    ir_learning.add_command(store, submitted_revision, name, command,
                        remote=selected_remote, key=request.form.get("key", ""), learn=action == "learn",
                        frequency=frequency, replace="replace" in request.form)
                    flash("Command saved as unverified. Test one press, then confirm the response."
                          + (" LIRC may take a moment to load the captured code." if action == "learn" else ""))
                elif action in ("test", "verify"):
                    macro = item["settings"].get("commands", {}).get(command)
                    require(macro is not None, "Choose an existing command.")
                    fingerprint = hashlib.sha256(json.dumps(macro, sort_keys=True).encode()).hexdigest()
                    if action == "test":
                        session.pop("ir_test", None)
                        pulses = macro.get("sequence", [])
                        require(len(pulses) == 1 and pulses[0].get("count", 1) == 1,
                                "Single-press testing requires exactly one pulse with count 1. Edit the command first.")
                        pulse = pulses[0]
                        remote = ir_learning.lirc_name(pulse.get("remote", item["settings"].get("remote", "")))
                        key = ir_learning.lirc_name(pulse["key"])
                        with exclusive(store.directory / "scene.lock"):
                            require(store.read()["revision"] == submitted_revision, "Configuration changed. Reload before testing.")
                            require(remote in ir_learning.catalog(cfg),
                                    f"LIRC has not loaded remote {remote}. The saved command has not been sent. "
                                    "For app-learned codes, run the updated Pi installer once to install the "
                                    "learned-code include and desk-ir-reload.path service; then check lircd's journal.")
                            require(key in ir_learning.catalog(cfg, remote),
                                    "Code is not loaded in LIRC yet. Wait a moment and retry; check the IR reload service if it persists.")
                            Hardware(cfg).irsend("SEND_ONCE", remote, key)
                            EventLog(store.directory).emit("transport", "IR learning test sent; physical response unverified",
                                                          remote=remote, key=key, live=True)
                        session["ir_test"] = [name, command, fingerprint]
                        flash("One press sent. Check the device's response, then confirm it below.")
                    else:
                        require(session.get("ir_test") == [name, command, fingerprint],
                                "Test this saved command before verifying it.")
                        require("observed" in request.form, "Confirm you observed the correct physical response.")
                        discrete = "discrete" in request.form
                        require(command not in ("power_on", "power_off") or discrete,
                                "Power on/off commands must select a known state, not toggle it.")
                        def verify(current):
                            saved = ir_learning.device(current, name)["settings"]["commands"][command]
                            saved.update(verified=True, discrete=discrete)
                        store.update(submitted_revision, verify)
                        session.pop("ir_test", None)
                        flash("Command marked verified.")
                else:
                    raise DeskError("Unknown IR action.")
                return redirect(url_for("ir_commands", name=name))
            except (DeskError, OSError) as exc:
                error = str(exc)
        remotes, available_keys = [], []
        try:
            remotes = ir_learning.catalog(cfg)
            if selected_remote and selected_remote in remotes:
                available_keys = ir_learning.catalog(cfg, selected_remote)
        except DeskError as exc:
            connection_error = str(exc)
        return render_template("ir_commands.html", cfg=cfg, name=name, item=item, error=error,
                               connection_error=connection_error, remotes=remotes, available_keys=available_keys,
                               selected_remote=selected_remote, tested=session.get("ir_test", [])), 422 if error else 200

    @app.post("/hardware/<name>/delete")
    def hardware_delete(name):
        store.delete_hardware(revision(), name)
        flash("Hardware deleted.")
        return redirect(url_for("hardware_list"))

    @app.get("/api/map")
    def map_model():
        try:
            return hardware_map.model(store.read())
        except (DeskError, OSError, ValueError):
            return {"error": "Map unavailable. Check the developer console."}, 503

    @app.post("/map/save")
    def map_save():
        try:
            cfg = store.save_hardware_map(revision(), parse("devices"))
            return {"ok": True, "revision": cfg["revision"], "model": hardware_map.model(cfg),
                    "message": "Hardware and map saved."}
        except DeskError as exc:
            return {"ok": False, "error": str(exc) + " Nothing was saved."}, 409

    @app.post("/api/map/run")
    def map_run():
        try:
            result = hardware_map.run(store.read(), request.form)
            return {"ok": True, "result": result, "message": "Commands sent. IR/KVM physical response is unverified."}
        except DeskError as exc:
            return {"ok": False, "error": str(exc)}, 409
        except Exception:
            return {"ok": False, "error": "Input could not complete. Earlier actions may have taken effect; check the developer console before trying again."}, 503

    @app.get("/tasks")
    def tasks():
        return render_template("tasks.html", cfg=store.read())

    @app.route("/tasks/new", methods=["GET", "POST"])
    @app.route("/tasks/<name>/edit", methods=["GET", "POST"])
    def task_edit(name=None):
        cfg = store.read()
        if name is not None and name not in cfg["scenes"]:
            abort(404)
        task = cfg["scenes"].get(name, {"label": "", "steps": [{"kind": "wait", "seconds": 1}]})
        key = next((k for k, v in cfg["keypad"]["bindings"].items() if v == name), "")
        error = None
        if request.method == "POST":
            try:
                task = {"label": request.form.get("label", "").strip(),
                        "keep_active_task": request.form.get("keep_active_task") == "on",
                        **task_appearance(name or request.form.get("id", ""), task),
                        **{field: request.form[field] for field in ("color", "icon") if field in request.form},
                        "steps": task["steps"]}
                task["steps"] = parse("steps")
                if task["keep_active_task"]:
                    task["controls"] = {}
                elif "controls_present" in request.form:
                    task["controls"] = {}
                    for control in CONTROL_NAMES:
                        mode = request.form.get(f"{control}_mode", "")
                        if mode:
                            task["controls"][control] = {"mode": mode,
                                **{key: request.form.get(f"{control}_{key}", "") for key in ("device", "increase", "decrease")}}
                            if control == "dial" and mode == "volume" and "dial_click_sources" in request.form:
                                sources = parse("dial_click_sources")
                                mapping = task["controls"][control]
                                mapping["click"] = {"action": "cycle_volume", "sources": sources}
                                if isinstance(sources, list) and sources and isinstance(sources[0], dict):
                                    for field in ("device", "increase", "decrease"):
                                        mapping[field] = sources[0].get(field, "")
                                validate_mappings({control: mapping})
                            elif control == "dial" and request.form.get("dial_click_action"):
                                task["controls"][control]["click"] = {
                                    "action": request.form["dial_click_action"], "sources": parse("dial_click_sources")}
                else:
                    task["controls"] = cfg["scenes"].get(name, {}).get("controls", {})
                if task["keep_active_task"]:
                    task["key_commands"] = {}
                elif "key_commands_present" in request.form:
                    task["key_commands"] = {}
                    for code in KEYS:
                        command = request.form.get(f"command_{code}")
                        if command:
                            task["key_commands"][code] = command
                            if command == "custom":
                                task["key_commands"][code] = parse(f"macro_{code}")
                            elif command == "ir":
                                task["key_commands"][code] = dict(kind="ir",
                                    device=request.form.get(f"ir_device_{code}", ""),
                                    command=request.form.get(f"ir_command_{code}", ""))
                else:
                    task["key_commands"] = cfg["scenes"].get(name, {}).get("key_commands", {})
                # Retain old saved data, but never interpret media presets as app shortcuts.
                previous_task = cfg["scenes"].get(name, {})
                if "media_shortcuts" in previous_task:
                    task["media_shortcuts"] = previous_task["media_shortcuts"]
                key = request.form.get("key", "")
                store.save_task(revision(), name or request.form.get("id", ""), task, key, creating=name is None)
                flash("Task saved. The numpad uses the new configuration on its next press.")
                return redirect(url_for("tasks"))
            except DeskError as exc:
                error = str(exc)
        return render_template("task_edit.html", cfg=cfg, task=task, name=name, key=key, error=error,
                               reserved_command_keys=reserved_keys(cfg),
                               form_revision=request.form.get("revision", cfg["revision"])), 422 if error else 200

    @app.post("/tasks/<name>/delete")
    def task_delete(name):
        store.delete_task(revision(), name)
        flash("Task and its key mapping deleted.")
        return redirect(url_for("tasks"))

    @app.route("/tasks/<name>/preview", methods=["GET", "POST"])
    def preview(name):
        cfg = store.read()
        if name not in cfg["scenes"]:
            abort(404)
        lines = []
        runner = Runner(cfg, Hardware(cfg), emit=lines.append)
        runner.run(name)
        probed = request.method == "POST"
        passed = []
        problems = runner.issues(name, probe=True, passed=passed) if probed else []
        return render_template("preview.html", cfg=cfg, name=name, task=cfg["scenes"][name], problems=problems,
                               lines=lines, probed=probed, passed=passed, reserved_command_keys=reserved_keys(cfg))

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = store.read()
        if request.method == "POST":
            def edit(config):
                socket = text(request.form.get("ir_socket", ""), "LIRC socket")
                require(socket.startswith("/"), "LIRC socket must be an absolute path.")
                config.setdefault("ir", {})["socket"] = socket
                receiver = request.form.get("ir_receiver", config["ir"].get("receiver", "/dev/desk-ir-rx"))
                ir_learning.receiver_path(receiver)
                config["ir"]["receiver"] = receiver
                token_env = request.form.get("token_env", "SMARTTHINGS_TOKEN")
                require(token_env.replace("_", "").isalnum() and not token_env[0].isdigit(), "Invalid token environment variable name.")
                config.setdefault("smartthings", {})["token_env"] = token_env
                token_file = text(request.form.get("token_file", ""), "token file")
                require(not token_file or token_file.startswith("/"), "OAuth token file must be an absolute path.")
                if token_file:
                    config["smartthings"]["token_file"] = token_file
                else:
                    config["smartthings"].pop("token_file", None)
            store.update(revision(), edit)
            flash("Connection settings saved.")
            return redirect(url_for("settings"))
        return render_template("settings.html", cfg=cfg, numpad=selected_input(cfg["keypad"], scan_usb()),
                               tls_status=tls.status(store.directory, cfg.get("smartthings", {}).get("redirect_uri", "")),
                               oauth_status=oauth.status(cfg.get("smartthings", {})))

    @app.post("/https/configure")
    def https_configure():
        try:
            tls.save(store.directory, request.form)
            flash("HTTPS setup saved. Start a certificate request to get the TXT record to add in Lightsail.")
        except (DeskError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings", _anchor="https"))

    @app.post("/https/action")
    def https_action():
        try:
            tls.start(store.directory, request.form.get("action", ""))
            flash("Certificate operation started. Refresh Settings for progress.")
        except (DeskError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings", _anchor="https"))

    @app.get("/https/status")
    def https_status():
        response = app.make_response(render_template("tls_operation.html", tls_status=tls.status(store.directory)))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/https/control")
    def https_control():
        try:
            tls.control(store.directory, request.form.get("action", ""), request.form.get("id", ""))
            flash("Certificate action recorded. Progress updates below.")
        except (DeskError, OSError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings", _anchor="https"))

    @app.post("/smartthings/oauth/configure")
    def oauth_configure():
        try:
            uri = request.form.get("redirect_uri", "").strip()
            oauth.validate_redirect(uri)
            endpoint = request.form.get("token_endpoint", "/oauth/token")
            require(endpoint in oauth.ENDPOINTS, "Choose a supported SmartThings OAuth endpoint.")
            def edit(cfg):
                cfg.setdefault("smartthings", {}).update(redirect_uri=uri, oauth_token_endpoint=endpoint)
            store.update(revision(), edit)
            allow_oauth_host()
            flash("OAuth setup saved. Connect to SmartThings to authorize your account.")
        except DeskError as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings"))

    @app.post("/smartthings/oauth/connect")
    def oauth_connect():
        try:
            destination = oauth.begin(store, session["csrf"], request.host, revision())
            # Browsers can apply form-action 'self' to the entire POST redirect
            # chain. Finish the local submission before navigating to Samsung.
            return render_template("oauth_authorize.html", destination=destination)
        except (DeskError, OSError) as exc:
            flash(str(exc) if isinstance(exc, DeskError) else "Could not save the authorization session. Check runtime directory permissions.", "error")
            return redirect(url_for("settings"))

    @app.get("/oauth/callback")
    def oauth_callback():
        try:
            require(session.get("authenticated"), "Your login session is missing or expired. Sign in at the registered host and start Connect to SmartThings again.")
            oauth.finish(store, session["csrf"], request.host, request.args)
            flash("SmartThings OAuth connected. Tokens were saved and OAuth is now active. Check the connection below.")
        except (DeskError, OSError) as exc:
            flash(str(exc) if isinstance(exc, DeskError) else "OAuth setup could not be saved. Check runtime directory permissions and start again.", "error")
        # Remove authorization codes from the address bar; never render them.
        return redirect(url_for("settings"), code=303)

    @app.post("/smartthings/oauth/check")
    def oauth_check():
        try:
            cfg = store.read()
            require(cfg["revision"] == revision(), "Configuration changed. Reload before checking OAuth.")
            config = cfg.get("smartthings", {})
            require(config.get("token_file"), "Connect with OAuth before testing the connection.")
            client = Hardware(cfg).smartthings
            refresh = request.form.get("refresh") == "yes"
            if refresh:
                client.tokens.get(force_refresh=True)
            devices = client.devices()
            flash(("OAuth refresh succeeded. " if refresh else "OAuth connection succeeded. ")
                  + f"SmartThings returned {len(devices)} devices. No device commands were sent.")
        except (DeskError, OSError) as exc:
            flash(str(exc) if isinstance(exc, DeskError) else "Could not read or save OAuth tokens. Check token directory permissions.", "error")
        return redirect(url_for("settings"))

    @app.post("/discover/<kind>")
    def discover(kind):
        cfg = store.read()
        try:
            if kind == "input":
                from .keypad import devices
                data = devices()
            elif kind == "smartthings":
                data = Hardware(cfg).smartthings.devices()
            else:
                abort(404)
            return render_template("discovery.html", data=json.dumps(data, indent=2), kind=kind)
        except OSError as exc:
            raise DeskError(f"Discovery failed: {exc}") from exc

    @app.get("/backup")
    def backup():
        raw = json.dumps({"format": "desk-backup-1", "config": store.read()}, indent=2).encode()
        return send_file(io.BytesIO(raw), mimetype="application/json", as_attachment=True, download_name="desk-backup.json")

    @app.post("/restore")
    def restore():
        upload = request.files.get("backup")
        require(upload is not None, "Choose a JSON backup to restore.")
        try:
            incoming = json.load(upload)
            store.restore(revision(), incoming)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise DeskError("Backup is malformed or incompatible. Configuration was not changed.") from exc
        flash("Hardware, tasks, and mappings restored. Local connection and credential settings were kept.")
        return redirect(url_for("dashboard"))

    return app
