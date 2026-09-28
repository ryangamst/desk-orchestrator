"""Private ACME state, manual DNS issuance, and an HTTPS listener."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .core import DeskError, atomic_json, exclusive, require
from . import tls_dns

SERVERS = {
    "staging": "https://acme-staging-v02.api.letsencrypt.org/directory",
    "production": "https://acme-v02.api.letsencrypt.org/directory",
}
RELOAD_SECONDS = 30


def root(directory):
    path = Path(directory).resolve() / "tls"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else (default or {})


def installed():
    return all(importlib.util.find_spec(name) is not None for name in ("certbot", "cheroot", "dns", "cryptography"))


def defaults(uri=""):
    return dict(domain=urlsplit(uri).hostname or "", zone="", email="", agreed=False, revision=0)


def validate(values):
    config = {key: str(values.get(key, "")).strip() for key in ("domain", "zone", "email")}
    for key in ("domain", "zone"):
        name = config[key].lower().rstrip(".")
        require(len(name) <= 253 and "." in name and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split(".")),
                "Enter a full DNS hostname and Lightsail zone without a scheme, port, or wildcard.")
        config[key] = name
    require(config["domain"] == config["zone"] or config["domain"].endswith("." + config["zone"]), "The hostname must be inside the Lightsail zone.")
    require(not config["email"] or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", config["email"]), "Enter a valid account email address.")
    config["agreed"] = values.get("agreed") in (True, "yes")
    return config


def available_locked(base):
    job = read(base / "job.json")
    # The worker holds job.lock throughout execution. Cover the short spawn gap.
    require(job.get("state") != "queued" or time.time() - job.get("time", 0) > 30,
            "A certificate operation is already queued.")


def save(directory, values):
    base = root(directory)
    with exclusive(base / "job.lock"):
        available_locked(base)
        previous = read(base / "settings.json", defaults())
        require(str(previous["revision"]) == str(values.get("revision")), "HTTPS settings changed. Reload the page.")
        config = validate(values)
        # A lineage and running listener have one fixed identity.
        if (base / "production/config/live/desk/fullchain.pem").exists():
            require(config["domain"] == previous["domain"], "Changing a certified hostname requires a new TLS data directory and listener restart.")
        config["revision"] = previous["revision"] + 1
        atomic_json(base / "settings.json", config)
    return config


def certificate_paths(directory):
    base = root(directory) / "production/config/live/desk"
    return base / "fullchain.pem", base / "privkey.pem"


def certificate(directory, domain):
    from cryptography import x509
    cert_path, key_path = certificate_paths(directory)
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    require(domain in names, "Certificate does not match the configured hostname.")
    now = datetime.now(timezone.utc)
    require(cert.not_valid_before_utc <= now < cert.not_valid_after_utc, "Certificate is not currently valid.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert_path), str(key_path))
    return context, cert.not_valid_after_utc.isoformat()


def status(directory, uri=""):
    base = root(directory)
    config = read(base / "settings.json", defaults(uri))
    job = read(base / "job.json")
    busy = False
    try:
        with exclusive(base / "job.lock"):
            busy = job.get("state") == "queued" and time.time() - job.get("time", 0) <= 30
    except DeskError:
        busy = True
    if not busy and job.get("state") in ("queued", "running"):
        job = dict(job, state="interrupted", message="The previous certificate operation was interrupted. Remove any displayed TXT value, acknowledge cleanup, and start again.")
    challenge = read(base / "challenge.json")
    if challenge and not busy:
        challenge = dict(challenge, state="cleanup", message="Remove only this TXT value from AWS Lightsail, then acknowledge below.")
    if challenge:
        record = challenge.get("record", {})
        zone = challenge.get("zone", challenge.get("config", {}).get("zone", config.get("zone", "")))
        name = record.get("name", "")
        challenge = dict(challenge, zone=zone, host=name[:-len(zone)-1] if zone and name.endswith("." + zone) else name,
                         value=record.get("target", "").strip('"'))
    config.pop("profile", None)  # Ignore legacy AWS profile settings.
    result = dict(config=config, job=job, challenge=challenge, busy=busy, installed=installed(), expires="", certificate_error="", renewal_needed=False)
    if certificate_paths(directory)[0].exists():
        try:
            _, result["expires"] = certificate(directory, config["domain"])
            result["renewal_needed"] = (datetime.fromisoformat(result["expires"]) - datetime.now(timezone.utc)).days < 30
        except (ValueError, OSError, DeskError, ImportError) as exc:
            result["certificate_error"] = str(exc)
    return result


def start(directory, action):
    require(action in ("staging", "issue", "renew"), "Unknown certificate operation.")
    require(installed(), 'Install HTTPS support with pip install ".[web,tls]".')
    base = root(directory)
    with exclusive(base / "job.lock"):
        available_locked(base)
        config = validate(read(base / "settings.json"))
        require(not (base / "challenge.json").exists(), "Remove the previous TXT value and acknowledge cleanup before starting again.")
        require(config["email"] and config["agreed"], "Save an account email and accept the Let's Encrypt subscriber agreement first.")
        if action == "renew":
            require(certificate_paths(directory)[0].exists(), "Issue a production certificate first.")
        ident = secrets.token_hex(16)
        job = dict(id=ident, action=action, dns_mode="manual", config=config, state="queued", time=time.time(), message="Operation queued. Refresh this page for progress.")
        atomic_json(base / "job.json", job)
        try:
            subprocess.Popen([sys.executable, "-m", "desk_orchestrator.tls", "worker", "--data-dir", str(Path(directory).resolve()), "--job", ident],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            atomic_json(base / "job.json", dict(job, state="failed", message="Could not start the certificate worker."))
            raise DeskError("Could not start the certificate worker.") from None


def certbot_command(base, config, mode):
    paths = base / mode
    command = [sys.executable, "-c", "from certbot.main import main; raise SystemExit(main())", "certonly", "--manual", "--preferred-challenges", "dns",
               "--manual-auth-hook", shlex.join([sys.executable, "-m", "desk_orchestrator.tls_dns", "auth"]),
               "--manual-cleanup-hook", shlex.join([sys.executable, "-m", "desk_orchestrator.tls_dns", "cleanup"]),
               "--non-interactive", "--agree-tos", "--email", config["email"], "--server", SERVERS[mode],
               "--cert-name", "desk", "-d", config["domain"], "--keep-until-expiring", "--no-directory-hooks"]
    for kind in ("config", "work", "logs"):
        command.extend(["--" + kind + "-dir", str(paths / kind)])
    if mode == "staging":
        command.remove("--keep-until-expiring")
        command.append("--force-renewal")
    return command


def worker(directory, ident):
    os.umask(0o077)
    base = root(directory)
    with exclusive(base / "job.lock", blocking=True):
        job = read(base / "job.json")
        if job.get("id") != ident:
            return 1
        def report(state, message):
            job.update(state=state, message=message, time=time.time())
            atomic_json(base / "job.json", job)
        report("running", "Preparing the certificate request. Any required TXT record will appear here shortly.")
        process = None
        try:
            require(job.get("dns_mode") == "manual" and job.get("action") in ("staging", "issue", "renew"),
                    "Automatic DNS requests are no longer supported. Restart the HTTPS service, then start a manual request in Settings.")
            require(not tls_dns.cancelled(base, ident), "Certificate request cancelled.")
            config = validate(job["config"])
            require(config["email"] and config["agreed"], "Save an account email and accept the Let's Encrypt subscriber agreement first.")
            mode = "staging" if job["action"] == "staging" else "production"
            env = dict(os.environ, DESK_TLS_JOB=str(base / "job.json"), DESK_TLS_JOB_ID=ident)
            with (base / "last-operation.log").open("w") as log:
                process = subprocess.Popen(certbot_command(base, config, mode), env=env, stdout=log, stderr=log, start_new_session=True)
                deadline = time.monotonic() + 40 * 60
                while True:
                    require(not tls_dns.cancelled(base, ident), "Certificate request cancelled.")
                    require(time.monotonic() < deadline, "Certificate request timed out. Remove any displayed TXT value and start again.")
                    try:
                        code = process.wait(timeout=1)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            require(code == 0, "Certbot failed or the DNS challenge expired. Remove any displayed TXT value. See tls/last-operation.log for details.")
            if mode == "production":
                certificate(directory, config["domain"])
            messages = {"staging": "Staging validation succeeded. Clean up its TXT value before requesting a trusted certificate.",
                        "issue": "Trusted certificate ready. The HTTPS listener loads it automatically.",
                        "renew": "Renewal check completed. The HTTPS listener loads renewed certificates automatically."}
            report("succeeded", messages[job["action"]])
            return 0
        except Exception as exc:
            message = str(exc) if isinstance(exc, DeskError) else "Certificate operation failed. Check the local service and certificate logs."
            report("cancelled" if tls_dns.cancelled(base, ident) else "failed", message)
            return 1
        finally:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            tls_dns.cleanup(base / "challenge.json")


def control(directory, action, ident):
    """Signals go in separate files: web requests never take the worker's lock."""
    base = root(directory)
    if action == "cleanup":
        with exclusive(base / "job.lock"):
            available_locked(base)
            challenge = read(base / "challenge.json")
            require(challenge and challenge.get("id", "legacy") == ident, "The TXT record changed. Refresh Settings.")
            (base / "challenge.json").unlink()
        return
    state = status(directory)
    require(state["busy"], "No certificate request is running. Refresh Settings.")
    if action == "cancel":
        require(state["job"].get("id") == ident, "The certificate request changed. Refresh Settings.")
        atomic_json(base / "cancel.json", dict(job_id=ident))
    elif action == "continue":
        challenge = state["challenge"]
        require(challenge and challenge.get("id") == ident and challenge.get("state") == "waiting",
                "The TXT challenge changed or is already being checked. Refresh Settings.")
        atomic_json(base / "continue.json", dict(challenge_id=ident, id=secrets.token_hex(16)))
    else:
        raise DeskError("Unknown certificate action.")


