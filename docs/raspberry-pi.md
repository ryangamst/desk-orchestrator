# Raspberry Pi 4 installation

These are deployment instructions, not changes made to your current CachyOS PC.
Use Raspberry Pi OS (Bookworm or newer) or an Ubuntu Server Pi image providing
Python 3.11 or newer, on a **Raspberry Pi 4 Model B**. Commands below run on the Pi
unless stated otherwise. Keep SSH access during commissioning.

## Automated installation (recommended)

Flash the OS, configure networking and SSH, boot the Pi, and copy the project to
it. The installer handles the application and system setup after that. It needs
internet access for apt and Python packages. No Git remote is required.

From this development PC, copy only deployment sources (replace the SSH target):

```bash
cd /home/you/Documents/Code/desk-orchestrator
python3 deploy/update_pi.py --bundle /tmp/desk-orchestrator-pi.tar.gz
scp /tmp/desk-orchestrator-pi.tar.gz YOUR_PI_USER@YOUR_PI_HOST:~/
```

On the Pi:

```bash
mkdir -p ~/desk-orchestrator
tar -xzf ~/desk-orchestrator-pi.tar.gz -C ~/desk-orchestrator
cd ~/desk-orchestrator
sudo bash deploy/setup-pi.sh --reboot
```

This runs without prompts and reboots only when firmware changes require it.
Omit `--reboot` to reboot yourself. Reconnect after reboot and rerun the same
command to verify LIRC and HID permissions; it does not reboot again when nothing
changed. No remote-control commands are transmitted by setup.

The installer:

- Checks the Pi model, OS, Python version, firmware overlays, and known USB conflicts.
- Installs Python venv/build dependencies, LIRC, `ir-keytable`, and CA certificates.
- Copies application/deployment sources to `/opt/desk-orchestrator`, creates its
  virtual environment, and installs `web`, `keypad`, and `tls` dependencies.
- Creates the `desk` service account, input-device permissions, protected state
  directory, seed config, and credentials file.
- Initializes managed configuration and a random web password, enables the web
  app at boot, and checks that its login page responds.
- Configures ANAVI receive GPIO 18 / transmit GPIO 17 and `dwc2` peripheral mode;
  enables the USB keyboard gadget at boot and its udev permissions.
- Selects the GPIO transmitter through `/dev/desk-ir-tx`, configures LIRC's
  `default` driver, and grants the service account access to its Unix socket.
- Creates `/dev/desk-ir-rx` with receiver permissions for `desk`, includes captured
  waveforms from `/var/lib/desk-orchestrator/ir-codes/`, and enables the
  `desk-ir-reload.path` watcher to reload lircd after learning.
- Installs the numpad service with the web-managed configuration. A fresh install
  leaves this live listener disabled until commissioning is complete.
- On reruns, stops an installed HTTPS listener before replacing code and resumes
  it only if it was previously running. Its unit, bind address, certificates,
  OAuth tokens, and other existing private state are preserved. A failed install retains a resume
  marker so the next successful run can restore HTTPS.

HTTPS is a separate opt-in installation. No AWS credentials or CLI are needed:

```sh
sudo python3 /opt/desk-orchestrator/deploy/setup_https.py --user desk --install
```

That installer checks Python dependencies and service-account file
access before installing the port-443 service. It waits for certificate issuance
configured in Settings. See [Lightsail HTTPS setup](https-lightsail.md).

A read-only check is available before installation:

```bash
bash deploy/setup-pi.sh --check
```

It checks prerequisites and known firmware/gadget conflicts; it does not test
package downloads, wiring, remote-control codes, or equipment responses. It also
refuses to install on the development PC. Unknown GPIO usage still needs checking
against your hardware. Conflicting overlays are reported for you to resolve;
compatible declarations in the main config are consolidated into a managed block.
Firmware includes are inspected but never rewritten.

### First login

From the desktop, open a tunnel (use local port 8081 if 8080 is already occupied):

```bash
ssh -L 8080:127.0.0.1:8080 YOUR_PI_USER@YOUR_PI_HOST
```

