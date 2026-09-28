"""Offline deployment checks: never execute apt, systemctl, or touch host firmware."""
import importlib.util
import argparse
from contextlib import ExitStack
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('setup_pi', Path(__file__).resolve().parents[1] / 'deploy/setup_pi.py')
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.boot = self.root / 'config.txt'

    def firmware(self, text):
        self.boot.write_text(text)
        return setup.boot_contents(self.boot)

    def test_boot_is_idempotent_and_unconditional(self):
        original = '# my display\nhdmi_force_hotplug=1\n[pi4]\narm_boost=1\n[cm4]\notg_mode=1\n'
        first = self.firmware(original)
        self.assertTrue(first.startswith(original))
        self.assertIn(setup.BEGIN + '\n[all]\n', first)
        self.assertEqual(first, self.firmware(first))
        self.assertEqual(first.count('dtoverlay=gpio-ir,gpio_pin=18'), 1)

    def test_compatible_existing_overlays_are_consolidated(self):
        result = self.firmware('[pi4]\n' + '\n'.join(setup.OVERLAYS) + '\n')
        for overlay in setup.OVERLAYS:
            self.assertEqual(result.count(overlay), 1)

    def test_serial_setup_removes_our_gadget_overlay_and_preserves_external_usb_settings(self):
        self.boot.write_text(setup.boot_contents(self.boot) if self.boot.exists() else '# display\n')
        self.boot.write_text(setup.boot_contents(self.boot))
        serial_boot = setup.boot_contents(self.boot, 'ch9328')
        self.assertNotIn('dwc2', serial_boot)
        self.boot.write_text('otg_mode=1\ndtoverlay=dwc2,dr_mode=host\n' + serial_boot)
        updated = setup.boot_contents(self.boot, 'ch9328')
        self.assertIn('otg_mode=1\ndtoverlay=dwc2,dr_mode=host', updated)
        self.boot.write_text(updated)
        self.assertEqual(setup.boot_contents(self.boot, 'ch9328'), updated)

    def test_foreign_overlay_and_usb_host_conflicts_are_rejected(self):
        for content in ('dtoverlay=dwc2,dr_mode=host', 'dtoverlay=gpio-ir,gpio_pin=23',
                        'dtoverlay=gpio-ir-tx', 'otg_mode=1', 'dtoverlay=dwc-otg'):
            with self.subTest(content=content), self.assertRaises(setup.SetupError):
                self.firmware(content)

    def test_included_conflict_is_found_without_modifying_files(self):
        included = self.root / 'usercfg.txt'
        included.write_text('dtoverlay=dwc2,dr_mode=host\n')
        with self.assertRaisesRegex(setup.SetupError, 'usercfg.txt'):
            self.firmware('include usercfg.txt\n')
        self.assertEqual(self.boot.read_text(), 'include usercfg.txt\n')
        self.assertEqual(included.read_text(), 'dtoverlay=dwc2,dr_mode=host\n')

    def test_empty_include_preserved_and_inactive_include_ignored(self):
        (self.root / 'usercfg.txt').write_text('# local settings\n')
        result = self.firmware('include usercfg.txt\n[cm4]\ninclude not-present.txt\n')
        self.assertIn('include usercfg.txt', result)

    def test_unreadable_circular_and_malformed_firmware_fail(self):
        for content in ('include missing.txt\n', 'include config.txt\n', setup.BEGIN,
                        setup.END + '\n' + setup.BEGIN):
            with self.subTest(content=content), self.assertRaises(setup.SetupError):
                self.firmware(content)

    def test_lirc_keeps_other_options_and_sections(self):
        original = '[lircd]\ndriver=devinput\ndevice=auto\nloglevel=notice\n[lircmd]\nuinput=True\n'
        updated = setup.lirc_contents(original)
        self.assertIn('device = /dev/desk-ir-tx', updated)
        self.assertIn('driver = default', updated)
        self.assertIn('output = /run/lirc/lircd', updated)
        self.assertIn('loglevel = notice', updated)
        self.assertIn('uinput = True', updated)
        self.assertEqual(updated, setup.lirc_contents(updated))

    def test_backup_on_change_only(self):
        target = self.root / 'settings'
        target.write_text('user settings\n')
        options = dict(uid=os.getuid(), gid=os.getgid())
        self.assertTrue(setup.write_file(target, 'managed\n', **options))
        self.assertFalse(setup.write_file(target, 'managed\n', **options))
        backups = list(self.root.glob('settings.desk-backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'user settings\n')
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_text(), 'managed\n')

    def test_write_does_not_follow_symlink(self):
        target = self.root / 'secret'
        target.write_text('untouched')
        link = self.root / 'link'
        link.symlink_to(target)
        with self.assertRaises(setup.SetupError):
            setup.write_file(link, 'replacement')
        self.assertEqual(target.read_text(), 'untouched')

    def test_real_web_initialization_preserves_password_and_saved_config(self):
        state, etc = self.root / 'state', self.root / 'etc'
        state.mkdir()
        etc.mkdir()
        (etc / 'desk.toml').write_text((setup.SOURCE / 'config/desk.example.toml').read_text())
        password_file = self.root / 'password'
        password_file.write_text('initial password for tests\n')

        def run_as_current_user(*args, **kwargs):
            # Exercise actual app initialization, replacing only runuser/Pi interpreter.
            self.assertEqual(args[:4], ('runuser', '-u', 'desk', '--'))
            return subprocess.run([sys.executable, *args[5:]], check=True, **kwargs,
                                  env=dict(os.environ, PYTHONPATH=str(setup.SOURCE)))

        with patch.object(setup, 'STATE', state), patch.object(setup, 'ETC', etc), \
             patch.object(setup, 'run', side_effect=run_as_current_user):
            setup.initialize_web(password_file)
            auth = (state / 'web-auth.json').read_bytes()
            config = (state / 'controller.json').read_bytes()
            password_file.write_text('different password on rerun')
            setup.initialize_web(password_file)
        self.assertEqual((state / 'web-auth.json').read_bytes(), auth)
        self.assertEqual((state / 'controller.json').read_bytes(), config)
        self.assertEqual((state / 'web-auth.json').stat().st_mode & 0o777, 0o600)
        self.assertFalse((etc / 'initial-web-password.txt').exists())

    def test_generated_password_is_private_and_reused_after_partial_install(self):
        state, etc = self.root / 'state', self.root / 'etc'
        etc.mkdir()
        real_write = setup.write_file

        def user_write(path, content, **kwargs):
            return real_write(path, content, **kwargs, uid=os.getuid(), gid=os.getgid())

        with patch.object(setup, 'STATE', state), patch.object(setup, 'ETC', etc), \
             patch.object(setup, 'write_file', side_effect=user_write), patch.object(setup, 'run') as run:
            setup.initialize_web(None)
            first = run.call_args.kwargs['input']
            setup.initialize_web(None)
            self.assertEqual(first, run.call_args.kwargs['input'])
        saved = etc / 'initial-web-password.txt'
        self.assertGreaterEqual(len(first), 24)
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_check_never_installs(self):
        with patch.object(setup, 'preflight', return_value=(self.boot, 'new')), \
             patch.object(setup, 'install') as install:
            self.assertEqual(setup.main(['--check']), 0)
            install.assert_not_called()

    def test_non_root_never_installs(self):
        with patch.object(setup, 'preflight', return_value=(self.boot, 'new')), \
             patch.object(setup.os, 'geteuid', return_value=1000), patch.object(setup, 'install') as install:
            self.assertEqual(setup.main([]), 1)
            install.assert_not_called()

    def test_copy_excludes_local_secrets_and_uses_readable_venv_umask(self):
        source, app = self.root / 'source', self.root / 'app'
        source.mkdir()
        for name in setup.FILES:
            original = setup.SOURCE / name
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            if original.is_dir():
                (source / name).mkdir()
                (source / name / 'sample.txt').write_text('source')
            else:
                (source / name).write_text('source')
        (source / '.runtime').mkdir()
        (source / '.runtime/secret').write_text('local secret')
        (source / 'config/desk.toml').write_text('local credentials')
        masks = []
        commands = []

        def fake_run(*args, **kwargs):
            commands.append(tuple(str(arg) for arg in args))
            current = os.umask(0o022)
            os.umask(current)
            masks.append(current)

        with patch.object(setup, 'SOURCE', source), patch.object(setup, 'APP', app), \
             patch.object(setup.os, 'chown'), patch.object(setup, 'run', side_effect=fake_run):
            old = os.umask(0o077)
            try:
                setup.install_project()
            finally:
                restored = os.umask(old)
        self.assertEqual(restored, 0o077)
        self.assertEqual(masks, [0o022] * 3)
        self.assertTrue(any(command[-1] == f'{app}[web,keypad,tls,serial]' for command in commands))
        self.assertFalse((app / '.runtime').exists())
        self.assertFalse((app / 'config/desk.toml').exists())
        self.assertEqual((app / 'desk_orchestrator/sample.txt').stat().st_mode & 0o777, 0o644)

    def test_copy_rejects_destination_symlinks_before_writing(self):
        app = self.root / 'app'
        (app / 'desk_orchestrator').mkdir(parents=True)
        external = self.root / 'external'
        external.write_text('untouched')
        (app / 'desk_orchestrator/web.py').symlink_to(external)
        with patch.object(setup, 'APP', app), self.assertRaises(setup.SetupError):
            setup.install_project()
        self.assertEqual(external.read_text(), 'untouched')


class InstallationFlowTests(unittest.TestCase):
    """Run installation decisions against a simulated Pi filesystem and commands."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.etc = self.root / 'etc/desk-orchestrator'
        self.state = self.root / 'var/lib/desk-orchestrator'
        self.boot = self.root / 'boot/config.txt'
        self.boot.parent.mkdir()
        self.boot.write_text('# existing firmware\n')
        self.boot_id = self.root / 'proc/sys/kernel/random/boot_id'
        self.boot_id.parent.mkdir(parents=True)
        self.boot_id.write_text('first-boot')
        lirc = self.root / 'etc/lirc/lirc_options.conf'
        lirc.parent.mkdir(parents=True)
        lirc.write_text('[lircd]\ndriver=devinput\ndevice=auto\n')
        self.remote = lirc.parent / 'lircd.conf.d/learned.conf'
        self.remote.parent.mkdir()
        self.remote.write_text('learned waveforms')
        self.commands = []
        self.args = argparse.Namespace(password_file=None, reboot=True)

        def mapped_path(value):
            value = str(value)
            if value.startswith(('/etc/', '/proc/', '/dev/')):
                return self.root / value.lstrip('/')
            return Path(value)

        def init_web(_):
            for name in ('web-auth.json', 'controller.json'):
                target = self.state / name
                if not target.exists():
                    target.write_text('preserve this saved state')

        replacements = {
            'ETC': self.etc, 'STATE': self.state, 'APP': setup.SOURCE,
            'SYSTEMD': self.root / 'etc/systemd/system', 'Path': mapped_path,
            'initialize_web': init_web, 'wait_for_web': lambda: None,
        }
        for name, value in replacements.items():
            self.stack.enter_context(patch.object(setup, name, value))
        self.stack.enter_context(patch.object(setup.pwd, 'getpwnam',
                                            return_value=SimpleNamespace(pw_uid=os.getuid())))
        self.stack.enter_context(patch.object(setup.grp, 'getgrnam',
                                            return_value=SimpleNamespace(gr_gid=os.getgid())))
        self.stack.enter_context(patch.object(setup.os, 'chown'))
        self.stack.enter_context(patch.object(setup, 'run', side_effect=lambda *args, **kw: self.commands.append(args)))
        self.active = self.stack.enter_context(patch.object(setup, 'active', return_value=False))
        self.project = self.stack.enter_context(patch.object(setup, 'install_project'))

    def add_transmitter(self):
        device = self.root / 'dev/desk-ir-tx'
        device.parent.mkdir()
        device.touch()

    def test_first_install_retry_and_post_reboot_preserve_user_data(self):
        setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertIn(('systemctl', 'reboot'), self.commands)
        self.assertIn(('systemctl', 'enable', '--now', 'desk-ir-reload.path'), self.commands)
        self.assertIn('desk-ir-rx', (self.root / 'etc/udev/rules.d/99-desk-ir.rules').read_text())
        self.assertIn('ir-codes/*.conf', (self.root / 'etc/lirc/lircd.conf.d/desk-learned.conf').read_text())
        self.assertTrue((self.root / 'etc/systemd/system/desk-ir-reload.service').exists())
        self.assertNotIn(('systemctl', 'start', 'lircd.service', 'desk-gadget.service'), self.commands)
        (self.etc / 'credentials.env').write_text('user credentials')
        (self.etc / 'desk.toml').write_text('user seed')
        backups = list(self.root.rglob('*.desk-backup-*'))
        self.commands.clear()
        setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertIn(('systemctl', 'reboot'), self.commands)
        self.assertEqual(len(backups), len(list(self.root.rglob('*.desk-backup-*'))))
        self.boot_id.write_text('second-boot')
        self.add_transmitter()
        self.commands.clear()
        setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertNotIn(('systemctl', 'reboot'), self.commands)
        self.assertIn(('systemctl', 'start', 'lircd.service', 'desk-gadget.service'), self.commands)
        self.assertFalse((self.etc / 'setup-reboot-required').exists())
        self.assertEqual((self.etc / 'credentials.env').read_text(), 'user credentials')
        self.assertEqual((self.etc / 'desk.toml').read_text(), 'user seed')
        self.assertEqual((self.state / 'controller.json').read_text(), 'preserve this saved state')
        self.assertEqual((self.state / 'web-auth.json').read_text(), 'preserve this saved state')
        self.assertEqual(self.remote.read_text(), 'learned waveforms')
        for command in self.commands:
            if command[:2] == ('systemctl', 'enable'):
                self.assertNotIn('desk-orchestrator.service', command)
        self.assertNotIn(('systemctl', 'start', 'desk-orchestrator.service'), self.commands)

    def test_serial_install_does_not_start_or_require_gadget(self):
        self.args.keyboard_transport = 'ch9328'
        self.boot.write_text(setup.boot_contents(self.boot, 'ch9328'))
        self.add_transmitter()
        setup.install(self.args, self.boot, setup.boot_contents(self.boot, 'ch9328'))
        self.assertIn(('systemctl', 'disable', '--now', 'desk-gadget.service'), self.commands)
        self.assertIn(('systemctl', 'start', 'lircd.service'), self.commands)
        self.assertIn(('usermod', '-aG', 'input,dialout', 'desk'), self.commands)
        self.assertFalse(any('/dev/hidg0' in command for command in self.commands))
        for command in self.commands:
            if command[:2] in (('systemctl', 'enable'), ('systemctl', 'start')):
                self.assertNotIn('desk-gadget.service', command)

    def test_failed_update_remembers_previously_active_listener(self):
        self.boot.write_text(setup.boot_contents(self.boot))
        self.add_transmitter()
        self.active.side_effect = lambda unit: unit == 'desk-orchestrator.service'
        self.project.side_effect = setup.SetupError('simulated pip failure')
        with self.assertRaisesRegex(setup.SetupError, 'pip failure'):
            setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertTrue((self.etc / 'setup-resume-listener').exists())
        self.assertIn(('systemctl', 'stop', 'desk-orchestrator.service'), self.commands)
        self.active.side_effect = None
        self.active.return_value = False
        self.project.side_effect = None
        setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertIn(('systemctl', 'start', 'desk-orchestrator.service'), self.commands)
        self.assertFalse((self.etc / 'setup-resume-listener').exists())

    def test_https_resume_survives_interrupted_install_and_preserves_unit(self):
        unit = setup.SYSTEMD / 'desk-https.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('custom HTTPS bind and paths')
        self.active.side_effect = lambda name: name == 'desk-https.service'
        self.project.side_effect = setup.SetupError('simulated pip failure')
        with self.assertRaisesRegex(setup.SetupError, 'pip failure'):
            setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertTrue((self.etc / 'setup-resume-https').exists())
        self.assertIn(('systemctl', 'stop', 'desk-https.service'), self.commands)
        self.project.side_effect = None
        self.active.side_effect = None
        self.active.return_value = False
        setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertIn(('systemctl', 'start', 'desk-https.service'), self.commands)
        self.assertFalse((self.etc / 'setup-resume-https').exists())
        self.assertEqual(unit.read_text(), 'custom HTTPS bind and paths')

    def test_installed_but_stopped_https_stays_stopped(self):
        unit = setup.SYSTEMD / 'desk-https.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('existing HTTPS service')
        setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertIn(('systemctl', 'stop', 'desk-https.service'), self.commands)
        self.assertNotIn(('systemctl', 'start', 'desk-https.service'), self.commands)

    def test_missing_hardware_keeps_web_available_but_reports_failure(self):
        self.boot.write_text(setup.boot_contents(self.boot))
        with self.assertRaisesRegex(setup.SetupError, 'IR transmitter missing'):
            setup.install(self.args, self.boot, setup.boot_contents(self.boot))
        self.assertIn(('systemctl', 'restart', 'desk-web.service'), self.commands)
        self.assertNotIn(('systemctl', 'reboot'), self.commands)


if __name__ == '__main__':
    unittest.main()
