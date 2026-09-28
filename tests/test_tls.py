from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from desk_orchestrator import tls, tls_dns
from desk_orchestrator.core import DeskError, atomic_json, exclusive
from desk_orchestrator.web import create_app, set_password

CONFIG = dict(domain="smartthings.example.com", zone="example.com", profile="default", email="owner@example.com", agreed=True)
SEED = Path(__file__).resolve().parents[1] / "config/desk.example.toml"
TOKEN = "acme-challenge-value-123456789"


class TLSTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.base = tls.root(self.directory)
        tls.save(self.directory, dict(CONFIG, revision=0))

    def certificate(self, *, name=CONFIG["domain"], expired=False, serial=1):
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
                .serial_number(serial).not_valid_before(now - timedelta(days=2))
                .not_valid_after(now + timedelta(days=-1 if expired else 30))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
                .sign(key, hashes.SHA256()))
        cert_path, key_path = tls.certificate_paths(self.directory)
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        return cert_path, key_path

    def test_config_validation_and_private_state(self):
        for changes in (dict(domain="https://x.example.com"), dict(domain="x.evil-example.com"), dict(zone="evil.com"), dict(domain="*.example.com"), dict(email="a\nb@example.com")):
            with self.subTest(changes=changes), self.assertRaises(DeskError):
                tls.validate(dict(CONFIG, **changes))
        self.assertEqual((self.base / "settings.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(DeskError, "changed"):
            tls.save(self.directory, dict(CONFIG, revision=0))

    def test_terms_required_before_staging_or_production(self):
        tls.save(self.directory, dict(CONFIG, revision=1, agreed=False))
        with patch.object(tls, "installed", return_value=True), patch.object(tls.subprocess, "Popen") as launch:
            for action in ("staging", "issue"):
                with self.assertRaisesRegex(DeskError, "agreement"):
                    tls.start(self.directory, action)
        launch.assert_not_called()

    def test_queued_job_blocks_another_job_and_edits(self):
        with patch.object(tls, "installed", return_value=True), patch.object(tls.subprocess, "Popen"):
            tls.start(self.directory, "issue")
            self.assertTrue(tls.status(self.directory)["busy"])
            with self.assertRaises(DeskError):
                tls.start(self.directory, "staging")
            with self.assertRaises(DeskError):
                tls.save(self.directory, dict(CONFIG, revision=1))

    def test_stale_job_recovers_and_lock_blocks_running_job(self):
        atomic_json(self.base / "job.json", dict(state="running", time=0))
        self.assertEqual(tls.status(self.directory)["job"]["state"], "interrupted")
        with exclusive(self.base / "job.lock"):
            self.assertTrue(tls.status(self.directory)["busy"])
            with self.assertRaises(DeskError):
                tls.save(self.directory, dict(CONFIG, revision=1))

    def test_separate_acme_directories_and_fixed_hooks(self):
        staging = tls.certbot_command(self.base, CONFIG, "staging")
        production = tls.certbot_command(self.base, CONFIG, "production")
        self.assertIn(tls.SERVERS["staging"], staging)
        self.assertNotIn(tls.SERVERS["staging"], production)
        self.assertIn(str(self.base / "staging/config"), staging)
        self.assertIn(str(self.base / "production/config"), production)
        self.assertIn("--force-renewal", staging)
        self.assertIn("--keep-until-expiring", production)
        self.assertIn("desk_orchestrator.tls_dns", production[production.index("--manual-auth-hook") + 1])

    def queue(self, action):
        with patch.object(tls, "installed", return_value=True), patch.object(tls.subprocess, "Popen"):
            tls.start(self.directory, action)
        return tls.read(self.base / "job.json")["id"]

    def test_legacy_profile_is_ignored_and_removed_on_save(self):
        self.assertNotIn("profile", tls.status(self.directory)["config"])
        self.assertNotIn("profile", tls.read(self.base / "settings.json"))
        with self.assertRaisesRegex(DeskError, "Unknown"):
            tls.start(self.directory, "check")

    def test_legacy_automatic_job_cannot_launch_certbot_after_upgrade(self):
        atomic_json(self.base / "job.json", dict(id="legacy", config=CONFIG, action="renew", state="queued"))
        with patch.object(tls.subprocess, "Popen") as launch:
            self.assertEqual(tls.worker(self.directory, "legacy"), 1)
        launch.assert_not_called()
        self.assertIn("Restart", tls.status(self.directory)["job"]["message"])

    def test_failure_cleans_challenge_and_preserves_existing_certificate(self):
        cert_path, _ = self.certificate()
        before = cert_path.read_bytes()
        ident = self.queue("issue")
        child = Mock(); child.wait.return_value = 1
        with patch.object(tls_dns, "cleanup") as cleanup, patch.object(tls.subprocess, "Popen", return_value=child):
            self.assertEqual(tls.worker(self.directory, ident), 1)
        self.assertEqual(cleanup.call_count, 1)
        self.assertEqual(cert_path.read_bytes(), before)
        self.assertEqual(tls.status(self.directory)["job"]["state"], "failed")

    def test_staging_success_never_activates_a_certificate(self):
        ident = self.queue("staging")
        child = Mock(); child.wait.return_value = 0
        with patch.object(tls.subprocess, "Popen", return_value=child):
            self.assertEqual(tls.worker(self.directory, ident), 0)
        self.assertFalse(tls.certificate_paths(self.directory)[0].exists())
        self.assertEqual(tls.status(self.directory)["expires"], "")

    def test_expired_and_wrong_hostname_certificates_are_rejected(self):
        self.certificate(name="wrong.example.com")
        with self.assertRaisesRegex(DeskError, "hostname"):
            tls.certificate(self.directory, CONFIG["domain"])
        self.certificate(expired=True)
        with self.assertRaisesRegex(DeskError, "valid"):
            tls.certificate(self.directory, CONFIG["domain"])
        self.certificate()
        context, expiry = tls.certificate(self.directory, CONFIG["domain"])
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        self.assertTrue(expiry)

    def test_certificate_replacement_creates_a_fresh_context(self):
        self.certificate(serial=1)
        old, _ = tls.certificate(self.directory, CONFIG["domain"])
        self.certificate(serial=2)
        new, _ = tls.certificate(self.directory, CONFIG["domain"])
        self.assertIsNot(old, new)
        with self.assertRaisesRegex(DeskError, "hostname"):
            tls.save(self.directory, dict(CONFIG, domain="other.example.com", revision=1))

    def test_web_requires_login_csrf_and_explicit_consent(self):
        set_password(self.directory, "test-password-long")
        app = create_app(self.directory, SEED)
        client = app.test_client()
        with patch.object(tls, "start") as start:
            self.assertEqual(client.post("/https/action", data=dict(action="issue")).status_code, 400)
            with client.session_transaction() as session:
                session["csrf"] = "browser-csrf"
            self.assertEqual(client.post("/https/action", data=dict(action="issue", csrf="browser-csrf")).status_code, 302)
            start.assert_not_called()
            with client.session_transaction() as session:
                session["authenticated"] = True
            response = client.get("/settings")
            self.assertIn(b"Test issuance (staging)", response.data)
            client.post("/https/action", data=dict(action="staging", csrf="browser-csrf"))
            start.assert_called_once_with(self.directory, "staging")


class DNSHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.receipt = self.base / "challenge.json"

    def confirm(self, *, cancel=False):
        challenge = tls_dns.read(self.receipt)
        if cancel:
            atomic_json(self.base / "cancel.json", dict(job_id="job"))
        else:
            atomic_json(self.base / "continue.json", dict(challenge_id=challenge["id"], id="click"))

    def test_challenge_waits_for_confirmation_and_public_dns(self):
        with patch.object(tls_dns.time, "sleep", side_effect=lambda _: self.confirm()), \
             patch.object(tls_dns, "public_txt_matches", return_value=True) as check:
            tls_dns.authenticate(CONFIG, self.receipt, CONFIG["domain"], TOKEN, "job")
        check.assert_called_once_with("_acme-challenge." + CONFIG["domain"], TOKEN)
        saved = tls_dns.read(self.receipt)
        self.assertEqual(saved["state"], "validated")
        self.assertEqual(saved["record"]["target"], TOKEN)
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        tls_dns.cleanup(self.receipt)
        self.assertEqual(tls_dns.read(self.receipt)["state"], "cleanup")
        self.assertEqual(tls_dns.read(self.receipt)["record"], saved["record"])

    def test_public_dns_failure_returns_to_waiting_and_can_cancel(self):
        states = []
        def tick(_):
            states.append(tls_dns.read(self.receipt)["state"])
            self.confirm(cancel=len(states) > 1)
        with patch.object(tls_dns.time, "sleep", side_effect=tick), \
             patch.object(tls_dns, "public_txt_matches", return_value=False), \
             self.assertRaisesRegex(DeskError, "cancelled"):
            tls_dns.authenticate(CONFIG, self.receipt, CONFIG["domain"], TOKEN, "job")
        self.assertEqual(states, ["waiting", "waiting"])
        self.assertIn("not visible", tls_dns.read(self.receipt)["message"])

    def test_stale_confirmation_cannot_validate_new_challenge(self):
        atomic_json(self.base / "continue.json", dict(challenge_id="old", id="old"))
        with patch.object(tls_dns.time, "sleep", side_effect=lambda _: self.confirm(cancel=True)), \
             patch.object(tls_dns, "public_txt_matches") as check, self.assertRaises(DeskError):
            tls_dns.authenticate(CONFIG, self.receipt, CONFIG["domain"], TOKEN, "job")
        check.assert_not_called()

    def test_unexpected_challenges_do_not_write_state(self):
        for domain, token in (("evil.com", TOKEN), (CONFIG["domain"], "bad\nvalue")):
            with self.assertRaises(DeskError):
                tls_dns.authenticate(CONFIG, self.receipt, domain, token, "job")
        self.assertFalse(self.receipt.exists())

    def test_timeout_retains_exact_record_for_manual_cleanup(self):
        with patch.object(tls_dns.time, "monotonic", side_effect=[0, tls_dns.CHALLENGE_SECONDS + 1]), \
             self.assertRaisesRegex(DeskError, "expired"):
            tls_dns.authenticate(CONFIG, self.receipt, CONFIG["domain"], TOKEN, "job")
        tls_dns.cleanup(self.receipt)
        self.assertEqual(tls_dns.read(self.receipt)["record"]["target"], TOKEN)

    def test_old_receipt_is_not_overwritten(self):
        atomic_json(self.receipt, dict(record={"target": "old"}))
        with self.assertRaisesRegex(DeskError, "cleanup"):
            tls_dns.authenticate(CONFIG, self.receipt, CONFIG["domain"], TOKEN, "job")
        self.assertEqual(tls_dns.read(self.receipt)["record"]["target"], "old")

    def test_public_resolvers_must_both_see_exact_value(self):
        import dns.resolver
        import dns.exception
        answer = Mock(strings=[TOKEN.encode()])
        with patch.object(dns.resolver, "Resolver") as resolver:
            resolver.return_value.resolve.side_effect = [[answer], [answer]]
            self.assertTrue(tls_dns.public_txt_matches("_acme-challenge.example.com", TOKEN))
            self.assertEqual(resolver.return_value.nameservers, ["8.8.8.8"])
            resolver.return_value.resolve.side_effect = [[answer], [Mock(strings=[b"other"])]]
            self.assertFalse(tls_dns.public_txt_matches("_acme-challenge.example.com", TOKEN))
            resolver.return_value.resolve.side_effect = dns.exception.Timeout()
            self.assertFalse(tls_dns.public_txt_matches("_acme-challenge.example.com", TOKEN))


class ManualFlowTests(unittest.TestCase):
    def setUp(self):
        fixture = TLSTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.directory, self.base = fixture.directory, fixture.base
        set_password(self.directory, "test-password-long")
        self.client = create_app(self.directory, SEED).test_client()
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf="csrf")

    def launch(self):
        with patch.object(tls, "installed", return_value=True), patch.object(tls.subprocess, "Popen"):
            tls.start(self.directory, "staging")
        ident = tls.read(self.base / "job.json")["id"]
        # Actual worker + separate hook process, but no ACME or network access.
        hook = """
import os
from pathlib import Path
from desk_orchestrator import tls_dns
job = Path(os.environ['DESK_TLS_JOB'])
data = tls_dns.read(job)
tls_dns.public_txt_matches = lambda *args: True
tls_dns.authenticate(data['config'], job.parent / 'challenge.json', data['config']['domain'], 'acme-challenge-value-123456789', data['id'])
tls_dns.cleanup(job.parent / 'challenge.json')
"""
        script = """
import sys
from desk_orchestrator import tls
tls.certbot_command = lambda *args: [sys.executable, '-c', sys.argv[3]]
sys.exit(tls.worker(sys.argv[1], sys.argv[2]))
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.directory), ident, hook], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        def stop():
            if child.poll() is None:
                atomic_json(self.base / "cancel.json", dict(job_id=ident))
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()
            child.stderr.close()
        self.addCleanup(stop)
        for _ in range(100):
            if (self.base / "challenge.json").exists():
                return ident, child
            if child.poll() is not None:
                self.fail(child.stderr.read().decode())
            time.sleep(.05)
        self.fail("Manual challenge was not published")

    def test_complete_manual_flow_via_web(self):
        ident, child = self.launch()
        state = tls.status(self.directory)
        challenge = state["challenge"]
        response = self.client.get("/https/status")
        self.assertIn(TOKEN.encode(), response.data)
        self.assertIn(b"_acme-challenge.smartthings", response.data)
        self.assertIn(b"Check TXT and continue", response.data)
        self.assertNotIn(b"AWS profile", self.client.get("/settings").data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        with self.assertRaises(DeskError):
            tls.start(self.directory, "issue")
        with self.assertRaises(DeskError):
            tls.control(self.directory, "cleanup", challenge["id"])
        self.assertEqual(self.client.post("/https/control", data=dict(action="continue", id=challenge["id"])).status_code, 400)
        tls_status = self.client.post("/https/control", data=dict(csrf="csrf", action="continue", id=challenge["id"]))
        self.assertEqual(tls_status.status_code, 302)
        self.assertEqual(child.wait(timeout=8), 0, child.stderr.read().decode())
        self.assertEqual(tls.status(self.directory)["job"]["state"], "succeeded")
        with self.assertRaisesRegex(DeskError, "cleanup"):
            tls.start(self.directory, "issue")
        self.assertIn(b"I removed this TXT value", self.client.get("/https/status").data)
        self.client.post("/https/control", data=dict(csrf="csrf", action="cleanup", id=challenge["id"]))
        self.assertFalse((self.base / "challenge.json").exists())

    def test_cancel_terminates_worker_and_preserves_cleanup(self):
        ident, child = self.launch()
        with self.assertRaisesRegex(DeskError, "changed"):
            tls.control(self.directory, "cancel", "old-job")
        tls.control(self.directory, "cancel", ident)
        self.assertEqual(child.wait(timeout=8), 1)
        state = tls.status(self.directory)
        self.assertFalse(state["busy"])
        self.assertEqual(state["job"]["state"], "cancelled")
        self.assertEqual(state["challenge"]["state"], "cleanup")
        self.assertEqual(state["challenge"]["value"], TOKEN)

    def test_interrupted_and_legacy_challenges_offer_manual_cleanup(self):
        atomic_json(self.base / "challenge.json", dict(config=CONFIG, record=dict(name="_acme-challenge." + CONFIG["domain"], target='"' + TOKEN + '"')))
        atomic_json(self.base / "job.json", dict(state="running", id="old", time=0))
        state = tls.status(self.directory)
        self.assertEqual(state["job"]["state"], "interrupted")
        self.assertEqual(state["challenge"]["value"], TOKEN)
        self.assertIn(b'I removed this TXT value', self.client.get("/https/status").data)
        tls.control(self.directory, "cleanup", "legacy")
        self.assertFalse((self.base / "challenge.json").exists())

    def test_status_and_control_require_login(self):
        with self.client.session_transaction() as session:
            session.pop("authenticated")
        self.assertEqual(self.client.get("/https/status").status_code, 302)
        with patch.object(tls, "control") as control:
            self.client.post("/https/control", data=dict(csrf="csrf", action="cancel", id="id"))
            control.assert_not_called()


if __name__ == "__main__":
    unittest.main()