Open [the configuration app](http://127.0.0.1:8080). On the Pi, retrieve the
random initial password:

```bash
sudo cat /etc/desk-orchestrator/initial-web-password.txt
```

The file is root-readable only. To supply your own password without a prompt,
pass `--password-file /path/to/private-file`; it must contain at least 12
characters. The contents are not logged or passed in process arguments. This
option is ignored once web authentication exists. No default password is used.
To reset a password later:

```bash
sudo -u desk /opt/desk-orchestrator/.venv/bin/desk web-password --data-dir /var/lib/desk-orchestrator
sudo systemctl restart desk-web
sudo rm -f /etc/desk-orchestrator/initial-web-password.txt
```

If HTTPS is installed, also restart `desk-https` after changing the app password
so both servers load the same session-signing key.

Select the physical numpad in the web app. Supply SmartThings credentials in
`/etc/desk-orchestrator/credentials.env`, discover device IDs, capture real IR
codes, confirm KVM operation, and calibrate volume using the
[commissioning guide](commissioning.md). The installer cannot infer these from a
fresh OS. Restart the web app after changing credentials. After commissioning:

```bash
sudo systemctl enable --now desk-orchestrator.service
```

### Reruns, upgrades, and troubleshooting

For routine application updates, use the [automated update script](updating-pi.md)
from your development computer:

```bash
bash deploy/update-pi.sh pi@192.168.1.50
```

This includes developer-console changes, dependencies, backups, and service
restarts without repeating hardware setup. For changes to system packages, boot
configuration, or service units, copy the new project sources to the Pi and rerun
the installer instead. The installer preserves `/var/lib/desk-orchestrator/controller.json`, web authentication,
`/etc/desk-orchestrator/desk.toml`, credentials, and learned LIRC remote files.
It does not copy the development environment, local controller config, or preview
password. Python packages are reinstalled from the supplied project. Package
versions follow `pyproject.toml`; this is not an offline or locked-version build.

Changed system config and service files receive sibling
`.desk-backup-TIMESTAMP` backups. Project-managed settings (boot block, LIRC
transmitter, units, and udev rules) are reapplied. LIRC options outside the managed
settings are retained, but its INI formatting/comments may change; the original
is backed up. Put your own systemd overrides in a separate drop-in file.

Reruns briefly stop the web app and an active numpad listener during the Python
update. A previously active listener is resumed after successful hardware checks.
If installation fails, fix the reported problem and rerun; an interrupted update
may leave services stopped. The installer records pending firmware reboots and
listener resumption so retries can finish them. It never enables a previously
disabled listener. Configuration backups are not a full software rollback.

```bash
sudo systemctl status desk-web desk-gadget lircd.socket lircd
sudo journalctl -u desk-web -u desk-gadget -u lircd -n 100 --no-pager
ls -l /dev/desk-ir-tx /dev/hidg0
sudo -u desk irsend --device=/run/lirc/lircd LIST '' ''
```

The last command only lists learned remote names. Empty results are expected
until codes are captured. If GPIO overlays or the UDC are missing after reboot,
check the Pi kernel/firmware and power/data wiring before enabling live control.

Automated checks run on the development PC with mocked system commands and
throwaway files. Real Pi boot, apt installation, USB enumeration, and IR hardware
still require verification on your Pi.

The setup follows the [Raspberry Pi overlay reference](https://github.com/raspberrypi/firmware/blob/master/boot/overlays/README),
[ANAVI hardware guide](https://github.com/AnaviTechnology/anavi-docs/blob/main/anavi-infrared-phat/anavi-infrared-phat.md),
and [LIRC configuration guide](https://www.lirc.org/html/configuration-guide.html).
The remaining sections describe wiring and the equivalent manual installation.

## Wiring

```text
GMMK numpad ──USB-A host──> Raspberry Pi 4
                               ├── ANAVI IR TX ──> OPPO HA-1 / SMSL DA-9
                               ├── network ──> SmartThings ──> Samsung G8
                               └── USB-C device ──> KVM keyboard/HID port

KVM port 1: Linux PC        KVM DisplayPort output ──> Samsung G9
KVM port 2: Steam Deck      KVM switched USB hub ──> OPPO HA-1 USB
KVM port 3: MacBook

Linux PC DisplayPort ──> G8 DisplayPort
MacBook HDMI ──> G8 HDMI 1
```

The Pi must keep running when the KVM changes computers. Use an independently
powered arrangement designed for Pi USB-C device mode and prevent upstream
back-power. A typical host-mode USB-C docking hub is not automatically suitable.
The ANAVI board also occupies the GPIO header; verify access and electrical
compatibility before choosing GPIO power. The KVM HID port must not be assumed
to supply the Pi's required power. A CH9328 USB keyboard bridge is supported
when USB-C power/data wiring is inconvenient. It uses a Pi USB-A port and leaves
the IR HAT connected. See [CH9328 setup and migration](ch9328.md).

References: [Pi USB hardware](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html),
[Pi recommended power](https://www.raspberrypi.com/documentation/computers/getting-started.html),
[Linux HID gadget](https://docs.kernel.org/usb/gadget_hid.html).

## Operating system and overlays

Install runtime/build tools:

```bash
sudo apt update
sudo apt install python3-venv python3-dev build-essential lirc ir-keytable
```

In Ubuntu Pi's `/boot/firmware/config.txt`, add applicable overlays (preserve
existing settings and check for conflicting GPIO users):

```ini
dtoverlay=gpio-ir,gpio_pin=18
dtoverlay=gpio-ir-tx,gpio_pin=17
dtoverlay=dwc2,dr_mode=peripheral
```

Reboot. Confirm `/sys/class/udc/` contains the device controller and LIRC nodes
exist. ANAVI's documentation uses GPIO 18 receive and GPIO 17 transmit; it was
written for Raspberry Pi OS, so verify your Ubuntu image's kernel supports these
overlays. Do not install `g_ether` or another gadget driver alongside this gadget.

In `/etc/lirc/lirc_options.conf`, use `driver = default` and the actual transmitter
node as `device`. Identify receive/transmit nodes with `ir-keytable` and device
properties, not by assuming numbering. Follow [IR learning](commissioning.md#infrared-learning).
The application talks to the lircd Unix socket; it does not need raw GPIO access.

## Install the project

Copy this project from your PC to `/opt/desk-orchestrator` on the Pi using your
normal SSH/SCP workflow. Then:

```bash
sudo useradd --system --user-group --home-dir /var/lib/desk-orchestrator desk
sudo usermod -aG input desk
sudo install -d -o root -g desk -m 0750 /etc/desk-orchestrator
sudo install -d -o desk -g desk -m 0700 /var/lib/desk-orchestrator
sudo python3 -m venv /opt/desk-orchestrator/.venv
sudo /opt/desk-orchestrator/.venv/bin/pip install '/opt/desk-orchestrator[web,keypad,tls]'
sudo install -o root -g desk -m 0640 /opt/desk-orchestrator/config/desk.example.toml /etc/desk-orchestrator/desk.toml
sudo install -o root -g desk -m 0640 /opt/desk-orchestrator/deploy/credentials.env.example /etc/desk-orchestrator/credentials.env
```

If the `desk` account already exists, reuse it instead of rerunning `useradd`.
In the deployed config set `runtime.directory = "/var/lib/desk-orchestrator"`.
Use that same config for all service/CLI live runs so their locks are shared.
Use an absolute OAuth token path there as well, owned by `desk` with mode 0600.
Fill credentials privately in the environment file; do not paste them into chat.

The service's `input` group allows reading Linux input devices. If stricter
isolation is wanted, replace it with a udev rule matching only the numpad's
observed vendor/product/serial and assigning group `desk`.

## USB keyboard gadget

Install permissions and the gadget unit after checking no existing `hidg0` or `hidg1` device
belongs to another application:

```bash
sudo install -m 0644 /opt/desk-orchestrator/deploy/99-desk-hid.rules /etc/udev/rules.d/
sudo install -m 0644 /opt/desk-orchestrator/deploy/desk-gadget.service /etc/systemd/system/
sudo udevadm control --reload-rules
sudo systemctl daemon-reload
sudo systemctl enable --now desk-gadget.service
```

The setup script configures an 8-byte boot keyboard and a separate 2-byte
Consumer Control interface for media commands. Verify `/dev/hidg0` (keyboard)
and `/dev/hidg1` (media) ownership and the actual enumerated device nodes,
adjusting both config and udev rule if needed. Confirm the KVM uses double-left-
Ctrl leading keys. The script advertises a self-powered gadget and uses Linux
gadget example VID/PID for this private prototype.

When upgrading an existing keyboard-only installation or enabling extended macro
keys (F13–F24 / usages above 0x65), after copying the updated
application with the normal updater, install the new permissions and restart the
gadget. This briefly disconnects the USB keyboard from the KVM. Do this while no
task is running:

```bash
sudo systemctl stop desk-orchestrator.service
sudo install -m 0644 /opt/desk-orchestrator/deploy/99-desk-hid.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo systemctl restart desk-gadget.service
sudo udevadm trigger --subsystem-match=hidg
sudo udevadm settle
sudo -u desk test -w /dev/hidg0
sudo -u desk test -w /dev/hidg1
sudo systemctl start desk-orchestrator.service
```

Check both paths in the KVM hardware editor, then configure **Keyboard commands**
in the task editor. A KVM's dedicated keyboard port may filter consumer controls;
verify media behavior with the actual KVM and host.

Allow `desk` to access `/run/lirc/lircd` through the distribution's lircd socket
permissions/group. Inspect `systemctl cat lircd.socket`; a socket unit override
with `SocketGroup=desk` and `SocketMode=0660` is appropriate when socket activation
is in use. If lircd creates its own socket, configure its supported permission
mechanism instead. Do not grant world-write access to all devices. Test LIRC
listing as `desk` before starting the service.

Use `desk input-devices` and `/dev/input/by-id/` to select the numpad's stable
`event-kbd` symlink. Linux key codes are used rather than text characters, so Num
Lock normally does not affect the mapping; firmware remaps must be checked on
your device. The listener grabs only this configured device and reconnects if it
is unplugged. A USB-connected GMMK numpad is assumed; no Glorious-specific SDK.

## Start at boot

Finish [commissioning](commissioning.md) and run the numpad listener in dry-run
mode first. Then install/enable the live service:

```bash
sudo install -m 0644 /opt/desk-orchestrator/deploy/desk-orchestrator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now desk-orchestrator.service
sudo journalctl -u desk-orchestrator.service -f
```

The service starts listening; it does not select a scene on boot. It runs without
root, restricts filesystem writes to its state directory, and reads credentials
from the root-managed environment file. Input and HID access remain necessary.
Stop it with `sudo systemctl stop desk-orchestrator` while editing/testing manual
commands. Restart after config changes; config is loaded at startup.

Use the same environment credentials for manual probe/live commands; systemd's
EnvironmentFile is not automatically loaded into an interactive shell.

Do not enable these units on the CachyOS development machine. Hardware behavior,
USB-C power compatibility, and Ubuntu kernel support remain Pi-side checks.
