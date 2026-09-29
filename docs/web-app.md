# Configuration web app

The front end uses **Flask, Jinja templates, and Waitress**, with plain JavaScript
for editable form rows. It needs no Node build, CDN, or internet connection to
render. SmartThings discovery/checks still need internet access.

## Included

- Hardware create/edit/delete: name, type, control method, and connection notes.
- IR remote configuration with named commands, editable pulse sequences,
  verification flags, and calibrated approximate volume presets.
- SmartThings monitor configuration with capability and input-value mappings.
- USB HID KVM settings; computing and other equipment can be inventory-only.
- Task create/edit/delete, ordered actions, move/remove buttons, delays, and
  numpad assignments. No raw JSON editing is needed for these forms.
- Overview, live virtual numpad, active task, and setup gaps.
- Dry-run previews and explicit read-only connection checks.
- Input-device and SmartThings inventory discovery.
- A dedicated Numpad page with connected USB devices and selectable keyboard
  inputs, stable device paths, saved identity, and connection/access status.
- Custom numpad/keyboard profiles with a visual container/key editor, hardware
  key and axis learning, placeable dial/slider controls, profile-specific inputs
  and key assignments, and automatic input reconnection.
  The built-in GMMK profile remains the default. See
  [Custom numpads and keyboards](../README.md#custom-numpads-and-keyboards).
- Backup download and validated restore, password login, CSRF protection,
  atomic saves, and protection against overwriting changes from another tab.
- Hardware referenced by a task cannot be deleted or changed to an incompatible
  method. Key conflicts are rejected. Deleting a task removes its mappings.
- The **Hardware** page is a map of devices, typed ports, and connections. It is
  edited visually and operated live. See [Hardware map](#hardware-map).

The Overview's **Virtual numpad** sends live commands: pressing a mapped key runs
the same task as the physical numpad, using the saved key mapping and the shared
runner. Unassigned keys are disabled. The virtual numpad does not require a USB
numpad or a running input listener. Use the web app hosted on the Pi to operate
the Pi's hardware; the desktop site targets the desktop controller.

Keys assigned to the active task's keyboard commands use that task's color.
Hover or focus a key to see its command or custom macro name and highlight its
task card. Hovering a task also highlights its active keyboard mappings. These
colors and previews update automatically when the active task or mappings change.

The wheel's clockwise/counterclockwise buttons and the slider's up/down buttons
use the active task's mapped IR commands, including the input's configured
direction reversal. Focus the wheel to turn it with arrow keys or scrolling, or
drag the slider. Slider position describes browser movement, not actual volume;
each accepted movement sends one directional command. Both controls require a
configured input and active-task mapping. Changing tasks resets the virtual
slider's baseline without transmitting a command.

Physical and virtual input share the same execution lock,
command verification, and partial-failure behavior. Inputs received while busy
are rejected, not queued; rapid key/control inputs are limited to 350/100 ms.
Requests carry the displayed mapping revision, and control requests also verify
the active task under the execution lock. Changed mappings, task cards, and
Overview counts refresh in the background without reloading the page. Switching
profiles or changing the custom layout reloads the Overview to display its new keys.
Recent request IDs are remembered across HTTP/HTTPS workers to prevent
duplicate dispatch. Connection failures never trigger automatic retries: check
**Activity** before trying again because the input may already have run.

The virtual numpad polls active task and availability every 1.5 seconds, and
refreshes immediately after a press completes. Controls stay in place while
the command runs; results appear inline. Only
authenticated, CSRF-protected POST requests can operate hardware; opening the
page and polling state never transmit commands. Previews and connection checks
remain read-only. Recorded execution is not physical device telemetry.

## Hardware map

**Hardware** in the sidebar draws every hardware item as a node with ports, and
the cables between them. (`/map` redirects there.) The page opens live; choose
**Edit** to change it.

**Edit** mode changes hardware and the diagram. **Exit edit mode** returns to the
live map, and asks first if there are unsaved changes.

- **Add hardware** asks for a name, a device ID (suggested from the name; it can't
  change later), a type, and a control method, then places the device in the next
  free spot for its type. A second USB keyboard / KVM is refused. The new device
  has no control settings until you save; then use its **Control settings** link
  (and **Add & learn IR commands** for IR devices).
- Select a device to rename it or edit its type and connection notes. The control
  method is changed on the Control settings page. **Delete** removes the device
  and its connections when you save; it is disabled, with the task names, while
  a task's actions, dial/slider mappings, or key mappings use the device. The
  Raspberry Pi controller cannot be deleted.
- Drag a device to move it; with a device focused, arrow keys move it by 10
  (Shift: 50). **Auto-arrange** lays devices out in columns by type.
- Select a device to edit its ports. Each port has a name, a signal type
  (Video, Audio, USB / data, Infrared, Network, Power, Other), a direction
  (input, output, two-way), and optionally the command it runs: an IR command,
  a SmartThings input, or a KVM port 1–4. **Add suggested ports** creates ports
  from the device's saved inputs, KVM ports, or IR receiver.
- Drag from one port's dot to another's to connect them, or use **Add a
  connection** in the side panel. Select a connection and press Delete, or use
  **Remove**. Duplicate and self connections are rejected.
- **Save** writes added, renamed, and deleted hardware together with all
  positions, ports, and connections in one revision-checked save. If anything is
  invalid (for example a device still used by a task) or the tab is stale,
  nothing is saved. **Discard changes** returns to the saved state. Leaving the
  page with unsaved changes asks first.

Outside edit mode the page sends live commands, with the same rules as the
virtual numpad:

- By default the page shows only the map. The route of the last task sent stays
  outlined in its color.
- Select a device to open its side panel and send any single command it
  supports: IR commands and volume presets, SmartThings inputs, or KVM ports.
  Selecting a port that has a command offers **Send**. Single commands never
  change the active task. **Close** returns to the map alone.
- The panel's **Used by tasks** buttons run a task. Hovering or focusing one
  highlights its route: the devices and ports its actions switch, the cables on
  those ports and the devices at their far ends, and the controller's links to
  the devices it drives. Numpad keys are pressed from the Overview's virtual
  numpad.

Map data has no separate store. Each item in `controller.json`'s `inventory`
carries an optional `map` field:

```json
"g8": {"name": "Samsung G8 OLED", "type": "Monitor", "method": "smartthings", "notes": "", "settings": {…},
       "map": {"x": 640, "y": 40,
               "ports": {"hdmi_1": {"label": "HDMI 1", "signal": "video", "direction": "in",
                                    "action": {"kind": "monitor", "input": "hdmi1"}}},
               "links": []}}
```

A connection is stored once, on the device that owns its output side
(`{"port", "to", "to_port"}`). Validation rejects links to missing devices or
ports and port commands that no longer exist on the device, just as task actions
are checked. Deleting hardware removes connections into it. Saving the hardware
form keeps the map. Backups and restore include the map.

The **numpad** appears on the map once a USB input is selected on the Numpad page
(the example config's placeholder path doesn't count). It is shown with the
selected device's name and starts with one USB port connected to the
controller's numpad input. Select it to see its task keys and the active
task's key mappings, each with a **Press** button that uses the virtual
numpad's input path, plus the wheel and slider targets. In edit mode you can
move it and edit its ports and connections. Its ports can't run commands, and
it can't be renamed or deleted there: the device is chosen on the Numpad page.
Its layout is saved in `keypad.map`, next to the numpad's device path and key
bindings, and it keeps its layout if a different USB input is selected later.
`desk_numpad` is a reserved ID. Restoring a backup on a Pi without a configured
numpad drops connections to it.

The **Raspberry Pi controller** (`desk_controller`) is added automatically, once,
to existing configurations. Its ports cover the IR transmitter, USB keyboard to
the KVM, the numpad, and the network. You can rename it and edit its ports and
notes, but it cannot be deleted or given a control method. Restoring a backup
made before the map keeps this Pi's controller node.

The map shows saved configuration and the last task sent. It is not measured
hardware state.

## Run on the development PC

```bash
cd /home/you/Documents/Code/desk-orchestrator
.venv/bin/pip install '.[web]'
.venv/bin/desk web-password --data-dir .runtime/web
.venv/bin/desk -c config/desk.example.toml web --data-dir .runtime/web
```

Open `http://127.0.0.1:8080`. Password entry is interactive and not echoed or put
in shell history. Reset it with the same command and restart the web process;
existing sessions expire. For scripted initial setup, the app accepts
`DESK_WEB_PASSWORD` only when no password file exists. There is no default password.
The implementation preview stores its random password in
`.runtime/web/local-preview-password.txt` (mode 0600); remove that convenience
file after changing the password. Do not copy preview authentication to the Pi.

To keep the desktop web app running independently of a terminal, use the local
user service after initializing the password. Stop any foreground web process
first so port 8080 is available:

```bash
systemctl --user enable --now "$PWD/deploy/desk-web-local.service"
systemctl --user status desk-web-local.service
```

This unit assumes the checkout is in `~/Documents/Code/desk-orchestrator`;
adjust its paths if you move the project. It starts at user login and restarts
after a failure. It reuses `.runtime/web`, including saved configuration and
authentication. Manage it without sudo:

```bash
systemctl --user restart desk-web-local.service
journalctl --user -u desk-web-local.service -n 50
systemctl --user stop desk-web-local.service
```

Restart it after Python code changes. To disable automatic startup, run
`systemctl --user disable --now desk-web-local.service`. The Raspberry Pi uses
the separate system service described below.

On first startup the app imports the `--config` TOML/JSON seed once. The seed is
never overwritten. Edits are persisted to `DATA_DIR/controller.json`. Use that
file with the existing CLI:

```bash
.venv/bin/desk -c .runtime/web/controller.json plan work
.venv/bin/desk -c .runtime/web/controller.json listen
```

The listener reloads tasks, hardware controls, and key bindings before each new
key press. A running task uses its original snapshot, so editing a task cannot
change it halfway through execution. Changing the numpad path or exclusive access
requires restarting the listener. Run **all** live controller processes from the
same managed JSON file so they share a lock and state directory. Do not leave a
listener running against the old seed TOML after switching to the UI.

## Deploy on Raspberry Pi / Ubuntu Server

The [automated Pi installer](raspberry-pi.md#automated-installation-recommended)
installs this web app, initializes its password, and configures both services.
If you used it, continue with SSH-tunnel access below; skip the manual installation
and managed-configuration drop-in commands.

For manual deployment, use the [Pi hardware setup](raspberry-pi.md) first. These instructions
assume the project is in `/opt/desk-orchestrator`, the service user is `desk`, and
the seed configuration is `/etc/desk-orchestrator/desk.toml`.

```bash
sudo /opt/desk-orchestrator/.venv/bin/pip install '/opt/desk-orchestrator[web,keypad]'
sudo -u desk /opt/desk-orchestrator/.venv/bin/desk web-password --data-dir /var/lib/desk-orchestrator
sudo install -m 0644 /opt/desk-orchestrator/deploy/desk-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now desk-web.service
```

Check `sudo journalctl -u desk-web.service` and confirm the import succeeded.
The web service works independently of the HID gadget, so configuration is usable
before hardware commissioning. It uses the existing non-root `desk` user and
the LIRC, input-device, and credential access needed for discovery.

Access the default loopback service through an SSH tunnel from your desktop:

```bash
ssh -L 8080:127.0.0.1:8080 YOUR_PI_USER@YOUR_PI_HOST
```

Visit `http://127.0.0.1:8080` on the desktop. If the desktop preview already uses
port 8080, stop it or use `-L 8081:127.0.0.1:8080` and visit port 8081.

For direct access on a trusted home LAN, use `sudo systemctl edit desk-web`:

```ini
[Service]
ExecStart=
ExecStart=/opt/desk-orchestrator/.venv/bin/desk --config /etc/desk-orchestrator/desk.toml web --data-dir /var/lib/desk-orchestrator --host 0.0.0.0 --port 8080 --trusted-host desk-pi.local --trusted-host 192.168.1.50
```

Replace the example hostname/IP with the Pi's actual address and restart the
service. Allow the port in the Pi firewall only for the intended LAN. HTTP does
not encrypt passwords or sessions; prefer the SSH tunnel or an HTTPS reverse proxy
on untrusted networks. With HTTPS, add `--secure-cookie`. Do not port-forward this
app to the internet. No reverse proxy is installed automatically.

Once `controller.json` exists, point the numpad service at it:

```bash
sudo install -d /etc/systemd/system/desk-orchestrator.service.d
sudo install -m 0644 /opt/desk-orchestrator/deploy/web-managed-controller.conf /etc/systemd/system/desk-orchestrator.service.d/web-managed.conf
sudo systemctl daemon-reload
sudo systemctl restart desk-orchestrator.service
```

This preserves the service's hardware dependencies and protections. The web UI
does not start a second listener. Restart the web service after software updates;
Waitress does not hot-reload Python files.

## Select the physical USB numpad

Open **Numpad** in the sidebar or **Configure USB device** below the overview's
numpad heading. The page lists USB peripherals attached to the computer hosting
the app, including devices that cannot be used as a numpad. The selector offers
input interfaces that advertise KP1, KP2, KP3, KPENTER, and KPPLUS. Full keyboards
and some multi-function peripherals may advertise these too, so select the
dedicated numpad by its name, USB port, and input interface.

Click **Refresh USB devices** after plugging in a device. Choose an input and
**Save numpad device**. The app prefers `/dev/input/by-id/` aliases, then
`/dev/input/by-path/`; a bare event number is used only if neither exists. By-path
tracks a physical port, while event numbers may change after reconnect/reboot.
The manual option supports devices temporarily unplugged or using custom firmware.

Saving retains all key mappings. Restart the numpad listener to open the selected
device: `sudo systemctl restart desk-orchestrator`. The web app does not restart
services or grab the selected device itself. Its connection status describes USB
presence/access, not confirmation that the listener is running.

Discovery reads Linux sysfs without root, additional packages, or capturing keys.
If an interface is visible but unreadable, check `/dev/input` visibility and the
service user's input permissions. The web service and listener should run as the
same `desk` user, as in the provided systemd units. Missing sysfs produces an
explicit discovery error. On the desktop preview, the list describes the desktop;
after deployment it describes the Pi.

## Configure the rotary dial and slider

The **Task editor → Dial and slider** section lets each task select a device
and two directional commands independently for the rotary dial and slider.
Choose **Device volume (relative)** for volume up/down, **Directional IR
commands** for other command pairs, or **Unassigned**. The current output adapter
supports IR devices; it does not change a PC's software mixer or send SmartThings
volume commands.

### Configure what a wheel click does

In **Task editor → Dial and slider → Rotary dial**, choose **Device volume
(relative)**. Under **Wheel click**, configure **Source 1 · Default** with its
volume device and increase/decrease commands. This is the default volume control
as soon as the task completes; no click is needed to activate it. Use **Add volume
source** for a second, third, or further device. There is no separate default
target or click-action selector.

Use **Move up**, **Move down**, and **Remove** to edit the ordered list. The first
entry is always the default, and the final entry cannot be removed. The app
supports 1–100 entries per task. Choose **Unassigned** under Function to disable
the rotary mapping.

For example, a list ordered **SMSL → OPPO → source 3 → source 4** starts on SMSL,
then selects OPPO on click 1, source 3 on click 2, source 4 on click 3, and SMSL
again on click 4. A successful task run restores source 1; editing the control
mapping also clears its previous selection. A single-source list stays on that
source when clicked. Each task has its own list. Slider mappings are independent.

On **Numpad → Rotary dial and slider inputs**, enter the **Wheel click key code**
reported when you press the wheel in the developer console or input monitor.
Do not infer the code from rotation events. If the click arrives on a different
interface, select its **Wheel click input device**; otherwise leave that field
blank. Restart the listener after changing these physical input settings.

Only key-down clicks advance the cycle; releases and held-key repeat events do
not. Clicks send no IR commands. Rotating sends the selected source's bounded
volume commands, subject to the same verification, task lock, and rate limits
as other controls. The physical and virtual wheel share the selection across
controller processes. Click the virtual wheel, or focus it and press Enter or
Space, to cycle; its target label shows the source and position in the list.
Disabling the click input leaves rotation available.

### Set up rotation and slider inputs

1. In **Hardware**, use **Add volume commands** to create `volume_up` and
   `volume_down` entries on the target device, then enter and verify their learned
   LIRC keys. Existing entries are preserved. The
   editor will select these automatically when present. Each command must contain
   exactly one IR pulse with count 1 and a gap of 0.02–0.5 seconds. Leave commands
   unverified until you have learned and checked their actual codes.
2. Open **Numpad** and find the selected numpad's input interfaces. On the
   original-firmware unit inspected during development, a separate **Consumer
   Control** interface advertised `KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, and
   `ABS_VOLUME`. Capability flags alone do not prove either control emits them.
3. Verify each control separately using the chosen interface on the host where
   it is connected:

   ```bash
   .venv/bin/desk input-monitor --device /dev/input/by-id/YOUR_CONTROL_INTERFACE --seconds 20
   ```

   This reads for a bounded duration (1–60 seconds), prints capabilities and
   events, and neither grabs the device nor sends commands. Stop the live listener
   first if it already has exclusive access. The account needs permission to read
   that input; the app does not grant device permissions automatically.
4. In **Rotary dial and slider inputs**, select the interface and signal type.
   For key events (type 1), specify separate increase/decrease codes; standard
   volume codes are 115 and 114. For relative movement (type 2), use the reported
   axis code, such as 8 for `REL_WHEEL`. For absolute movement (type 3), use its
   axis code, such as 32 for `ABS_VOLUME`, and set a jitter threshold in raw units.
   Reverse direction if needed. These are starting examples, not verified defaults.
   A physical rotary wheel can emit **key** events. If the developer console shows
   `KEY_VOLUMEUP` / `KEY_VOLUMEDOWN`, choose **Two keys** with codes **115 / 114**,
   using the device path in that input event. Do not select Relative axis just
   because the control turns. The form rejects key codes used as axis codes.
5. Save the input settings and restart the listener. It opens each selected
   interface once, using the numpad's exclusive-access preference. If both controls
   produce identical events on the same interface, they cannot have independent
   mappings; leave one disabled until distinct signals are available.
6. Edit each task to select its target device and commands, save, and review its
   preview. The control mappings are checked along with the task actions. Use
   `desk -c .runtime/web/controller.json listen` for a dry run; add `--live` only
   after commissioning. A dry run prints control actions without transmitting IR
   or writing active-task state.

In **Task editor → Task details**, enable **Run without changing the active task**
for tasks such as Audio Power. Its action sequence still runs in order, but the
current task, keyboard mappings, and selected wheel volume source remain active,
including if an action fails. If no task is active, none becomes active. The
numpad key that launches the task remains available. Keyboard mapping and
Dial and slider sections are hidden and disabled for these tasks; saving removes
any existing mappings belonging to that task. Clear the option to configure a
normal task again. Existing tasks default to normal activation.

In JSON/TOML configuration, this option is `keep_active_task = true`; it cannot
be combined with nonempty `key_commands` or `controls`. These runs use the same
execution lock and console logging, with their latest execution status stored in
`action-status.json`; `status.json` continues to track the active-task run and
its volume-source selection. No controls run while either kind of task executes.

The last successfully completed live task with normal activation owns the controls. No control action
runs before a successful task, during task execution, or after partial failure
of a task with normal activation. Hardware preflight checks do not block execution.
The listener shares the task execution lock, discards repeats/busy input, caps
dispatch at ten actions per second (actual IR timing may be slower), and never
queues a backlog. Reconnection and task transitions reset the slider baseline;
the initial absolute reading sends nothing. Mappings are reloaded from saved
configuration on each actionable event. Deleting or changing referenced hardware
or command names is blocked until its task mappings are updated.

In the developer console, **Input received** records the raw Linux signal, not
an outgoing command. Follow its trace through **Input mapped to control**,
**Control mapped to task command** (for example, `oppo.volume_up`), and **LIRC
accepted SEND_ONCE** to see the actual remote/key transmitted. Releases and
repeats are intentionally ignored. An unmatched volume-key press now explains
how to configure its input; a mapped control with no successfully completed
active task is also ignored and logged separately.

The slider adjusts volume by direction of movement. Its position is **not** a
percentage or dB target. IR volume has no reliable absolute feedback here, so the
app does not infer a volume level or run repeated calibration sweeps when you
move it. Choosing **Device volume** means you must select genuine volume-up/down
commands for that device.

### Stock GMMK Numpad raw HID slider

The stock device captured on the Pi (USB `320f:5088`) produces slider reports
on raw HID interface 01, while all four evdev interfaces remain silent during
slider movement. The advertised `ABS_VOLUME` capability is not its actual slider
signal. The supported nine-byte report is `04 f8 32 00 XX HH LL 00 00`; `HH LL`
is interpreted as a big-endian position. The capture is retained in
`tests/fixtures/gmmk_slider.txt`; the observed range is not treated as calibration
or an absolute volume target. The coarse `XX` field is not used.

After updating the Pi, select **GMMK Numpad slider (raw HID)** under Slider,
save, and restart `desk-orchestrator`. This mode automatically uses the numpad
selected above, matching USB ancestry, vendor/product IDs and interface 01.
It re-discovers the raw device after disconnection and does not depend on a
`hidrawN` number. No device path or code needs to be entered for this mode.
Changing the selected numpad updates the raw slider’s reference as well.
The rotary dial remains **Two keys**, normally on Consumer Control with 115/114.
Existing configurations are preserved by updates; raw mode must be selected.

The adapter opens the raw interface read-only and never grabs it, writes reports,
or changes firmware. Setup and the updater install `99-desk-gmmk.rules`, granting
only the `desk` group read access to the GMMK’s raw interfaces. The first report
on connection or task transition establishes a baseline. The movement threshold
uses raw position units (default 4); raise it if the physical slider jitters.
Reverse direction remains available. Buffered reports are reduced to the latest
position; controls retain the shared execution lock and discard busy movement.

Read positions without sending commands (no need to stop the evdev listener):

```bash
sudo -u desk /opt/desk-orchestrator/.venv/bin/desk input-monitor \
  --gmmk-slider \
  --device /dev/input/by-id/usb-Glorious_GMMK_Numpad-event-kbd --seconds 20
```

The console identifies the raw reader separately as `gmmk-slider:…`, shows its
resolved raw device, and reports disconnection or permission failures. If the
firmware sends another report format, it is ignored. QMK MIDI firmware is not
handled by this adapter.


Backups preserve per-task mappings. Restore retains this host's physical dial
and slider input paths/settings along with its numpad selection.

## Backups and credentials

Download backups from Settings before bulk changes. Restore replaces hardware,
tasks, and bindings only; it preserves this Pi's executable, runtime directory,
numpad device, and credential references. Backups contain hardware IDs and
connection references, but not environment values or OAuth token contents. They
do not include learned lircd waveforms; back up `/etc/lirc/lircd.conf.d/` and
`/var/lib/desk-orchestrator/ir-codes/` separately. Uploads are capped at 1 MB.

To test a SmartThings input, save the device and its input mappings, then reopen
**Hardware → Configure hardware → Test saved inputs**. Each button sends the
saved input value to that monitor and waits for the configured status attribute
to match. Tests are available before checking **Capability and input values
verified on this device**; they do not mark hardware verified automatically.
Check the physical picture yourself before marking it verified. Unsaved edits
disable the buttons until saved, and stale pages must be reloaded. Offline
devices, placeholder values, and overlapping scene/test execution are rejected.
Results appear below the buttons and command attempts appear in the developer
console. A failed confirmation can mean the monitor changed inputs without
reporting back; inspect it before retrying.

SmartThings secrets remain in the server's environment/OAuth token file. Use
**Settings → SmartThings OAuth** to link your account and test connection/refresh.
See the [OAuth setup guide](smartthings-oauth.md) for credentials and callback
hosting. The same hostname must serve the login, Settings, and callback pages.

## What to add next

1. **Guided volume calibration:** measure physical response and build calibrated
   presets. Single-button IR learning is available from Hardware.
2. **Measured state feedback:** distinguish “command sent” from “device is on”,
   and make exact HA-1 volume possible. Requires hardware support or sensors.
3. **Execution history and recovery:** retain multiple runs, add bounded
   cancellation, and define a deliberate recovery task after partial failure.
4. **Wiring-aware tasks:** the Hardware page’s map now records which computer is on each
   KVM port and which cable feeds each monitor input. Tasks still select ports and
   inputs explicitly; a later step could offer “switch to this computer” actions
   derived from the map.

The adapters support IR commands/volume presets, SmartThings input selection, and
one USB HID KVM. Choosing a hardware type does not add control protocols:
Wake-on-LAN, generic SmartThings actions, or a second KVM need adapter extensions.

## Verification

Install both optional extras, then run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover persistence, references, key conflicts, stale-save rejection,
backups, authentication, CSRF, escaping, and controller behavior. Browser checks
cover editor rendering, saves, and responsive layout. Live Pi hardware remains
a separate commissioning step.

## Developer console

Open **Developer console** in the sidebar to follow the controller on the host
named at the top of the page. It refreshes every second and shows:

- Input device path, Linux key names and numeric codes, press/release/repeat values.
- Task/control mappings and ignored-input reasons (busy, debounce, repeat,
  unmapped input, movement threshold, or changed control configuration).
- Resolved command destinations: LIRC remote/socket and learned keys, USB HID
  device/port and reports, or SmartThings device/component/capability/input value.
- Dry runs, command failures, and completion. IR/KVM send
  success does **not** verify the physical device response; SmartThings monitor
  completion includes its input feedback check.

Expand a row for full details and select its trace to follow the originating
input through execution. Search, category/level filters, pause/resume, clear
view, and JSON export work on the browser's loaded events. Clear view does not
delete the shared history. The last 2,000 events persist in
`runtime.directory/events.sqlite3`, shared across web, CLI, and listener processes.
Old events expire automatically, including while the view is paused. A gap
notice indicates that older events are no longer available. Dry runs now write
diagnostics, but still send no hardware commands or active-task state.

Listener status refreshes every five seconds; reports older than 15 seconds
are marked stale. The page reports disconnected interfaces and mismatches between
saved inputs and running listeners. No report means the listener may not be
running, may use another runtime directory, or may still run older code.
The [automated update script](updating-pi.md) installs the new code and restarts
the web service and any running numpad service. For manual updates, restart both
services yourself. They must use the same managed `controller.json`. The console
never opens an input device, starts another listener, or transmits commands itself.

Only configured listener interfaces are logged, never keyboard events from the
browser. The password-protected endpoint returns selected diagnostics; it does
not serialize credentials, environment variables, or raw API response bodies.
A log storage failure is reported to the service journal and does not abort a
hardware command. Logs are local troubleshooting records, not an audit trail.


## Add and learn IR commands

Save an Infrared (LIRC) hardware device, then choose **Add & learn IR commands**
from its hardware card or configuration page.

- **Add an existing LIRC key:** choose a remote, load its keys, select a key and
  give the action a command ID such as `volume_up` or `usb`.
- **Learn a button:** enter a command ID and carrier frequency (38,000 Hz by
  default), start learning, then briefly press the physical remote button within
  12 seconds. The receiver is configurable in Settings and defaults to
  `/dev/desk-ir-rx`. Select **Replace an existing command** to relearn an action;
  its verification and discrete-state flags are reset.
- **Test one press:** sends one real `SEND_ONCE`. Once you observe the correct
  response, confirm it and mark the command verified. Power on/off actions also
  require confirmation that they select a fixed state instead of toggling.

Capture uses `mode2 --driver default` on the receiver while lircd retains the separate
transmitter. Capture and test sends share the task execution lock: competing
requests are rejected, not queued. Closing the page does not stop a capture;
its receiver process exits after a frame or the 12-second timeout. Missing LIRC,
permissions, receiver timeouts, concurrent edits and unavailable codes show
errors without marking a command verified. A failed test is never retried
implicitly.

Each capture creates an immutable file under the app data directory's
`ir-codes/`, with a unique LIRC remote name. Pulse-level **Remote override**
keeps existing device remotes and macros intact and is used by tasks, presets,
and directional controls. Removing an action does not delete its waveform:
existing configurations and backups may still reference it.

This is single-frame raw learning, capped at 1,023 timings / 0.5 seconds, with
20 ms spaces delimiting frames. It cannot infer carrier frequency, toggle bits,
or multi-frame protocols. Test repeated presses on the equipment; use the
[irrecord commissioning workflow](commissioning.md#infrared-learning) when a
remote needs protocol-aware capture, then add its installed keys from this page.

### Enable capture on an existing Pi

After copying/updating the code, run `sudo bash /opt/desk-orchestrator/deploy/setup-pi.sh`
once on the Pi. Ordinary application updates do not install systemd or udev
changes. Setup preserves saved configuration and learned waveforms, installs the
`/dev/desk-ir-rx` receiver rule, creates the LIRC include, and enables
`desk-ir-reload.path`. The watcher invokes a fixed service to send SIGHUP to
lircd after capture files change; the web app remains unprivileged.

If testing reports that a captured code is not loaded, check:

```bash
ls -l /dev/desk-ir-rx
sudo systemctl status desk-ir-reload.path desk-ir-reload.service lircd
sudo journalctl -u desk-ir-reload.service -u lircd -n 50 --no-pager
```

Custom installations must include their data directory's `ir-codes/*.conf` in
lircd's configuration, grant lircd read access, and arrange reloads after file
changes. The supplied units target `/var/lib/desk-orchestrator`. The main
`/etc/lirc/lircd.conf` must include `/etc/lirc/lircd.conf.d/*.conf` (the distro
default). Keep lircd on TX and mode2 on RX; do not share one receive node between
processes.

References: [mode2](https://www.lirc.org/html/mode2.html),
[raw code configuration](https://www.lirc.org/html/lircd.conf.html),
[lircd reload behavior](https://www.lirc.org/html/lircd.html).


### Receiver stopped / gpio_ir_recv appears as an input device

Linux can expose the IR receiver both as `/dev/input/eventN` (decoded keys) and
`/dev/lircN` (raw pulse timings). The **Find input devices** list shows the former.
Raw learning needs the latter. Set **Settings → IR receiver** to
`/dev/desk-ir-rx`, the receive alias installed by setup. The **Numpad** selection
is independent: select the actual USB numpad there, not `gpio_ir_recv`.

If `/dev/desk-ir-rx` is missing, update the app and rerun the Pi installer once;
application-only updates do not install the receiver udev rule. To diagnose on
the Pi, with browser learning idle:

```bash
ls -l /dev/desk-ir-rx /dev/lirc*
sudo -u desk env LIRC_OPTIONS_PATH=/dev/null timeout 12s mode2 --driver default --device /dev/desk-ir-rx
```

Press a remote button during the second command. Pulse/space lines confirm raw
capture access as the web service account. The timeout exiting after 12 seconds
is expected. If using `/dev/lircN` directly, identify the receive node; do not
assume its number or select the transmit-only node. The app now rejects evdev
receiver paths and reports mode2's diagnostic text when capture exits.


If mode2 prints `code: 0x...` lines instead of `pulse` and `space`, the app's old
`--raw` capture command can be the cause. LIRC 0.10.x bypasses driver initialization
in that path and can format raw timing bytes as codes. Updated capture explicitly
uses the `default` kernel LIRC driver and an empty options file so machine-wide
settings cannot change its output format. It also requires read/write access,
as the default driver opens the device in both directions. The installer’s
`GROUP="desk", MODE="0660"` receiver rule already supplies that access.
Unexpected `code:` output now reports a format error immediately instead of a
misleading no-signal timeout. Deploy the updated app; existing receiver rules
and learned commands can remain in place.


### Learned successfully, but Test one press fails

Saving a waveform and loading it into lircd are separate steps. A receive-only
udev repair makes learning possible; it does not install the learned-code include
or reload watcher. The test page checks that the remote is loaded before sending,
and reports the actual irsend output for failures, including whether `LIST` or
`SEND_ONCE` failed. Failed sends are not automatically retried.

On the Pi, these checks do not transmit anything:

```bash
sudo -u desk irsend --device /run/lirc/lircd LIST '' ''
sudo systemctl status desk-ir-reload.path desk-ir-reload.service lircd --no-pager
sudo journalctl -u desk-ir-reload.service -u lircd -n 40 --no-pager
```

If the learned remote is absent, check that `/etc/lirc/lircd.conf` includes
`/etc/lirc/lircd.conf.d/*.conf` and that `desk-learned.conf` in that directory
includes `/var/lib/desk-orchestrator/ir-codes/*.conf`. The updated Pi installer
installs the include and watcher while preserving captures. If the remote is
loaded but sending fails, use the reported error and lircd journal to check
that lircd uses the `default` driver and `/dev/desk-ir-tx` transmitter.
A completed `desk-ir-reload.service` is normally inactive (dead), with a
successful exit; the `.path` watcher should be active (waiting).

## GMMK Numpad RGB lighting

The Numpad page includes a stock-firmware USB RGB panel for separate keys and
side-light colors, solid/breathing modes, off, and brightness. Save settings,
then apply them explicitly to the chosen onboard profile/layer. The lighting-only
adapter preserves input settings and needs no firmware change. See
[stock RGB setup and research](gmmk-rgb.md) for permissions, supported behavior,
and the remaining physical verification.
