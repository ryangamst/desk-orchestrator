"""Updater tests use temporary installations; no real SSH, services, or packages are changed."""
from contextlib import ExitStack
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('update_pi', Path(__file__).resolve().parents[1] / 'deploy/update_pi.py')
update = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_optional_service_detection_does_not_require_https_on_old_installs(self):
        for state, expected in [('not-found', update.BASE_UNITS), ('loaded', update.UNITS)]:
            with patch.object(update.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, state + '\n')):
                self.assertEqual(update.managed_units(), list(expected))

    def test_https_readiness_waits_for_trusted_login_but_allows_initial_setup(self):
        with patch.object(update, 'STATE', self.root), patch.object(update.urllib.request, 'build_opener') as opener:
            update.wait_for_https()
            opener.assert_not_called()
            cert = self.root / 'tls/production/config/live/desk/fullchain.pem'
            cert.parent.mkdir(parents=True)
            cert.touch()
            (self.root / 'tls/settings.json').write_text('{"domain":"desk.example.com"}')
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'<input name="password">'
            update.wait_for_https()
            opener.return_value.open.assert_called_once_with('https://desk.example.com/login', timeout=2)

    def test_installed_app_smoke_covers_oauth_tls_and_input_editor(self):
        state = self.root / 'state'
        from desk_orchestrator.web import set_password
        from desk_orchestrator.config_store import ConfigStore
        seed = update.SOURCE / 'config/desk.example.toml'
        set_password(state, 'private-test-password')
        ConfigStore(state, seed)
        script = update.SMOKE.replace('/var/lib/desk-orchestrator', str(state)).replace('/etc/desk-orchestrator/desk.toml', str(seed))
        subprocess.run([sys.executable, '-c', script], check=True, capture_output=True, text=True)

    def test_current_project_bundle_contains_console_but_no_private_state(self):
        source = self.root / 'source'
        for name in (*update.FILES, *update.DIRECTORIES):
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            original = update.SOURCE / name
            if original.is_dir():
                shutil.copytree(original, destination)
            else:
                shutil.copyfile(original, destination)
        for name in ('.runtime/web/web-auth.json', '.venv/private.py', 'config/desk.toml',
                     'deploy/credentials.env', 'desk_orchestrator/.env', 'docs/private.txt'):
            path = source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private')
        archive = self.root / 'source.tar.gz'
        update.bundle(archive, source)
        with tarfile.open(archive) as tar:
            names = tar.getnames()
        self.assertIn('desk_orchestrator/templates/console.html', names)
        self.assertIn('desk_orchestrator/static/favicon.svg', names)
        self.assertIn('desk_orchestrator/static/studio.css', names)
        self.assertIn('desk_orchestrator/templates/icons.html', names)
        self.assertIn('desk_orchestrator/static/virtual_numpad.js', names)
        for name in ('desk_orchestrator/hardware_map.py', 'desk_orchestrator/templates/hardware_map.html',
                     'desk_orchestrator/static/hardware_map.js', 'desk_orchestrator/static/hardware_map.css'):
            self.assertIn(name, names)
        self.assertIn('desk_orchestrator/virtual_numpad.py', names)
        self.assertIn('desk_orchestrator/diagnostics.py', names)
        self.assertIn('deploy/update-pi.sh', names)
        for name in ('deploy/setup_https.py', 'desk_orchestrator/oauth.py', 'desk_orchestrator/tls.py',
                     'desk_orchestrator/tls_dns.py', 'desk_orchestrator/templates/tls_settings.html',
                     'desk_orchestrator/templates/oauth_authorize.html', 'desk_orchestrator/static/oauth_authorize.js', 'desk_orchestrator/static/tls_settings.js',
                     'desk_orchestrator/templates/tls_operation.html'):
            self.assertIn(name, names)
        self.assertFalse(any('private' in name or '.runtime' in name or '.venv' in name or '__pycache__' in name for name in names))
        self.assertNotIn('config/desk.toml', names)
        self.assertNotIn('deploy/credentials.env', names)
        self.assertNotIn('desk_orchestrator/.env', names)
        unpacked = self.root / 'unpacked'
        update.unpack(archive, unpacked)
        self.assertEqual((unpacked / 'desk_orchestrator/templates/console.html').read_bytes(),
                         (update.SOURCE / 'desk_orchestrator/templates/console.html').read_bytes())
        self.assertEqual((unpacked / 'desk_orchestrator/static/favicon.svg').read_bytes(),
                         (update.SOURCE / 'desk_orchestrator/static/favicon.svg').read_bytes())

    def test_unsafe_paths_links_and_duplicates_are_rejected(self):
        for name, kind, duplicate in [('../escape.py', tarfile.REGTYPE, False),
                                      ('/etc/passwd', tarfile.REGTYPE, False),
                                      ('desk_orchestrator/web.py', tarfile.SYMTYPE, False),
                                      ('desk_orchestrator/web.py', tarfile.LNKTYPE, False),
                                      ('desk_orchestrator/web.py', tarfile.REGTYPE, True)]:
            with self.subTest(name=name, kind=kind, duplicate=duplicate):
                archive = self.root / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as tar:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '/etc/passwd' if kind != tarfile.REGTYPE else ''
                    member.size = 1 if kind == tarfile.REGTYPE else 0
                    tar.addfile(member, io.BytesIO(b'x'))
                    if duplicate:
                        tar.addfile(member, io.BytesIO(b'x'))
                with self.assertRaises(update.UpdateError):
                    update.unpack(archive, self.root / 'unpacked')

    def test_incomplete_archive_rejected(self):
        archive = self.root / 'empty.tar.gz'
        with tarfile.open(archive, 'w:gz'):
            pass
        with self.assertRaisesRegex(update.UpdateError, 'Incomplete'):
            update.unpack(archive, self.root / 'unpacked')

    def test_ssh_target_validation_happens_before_any_network_access(self):
        script = update.SOURCE / 'deploy/update-pi.sh'
        for target in ('--bad-option', 'pi@host;touch /tmp/unwanted', 'pi@$(whoami)'):
            result = subprocess.run(['bash', str(script), target], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Expected USER@HOST', result.stderr)

    def test_push_wrapper_transfers_archive_and_preserves_remote_failure_status(self):
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        log = self.root / 'transport.log'
        ssh = bin_dir / 'ssh'
        ssh.write_text("""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$TRANSPORT_LOG"
case "$*" in
  *'mktemp -d'*) echo /tmp/desk-update.ABCDEFGH ;;
  *'--archive'*) exit "${REMOTE_STATUS:-0}" ;;
esac
""")
        scp = bin_dir / 'scp'
        scp.write_text('#!/usr/bin/env bash\nprintf "scp %s\\n" "$*" >> "$TRANSPORT_LOG"\n')
        ssh.chmod(0o755)
        scp.chmod(0o755)
        environment = dict(os.environ, PATH=str(bin_dir) + ':' + os.environ['PATH'], TRANSPORT_LOG=str(log))
        for status in (0, 42):
            environment['REMOTE_STATUS'] = str(status)
            result = subprocess.run(['bash', str(update.SOURCE / 'deploy/update-pi.sh'), 'pi@192.168.1.50'],
                                    capture_output=True, text=True, env=environment)
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertEqual('Update finished.' in result.stdout, status == 0)
        commands = log.read_text()
        self.assertIn('scp ', commands)
        self.assertIn("sudo python3 '/tmp/desk-update.ABCDEFGH/update_pi.py' --archive", commands)
        self.assertIn("rm -rf -- '/tmp/desk-update.ABCDEFGH'", commands)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.app = self.root / 'app'
        self.state = self.root / 'state'
        self.source = self.root / 'source'
        self.wheels = self.root / 'wheels'
        self.backup = self.root / 'backup'
        for directory in (self.app, self.state, self.source, self.wheels):
            directory.mkdir()
        (self.app / 'desk_orchestrator').mkdir()
        (self.app / 'desk_orchestrator/obsolete.py').write_text('old code')
        (self.app / '.venv/bin').mkdir(parents=True)
        (self.app / '.venv/bin/python').write_text('old interpreter')
        (self.state / 'controller.json').write_text('user config')
        (self.state / 'web-auth.json').write_text('user authentication')
        (self.state / 'events.sqlite3').write_text('existing diagnostics')
        self.private_state = {'oauth/tokens-test.json': 'saved OAuth pair',
                              'tls/settings.json': 'local TLS settings',
                              'tls/production/config/archive/desk/privkey1.pem': 'certificate private key',
                              '.aws/credentials': 'service account AWS credentials'}
        for name, content in self.private_state.items():
            path = self.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for name in update.DIRECTORIES:
            (self.source / name).mkdir()
            (self.source / name / 'new.py').write_text('new code')
        for name in update.FILES:
            (self.source / name).parent.mkdir(parents=True, exist_ok=True)
            (self.source / name).write_text('new file')
        (self.wheels / 'desk_orchestrator-0.1.0-py3-none-any.whl').touch()
        self.commands = []
        self.fail_pip = False
        self.stack.enter_context(patch.object(update, 'APP', self.app))
        self.stack.enter_context(patch.object(update, 'STATE', self.state))
        self.keyboard_unit = self.root / 'desk-orchestrator.service'
        self.keyboard_unit.write_text('[Unit]\nRequires=desk-gadget.service custom.service\n[Service]\nUser=desk\n')
        self.stack.enter_context(patch.object(update, 'KEYBOARD_UNIT', self.keyboard_unit))
        self.rule = self.root / 'udev/99-desk-gmmk.rules'
        self.stack.enter_context(patch.object(update, 'GMMK_RULE', self.rule))
        self.stack.enter_context(patch.object(update.time, 'sleep'))
        self.stack.enter_context(patch.object(update, 'run', side_effect=self.fake_run))
        self.active = self.stack.enter_context(patch.object(update, 'active_units', return_value=list(update.UNITS)))
        self.managed = self.stack.enter_context(patch.object(update, 'managed_units', return_value=list(update.UNITS)))
        self.web = self.stack.enter_context(patch.object(update, 'wait_for_web'))
        self.https = self.stack.enter_context(patch.object(update, 'wait_for_https'))

    def fake_run(self, *args, **kwargs):
        self.commands.append(args)
        if args[0] == 'tar':
            return subprocess.run([str(a) for a in args], check=True)
        if 'install' in args and self.fail_pip:
            (self.app / '.venv/bin/python').write_text('partially updated interpreter')
            raise update.UpdateError('simulated dependency installation failure')

    def test_update_replaces_deleted_files_preserves_state_and_restarts_both_services(self):
        update.apply_update(self.source, self.wheels, self.backup)
        self.assertEqual(self.rule.read_text(), 'new file')
        self.assertFalse((self.app / 'desk_orchestrator/obsolete.py').exists())
        self.assertTrue((self.app / 'desk_orchestrator/new.py').exists())
        self.assertEqual((self.state / 'controller.json').read_text(), 'user config')
        self.assertEqual((self.state / 'web-auth.json').read_text(), 'user authentication')
        self.assertEqual((self.state / 'events.sqlite3').read_text(), 'existing diagnostics')
        self.assertEqual((self.backup / 'app/desk_orchestrator/obsolete.py').read_text(), 'old code')
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o700)
        with tarfile.open(self.backup / 'state.tar.gz') as tar:
            self.assertEqual(tar.extractfile('./controller.json').read(), b'user config')
            for name, content in self.private_state.items():
                self.assertEqual(tar.extractfile('./' + name).read(), content.encode())
                self.assertEqual((self.state / name).read_text(), content)
        for unit in update.UNITS:
            self.assertIn(('systemctl', 'start', unit), self.commands)
        self.assertFalse(any(command[:2] == ('systemctl', 'enable') for command in self.commands))
        pip = next(command for command in self.commands if 'install' in command)
        self.assertIn('--no-index', pip)
        self.assertIn('--force-reinstall', pip)  # Source version may be unchanged.
        self.assertTrue(str(pip[-1]).endswith('[web,keypad,tls,serial]'))
        self.assertNotIn('desk-gadget.service', self.keyboard_unit.read_text())
        self.assertIn('Requires=custom.service', self.keyboard_unit.read_text())
        self.assertIn(('usermod', '-aG', 'dialout', 'desk'), self.commands)
        self.https.assert_called_once()
        self.assertIn(('systemctl', 'stop', *update.UNITS), self.commands)

    def test_inactive_listener_is_not_started(self):
        self.active.return_value = ['desk-web.service']
        update.apply_update(self.source, self.wheels, self.backup)
        self.assertNotIn(('systemctl', 'start', 'desk-orchestrator.service'), self.commands)
        self.assertNotIn(('systemctl', 'start', 'desk-https.service'), self.commands)
        self.https.assert_not_called()

    def test_optional_https_not_installed_is_not_stopped_or_enabled(self):
        self.managed.return_value = list(update.BASE_UNITS)
        self.active.return_value = list(update.BASE_UNITS)
        update.apply_update(self.source, self.wheels, self.backup)
        self.assertIn(('systemctl', 'stop', *update.BASE_UNITS), self.commands)
        self.assertFalse(any('desk-https.service' in command for command in self.commands))

    def test_https_readiness_failure_rolls_back_and_preserves_credentials(self):
        self.https.side_effect = [update.UpdateError('HTTPS not ready'), None]
        with self.assertRaisesRegex(update.UpdateError, 'HTTPS not ready'):
            update.apply_update(self.source, self.wheels, self.backup)
        self.assertEqual(self.https.call_count, 2)
        self.assertTrue((self.app / 'desk_orchestrator/obsolete.py').exists())
        for name, content in self.private_state.items():
            self.assertEqual((self.state / name).read_text(), content)

    def test_permission_rule_is_restored_after_failure_following_install(self):
        self.rule.parent.mkdir()
        self.rule.write_text('previous rule')
        with patch.object(update, 'restart', side_effect=[update.UpdateError('restart failed'), None]):
            with self.assertRaises(update.UpdateError):
                update.apply_update(self.source, self.wheels, self.backup)
        self.assertEqual(self.rule.read_text(), 'previous rule')
        self.assertIn('Requires=desk-gadget.service custom.service', self.keyboard_unit.read_text())
        self.assertTrue((self.app / 'desk_orchestrator/obsolete.py').exists())

    def test_pip_failure_restores_code_and_venv_without_reverting_user_state(self):
        self.fail_pip = True
        with self.assertRaisesRegex(update.UpdateError, 'dependency installation'):
            update.apply_update(self.source, self.wheels, self.backup)
        self.assertEqual((self.app / '.venv/bin/python').read_text(), 'old interpreter')
        self.assertEqual((self.app / 'desk_orchestrator/obsolete.py').read_text(), 'old code')
        self.assertFalse((self.app / 'desk_orchestrator/new.py').exists())
        self.assertEqual((self.state / 'controller.json').read_text(), 'user config')
        for unit in update.UNITS:
            self.assertIn(('systemctl', 'start', unit), self.commands)

    def test_health_failure_rolls_back_and_checks_old_app(self):
        self.web.side_effect = [update.UpdateError('new server failed'), None]
        with self.assertRaisesRegex(update.UpdateError, 'new server failed'):
            update.apply_update(self.source, self.wheels, self.backup)
        self.assertTrue((self.app / 'desk_orchestrator/obsolete.py').exists())
        self.assertEqual(self.web.call_count, 2)

    def test_backup_failure_does_not_modify_application(self):
        with patch.object(update, 'snapshot', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                update.apply_update(self.source, self.wheels, self.backup)
        self.assertTrue((self.app / 'desk_orchestrator/obsolete.py').exists())
        self.assertFalse(any('install' in command for command in self.commands))
        self.assertIn(('systemctl', 'start', 'desk-web.service'), self.commands)

    def test_dependency_download_failure_never_stops_services(self):
        bundle = self.root / 'bundle.tar.gz'
        bundle.touch()
        staging = tempfile.TemporaryDirectory(dir=self.root)
        with patch.object(update, 'check_installation'), \
             patch.object(update, 'BACKUPS', self.root / 'backups'), \
             patch.object(update, 'LOCK', self.root / 'lock'), \
             patch.object(update.tempfile, 'TemporaryDirectory', return_value=staging), \
             patch.object(update, 'unpack'), \
             patch.object(update, 'run', side_effect=update.UpdateError('download failed')) as run:
            with self.assertRaisesRegex(update.UpdateError, 'download failed'):
                update.update(bundle)
            self.assertEqual(run.call_count, 1)
            self.assertIn('wheel', run.call_args.args)
        self.assertTrue((self.app / 'desk_orchestrator/obsolete.py').exists())

    def test_manual_rollback_recovers_missing_app_and_keeps_current_settings(self):
        name = '20260923T120000Z-a1b2c3d4'
        backup = self.root / name
        update.snapshot(backup, ['desk-web.service'])
        shutil.rmtree(self.app)
        with patch.object(update, 'check_host'), patch.object(update, 'BACKUPS', self.root), \
             patch.object(update, 'LOCK', self.root / 'lock'):
            update.rollback(name)
        self.assertTrue((self.app / 'desk_orchestrator/obsolete.py').exists())
        self.assertEqual((self.state / 'controller.json').read_text(), 'user config')
        self.assertIn(('systemctl', 'start', 'desk-web.service'), self.commands)
        self.assertNotIn(('systemctl', 'start', 'desk-orchestrator.service'), self.commands)

    def test_rollback_rejects_path_traversal(self):
        with patch.object(update, 'check_host'):
            with self.assertRaises(update.UpdateError):
                update.rollback('../../etc')


if __name__ == '__main__':
    unittest.main()
