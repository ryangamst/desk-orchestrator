# Stock GMMK Numpad RGB

Research completed September 25, 2026. **Stock firmware can accept RGB commands
over USB.** Desk Orchestrator now has a lighting-only adapter and a **Numpad →
RGB lighting** panel. Physical operation still needs verification on the user's
numpad; offline tests do not establish firmware compatibility or visible output.

## Controls and setup

1. Select the physical GMMK Numpad keyboard input on the Numpad page.
2. Choose separate colors and modes for **Keys** and **Side lights**: solid,
   breathing, off, or use the existing onboard effect. Brightness applies to the
   color overlay in 5% increments; breathing speed is firmware-controlled.
3. Choose the onboard profile and layer to edit (1–3 each). Match the selection
   on the physical numpad. These are the numpad's firmware profiles, not desk tasks.
4. Save RGB settings, then click **Apply saved lighting**. The app sends one
   report to that profile/layer; it does not switch profiles. The saved form is
   the requested appearance, not a readback of the hardware.

Applying replaces that profile/layer's per-key lighting layout, including its
side LEDs. Off uses a black overlay. Use onboard effect clears the zone's
overlay so the underlying preset can show. Other profiles/layers are untouched.
The numpad's indicator priorities and per-key enable setting may affect what
appears. If the overlay is disabled, enable per-key lighting in CORE first.

The installer/updater includes read/write access for the `desk` group only on
USB interface 02 of `320f:5088`. Reconnect the numpad after updating permissions.
The keyboard and raw slider interfaces keep read-only permissions. Local desktop
preview uses the desktop's permissions and devices, not the Pi's.

Saved lighting settings are host-local, like numpad selection: backups contain
them, but restoring a backup preserves this host's lighting settings. Neither
page loads, saves, startup nor task execution sends RGB commands. There is no
streaming, automatic retry, firmware flashing, or Bluetooth control.

## Why this route

The [Glorious Numpad guide](https://www.gloriousgaming.com/pages/guide-numpad)
documents CORE lighting customization and physical shortcuts: Num Lock + 6/4
changes effect, + 8/2 adjusts brightness, and + 5 changes the active RGB zone.
Those are firmware key combinations; injecting ordinary host key events is not
a way to invoke them inside a USB keyboard.

[QMK's Numpad implementation](https://github.com/qmk/qmk_firmware/blob/master/keyboards/gmmk/numpad/readme.md)
and [VIA's Numpad definition](https://github.com/the-via/keyboards/blob/master/v3/gmmk/numpad/gmmk_numpad.json)
provide an alternative RGB Matrix route. QMK documents Bluetooth as broken and
uses MIDI for the slider. This project preserves the stock firmware and existing
controls, as requested. The public [Open Glorious Core](https://github.com/MechNoxer/Open-Glorious-Core)
driver currently identifies GMMK Pro `320f:5044`; it is not evidence of Numpad
protocol compatibility.

The decisive source was Glorious's own application, obtained through its
[software page](https://www.gloriousgaming.com/pages/software). The linked
[macOS CORE archive](https://gloriouscore.nyc3.digitaloceanspaces.com/CORE2/app/GloriousCore21.dmg)
contains CORE **2.1.3** despite the unversioned download name. Its `app.asar`
includes Numpad-specific protocol definitions. The archive was extracted for
static inspection only; CORE was neither installed nor executed. No vendor
source or installer is redistributed in this repository.

Archive SHA-256:
`d4871080797a3665603b9b7929d4afbb633299376afde6bc74fb93026e6e69c0`

Relevant files inside `app.asar`:

- `src/electron-process/renderer-interop/protocol/device/keyboard/GmmkNumpadSeries.ts`:
  `SetLEDLayoutToDevice`, `LayoutToData`, `SetLEDTypeToDevice`, `SetLEDEffectToDevice`.
- `src/electron-process/renderer-interop/others/GMMKLocation.ts`:
  LED matrix for `0x320F0x5088`.

CORE's effect/type commands `0x02`/`0x07` support preset effects but the latter
also writes sensitivity, latency, indicators and wireless brightness. This
adapter deliberately uses the separate **`0x06` lighting layout command** so
it can preserve those settings without an unverified settings readback protocol.
Full preset-effect selection, speed adjustment, per-task colors and individual
key editing are not part of this implementation.

## Lighting layout wire format

Offsets include HID report ID. Values below are derived from the Numpad-specific
CORE implementation, not the GMMK Pro driver.

| Offset | Value |
| --- | --- |
| 0 | Report ID `0x07` |
| 1 | Lighting layout command `0x06` |
| 2, 3 | Profile and layer, each 1–3 |
| 4 | 42 LED matrix slots |
| 5 | Page 0 (Numpad uses one page) |
| 6 | Overlay brightness 0–20 |
| 7 | Reserved, zero |
| 8–217 | 42 records, five bytes each: visibility, slot index, R, G, B |
| Remaining bytes | Zero padding |

Visibility is `0xff` for solid, `0xfe` for breathing, and zero for a cleared
overlay. CORE maps 17 key positions and 14 side LEDs into this matrix, leaving
unused slots and the second Num+ occurrence zeroed. A black solid record turns
that position off. CORE allocates 264 bytes; the adapter accepts the stock vendor
feature descriptor with 256 or 264 total bytes and uses its declared length.
Other descriptors fail closed. The meaningful layout fits in either length.

Discovery follows the selected Linux input's USB ancestor and checks VID/PID,
interface 02, and the stock report descriptor. It does not choose the first
matching product or store a transient hidraw number. The adapter opens only this
lighting interface and uses `HIDIOCSFEATURE`, never writes keyboard input or
consumes slider reports. USB acceptance is not an acknowledgement of visible RGB.

## Verification and physical acceptance

Offline tests cover report bytes, key/side positions, clearing/off behavior,
invalid values, descriptors, two same-model devices, wrong models, reconnects,
permissions, short writes, stale forms, authentication/CSRF and explicit-only
application. Hardware errors are reported without automatic retries.

On the Pi, confirm solid red keys and blue sides, then breathing, lower
brightness, off, and restoring the onboard effect. Check each intended
profile/layer and unplug/replug behavior; persistence has not been verified.
Check number keys, wheel rotation/click and the slider before and after applying.
If a stock firmware revision rejects the descriptor or does not show the
overlay, record that version and its descriptor and compare USB captures from
CORE before extending the protocol. Do not substitute another GMMK model's
combined configuration commands.
