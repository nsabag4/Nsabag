"""Hardware-free unit tests for the AV210 userspace driver.

Golden byte values are taken directly from the byte-level specification
extracted from the SANE avision backend (line references in the modules).
"""

from __future__ import annotations

from typing import List

import pytest

from av210 import protocol
from av210.protocol import (
    EndOfPaperError,
    NoPaperError,
    PaperJamError,
    ScanCancelledError,
    ScanMode,
    ScanParams,
    SenseStatus,
)
from av210.transport import (
    AV210_MODEL_FEATURES,
    REQUEST_SENSE_CDB,
    AvisionTransport,
    DeviceBusyError,
    ModelFeatures,
    StatusPipe,
    TransportError,
)

# ---------------------------------------------------------------------------
# CDB builders -- exact wire bytes
# ---------------------------------------------------------------------------


def test_inquiry_cdb() -> None:
    # opcode 0x12, allocation length 0x60 at byte 4 (avision.c:3283-3309)
    assert protocol.INQUIRY_CDB == bytes.fromhex("120000006000")


def test_test_unit_ready_cdb() -> None:
    # avision.c:1715-1718
    assert protocol.TEST_UNIT_READY_CDB == bytes.fromhex("000000000000")


def test_media_check_cdb() -> None:
    # avision.c:6879-6897
    assert protocol.MEDIA_CHECK_CDB == bytes.fromhex("080000000100")


def test_reserve_release_cdbs() -> None:
    # avision.c:6852-6875
    assert protocol.RESERVE_UNIT_CDB == bytes.fromhex("160000000000")
    assert protocol.release_unit_cdb(0) == bytes.fromhex("170000000000")
    assert protocol.release_unit_cdb(1) == bytes.fromhex("170000000001")
    with pytest.raises(ValueError):
        protocol.release_unit_cdb(3)


def test_start_scan_cdb() -> None:
    # byte4 = 0x01 fixed, byte5 bit7 = quality (avision.c:6940-6965)
    assert protocol.start_scan_cdb(quality=True) == bytes.fromhex("1b0000000180")
    assert protocol.start_scan_cdb(quality=False) == bytes.fromhex("1b0000000100")


def test_read_image_data_cdb() -> None:
    # READ 0x28, dtc 0x00, dq 0x0A0D, BE24 transferlen (avision.c:7040-7057)
    cdb = protocol.read_cdb(protocol.DTC_READ_IMAGE_DATA, 0x0A0D, 244224)
    assert cdb == bytes.fromhex("280000000a0d03ba0000")


def test_get_calib_format_cdb() -> None:
    # READ dtc 0x60, dq 0x0A0D, 32 bytes (avision.c:5291-5295)
    assert protocol.GET_CALIB_FORMAT_CDB == bytes.fromhex("280060000a0d00002000")


def test_gamma_cdb() -> None:
    # SEND dtc 0x81, dq = color index, transferlen 512 (avision.c:5962-6021)
    assert protocol.gamma_cdb(0) == bytes.fromhex("2a008100000000020000")
    assert protocol.gamma_cdb(1) == bytes.fromhex("2a008100000100020000")
    assert protocol.gamma_cdb(2) == bytes.fromhex("2a008100000200020000")
    with pytest.raises(ValueError):
        protocol.gamma_cdb(3)


def test_object_position_cdb() -> None:
    # opcode 0x31, action in byte 1 (avision.c:6924-6938)
    assert protocol.object_position_cdb(protocol.OP_GO_HOME) == bytes.fromhex(
        "31020000000000000000"
    )


def test_request_sense_cdb() -> None:
    # exact wire bytes (avision.c:2656-2681)
    assert REQUEST_SENSE_CDB == bytes.fromhex("03000000160000000000")


def test_set_window_cdb() -> None:
    # transferlen 70 = 0x46 in BE24 at bytes 6-8 (avision.c:6364-6388)
    assert protocol.SET_WINDOW_CDB == bytes.fromhex("24000000000000004600")


