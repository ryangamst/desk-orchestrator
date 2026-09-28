#!/usr/bin/env python3
"""Bundle local changes or update an existing Desk Orchestrator Pi installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime
import fcntl
import grp
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid

SOURCE = Path(__file__).resolve().parent.parent
APP = Path('/opt/desk-orchestrator')
STATE = Path('/var/lib/desk-orchestrator')
BACKUPS = Path('/var/backups/desk-orchestrator')
LOCK = Path('/run/lock/desk-setup.lock')  # Also used by setup_pi.py.
GMMK_RULE = Path('/etc/udev/rules.d/99-desk-gmmk.rules')
KEYBOARD_UNIT = Path('/etc/systemd/system/desk-orchestrator.service')
BASE_UNITS = ('desk-web.service', 'desk-orchestrator.service')
UNITS = (*BASE_UNITS, 'desk-https.service')
EXTRAS = 'web,keypad,tls,serial'
DIRECTORIES = ('desk_orchestrator', 'docs')
FILES = ('pyproject.toml', 'README.md', 'config/desk.example.toml',
         'deploy/setup-pi.sh', 'deploy/setup_pi.py', 'deploy/update-pi.sh',
         'deploy/update_pi.py', 'deploy/setup-gadget.sh', 'deploy/desk-gadget.service',
         'deploy/desk-orchestrator.service', 'deploy/desk-web.service',
         'deploy/desk-ir-reload.path', 'deploy/desk-ir-reload.service',
         'deploy/99-desk-hid.rules', 'deploy/99-desk-gmmk.rules', 'deploy/web-managed-controller.conf',
         'deploy/credentials.env.example', 'deploy/setup_https.py')
SMOKE = '''
from desk_orchestrator.web import create_app
app = create_app('/var/lib/desk-orchestrator', '/etc/desk-orchestrator/desk.toml')
app.testing = True
with app.test_client() as client:
    with client.session_transaction() as session:
        session['authenticated'] = True
    for path in ('/health', '/console', '/api/events', '/api/numpad', '/settings', '/hardware', '/static/favicon.svg', '/static/studio.css', '/static/virtual_numpad.js', '/api/map', '/static/hardware_map.js', '/static/hardware_map.css', '/static/oauth_authorize.js', '/static/tls_settings.js', '/https/status'):
        response = client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f'{path} failed with HTTP {response.status_code}')
    for name, item in app.extensions['desk_store'].read()['inventory'].items():
        if item.get('method') == 'smartthings':
            if client.get(f'/hardware/{name}/edit').status_code != 200:
                raise RuntimeError('SmartThings input editor failed')
from desk_orchestrator import tls
if not tls.installed():
    raise RuntimeError('HTTPS dependencies are missing')
print('Authenticated web, OAuth/HTTPS settings, hardware editor, and event storage checks passed.')
'''


class UpdateError(Exception):
    pass


def run(*args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def allowed(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        return False
    if any(part.startswith('.') or part == '__pycache__' for part in path.parts):
        return False
    if path.suffix in {'.pyc', '.pyo'}:
        return False
    # Only package source/assets, documentation, and explicit deployment files.
    if path.parts[0] == 'desk_orchestrator':
        return path.suffix in {'.py', '.html', '.css', '.js', '.svg'}
    if path.parts[0] == 'docs':
        return path.suffix == '.md'
    return str(path) in FILES


def bundle(destination, source=SOURCE):
    entries = []
    for name in (*DIRECTORIES, *FILES):
        path = source / name
        if not path.exists() or path.is_symlink():
            raise UpdateError(f'Missing or symlinked source: {path}')
        for entry in sorted(path.rglob('*')) if path.is_dir() else [path]:
            relative = entry.relative_to(source).as_posix()
            if entry.is_symlink():
                raise UpdateError(f'Symlinked source is not supported: {entry}')
            if entry.is_file() and allowed(relative):
                entries.append((entry, relative))
    with tarfile.open(destination, 'w:gz') as archive:
        for entry, relative in entries:
            archive.add(entry, arcname=relative, recursive=False)
    print(f'Bundled {len(entries)} source files; local state, credentials, and virtualenv excluded.', flush=True)


def unpack(archive_path, destination):
    # Do not trust archive ownership, permissions, symlinks, or paths as root.
    with tarfile.open(archive_path, 'r:gz') as archive:
        seen = set()
        total = 0
        for member in archive:
            if not member.isfile() or not allowed(member.name) or member.name in seen:
                raise UpdateError(f'Unsupported/duplicate archive member: {member.name}')
            seen.add(member.name)
            total += member.size
            if total > 100 * 1024 * 1024 or len(seen) > 10000:
                raise UpdateError('Source archive exceeds the 100 MB / 10,000 file limit.')
            output = destination / member.name
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as reader, output.open('wb') as writer:
                shutil.copyfileobj(reader, writer)
            output.chmod(0o644)
    for required in (*FILES, 'desk_orchestrator/__init__.py', 'desk_orchestrator/templates/console.html'):
        if not (destination / required).is_file():
            raise UpdateError(f'Incomplete update: missing {required}')


def managed_units():
    result = subprocess.run(['systemctl', 'show', 'desk-https.service', '-p', 'LoadState', '--value'],
                            capture_output=True, text=True)
    return [*BASE_UNITS, *(['desk-https.service'] if result.stdout.strip() == 'loaded' else [])]


def active_units():
    return [unit for unit in managed_units() if subprocess.run(
        ['systemctl', 'is-active', '--quiet', unit]).returncode == 0]


def check_host():
    if os.geteuid() != 0:
        raise UpdateError('Run the Pi updater with sudo.')
    model = Path('/proc/device-tree/model')
    if not model.exists() or not model.read_text().startswith('Raspberry Pi 4 Model B'):
        raise UpdateError('Run installation updates on the Raspberry Pi 4, not the development PC.')


def check_installation():
    check_host()
    if APP.is_symlink() or not (APP / '.venv/bin/python').exists():
        raise UpdateError('Expected an existing installation at /opt/desk-orchestrator; run setup-pi.sh first.')
    for name in ('controller.json', 'web-auth.json'):
        if not (STATE / name).is_file():
            raise UpdateError(f'Missing {STATE / name}; finish initial setup first.')
    for unit in BASE_UNITS:
        run('systemctl', 'cat', unit, stdout=subprocess.DEVNULL)


def replace_sources(source):
    # Replace the whole package so deleted/renamed Python files and assets cannot linger.
    for name in (*DIRECTORIES, *FILES):
        target = APP / name
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise UpdateError(f'Unexpected installation symlink: {target}')
        if name in DIRECTORIES:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source / name, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        for item in [target, *target.rglob('*')] if target.is_dir() else [target]:
            item.chmod(0o755 if item.is_dir() or item.suffix == '.sh' else 0o644)


def wait_for_web():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        try:
            with opener.open('http://127.0.0.1:8080/login', timeout=2) as response:
                if response.status == 200 and b'name="password"' in response.read():
                    run('systemctl', 'is-active', '--quiet', 'desk-web.service')
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise UpdateError('Web app did not respond on 127.0.0.1:8080. Check journalctl -u desk-web.')


def restart(units):
    for unit in units:
        run('systemctl', 'start', unit)
    if 'desk-web.service' in units:
        wait_for_web()
    if 'desk-https.service' in units:
        wait_for_https()
    # Catch processes which start and immediately fail; systemd start alone isn't enough.
    if units:
        time.sleep(2)
    for unit in units:
        run('systemctl', 'is-active', '--quiet', unit)


def wait_for_https():
    # A newly installed listener may legitimately be waiting for its first cert.
    if not (STATE / 'tls/production/config/live/desk/fullchain.pem').exists():
        print('HTTPS service is waiting for certificate issuance in Settings.', flush=True)
        return
    config = json.loads((STATE / 'tls/settings.json').read_text())
    domain = config.get('domain', '')
    if not re.fullmatch(r'[a-z0-9.-]+', domain) or '..' in domain:
        raise UpdateError('Invalid HTTPS hostname in TLS settings.')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        try:
            with opener.open(f'https://{domain}/login', timeout=2) as response:
                if response.status == 200 and b'name="password"' in response.read():
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise UpdateError('HTTPS login did not respond with a trusted certificate. Check local DNS, certificate status, and journalctl -u desk-https.')


def snapshot(backup, units):
    backup.mkdir(mode=0o700)
    shutil.copytree(APP, backup / 'app', symlinks=True)
    run('tar', '-czf', backup / 'state.tar.gz', '-C', STATE, '.')
    (backup / 'services.json').write_text(json.dumps(units))
    (backup / 'gmmk-rule.json').write_text(json.dumps(GMMK_RULE.read_text() if GMMK_RULE.exists() else None))
    if KEYBOARD_UNIT.is_symlink():
        raise UpdateError('Refusing to replace symlinked controller service.')
    (backup / 'keyboard-unit.json').write_text(json.dumps(KEYBOARD_UNIT.read_text() if KEYBOARD_UNIT.exists() else None))


def configure_serial_access():
    try:
        grp.getgrnam('dialout')
    except KeyError:
        run('groupadd', '--system', 'dialout')
    run('usermod', '-aG', 'dialout', 'desk')
    # Preserve local service customizations; remove only the old hard gadget dependency.
    if KEYBOARD_UNIT.exists():
        lines = []
        for line in KEYBOARD_UNIT.read_text().splitlines():
            if line.startswith('Requires='):
                dependencies = [value for value in line.partition('=')[2].split() if value != 'desk-gadget.service']
                if not dependencies:
                    continue
                line = 'Requires=' + ' '.join(dependencies)
            lines.append(line)
        KEYBOARD_UNIT.write_text('\n'.join(lines) + '\n')
    run('systemctl', 'daemon-reload')


def set_gmmk_rule(contents):
    if contents is None:
        GMMK_RULE.unlink(missing_ok=True)
    else:
        GMMK_RULE.parent.mkdir(parents=True, exist_ok=True)
        GMMK_RULE.write_text(contents)
        GMMK_RULE.chmod(0o644)
    run('udevadm', 'control', '--reload-rules')
    run('udevadm', 'trigger', '--subsystem-match=hidraw')
    run('udevadm', 'settle', '--timeout=30')


def restore(backup, units):
    run('systemctl', 'stop', *managed_units())
    if APP.exists():
        shutil.rmtree(APP)
    shutil.copytree(backup / 'app', APP, symlinks=True)
    rule_backup = backup / 'gmmk-rule.json'
    if rule_backup.exists():
        set_gmmk_rule(json.loads(rule_backup.read_text()))
    unit_backup = backup / 'keyboard-unit.json'
    if unit_backup.exists():
        contents = json.loads(unit_backup.read_text())
        if contents is not None:
            KEYBOARD_UNIT.write_text(contents)
            run('systemctl', 'daemon-reload')
    restart(units)


def apply_update(source, wheels, backup):
    units = active_units()
    have_backup = False
    changed = False
    try:
        # Include an installed HTTPS listener and any inactive unit waiting to retry.
        run('systemctl', 'stop', *managed_units())
        snapshot(backup, units)
        have_backup = True
        print(f'Backup ready: {backup}', flush=True)
        changed = True
        replace_sources(source)
        package_wheels = list(wheels.glob('desk_orchestrator-*.whl'))
        if len(package_wheels) != 1:
            raise UpdateError('Expected exactly one desk-orchestrator wheel.')
        run(APP / '.venv/bin/python', '-m', 'pip', 'install', '--no-input', '--no-index',
            '--find-links', wheels, '--force-reinstall', f'{package_wheels[0]}[{EXTRAS}]')
        run(APP / '.venv/bin/python', '-m', 'pip', 'check')
        run('runuser', '-u', 'desk', '--', APP / '.venv/bin/python', '-c', SMOKE, cwd='/')
        set_gmmk_rule((APP / 'deploy/99-desk-gmmk.rules').read_text())
        configure_serial_access()
        restart(units)
    except BaseException as error:
        print(f'Update error: {error}', file=sys.stderr, flush=True)
        if have_backup and changed:
            print(f'Update failed. Restoring previous application from {backup}...', flush=True)
            restore(backup, units)
            print('Previous application restored. Configuration and diagnostics were not reset.', flush=True)
        else:
            restart(units)
        raise
    print(f'Update complete. Previous application and state snapshot: {backup}', flush=True)
    if 'desk-web.service' not in units:
        print('Web service was stopped before the update and remains stopped. Start it with sudo systemctl start desk-web.')
    if 'desk-orchestrator.service' not in units:
        print('Numpad listener was not running and remains stopped. No live control was enabled.')


@contextmanager
def update_lock():
    with LOCK.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError('Another setup or update is running.') from None
        yield


def update(archive):
    check_installation()
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUPS.chmod(0o700)
    with update_lock(), tempfile.TemporaryDirectory(prefix='desk-update-root-', dir='/var/tmp') as temporary:
        staging = Path(temporary)
        # Snapshot the upload before reading it as root; all further work is private.
        shutil.copyfile(archive, staging / 'source.tar.gz')
        source, wheels = staging / 'source', staging / 'wheels'
        source.mkdir()
        wheels.mkdir()
        unpack(staging / 'source.tar.gz', source)
        print('Preparing Python dependencies while existing services keep running...', flush=True)
        run(APP / '.venv/bin/python', '-m', 'pip', 'wheel', '--no-input',
            '--wheel-dir', wheels, f'{source}[{EXTRAS}]')
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        apply_update(source, wheels, BACKUPS / (stamp + '-' + uuid.uuid4().hex[:8]))


def rollback(name):
    check_host()
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', name):
        raise UpdateError('Use the backup directory name printed by the updater.')
    backup = BACKUPS / name
    if backup.is_symlink() or not (backup / 'services.json').is_file() or not (backup / 'app').is_dir():
        raise UpdateError(f'No complete backup at {backup}')
    units = json.loads((backup / 'services.json').read_text())
    if not isinstance(units, list) or any(unit not in UNITS for unit in units):
        raise UpdateError('Invalid service list in backup.')
    if APP.is_symlink():
        raise UpdateError('Refusing to replace a symlinked application directory.')
    with update_lock():
        restore(backup, units)
    print(f'Restored application from {backup}. Saved settings and diagnostics were retained.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--bundle', type=Path, help='create a deployable archive on the development computer')
    modes.add_argument('--archive', type=Path, help='apply a source archive on the Pi (requires sudo)')
    modes.add_argument('--rollback', metavar='BACKUP_NAME', help='restore code/dependencies from a retained backup on the Pi')
    args = parser.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise UpdateError('Python 3.11+ is required.')
        if args.bundle:
            bundle(args.bundle)
        else:
            # All deployed Python files must be readable by the desk service user.
            os.umask(0o022)
            if args.rollback:
                rollback(args.rollback)
            else:
                update(args.archive.resolve())
        return 0
    except (UpdateError, OSError, subprocess.CalledProcessError, tarfile.TarError, ValueError, KeyboardInterrupt) as error:
        print(f'Update failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
