"""Userspace driver for Avision AV210-family sheetfed scanners.

A portable Python port of the SANE ``avision`` backend's USB protocol
(avision.c / avision.h) for the AV210 family (USB VID 0x0638, PIDs 0x0A24,
0x0A25, 0x0A2F, 0x0A3A, 0x1A35).
"""

from .protocol import (
    AvisionError,
    CoverOpenError,
    EndOfPaperError,
    NoPaperError,
    PaperJamError,
    ScanCancelledError,
    ScanMode,
    ScannerInfo,
    SenseData,
    SenseError,
)
from .scanner import AV210Scanner
from .transport import (
    AvisionTransport,
    DeviceBusyError,
    DeviceNotFoundError,
    ModelFeatures,
    TransportError,
)

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "AV210Scanner",
    "AvisionTransport",
    "AvisionError",
    "CoverOpenError",
    "DeviceBusyError",
    "DeviceNotFoundError",
    "EndOfPaperError",
    "ModelFeatures",
    "NoPaperError",
    "PaperJamError",
    "ScanCancelledError",
    "ScanMode",
    "ScannerInfo",
    "SenseData",
    "SenseError",
    "TransportError",
]