# ---------------------------------------------------------------------------
# SET WINDOW golden payload -- 300 dpi color, 8.5" x 11.8" (spec section 5.4)
# ---------------------------------------------------------------------------

GOLDEN_WINDOW = bytes.fromhex(
    "000000000000"  # [0:6]   header reserved
    "003e"          # [6:8]   desclen 62
    "00" "00"       # [8]     window id, [9] reserved
    "012c" "012c"   # [10:14] X/Y resolution 300
    "00000000"      # [14:18] upper-left X
    "00000000"      # [18:22] upper-left Y
    "000027c1"      # [22:26] width  = 2544*1200/300 + 1 = 10177
    "00003751"      # [26:30] length = 3540*1200/300 + 1 = 14161
    "80" "80" "80"  # [30:33] brightness, threshold, contrast
    "05" "08"       # [33]    image comp color, [34] 8 bpc
    "0000"          # [35:37] halftone pattern
    "03"            # [37]    padding_and_bitset
    "0000" "00" "00"  # [38:40] bit ordering, [40] compr type, [41] compr arg
    "0000"          # [42:44] paper length
    "00000000"      # [44:48] reserved
    "ff" "14"       # [48]    vendor specific, [49] paralen 20
    "e0"            # [50]    bitset1: ADF | use-line-width | RGB filter
    "ff" "00"       # [51]    highlight, [52] shadow
    "1dd0"          # [53:55] line_width 7632
    "0dd4"          # [55:57] line_count 3540
    "10"            # [57]    bitset2: quality scan
    "00"            # [58]    ir exposure
    "0000" "0000" "0000"  # [59:65] r/g/b exposure
    "00" "00"       # [65]    bitset3 simplex, [66] auto focus
    "00" "00"       # [67]    line_width_msb, [68] line_count_msb
    "00"            # [69]    background lines
)


def _make_params_300dpi_color() -> ScanParams:
    return ScanParams(
        xres=300, yres=300, tlx=0, tly=0, pixels_per_line=2544, lines=3540,
        bytes_per_line=7632, line_difference=0, mode=ScanMode.COLOR,
    )


def test_window_payload_golden() -> None:
    assert len(GOLDEN_WINDOW) == 70
    cdb, payload = protocol.build_set_window(_make_params_300dpi_color())
    assert cdb == protocol.SET_WINDOW_CDB
    assert payload == GOLDEN_WINDOW


def test_window_payload_gray_lineart_bytes() -> None:
    params = ScanParams(
        xres=300, yres=300, tlx=0, tly=0, pixels_per_line=2544, lines=3540,
        bytes_per_line=2544, line_difference=0, mode=ScanMode.GRAY,
    )
    _, payload = protocol.build_set_window(params)
    assert payload[33] == 0x02 and payload[34] == 8  # gray composition
    assert payload[50] == 0xC0  # ADF + use-line-width, FILTER_NONE

    params = ScanParams(
        xres=300, yres=300, tlx=0, tly=0, pixels_per_line=2528, lines=3540,
        bytes_per_line=316, line_difference=0, mode=ScanMode.LINEART,
    )
    _, payload = protocol.build_set_window(params)
    assert payload[33] == 0x00 and payload[34] == 1  # lineart composition


def test_window_payload_gray_filter() -> None:
    # AV_USE_GRAY_FILTER models (AV210D2+, PID 0x1A35, avision.c:280-281)
    # select AVISION_FILTER_GRAY 0x30 for non-color modes
    # (avision.c:6574-6577, avision.h:626)
    params = ScanParams(
        xres=300, yres=300, tlx=0, tly=0, pixels_per_line=2544, lines=3540,
        bytes_per_line=2544, line_difference=0, mode=ScanMode.GRAY,
    )
    _, payload = protocol.build_set_window(params, gray_filter=True)
    assert payload[50] == 0xC0 | 0x30
    # color scans keep the RGB filter regardless (avision.c:6570-6572)
    _, payload = protocol.build_set_window(_make_params_300dpi_color(), gray_filter=True)
    assert payload[50] == 0xE0


