# SmartThings and HTTPS change inventory

Reviewed 2026-09-25. **Subsequent revision:** AWS API automation has been replaced
with manual TXT entry. Settings is now one column. The original implementation
inventory below is retained as history; the current workflow and migration steps
are in [manual HTTPS setup](https-lightsail.md). No AWS profile or CLI is required;
renewal now needs the user to complete DNS verification. The HTTPS unit no longer
adds write access to `.aws`. Existing certificates remain usable.

Original audit: This records the work from local SmartThings testing through
OAuth, Lightsail HTTPS, and the installation/update audit. It distinguishes
application changes from this computer's private setup. No Pi deployment was
performed during this audit.

## Application changes

### SmartThings discovery, mappings, and testing

- Used discovery and device capabilities to identify the G8's available inputs
  and map local input names to the exact values SmartThings accepts.
- Added input-test controls to the hardware editor. Tests use saved mappings,
  reject stale configuration, and require authentication and CSRF validation.
  Unsaved edits disable testing until saved.
- Input tests share the equipment-operation lock, send the selected command,
  poll for the resulting input, and report/log the outcome. API acceptance alone
  does not count as confirmation that the monitor switched inputs.
- Corrected the local Work task's `hdmi1` reference to `hdmi2`, allowing removal
  of the obsolete input. This was a saved-configuration correction: validation
  still rejects removal of inputs that tasks reference.

### OAuth account linking

- Added OAuth setup, **Connect to SmartThings**, the callback, connection checks,
  and a forced refresh test to Settings.
- Added explicit selection between the CLI OAuth-In App `/oauth/token` endpoint
  and the preview `/v1/oauth/token` endpoint, with corresponding authorization
  endpoints. Requests include device read and execute permissions.
- Added validation of the exact callback URI, browser session, random state,
  ten-minute expiry, and unchanged settings/client credentials. Pending state
  is consumed before exchanging the single-use authorization code.
- Save received tokens privately and atomically under the runtime `oauth/`
  directory. Activate the new connection only after successful token/config
  persistence; failed authorization preserves the previous connection.
- Support token refresh before expiry and one retry after an API 401. Refresh
  saves rotating tokens. Connection/refresh checks read devices without sending
  equipment commands.
- Fixed browser navigation after Connect: a local handoff page with external
  JavaScript navigates the same tab to Samsung, with a manual continuation link.
  This works with the app's Content Security Policy.
- Use `SameSite=Lax` for the top-level callback. HTTPS sessions use a separate
  secure cookie. Added token-exchange-specific error context and clarified that
  loaded client credentials have not yet been verified by SmartThings.

Implementation: `desk_orchestrator/oauth.py`, `smartthings.py`, `web.py`,
configuration handling, hardware/settings templates, and OAuth navigation assets.
See [OAuth setup and testing](smartthings-oauth.md).

### Let's Encrypt through AWS Lightsail

- Added HTTPS Settings for hostname, Lightsail DNS zone, AWS profile, account
  email, and acceptance of the Let's Encrypt subscriber agreement.
- Added background operations for checking Lightsail access, staging issuance,
  trusted issuance, and renewal, with status and certificate expiry reporting.
- Implemented Certbot DNS-01 hooks using AWS CLI v2 and Lightsail DNS APIs in
  `us-east-1`. This does not use Route 53. Temporary TXT records are tracked for
  exact cleanup while preserving unrelated values.
- Check public DNS propagation separately from the local DNS override. Keep
  staging and production ACME state separate; staging certificates are not served.
  Serialize certificate operations and retain cleanup receipts after failures.
- Added a direct Cheroot HTTPS listener with TLS 1.2 or newer, the same managed
  application state, and private certificate storage. It waits for the first
  production certificate, checks renewal every 12 hours, and reloads a changed
  certificate within 30 seconds while running.
- Added an optional systemd HTTPS installer. The service runs as an unprivileged
  account with the capability to bind port 443 and restricted filesystem writes.

Implementation: `desk_orchestrator/tls.py`, `tls_dns.py`, CLI/web integration,
TLS settings template, `pyproject.toml` TLS extra, and `deploy/setup_https.py`.
See [Lightsail HTTPS setup](https-lightsail.md).

## Development-machine setup

These are local state or account/network changes, excluded from source bundles:

- Started the local web environment and configured private PAT credentials for
  initial device testing. The user's shell is fish; Bash-only token-entry
  commands require running Bash or using fish equivalents.
- Installed the SmartThings CLI. The user created the SmartThings OAuth app and
  corrected its client ID/secret in the service's private environment file.
- Configured G8 device/input mappings and corrected the Work task reference.
- Registered `https://desk.example.com/oauth/callback`; configured local
  DNS for `desk.example.com` to this computer at `192.168.1.20`.
- Used the default AWS profile for the Lightsail zone `example.com`; configured
  the local HTTPS service and certificate state.
- Local data lives under `.runtime/web`; the system HTTPS service and local
  HTTP service share this state and the credentials environment file. The local
  `.runtime/enable-https.sh` helper is not a portable deployment script.

