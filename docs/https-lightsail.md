# HTTPS with Let's Encrypt and manual Lightsail DNS

The app shows the TXT record you need to enter in AWS Lightsail. It never signs
in to AWS, reads an AWS profile, or changes DNS records itself. AWS CLI and AWS
credentials are not required on the Pi. Existing trusted certificates continue
to work after switching from automated DNS to this manual flow.

The Pi installer/updater includes the HTTPS dependencies. For a development or
manual installation, install them in the app's virtual environment:

```sh
.venv/bin/pip install '.[web,tls]'
```

## Request a certificate

1. In **Settings → HTTPS**, save the hostname, public Lightsail zone, and account
   email. Read and accept the linked Let's Encrypt subscriber agreement.
2. Choose **Test issuance (staging)**. Progress updates in place without replacing
   your other Settings edits. When a challenge is needed, the page displays the
   TXT type, name within your zone, full DNS name, and exact value.
3. On a device where you sign in to AWS, open Lightsail → **Domains & DNS**, select
   your zone, and add the TXT record. Copy the displayed zone-relative name and
   value. Preserve other TXT values at the same name.
4. Choose **Check TXT and continue** in this app. It checks the exact value on
   both Cloudflare and Google public DNS resolvers. If propagation is incomplete,
   the request stays open; wait and try again. Complete each challenge within
   30 minutes. **Cancel certificate request** stops a pending operation.
5. Keep the TXT value until the page tells you to remove it. Delete only that
   value in Lightsail and choose **I removed this TXT value**. The app cannot
   remove it for you. If you never added it, acknowledge cleanup anyway.
6. Choose **Issue trusted certificate** and repeat the manual steps if another
   TXT challenge appears. Staging certificates are never served. Let's Encrypt
   may reuse an existing authorization, so a request does not always need TXT.
7. Open HTTPS Settings and log in using your app password. HTTP and HTTPS use
   separate session cookies. Continue with [SmartThings OAuth](smartthings-oauth.md).

For this development setup, the hostname is `desk.example.com`, the zone
is `example.com`, and local DNS points the hostname to `192.168.1.20`.
The challenge name within the Lightsail zone is `_acme-challenge.smartthings`;
the full name is `_acme-challenge.desk.example.com`. Always use the **value
shown by the current request**, not a value from an earlier request.

Certificate requests need outbound HTTPS to Let's Encrypt and public DNS queries
over port 53. The hostname's public A record and inbound internet ports 80/443
are not required for DNS-01. The Lightsail zone must be publicly delegated to its
AWS nameservers. The browser and Pi must resolve the app hostname locally.
Trusted certificates publish the hostname in public certificate transparency logs.

## Renewal and recovery

Renewal is manual. Settings shows certificate expiry and a reminder when fewer
than 30 days remain. Choose **Check renewal** before expiry and complete any new
TXT challenge. Certbot keeps a certificate that is not yet due for renewal.
The app does not start unattended certificate requests or send expiry emails.
The running HTTPS listener loads a replacement certificate within 30 seconds.

If HTTPS is unavailable because a certificate expired, use the localhost HTTP
app, through an SSH tunnel if necessary, to renew. The HTTPS service waits for a
valid production certificate before opening its listener.

An interrupted or failed request retains its TXT details for manual cleanup.
Remove the exact value and acknowledge cleanup before retrying. Existing legacy
challenge receipts from the AWS automation are also shown for manual cleanup;
no AWS call is made. Certificate operations are serialized and stop after a
40-minute overall timeout. Private logs are in `tls/last-operation.log` and
`tls/production/logs/` inside the data directory.

Existing AWS profiles are not deleted by an app update: they may be shared with
other tools. Profiles or permissions provisioned solely for the old certificate
feature can be removed separately. The new code does not use them. Saving HTTPS
setup drops the obsolete profile field from its configuration.

## HTTPS service

The service runs as an unprivileged user with only the capability needed to bind
port 443. It serves the Flask app using Cheroot, with TLS 1.2 or later and secure
session cookies. HTTP and HTTPS must use the same data directory and credentials
environment file. No reverse proxy is required.

On the Pi, preview the unit with:

```sh
python3 /opt/desk-orchestrator/deploy/setup_https.py --user desk
```

Add `--install` and run with sudo to install and start it. The installer checks
Python dependencies and file access as the service account. It backs up changed
unit contents, enables/restarts the service once, and checks service status.
The listener can be installed before issuance; it waits for a valid certificate.

For this development machine:

```sh
sudo python3 deploy/setup_https.py --user you \
  --app-dir /home/you/Documents/Code/desk-orchestrator \
  --data-dir /home/you/Documents/Code/desk-orchestrator/.runtime/web \
  --config /home/you/Documents/Code/desk-orchestrator/config/desk.example.toml \
  --env-file /home/you/Documents/Code/desk-orchestrator/.runtime/web/credentials.env \
  --host 192.168.1.20 --install
```

The unit needs write access only to its data directory; the previous `.aws`
write exception is no longer generated. Ordinary updates preserve installed
units. Rerun this installer with your existing paths and bind address to replace
an older HTTPS unit. Restart both HTTP and HTTPS after updating the app code so
an old listener cannot continue its previous automatic renewal loop.

HTTPS settings, ACME accounts, and private certificates are excluded from the
web configuration export; the private deployment state snapshot includes them.
Staging and production use separate directories. Changing an already certified
hostname requires a new TLS data directory and service restart.

References: [Certbot manual DNS and renewal](https://eff-certbot.readthedocs.io/en/stable/using.html#manual),
[Let's Encrypt DNS-01](https://letsencrypt.org/docs/challenge-types/).