def test_compute_scan_params_matches_worked_example() -> None:
    info = _make_inquiry_info()
    params = protocol.compute_scan_params(
        info, 300, ScanMode.COLOR,
        br_x_mm=8.5 * 25.4, br_y_mm=11.8 * 25.4,
    )
    assert params.pixels_per_line == 2544  # 2550 rounded down to boundary 8
    assert params.lines == 3540
    assert params.bytes_per_line == 7632
    assert params.line_difference == 0  # needs_software_colorpack unset


def test_compute_scan_params_line_difference() -> None:
    info = _make_inquiry_info(colorpack=True)
    # inquiry [53] = 2, ASIC C7 doubles it (avision.c:4820-4822) -> 4 at
    # optical resolution 600; at 300 dpi: 4 * 300 // 600 = 2
    params = protocol.compute_scan_params(
        info, 300, ScanMode.COLOR, br_x_mm=8.5 * 25.4, br_y_mm=11.8 * 25.4
    )
    assert params.line_difference == 2
    # gray scans never use the colorpack offset (avision.c:3019-3045)
    params = protocol.compute_scan_params(
        info, 300, ScanMode.GRAY, br_x_mm=8.5 * 25.4, br_y_mm=11.8 * 25.4
    )
    assert params.line_difference == 0


def test_compute_scan_params_bry_clamped() -> None:
    # For a full-height color scan on a colorpack device the C first extends
    # bry by 2*ld, then clamps to y_max - 2*ld when bry + 2*ld would exceed
    # y_max (avision.c:3029-3041) -- NOT a plain min(bry + 2*ld, y_max).
    info = _make_inquiry_info(colorpack=True)
    params = protocol.compute_scan_params(info, 300, ScanMode.COLOR)
    ld = params.line_difference
    assert ld == 2
    max_y = int(300 * info.y_range_mm / 25.4)
    # bry was clamped to max_y - 2*ld; lines = bry - tly - 2*ld
    assert params.lines == max_y - 4 * ld


def test_compute_scan_params_rejects_bad_resolution() -> None:
    info = _make_inquiry_info()
    with pytest.raises(ValueError):
        protocol.compute_scan_params(info, 50, ScanMode.COLOR)
    with pytest.raises(ValueError):
        protocol.compute_scan_params(info, 1200, ScanMode.COLOR)


# ---------------------------------------------------------------------------
# INQUIRY parser
# ---------------------------------------------------------------------------


def _make_inquiry_blob(colorpack: bool = False) -> bytes:
    data = bytearray(96)
    data[8:16] = b"AVision "
    data[16:32] = b"AV210C2 ".ljust(16)
    data[32:36] = b"1.00"
    data[36] = 0xC8  # ADF | 1-pass color | not flatbed
    data[39] = 0x07  # new protocol (bit2) + Avision brand (bits 0-1)
    data[44:46] = protocol.be16(600)  # X res color -> max res (avision.c:4829)
    data[50] = 0x94  # light control | SW calib | keeps gamma
    if colorpack:
        data[50] |= 0x20  # NEED_SW_COLORPACK (avision.c:4778)
    data[51] = 0x04  # HAS_PUSH_BUTTON -> button_control (avision.c:4789)
    data[53] = 2  # raw line difference; C7 doubles it -> 4
    data[60] = 0x40  # 3 channels per pixel
    data[61] = 0x10  # 8 bits per channel
    data[62] = 0x40  # roller (ADF) -> sheetfed
    data[85:87] = protocol.be16(2550)  # ADF max X, dots @300 dpi
    data[87:89] = protocol.be16(4200)  # ADF max Y (14 in)
    data[89:91] = protocol.be16(600)  # optical res, extended mode
    data[91] = protocol.ASIC_C7
    data[92] = 2  # buttons
    data[93] = 0x20  # NO_SINGLE_CHANNEL_GRAY -- must be IGNORED (AV_GRAY_MODES)
    return bytes(data)


