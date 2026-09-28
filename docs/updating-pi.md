# Updating the Raspberry Pi

Use this after the initial setup has succeeded. Run the update from your
**development computer**, in the current project directory:

```bash
cd /home/you/Documents/Code/desk-orchestrator
bash deploy/update-pi.sh pi@192.168.1.50
```

This copies your current local changes, including uncommitted changes. No Git
repository or commit is required. SSH may request your Pi login password, and
sudo may request it again for installation. Existing SSH keys and passwordless
sudo work without those prompts. The updater does not change either policy.
Use a different `USER@HOST` if the Pi's address changes. SSH options such as a
custom port or identity can be set in your normal SSH config using a host alias.
The wrapper accepts hostnames and IPv4 addresses.

The Pi needs enough free disk space for a copy of the installed application and
virtual environment, a state snapshot, and temporary package builds. It needs
internet access when required Python packages are not cached. Keep the SSH
session connected until the script reports completion, and avoid manual CLI
commands or editing settings during the short service restart window.

## What happens

1. Bundles package source, templates, JavaScript/CSS, SVG assets, documentation, and an
   explicit list of deployment files. Local virtual environments, `.runtime`,
   preview authentication, and `config/desk.toml` are excluded.
2. Transfers the archive and updater over SSH to a private temporary directory.
3. Builds/downloads Python wheels for `web`, `keypad`, and `tls` while the currently
   installed services continue running. A download/build failure leaves them running.
4. Stops the web app, numpad listener, and HTTPS service if installed, then saves the previous application,
   virtual environment, and a snapshot of `/var/lib/desk-orchestrator` in a
   root-only timestamped directory under `/var/backups/desk-orchestrator`.
5. Replaces the package source, removes obsolete package files, and installs the
   new wheel and dependencies. An unchanged project version still installs the
   new code. Checks dependency consistency with `pip check`.
6. Checks authenticated health, developer console, event storage, OAuth/HTTPS
   settings, the hardware list and existing SmartThings editors, the OAuth
   navigation JavaScript, favicon, virtual numpad state/assets, and TLS dependency availability using the actual
   installed app as the `desk` service account. These
   checks do not transmit hardware commands.
7. Restarts the services that were running and checks their status. If the web
   app was running, it also checks the HTTP login page on `127.0.0.1:8080`.
   If HTTPS was running and a production certificate exists, it checks the HTTPS
   login page at the saved hostname on port 443 with normal certificate validation.
   Without a certificate, the HTTPS service may remain waiting for first issuance.

The updater retains saved configuration, passwords, credentials, learned IR
codes, diagnostics, OAuth token files, TLS configuration, ACME accounts,
certificates, and the service account's AWS profile/cache. It does not enable a disabled service, reboot the Pi,
change boot overlays, alter installed systemd units/drop-ins, or run apt upgrades.
Updated deployment templates are copied into `/opt/desk-orchestrator/deploy` but
are not installed into `/etc`. Use the [initial installer](raspberry-pi.md) when
changes require new system packages, hardware configuration, or systemd units.
Custom web bind/port settings require adapting the updater's HTTP readiness check;
its default matches the provided service at port 8080.
HTTPS readiness requires the Pi itself to resolve the saved hostname to its
listener. Custom HTTPS ports also require adapting the readiness check.

The `state.tar.gz` deployment snapshot is a private recovery backup: it includes
OAuth tokens, TLS private keys and any `.aws` files under the service account's
home/state directory. Keep it protected. It differs from the web app's JSON
configuration export, which excludes secret values. `/etc/desk-orchestrator` and
systemd units are left in place, not restored from this snapshot; back those up
separately before changing machine-level settings.

## Enabling media keys and extended macro shortcuts on an existing Pi

