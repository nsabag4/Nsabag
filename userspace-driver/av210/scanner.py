"""High-level driver for the Avision AV210 family of sheetfed scanners.

Orchestrates the per-page command sequence exactly as the SANE backend does
(appendix of the scan-flow spec; reader_process avision.c:7545-8440,
sane_start avision.c:9210-9480):

    MEDIA CHECK -> [wait for light] -> SET WINDOW -> [wait for light again]
    -> [calibration, page 0] -> SEND GAMMA x3 -> [tune scan length]
    -> RESERVE UNIT -> START SCAN -> [acceleration table] -> READ ...
    (until EOF sense) -> RELEASE UNIT.
"""

from __future__ import annotations

import time
from typing import Iterator, Optional

from PIL import Image

from . import protocol
from .protocol import (
    AvisionError,
    EndOfPaperError,
    NoPaperError,
    ScanMode,
    ScanParams,
    ScannerInfo,
    SenseError,
)
from .transport import (
    AvisionTransport,
    DeviceBusyError,
    ModelFeatures,
    TransportError,
)

#: wait_ready: up to 10 TEST UNIT READY attempts, 1 s apart
#: (avision.c:3311-3341; delay = 1 at the sane_open call site, avision.c:8810).
WAIT_READY_TRIES = 10
#: wait_4_light: up to 90 attempts, 1 s apart (avision.c:3368-3417).
WAIT_LIGHT_TRIES = 90
#: max bytes per image READ for USB (avision.c:7736-7738).
MAX_BYTES_PER_READ = 0x100000