def _make_inquiry_info(colorpack: bool = False) -> protocol.ScannerInfo:
    return protocol.parse_inquiry(_make_inquiry_blob(colorpack=colorpack))


def test_parse_inquiry_fields() -> None:
    info = _make_inquiry_info()
    assert info.vendor == "AVision"
    assert info.model == "AV210C2"
    assert info.fw_version == "1.00"
    assert info.has_adf and info.one_pass_color and info.is_not_flatbed
    assert info.new_protocol
    assert info.light_control
    assert info.button_control
    assert info.needs_calibration
    assert info.keeps_gamma and not info.keeps_window
    assert not info.needs_software_colorpack
    assert info.line_difference == 4  # 2 doubled for ASIC C7 (avision.c:4820-4822)
    assert info.color_boundary == 8 and info.gray_boundary == 8  # defaults
    assert info.channels_per_pixel == 3
    assert info.bits_per_channel == 8
    assert info.is_sheetfed and not info.has_duplex
    assert info.optical_res == 600 and info.max_res == 600
    assert info.asic_type == protocol.ASIC_C7
    assert info.buttons == 2
    assert info.data_dq == 0x0A0D  # new protocol (avision.c:5004-5008)
    assert info.read_stripe_size == 32  # ASIC C7 >= C5 (avision.c:4997-5002)
    # ADF range: 2550 dots @300 dpi + 0.1 mm (avision.c:4923-4924)
    assert info.x_range_mm == pytest.approx(2550 * 25.4 / 300 + 0.1)
    assert info.y_range_mm == pytest.approx(4200 * 25.4 / 300)


def test_parse_inquiry_rejects_short_blob() -> None:
    with pytest.raises(ValueError):
        protocol.parse_inquiry(b"\x00" * 32)


def test_parse_inquiry_resolution_defaults() -> None:
    # attach() order (avision.c:4836-4855): max raised to optical first,
    # then optical 0 -> 300 (sheetfed), then max 0 -> 1200
    data = bytearray(_make_inquiry_blob())
    data[44:46] = protocol.be16(0)  # max res 0
    data[89:91] = protocol.be16(0)  # optical res 0
    info = protocol.parse_inquiry(bytes(data))
    assert info.optical_res == 300
    assert info.max_res == 1200


def test_parse_inquiry_channels_both_bits_is_three() -> None:
    # bit 6 (3 channels) is tested before bit 7 (1 channel)
    # (avision.c:4860-4863)
    data = bytearray(_make_inquiry_blob())
    data[60] = 0xC0
    assert protocol.parse_inquiry(bytes(data)).channels_per_pixel == 3


def test_parse_inquiry_tune_scan_length_bit() -> None:
    # inquiry [94] bit 2 (avision.c:4929)
    assert not _make_inquiry_info().tune_scan_length
    data = bytearray(_make_inquiry_blob())
    data[94] = 0x04
    assert protocol.parse_inquiry(bytes(data)).tune_scan_length


# ---------------------------------------------------------------------------
# Sense decoder
# ---------------------------------------------------------------------------


def _sense(key: int = 0, asc: int = 0, ascq: int = 0, valid: bool = True) -> bytes:
    sense = bytearray(22)
    sense[0] = 0x70 | (0x80 if valid else 0x00)
    sense[2] = key & 0x0F
    sense[7] = 14  # additional sense length
    sense[12] = asc
    sense[13] = ascq
    return bytes(sense)


def test_sense_good() -> None:
    decoded = protocol.decode_sense(_sense())
    assert decoded.status is SenseStatus.GOOD
    assert protocol.exception_for_sense(decoded) is None


def test_sense_not_valid_is_io_error() -> None:
    # validity gate (avision.c:2210-2213)
    decoded = protocol.decode_sense(_sense(valid=False))
    assert decoded.status is SenseStatus.IO_ERROR


