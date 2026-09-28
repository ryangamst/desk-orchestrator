#!/usr/bin/env bash
# Run only on the Pi after configuring dwc2 peripheral mode and power wiring.
set -euo pipefail
gadget=/sys/kernel/config/usb_gadget/desk_keyboard
if [[ ${EUID} -ne 0 ]]; then
  echo "This script requires root on the Raspberry Pi." >&2
  exit 1
fi
if [[ ${1:-start} == stop ]]; then
  if [[ -f "$gadget/UDC" ]]; then printf '\n' > "$gadget/UDC"; fi
  exit 0
fi
if [[ ${1:-start} != start ]]; then
  echo "Usage: $0 [start|stop]" >&2
  exit 1
fi
modprobe libcomposite
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
shopt -s nullglob
controllers=(/sys/class/udc/*)
if [[ ${#controllers[@]} != 1 ]]; then
  echo "Expected one USB device controller; enable dtoverlay=dwc2,dr_mode=peripheral and reboot." >&2
  exit 1
fi
if [[ -f "$gadget/UDC" ]] && [[ -n $(cat "$gadget/UDC") ]]; then
  if [[ -L "$gadget/configs/c.1/hid.usb1" ]] && [[ $(cat "$gadget/bcdDevice") == 0x0102 ]]; then exit 0; fi
  # Upgrade an older gadget descriptor; the host will re-enumerate it.
  printf '\n' > "$gadget/UDC"
fi
mkdir -p "$gadget"
# Linux Foundation gadget example IDs, for local experimentation only.
# Use an appropriately assigned VID/PID for any distributed hardware product.
printf '0x1d6b' > "$gadget/idVendor"
printf '0x0104' > "$gadget/idProduct"
printf '0x0102' > "$gadget/bcdDevice"
printf '0x0200' > "$gadget/bcdUSB"
mkdir -p "$gadget/strings/0x409"
printf 'desk-keyboard-001' > "$gadget/strings/0x409/serialnumber"
printf 'Desk Orchestrator' > "$gadget/strings/0x409/manufacturer"
printf 'Desk KVM Keyboard' > "$gadget/strings/0x409/product"
mkdir -p "$gadget/configs/c.1/strings/0x409"
printf 'Keyboard' > "$gadget/configs/c.1/strings/0x409/configuration"
printf '0xC0' > "$gadget/configs/c.1/bmAttributes"
printf '2' > "$gadget/configs/c.1/MaxPower"
mkdir -p "$gadget/functions/hid.usb0"
printf '1' > "$gadget/functions/hid.usb0/protocol"
printf '1' > "$gadget/functions/hid.usb0/subclass"
printf '8' > "$gadget/functions/hid.usb0/report_length"
# Boot-format keyboard reports: modifier, reserved, six keys, LED output.
# Extended usage range includes F13–F24 and other Keyboard/Keypad usages.
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x26\xdf\x00\x05\x07\x19\x00\x2a\xdf\x00\x81\x00\xc0' > "$gadget/functions/hid.usb0/report_desc"
if [[ ! -L "$gadget/configs/c.1/hid.usb0" ]]; then
  ln -s "$gadget/functions/hid.usb0" "$gadget/configs/c.1/hid.usb0"
fi
# Separate Consumer Control interface keeps the boot keyboard reports unchanged.
# One 16-bit usage (0..0x03ff); zero releases the media key. No report IDs.
mkdir -p "$gadget/functions/hid.usb1"
printf '0' > "$gadget/functions/hid.usb1/protocol"
printf '0' > "$gadget/functions/hid.usb1/subclass"
printf '2' > "$gadget/functions/hid.usb1/report_length"
printf '\x05\x0c\x09\x01\xa1\x01\x15\x00\x26\xff\x03\x19\x00\x2a\xff\x03\x75\x10\x95\x01\x81\x00\xc0' > "$gadget/functions/hid.usb1/report_desc"
if [[ ! -L "$gadget/configs/c.1/hid.usb1" ]]; then
  ln -s "$gadget/functions/hid.usb1" "$gadget/configs/c.1/hid.usb1"
fi
basename "${controllers[0]}" > "$gadget/UDC"
