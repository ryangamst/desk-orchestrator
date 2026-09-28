"""HTTPS deployment checks use a simulated service account and unit directory."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('setup_https', Path(__file__).resolve().parents[1] / 'deploy/setup_https.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class HTTPSSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = SimpleNamespace(pw_uid=1000, pw_dir=str(self.root / 'home'))
        self.user = patch.object(setup.pwd, 'getpwnam', return_value=self.account)
        self.user.start(); self.addCleanup(self.user.stop)
        self.args = ['--user', 'desk', '--app-dir', str(self.root / 'app'),
                     '--data-dir', str(self.root / 'state'), '--config', str(self.root / 'seed.toml'),
                     '--env-file', str(self.root / 'credentials.env'), '--host', '192.0.2.10']

    def test_preview_is_read_only_and_uses_service_account_home(self):
        with patch.object(setup.subprocess, 'run') as run, patch('builtins.print') as output:
            setup.main(self.args)
        run.assert_not_called()
        text = output.call_args.args[0]
        self.assertIn('User=desk', text)
        self.assertIn('HOME=' + self.account.pw_dir, text)
        self.assertIn('CAP_NET_BIND_SERVICE', text)
        self.assertIn('192.0.2.10', text)
        self.assertNotIn('User=root', text)
        self.assertNotIn('.aws', text)
        self.assertFalse((self.root / 'desk-https.service').exists())

    def test_failed_prerequisites_do_not_replace_unit_or_restart(self):
        target = self.root / 'desk-https.service'
        target.write_text('existing custom service')
        with patch.object(setup, 'SYSTEMD', self.root), patch.object(setup.os, 'geteuid', return_value=0), \
             patch.object(setup, 'check_prerequisites', side_effect=ValueError('TLS dependencies missing')), \
             patch.object(setup.subprocess, 'run') as run:
            self.assertEqual(setup.main([*self.args, '--install']), 1)
        run.assert_not_called()
        self.assertEqual(target.read_text(), 'existing custom service')

    def test_install_backs_up_unit_and_only_restarts_once(self):
        target = self.root / 'desk-https.service'
        target.write_text('previous bind settings')
        with patch.object(setup, 'SYSTEMD', self.root), patch.object(setup.os, 'geteuid', return_value=0), \
             patch.object(setup, 'check_prerequisites') as check, patch.object(setup.subprocess, 'run') as run:
            setup.main([*self.args, '--install'])
        check.assert_called_once()
        backups = list(self.root.glob('desk-https.service.desk-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'previous bind settings')
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls.count(['systemctl', 'restart', 'desk-https.service']), 1)
        self.assertIn(['systemctl', 'is-active', '--quiet', 'desk-https.service'], calls)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_prerequisites_run_as_service_account_with_correct_paths(self):
        app, data = self.root / 'app', self.root / 'state'
        (app / '.venv/bin').mkdir(parents=True); (app / '.venv/bin/desk').touch()
        data.mkdir()
        config, env = self.root / 'seed.toml', self.root / 'credentials.env'
        config.touch(); env.touch()
        with patch.object(setup.subprocess, 'run') as run:
            setup.check_prerequisites('desk', app, data, config, env)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ['runuser', '-u', 'desk', '--', 'env'])
        self.assertIn('HOME=' + self.account.pw_dir, argv)
        self.assertIn(str(app / '.venv/bin/python'), argv)
        self.assertNotIn('aws', argv[argv.index('-c') + 1])
        self.assertEqual(argv[-3:], [str(data), str(config), str(env)])

    def test_root_account_is_rejected(self):
        self.account.pw_uid = 0
        with self.assertRaisesRegex(ValueError, 'non-root'):
            setup.main(self.args)