def test_sense_no_paper() -> None:
    # ASC/ASCQ 0x80/0x03 "ADF chute empty" -> NO_DOCS (avision.c:2308)
    decoded = protocol.decode_sense(_sense(key=0x02, asc=0x80, ascq=0x03))
    assert decoded.status is SenseStatus.NO_DOCS
    assert isinstance(protocol.exception_for_sense(decoded), NoPaperError)


def test_sense_paper_end_is_eof() -> None:
    # ASC/ASCQ 0x80/0x04 "ADF paper end" -> EOF (avision.c:2309)
    decoded = protocol.decode_sense(_sense(key=0x02, asc=0x80, ascq=0x04))
    assert decoded.status is SenseStatus.EOF
    assert isinstance(protocol.exception_for_sense(decoded), EndOfPaperError)


def test_sense_medium_error_is_jam() -> None:
    # sense key 0x03 MEDIUM ERROR (avision.c:2226)
    decoded = protocol.decode_sense(_sense(key=0x03))
    assert decoded.status is SenseStatus.JAMMED
    assert isinstance(protocol.exception_for_sense(decoded), PaperJamError)


def test_sense_aborted_is_cancelled() -> None:
    # sense key 0x0b ABORTED COMMAND (avision.c:2243-2246)
    decoded = protocol.decode_sense(_sense(key=0x0B))
    assert decoded.status is SenseStatus.CANCELLED
    assert isinstance(protocol.exception_for_sense(decoded), ScanCancelledError)


# ---------------------------------------------------------------------------
# Gamma
# ---------------------------------------------------------------------------


def test_gamma_payload_golden() -> None:
    payload = protocol.build_gamma_payload(ScanMode.COLOR)
    assert len(payload) == 512
    # interpolated doubled table (avision.c:6027-6030) of the 2.22 curve
    # (avision.c:8769-8778)
    assert payload[:6] == bytes((0, 10, 21, 24, 28, 31))
    assert payload[508:] == bytes((254, 254, 255, 255))


def test_gamma_payload_lineart_inverted() -> None:
    # lineart tables are inverted, v = 255 - v (avision.c:5909-5910)
    payload = protocol.build_gamma_payload(ScanMode.LINEART)
    assert payload[0] == 255
    assert payload[510] == 0 and payload[511] == 0


# ---------------------------------------------------------------------------
# Calibration format + upload
# ---------------------------------------------------------------------------


def _calib_format_blob() -> bytes:
    data = bytearray(32)
    data[0:2] = protocol.be16(2560)  # pixels per line
    data[2] = 2  # bytes per channel
    data[3] = 9  # raw lines (color: /3 -> 3)
    data[4] = 1  # flags: calibration needed
    data[5] = 0x00  # ability1: one-command upload, no dark pass
    data[9:11] = protocol.be16(0xE000)  # r white target
    data[11:13] = protocol.be16(0xE100)  # g
    data[13:15] = protocol.be16(0xE200)  # b
    data[15:17] = protocol.be16(0xFFFF)  # dark targets invalid
    data[17:19] = protocol.be16(0xFFFF)
    data[19:21] = protocol.be16(0xFFFF)
    return bytes(data)


def test_parse_calib_format_color() -> None:
    fmt = protocol.parse_calib_format(_calib_format_blob(), color_mode=True)
    assert fmt.pixel_per_line == 2560
    assert fmt.bytes_per_channel == 2
    assert fmt.channels == 3
    assert fmt.lines == 3  # raw 9 / 3, line interleave (avision.c:5329)
    assert fmt.needs_calibration
    assert not fmt.has_dark_pass
    assert fmt.one_command_upload
    assert fmt.calib_data_size == 3 * 2 * 2560 * 3


def test_build_calibration_upload_one_command() -> None:
    fmt = protocol.parse_calib_format(_calib_format_blob(), color_mode=True)
    white = bytearray(fmt.pixel_per_line * fmt.channels * 2)
    commands = protocol.build_calibration_upload(fmt, white, None)
    assert len(commands) == 1
    cdb, payload = commands[0]
    # SEND dtc 0x82, dq 0x0012 for color (avision.c:5416-5424), BE24 length
    assert cdb == protocol.send_cdb(0x82, 0x12, len(payload))
    assert cdb[:6] == bytes.fromhex("2a0082000012")
    assert len(payload) == 2560 * 3 * 2


