"""Certbot hooks for human-managed DNS. Never access a DNS provider account."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import sys
import time

from .core import DeskError, atomic_json, require

CHALLENGE_SECONDS = 30 * 60


def read(path):
    return json.loads(path.read_text()) if path.exists() else {}


def public_txt_matches(name, token):
    import dns.exception
    import dns.resolver
    # Bypass the LAN override used to reach the HTTPS application.
    for server in ("1.1.1.1", "8.8.8.8"):
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [server]
        try:
            answers = resolver.resolve(name, "TXT", lifetime=5)
            if not any(b"".join(answer.strings).decode() == token for answer in answers):
                return False
        except dns.exception.DNSException:
            return False
    return True


def cleanup(receipt):
    """Keep the exact record visible until the user acknowledges removing it."""
    saved = read(receipt)
    if saved:
        saved.update(state="cleanup", message="Remove only this TXT value from AWS Lightsail, then acknowledge below.")
        atomic_json(receipt, saved)


def cancelled(base, ident):
    return read(base / "cancel.json").get("job_id") == ident


def authenticate(config, receipt, domain, token, ident):
    require(domain == config["domain"], "Unexpected ACME challenge domain.")
    require(re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token), "Invalid ACME challenge value.")
    require(not receipt.exists(), "A previous TXT record still needs cleanup in Settings.")
    saved = dict(id=secrets.token_hex(16), job_id=ident, state="waiting", created=time.time(),
                 expires=time.time() + CHALLENGE_SECONDS, zone=config["zone"],
                 record=dict(name="_acme-challenge." + domain, type="TXT", target=token),
                 message="Add this TXT record in AWS Lightsail, then choose Check TXT and continue.")
    atomic_json(receipt, saved)
    deadline = time.monotonic() + CHALLENGE_SECONDS
    last_request = None
    while time.monotonic() < deadline:
        require(not cancelled(receipt.parent, ident), "Certificate request cancelled.")
        request = read(receipt.parent / "continue.json")
        if (request.get("challenge_id") == saved["id"] and request.get("id") != last_request):
            last_request = request["id"]
            saved.update(state="checking", message="Checking the exact TXT value in public DNS…")
            atomic_json(receipt, saved)
            if public_txt_matches(saved["record"]["name"], token):
                require(not cancelled(receipt.parent, ident), "Certificate request cancelled.")
                saved.update(state="validated", message="TXT value found. Keep it in DNS until certificate validation finishes.")
                atomic_json(receipt, saved)
                return
            saved.update(state="waiting", message="The TXT value is not visible on both public resolvers yet. Check the name/value, allow propagation, then try again.")
            atomic_json(receipt, saved)
        time.sleep(1)
    raise DeskError("The manual DNS challenge expired after 30 minutes. Remove the shown TXT value and start again.")


def main():
    try:
        job = Path(os.environ["DESK_TLS_JOB"])
        data = read(job)
        require(data.get("id") == os.environ.get("DESK_TLS_JOB_ID"), "Certificate job changed; start again.")
        receipt = job.parent / "challenge.json"
        if sys.argv[1] == "auth":
            authenticate(data["config"], receipt, os.environ["CERTBOT_DOMAIN"], os.environ["CERTBOT_VALIDATION"], data["id"])
        elif sys.argv[1] == "cleanup":
            cleanup(receipt)
        else:
            raise DeskError("Unknown DNS hook.")
    except (DeskError, OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
