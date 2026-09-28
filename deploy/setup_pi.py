#!/usr/bin/env python3
"""Unattended Raspberry Pi 4 installer; uses only the system Python standard library."""
from __future__ import annotations

import argparse
import configparser
import datetime
import fcntl
import grp
import io
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

SOURCE = Path(__file__).resolve().parent.parent
APP = Path('/opt/desk-orchestrator')
ETC = Path('/etc/desk-orchestrator')
STATE = Path('/var/lib/desk-orchestrator')
SYSTEMD = Path('/etc/systemd/system')
BEGIN = '# BEGIN DESK ORCHESTRATOR'
END = '# END DESK ORCHESTRATOR'
OVERLAYS = ('dtoverlay=gpio-ir,gpio_pin=18',
            'dtoverlay=gpio-ir-tx,gpio_pin=17',
            'dtoverlay=dwc2,dr_mode=peripheral')
FILES = ('pyproject.toml', 'README.md', 'desk_orchestrator', 'docs',
         'config/desk.example.toml', 'deploy/setup-pi.sh', 'deploy/setup_pi.py',
         'deploy/update-pi.sh', 'deploy/update_pi.py',
         'deploy/setup-gadget.sh', 'deploy/desk-gadget.service',
         'deploy/desk-orchestrator.service', 'deploy/desk-web.service',
         'deploy/desk-ir-reload.path', 'deploy/desk-ir-reload.service',
         'deploy/99-desk-hid.rules', 'deploy/99-desk-gmmk.rules', 'deploy/web-managed-controller.conf',
         'deploy/credentials.env.example', 'deploy/setup_https.py')


class SetupError(Exception):
    pass


