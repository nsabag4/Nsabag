"""Command-line interface: ``av210 probe`` and ``av210 scan``."""

from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path
from typing import List, Optional

from .protocol import AvisionError, NoPaperError, ScanMode
from .scanner import AV210Scanner
from .transport import DeviceNotFoundError

_DEVICE_HELP = """\
No AV210-family scanner was found.

  * Is the scanner plugged in and powered on? The AV210C2 needs its
    external 24V power supply -- without it the scanner does not
    enumerate on USB at all. Also try a different USB port/cable.

  * Windows: the driver talks to the device through WinUSB. Run Zadig
    (https://zadig.akeo.ie), pick the scanner (USB ID 0638:0A3A for the
    AV210C2), select the "WinUSB" driver and click "Replace Driver".
    Then run 'av210 probe' again.

  * Linux: your user needs permission for the USB device. Copy the
    provided udev rule and replug the scanner:
        sudo cp 99-av210.rules /etc/udev/rules.d/
        sudo udevadm control --reload && sudo udevadm trigger

  * If another driver (e.g. SANE via scanimage/xsane) currently has the
    device open, close it first.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="av210",
        description="Userspace driver for Avision AV210-family sheetfed scanners "
        "(USB 0638:0A3A and friends).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="find the scanner and print endpoints + decoded INQUIRY")

    scan = sub.add_parser("scan", help="scan one page (or the whole feeder) to PNG/PDF")
    scan.add_argument(
        "-o", "--output", required=True, metavar="FILE",
        help="output file: .png, .jpg, .tiff ... or .pdf (multi-page capable)",
    )
    scan.add_argument(
        "--resolution", type=int, default=300, choices=(150, 200, 300, 600),
        help="scan resolution in dpi (default: 300)",
    )
    scan.add_argument(
        "--mode", default="color", choices=[m.value for m in ScanMode],
        help="scan mode (default: color)",
    )
    scan.add_argument(
        "--all-pages", action="store_true",
        help="scan every sheet in the feeder (multi-page output; use a .pdf "
        "output file to get a single document)",
    )
    return parser


def _cmd_probe() -> int:
    scanner = AV210Scanner()
    info = scanner.open()
    device = scanner.transport.device
    describe = getattr(device, "describe", None)
    if callable(describe):
        print("Endpoints: %s" % describe())
    print("INQUIRY:")
    for field in fields(info):
        value = getattr(info, field.name)
        if isinstance(value, float):
            value = "%.1f" % value
        elif isinstance(value, int) and not isinstance(value, bool) and field.name in (
            "data_dq", "max_shading_target"
        ):
            value = "0x%04X" % value
        print("  %-26s %s" % (field.name, value))
    scanner.close()
    return 0


def _numbered_path(base: Path, page: int) -> Path:
    return base.with_name("%s-%d%s" % (base.stem, page + 1, base.suffix))


def _cmd_scan(args: argparse.Namespace) -> int:
    output = Path(args.output)
    mode = ScanMode(args.mode)
    is_pdf = output.suffix.lower() == ".pdf"

    scanner = AV210Scanner()
    scanner.open()
    try:
        if args.all_pages:
            images = list(scanner.scan_adf_batch(args.resolution, mode))
        else:
            images = [scanner.scan_page(args.resolution, mode)]
    finally:
        scanner.close()

    if is_pdf:
        # PIL renders "1"/"L"/"RGB" pages into a single PDF.
        pages = [im.convert("RGB") if im.mode not in ("1", "L", "RGB") else im
                 for im in images]
        pages[0].save(
            output, "PDF", resolution=float(args.resolution),
            save_all=len(pages) > 1, append_images=pages[1:],
        )
        print("Wrote %d page(s) to %s" % (len(pages), output))
    elif len(images) == 1:
        images[0].save(output, dpi=(args.resolution, args.resolution))
        print("Wrote %s" % output)
    else:
        for page, image in enumerate(images):
            path = _numbered_path(output, page)
            image.save(path, dpi=(args.resolution, args.resolution))
            print("Wrote %s" % path)
        print("Scanned %d pages" % len(images))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "probe":
            return _cmd_probe()
        return _cmd_scan(args)
    except DeviceNotFoundError as exc:
        print("Error: %s\n" % exc, file=sys.stderr)
        print(_DEVICE_HELP, file=sys.stderr)
        return 2
    except NoPaperError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 3
    except AvisionError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
