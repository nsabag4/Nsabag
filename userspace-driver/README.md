# AV210 userspace scanner driver

A standalone, portable **userspace USB driver** for the Avision AV210 family
of sheetfed scanners, written in Python on top of libusb (pyusb).

It is a faithful port of the scan protocol implemented by the SANE `avision`
backend (`avision.c` / `avision.h`, ~20 years of field testing). Every opcode,
byte offset, timeout and quirk in this driver cites the corresponding line in
those sources.

## Supported hardware

Avision AV210 family, USB vendor ID `0x0638`:

| Product ID | Model |
|---|---|
| `0x0A24` | AV210 |
| `0x0A25` | AV210C2 (pre-production) |
| `0x0A2F` | AV210C2-G |
| `0x0A3A` | **AV210C2** (primary target) |
| `0x1A35` | AV210D2+ |

These scanners speak raw SCSI-over-USB with no wrapper: a 10-byte-padded CDB
on bulk-out, optional payload phases, and a mandatory 1-byte status read after
every command, with REQUEST SENSE on CHECK CONDITION.

## Windows setup

Windows has no inbox driver for this protocol; the driver talks to the device
through **WinUSB**:

1. Download [Zadig](https://zadig.akeo.ie) and run it.
2. `Options -> List All Devices`, select the scanner (`USB ID 0638 0A3A` for
   the AV210C2).
3. Choose **WinUSB** as the target driver and click **Replace Driver**.
4. Install and test the driver:

   ```sh
   pip install .     # from this directory (the package is not published on PyPI)
   av210 probe
   ```

   (`libusb-package` is pulled in automatically on Windows and bundles the
   libusb-1.0 DLL.)

Reverting is easy: in Device Manager, uninstall the device and rescan, or use
Zadig to restore the previous driver.

## Linux setup

```sh
pip install .                       # from this directory
sudo cp 99-av210.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
# unplug/replug the scanner, then:
av210 probe
```

The driver automatically detaches a bound kernel driver and claims interface 0.
Close any SANE frontend (scanimage, xsane, simple-scan) before using it — only
one program can own the USB interface at a time.

## Usage

```sh
# discover the device, print endpoints and the decoded 96-byte INQUIRY block
av210 probe

# scan one sheet
av210 scan -o page.png --resolution 300 --mode color

# scan the whole feeder into a single PDF
av210 scan -o document.pdf --resolution 200 --mode gray --all-pages
```

Modes: `color` (24-bit RGB), `gray` (8-bit), `lineart` (1-bit).
Resolutions: 150 / 200 / 300 / 600 dpi (the CLI restricts to these; the
library accepts anything from 75 dpi up to the device's INQUIRY maximum).

As a library:

```python
from av210 import AV210Scanner, ScanMode

with AV210Scanner() as scanner:
    print(scanner.info.model)
    image = scanner.scan_page(resolution=300, mode=ScanMode.COLOR)
    image.save("page.png")
```

## Development / tests

The protocol layer is pure computation and the transport is written against a
duck-typed USB device, so the test suite runs entirely without hardware:

```sh
pip install pyusb pillow pytest
python -m pytest tests/ -q
```

## Limitations

- **No TWAIN/WIA/SANE integration.** This is a standalone tool + Python
  library; scanning buttons inside office applications will not see it.
  Office users who need scans inside Windows applications may prefer the
  `windows-bridge/` approach instead.
- Simplex only (the AV210 family is single-sided).
- 8-bit gray / 24-bit color / 1-bit lineart; the 12/16-bit modes of the SANE
  backend are not exposed.
- Scanner buttons are not monitored.
- If the device demands shading calibration (GET CALIBRATION FORMAT
  `flags == 1`) the full dark/white calibration of the SANE backend is
  executed; typical AV210 units decline and the step is skipped.
