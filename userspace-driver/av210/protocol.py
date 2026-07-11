"""Avision AV210-family SCSI-over-USB protocol: CDB builders, parsers, math.

Everything in this module is a byte-faithful port of the SANE ``avision``
backend (avision.c, 9580 lines / avision.h, 947 lines).  Each constant and
formula cites the line in those files it was taken from.  This module is pure
computation -- no I/O -- so it is fully unit-testable without hardware.

All Avision multi-byte wire fields are BIG-endian (``set_double`` /
``set_triple`` / ``set_quad``, avision.h:855-905) unless explicitly noted as
little-endian (calibration sample words and uploaded gain words).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# SCSI opcodes (avision.h:599-611)
# ---------------------------------------------------------------------------

SCSI_TEST_UNIT_READY = 0x00
SCSI_REQUEST_SENSE = 0x03
SCSI_MEDIA_CHECK = 0x08
SCSI_INQUIRY = 0x12
SCSI_RESERVE_UNIT = 0x16
SCSI_RELEASE_UNIT = 0x17
SCSI_SCAN = 0x1B
SCSI_SET_WINDOW = 0x24
SCSI_READ = 0x28
SCSI_SEND = 0x2A
SCSI_OBJECT_POSITION = 0x31

#: OBJECT POSITION byte-1 actions (avision.h:613-615)
OP_REJECT_PAPER = 0x00
OP_LOAD_PAPER = 0x01
OP_GO_HOME = 0x02

# Datatype codes for READ (0x28) / SEND (0x2A) (avision.h:924-939)
DTC_READ_IMAGE_DATA = 0x00
DTC_GET_CALIBRATION_FORMAT = 0x60
DTC_WHITE_CALIB_GRAY = 0x61
DTC_WHITE_CALIB_COLOR = 0x62
DTC_DARK_CALIB = 0x66
DTC_ACCELERATION_TABLE = 0x6C
DTC_DOWNLOAD_GAMMA_TABLE = 0x81
DTC_DOWNLOAD_CALIB_DATA = 0x82
DTC_ATTACH_TRUNCATE_TAIL = 0x95
DTC_ATTACH_TRUNCATE_HEAD = 0x96
DTC_LIGHT_STATUS = 0xA0

#: Datatype qualifier for new-protocol devices (avision.c:5004-5008).
DATA_DQ_NEW_PROTOCOL = 0x0A0D

#: Window descriptor geometry base resolution (avision.c:6342-6355;
#: 1200 dpi for every ASIC except C5).
WINDOW_BASE_DPI = 1200
#: Inquiry geometry base resolution, AVISION_BASE_RES (avision.c:1610).
INQUIRY_BASE_DPI = 300

MM_PER_INCH = 25.4

# Shading constants (avision.c:1614-1624)
INVALID_WHITE_SHADING = 0x0000
DEFAULT_WHITE_SHADING = 0xFFF0
MAX_WHITE_SHADING = 0xFFFF
WHITE_MAP_RANGE = 0x4FFF
INVALID_DARK_SHADING = 0xFFFF
DEFAULT_DARK_SHADING = 0x0000

#: Fallback ranges for sheetfed scanners, inches
#: (A4_X_RANGE avision.c:1594, SHEETFEED_Y_RANGE avision.c:1600).
A4_X_RANGE_INCH = 8.5
SHEETFEED_Y_RANGE_INCH = 14.0

#: Light status values accepted by wait_4_light (avision.c:3387): 1 = "on",
#: 5 = "backlight on".
LIGHT_STATUS_OK = (1, 5)

# ---------------------------------------------------------------------------
# Endianness helpers
# ---------------------------------------------------------------------------


def be16(value: int) -> bytes:
    """Big-endian 16-bit, Avision ``set_double`` (avision.h:862-866)."""
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


def be24(value: int) -> bytes:
    """Big-endian 24-bit, Avision ``set_triple`` (avision.h:877-882)."""
    return bytes(((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF))


def be32(value: int) -> bytes:
    """Big-endian 32-bit, Avision ``set_quad`` (avision.h:894-900)."""
    return bytes(
        ((value >> 24) & 0xFF, (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)
    )


def get_be16(data: bytes, offset: int) -> int:
    """Big-endian 16-bit read, Avision ``get_double`` (avision.h:855-858)."""
    return (data[offset] << 8) | data[offset + 1]


def get_le16(data: bytes, offset: int) -> int:
    """Little-endian 16-bit read, ``get_double_le`` (avision.h:868-871)."""
    return data[offset] | (data[offset + 1] << 8)


def get_be32(data: bytes, offset: int) -> int:
    """Big-endian 32-bit read, Avision ``get_quad`` (avision.h:884-888)."""
    return (
        (data[offset] << 24)
        | (data[offset + 1] << 16)
        | (data[offset + 2] << 8)
        | data[offset + 3]
    )


def _bit(value: int, bit: int) -> bool:
    """BIT() macro (avision.h:851)."""
    return bool((value >> bit) & 1)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class AvisionError(Exception):
    """Base class for all driver errors."""


class SenseError(AvisionError):
    """A command finished with CHECK CONDITION and non-GOOD sense data.

    ``sense`` is ``None`` when the condition was detected without a sense
    exchange (e.g. MEDIA CHECK reporting an empty feeder).

    ``data`` carries any data-in payload that was fully read before the
    trailing status byte reported the condition.  The C transport reads the
    complete payload before the status/sense phase (avision.c:2622-2646),
    and reader_process still accounts those ``this_read`` bytes into the
    stripe when read_data returns SANE_STATUS_EOF (avision.c:7873-7885), so
    callers must not discard it.
    """

    def __init__(self, message: str, sense: "Optional[SenseData]" = None) -> None:
        super().__init__(message)
        self.sense = sense
        self.data: bytes = b""


class NoPaperError(SenseError):
    """ADF chute empty -- sense ASC/ASCQ 0x80/0x03 (avision.c:2308)."""


class EndOfPaperError(SenseError):
    """ADF paper end -- sense ASC/ASCQ 0x80/0x04 (avision.c:2309).

    This is the *normal* end-of-page signal during image READs.
    """


class PaperJamError(SenseError):
    """ADF paper jam -- ASC/ASCQ 0x80/0x01 or sense key 0x03 (avision.c:2306, 2226)."""


class CoverOpenError(SenseError):
    """ADF/scanner cover open -- ASC/ASCQ 0x80/0x02 etc. (avision.c:2307-2321)."""


class ScanCancelledError(SenseError):
    """Cancel button -- sense key 0x0b ABORTED COMMAND (avision.c:2243-2246)."""


# ---------------------------------------------------------------------------
# Sense decoding (sense_handler, avision.c:2178-2367)
# ---------------------------------------------------------------------------


class SenseStatus(Enum):
    """SANE-status equivalents produced by sense_handler (avision.c:2178-2367)."""

    GOOD = "good"
    IO_ERROR = "io-error"
    JAMMED = "jammed"
    COVER_OPEN = "cover-open"
    NO_DOCS = "no-docs"
    EOF = "eof"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SenseData:
    """Decoded 22-byte fixed-format SCSI sense block."""

    valid: bool  #: sense[0] bit 7 (avision.c:2210)
    error_code: int  #: sense[0] & 0x7f (avision.c:2186)
    sense_key: int  #: sense[2] & 0x0f (avision.c:2187)
    end_of_scan: bool  #: EOM, sense[2] bit 6 (avision.c:2251-2254)
    ili: bool  #: incorrect length indicator, sense[2] bit 5 (avision.c:2256-2259)
    asc: int  #: sense[12] (avision.c:2262)
    ascq: int  #: sense[13] (avision.c:2263)
    status: SenseStatus  #: mapped SANE-equivalent status
    raw: bytes


#: sense-key -> status map (avision.c:2215-2247); missing keys keep the
#: default SANE_STATUS_IO_ERROR (avision.c:2181).
_SENSE_KEY_STATUS = {
    0x00: SenseStatus.GOOD,  # "ok ?!?"
    0x03: SenseStatus.JAMMED,  # MEDIUM ERROR (mostly ADF)
    0x0B: SenseStatus.CANCELLED,  # ABORTED COMMAND (cancel button)
}

#: (ASC << 8 | ASCQ) -> status overrides (avision.c:2265-2332); only the
#: entries that change the status are listed -- the rest are text-only.
_ASC_ASCQ_STATUS = {
    0x8001: SenseStatus.JAMMED,  # ADF paper jam (avision.c:2306)
    0x8002: SenseStatus.COVER_OPEN,  # ADF cover open (avision.c:2307)
    0x8003: SenseStatus.NO_DOCS,  # ADF chute empty (avision.c:2308)
    0x8004: SenseStatus.EOF,  # ADF paper end (avision.c:2309)
    0x8007: SenseStatus.COVER_OPEN,  # flatbed cover open, OKI (avision.c:2312)
    0x8100: SenseStatus.COVER_OPEN,  # ADF/MFP front door open (avision.c:2320)
    0x8101: SenseStatus.COVER_OPEN,  # ADF holder cartridge open (avision.c:2321)
    0x8102: SenseStatus.NO_DOCS,  # ADF no film inside (avision.c:2322)
    0x8104: SenseStatus.NO_DOCS,  # ADF film end (avision.c:2324)
}


def decode_sense(sense: bytes) -> SenseData:
    """Decode a 22-byte REQUEST SENSE block per sense_handler (avision.c:2178-2367)."""
    if len(sense) < 14:
        # Too short to interpret -- treat like "sense not valid".
        sense = bytes(sense) + b"\x00" * (14 - len(sense))
        return SenseData(
            valid=False, error_code=sense[0] & 0x7F, sense_key=sense[2] & 0x0F,
            end_of_scan=False, ili=False, asc=sense[12], ascq=sense[13],
            status=SenseStatus.IO_ERROR, raw=bytes(sense),
        )

    valid = _bit(sense[0], 7)
    error_code = sense[0] & 0x7F
    sense_key = sense[2] & 0x0F
    end_of_scan = _bit(sense[2], 6)
    ili = _bit(sense[2], 5)
    asc = sense[12]
    ascq = sense[13]

    if not valid:
        # "sense not valid" -> default SANE_STATUS_IO_ERROR (avision.c:2210-2213)
        status = SenseStatus.IO_ERROR
    else:
        status = _SENSE_KEY_STATUS.get(sense_key, SenseStatus.IO_ERROR)
        status = _ASC_ASCQ_STATUS.get((asc << 8) | ascq, status)

    return SenseData(
        valid=valid, error_code=error_code, sense_key=sense_key,
        end_of_scan=end_of_scan, ili=ili, asc=asc, ascq=ascq,
        status=status, raw=bytes(sense),
    )


_SENSE_EXCEPTIONS = {
    SenseStatus.NO_DOCS: (NoPaperError, "No paper in the document feeder"),
    SenseStatus.EOF: (EndOfPaperError, "End of paper reached"),
    SenseStatus.JAMMED: (PaperJamError, "Paper jam in the document feeder"),
    SenseStatus.COVER_OPEN: (CoverOpenError, "Scanner cover is open"),
    SenseStatus.CANCELLED: (ScanCancelledError, "Scan cancelled at the device"),
    SenseStatus.IO_ERROR: (SenseError, "Scanner reported an error"),
}


def exception_for_sense(sense: SenseData) -> Optional[SenseError]:
    """Map decoded sense to an exception; ``None`` when the sense says GOOD.

    Mirrors avision_cmd returning sense_handler's result (avision.c:2703).
    """
    if sense.status is SenseStatus.GOOD:
        return None
    cls, message = _SENSE_EXCEPTIONS[sense.status]
    return cls(
        "%s (sense key 0x%02x, ASC/ASCQ 0x%02x/0x%02x)"
        % (message, sense.sense_key, sense.asc, sense.ascq),
        sense,
    )


# ---------------------------------------------------------------------------
# CDB builders (the transport zero-pads all CDBs to 10 bytes on the wire,
# avision.c:2544-2551)
# ---------------------------------------------------------------------------

#: INQUIRY, allocation length AVISION_INQUIRY_SIZE_V1 = 0x60 (avision.c:1605,
#: 3283-3309); command_header layout avision.h:631-637.
INQUIRY_SIZE = 0x60
INQUIRY_CDB = bytes((SCSI_INQUIRY, 0, 0, 0, INQUIRY_SIZE, 0))

#: TEST UNIT READY (avision.c:1715-1718).
TEST_UNIT_READY_CDB = bytes((SCSI_TEST_UNIT_READY, 0, 0, 0, 0, 0))

#: MEDIA CHECK, reads 1 byte; result bit0 = paper present (avision.c:6879-6897).
MEDIA_CHECK_CDB = bytes((SCSI_MEDIA_CHECK, 0, 0, 0, 0x01, 0))

#: RESERVE UNIT (avision.c:6852-6862).
RESERVE_UNIT_CDB = bytes((SCSI_RESERVE_UNIT, 0, 0, 0, 0, 0))

#: GET CALIBRATION FORMAT response length (avision.c:5284).
CALIB_FORMAT_SIZE = 32


def release_unit_cdb(release_type: int) -> bytes:
    """RELEASE UNIT (avision.c:6864-6875).

    ``release_type`` in byte 5: 0 = plain release (normal end of page),
    1 = release paper / fast feed-out (used on cancel, avision.c:7032-7035),
    2 = end job.
    """
    if release_type not in (0, 1, 2):
        raise ValueError("release_type must be 0, 1 or 2")
    return bytes((SCSI_RELEASE_UNIT, 0, 0, 0, 0, release_type))


def start_scan_cdb(quality: bool = True) -> bytes:
    """START SCAN 0x1B (start_scan, avision.c:6940-6965).

    Byte 4 (transferlen) is fixed 0x01 (avision.c:6951).  Byte 5 bit 7 =
    quality scan (avision.c:6958-6961; OPT_QSCAN default TRUE).  The preview
    bit 6 is never set for ASIC C7 devices (avision.c:6954-6956), so it is
    not exposed here.
    """
    bitset1 = 0x80 if quality else 0x00
    return bytes((SCSI_SCAN, 0, 0, 0, 0x01, bitset1))


def object_position_cdb(action: int) -> bytes:
    """OBJECT POSITION 0x31 (avision.c:6924-6938; actions avision.h:613-615).

    Provided for completeness: the SANE backend only ever issues GO_HOME for
    film scanners -- the AV210 family feeds/ejects implicitly via START SCAN
    and RELEASE UNIT.
    """
    if action not in (OP_REJECT_PAPER, OP_LOAD_PAPER, OP_GO_HOME):
        raise ValueError("invalid OBJECT POSITION action")
    return bytes((SCSI_OBJECT_POSITION, action, 0, 0, 0, 0, 0, 0, 0, 0))


def read_cdb(datatypecode: int, datatypequal: int, length: int) -> bytes:
    """READ 0x28 CDB (command_read, avision.h:647-656).

    Bytes: opc, bitset1=0, datatypecode, readtype=0, datatypequal (BE16),
    transferlen (BE24), control=0.
    """
    if not 0 <= length <= 0xFFFFFF:
        raise ValueError("transfer length out of 24-bit range")
    return bytes((SCSI_READ, 0, datatypecode, 0)) + be16(datatypequal) + be24(length) + b"\x00"


def send_cdb(datatypecode: int, datatypequal: int, length: int) -> bytes:
    """SEND 0x2A CDB (command_send, avision.h:667-676); layout as read_cdb."""
    if not 0 <= length <= 0xFFFFFF:
        raise ValueError("transfer length out of 24-bit range")
    return bytes((SCSI_SEND, 0, datatypecode, 0)) + be16(datatypequal) + be24(length) + b"\x00"


def constrain_read_size(size: int) -> int:
    """``read_constrains`` transform for ``AV_NO_64BYTE_ALIGN`` devices
    (avision.c:1626-1631): halve a 64-byte-aligned transfer length, then add
    2 if the result is still 64-byte-aligned.  Applied to every image-data
    READ (avision.c:7826) and calibration READ (avision.c:5370); callers gate
    on the model actually carrying the flag (AV210 pre-production, PID
    0x0A25, avision.c:253-254).
    """
    if size % 64 == 0:
        size //= 2
    if size % 64 == 0:
        size += 2
    return size


def media_check_present(result: bytes) -> bool:
    """MEDIA CHECK result: bit0 of the returned byte = paper present
    (media_check, avision.c:6879-6897)."""
    return bool(result and (result[0] & 0x01))


# ---------------------------------------------------------------------------
# INQUIRY parsing (attach(), avision.c:4369-5008)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScannerInfo:
    """Everything parsed from the 96-byte INQUIRY block that this driver uses.

    Field-by-field source citations are in :func:`parse_inquiry`.
    """

    vendor: str
    model: str
    fw_version: str
    has_adf: bool
    one_pass_color: bool
    is_not_flatbed: bool
    new_protocol: bool
    light_control: bool
    button_control: bool
    needs_software_colorpack: bool
    needs_calibration: bool
    needs_sw_gamma: bool
    keeps_gamma: bool
    keeps_window: bool
    line_difference: int
    color_boundary: int
    gray_boundary: int
    lineart_boundary: int
    channels_per_pixel: int
    bits_per_channel: int
    is_sheetfed: bool
    has_duplex: bool
    max_shading_target: int
    adf_max_x_dots: int
    adf_max_y_dots: int
    optical_res: int
    max_res: int
    asic_type: int
    buttons: int
    adf_bgr_order: bool
    needs_line_pack: bool
    tune_scan_length: bool
    background_raster: bool
    x_range_mm: float
    y_range_mm: float
    data_dq: int
    read_stripe_size: int


# ASIC types (avision.h:312-322)
ASIC_Cx = 0
ASIC_C1 = 1
ASIC_W1 = 2
ASIC_C2 = 3
ASIC_C5 = 5
ASIC_C6 = 6
ASIC_C7 = 7
ASIC_OA980 = 128
ASIC_OA982 = 129


def parse_inquiry(data: bytes) -> ScannerInfo:
    """Parse the 96-byte INQUIRY result exactly like attach() (avision.c:4369-5008).

    The AV210 family model-table entry carries ``AV_GRAY_MODES`` (avision.c:263),
    so inquiry byte [93] bit 5 (NO_SINGLE_CHANNEL_GRAY_MODE) is deliberately
    ignored (avision.c:4889-4890) and gray/lineart modes stay available.
    """
    if len(data) < INQUIRY_SIZE:
        raise ValueError("INQUIRY result must be %d bytes, got %d" % (INQUIRY_SIZE, len(data)))

    vendor = data[8:16].decode("ascii", "replace").strip()
    model = data[16:32].decode("ascii", "replace").strip()
    fw_version = data[32:36].decode("ascii", "replace").strip()

    # byte 36 (avision.c:4550-4566)
    has_adf = _bit(data[36], 7)
    one_pass_color = _bit(data[36], 6)
    is_not_flatbed = _bit(data[36], 3)

    # byte 39 bit 2 = NEW_PROTOCOL (avision.c:4738)
    new_protocol = _bit(data[39], 2)

    # byte 50, ESA1 (avision.c:4762-4789)
    light_control = _bit(data[50], 7)
    button_control = _bit(data[50], 6) or _bit(data[51], 2)  # avision.c:4789
    needs_software_colorpack = _bit(data[50], 5)  # avision.c:4778
    needs_calibration = _bit(data[50], 4)  # avision.c:4762
    needs_sw_gamma = _bit(data[50], 3)  # avision.c:4770
    keeps_gamma = _bit(data[50], 2)  # avision.c:4771
    keeps_window = _bit(data[50], 1)  # avision.c:4764

    # byte 91 = ASIC type (avision.c:4534, 4739)
    asic_type = data[91]

    # byte 53 = line difference with ASIC compensation (avision.c:4812-4825)
    line_difference = data[53]
    if asic_type in (ASIC_C2, ASIC_C5):
        line_difference //= 2
    elif asic_type == ASIC_C7:
        line_difference *= 2

    # bytes 54-59, boundaries default to 8 when 0 (avision.c:4796-4810)
    color_boundary = data[54] or 8
    gray_boundary = data[55] or 8
    lineart_boundary = data[57] or 8

    # byte 60 = channels per pixel; bit 6 (3 channels) is tested *before*
    # bit 7 (1 channel), so a device setting both is 3-channel
    # (avision.c:4860-4867)
    if _bit(data[60], 6):
        channels_per_pixel = 3
    elif _bit(data[60], 7):
        channels_per_pixel = 1
    else:
        channels_per_pixel = 3 if (data[36] >> 4) & 0x07 else 1

    # byte 61 = bits per channel, first match from 16 downward (avision.c:4869-4884)
    bits_per_channel = 8
    for bit, depth in ((1, 16), (2, 12), (3, 10), (4, 8), (5, 6), (6, 4), (7, 1)):
        if _bit(data[61], bit):
            bits_per_channel = depth
            break

    # byte 62 = scanner type; sheetfed = bit6 or bit4 (avision.c:4729-4732)
    is_sheetfed = _bit(data[62], 6) or _bit(data[62], 4)
    has_duplex = _bit(data[62], 2) or _bit(data[94], 5)  # avision.c:4745

    max_shading_target = get_be16(data, 75)  # avision.c:4794

    adf_max_x_dots = get_be16(data, 85)  # avision.c:4923-4926
    adf_max_y_dots = get_be16(data, 87)

    # resolutions, new protocol path (avision.c:4827-4830)
    if new_protocol:
        optical_res = get_be16(data, 89)
        max_res = get_be16(data, 44)
    else:
        optical_res = data[37] * 100
        max_res = data[38] * 100
    # fixups in the exact attach() order (avision.c:4836-4855):
    # 1. raise max res to the optical res, 2. default a zero optical res
    # (300 dpi for sheetfed), 3. default a zero max res to 1200 dpi.
    if optical_res > max_res:
        max_res = optical_res
    if optical_res == 0:
        optical_res = 300
    if max_res == 0:
        max_res = 1200

    buttons = data[92]  # avision.c:4895-4896
    adf_bgr_order = _bit(data[93], 6)  # avision.c:4783
    needs_line_pack = _bit(data[94], 6)  # avision.c:4780
    # attach/truncate scan-length tuning (avision.c:4929; no AV210-family
    # model carries AV_NO_TUNE_SCAN_LENGTH, avision.c:244-281)
    tune_scan_length = _bit(data[94], 2)
    background_raster = _bit(data[95], 2)  # avision.c:4933

    # ADF ranges in mm (avision.c:4899-4927); +0.1 mm on X for US paper sizes.
    if adf_max_x_dots and adf_max_y_dots:
        x_range_mm = adf_max_x_dots * MM_PER_INCH / INQUIRY_BASE_DPI + 0.1
        y_range_mm = adf_max_y_dots * MM_PER_INCH / INQUIRY_BASE_DPI
    else:
        # sheetfed fallback (avision.c:1594, 1600, 4943-...)
        x_range_mm = A4_X_RANGE_INCH * MM_PER_INCH
        y_range_mm = SHEETFEED_Y_RANGE_INCH * MM_PER_INCH

    # data_dq (avision.c:5004-5008)
    data_dq = DATA_DQ_NEW_PROTOCOL if new_protocol else 0

    # read_stripe_size (avision.c:4997-5002)
    if ASIC_C7 < asic_type < ASIC_OA980:
        read_stripe_size = 16
    elif asic_type >= ASIC_C5:
        read_stripe_size = 32
    else:
        read_stripe_size = 8

    return ScannerInfo(
        vendor=vendor, model=model, fw_version=fw_version,
        has_adf=has_adf, one_pass_color=one_pass_color,
        is_not_flatbed=is_not_flatbed, new_protocol=new_protocol,
        light_control=light_control, button_control=button_control,
        needs_software_colorpack=needs_software_colorpack,
        needs_calibration=needs_calibration, needs_sw_gamma=needs_sw_gamma,
        keeps_gamma=keeps_gamma, keeps_window=keeps_window,
        line_difference=line_difference, color_boundary=color_boundary,
        gray_boundary=gray_boundary, lineart_boundary=lineart_boundary,
        channels_per_pixel=channels_per_pixel, bits_per_channel=bits_per_channel,
        is_sheetfed=is_sheetfed, has_duplex=has_duplex,
        max_shading_target=max_shading_target,
        adf_max_x_dots=adf_max_x_dots, adf_max_y_dots=adf_max_y_dots,
        optical_res=optical_res, max_res=max_res, asic_type=asic_type,
        buttons=buttons, adf_bgr_order=adf_bgr_order,
        needs_line_pack=needs_line_pack, tune_scan_length=tune_scan_length,
        background_raster=background_raster,
        x_range_mm=x_range_mm, y_range_mm=y_range_mm,
        data_dq=data_dq, read_stripe_size=read_stripe_size,
    )


# ---------------------------------------------------------------------------
# Scan modes and parameter computation (compute_parameters, avision.c:2925-3279)
# ---------------------------------------------------------------------------


class ScanMode(Enum):
    """Supported scan modes and their window image-composition bytes
    (set_window, avision.c:6523-6568; mode table avision.c:5220-5252)."""

    COLOR = "color"  # AV_TRUECOLOR, image_comp 0x05, 8 bpc
    GRAY = "gray"  # AV_GRAYSCALE, image_comp 0x02, 8 bpc
    LINEART = "lineart"  # AV_THRESHOLDED, image_comp 0x00, 1 bpc


#: window byte 33 / byte 34 per mode (avision.c:6523-6568)
_MODE_IMAGE_COMP = {ScanMode.COLOR: 0x05, ScanMode.GRAY: 0x02, ScanMode.LINEART: 0x00}
_MODE_BPC = {ScanMode.COLOR: 8, ScanMode.GRAY: 8, ScanMode.LINEART: 1}


@dataclass(frozen=True)
class ScanParams:
    """Hardware scan parameters as computed by compute_parameters
    (avision.c:2925-3279)."""

    xres: int  #: hw X resolution, dpi
    yres: int  #: hw Y resolution, dpi
    tlx: int  #: top-left X in pixels at xres
    tly: int  #: top-left Y in pixels at yres
    pixels_per_line: int  #: boundary-rounded width in pixels
    lines: int  #: hw_lines (excluding the 2*line_difference extra lines)
    bytes_per_line: int
    line_difference: int  #: software colorpack line offset at yres
    mode: ScanMode


def compute_scan_params(
    info: ScannerInfo,
    resolution: int,
    mode: ScanMode,
    tl_x_mm: float = 0.0,
    tl_y_mm: float = 0.0,
    br_x_mm: Optional[float] = None,
    br_y_mm: Optional[float] = None,
) -> ScanParams:
    """Port of compute_parameters (avision.c:2925-3279) for the AV210 family.

    No ``AV_SOFT_SCALE`` -> hw res == user res (avision.c:3000-3003).  ASIC C7
    minimum is 75 dpi (avision.c:7097-7098).
    """
    if resolution < 75 or resolution > info.max_res:
        raise ValueError(
            "resolution %d out of range 75-%d dpi" % (resolution, info.max_res)
        )

    if br_x_mm is None:
        br_x_mm = info.x_range_mm
    if br_y_mm is None:
        br_y_mm = info.y_range_mm

    # mm -> pixels at hw resolution (avision.c:3010-3017)
    tlx = int(resolution * tl_x_mm / MM_PER_INCH)
    tly = int(resolution * tl_y_mm / MM_PER_INCH)
    brx = int(resolution * br_x_mm / MM_PER_INCH)
    bry = int(resolution * br_y_mm / MM_PER_INCH)
    if brx <= tlx or bry <= tly:
        raise ValueError("empty scan area")

    # software colorpack line difference (avision.c:3019-3045)
    if (
        mode is ScanMode.COLOR
        and info.needs_software_colorpack
        and info.line_difference != 0
    ):
        line_difference = info.line_difference * resolution // info.optical_res
        bry += 2 * line_difference
        # clamp when the *already extended* bry plus another 2*ld would
        # exceed the real scan boundary; the clamp target is
        # y_max - 2*line_difference, not y_max (avision.c:3029-3041)
        max_y = int(resolution * info.y_range_mm / MM_PER_INCH)
        if bry + 2 * line_difference > max_y:
            bry = max_y - 2 * line_difference
    else:
        line_difference = 0

    # pixel boundary rounding (avision.c:2905-2915, 3212-3213); lineart on
    # non-C5 ASICs is forced to a 32-pixel boundary.
    if mode is ScanMode.COLOR:
        boundary = info.color_boundary
    elif mode is ScanMode.GRAY:
        boundary = info.gray_boundary
    else:
        boundary = info.lineart_boundary if info.asic_type == ASIC_C5 else 32

    pixels_per_line = (brx - tlx) - ((brx - tlx) % boundary)
    lines = bry - tly - 2 * line_difference
    if pixels_per_line <= 0 or lines <= 0:
        raise ValueError("scan area too small after boundary rounding")

    # bytes per line (avision.c:3231-3274)
    if mode is ScanMode.LINEART:
        bytes_per_line = pixels_per_line // 8
    elif mode is ScanMode.GRAY:
        bytes_per_line = pixels_per_line
    else:
        bytes_per_line = pixels_per_line * 3

    return ScanParams(
        xres=resolution, yres=resolution, tlx=tlx, tly=tly,
        pixels_per_line=pixels_per_line, lines=lines,
        bytes_per_line=bytes_per_line, line_difference=line_difference,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# SET WINDOW (set_window, avision.c:6322-6588)
# ---------------------------------------------------------------------------

#: transferlen = 8 header + 42 descriptor + 20 avision paralen (avision.c:6364-6388)
WINDOW_TRANSFER_LEN = 70
WINDOW_DESC_LEN = 62  # 0x003E
WINDOW_PARALEN = 20  # 0x14

#: SET WINDOW CDB: opcode, 5 reserved, BE24 transferlen, control
SET_WINDOW_CDB = bytes((SCSI_SET_WINDOW, 0, 0, 0, 0, 0)) + be24(WINDOW_TRANSFER_LEN) + b"\x00"

# filter bits in window bitset1 (avision.h:620-626)
FILTER_NONE = 0x00
FILTER_RGB = 0x20
FILTER_GRAY = 0x30


def build_set_window(params: ScanParams, gray_filter: bool = False) -> Tuple[bytes, bytes]:
    """Build the SET WINDOW CDB + 70-byte payload (avision.c:6322-6588).

    Returns ``(cdb, payload)``.  All offsets follow command_set_window /
    command_set_window_window (avision.h:639-645, 731-806); values are those
    written for the AV210 family (ADF sheetfed, simplex, no multi-sheet).

    ``gray_filter``: models carrying ``AV_USE_GRAY_FILTER`` (AV210D2+,
    PID 0x1A35, avision.c:280-281) get AVISION_FILTER_GRAY instead of
    AVISION_FILTER_NONE for non-color modes (avision.c:6570-6580).
    """
    w = bytearray(WINDOW_TRANSFER_LEN)

    line_count = params.lines + 2 * params.line_difference

    w[6:8] = be16(WINDOW_DESC_LEN)  # header desclen
    w[8] = 0x00  # window ID, AV_WINID (avision.h:590)
    w[10:12] = be16(params.xres)
    w[12:14] = be16(params.yres)
    # geometry in 1200-dpi units (avision.c:6396-6403)
    w[14:18] = be32(params.tlx * WINDOW_BASE_DPI // params.xres)
    w[18:22] = be32(params.tly * WINDOW_BASE_DPI // params.yres)
    w[22:26] = be32(params.pixels_per_line * WINDOW_BASE_DPI // params.xres + 1)
    w[26:30] = be32(line_count * WINDOW_BASE_DPI // params.yres + 1)
    w[30] = 0x80  # brightness, fixed (avision.c:6517)
    w[31] = 0x80  # threshold (avision.c:6516)
    w[32] = 0x80  # contrast (avision.c:6518)
    w[33] = _MODE_IMAGE_COMP[params.mode]  # image composition (avision.c:6523-6568)
    w[34] = _MODE_BPC[params.mode]  # bits per channel
    # w[35:37] halftone pattern = 0
    w[37] = 0x03  # padding_and_bitset, fixed (avision.c:6510)
    # w[38:40] bit ordering, w[40] compression type, w[41] compression arg = 0
    # w[42:44] paper length = 0 (paper-length option default FALSE, avision.c:7483)
    w[48] = 0xFF  # vendor_specific (avision.c:6511)
    w[49] = WINDOW_PARALEN  # paralen (avision.c:6512)
    # bitset1 (avision.c:6429-6432, 6570-6580): bit7 ADF (always, sheetfed),
    # bit6 "use my line_width/line_count", bits5-3 filter, bits2-0 speed=0
    if params.mode is ScanMode.COLOR:
        filt = FILTER_RGB
    else:
        filt = FILTER_GRAY if gray_filter else FILTER_NONE
    w[50] = 0x80 | 0x40 | filt
    w[51] = 0xFF  # highlight (avision.c:6519)
    w[52] = 0x00  # shadow (avision.c:6520)
    w[53:55] = be16(params.bytes_per_line & 0xFFFF)  # line_width (avision.c:6413)
    w[55:57] = be16(line_count & 0xFFFF)  # line_count (avision.c:6414)
    # bitset2 (avision.c:6478-6487): bit4 quality-scan set (OPT_QSCAN default
    # TRUE, avision.c:7304); bit3 speed-cal clear (quality-cal default TRUE)
    w[57] = 0x10
    # w[58] ir_exposure, w[59:65] r/g/b exposure = 0 (film only)
    # w[65] bitset3 = 0x00: simplex, no duplex/multi-sheet bits (avision.c:6452-6460)
    # w[66] auto_focus = 0
    w[67] = (params.bytes_per_line >> 16) & 0xFF  # line_width_msb (avision.c:6417-6420)
    w[68] = (line_count >> 16) & 0xFF  # line_count_msb (avision.c:6421-6422)
    # w[69] background_lines = 0 (OPT_BACKGROUND default 0, avision.c:7261)

    return SET_WINDOW_CDB, bytes(w)


# ---------------------------------------------------------------------------
# Tune scan length (send_tune_scan_length, avision.c:5068-5177)
# ---------------------------------------------------------------------------

#: dpi base of the attach/truncate line counts -- 1200 as in the window
#: descriptor (no AV_OVERSCAN_OPTDPI on the AV210 family, avision.c:5093-5099)
TUNE_SCAN_LENGTH_DPI = 1200


def tune_scan_length_commands(
    overscan_top_mm: float = 0.0, overscan_bottom_mm: float = 0.0
) -> List[Tuple[bytes, bytes]]:
    """SEND pair tuning the ADF scan length (send_tune_scan_length,
    avision.c:5068-5177).

    dtc 0x96 (attach/truncate head) then 0x95 (tail), datatypequal 0x0001 =
    "attach" (avision.c:5124), 2-byte BE payload of 1200-dpi lines.  The C
    always sends both, even for 0, "as the scanner keeps it in RAM and
    previous runs could already have set something" (avision.c:5126-5127).
    No AV210-family model has ADF offset compensation (all model-table
    offsets are zero, avision.c:243-286, and adf_offset_compensation
    additionally requires an interlaced duplexer, avision.c:8827-8834).
    """
    top = int(TUNE_SCAN_LENGTH_DPI * overscan_top_mm / MM_PER_INCH)
    bottom = int(TUNE_SCAN_LENGTH_DPI * overscan_bottom_mm / MM_PER_INCH)
    return [
        (send_cdb(DTC_ATTACH_TRUNCATE_HEAD, 0x0001, 2), be16(top)),
        (send_cdb(DTC_ATTACH_TRUNCATE_TAIL, 0x0001, 2), be16(bottom)),
    ]


# ---------------------------------------------------------------------------
# Acceleration table (get_acceleration_info / send_acceleration_table,
# avision.c:6127-6320)
# ---------------------------------------------------------------------------

#: GET acceleration info: READ dtc 0x6C, 24 bytes (avision.c:6138-6146)
ACCEL_INFO_SIZE = 24


@dataclass(frozen=True)
class AccelerationInfo:
    """Decoded acceleration info block (avision.c:6158-6166, avision.h:851-862)."""

    total_steps: int  #: [0:2] BE16
    stable_steps: int  #: [2:4] BE16
    table_units: int  #: [4:8] BE32
    base_units: int  #: [8:12] BE32
    start_speed: int  #: [12:14] BE16
    target_speed: int  #: [14:16] BE16
    ability: int  #: [16]
    table_count: int  #: [17]


def parse_acceleration_info(data: bytes) -> AccelerationInfo:
    """Parse the 24-byte acceleration info result (avision.c:6158-6166)."""
    if len(data) < ACCEL_INFO_SIZE:
        raise ValueError(
            "acceleration info must be %d bytes, got %d" % (ACCEL_INFO_SIZE, len(data))
        )
    return AccelerationInfo(
        total_steps=get_be16(data, 0),
        stable_steps=get_be16(data, 2),
        table_units=get_be32(data, 4),
        base_units=get_be32(data, 8),
        start_speed=get_be16(data, 12),
        target_speed=get_be16(data, 14),
        ability=data[16],
        table_count=data[17],
    )


def build_acceleration_table(info: AccelerationInfo) -> bytes:
    """Construct one acceleration table payload (send_acceleration_table,
    avision.c:6202-6297).

    Binary search for the acceleration rate that yields exactly
    ``total_steps - stable_steps + 1`` ramp entries, fill the stable steps,
    pad the total step time up to a multiple of ``base_units``, then decrease
    every byte by one.  The C uses 32-bit floats; Python doubles converge to
    the same table via the exact-step-count exit condition.
    """
    if (
        info.target_speed > info.start_speed
        or info.target_speed == 0
        or info.total_steps <= info.stable_steps
    ):
        # avision.c:6190-6195
        raise ValueError("acceleration table does not look right")
    if info.ability != 0:
        # avision.c:6197-6200
        raise ValueError("unsupported acceleration table ability %d" % info.ability)

    total = info.total_steps
    # the C mallocs total_steps + 1000 slack for the ramp loop (avision.c:6203)
    table = bytearray(total + 1000)
    accel_steps = (total - info.stable_steps + 1) & 0xFFFF  # avision.c:6218

    # acceleration ramp (avision.c:6220-6249)
    low_lim = 0.001
    up_lim = 1.0
    while (up_lim - low_lim) > 0.0001:
        mid = (up_lim + low_lim) / 2  # accel rate
        now_count = info.start_speed
        now_count_f = float(now_count)
        i = 0
        table[i] = info.start_speed & 0xFF
        i += 1
        while now_count != info.target_speed:
            now_count_f = now_count_f - (now_count_f - info.target_speed) * mid
            now_count = int(now_count_f + 0.5) & 0xFFFF
            if i >= len(table):  # C relies on the +1000 slack here
                table.extend(bytes(1000))
            table[i] = now_count & 0xFF
            i += 1
        if i == accel_steps:
            break
        if i > accel_steps:
            low_lim = mid
        else:
            up_lim = mid

    # fill stable steps (avision.c:6251-6253)
    for i in range(accel_steps, total):
        table[i] = table[i - 1]

    # pad total step time up to a multiple of base_units (avision.c:6259-6288)
    table_total = sum(table[:total])
    if (table_total * info.table_units) % info.base_units == 0:
        add_count = 0
    else:
        add_count = (
            info.base_units - (table_total * info.table_units) % info.base_units
        ) // info.table_units
    if add_count > 255:  # avision.c:6276-6279
        add_count = 255
    i = 0
    while i < total - 1 and add_count > 0:
        temp_count = 255 - table[i]
        if temp_count > add_count:
            temp_count = add_count
        table[i] = (table[i] + temp_count) & 0xFF
        add_count -= temp_count
        i += 1

    # decrease all by one (avision.c:6294-6297)
    return bytes((v - 1) & 0xFF for v in table[:total])


# ---------------------------------------------------------------------------
# Gamma (send_gamma, avision.c:5886-6061)
# ---------------------------------------------------------------------------

#: raw = logical = 512 for the "default" ASIC branch incl. C7 (avision.c:5912-5935)
GAMMA_TABLE_RAW_SIZE = 512
GAMMA_TABLE_LOGICAL_SIZE = 512
_GAMMA_VALUES = GAMMA_TABLE_RAW_SIZE // 256  # = 2 (avision.c:5936)

#: default user gamma of the backend (sane_open, avision.c:8769-8778)
DEFAULT_GAMMA = 2.22


def default_gamma_table(gamma: float = DEFAULT_GAMMA) -> List[int]:
    """256-entry default gamma curve: ``(j/255)^(1/gamma) * 255``
    (avision.c:8769-8778)."""
    return [int(math.pow(j / 255.0, 1.0 / gamma) * 255.0) for j in range(256)]


def gamma_cdb(color: int) -> bytes:
    """SEND CDB for one gamma table download (avision.c:5962-6021).

    dtc 0x81, datatypequal = **color index** 0/1/2 (not data_dq), transferlen
    512.
    """
    if color not in (0, 1, 2):
        raise ValueError("color must be 0 (R), 1 (G) or 2 (B)")
    return send_cdb(DTC_DOWNLOAD_GAMMA_TABLE, color, GAMMA_TABLE_RAW_SIZE)


def build_gamma_payload(mode: ScanMode, gamma_table: Optional[List[int]] = None) -> bytes:
    """Build one 512-byte gamma payload (send_gamma, avision.c:5974-6047).

    For lineart the curve is inverted, ``v = 255 - v`` (avision.c:5909-5910).
    Linear interpolation into the doubled table (avision.c:6027-6030):
    ``data[2j] = v1``, ``data[2j+1] = (v1 + v2) / 2``.

    The same payload is sent three times (dq = 0, 1, 2) since the default
    gamma table is identical for all channels.
    """
    table = list(gamma_table) if gamma_table is not None else default_gamma_table()
    if len(table) != 256:
        raise ValueError("gamma table must have 256 entries")
    if mode is ScanMode.LINEART:
        table = [255 - v for v in table]

    data = bytearray(GAMMA_TABLE_RAW_SIZE)
    for j in range(256):
        v1 = table[j]
        v2 = table[j + 1] if j < 255 else v1
        # (v1 * (gv - k) + v2 * k) / gv with gv = 2, k = 0, 1 (avision.c:6027-6030)
        data[2 * j] = v1
        data[2 * j + 1] = (v1 + v2) // 2
    return bytes(data)


# ---------------------------------------------------------------------------
# Calibration (get_calib_format / normal_calibration, avision.c:5279-5835)
# ---------------------------------------------------------------------------


@dataclass
class CalibrationFormat:
    """GET CALIBRATION FORMAT response (get_calib_format, avision.c:5279-5337)."""

    pixel_per_line: int  #: [0-1] BE16
    bytes_per_channel: int  #: [2]
    lines: int  #: [3], already divided by 3 for 3-channel line interleave
    flags: int  #: [4]; 1 = calibration needed (avision.c:5734)
    ability1: int  #: [5]
    r_gain: int  #: [6]
    g_gain: int  #: [7]
    b_gain: int  #: [8]
    r_shading_target: int  #: [9-10] BE16
    g_shading_target: int  #: [11-12] BE16
    b_shading_target: int  #: [13-14] BE16
    r_dark_shading_target: int  #: [15-16] BE16
    g_dark_shading_target: int  #: [17-18] BE16
    b_dark_shading_target: int  #: [19-20] BE16
    channels: int  #: derived (avision.c:5327-5332)

    @property
    def needs_calibration(self) -> bool:
        """True iff the device claims calibration data must be produced
        (normal_calibration, avision.c:5733-5737)."""
        return self.flags == 1

    @property
    def has_dark_pass(self) -> bool:
        """ability1 bit 2 = separate dark-shading read (avision.c:5751)."""
        return _bit(self.ability1, 2)

    @property
    def one_command_upload(self) -> bool:
        """ability1 bit 0 clear -> all channels in one SEND (avision.c:5449-5452)."""
        return self.channels == 1 or not _bit(self.ability1, 0)

    @property
    def calib_data_size(self) -> int:
        """lines * bytes_per_channel * pixel_per_line * channels
        (avision.c:5741-5744)."""
        return self.lines * self.bytes_per_channel * self.pixel_per_line * self.channels


#: GET CALIBRATION FORMAT: READ dtc 0x60, dq 0x0A0D, 32 bytes (avision.c:5291-5295)
GET_CALIB_FORMAT_CDB = read_cdb(DTC_GET_CALIBRATION_FORMAT, DATA_DQ_NEW_PROTOCOL, CALIB_FORMAT_SIZE)


def parse_calib_format(data: bytes, color_mode: bool) -> CalibrationFormat:
    """Parse the 32-byte calibration format block (avision.c:5307-5332).

    ``color_mode`` selects the channel count: 3 channels (and raw line count
    divided by 3 -- line-interleaved R..RG..GB..B) when scanning color or when
    ability1 bit 3 forces color calibration even in gray mode.
    """
    if len(data) < CALIB_FORMAT_SIZE:
        raise ValueError("calibration format must be %d bytes" % CALIB_FORMAT_SIZE)

    lines = data[3]
    ability1 = data[5]
    if color_mode or _bit(ability1, 3):
        channels = 3
        lines //= 3  # line interleave (avision.c:5329)
    else:
        channels = 1

    return CalibrationFormat(
        pixel_per_line=get_be16(data, 0),
        bytes_per_channel=data[2],
        lines=lines,
        flags=data[4],
        ability1=ability1,
        r_gain=data[6], g_gain=data[7], b_gain=data[8],
        r_shading_target=get_be16(data, 9),
        g_shading_target=get_be16(data, 11),
        b_shading_target=get_be16(data, 13),
        r_dark_shading_target=get_be16(data, 15),
        g_dark_shading_target=get_be16(data, 17),
        b_dark_shading_target=get_be16(data, 19),
        channels=channels,
    )


def sort_and_average(fmt: CalibrationFormat, data: bytes) -> bytearray:
    """Per-pixel sort-and-average of raw calibration lines
    (sort_and_average, avision.c:5529-5578 + bubble_sort, avision.c:2716-2752).

    Input samples are 16-bit little-endian (or 8-bit scaled by 0xFFFF/255 when
    bytes_per_channel == 1, avision.c:5563-5566).  bubble_sort discards the
    lowest third of the samples and averages the rest (avision.c:2722-2749).
    Output is one BE16 word per pixel element, as stored by the C code.
    """
    epl = fmt.pixel_per_line * fmt.channels
    stride = fmt.bytes_per_channel * epl
    avg = bytearray(epl * 2)

    for i in range(epl):
        base = i * fmt.bytes_per_channel
        samples = []
        for line in range(fmt.lines):
            off = base + line * stride
            if fmt.bytes_per_channel == 1:
                samples.append(0xFFFF * data[off] // 255)
            else:
                samples.append(get_le16(data, off))
        samples.sort()
        rest = samples[len(samples) // 3:]  # drop lowest third (avision.c:2722)
        value = int(sum(rest) / len(rest)) if rest else 0
        avg[i * 2: i * 2 + 2] = be16(value)
    return avg


def compute_dark_shading(fmt: CalibrationFormat, avg: bytearray, max_shading_target: int) -> None:
    """In-place dark shading computation (compute_dark_shading_data,
    avision.c:5582-5624).

    Note the C reads the (big-endian-stored) averages with ``get_double_le``
    (avision.c:5616) -- this byte swap is reproduced verbatim, as the same
    convention is used again when the dark data is merged during upload.
    """
    map_value = DEFAULT_DARK_SHADING
    if max_shading_target != INVALID_DARK_SHADING:
        map_value = (max_shading_target << 8) & 0xFFFF  # avision.c:5592-5593

    targets = [fmt.r_dark_shading_target, fmt.g_dark_shading_target, fmt.b_dark_shading_target]
    for i in range(fmt.channels):
        if targets[i] == INVALID_DARK_SHADING:
            targets[i] = map_value
    if fmt.channels == 1:
        targets = [targets[1]] * 3  # "set to green" (avision.c:5604-5607)

    epl = fmt.pixel_per_line * fmt.channels
    for i in range(epl):
        tmp = get_le16(avg, i * 2)  # sic -- get_double_le (avision.c:5616)
        t = targets[i % 3]
        value = tmp - t if tmp > t else 0
        avg[i * 2: i * 2 + 2] = be16(value)  # set_double, BE (avision.c:5618-5621)


def subtract_dark_average(white: bytearray, dark: bytearray, fmt: CalibrationFormat) -> None:
    """Byte-wise decrease of the white averages by the dark averages
    (normal_calibration, avision.c:5817-5826).

    The C subtracts uint8_t-wise over ``elements_per_line`` *bytes* -- this
    quirk is reproduced exactly (modulo-256 per byte, first epl bytes only).
    """
    epl = fmt.pixel_per_line * fmt.channels
    for i in range(epl):
        white[i] = (white[i] - dark[i]) & 0xFF


def compute_white_shading(fmt: CalibrationFormat, avg: bytearray, max_shading_target: int) -> None:
    """In-place white shading -> gain-word computation
    (compute_white_shading_data, avision.c:5626-5710).  Output words are
    little-endian (avision.c:5705-5706)."""
    inquiry_mst = DEFAULT_WHITE_SHADING
    if max_shading_target != INVALID_WHITE_SHADING:
        inquiry_mst = (max_shading_target << 4) & 0xFFFF  # avision.c:5642-5643

    mst = [fmt.r_shading_target, fmt.g_shading_target, fmt.b_shading_target]
    for i in range(3):
        if mst[i] == INVALID_WHITE_SHADING:
            mst[i] = inquiry_mst  # avision.c:5650-5654
        elif mst[i] < 0x110:
            # some firmware returns the bytes swapped (avision.c:5656-5663)
            mst[i] = ((mst[i] & 0xFF) << 8) | (mst[i] >> 8)
        if mst[i] < DEFAULT_WHITE_SHADING // 2:
            mst[i] = DEFAULT_WHITE_SHADING  # avision.c:5664-5668
    if fmt.channels == 1:
        mst = [mst[1]] * 3  # "set to green" (avision.c:5676-5679)

    epl = fmt.pixel_per_line * fmt.channels
    for i in range(epl):
        tmp = get_be16(avg, i * 2)
        if tmp == INVALID_WHITE_SHADING:
            tmp = DEFAULT_WHITE_SHADING  # avision.c:5688-5691
        result = int(mst[i % 3] * WHITE_MAP_RANGE / (tmp + 0.5))  # avision.c:5693
        if result > MAX_WHITE_SHADING:
            result = WHITE_MAP_RANGE  # over-amplification clip (avision.c:5696-5698)
        avg[i * 2] = result & 0xFF  # set_double_le (avision.c:5706)
        avg[i * 2 + 1] = (result >> 8) & 0xFF


def build_calibration_upload(
    fmt: CalibrationFormat, white: bytearray, dark: Optional[bytearray]
) -> List[Tuple[bytes, bytes]]:
    """Build the SEND command(s) uploading the gain words (set_calib_data,
    avision.c:5395-5516).

    Returns a list of ``(cdb, payload)`` pairs.  dtc 0x82; datatypequal 0x0012
    for color, 0x0011 for gray (no ``AV_GRAY_CALIB_BLUE`` on the AV210 family,
    avision.c:5416-5424); or 0/1/2 for the per-channel variant.
    """
    epl = fmt.pixel_per_line * fmt.channels
    white = bytearray(white)

    # merge dark data into the low 6 bits of each (LE) white word (avision.c:5431-5445)
    if fmt.has_dark_pass and dark is not None:
        for i in range(epl):
            value = get_le16(white, i * 2) & 0xFFC0
            value |= (get_le16(dark, i * 2) >> 10) & 0x3F
            white[i * 2] = value & 0xFF
            white[i * 2 + 1] = (value >> 8) & 0xFF

    if fmt.one_command_upload:
        dq = 0x12 if fmt.channels > 1 else 0x11  # avision.c:5416-5423
        return [(send_cdb(DTC_DOWNLOAD_CALIB_DATA, dq, epl * 2), bytes(white))]

    # per-channel upload, dq = channel index (avision.c:5469-5511)
    commands: List[Tuple[bytes, bytes]] = []
    for channel in range(3):
        payload = bytearray(fmt.pixel_per_line * 2)
        for i in range(fmt.pixel_per_line):
            src = (i * 3 + channel) * 2
            payload[i * 2: i * 2 + 2] = white[src: src + 2]  # whole-word copy
        commands.append(
            (send_cdb(DTC_DOWNLOAD_CALIB_DATA, channel, len(payload)), bytes(payload))
        )
    return commands
