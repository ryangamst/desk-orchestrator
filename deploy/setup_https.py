#!/usr/bin/env python3
"""Print an HTTPS system service; --install installs and starts it as root."""
import argparse
import os
from pathlib import Path
import pwd
import subprocess
import sys

SYSTEMD = Path('/etc/systemd/system')


def service_environment(user):
    home = Path(pwd.getpwnam(user).pw_dir)
    return [f'HOME={home}', f'PATH={home}/.local/bin:/usr/local/bin:/usr/bin:/bin']


def check_prerequisites(user, app, data, config, env_file):
    """Check the actual service identity before touching its systemd unit."""
    for path in (app / '.venv/bin/desk', data, config, env_file):
        if not path.exists():
            raise ValueError(f'Missing required path: {path}')
    prefix = ['runuser', '-u', user, '--', 'env', *service_environment(user)]
    script = '''
import os, sys
from pathlib import Path
from desk_orchestrator import tls
if not tls.installed():
    sys.exit('Missing HTTPS Python dependencies. Install .[web,tls] in the app virtual environment first.')
for value in sys.argv[1:]:
    if not os.access(value, os.R_OK):
        sys.exit('The HTTPS service account cannot read: ' + value)
if not os.access(sys.argv[1], os.W_OK):
    sys.exit('The HTTPS service account cannot write its data directory.')
'''
    subprocess.run([*prefix, str(app / '.venv/bin/python'), '-c', script,
                    str(data), str(config), str(env_file)], check=True, cwd=app)


def quote(value):
    value = str(value)
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('Service paths cannot contain control characters.')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def path_value(value):
    # WorkingDirectory is a single path, not a shell-like word list.
    value = str(value)
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('Service paths cannot contain control characters.')
    return value.replace('\\', '\\\\').replace('%', '%%').replace(' ', '\\x20').replace('\t', '\\x09')


def unit(user, app, data, config, env_file, host):
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        raise ValueError('Run HTTPS as a non-root account.')
    home = Path(account.pw_dir)
    command = ' '.join(quote(v) for v in [app / '.venv/bin/desk', '--config', config, 'https', '--data-dir', data, '--host', host, '--port', '443'])
    return f'''[Unit]
Description=Desk Orchestrator HTTPS with manual DNS certificates
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={path_value(app)}
Environment={quote('HOME=' + str(home))}
Environment={quote('PATH=' + str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin')}
EnvironmentFile={path_value(env_file)}
ExecStart={command}
Restart=on-failure
RestartSec=5
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
PrivateTmp=yes
ReadWritePaths={quote(data)}

[Install]
WantedBy=multi-user.target
'''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True)
    parser.add_argument('--app-dir', type=Path, default=Path('/opt/desk-orchestrator'))
    parser.add_argument('--data-dir', type=Path, default=Path('/var/lib/desk-orchestrator'))
    parser.add_argument('--config', type=Path, default=Path('/etc/desk-orchestrator/desk.toml'))
    parser.add_argument('--env-file', type=Path, default=Path('/etc/desk-orchestrator/credentials.env'))
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    for name in ('app_dir', 'data_dir', 'config', 'env_file'):
        setattr(args, name, getattr(args, name).resolve())
    content = unit(args.user, args.app_dir.resolve(), args.data_dir.resolve(), args.config.resolve(), args.env_file.resolve(), args.host)
    if not args.install:
        print(content, end='')
        return
    if os.geteuid() != 0:
        parser.error('--install needs sudo to create the system service for port 443.')
    try:
        check_prerequisites(args.user, args.app_dir, args.data_dir, args.config, args.env_file)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'HTTPS setup checks failed: {exc}', file=sys.stderr)
        return 1
    target = SYSTEMD / 'desk-https.service'
    if target.is_symlink():
        parser.error('Refusing to overwrite a symlinked HTTPS unit.')
    if target.exists() and target.read_text() != content:
        # Preserve the previous bind address/service settings for recovery.
        from datetime import datetime, timezone
        backup = target.with_name(target.name + '.desk-backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        backup.write_text(target.read_text())
        backup.chmod(0o600)
    target.write_text(content)
    target.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', 'desk-https.service'], check=True)
    subprocess.run(['systemctl', 'restart', 'desk-https.service'], check=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'desk-https.service'], check=True)
    print('HTTPS service installed. It waits for a trusted certificate configured in Settings.')


if __name__ == '__main__':
    sys.exit(main())