The updater copies the new gadget script but does not restart the USB gadget or
install its new HID permission rule. Follow the upgrade commands in
[USB keyboard gadget setup](raspberry-pi.md#usb-keyboard-gadget) once to expose
`/dev/hidg1` for media controls and update the keyboard descriptor for F13–F24
and advanced keyboard usages. Even if media controls are already installed,
restart `desk-gadget.service` after this update to refresh that descriptor. Then open a task's **Keyboard commands** section
to assign unused numpad keys. Existing tasks keep their saved mappings.

## Enabling HTTPS on an existing Pi for the first time

An ordinary update installs the TLS Python dependencies but does not open a new
LAN listener. Run on the Pi:

```sh
sudo python3 /opt/desk-orchestrator/deploy/setup_https.py --user desk --install
```

Continue with [HTTPS setup](https-lightsail.md). Once the HTTPS unit exists,
subsequent updates include it in stop/start and rollback handling. Services that
were stopped before the update stay stopped. DNS verification is manual: Settings
shows TXT records for you to enter in Lightsail. No AWS CLI or profile is required.
Existing AWS files are preserved, but no longer used by the application. Rerun
the HTTPS installer with your existing options to remove the old unit's `.aws`
write exception.

Python dependency versions follow `pyproject.toml`; builds are not version-locked.
Only the declared application dependencies are updated. Backup directories are
retained so you can choose when to remove old ones after verifying an update.

## Open the updated developer console

Keep this SSH tunnel open on your development computer:

```bash
ssh -L 8081:127.0.0.1:8080 pi@192.168.1.50
```

Visit [the developer console](http://127.0.0.1:8081/console), sign in if needed,
and reload the page after updating. The first port is the port on your computer;
the final port is the web app's port on the Pi.

A listener that was stopped before updating stays stopped. In that case, the
console works but will not show new physical numpad events. After hardware
commissioning, start your existing listener on the Pi if desired:

```bash
sudo systemctl start desk-orchestrator
```

## If an update fails

A handled error after changes begin triggers an automatic restore of the previous
application and virtual environment, followed by restarting previously running
services. The updater still exits with an error so a failed deployment cannot
look successful. Saved settings and diagnostic history are not reset during
rollback; the separate `state.tar.gz` is retained for manual recovery if needed.

If power loss or an SSH interruption prevents automatic recovery, reconnect and
inspect the backups on the Pi:

```bash
sudo ls -1 /var/backups/desk-orchestrator
sudo journalctl -u desk-web -u desk-https -u desk-orchestrator -n 80 --no-pager
```

Restore a complete backup using the directory name printed by the updater:

```bash
sudo python3 /opt/desk-orchestrator/deploy/update_pi.py --rollback BACKUP_NAME
```

For example, a name looks like `20260923T120000Z-a1b2c3d4`. If the application
folder itself is incomplete, copy `deploy/update_pi.py` from the development
computer and run that copy with the same `--rollback` argument. Rollback preserves
current saved settings; updates that introduce incompatible state/database
migrations need a separate migration/recovery procedure. The current developer
console adds its own event database without replacing controller settings.

## Update from files already on the Pi

If you copied the current project to the Pi yourself, run from that source copy:

```bash
python3 deploy/update_pi.py --bundle /tmp/desk-update.tar.gz
sudo python3 deploy/update_pi.py --archive /tmp/desk-update.tar.gz
rm /tmp/desk-update.tar.gz
```

Do not overwrite the live `/opt/desk-orchestrator` files yourself before running
an update: the updater needs that directory intact to back up the old version.

### GMMK slider update

Updates also install `deploy/99-desk-gmmk.rules` and reload hidraw permissions.
The rule grants the existing `desk` service group read-only access to USB
`320f:5088`; no firmware is written. Its previous rule contents are included in
the update backup and restored if the update fails or is rolled back.

After installing, open Numpad and choose **GMMK Numpad slider (raw HID)** for
the slider. Save control inputs, then run `sudo systemctl restart desk-orchestrator`
on the Pi. Keep the dial’s two-key mapping. Existing task and IR mappings remain
unchanged. Use the bounded `input-monitor --gmmk-slider` check in the web-app guide
before checking a small movement against the active task’s volume target.

## CH9328 upgrade

Updates include the `serial` extra (pyserial), service serial permissions, and
removal of the numpad service's hard gadget dependency. To select the tested
bridge and disable the old gadget after updating, follow [CH9328 migration](ch9328.md).