def run(*args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


def boot_contents(path: Path, transport='gadget'):
    """Consolidate our overlays into an unconditional, repeatable final block."""
    original = path.read_text()
    if original.count(BEGIN) != original.count(END) or original.count(BEGIN) > 1:
        raise SetupError(f'Malformed managed block in {path}; repair it before setup.')
    base = re.sub(re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n?', '', original,
                  flags=re.S)
    if BEGIN in base or END in base:
        raise SetupError(f'Malformed managed block in {path}.')

    def scan(file, content, inherited='all', stack=()):
        if file in stack:
            raise SetupError(f'Circular firmware include: {file}')
        scope = inherited
        lines = []
        for line in content.splitlines():
            setting = line.split('#', 1)[0].strip()
            if setting.startswith('[') and setting.endswith(']'):
                scope = setting[1:-1]
            # Known filters which cannot select a Pi 4. Unknown filters are
            # conservatively checked as potentially active.
            relevant = scope not in {'none', 'pi0', 'pi0w', 'pi1', 'pi2', 'pi3',
                                     'pi3+', 'pi5', 'cm4', 'cm4s', 'cm5'}
            if setting.startswith('include ') and relevant:
                included = (file.parent / setting.split(None, 1)[1]).resolve()
                if not included.is_file():
                    raise SetupError(f'Cannot inspect firmware include {included}')
                scan(included, included.read_text(), scope, (*stack, file))
            compact = re.sub(r'\s+', '', setting)
            if transport == 'ch9328' and (compact.startswith('dtoverlay=dwc') or compact == 'otg_mode=1'):
                lines.append(line)  # USB configuration outside our block belongs to the user.
                continue
            if relevant and re.match(r'dtoverlay=(gpio-ir|gpio-ir-tx|dwc2)(,|$)', compact):
                if compact not in OVERLAYS or file != path:
                    raise SetupError(f'Conflicting/external overlay in {file}: {setting}. '
                                     'Remove it or consolidate the required overlays in config.txt.')
                # Compatible top-level declarations move to the managed block.
                continue
            if relevant and (compact == 'otg_mode=1' or compact.startswith('dtoverlay=dwc-otg')):
                raise SetupError(f'USB host-mode conflict in {file}: {setting}; disable it first.')
            lines.append(line)
        return lines

    output = scan(path, base)
    overlays = OVERLAYS if transport == 'gadget' else OVERLAYS[:2]
    block = '\n'.join((BEGIN, '[all]', *overlays, END))
    return '\n'.join(output).rstrip() + '\n\n' + block + '\n'


def lirc_contents(original):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(original)
    if not config.has_section('lircd'):
        config.add_section('lircd')
    config['lircd']['driver'] = 'default'
    config['lircd']['device'] = '/dev/desk-ir-tx'
    config['lircd']['output'] = '/run/lirc/lircd'
    result = io.StringIO()
    config.write(result)
    return result.getvalue()


def write_file(path, content, mode=0o644, uid=0, gid=0, backup=True):
    """Atomic replacement, with a private backup beside each changed existing file."""
    path = Path(path)
    if path.is_symlink():
        raise SetupError(f'Refusing to replace symlink: {path}')
    if path.exists() and path.read_text() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and backup:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        saved = path.with_name(path.name + '.desk-backup-' + stamp)
        shutil.copyfile(path, saved)
        saved.chmod(0o600)
        print(f'Backup: {saved}', flush=True)
    fd, temporary = tempfile.mkstemp(prefix='.desk-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True


def preflight(transport='gadget'):
    if sys.version_info < (3, 11):
        raise SetupError('Python 3.11+ is required. Use a current Raspberry Pi OS or Ubuntu Server image.')
    model_file = Path('/proc/device-tree/model')
    model = model_file.read_text().rstrip('\0') if model_file.exists() else ''
    if not model.startswith('Raspberry Pi 4 Model B'):
        raise SetupError(f'This installer requires a Raspberry Pi 4 Model B; detected {model or "non-Pi host"}.')
    os_info = dict(re.findall(r'^(\w+)=["\']?([^"\'\n]+)', Path('/etc/os-release').read_text(), re.M))
    if os_info.get('ID') not in {'raspbian', 'debian', 'ubuntu'}:
        raise SetupError('This installer requires Raspberry Pi OS or Ubuntu (apt/systemd).')
    for command in ('apt-get', 'systemctl', 'runuser', 'useradd', 'usermod', 'groupadd', 'udevadm'):
        if not shutil.which(command):
            raise SetupError(f'Required system command missing: {command}')
    if not Path('/run/systemd/system').is_dir():
        raise SetupError('Boot the Pi normally with systemd before running setup.')
    boot = next((p for p in (Path('/boot/firmware/config.txt'), Path('/boot/config.txt')) if p.is_file()), None)
    if boot is None:
        raise SetupError('Cannot find /boot/firmware/config.txt or /boot/config.txt.')
    for overlay in (('gpio-ir', 'gpio-ir-tx', 'dwc2') if transport == 'gadget' else ('gpio-ir', 'gpio-ir-tx')):
        if not (boot.parent / 'overlays' / (overlay + '.dtbo')).is_file():
            raise SetupError(f'Missing firmware overlay {overlay}. Install the Pi kernel/firmware first.')
    content = boot_contents(boot, transport)
    for path in Path('/sys/kernel/config/usb_gadget').glob('*') if transport == 'gadget' else ():
        if path.name != 'desk_keyboard':
            raise SetupError(f'Another USB gadget exists: {path}; resolve this before setup.')
    for module in Path('/sys/module').glob('g_*') if transport == 'gadget' else ():
        raise SetupError(f'Another USB gadget driver is loaded: {module.name}; disable it and reboot first.')
    if transport == 'gadget' and Path('/dev/hidg0').exists() and not Path('/sys/kernel/config/usb_gadget/desk_keyboard/functions/hid.usb0').exists():
        raise SetupError('/dev/hidg0 is already owned by another gadget.')
    for name in FILES:
        source = SOURCE / name
        if not source.exists():
            raise SetupError(f'Incomplete project copy: missing {source}')
        for item in [source, *source.rglob('*')] if source.is_dir() else [source]:
            if item.is_symlink():
                raise SetupError(f'Project installation sources must not contain symlinks: {item}')
    try:
        account = pwd.getpwnam('desk')
    except KeyError:
        pass
    else:
        if account.pw_uid == 0 or account.pw_dir != str(STATE):
            raise SetupError('Existing desk user is not this project’s service account (home must be /var/lib/desk-orchestrator).')
    print(f'Preflight passed: {model}; {os_info.get("PRETTY_NAME", "Debian family")}; Python {sys.version.split()[0]}', flush=True)
    return boot, content


def install_project():
    APP.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        target = APP / name
        for parent in (APP, *target.relative_to(APP).parents):
            candidate = parent if parent.is_absolute() else APP / parent
            if candidate.is_symlink():
                raise SetupError(f'Installation target contains a symlink: {candidate}')
        for item in [target, *target.rglob('*')] if target.is_dir() else [target]:
            if item.is_symlink():
                raise SetupError(f'Installation target contains a symlink: {item}')
    if SOURCE != APP:
        for name in FILES:
            source, target = SOURCE / name, APP / name
            if target.is_symlink():
                raise SetupError(f'Installation target is a symlink: {target}')
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'desk.toml'))
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                target.parent.chmod(0o755)
                shutil.copyfile(source, target)
    # Do not copy development .venv, .runtime, passwords, or local credentials.
    for name in FILES:
        target = APP / name
        for item in [target, *target.rglob('*')] if target.is_dir() else [target]:
            if item.is_symlink():
                raise SetupError(f'Installation target contains a symlink: {item}')
            os.chown(item, 0, 0)
            item.chmod(0o755 if item.is_dir() or item.suffix == '.sh' else 0o644)
    APP.chmod(0o755)
    os.chown(APP, 0, 0)
    # The root-owned venv must remain readable/executable by the desk user.
    old_mask = os.umask(0o022)
    try:
        run(sys.executable, '-m', 'venv', APP / '.venv')
        run(APP / '.venv/bin/python', '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check', f'{APP}[web,keypad,tls,serial]')
        run(APP / '.venv/bin/python', '-m', 'pip', 'check')
    finally:
        os.umask(old_mask)


