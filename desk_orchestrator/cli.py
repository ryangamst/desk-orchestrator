import argparse
import asyncio
import json
import getpass
import signal
import sys
import tomllib

from .core import DeskError, Runner, load_config
from .hardware import Hardware
from .keypad import devices, listen, monitor_input
from .smartthings import segment


def parser():
    root = argparse.ArgumentParser(description="Desk scenes. Commands are dry-run unless --live is supplied.")
    root.add_argument("--config", "-c", default="config/desk.example.toml")
    commands = root.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List scenes and numpad bindings")
    plan = commands.add_parser("plan", help="Print a scene and its configuration gaps (no I/O)")
    plan.add_argument("scene")
    run = commands.add_parser("run", help="Run one scene")
    run.add_argument("scene")
    run.add_argument("--live", action="store_true")
    check = commands.add_parser("check", help="Validate setup; optionally probe read-only hardware APIs")
    check.add_argument("scene", nargs="?")
    check.add_argument("--probe", action="store_true")
    commands.add_parser("status", help="Show last execution record (not measured hardware state)")
    commands.add_parser("input-devices", help="List Linux input devices")
    monitor = commands.add_parser("input-monitor", help="Read a selected input briefly without grabbing it or sending commands")
    monitor.add_argument("--device", required=True)
    monitor.add_argument("--gmmk-slider", action="store_true", help="Read raw GMMK slider positions; --device is the numpad's stable keyboard input path")
    monitor.add_argument("--seconds", type=int, default=20, choices=range(1, 61), metavar="1-60")
    keypad = commands.add_parser("listen", help="Listen to the configured numpad")
    keypad.add_argument("--live", action="store_true")
    web = commands.add_parser("web", help="Serve the configuration app using Waitress")
    web.add_argument("--data-dir", default=".runtime/web")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8080)
    web.add_argument("--trusted-host", action="append", default=[])
    web.add_argument("--secure-cookie", action="store_true", help="Require HTTPS for session cookies")
    https = commands.add_parser("https", help="Serve HTTPS with a Lightsail DNS certificate and automatic renewal")
    https.add_argument("--data-dir", default=".runtime/web")
    https.add_argument("--host", default="0.0.0.0")
    https.add_argument("--port", type=int, default=443)
    password = commands.add_parser("web-password", help="Set or reset the web password; restart the web service afterward")
    password.add_argument("--data-dir", default=".runtime/web")
    st = commands.add_parser("smartthings", help="Read-only device/capability discovery")
    st.add_argument("query", choices=["devices", "device", "status", "capability"])
    st.add_argument("identifier", nargs="?")
    st.add_argument("--version", type=int, default=1)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "https":
            from . import tls
            if not tls.installed():
                raise DeskError('Install HTTPS support: pip install ".[web,tls]"')
            tls.serve(args.data_dir, args.config, args.host, args.port)
            return 0
        if args.action in ("web", "web-password"):
            try:
                from .web import create_app, set_password
                from waitress import serve
            except ImportError:
                raise DeskError('Install web support: pip install ".[web]"') from None
            if args.action == "web-password":
                password = getpass.getpass("New controller password (12+ characters): ")
                if password != getpass.getpass("Repeat password: "):
                    raise DeskError("Passwords did not match.")
                set_password(args.data_dir, password)
                print("Password saved. Restart the web service to apply it and expire existing sessions.")
            else:
                if args.host not in ("127.0.0.1", "localhost", "::1") and not args.trusted_host:
                    raise DeskError("LAN access requires --trusted-host with the Pi hostname or IP used in the browser.")
                app = create_app(args.data_dir, args.config, trusted_hosts=["localhost", "127.0.0.1", "[::1]", *args.trusted_host], secure_cookie=args.secure_cookie)
                print(f"Desk configuration app: http://{args.host}:{args.port}", flush=True)
                print(f"Controller config: {app.extensions['desk_store'].path}", flush=True)
                serve(app, host=args.host, port=args.port, threads=4)
            return 0
        if args.action == "input-devices":
            print(json.dumps(devices(), indent=2))
            return 0
        if args.action == "input-monitor":
            if args.gmmk_slider:
                from .gmmk_slider import monitor
                asyncio.run(monitor(args.device, args.seconds))
            else:
                asyncio.run(monitor_input(args.device, args.seconds))
            return 0
        config = load_config(args.config)
        hardware = Hardware(config)
        runner = Runner(config, hardware, emit=lambda text: print(text, flush=True))
        if args.action == "list":
            for name, scene in config["scenes"].items():
                keys = [k for k, value in config.get("keypad", {}).get("bindings", {}).items() if value == name]
                print(f"{name:16} {', '.join(keys):16} {scene.get('label', name)}")
        elif args.action in ("plan", "run"):
            runner.run(args.scene, live=getattr(args, "live", False))
        elif args.action == "check":
            issues = []
            for name in [args.scene] if args.scene else config["scenes"]:
                issues.extend(f"{name}: {issue}" for issue in runner.issues(name, probe=args.probe))
            print("\n".join(issues) if issues else "Checks passed. Physical IR/KVM responses still require verification.")
            return 1 if issues else 0
        elif args.action == "status":
            path = runner.runtime / "status.json"
            print(path.read_text() if path.exists() else "No live scene execution recorded.")
        elif args.action == "listen":
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            asyncio.run(listen(config, runner, live=args.live, reload_config=lambda: load_config(args.config)))
        elif args.action == "smartthings":
            client = hardware.smartthings
            if args.query == "devices":
                data = client.devices()
            else:
                if not args.identifier:
                    raise DeskError("This query requires a device or capability ID.")
                if args.query == "status":
                    data = client.status(args.identifier)
                elif args.query == "device":
                    data = client.call("GET", f"/v1/devices/{segment(args.identifier)}")
                else:
                    data = client.call("GET", f"/v1/capabilities/{segment(args.identifier)}/{args.version}")
            print(json.dumps(data, indent=2))
        return 0
    except (DeskError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
        return 130
