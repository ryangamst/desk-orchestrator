"""Commission the tested CH9328 bridge on an installed Pi without sending keys.

Run: sudo /opt/desk-orchestrator/.venv/bin/python -m desk_orchestrator.configure_keyboard
Use --device /dev/serial/by-id/... if more than one USB serial adapter is connected.
"""
import argparse
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
import stat
import subprocess
import sys

from .config_store import ConfigStore
from .core import DeskError, atomic_json, exclusive, require
from .keyboard_transport import serial_devices, serial_module, validate_settings

STATE = Path('/var/lib/desk-orchestrator')
SEED = Path('/etc/desk-orchestrator/desk.toml')
UNITS = ('desk-orchestrator.service', 'desk-web.service', 'desk-https.service')


def choose_device(requested, candidates):
    if requested:
        # Upgrade an explicitly supplied ttyUSB node to its discovered stable alias.
        resolved = Path(requested).resolve()
        return next((item['path'] for item in candidates if Path(item['node']).resolve() == resolved), requested)
    require(len(candidates) == 1, 'Connect exactly one USB serial adapter, or specify --device /dev/serial/by-id/...')
    require(candidates[0]['stable'], 'No stable serial alias found. Specify --device explicitly or configure a udev alias.')
    return candidates[0]['path']


def save_configuration(device, directory=STATE, seed=SEED):
    store = ConfigStore(directory, seed)
    with exclusive(Path(directory) / 'scene.lock'):
        cfg = store.read()
        matches = [(name, item) for name, item in cfg['inventory'].items() if item['method'] == 'usb_hid']
        require(len(matches) == 1, 'Configure one USB keyboard / KVM hardware entry in the app first.')
        name, item = matches[0]
        item = copy.deepcopy(item)
        item['settings'] = dict(transport='ch9328', device=device, baudrate=9600,
                                key_delay=item['settings'].get('key_delay', .08), confirmed=True)
        validate_settings(item['settings'])
        backup = Path(directory) / ('controller.before-ch9328-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        atomic_json(backup, cfg)
        store.save_hardware(cfg['revision'], name, item)
        print(f'CH9328 saved: {device}\nConfiguration backup: {backup}', flush=True)


def run(*args):
    return subprocess.run([str(arg) for arg in args], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', help='serial adapter path; autodetected only when unique and stable')
    parser.add_argument('--save', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.save:
            require(os.geteuid() != 0, 'Save settings as the desk service user.')
            require(args.device, 'A serial device is required.')
            save_configuration(args.device)
            return 0
        require(os.geteuid() == 0, 'Run this commissioning command with sudo after updating the Pi.')
        serial_module()
        device = choose_device(args.device, serial_devices())
        validate_settings(dict(transport='ch9328', device=device))
        require(stat.S_ISCHR(Path(device).stat().st_mode), 'The serial path must identify a character device.')
        # Confirm access without opening the adapter or sending a report.
        for mode in ('-r', '-w'):
            run('runuser', '-u', 'desk', '--', 'test', mode, device)
        dependencies = subprocess.run(['systemctl', 'show', 'desk-orchestrator.service', '-p', 'Requires', '--value'],
                                      check=True, capture_output=True, text=True).stdout.split()
        require('desk-gadget.service' not in dependencies, 'Run the updated Pi updater first to remove the old gadget dependency.')
        active = [unit for unit in UNITS if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0]
        try:
            if active:
                run('systemctl', 'stop', *active)
            run('runuser', '-u', 'desk', '--', sys.executable, '-m',
                'desk_orchestrator.configure_keyboard', '--save', '--device', device)
            run('systemctl', 'disable', '--now', 'desk-gadget.service')
        finally:
            if active:
                run('systemctl', 'start', *active)
        for unit in active:
            run('systemctl', 'is-active', '--quiet', unit)
        print('CH9328 configured. Previously active services restarted. No keyboard command was sent.\n'
              'Test a KVM task from the numpad or app. Media bindings need application keyboard shortcuts.')
        return 0
    except (DeskError, OSError, subprocess.CalledProcessError) as exc:
        print(f'CH9328 setup failed: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