def initialize_web(password_file):
    # Run app initialization with the service identity. Password travels on stdin,
    # never in process arguments, service EnvironmentFile, or installer output.
    password = ''
    if not (STATE / 'web-auth.json').exists():
        saved = ETC / 'initial-web-password.txt'
        if password_file:
            password = password_file.read_text().rstrip('\r\n')
        elif saved.exists():
            password = saved.read_text().rstrip('\r\n')
        else:
            password = secrets.token_urlsafe(24)
            write_file(saved, password + '\n', mode=0o600, backup=False)
        if len(password) < 12:
            raise SetupError('The initial web password must contain at least 12 characters.')
    run('runuser', '-u', 'desk', '--', APP / '.venv/bin/python', '-c',
        'import sys; from pathlib import Path; from desk_orchestrator.web import set_password, create_app; '
        f'd = Path({str(STATE)!r}); p = sys.stdin.read(); '
        'set_password(d, p) if not (d / "web-auth.json").exists() else None; '
        f'create_app(d, {str(ETC / "desk.toml")!r})', input=password, text=True, cwd='/')


def wait_for_web():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(30):
        try:
            with opener.open('http://127.0.0.1:8080/login', timeout=2) as response:
                if response.status == 200 and b'name="password"' in response.read():
                    return
        except OSError:
            pass
        time.sleep(1)
    raise SetupError('Web app did not become ready. Run: journalctl -u desk-web -n 50')