class AV210Scanner:
    """One AV210-family scanner.

    Usage::

        with AV210Scanner() as scanner:
            image = scanner.scan_page(resolution=300, mode=ScanMode.COLOR)
    """

    def __init__(self, transport: Optional[AvisionTransport] = None) -> None:
        self._transport = transport
        self._info: Optional[ScannerInfo] = None

    # -- lifecycle ----------------------------------------------------------

    def open(self, wait_ready_delay: float = 1.0) -> ScannerInfo:
        """Open the device: INQUIRY wake-up, then wait_ready
        (sane_open, avision.c:8802-8824)."""
        if self._transport is None:
            self._transport = AvisionTransport.open()
        self._info = self.inquiry()
        self.wait_ready(delay=wait_ready_delay)
        return self._info

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        self._info = None

    def __enter__(self) -> "AV210Scanner":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def transport(self) -> AvisionTransport:
        if self._transport is None:
            raise AvisionError("scanner is not open")
        return self._transport

    @property
    def info(self) -> ScannerInfo:
        if self._info is None:
            raise AvisionError("scanner is not open -- call open() first")
        return self._info

    @property
    def features(self) -> ModelFeatures:
        """Model-table feature flags for the connected product ID
        (avision.c:242-286)."""
        return self.transport.model_features

    # -- basic commands -------------------------------------------------------

    def inquiry(self) -> ScannerInfo:
        """INQUIRY (0x12, 96 bytes); retried once like the C wrapper
        (inquiry(), avision.c:3283-3309)."""
        last_error: Optional[Exception] = None
        for _ in range(2):
            try:
                data = self.transport.send_cmd(
                    protocol.INQUIRY_CDB, data_in_len=protocol.INQUIRY_SIZE
                )
                self._info = protocol.parse_inquiry(data)
                return self._info
            except (TransportError, DeviceBusyError, SenseError) as exc:
                last_error = exc
        raise TransportError("INQUIRY failed: %s" % last_error)

    def wait_ready(self, delay: float = 1.0) -> None:
        """TEST UNIT READY poll (wait_ready, avision.c:3311-3341): up to 10
        tries, sleeping ``delay`` seconds after *every* attempt (the C sleeps
        unconditionally, avision.c:3322); BUSY and errors both keep looping."""
        for _ in range(WAIT_READY_TRIES):
            try:
                self.transport.send_cmd(protocol.TEST_UNIT_READY_CDB)
                ok = True
            except (TransportError, DeviceBusyError, SenseError):
                ok = False
            if delay > 0:
                time.sleep(delay)
            if ok:
                return
        raise TransportError("scanner not ready after %d attempts" % WAIT_READY_TRIES)

    def media_check(self) -> bool:
        """MEDIA CHECK (0x08): True iff paper is present
        (media_check, avision.c:6879-6897)."""
        result = self.transport.send_cmd(protocol.MEDIA_CHECK_CDB, data_in_len=1)
        return protocol.media_check_present(result)

    def wait_for_light(self, delay: float = 1.0) -> None:
        """wait_4_light (avision.c:3344-3421): READ light status (dtc 0xA0,
        1 byte); 1 = on / 5 = backlight on are OK; otherwise SEND light-on
        (payload 0x01) and sleep, up to 90 tries."""
        info = self.info
        read = protocol.read_cdb(protocol.DTC_LIGHT_STATUS, info.data_dq, 1)
        send = protocol.send_cdb(protocol.DTC_LIGHT_STATUS, info.data_dq, 1)
        for _ in range(WAIT_LIGHT_TRIES):
            status = self.transport.send_cmd(read, data_in_len=1)
            if status[0] in protocol.LIGHT_STATUS_OK:
                return
            self.transport.send_cmd(send, data_out=b"\x01")
            if delay > 0:
                time.sleep(delay)
        raise DeviceBusyError("lamp did not come on after %d attempts" % WAIT_LIGHT_TRIES)

    # -- calibration (normal_calibration, avision.c:5715-5835) ---------------

    def _calibrate(self, mode: ScanMode) -> None:
        """Run calibration when the device demands it.

        Decision chain per sane_start (avision.c:9343-9396): the AV210 family
        has no ``AV_NO_CALIB`` flag, so calibration is attempted whenever
        inquiry byte [50] bit 4 is set; GET CALIBRATION FORMAT is then issued
        and the device itself says whether data must be produced
        (``flags == 1``, avision.c:5733-5737).
        """
        info = self.info
        raw = self.transport.send_cmd(
            protocol.GET_CALIB_FORMAT_CDB, data_in_len=protocol.CALIB_FORMAT_SIZE
        )
        fmt = protocol.parse_calib_format(raw, color_mode=(mode is ScanMode.COLOR))
        if not fmt.needs_calibration:
            # "Scanner claims no calibration needed -> skipped!"
            # (avision.c:5734-5736) -- the typical sheetfed CIS answer.
            return

        dark_avg: Optional[bytearray] = None
        if fmt.has_dark_pass:  # ability1 bit 2 (avision.c:5750-5774)
            dark_raw = self._read_calib_data(protocol.DTC_DARK_CALIB, fmt)
            dark_avg = protocol.sort_and_average(fmt, dark_raw)
            protocol.compute_dark_shading(fmt, dark_avg, info.max_shading_target)

        # white pass: dtc 0x62 color / 0x61 gray (avision.c:5776-5787)
        dtc = (
            protocol.DTC_WHITE_CALIB_COLOR
            if fmt.channels > 1
            else protocol.DTC_WHITE_CALIB_GRAY
        )
        white_raw = self._read_calib_data(dtc, fmt)
        white_avg = protocol.sort_and_average(fmt, white_raw)
        if dark_avg is not None:  # avision.c:5816-5826
            protocol.subtract_dark_average(white_avg, dark_avg, fmt)
        protocol.compute_white_shading(fmt, white_avg, info.max_shading_target)

        for cdb, payload in protocol.build_calibration_upload(fmt, white_avg, dark_avg):
            self.transport.send_cmd(cdb, data_out=payload)

    def _read_calib_data(self, datatypecode: int, fmt: protocol.CalibrationFormat) -> bytes:
        """Chunked calibration read (get_calib_data, avision.c:5340-5392;
        chunk = whole size for USB, avision.c:5351, shrunk per-read by
        read_constrains on AV_NO_64BYTE_ALIGN models, avision.c:5370)."""
        data_size = fmt.calib_data_size
        get_size = data_size
        out = bytearray()
        while data_size:
            if get_size > data_size:
                get_size = data_size
            get_size = self._constrain_read(get_size)
            cdb = protocol.read_cdb(datatypecode, self.info.data_dq, get_size)
            out += self.transport.send_cmd(cdb, data_in_len=get_size)
            data_size -= get_size
        return bytes(out)

    # -- acceleration table (avision.c:6127-6320) -----------------------------

    def _send_acceleration_table(self) -> None:
        """Port of send_acceleration_table (avision.c:6169-6320): per table,
        READ the acceleration info (dtc 0x6C, dq data_dq, 24 bytes,
        avision.c:6138-6146), build the table and SEND it back (dtc 0x6C,
        dq = table index, avision.c:6208-6211, 6304-6306)."""
        info_cdb = protocol.read_cdb(
            protocol.DTC_ACCELERATION_TABLE, self.info.data_dq, protocol.ACCEL_INFO_SIZE
        )
        table = 0
        while True:
            raw = self.transport.send_cmd(
                info_cdb, data_in_len=protocol.ACCEL_INFO_SIZE
            )
            accel = protocol.parse_acceleration_info(raw)
            if accel.table_count == 0:
                # "device does not need tables" (avision.c:6185-6188)
                return
            try:
                payload = protocol.build_acceleration_table(accel)
            except ValueError as exc:
                raise TransportError("acceleration table: %s" % exc) from exc
            cdb = protocol.send_cdb(
                protocol.DTC_ACCELERATION_TABLE, table, len(payload)
            )
            self.transport.send_cmd(cdb, data_out=payload)
            table += 1
            if table >= accel.table_count:  # do-while (avision.c:6316)
                return

    # -- read-size constraints -------------------------------------------------

    def _constrain_read(self, size: int) -> int:
        """Apply read_constrains (avision.c:1626-1631) when the model carries
        AV_NO_64BYTE_ALIGN (AV210 pre-production, PID 0x0A25)."""
        if self.features & ModelFeatures.NO_64BYTE_ALIGN:
            return protocol.constrain_read_size(size)
        return size

    # -- gamma (send_gamma, avision.c:5886-6061) ------------------------------

    def _send_gamma(self, mode: ScanMode) -> None:
        """Three SEND 0x2A / dtc 0x81 commands, one per color R/G/B, 512-byte
        payload each -- sent even in gray/lineart mode (avision.c:5962)."""
        payload = protocol.build_gamma_payload(mode)
        for color in range(3):
            self.transport.send_cmd(protocol.gamma_cdb(color), data_out=payload)

    # -- scanning -------------------------------------------------------------

    def scan_page(
        self,
        resolution: int = 300,
        mode: ScanMode = ScanMode.COLOR,
        page: int = 0,
        quality: bool = True,
    ) -> Image.Image:
        """Scan one sheet from the ADF and return it as a PIL image.

        :param page: 0-based page index within a batch; controls whether the
            window/gamma are re-sent (inquiry [50] bits 1/2) and whether
            calibration may run (page 0 only, avision.c:9343-9346).
        :raises NoPaperError: the feeder is empty.
        """
        info = self.info
        params = protocol.compute_scan_params(info, resolution, mode)

        # 1. MEDIA CHECK on every sane_start (avision.c:9279-9288)
        if not self.media_check():
            raise NoPaperError(
                "No paper in the document feeder -- insert a sheet and try again."
            )

        # 2. lamp check only when inquiry [50] bit 7 is set (avision.c:9291-9298)
        if info.light_control:
            self.wait_for_light()

        # 3. window re-sent per page unless kept (avision.c:9300-9319)
        if page == 0 or not info.keeps_window:
            gray_filter = bool(self.features & ModelFeatures.USE_GRAY_FILTER)
            cdb, payload = protocol.build_set_window(params, gray_filter=gray_filter)
            self.transport.send_cmd(cdb, data_out=payload)
            # "Re-check the light, as setting the window may have changed
            # which light is to be turned on." (avision.c:9311-9318)
            if info.light_control:
                self.wait_for_light()

        # 4. calibration: only before the first page (avision.c:9343-9346)
        if page == 0 and info.new_protocol and info.needs_calibration:
            self._calibrate(mode)

        # 5. gamma re-sent per page unless kept (avision.c:9409-9430)
        if page == 0 or not info.keeps_gamma:
            self._send_gamma(mode)

        # 6. tune scan length: always sent for ADF scans when the inquiry
        # advertises it, even for zero overscan -- "the scanner keeps it in
        # RAM and previous runs could already have set something"
        # (avision.c:9434-9441, 5126-5127); the AV210 family is ADF-only.
        if info.tune_scan_length:
            for cdb, payload in protocol.tune_scan_length_commands():
                self.transport.send_cmd(cdb, data_out=payload)

        # get_background_raster (avision.c:9446-9453) is a no-op here: with
        # zero background lines requested (window byte [69], the driver's
        # fixed OPT_BACKGROUND=0 default) the C returns before issuing any
        # command (avision.c:6609-6612).

        # 7-11. RESERVE UNIT -> START SCAN -> [acceleration table] ->
        # READ loop -> RELEASE UNIT
        # (reader_process, avision.c:7670-7704, 8367-8390)
        self.transport.send_cmd(protocol.RESERVE_UNIT_CDB)
        self.transport.send_cmd(protocol.start_scan_cdb(quality=quality))
        try:
            # AV_ACCEL_TABLE models (PIDs 0x0A24/0x0A25) need the acceleration
            # table uploaded right after START SCAN (avision.c:7695-7703)
            if self.features & ModelFeatures.ACCEL_TABLE:
                self._send_acceleration_table()
            raster = self._read_page_data(params)
        except BaseException:
            # do_cancel: forced RELEASE UNIT type 1 ejects the sheet
            # (avision.c:7032-7035)
            try:
                self.transport.send_cmd(protocol.release_unit_cdb(1))
            except AvisionError:
                pass
            raise
        self.transport.send_cmd(protocol.release_unit_cdb(0))

        return self._assemble_image(params, raster)

    def scan_adf_batch(
        self,
        resolution: int = 300,
        mode: ScanMode = ScanMode.COLOR,
        quality: bool = True,
    ) -> Iterator[Image.Image]:
        """Scan pages until the feeder is empty (spec section 8: the batch
        ends when MEDIA CHECK reports no paper).

        Raises :class:`NoPaperError` if the feeder is empty before the first
        page; afterwards an empty feeder simply ends the iteration.
        """
        page = 0
        while True:
            try:
                yield self.scan_page(resolution, mode, page=page, quality=quality)
            except NoPaperError:
                if page == 0:
                    raise
                return
            page += 1

    # -- image data -----------------------------------------------------------

    def _read_page_data(self, params: ScanParams) -> bytes:
        """READ 0x28 / dtc 0x00 loop (reader_process, avision.c:7545-8440).

        Reads whole-line multiples ("otherwise some scanners freeze",
        avision.c:7814-7824) in stripes of ``read_stripe_size + 2*ld`` lines
        (avision.c:7726-7730) until ``total_size`` bytes arrived or the device
        raises the ADF-paper-end sense 0x80/0x04 (avision.c:2309).
        """
        info = self.info
        bpl = params.bytes_per_line
        ld = params.line_difference
        lines_per_stripe = info.read_stripe_size + 2 * ld
        stripe_size = bpl * lines_per_stripe
        total_size = bpl * (params.lines + 2 * ld)  # avision.c:7771-7772
        max_read = max(MAX_BYTES_PER_READ // bpl, 1) * bpl

        out = bytearray()
        stripe = bytearray()
        received = 0
        eof = False
        while received < total_size and not eof:
            to_read = min(stripe_size - len(stripe), max_read, total_size - received)
            to_read = self._constrain_read(to_read)  # avision.c:7826
            cdb = protocol.read_cdb(
                protocol.DTC_READ_IMAGE_DATA, info.data_dq, to_read
            )
            # On an EOF/no-docs sense the payload has already been fully
            # transferred (the status byte follows the data phase,
            # avision.c:2622-2654) and the C keeps counting this_read into
            # the stripe (avision.c:7873-7885) -- so the exception's data
            # must not be dropped.
            try:
                chunk = self.transport.send_cmd(cdb, data_in_len=to_read)
            except EndOfPaperError as exc:
                eof = True  # normal ADF end of page (avision.c:2309)
                chunk = exc.data
            except NoPaperError as exc:
                eof = True  # some firmware reports chute empty at page end
                chunk = exc.data
            stripe += chunk
            received += len(chunk)
            if len(stripe) >= stripe_size or (eof and stripe) or received >= total_size:
                out += self._process_stripe(params, stripe, final=eof or received >= total_size)
        return bytes(out)

    def _process_stripe(self, params: ScanParams, stripe: bytearray, final: bool) -> bytes:
        """Per-stripe post-processing (avision.c:7916-8022).

        * ``line_difference > 0``: software color pack -- R of output line n
          from raw offset ``n*bpl``, G from ``n*bpl + ld*bpl + 1``, B from
          ``n*bpl + 2*ld*bpl + 2``, stepping 3 bytes per pixel
          (avision.c:7984-7999); the last ``2*ld`` lines carry over to the
          next stripe.
        * inquiry [94] bit 6: each raw line is RRR..GGG..BBB and is repacked
          to RGBRGB (avision.c:8000-8018).
        * otherwise the data is already RGB/gray/lineart and copied verbatim
          (avision.c:8019-8022).
        """
        bpl = params.bytes_per_line
        ld = params.line_difference
        info = self.info

        if params.mode is ScanMode.COLOR and ld > 0:
            useful = len(stripe) - 2 * ld * bpl  # avision.c:7916-7917
            if useful <= 0:
                if final:
                    stripe.clear()
                return b""
            useful -= useful % bpl
            out = bytearray(useful)
            n_lines = useful // bpl
            # Strided slice assignment replaces the per-pixel loop: each
            # channel is every 3rd byte of its (line-difference shifted)
            # source line, interleaved into RGB output.
            for n in range(n_lines):
                base = n * bpl
                g_off = base + ld * bpl + 1
                b_off = base + 2 * ld * bpl + 2
                out[base:base + bpl:3] = stripe[base:base + bpl:3]
                out[base + 1:base + bpl:3] = stripe[g_off:g_off + bpl:3]
                out[base + 2:base + bpl:3] = stripe[b_off:b_off + bpl:3]
            del stripe[:useful]
            if final:
                stripe.clear()
            return bytes(out)

        if params.mode is ScanMode.COLOR and info.needs_line_pack:
            whole = len(stripe) - len(stripe) % bpl
            ppl = params.pixels_per_line
            out = bytearray(whole)
            # RRR..GGG..BBB -> RGBRGB via strided slice assignment (each
            # contiguous channel block lands on every 3rd output byte).
            for n in range(whole // bpl):
                base = n * bpl
                out[base:base + bpl:3] = stripe[base:base + ppl]
                out[base + 1:base + bpl:3] = stripe[base + ppl:base + 2 * ppl]
                out[base + 2:base + bpl:3] = stripe[base + 2 * ppl:base + 3 * ppl]
            del stripe[:whole]
            return bytes(out)

        out_bytes = bytes(stripe)
        stripe.clear()
        return out_bytes

    def _assemble_image(self, params: ScanParams, raster: bytes) -> Image.Image:
        """Turn the post-processed raster into a PIL image.

        The image height is the number of complete lines actually delivered
        (ADF pages are usually shorter than the 14-inch window; EOF before
        ``total_size`` is normal, avision.c:7873-7876).
        """
        bpl = params.bytes_per_line
        lines = len(raster) // bpl
        if lines == 0:
            raise TransportError("scanner returned no image data")
        raster = raster[: lines * bpl]
        size = (params.pixels_per_line, lines)

        if params.mode is ScanMode.COLOR:
            return Image.frombytes("RGB", size, raster)
        if params.mode is ScanMode.GRAY:
            return Image.frombytes("L", size, raster)
        # lineart: 1 bit per pixel, MSB first, 1 = black (SANE convention) --
        # PIL's "1;I" raw mode inverts so 1-bits render black.
        return Image.frombytes("1", size, raster, "raw", "1;I", bpl, 1)
