import copy
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from desk_orchestrator import ir_learning as ir
from desk_orchestrator.config_store import ConfigStore
from desk_orchestrator.core import DeskError, exclusive
from desk_orchestrator.hardware import Hardware
from desk_orchestrator.diagnostics import target
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / 'config/desk.example.toml'
TIMINGS = [9000, 4500, 560, 560, 560, 1690, 560]


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ConfigStore(self.root, SEED)

    def test_irsend_preserves_failure_phase_socket_and_lirc_diagnostic(self):
        hw = Hardware({'ir': {'socket': '/run/lirc/test-socket'}})
        for operation, diagnostic in [('LIST', 'irsend: unknown remote: desk_learned_missing'),
                                      ('SEND_ONCE', 'irsend: hardware does not support sending')]:
            result = subprocess.CompletedProcess([], 1, stdout='', stderr=diagnostic)
            with self.subTest(operation=operation), patch('desk_orchestrator.hardware.subprocess.run', return_value=result) as run:
                with self.assertRaises(DeskError) as caught:
                    hw.irsend(operation, 'desk_learned_missing', 'KEY_CAPTURED')
                self.assertIn(operation, str(caught.exception))
                self.assertIn('/run/lirc/test-socket', str(caught.exception))
                self.assertIn(diagnostic, str(caught.exception))
                run.assert_called_once()
        with patch('desk_orchestrator.hardware.subprocess.run', return_value=
                   subprocess.CompletedProcess([], 1, stdout='x' * 10000, stderr='')):
            with self.assertRaises(DeskError) as caught:
                hw.irsend('LIST', '', '')
            self.assertLess(len(str(caught.exception)), 2200)

    def test_frame_ignores_leading_silence_and_stops_before_repeats(self):
        frame = ir.Frame()
        self.assertFalse(frame.feed('Using device /dev/test'))
        self.assertFalse(frame.feed('space 80000'))
        for i, value in enumerate(TIMINGS):
            self.assertFalse(frame.feed(f'{"space" if i % 2 else "pulse"} {value}'))
        self.assertTrue(frame.feed('space 40000'))
        self.assertEqual(frame.finish(), TIMINGS)

    def test_noise_truncation_and_malformed_frames_rejected(self):
        for lines in (['pulse 500'], ['pulse 500', 'pulse 500'], ['pulse bad'],
                      ['pulse -1'], ['overflow 1'], ['pulse 16777216']):
            with self.subTest(lines=lines), self.assertRaises(DeskError):
                frame = ir.Frame()
                for line in lines:
                    frame.feed(line)
                frame.finish()
        frame = ir.Frame()
        for i in range(1023):
            frame.feed(f'{"space" if i % 2 else "pulse"} 100')
        with self.assertRaises(DeskError):
            frame.feed('space 100')

    def test_capture_reads_stream_and_cleans_up(self):
        read, write = os.pipe()
        os.write(write, b'space 50000\npulse 9000\nspace 4500\npulse 560\nspace 560\npulse 560\ntimeout 40000\n')
        os.close(write)
        process = Mock(stdout=os.fdopen(read, 'rb', buffering=0))
        process.poll.return_value = None
        with patch.object(ir, 'check_receiver'), patch.object(ir.subprocess, 'Popen', return_value=process) as start:
            self.assertEqual(ir.capture('/dev/desk-ir-rx'), [9000, 4500, 560, 560, 560])
        argv = start.call_args.args[0]
        self.assertIn('/dev/desk-ir-rx', argv)
        self.assertNotIn('--raw', argv)
        self.assertEqual(argv[argv.index('--driver') + 1], 'default')
        self.assertEqual(start.call_args.kwargs['env']['LIRC_OPTIONS_PATH'], '/dev/null')
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        self.assertTrue(process.stdout.closed)

    def test_timeout_and_missing_receiver_cleanup(self):
        read, write = os.pipe()
        self.addCleanup(os.close, write)
        process = Mock(stdout=os.fdopen(read, 'rb', buffering=0))
        process.poll.return_value = None
        with patch.object(ir, 'check_receiver'), patch.object(ir.subprocess, 'Popen', return_value=process), self.assertRaisesRegex(DeskError, 'No IR signal'):
            ir.capture('/dev/desk-ir-rx', timeout=0)
        process.terminate.assert_called_once()
        self.assertTrue(process.stdout.closed)
        with self.assertRaises(DeskError):
            ir.capture('/tmp/arbitrary')

    def test_decoded_input_and_transmitter_rejected_before_starting_capture(self):
        for receiver in ('/dev/input/event4', '/dev/input/by-path/receiver', '/dev/desk-ir-tx'):
            with self.subTest(receiver=receiver), patch.object(ir.subprocess, 'Popen') as start:
                with self.assertRaisesRegex(DeskError, 'receiver'):
                    ir.capture(receiver)
                start.assert_not_called()
        with patch.object(ir.Path, 'resolve', return_value=Path('/dev/input/event4')):
            with self.assertRaisesRegex(DeskError, 'decoded input'):
                ir.receiver_path('/dev/custom-alias')

    def test_receiver_preflight_explains_missing_node_and_permissions(self):
        with patch.object(ir.Path, 'stat', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(DeskError, 'updated Pi installer'):
                ir.check_receiver('/dev/desk-ir-rx')
        with patch.object(ir.os, 'access', return_value=False):
            with self.assertRaisesRegex(DeskError, 'not readable and writable by the web service'):
                ir.check_receiver('/dev/null')
        with patch.object(ir.Path, 'stat', return_value=Mock(st_mode=0o100600)):
            with self.assertRaisesRegex(DeskError, 'character device'):
                ir.check_receiver('/dev/desk-ir-rx')

    def test_capture_exposes_bounded_mode2_error_including_unterminated_line(self):
        read, write = os.pipe()
        os.write(write, b'Using raw access\nmode2: Permission denied')
        os.close(write)
        process = Mock(stdout=os.fdopen(read, 'rb', buffering=0))
        process.poll.return_value = 1
        with patch.object(ir, 'check_receiver'), patch.object(ir.subprocess, 'Popen', return_value=process) as start:
            with self.assertRaisesRegex(DeskError, 'mode2: Permission denied'):
                ir.capture('/dev/desk-ir-rx')
        self.assertEqual(start.call_args.kwargs['stderr'], subprocess.STDOUT)
        self.assertTrue(process.stdout.closed)
        process.wait.assert_called_once()

    def test_code_output_reports_format_problem_instead_of_no_signal(self):
        read, write = os.pipe()
        os.write(write, b'code: 0xffffff00\ncode: 0x3c230001\n')
        os.close(write)
        process = Mock(stdout=os.fdopen(read, 'rb', buffering=0))
        process.poll.return_value = None
        with patch.object(ir, 'check_receiver'), patch.object(ir.subprocess, 'Popen', return_value=process):
            with self.assertRaisesRegex(DeskError, 'IR data arrived'):
                ir.capture('/dev/desk-ir-rx')
        process.terminate.assert_called_once()
        self.assertTrue(process.stdout.closed)

    @unittest.skipUnless(shutil.which('mode2') and shutil.which('stdbuf'), 'LIRC unavailable')
    def test_real_mode2_decodes_reported_pi_timing_bytes(self):
        # Bytes from the Pi's --raw "code:" output: leading silence, a short
        # pulse/space frame, then a receiver timeout. The default driver has
        # a FIFO input mode which exercises mode2 without physical hardware.
        import sys
        if sys.byteorder != 'little':
            self.skipTest('Captured Pi bytes are little endian')
        fifo = self.root / 'receiver.fifo'
        os.mkfifo(fifo)
        payload = bytes.fromhex('ffffff00 da358400 3c230001 8f110000 '
                                '36020001 23020000 3a020001 b94d0003')
        stop = threading.Event()
        errors = []
        def feed():
            fd = None
            try:
                while not stop.is_set():
                    try:
                        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                        break
                    except OSError as exc:
                        import errno
                        if exc.errno != errno.ENXIO:
                            raise
                        stop.wait(.01)
                if fd is not None:
                    os.write(fd, payload)
            except Exception as exc:
                errors.append(exc)
            finally:
                if fd is not None:
                    os.close(fd)
        writer = threading.Thread(target=feed)
        writer.start()
        try:
            with patch.object(ir, 'check_receiver'):
                self.assertEqual(ir.capture(str(fifo), timeout=3), [9020, 4495, 566, 547, 570])
            self.assertEqual(errors, [])
        finally:
            stop.set()
            writer.join(timeout=2)

    def test_learning_saves_unverified_command_preserving_existing_remote(self):
        original = copy.deepcopy(self.store.read()['inventory']['oppo']['settings'])
        with patch.object(ir, 'capture', return_value=TIMINGS):
            ir.add_command(self.store, 1, 'oppo', 'volume_up', learn=True)
        cfg = self.store.read()
        settings = cfg['inventory']['oppo']['settings']
        macro = settings['commands']['volume_up']
        self.assertFalse(macro['verified'])
        self.assertFalse(macro['discrete'])
        self.assertEqual(settings['remote'], original['remote'])
        self.assertEqual(settings['commands']['usb'], original['commands']['usb'])
        pulse = macro['sequence'][0]
        path = self.root / 'ir-codes' / (pulse['remote'] + '.conf')
        self.assertIn('flags RAW_CODES', path.read_text())
        self.assertIn('frequency 38000', path.read_text())
        self.assertEqual(cfg['ir']['devices']['oppo']['commands']['volume_up'], macro)
        with self.assertRaises(DeskError):
            Hardware(cfg).check(dict(kind='ir', device='oppo', command='volume_up'))

    def test_conflicts_and_failed_capture_do_not_mutate_config(self):
        for revision, command in ((0, 'new'), (1, 'usb'), (1, '../bad')):
            with patch.object(ir, 'capture') as capture, self.assertRaises(DeskError):
                ir.add_command(self.store, revision, 'oppo', command, learn=True)
            capture.assert_not_called()
        def concurrent(_):
            self.store.update(1, lambda cfg: None)
            return TIMINGS
        with patch.object(ir, 'capture', side_effect=concurrent), self.assertRaises(DeskError):
            ir.add_command(self.store, 1, 'oppo', 'new', learn=True)
        self.assertEqual(list((self.root / 'ir-codes').glob('*.conf')), [])
        self.assertNotIn('new', self.store.read()['ir']['devices']['oppo']['commands'])
        with exclusive(self.root / 'scene.lock'), patch.object(ir, 'capture') as capture, self.assertRaises(DeskError):
            ir.add_command(self.store, 2, 'oppo', 'new', learn=True)
        capture.assert_not_called()

    def test_existing_key_import_and_remote_override_execution(self):
        with patch.object(ir, 'catalog', return_value=['KEY_UP']):
            ir.add_command(self.store, 1, 'oppo', 'volume_up', remote='other_remote', key='KEY_UP')
        cfg = self.store.read()
        cfg['ir']['devices']['oppo']['commands']['volume_up']['verified'] = True
        hw = Hardware(cfg)
        step = dict(kind='ir', device='oppo', command='volume_up')
        self.assertEqual(target(cfg, step)["remote"], "other_remote")
        with patch.object(hw, 'irsend', return_value='0000 KEY_UP\n') as send, patch('time.sleep'):
            hw.check(step, probe=True)
            hw.execute(step)
        self.assertEqual(send.call_args_list[0].args, ('LIST', 'other_remote', ''))
        self.assertEqual(send.call_args_list[1].args, ('SEND_ONCE', 'other_remote', 'KEY_UP'))

    @unittest.skipUnless(shutil.which('lircd') and shutil.which('irsend'), 'LIRC unavailable')
    def test_real_lircd_reads_generated_codes_and_reloads(self):
        codes = self.root / 'ir-codes'
        codes.mkdir()
        config = self.root / 'lircd.conf'
        config.write_text(f'include "{codes}/*.conf"\n')
        socket = self.root / 'lircd.sock'
        process = subprocess.Popen(['lircd', '--nodaemon', '--driver=default', '--device=/dev/null', '--options-file=/dev/null',
            f'--output={socket}', f'--pidfile={self.root}/pid', f'--logfile={self.root}/lirc.log', str(config)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                if socket.exists() or process.poll() is not None:
                    break
                time.sleep(.02)
            self.assertIsNone(process.poll(), (self.root / 'lirc.log').read_text())
            cfg = {'ir': {'socket': str(socket)}}
            self.assertEqual(ir.catalog(cfg), [])
            path, remote = ir.write_capture(codes, TIMINGS, 38000)
            process.send_signal(signal.SIGHUP)
            for _ in range(50):
                if remote in ir.catalog(cfg):
                    break
                time.sleep(.02)
            self.assertEqual(ir.catalog(cfg), [remote])
            self.assertEqual(ir.catalog(cfg, remote), ['KEY_CAPTURED'])
        finally:
            process.terminate()
            process.wait(timeout=3)


class LearningWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        set_password(self.tmp.name, 'ir-learning-test-password')
        self.app = create_app(self.tmp.name, SEED)
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions['desk_store']
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf='test-csrf')
        self.catalog = patch.object(ir, 'catalog', side_effect=lambda cfg, remote='': ['KEY_UP'] if remote else ['oppo_ha1']).start()
        self.addCleanup(patch.stopall)

    def post(self, **fields):
        return self.client.post('/hardware/oppo/ir', data={
            'csrf': 'test-csrf', 'revision': self.store.read()['revision'], **fields})

    def test_browse_and_add_then_test_and_verify(self):
        response = self.client.get('/hardware/oppo/ir?remote=oppo_ha1')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'KEY_UP', response.data)
        self.assertIn(b'name="csrf" value="test-csrf"', response.data)
        self.assertEqual(self.post(action='add', command='volume_up', remote='oppo_ha1', key='KEY_UP').status_code, 302)
        self.assertEqual(self.post(action='verify', command='volume_up', observed='on').status_code, 422)
        with patch.object(Hardware, 'irsend') as send:
            self.assertEqual(self.post(action='test', command='volume_up').status_code, 302)
            send.assert_called_once_with('SEND_ONCE', 'oppo_ha1', 'KEY_UP')
        self.assertEqual(self.post(action='verify', command='volume_up').status_code, 422)
        self.assertEqual(self.post(action='verify', command='volume_up', observed='on').status_code, 302)
        self.assertTrue(self.store.read()['ir']['devices']['oppo']['commands']['volume_up']['verified'])

    def test_learning_and_errors_preserve_unverified_state(self):
        with patch.object(ir, 'capture', return_value=TIMINGS):
            self.assertEqual(self.post(action='learn', command='volume_up').status_code, 302)
        self.assertFalse(self.store.read()['ir']['devices']['oppo']['commands']['volume_up']['verified'])
        with patch.object(ir, 'capture', side_effect=DeskError('No IR signal received')):
            response = self.post(action='learn', command='volume_down')
        self.assertEqual(response.status_code, 422)
        self.assertIn(b'No IR signal received', response.data)
        self.assertNotIn('volume_down', self.store.read()['ir']['devices']['oppo']['commands'])

    def test_saved_capture_missing_from_lirc_is_explained_without_sending(self):
        with patch.object(ir, 'capture', return_value=TIMINGS):
            self.post(action='learn', command='volume_up')
        with patch.object(Hardware, 'irsend') as send:
            response = self.post(action='test', command='volume_up')
        self.assertEqual(response.status_code, 422)
        self.assertIn(b'LIRC has not loaded remote', response.data)
        self.assertIn(b'desk-ir-reload.path', response.data)
        send.assert_not_called()
        self.assertFalse(self.store.read()['ir']['devices']['oppo']['commands']['volume_up']['verified'])

    def test_failed_send_reports_diagnostic_and_clears_previous_verification(self):
        self.post(action='add', command='volume_up', remote='oppo_ha1', key='KEY_UP')
        with patch.object(Hardware, 'irsend'):
            self.assertEqual(self.post(action='test', command='volume_up').status_code, 302)
        with patch.object(Hardware, 'irsend', side_effect=DeskError('LIRC SEND_ONCE failed: <transmission failed>')) as send:
            response = self.post(action='test', command='volume_up')
        self.assertEqual(response.status_code, 422)
        self.assertIn(b'LIRC SEND_ONCE failed: &lt;transmission failed&gt;', response.data)
        send.assert_called_once()
        self.assertEqual(self.post(action='verify', command='volume_up', observed='on').status_code, 422)

    def test_mutation_requires_csrf_login_current_revision(self):
        with patch.object(ir, 'capture') as capture:
            self.assertEqual(self.client.post('/hardware/oppo/ir', data={'action': 'learn'}).status_code, 400)
            self.assertEqual(self.post(action='learn', command='new', revision=0).status_code, 422)
            capture.assert_not_called()
        with self.client.session_transaction() as session:
            session.pop('authenticated')
        self.assertEqual(self.post(action='learn', command='new').status_code, 302)

    def test_settings_reject_evdev_receiver_without_changing_numpad(self):
        before = self.store.read()
        response = self.client.post('/settings', data={
            'csrf': 'test-csrf', 'revision': before['revision'],
            'ir_socket': '/run/lirc/lircd', 'ir_receiver': '/dev/input/event4'})
        self.assertEqual(response.status_code, 409)
        self.assertIn(b'decoded input device', response.data)
        self.assertEqual(self.store.read(), before)
        response = self.client.post('/settings', data={
            'csrf': 'test-csrf', 'revision': before['revision'],
            'ir_socket': '/run/lirc/lircd', 'ir_receiver': '/dev/desk-ir-rx'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.store.read()['keypad'], before['keypad'])

    def test_lirc_offline_keeps_page_usable(self):
        self.catalog.side_effect = DeskError('LIRC unavailable')
        response = self.client.get('/hardware/oppo/ir')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'LIRC unavailable', response.data)
        self.assertIn(b'Start learning', response.data)

    def test_changed_command_cannot_be_verified_from_previous_test(self):
        self.post(action='add', command='volume_up', remote='oppo_ha1', key='KEY_UP')
        with patch.object(Hardware, 'irsend'):
            self.post(action='test', command='volume_up')
        def edit(cfg):
            cfg['inventory']['oppo']['settings']['commands']['volume_up']['sequence'][0]['key'] = 'KEY_OTHER'
        self.store.update(self.store.read()['revision'], edit)
        self.assertEqual(self.post(action='verify', command='volume_up', observed='on').status_code, 422)