def install(args, boot, boot_text):
    transport = getattr(args, 'keyboard_transport', 'gadget')
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', PIP_NO_INPUT='1')
    print('Installing OS dependencies...', flush=True)
    run('apt-get', 'update', env=env)
    run('apt-get', 'install', '-y', '-o', 'Dpkg::Options::=--force-confdef',
        '-o', 'Dpkg::Options::=--force-confold', 'python3-venv', 'python3-dev', 'build-essential',
        'lirc', 'ir-keytable', 'ca-certificates', env=env)
    try:
        grp.getgrnam('input')
    except KeyError:
        run('groupadd', '--system', 'input')
    try:
        pwd.getpwnam('desk')
    except KeyError:
        run('useradd', '--system', '--user-group', '--home-dir', STATE,
            '--shell', '/usr/sbin/nologin', 'desk')
    try:
        grp.getgrnam('dialout')
    except KeyError:
        run('groupadd', '--system', 'dialout')
    run('usermod', '-aG', 'input,dialout', 'desk')
    account, group = pwd.getpwnam('desk'), grp.getgrnam('desk')
    for directory, mode, uid in ((ETC, 0o750, 0), (STATE, 0o700, account.pw_uid)):
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, uid, group.gr_gid)
        directory.chmod(mode)
    # Record a pending reboot before touching firmware. It survives interrupted
    # installs and is cleared only after a different kernel boot ID is observed.
    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    pending = ETC / 'setup-reboot-required'
    if boot.read_text() != boot_text:
        write_file(pending, boot_id + '\n', mode=0o600, backup=False)
        write_file(boot, boot_text)
    reboot_needed = pending.exists() and pending.read_text().strip() == boot_id
    if pending.exists() and not reboot_needed:
        pending.unlink()

    resume_listener = ETC / 'setup-resume-listener'
    listener_was_active = active('desk-orchestrator.service')
    if listener_was_active:
        write_file(resume_listener, 'Resume previously active listener after setup.\n', mode=0o600, backup=False)
        run('systemctl', 'stop', 'desk-orchestrator.service')
    if active('desk-web.service'):
        run('systemctl', 'stop', 'desk-web.service')
    resume_https = ETC / 'setup-resume-https'
    if active('desk-https.service'):
        write_file(resume_https, 'Resume previously active HTTPS service after setup.\n', mode=0o600, backup=False)
    if resume_https.exists() or (SYSTEMD / 'desk-https.service').exists():
        run('systemctl', 'stop', 'desk-https.service')
    print('Installing project and Python dependencies...', flush=True)
    install_project()
    if not (ETC / 'desk.toml').exists():
        seed = (APP / 'config/desk.example.toml').read_text().replace(
            'directory = "../.runtime"', f'directory = "{STATE}"', 1)
        write_file(ETC / 'desk.toml', seed, mode=0o640, gid=group.gr_gid)
    if not (ETC / 'credentials.env').exists():
        write_file(ETC / 'credentials.env', (APP / 'deploy/credentials.env.example').read_text(),
                   mode=0o640, gid=group.gr_gid)
    initialize_web(args.password_file)

    print('Configuring LIRC, USB gadget, and system services...', flush=True)
    options = Path('/etc/lirc/lirc_options.conf')
    write_file(options, lirc_contents(options.read_text()))
    run('systemctl', 'cat', 'lircd.socket', stdout=subprocess.DEVNULL)
    write_file(SYSTEMD / 'lircd.socket.d/desk.conf',
               '[Socket]\nSocketGroup=desk\nSocketMode=0660\n')
    write_file(SYSTEMD / 'lircd.service.d/desk.conf',
               '[Unit]\nRequires=dev-desk\\x2dir\\x2dtx.device\n'
               'After=dev-desk\\x2dir\\x2dtx.device\n\n[Service]\nTimeoutStartSec=30\n')
    learned = STATE / 'ir-codes'
    learned.mkdir(parents=True, exist_ok=True)
    os.chown(learned, account.pw_uid, group.gr_gid)
    learned.chmod(0o700)
    write_file(Path('/etc/lirc/lircd.conf.d/desk-learned.conf'),
               'include "/var/lib/desk-orchestrator/ir-codes/*.conf"\n')
    # Make the stable symlink available to systemd's device dependency tracking.
    write_file(Path('/etc/udev/rules.d/99-desk-ir.rules'),
               'SUBSYSTEM=="lirc", KERNEL=="lirc[0-9]*", DRIVERS=="gpio-ir-tx", '
               'SYMLINK+="desk-ir-tx", TAG+="systemd", ENV{SYSTEMD_ALIAS}="/dev/desk-ir-tx"\n'
               'SUBSYSTEM=="lirc", KERNEL=="lirc[0-9]*", DRIVERS=="gpio_ir_recv", '
               'SYMLINK+="desk-ir-rx", GROUP="desk", MODE="0660"\n')
    write_file(Path('/etc/udev/rules.d/99-desk-hid.rules'), (APP / 'deploy/99-desk-hid.rules').read_text())
    write_file(Path('/etc/udev/rules.d/99-desk-gmmk.rules'), (APP / 'deploy/99-desk-gmmk.rules').read_text())
    for unit in ('desk-gadget.service', 'desk-web.service', 'desk-orchestrator.service',
                 'desk-ir-reload.path', 'desk-ir-reload.service'):
        write_file(SYSTEMD / unit, (APP / 'deploy' / unit).read_text())
    write_file(SYSTEMD / 'desk-orchestrator.service.d/web-managed.conf',
               (APP / 'deploy/web-managed-controller.conf').read_text())
    run('systemctl', 'daemon-reload')
    run('udevadm', 'control', '--reload-rules')
    for subsystem in ('lirc', 'hidg', 'hidraw'):
        run('udevadm', 'trigger', '--subsystem-match=' + subsystem)
    run('udevadm', 'settle', '--timeout=30')
    if transport == 'gadget':
        run('systemctl', 'enable', 'desk-web.service', 'desk-gadget.service', 'lircd.socket', 'lircd.service')
    else:
        run('systemctl', 'disable', '--now', 'desk-gadget.service')
        run('systemctl', 'enable', 'desk-web.service', 'lircd.socket', 'lircd.service')
    # Restart the socket together with lircd to apply access permissions reliably.
    run('systemctl', 'stop', 'lircd.service', 'lircd.socket')
    run('systemctl', 'start', 'lircd.socket')
    run('systemctl', 'enable', '--now', 'desk-ir-reload.path')
    run('systemctl', 'restart', 'desk-web.service')
    wait_for_web()
    if resume_https.exists():
        run('systemctl', 'start', 'desk-https.service')
        run('systemctl', 'is-active', '--quiet', 'desk-https.service')
        resume_https.unlink()
    if not reboot_needed:
        if not Path('/dev/desk-ir-tx').exists():
            raise SetupError('IR transmitter missing after overlays were configured. Reboot, then rerun setup.')
        run('systemctl', 'start', 'lircd.service', *(['desk-gadget.service'] if transport == 'gadget' else []))
        run('runuser', '-u', 'desk', '--', 'irsend', '--device=/run/lirc/lircd', 'LIST', '', '')
        run('udevadm', 'settle', '--timeout=30')
        if transport == 'gadget':
            run('runuser', '-u', 'desk', '--', 'test', '-w', '/dev/hidg0')
    if resume_listener.exists() and not reboot_needed:
        run('systemctl', 'start', 'desk-orchestrator.service')
        resume_listener.unlink()
    print('\nInstallation complete. Web app is running on 127.0.0.1:8080.\n'
          'From your desktop: ssh -L 8080:127.0.0.1:8080 YOUR_PI_USER@YOUR_PI_HOST\n'
          'Then open http://127.0.0.1:8080', flush=True)
    if (ETC / 'initial-web-password.txt').exists():
        print(f'Initial password: sudo cat {ETC}/initial-web-password.txt (preserved on reruns).')
    print('Existing credentials, web password, learned IR codes, and controller settings were preserved.\n'
          'Select the numpad in the web app and complete docs/commissioning.md.\n'
          'The installer never enables a new live numpad listener. After commissioning:\n'
          '  sudo systemctl enable --now desk-orchestrator.service')
    print('HTTPS dependencies are installed. To enable HTTPS,\n'
          'run sudo python3 deploy/setup_https.py --user desk --install.\n'
          'Configure manual DNS certificates in Settings; see docs/https-lightsail.md.')
    if reboot_needed:
        print('A reboot is required for boot overlays. Hardware services start at boot.\n'
              'After reboot, rerun this command to verify hardware services and LIRC access.', flush=True)
    if args.reboot and reboot_needed:
        run('systemctl', 'reboot')
    elif reboot_needed:
        print('Reboot when ready: sudo reboot')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read-only platform, firmware, and conflict checks')
    parser.add_argument('--reboot', action='store_true', help='automatically reboot after success if overlays changed')
    parser.add_argument('--password-file', type=Path, help='initial password file (12+ characters); ignored if already initialized')
    parser.add_argument('--keyboard-transport', choices=('gadget', 'ch9328'),
                        help='keyboard output; defaults to saved configuration, then gadget')
    args = parser.parse_args(argv)
    try:
        if args.keyboard_transport is None:
            saved = STATE / 'controller.json'
            args.keyboard_transport = json.loads(saved.read_text()).get('kvm', {}).get('transport', 'gadget') if saved.exists() else 'gadget'
        if args.keyboard_transport not in ('gadget', 'ch9328'):
            raise SetupError('Saved keyboard transport is invalid; choose --keyboard-transport gadget or ch9328.')
        boot, content = preflight(args.keyboard_transport)
        if args.check:
            print('No changes made. Setup will install apt/Python dependencies, services, and boot overlays.')
            return 0
        if os.geteuid() != 0:
            raise SetupError('Run with sudo, or use --check for a read-only preflight.')
        if args.password_file and not (STATE / 'web-auth.json').exists():
            args.password_file = args.password_file.resolve()
            if len(args.password_file.read_text().rstrip('\r\n')) < 12:
                raise SetupError('The initial web password must contain at least 12 characters.')
        os.umask(0o077)
        with open('/run/lock/desk-setup.lock', 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SetupError('Another Desk Orchestrator installer is running.') from None
            install(args, boot, content)
        return 0
    except (SetupError, OSError, subprocess.CalledProcessError, configparser.Error, ValueError) as error:
        print(f'Setup failed: {error}\nFix the reported problem, then rerun the same command. '
              'If services were stopped, they stay stopped until a successful rerun.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