The user reported successful PAT testing. Completion of the corrected OAuth
authorization flow is still a separate live check; the deployment audit does
not establish that Samsung account linking has completed.

## Installation and update changes made by this audit

| Entry point | Coverage and changes |
| --- | --- |
| `deploy/setup-pi.sh` → `setup_pi.py` | Fresh installs and reruns now install `web,keypad,tls`. Reruns stop installed HTTPS before replacing code and resume it only if previously running. A private resume marker survives interrupted setup; the existing HTTPS unit is preserved. |
| `deploy/setup_https.py` | Before changing a unit, checks TLS Python dependencies, AWS CLI v2, and data/config/environment access as the actual service user. Backs up a changed unit, enables it, restarts once, and checks service status. Preview remains read-only. |
| `deploy/update-pi.sh` → `update_pi.py` | Existing SSH transport carries the new source/assets/scripts. Wheel preparation and installation now include TLS dependencies. Detects optional HTTPS, stops it during changes, and restores only previously running services on success or rollback. |
| Update verification | Checks authenticated Settings, hardware editors, OAuth JavaScript, event storage, and TLS dependencies. Checks HTTP readiness and trusted HTTPS readiness when a production certificate exists. No live equipment commands or certificate issuance occur in these checks. |
| Archive and recovery | Tests verify new OAuth/TLS modules, templates, JavaScript, and the HTTPS installer are bundled. Local secrets remain excluded. Existing OAuth tokens, certificates, TLS settings, and service-account AWS files survive update/rollback and are included in the private state snapshot. |
| Documentation and credentials example | Updated initial/manual install, update/rollback, OAuth, and Lightsail instructions; documented environment-file loading, service restarts, AWS service-account setup, and secret-backup boundaries. |

Routine updates preserve installed systemd units/drop-ins and `/etc` credentials.
They copy revised deployment scripts into `/opt/desk-orchestrator/deploy` but do
not apply new unit definitions automatically. Fresh HTTPS enablement remains an
explicit step. AWS CLI v2 and its account/profile setup are external prerequisites;
the scripts check the CLI but do not install it or copy desktop AWS credentials.

The root-only deployment `state.tar.gz` includes private runtime state. The web
app's JSON export excludes secret values and preserves the import target's
credential references. Neither is a complete backup of `/etc` service settings.

## Moving this setup to the Pi

1. Use the [initial installer](raspberry-pi.md) for a fresh Pi or the
   [updater](updating-pi.md) for an existing installation. Source bundles include
   code and example configuration, not this desktop's saved device mappings.
2. Configure or import the intended hardware/tasks on the Pi. Configure its
   private OAuth environment for the `desk` service account. No AWS profile is needed.
   Restart running app services after changing the environment file.
3. Enable HTTPS with
   `sudo python3 /opt/desk-orchestrator/deploy/setup_https.py --user desk --install`.
4. Set the Pi's local DNS, Lightsail HTTPS settings, and matching registered
   callback URI. Add the displayed TXT values manually for staging and trusted
   issuance, acknowledge cleanup, and open
   the Pi's HTTPS Settings page.
5. Authorize OAuth on that installation, check the connection, and force a token
   refresh. Avoid running two installations with copied rotating token pairs.
   Finally, use saved-input tests to verify actual G8 switching.

Automated deployment tests simulate Pi commands and files. Actual Pi OS package
installation, boot, AWS access, certificate issuance, Samsung consent, and physical
equipment remain live commissioning checks.

## Audit validation

- Passed 115 deployment, OAuth, web, TLS, and SmartThings client tests.
- Passed the local HTTPS socket integration test, including secure cookies,
  host validation, and certificate reload: 116 tests total.
- Built the Python wheel and checked OAuth/TLS modules, web assets, and optional
  dependency metadata. Archive tests verified inclusion and exclusion rules.
- Passed Bash syntax checks for install/update wrappers and Python compilation
  for all three deployment scripts.

No live service restart, AWS DNS change, certificate request, or hardware command
was performed during this audit.

## Manual DNS revision validation

The manual DNS revision passes 122 relevant tests, including an actual worker
and hook subprocess handshake through the web controls, cancellation, propagation
retry, stale confirmations, legacy receipts/jobs, and the local HTTPS listener.
The wheel includes the new progress template and JavaScript. Settings was checked
in the running local browser and now uses one full-width column. No certificate
request or AWS DNS change was performed while testing this revision.

## Task connection-check feedback

Task previews previously displayed only failed checks, so successful SmartThings
reads were invisible when IR/KVM setup warnings remained. They now display
completion, passed checks, the monitor's current input, and a busy state during
the request. A connection check reads status; saved-input hardware tests perform
actual switching. Missing input feedback remains an error.

Validated with 61 targeted web, runner, hardware, SmartThings, and control tests
and a live read-only Personal Computing check on the development app. The G8
reported `Display Port`. Pi verification/deployment requires SSH authentication;
no Pi files or services were changed during this investigation.
