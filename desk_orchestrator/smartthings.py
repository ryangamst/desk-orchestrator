"""SmartThings REST client with bounded requests and optional OAuth refresh."""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .core import DeskError, atomic_json, exclusive, number, require

API = "https://api.smartthings.com"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPFailure(DeskError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"SmartThings HTTP {status}; check credentials, permissions, device, and capability.")


def request(method, path, token=None, payload=None, *, basic=None, form=False):
    require(path.startswith("/") and not path.startswith("//"), "Invalid SmartThings API path.")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if basic:
        headers["Authorization"] = "Basic " + basic
    body = None
    if payload is not None:
        body = (urllib.parse.urlencode(payload) if form else json.dumps(payload)).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
    req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Never print the response body, request headers, or OAuth secrets.
        raise HTTPFailure(exc.code) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise DeskError("SmartThings network request failed or timed out.") from exc
    except (ValueError, UnicodeError) as exc:
        raise DeskError("SmartThings returned an invalid JSON response.") from exc


class Tokens:
    def __init__(self, config):
        self.config = config

    def available(self):
        if self.config.get("token_file"):
            require(Path(self.config["token_file"]).expanduser().is_file(), "OAuth token_file does not exist.")
        else:
            name = self.config.get("token_env", "SMARTTHINGS_TOKEN")
            require(bool(os.environ.get(name)), f"Set {name} or configure an OAuth token_file.")

    def get(self, *, rejected=None, force_refresh=False):
        self.available()
        if not self.config.get("token_file"):
            require(not force_refresh, "A personal access token cannot be refreshed. Connect with OAuth first.")
            require(rejected is None, "SmartThings token rejected; renew the PAT (new PATs last 24 hours).")
            return os.environ[self.config.get("token_env", "SMARTTHINGS_TOKEN")]
        path = Path(self.config["token_file"]).expanduser()
        with exclusive(path.with_suffix(".lock"), blocking=True):
            try:
                data = json.loads(path.read_text())
                require(isinstance(data, dict), "OAuth token file must contain an object.")
                valid = float(data.get("expires_at", 0)) > time.time() + 120
                require(isinstance(data.get("access_token"), str), "OAuth file has no access_token.")
            except (ValueError, TypeError) as exc:
                raise DeskError("Invalid OAuth token file.") from exc
            if valid and data["access_token"] != rejected and not force_refresh:
                return data["access_token"]
            client_id = os.environ.get(self.config.get("client_id_env", "SMARTTHINGS_CLIENT_ID"))
            secret = os.environ.get(self.config.get("client_secret_env", "SMARTTHINGS_CLIENT_SECRET"))
            require(client_id and secret and data.get("refresh_token"), "OAuth refresh needs client credentials and refresh_token.")
            basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
            fresh = request("POST", self.config.get("token_endpoint", "/v1/oauth/token"),
                            payload={"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                     "client_id": client_id}, basic=basic, form=True)
            require(isinstance(fresh, dict) and all(k in fresh for k in ("access_token", "refresh_token", "expires_in")),
                    "Incomplete OAuth refresh response; reauthorize if the refresh token was consumed.")
            require(all(isinstance(fresh[k], str) and fresh[k] for k in ("access_token", "refresh_token"))
                    and number(fresh["expires_in"], 1, 31536000), "Invalid OAuth refresh token response.")
            fresh["expires_at"] = time.time() + float(fresh["expires_in"])
            atomic_json(path, fresh)
            return fresh["access_token"]


def segment(value):
    return urllib.parse.quote(str(value), safe="")


class SmartThings:
    def __init__(self, config):
        self.tokens = Tokens(config)

    def call(self, method, path, payload=None):
        token = self.tokens.get()
        try:
            return request(method, path, token, payload)
        except HTTPFailure as exc:
            if exc.status != 401:
                raise
            token = self.tokens.get(rejected=token)
            return request(method, path, token, payload)

    def devices(self):
        # Follow only relative paths on the same trusted API origin.
        path, devices, seen = "/v1/devices", [], set()
        while path:
            require(path not in seen and len(seen) < 100, "Invalid SmartThings pagination.")
            seen.add(path)
            result = self.call("GET", path)
            devices.extend(result.get("items", []))
            href = result.get("_links", {}).get("next", {}).get("href", "")
            parsed = urllib.parse.urlsplit(href)
            require(not parsed.netloc or (parsed.scheme == "https" and parsed.netloc == "api.smartthings.com"),
                    "Unexpected SmartThings pagination host.")
            path = parsed.path + ("?" + parsed.query if parsed.query else "") if href else ""
        return devices

    def status(self, device_id):
        return self.call("GET", f"/v1/devices/{segment(device_id)}/status")

    def wake_if_off(self, config):
        def power_state():
            status = self.status(config["device_id"])
            return (status.get("components", {}).get(config["component"], {})
                    .get("switch", {}).get("switch", {}).get("value"))

        # Devices without a reported switch state retain normal input switching.
        if power_state() != "off":
            return
        body = {"commands": [{"component": config["component"], "capability": "switch",
                              "command": "on", "arguments": []}]}
        reply = self.call("POST", f"/v1/devices/{segment(config['device_id'])}/commands", body)
        results = reply.get("results", [])
        require(len(results) == 1 and results[0].get("status") in ("ACCEPTED", "COMPLETED"),
                "SmartThings did not accept the power-on command.")
        deadline = time.monotonic() + config.get("verify_timeout", 15)
        while power_state() != "on":
            require(time.monotonic() < deadline, "SmartThings power-on was not confirmed before timeout.")
            time.sleep(0.5)

    def set_input(self, config, value):
        self.wake_if_off(config)
        body = {"commands": [{"component": config["component"], "capability": config["capability"],
                              "command": config["command"], "arguments": [value]}]}
        reply = self.call("POST", f"/v1/devices/{segment(config['device_id'])}/commands", body)
        results = reply.get("results", [])
        require(len(results) == 1 and results[0].get("status") in ("ACCEPTED", "COMPLETED"),
                "SmartThings did not accept the input command.")
        deadline = time.monotonic() + config.get("verify_timeout", 15)
        while True:
            status = self.status(config["device_id"])
            actual = (status.get("components", {}).get(config["component"], {})
                      .get(config["capability"], {}).get(config["attribute"], {}).get("value"))
            if actual == value:
                return
            require(time.monotonic() < deadline, "SmartThings input was not confirmed before timeout.")
            time.sleep(0.5)