def test_sort_and_average() -> None:
    fmt = protocol.parse_calib_format(_calib_format_blob(), color_mode=True)
    # 1 pixel wide to keep the numbers small
    fmt.pixel_per_line = 1
    fmt.lines = 3
    # 3 lines x 3 channels x LE16: pixel 0 gets samples 100, 200, 300 ->
    # lowest third (1 sample) dropped, average of (200, 300) = 250
    def line(v: int) -> bytes:
        return b"".join(bytes((s & 0xFF, s >> 8)) for s in (v, 0x2000, 0x3000))
    raw = line(100) + line(200) + line(300)
    avg = protocol.sort_and_average(fmt, raw)
    assert protocol.get_be16(avg, 0) == 250
    assert protocol.get_be16(avg, 2) == 0x2000  # constant samples average to themselves
    assert protocol.get_be16(avg, 4) == 0x3000


# ---------------------------------------------------------------------------
# Tune scan length, acceleration table, read constraints, model features
# ---------------------------------------------------------------------------


def test_tune_scan_length_commands_golden() -> None:
    # SEND dtc 0x96 (head) then 0x95 (tail), dq 0x0001 = attach, 2-byte BE
    # payload; always sent, even for zero (avision.c:5085-5177)
    commands = protocol.tune_scan_length_commands()
    assert len(commands) == 2
    (head_cdb, head_payload), (tail_cdb, tail_payload) = commands
    assert head_cdb == bytes.fromhex("2a009600000100000200")
    assert tail_cdb == bytes.fromhex("2a009500000100000200")
    assert head_payload == b"\x00\x00" and tail_payload == b"\x00\x00"


def test_constrain_read_size() -> None:
    # read_constrains: halve 64-aligned sizes, +2 if still aligned
    # (avision.c:1626-1631)
    assert protocol.constrain_read_size(64) == 32
    assert protocol.constrain_read_size(128) == 66  # 64 is still aligned -> +2
    assert protocol.constrain_read_size(4096) == 2050
    assert protocol.constrain_read_size(100) == 100  # untouched
    assert protocol.constrain_read_size(7632) == 7632


def _accel_info_blob() -> bytes:
    data = bytearray(24)
    data[0:2] = protocol.be16(10)  # total steps
    data[2:4] = protocol.be16(5)  # stable steps
    data[4:8] = protocol.be32(1)  # table units
    data[8:12] = protocol.be32(1)  # base units
    data[12:14] = protocol.be16(8)  # start speed
    data[14:16] = protocol.be16(2)  # target speed
    data[16] = 0  # ability
    data[17] = 1  # table count
    return bytes(data)


def test_parse_acceleration_info() -> None:
    info = protocol.parse_acceleration_info(_accel_info_blob())
    assert info.total_steps == 10
    assert info.stable_steps == 5
    assert info.table_units == 1 and info.base_units == 1
    assert info.start_speed == 8 and info.target_speed == 2
    assert info.ability == 0 and info.table_count == 1


def test_build_acceleration_table() -> None:
    info = protocol.parse_acceleration_info(_accel_info_blob())
    table = protocol.build_acceleration_table(info)
    assert len(table) == info.total_steps
    # base_units 1 -> no padding; all entries decreased by one
    # (avision.c:6294-6297): ramp starts at start_speed - 1 and ends at
    # target_speed - 1, with the stable steps repeating the last ramp value
    assert table[0] == info.start_speed - 1
    assert table[-1] == info.target_speed - 1
    accel_steps = info.total_steps - info.stable_steps + 1
    assert all(v == info.target_speed - 1 for v in table[accel_steps:])
    assert all(a >= b for a, b in zip(table, table[1:]))  # decelerating ramp