def serve(directory, seed, host, port):
    from cheroot.ssl.builtin import BuiltinSSLAdapter
    from cheroot.wsgi import Server
    from .web import create_app
    base = root(directory)
    print("HTTPS service waiting for a valid production certificate; configure it in the local HTTP app.", flush=True)
    while True:
        config = read(base / "settings.json")
        try:
            context, _ = certificate(directory, config.get("domain", ""))
            break
        except (OSError, ValueError, DeskError):
            time.sleep(5)
    domain = config["domain"]
    app = create_app(directory, seed, trusted_hosts=[domain], secure_cookie=True)
    # Keep the HTTPS session separate from the development HTTP session.
    app.config["SESSION_COOKIE_NAME"] = "__Host-desk_session"
    cert_path, key_path = certificate_paths(directory)
    adapter = BuiltinSSLAdapter(str(cert_path), str(key_path))
    adapter.context = context
    server = Server((host, port), app, numthreads=8)
    server.ssl_adapter = adapter
    stop = threading.Event()

    def maintain():
        fingerprint = None
        while not stop.wait(RELOAD_SECONDS):
            try:
                current = (cert_path.read_bytes(), key_path.stat().st_mtime_ns)
                if current != fingerprint:
                    adapter.context, _ = certificate(directory, domain)
                    fingerprint = current
            except Exception as exc:
                print("HTTPS maintenance: " + (str(exc) if isinstance(exc, DeskError) else type(exc).__name__), flush=True)
    threading.Thread(target=maintain, daemon=True).start()
    print(f"Desk HTTPS: https://{domain}:{port} (listening on {host})", flush=True)
    try:
        server.start()
    finally:
        stop.set()
        server.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["worker", "staging", "issue", "renew"])
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--job")
    args = parser.parse_args()
    try:
        if args.action == "worker":
            return worker(args.data_dir, args.job)
        start(args.data_dir, args.action)
        print("Certificate operation queued; see Settings for status.")
        return 0
    except DeskError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
