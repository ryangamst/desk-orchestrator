"""Browser account linking for the SmartThings authorization-code flow."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .core import DeskError, atomic_json, exclusive, number, require
from .smartthings import API, HTTPFailure, request

ENDPOINTS = {"/oauth/token": "/oauth/authorize", "/v1/oauth/token": "/v1/oauth/authorize"}
SCOPES = "r:devices:* x:devices:*"
TTL = 600


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def credentials(config):
    client_id = os.environ.get(config.get("client_id_env", "SMARTTHINGS_CLIENT_ID"), "")
    secret = os.environ.get(config.get("client_secret_env", "SMARTTHINGS_CLIENT_SECRET"), "")
    require(client_id and secret, "OAuth client credentials are missing from the web service environment. Set SMARTTHINGS_CLIENT_ID and SMARTTHINGS_CLIENT_SECRET, then restart the service.")
    return client_id, secret


def validate_redirect(uri):
    require(isinstance(uri, str) and 0 < len(uri) <= 2048 and not any(c.isspace() for c in uri),
            "Enter the exact registered redirect URI.")
    try:
        parsed = urlsplit(uri)
        parsed.port
    except ValueError:
        raise DeskError("Invalid redirect URI.") from None
    require(parsed.hostname and not parsed.hostname.startswith(".") and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment,
            "The redirect URI must have a host and no credentials, query, or fragment.")
    require(parsed.path == "/oauth/callback", "Register a redirect URI ending exactly in /oauth/callback.")
    require(parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")),
            "Use HTTPS for the redirect URI. HTTP is supported only for local loopback development.")
    return parsed


def begin(store, csrf, host, revision):
    cfg = store.read()
    require(cfg["revision"] == revision, "Configuration changed. Reload before connecting.")
    config = cfg.get("smartthings", {})
    uri = config.get("redirect_uri", "")
    parsed = validate_redirect(uri)
    require(parsed.netloc.lower() == host.lower(),
            "Open Settings and sign in using the registered redirect URI's host and port before connecting. The browser must return to the same host.")
    endpoint = config.get("oauth_token_endpoint", config.get("token_endpoint", "/oauth/token"))
    require(endpoint in ENDPOINTS, "Choose a supported SmartThings OAuth endpoint.")
    client_id, secret = credentials(config)
    state = secrets.token_urlsafe(32)
    pending = dict(state=digest(state), browser=digest(csrf), created=time.time(), config=config,
                   credentials=digest(client_id + "\0" + secret), endpoint=endpoint)
    # Only the most recently started flow is valid; pending data survives a restart.
    with exclusive(store.directory / "oauth-connect.lock", blocking=True):
        atomic_json(store.directory / "oauth-pending.json", pending)
    return API + ENDPOINTS[endpoint] + "?" + urlencode(dict(
        client_id=client_id, response_type="code", redirect_uri=uri, scope=SCOPES, state=state))


def finish(store, csrf, host, args):
    require(len(args.getlist("state")) == 1 and 0 < len(args.get("state", "")) <= 256,
            "Missing or invalid OAuth state. Start Connect to SmartThings again.")
    with exclusive(store.directory / "oauth-connect.lock", blocking=True):
        path = store.directory / "oauth-pending.json"
        try:
            pending = json.loads(path.read_text())
        except (OSError, ValueError):
            raise DeskError("No pending authorization. Start Connect to SmartThings again.") from None
        require(hmac.compare_digest(pending["state"], digest(args["state"]))
                and hmac.compare_digest(pending["browser"], digest(csrf)),
                "Authorization does not match this browser session. Start Connect to SmartThings again.")
        require(validate_redirect(pending["config"]["redirect_uri"]).netloc.lower() == host.lower(),
                "Authorization returned to a different host. Start again from the registered host.")
        # Consume before exchanging a single-use code, including denial/expiry.
        path.unlink()
    require(0 <= time.time() - pending["created"] <= TTL, "Authorization expired. Start Connect to SmartThings again.")
    require(not args.get("error"), "SmartThings authorization was declined or failed. Your previous connection is unchanged.")
    require(len(args.getlist("code")) == 1 and 0 < len(args.get("code", "")) <= 4096,
            "SmartThings did not return an authorization code. Start again.")
    current = store.read()
    config = pending["config"]
    require(current.get("smartthings", {}) == config, "Connection settings changed during authorization. Start again.")
    client_id, secret = credentials(config)
    require(hmac.compare_digest(pending["credentials"], digest(client_id + "\0" + secret)),
            "Client credentials changed during authorization. Start again.")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    try:
        fresh = request("POST", pending["endpoint"], basic=basic, form=True, payload={
            "grant_type": "authorization_code", "code": args["code"],
            "redirect_uri": config["redirect_uri"], "client_id": client_id})
    except HTTPFailure as exc:
        message = f"SmartThings OAuth token exchange failed at {pending['endpoint']} (HTTP {exc.status}). "
        if exc.status == 401:
            message += ("SmartThings rejected client authentication. Verify the selected OAuth app type and its matching client ID and secret. "
                        "Restart the HTTPS service after changing its credentials. ")
        message += "Start Connect to SmartThings again after correcting the problem. Your previous connection is unchanged."
        raise DeskError(message) from None
    require(isinstance(fresh, dict) and all(isinstance(fresh.get(k), str) and fresh[k] for k in ("access_token", "refresh_token"))
            and number(fresh.get("expires_in"), 1, 31536000), "SmartThings returned incomplete OAuth tokens. Start authorization again.")
    require(isinstance(fresh.get("token_type", "bearer"), str) and fresh.get("token_type", "bearer").lower() == "bearer",
            "SmartThings returned an unsupported token type.")
    if "scope" in fresh:
        require(isinstance(fresh["scope"], str) and set(SCOPES.split()) <= set(fresh["scope"].split()),
                "SmartThings did not grant device read and execute permissions. Check the app's registered scopes and reconnect.")
    fresh["expires_at"] = time.time() + fresh["expires_in"]
    # Stage separately so any failure leaves the existing PAT/OAuth connection intact.
    token_path = store.directory / "oauth" / f"tokens-{secrets.token_hex(16)}.json"
    atomic_json(token_path, fresh)
    try:
        def activate(cfg):
            require(cfg.get("smartthings", {}) == config, "Connection settings changed during authorization. Start again.")
            cfg["smartthings"].update(token_file=str(token_path), token_endpoint=pending["endpoint"])
        store.update(current["revision"], activate)
    except BaseException:
        token_path.unlink(missing_ok=True)
        raise


def status(config):
    """Local metadata only: never contact Samsung or expose token values on GET."""
    result = dict(credentials_ready=False, token_file=config.get("token_file", ""),
                  message="Personal access token mode is selected." if not config.get("token_file") else "OAuth token file needs attention.",
                  usable=False, expires="", settings_url="")
    if config.get("redirect_uri"):
        try:
            parsed = validate_redirect(config["redirect_uri"])
            result["settings_url"] = f"{parsed.scheme}://{parsed.netloc}/settings"
        except DeskError:
            pass
    try:
        credentials(config)
        result["credentials_ready"] = True
    except DeskError:
        pass
    if not result["token_file"]:
        return result
    try:
        data = json.loads(Path(result["token_file"]).expanduser().read_text())
        require(isinstance(data, dict) and all(isinstance(data.get(k), str) and data[k] for k in ("access_token", "refresh_token")), "Invalid token file.")
        expiry = float(data.get("expires_at", 0))
        result.update(usable=True, message="OAuth tokens saved; connection not checked on this page.",
                      expires=datetime.fromtimestamp(expiry, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
        if expiry <= time.time() + 120:
            result["message"] = "OAuth access token needs refresh; the next request will attempt it."
    except (DeskError, OSError, ValueError, TypeError, OverflowError):
        pass
    return result