def test_build_acceleration_table_rejects_bad_info() -> None:
    # avision.c:6190-6200
    blob = bytearray(_accel_info_blob())
    blob[14:16] = protocol.be16(0)  # target speed 0
    with pytest.raises(ValueError):
        protocol.build_acceleration_table(protocol.parse_acceleration_info(bytes(blob)))
    blob = bytearray(_accel_info_blob())
    blob[16] = 1  # non-zero ability
    with pytest.raises(ValueError):
        protocol.build_acceleration_table(protocol.parse_acceleration_info(bytes(blob)))


def test_model_feature_table() -> None:
    # model table avision.c:242-286
    assert AV210_MODEL_FEATURES[0x0A24] == ModelFeatures.ACCEL_TABLE
    assert AV210_MODEL_FEATURES[0x0A25] == (
        ModelFeatures.ACCEL_TABLE | ModelFeatures.NO_64BYTE_ALIGN
    )
    assert AV210_MODEL_FEATURES[0x0A3A] == ModelFeatures.NONE
    assert AV210_MODEL_FEATURES[0x0A2F] == ModelFeatures.NONE
    assert AV210_MODEL_FEATURES[0x1A35] == ModelFeatures.USE_GRAY_FILTER


# ---------------------------------------------------------------------------
# Transport framing against a mock USB device
# ---------------------------------------------------------------------------


class MockUSB:
    """Scripted USB device: queued bulk-IN / interrupt-IN chunks, recorded
    bulk-OUT writes.  Reads return b'' when their queue is empty (= timeout)."""

    def __init__(self, bulk_in: List[bytes] = (), int_in: List[bytes] = ()) -> None:
        self.writes: List[bytes] = []
        self.bulk_in = list(bulk_in)
        self.int_in = list(int_in)
        self.bulk_read_calls = 0
        self.int_read_calls = 0

    def bulk_write(self, data: bytes, timeout_ms: int) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def bulk_read(self, length: int, timeout_ms: int) -> bytes:
        self.bulk_read_calls += 1
        if not self.bulk_in:
            return b""
        return self.bulk_in.pop(0)[:length]

    def interrupt_read(self, length: int, timeout_ms: int) -> bytes:
        self.int_read_calls += 1
        if not self.int_in:
            return b""
        return self.int_in.pop(0)[:length]


def test_cdb_padded_to_ten_bytes_and_data_returned() -> None:
    blob = _make_inquiry_blob()
    mock = MockUSB(bulk_in=[blob, b"\x00"])
    transport = AvisionTransport(mock)
    data = transport.send_cmd(protocol.INQUIRY_CDB, data_in_len=96)
    assert data == blob
    # 6-byte CDB zero-padded to 10 on the wire (avision.c:2544-2551)
    assert mock.writes == [bytes.fromhex("12000000600000000000")]
    assert transport.status_pipe is StatusPipe.BULK


def test_partial_reads_accumulate() -> None:
    mock = MockUSB(bulk_in=[b"\x01\x02\x03", b"\x04\x05\x06", b"\x00"])
    transport = AvisionTransport(mock)
    data = transport.send_cmd(protocol.MEDIA_CHECK_CDB[:6], data_in_len=6)
    assert data == bytes((1, 2, 3, 4, 5, 6))


def test_lone_status_byte_during_data_phase_restarts_command() -> None:
    # a 1-byte read when >1 byte is expected is a stray status byte ->
    # whole command re-sent (avision.c:2634-2637)
    mock = MockUSB(bulk_in=[b"\x05", b"\x01\x02\x03\x04", b"\x00"])
    transport = AvisionTransport(mock)
    data = transport.send_cmd(bytes.fromhex("080000000400"), data_in_len=4)
    assert data == bytes((1, 2, 3, 4))
    assert len(mock.writes) == 2  # CDB was re-sent


