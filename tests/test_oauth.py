import base64
import json
import os
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from desk_orchestrator.core import DeskError, atomic_json
from desk_orchestrator.oauth import TTL, status, validate_redirect
from desk_orchestrator.smartthings import HTTPFailure
from desk_orchestrator.web import create_app, set_password

SEED = Path(__file__).resolve().parents[1] / "config/desk.example.toml"
FRESH = dict(access_token="private-access", refresh_token="private-refresh", expires_in=86400,
             token_type="bearer", scope="r:devices:* x:devices:*")


def authorization_url(response):
    class LinkParser(HTMLParser):
        url = None
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and attrs.get("id") == "smartthings-authorize":
                self.url = attrs["href"]
    parser = LinkParser()
    parser.feed(response.get_data(as_text=True))
    return parser.url


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.env = patch.dict(os.environ, SMARTTHINGS_CLIENT_ID="test-client", SMARTTHINGS_CLIENT_SECRET="private-secret")
        self.env.start()
        self.addCleanup(self.env.stop)
        set_password(self.directory, "private-test-password")
        self.app = create_app(self.directory, SEED, trusted_hosts=["localhost", "desk.example.com"])
        self.app.testing = True
        self.client = self.app.test_client()
        self.store = self.app.extensions["desk_store"]
        with self.client.session_transaction() as session:
            session.update(authenticated=True, csrf="test-browser-csrf")
        self.configure()

    def form(self, **kwargs):
        return dict(csrf="test-browser-csrf", revision=self.store.read()["revision"], **kwargs)

    def configure(self, uri="http://localhost/oauth/callback", endpoint="/oauth/token"):
        return self.client.post("/smartthings/oauth/configure", data=self.form(redirect_uri=uri, token_endpoint=endpoint))

    def start(self):
        response = self.client.post("/smartthings/oauth/connect", data=self.form())
        self.assertEqual(response.status_code, 200)
        return response, parse_qs(urlsplit(authorization_url(response)).query)["state"][0]

    def finish(self, state, **kwargs):
        return self.client.get("/oauth/callback", query_string=dict(state=state, code="private-code", **kwargs))

    def test_authorization_url_and_browser_session_cookie(self):
        response, state = self.start()
        url = urlsplit(authorization_url(response))
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "api.smartthings.com", "/oauth/authorize"))
        params = parse_qs(url.query)
        self.assertEqual(params["redirect_uri"], ["http://localhost/oauth/callback"])
        self.assertEqual(params["scope"], ["r:devices:* x:devices:*"])
        self.assertGreaterEqual(len(state), 40)
        self.assertNotIn("private-secret", response.get_data(as_text=True))
        self.assertNotIn("Location", response.headers)
        self.assertIn("form-action 'self'", response.headers["Content-Security-Policy"])
        self.assertIn(b'/static/oauth_authorize.js', response.data)
        self.assertIn(b'Continue to SmartThings', response.data)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(self.app.config["SESSION_COOKIE_SAMESITE"], "Lax")
        self.assertTrue(self.app.config["SESSION_COOKIE_HTTPONLY"])
        pending = self.directory / "oauth-pending.json"
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(state, pending.read_text())
        self.assertNotIn("private-secret", pending.read_text())

    def test_exchange_activate_and_replay(self):
        _, state = self.start()
        with patch("desk_orchestrator.oauth.request", return_value=dict(FRESH)) as call:
            response = self.finish(state)
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.location, "/settings")
            call.assert_called_once_with("POST", "/oauth/token", form=True,
                basic=base64.b64encode(b"test-client:private-secret").decode(), payload=dict(
                    grant_type="authorization_code", code="private-code", client_id="test-client",
                    redirect_uri="http://localhost/oauth/callback"))
            self.finish(state)
            self.assertEqual(call.call_count, 1)
        config = self.store.read()["smartthings"]
        token_path = Path(config["token_file"])
        tokens = json.loads(token_path.read_text())
        self.assertEqual(config["token_endpoint"], "/oauth/token")
        self.assertEqual(tokens["refresh_token"], FRESH["refresh_token"])
        self.assertGreater(tokens["expires_at"], time.time() + 86000)
        self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.directory / "oauth-pending.json").exists())
        for secret in ("private-code", "private-access", "private-refresh", "private-secret"):
            self.assertNotIn(secret, response.get_data(as_text=True))
            self.assertNotIn(secret, response.headers.get("Set-Cookie", ""))
            self.assertNotIn(secret, self.client.get("/backup").text)

    def test_mismatched_state_and_browser_never_exchange(self):
        _, state = self.start()
        with patch("desk_orchestrator.oauth.request") as call:
            self.finish("wrong-state")
            other = self.app.test_client()
            with other.session_transaction() as session:
                session.update(authenticated=True, csrf="other-browser")
            other.get("/oauth/callback", query_string=dict(state=state, code="private-code"))
            self.assertTrue((self.directory / "oauth-pending.json").exists())
            call.assert_not_called()

    def test_token_exchange_error_identifies_client_authentication_without_secrets(self):
        _, state = self.start()
        before = self.store.read()
        with patch("desk_orchestrator.oauth.request", side_effect=HTTPFailure(401)):
            self.finish(state)
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("OAuth token exchange failed at /oauth/token (HTTP 401)", page)
        self.assertIn("rejected client authentication", page)
        self.assertIn("Restart the HTTPS service", page)
        self.assertNotIn("check credentials, permissions, device, and capability", page)
        for secret in ("private-secret", "private-code", "test-client"):
            self.assertNotIn(secret, page)
        self.assertEqual(self.store.read(), before)
        self.assertFalse((self.directory / "oauth-pending.json").exists())

    def test_expired_and_denied_are_consumed_without_exchange(self):
        for scenario in ("expired", "denied"):
            with self.subTest(scenario=scenario):
                _, state = self.start()
                path = self.directory / "oauth-pending.json"
                if scenario == "expired":
                    pending = json.loads(path.read_text())
                    pending["created"] -= TTL + 1
                    atomic_json(path, pending)
                with patch("desk_orchestrator.oauth.request") as call:
                    self.finish(state, **({"error": "access_denied"} if scenario == "denied" else {}))
                    call.assert_not_called()
                self.assertFalse(path.exists())
                self.assertNotIn("token_file", self.store.read()["smartthings"])

    def test_changed_settings_or_credentials_block_exchange(self):
        _, state = self.start()
        self.configure(endpoint="/v1/oauth/token")
        with patch("desk_orchestrator.oauth.request") as call:
            self.finish(state)
            call.assert_not_called()
        _, state = self.start()
        with patch.dict(os.environ, SMARTTHINGS_CLIENT_SECRET="changed"), patch("desk_orchestrator.oauth.request") as call:
            self.finish(state)
            call.assert_not_called()

    def test_failed_exchange_and_invalid_tokens_preserve_existing_connection(self):
        old = self.directory / "old.json"
        atomic_json(old, dict(FRESH, expires_at=time.time() + 3600))
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["smartthings"].update(token_file=str(old), token_endpoint="/v1/oauth/token"))
        before = self.store.read()["smartthings"]
        for reply in [HTTPFailure(400), {}, dict(FRESH, refresh_token=""), dict(FRESH, expires_in="bad"),
                      dict(FRESH, scope="r:devices:*"), dict(FRESH, token_type=None)]:
            with self.subTest(reply=repr(reply)):
                _, state = self.start()
                with patch("desk_orchestrator.oauth.request", **({"side_effect": reply} if isinstance(reply, Exception) else {"return_value": reply})):
                    self.finish(state)
                self.assertEqual(self.store.read()["smartthings"], before)
                self.assertEqual(json.loads(old.read_text())["access_token"], FRESH["access_token"])

    def test_failed_activation_removes_staged_tokens(self):
        _, state = self.start()
        before = self.store.read()
        with patch("desk_orchestrator.oauth.request", return_value=dict(FRESH)), patch.object(self.store, "update", side_effect=DeskError("Configuration changed.")):
            self.finish(state)
        self.assertEqual(self.store.read(), before)
        self.assertEqual(list((self.directory / "oauth").glob("*.json")), [])

    def test_setup_does_not_change_existing_refresh_endpoint(self):
        self.store.update(self.store.read()["revision"], lambda cfg: cfg["smartthings"].update(token_endpoint="/v1/oauth/token"))
        self.configure(endpoint="/oauth/token")
        self.assertEqual(self.store.read()["smartthings"]["token_endpoint"], "/v1/oauth/token")
        response, _ = self.start()
        self.assertEqual(urlsplit(authorization_url(response)).path, "/oauth/authorize")

    def test_preview_endpoints_and_restart(self):
        self.configure(endpoint="/v1/oauth/token")
        response, state = self.start()
        self.assertEqual(urlsplit(authorization_url(response)).path, "/v1/oauth/authorize")
        # Persistent authorization state and signed browser cookie survive restart.
        restarted = create_app(self.directory, SEED)
        client = restarted.test_client()
        client.set_cookie("desk_session", self.client.get_cookie("desk_session").value)
        with patch("desk_orchestrator.oauth.request", return_value=dict(FRESH)) as call:
            client.get("/oauth/callback", query_string=dict(state=state, code="private-code"))
            self.assertEqual(call.call_args.args[1], "/v1/oauth/token")

    def test_redirect_validation(self):
        for uri in ["https://desk.example.com/oauth/callback", "http://127.0.0.1:8080/oauth/callback"]:
            validate_redirect(uri)
        for uri in ["https://evil.test/other", "http://public.test/oauth/callback", "//evil.test/oauth/callback",
                    "https://user:pass@host/oauth/callback", "https://host/oauth/callback?x=1", "https://host/oauth/callback#fragment",
                    "https://host:bad/oauth/callback", "https://host/oauth/callback\n"]:
            with self.subTest(uri=uri), self.assertRaises(DeskError):
                validate_redirect(uri)

    def test_connect_requires_credentials_matching_host_csrf_and_saved_revision(self):
        with patch("desk_orchestrator.oauth.request") as call:
            self.assertEqual(self.client.get("/smartthings/oauth/connect").status_code, 405)
            self.assertEqual(self.client.post("/smartthings/oauth/connect").status_code, 400)
            self.client.post("/smartthings/oauth/connect", data=dict(self.form(), revision=0))
            self.assertFalse((self.directory / "oauth-pending.json").exists())
            with patch.dict(os.environ, SMARTTHINGS_CLIENT_SECRET=""):
                self.client.post("/smartthings/oauth/connect", data=self.form())
            self.assertFalse((self.directory / "oauth-pending.json").exists())
            self.configure("https://desk.example.com/oauth/callback")
            self.client.post("/smartthings/oauth/connect", data=self.form())
            self.assertFalse((self.directory / "oauth-pending.json").exists())
            call.assert_not_called()

    def test_callback_without_login_does_not_exchange_or_echo_code(self):
        _, state = self.start()
        other = self.app.test_client()
        with patch("desk_orchestrator.oauth.request") as call:
            response = other.get("/oauth/callback", query_string=dict(state=state, code="private-code"))
            call.assert_not_called()
        self.assertEqual(response.location, "/settings")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("private-code", response.text)

    def test_registered_host_is_allowed_but_unrelated_hosts_remain_blocked(self):
        self.configure("https://desk.example.com/oauth/callback")
        app = create_app(self.directory, SEED)
        client = app.test_client()
        self.assertEqual(client.get("/login", base_url="https://desk.example.com").status_code, 200)
        self.assertEqual(client.get("/login", base_url="https://unrelated.test").status_code, 400)
        with client.session_transaction(base_url="https://desk.example.com") as session:
            session.update(authenticated=True, csrf="host-csrf")
        response = client.post("/smartthings/oauth/connect", base_url="https://desk.example.com",
                               data=dict(csrf="host-csrf", revision=self.store.read()["revision"]))
        self.assertEqual(response.status_code, 200)
        state = parse_qs(urlsplit(authorization_url(response)).query)["state"][0]
        with patch("desk_orchestrator.oauth.request", return_value=dict(FRESH)):
            response = client.get("/oauth/callback", base_url="https://desk.example.com",
                                  query_string=dict(state=state, code="private-code"))
        self.assertEqual(response.location, "/settings")
        self.assertIn("token_file", self.store.read()["smartthings"])

    def test_invalid_endpoints_cannot_receive_credentials_and_checks_require_oauth(self):
        before = self.store.read()
        self.configure(endpoint="//evil.test/oauth/token")
        self.assertEqual(self.store.read(), before)
        with patch("desk_orchestrator.web.Hardware") as hardware:
            self.client.post("/smartthings/oauth/check", data=self.form(refresh="yes"))
            hardware.assert_not_called()
        _, state = self.start()
        with patch("desk_orchestrator.oauth.request") as call:
            self.client.get("/oauth/callback", query_string=[("state", state), ("state", state), ("code", "private-code")])
            self.client.get("/oauth/callback", query_string=dict(code="private-code"))
            call.assert_not_called()

    def test_refresh_rotates_tokens_then_checks_devices_without_commands(self):
        _, state = self.start()
        with patch("desk_orchestrator.oauth.request", return_value=dict(FRESH)):
            self.finish(state)
        with patch("desk_orchestrator.smartthings.request", side_effect=[dict(FRESH, access_token="rotated-access", refresh_token="rotated-refresh"), {"items": []}]) as call:
            self.client.post("/smartthings/oauth/check", data=self.form(refresh="yes"))
            self.assertEqual(call.call_count, 2)
            self.assertEqual(call.call_args_list[0].args[:2], ("POST", "/oauth/token"))
            self.assertEqual(call.call_args_list[0].kwargs["payload"]["grant_type"], "refresh_token")
            self.assertEqual(call.call_args_list[1].args[:2], ("GET", "/v1/devices"))
        saved = json.loads(Path(self.store.read()["smartthings"]["token_file"]).read_text())
        self.assertEqual(saved["refresh_token"], "rotated-refresh")

    def test_status_is_local_and_does_not_expose_tokens(self):
        self.assertFalse(status({})["usable"])
        self.assertFalse(status({"token_file": str(self.directory / "missing")})["usable"])
        file = self.directory / "tokens.json"
        atomic_json(file, dict(FRESH, expires_at=time.time() + 3600))
        with patch("desk_orchestrator.smartthings.request") as call:
            info = status({"token_file": str(file)})
            self.assertTrue(info["usable"])
            self.assertNotIn("private-access", json.dumps(info))
            self.assertNotIn("private-refresh", json.dumps(info))
            call.assert_not_called()
