"""Read-only adapter for the captured stock GMMK Numpad slider protocol."""
import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace


def slider_position(report):
    # Captured reports: 04 f8 32 00 XX HH LL 00 00. XX is a coarser
    # reading; HH LL tracks position across byte boundaries in big-endian order.
    # Do not interpret keyboard, dial, firmware replies, or partial reports.
    if len(report) != 9 or report[:4] != b"\x04\xf8\x32\x00" or report[7:] != b"\x00\x00":
        return None
    return int.from_bytes(report[5:7], "big")


def usb_parent(path):
    for parent in (path, *path.parents):
        if (parent / "idVendor").is_file() and (parent / "idProduct").is_file():
            return parent
    raise OSError("Cannot identify the selected numpad's USB device")


def find_device(input_path, sysfs=Path("/sys"), dev=Path("/dev")):
    """Match USB ancestry, VID/PID and interface, never the current hidraw number."""
    event = Path(input_path).resolve(strict=True).name
    if not event.startswith("event") or not event[5:].isdigit():
        raise OSError("Select the numpad's stable Linux input path for the GMMK slider")
    usb = usb_parent((sysfs / "class/input" / event / "device").resolve(strict=True))
    if ((usb / "idVendor").read_text().strip().lower(),
            (usb / "idProduct").read_text().strip().lower()) != ("320f", "5088"):
        raise OSError("Raw slider mode requires a Glorious GMMK Numpad (320f:5088)")
    matches = []
    for node in (sysfs / "class/hidraw").glob("hidraw*"):
        try:
            target = (node / "device").resolve(strict=True)
            if usb_parent(target) != usb:
                continue
            interface = next(p for p in (target, *target.parents) if (p / "bInterfaceNumber").is_file())
            if (interface / "bInterfaceNumber").read_text().strip() == "01":
                matches.append(dev / node.name)
        except (OSError, StopIteration):
            continue
    if len(matches) != 1:
        raise OSError("Expected one GMMK raw HID interface 01 for the selected numpad")
    return str(matches[0])


class SliderDevice:
    name = "Glorious GMMK Numpad raw HID slider"

    def __init__(self, input_path):
        self.path = find_device(input_path)
        self.fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)

    def close(self):
        os.close(self.fd)

    async def async_read_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            ready = loop.create_future()
            def readable():
                if not ready.done():
                    ready.set_result(None)
            loop.add_reader(self.fd, readable)
            try:
                await ready
            finally:
                loop.remove_reader(self.fd)
            # Drain this readiness batch before yielding. Only its latest slider
            # position matters; a buffered burst must never queue IR commands.
            latest = None
            for _ in range(256):
                try:
                    report = os.read(self.fd, 4096)
                except BlockingIOError:
                    break
                if not report:
                    raise OSError("GMMK raw HID device disconnected")
                position = slider_position(report)
                if position is not None:
                    latest = position
            if latest is not None:
                received = time.monotonic()
                yield SimpleNamespace(type=3, code=32, value=latest,
                                      timestamp=lambda t=received: t)


async def monitor(input_path, seconds):
    from contextlib import closing
    with closing(SliderDevice(input_path)) as device:
        print(f"Reading {device.name} ({device.path}) for {seconds} seconds; no commands will be sent.", flush=True)
        async def read():
            async for event in device.async_read_loop():
                print(f"position={event.value}", flush=True)
        try:
            await asyncio.wait_for(read(), seconds)
        except asyncio.TimeoutError:
            pass