def test_busy_then_good_retries() -> None:
    mock = MockUSB(bulk_in=[b"\x08", b"\x00"])
    transport = AvisionTransport(mock)
    transport.send_cmd(protocol.TEST_UNIT_READY_CDB)
    # BUSY consumed one attempt, second attempt succeeded
    assert len(mock.writes) == 2
    assert all(w == b"\x00" * 10 for w in mock.writes)


def test_busy_exhausts_budget() -> None:
    mock = MockUSB(bulk_in=[b"\x08", b"\x08", b"\x08"])
    transport = AvisionTransport(mock)
    with pytest.raises(DeviceBusyError):
        transport.send_cmd(protocol.TEST_UNIT_READY_CDB)
    assert len(mock.writes) == 3  # retry = 4 pre-decremented (avision.c:2529)


def test_no_status_byte_exhausts_budget() -> None:
    mock = MockUSB()
    transport = AvisionTransport(mock)
    with pytest.raises(TransportError):
        transport.send_cmd(protocol.TEST_UNIT_READY_CDB)
    assert len(mock.writes) == 3


def test_request_sense_path_maps_no_paper() -> None:
    sense = _sense(key=0x02, asc=0x80, ascq=0x03)
    # data byte, status 0x02, 22-byte sense, trailing status drain
    mock = MockUSB(bulk_in=[b"\x00", b"\x02", sense, b"\x00"])
    transport = AvisionTransport(mock)
    with pytest.raises(NoPaperError):
        transport.send_cmd(protocol.MEDIA_CHECK_CDB, data_in_len=1)
    # second write is the exact REQUEST SENSE wire CDB (avision.c:2656-2681)
    assert len(mock.writes) == 2
    assert mock.writes[1] == bytes.fromhex("03000000160000000000")


def test_sense_error_carries_completed_payload() -> None:
    # The data phase completes before the status byte is read
    # (avision.c:2622-2654), and reader_process keeps counting this_read
    # into the stripe on EOF (avision.c:7873-7885) -- the raised error must
    # carry the payload so callers can keep it.
    payload = b"\xaa\xbb\xcc\xdd"
    eof_sense = _sense(key=0x02, asc=0x80, ascq=0x04)
    mock = MockUSB(bulk_in=[payload, b"\x02", eof_sense, b"\x00"])
    transport = AvisionTransport(mock)
    cdb = protocol.read_cdb(protocol.DTC_READ_IMAGE_DATA, 0x0A0D, 4)
    with pytest.raises(EndOfPaperError) as excinfo:
        transport.send_cmd(cdb, data_in_len=4)
    assert excinfo.value.data == payload


def test_request_sense_good_sense_returns_data() -> None:
    # sense key 0 means "ok ?!?" -> command result is kept (avision.c:2216-2219)
    mock = MockUSB(bulk_in=[b"\x01", b"\x02", _sense(), b"\x00"])
    transport = AvisionTransport(mock)
    data = transport.send_cmd(protocol.MEDIA_CHECK_CDB, data_in_len=1)
    assert data == b"\x01"


def test_status_pipe_latches_to_interrupt() -> None:
    # bulk status read fails, interrupt succeeds -> INT pipe latched
    # (avision.c:2373-2431)
    mock = MockUSB(int_in=[b"\x00", b"\x00"])
    transport = AvisionTransport(mock)
    transport.send_cmd(protocol.TEST_UNIT_READY_CDB)
    assert transport.status_pipe is StatusPipe.INTERRUPT
    bulk_calls = mock.bulk_read_calls
    transport.send_cmd(protocol.TEST_UNIT_READY_CDB)
    # once latched, the bulk pipe is never probed for status again
    assert mock.bulk_read_calls == bulk_calls


def test_data_out_written_after_cdb() -> None:
    payload = bytes(range(70))
    mock = MockUSB(bulk_in=[b"\x00"])
    transport = AvisionTransport(mock)
    transport.send_cmd(protocol.SET_WINDOW_CDB, data_out=payload)
    assert mock.writes == [protocol.SET_WINDOW_CDB, payload]
