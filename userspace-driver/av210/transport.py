"""Raw SCSI-over-USB transport for Avision scanners.

Byte-faithful reimplementation of ``avision_cmd()`` (avision.c:2510-2709)
and ``avision_usb_status()`` (avision.c:2373-2454) on top of pyusb.

Avision USB scanners speak raw SCSI with no wrapper (no mass-storage CBW):

1. bulk-OUT: the CDB, zero-padded to at least 10 bytes (avision.c:2540-2551);
2. bulk-OUT: outgoing payload, if any (avision.c:2602-2620);
3. bulk-IN: exactly the expected payload length (avision.c:2622-2646);
4. a mandatory 1-byte status read from either the bulk-IN or interrupt-IN
   pipe -- "this is needed - otherwise the scanner will hang"
   (avision.c:2648-2649);
5. on status 0x02 (CHECK CONDITION), an inline REQUEST SENSE exchange
   (avision.c:2656-2707).

The transport is written against a small duck-typed USB device interface
(:class:`UsbDeviceProtocol`), so it can be unit-tested with a mock and used
with pyusb in production.
"""

from __future__ import annotations

import enum
from typing import Optional, Protocol

from .protocol import (
    SCSI_INQUIRY,
    SCSI_REQUEST_SENSE,
    SCSI_TEST_UNIT_READY,
    AvisionError,
    decode_sense,
    exception_for_sense,
)

#: Avision vendor ID.
AVISION_VENDOR_ID = 0x0638


class ModelFeatures(enum.IntFlag):
    """Model-table feature flags this driver implements (avision.h feature
    enum; per-model assignments avision.c:236-290).  ``AV_INT_BUTTON`` (all
    AV210 models) and ``AV_GRAY_MODES`` (a capability unlock, honored in
    :func:`av210.protocol.parse_inquiry`) need no per-command handling."""

    NONE = 0
    #: AV_ACCEL_TABLE: upload an acceleration table right after START SCAN
    #: (avision.c:7695-7703).
    ACCEL_TABLE = enum.auto()
    #: AV_NO_64BYTE_ALIGN: READ transfer lengths must not be multiples of 64
    #: (read_constrains, avision.c:1626-1631).
    NO_64BYTE_ALIGN = enum.auto()
    #: AV_USE_GRAY_FILTER: gray/lineart windows select AVISION_FILTER_GRAY
    #: (avision.c:6574-6577).
    USE_GRAY_FILTER = enum.auto()


#: AV210 family model table (avision.desc / avision.c:242-286):
#: AV210 (0x0A24: AV_INT_BUTTON | AV_ACCEL_TABLE),
#: AV210 pre-production (0x0A25: ... | AV_ACCEL_TABLE | AV_NO_64BYTE_ALIGN),
#: AV210C2 (0x0A3A) and AV210C2-G (0x0A2F): AV_INT_BUTTON | AV_GRAY_MODES,
#: AV210D2+ (0x1A35: AV_INT_BUTTON | AV_USE_GRAY_FILTER).
AV210_MODEL_FEATURES = {
    0x0A24: ModelFeatures.ACCEL_TABLE,
    0x0A25: ModelFeatures.ACCEL_TABLE | ModelFeatures.NO_64BYTE_ALIGN,
    0x0A3A: ModelFeatures.NONE,
    0x0A2F: ModelFeatures.NONE,
    0x1A35: ModelFeatures.USE_GRAY_FILTER,
}

#: AV210 family product IDs.
AV210_PRODUCT_IDS = frozenset(AV210_MODEL_FEATURES)

# Timeouts, milliseconds (avision.c:2527-2532)
STD_TIMEOUT_MS = 30000
STD_STATUS_TIMEOUT_MS = 10000
#: INQUIRY read/status timeout (avision.c:2555-2557)
INQUIRY_TIMEOUT_MS = 1000
#: TEST UNIT READY read/status timeout (avision.c:2559-2561)
TUR_TIMEOUT_MS = 15000
#: 1-try status read after a failed CDB write, "to clear the FIFO"
#: (avision.c:2586-2587)
DRAIN_STATUS_TIMEOUT_MS = 500

#: CDBs are zero-padded to this length on the wire (min_usb_size,
#: avision.c:2540).
MIN_USB_CDB_SIZE = 10

#: retry = 4 with pre-decrement -> at most 3 command executions
#: (avision.c:2529, 2568-2572).
COMMAND_RETRY_BUDGET = 4

# Status byte values (avision.h:592-595)
USB_STATUS_GOOD = 0x00
USB_STATUS_REQUEST_SENSE = 0x02
USB_STATUS_BUSY = 0x08

#: REQUEST SENSE wire CDB: opcode 0x03, allocation length 22 = 0x16 at byte 4
#: (avision.c:2656-2681).
SENSE_BUFFER_SIZE = 22
REQUEST_SENSE_CDB = bytes(
    (SCSI_REQUEST_SENSE, 0, 0, 0, SENSE_BUFFER_SIZE, 0, 0, 0, 0, 0)
)


class DeviceNotFoundError(AvisionError):
    """No AV210-family scanner was found on the USB bus."""


class TransportError(AvisionError):
    """USB I/O failed after exhausting the retry budget (avision.c:2568-2572)."""


class DeviceBusyError(AvisionError):
    """The device kept answering status 0x08 BUSY (avision.c:2449-2450).

    Callers poll with 1-second sleeps, like wait_ready (avision.c:3311-3341).
    """


class StatusPipe(enum.Enum):
    """Tri-state status-pipe latch (avision.h:133-137)."""

    UNTESTED = "untested"
    BULK = "bulk"
    INTERRUPT = "interrupt"


class _UsbIOError(Exception):
    """Internal: a bulk write failed (timeout/stall). Triggers command retry."""


class UsbDeviceProtocol(Protocol):
    """Minimal USB device interface the transport needs.

    Read methods return ``b''`` on timeout / recoverable error -- the framing
    layer turns that into a whole-command retry, matching avision_cmd.
    Write failures raise :class:`_UsbIOError`.
    """

    def bulk_write(self, data: bytes, timeout_ms: int) -> int: ...

    def bulk_read(self, length: int, timeout_ms: int) -> bytes: ...

    def interrupt_read(self, length: int, timeout_ms: int) -> bytes: ...


class PyUSBDevice:
    """pyusb-backed implementation of :class:`UsbDeviceProtocol`.

    Reproduces the sanei_usb conventions: detach the kernel driver, ensure a
    configuration is set, claim interface 0, and record the *first* bulk-IN,
    bulk-OUT and interrupt-IN endpoints from the descriptors (transport spec
    section 6 -- endpoints are discovered, never hardcoded).  On an endpoint
    stall the halt is cleared before the framing layer retries (the userspace
    equivalent of the kernel/sanei stall handling).
    """

    def __init__(self, dev: "object") -> None:
        import usb.core
        import usb.util

        self._usb = usb
        self._dev = dev
        #: USB product ID; selects the model-table feature flags
        #: (avision.c:242-286).
        self.product_id: Optional[int] = getattr(dev, "idProduct", None)

        # Detach a bound kernel driver (Linux); not implemented on Windows.
        try:
            if dev.is_kernel_driver_active(0):  # type: ignore[attr-defined]
                dev.detach_kernel_driver(0)  # type: ignore[attr-defined]
        except (NotImplementedError, usb.core.USBError):
            pass

        # Ensure a configuration is active (sanei_usb sets configuration only
        # when none is reported).
        try:
            cfg = dev.get_active_configuration()  # type: ignore[attr-defined]
        except usb.core.USBError:
            dev.set_configuration()  # type: ignore[attr-defined]
            cfg = dev.get_active_configuration()  # type: ignore[attr-defined]

        intf = cfg[(0, 0)]
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        self._interface = intf.bInterfaceNumber

        self.bulk_in_ep: Optional[int] = None
        self.bulk_out_ep: Optional[int] = None
        self.interrupt_in_ep: Optional[int] = None
        for ep in intf:
            addr = ep.bEndpointAddress
            ep_type = usb.util.endpoint_type(ep.bmAttributes)
            ep_in = usb.util.endpoint_direction(addr) == usb.util.ENDPOINT_IN
            if ep_type == usb.util.ENDPOINT_TYPE_BULK:
                if ep_in and self.bulk_in_ep is None:
                    self.bulk_in_ep = addr
                elif not ep_in and self.bulk_out_ep is None:
                    self.bulk_out_ep = addr
            elif ep_type == usb.util.ENDPOINT_TYPE_INTR:
                if ep_in and self.interrupt_in_ep is None:
                    self.interrupt_in_ep = addr

        if self.bulk_in_ep is None or self.bulk_out_ep is None:
            raise DeviceNotFoundError(
                "device has no bulk-in/bulk-out endpoint pair on interface 0"
            )

    def _clear_halt(self, endpoint: int) -> None:
        try:
            self._dev.clear_halt(endpoint)  # type: ignore[attr-defined]
        except self._usb.core.USBError:
            pass

    def bulk_write(self, data: bytes, timeout_ms: int) -> int:
        try:
            return int(self._dev.write(self.bulk_out_ep, data, timeout_ms))  # type: ignore[attr-defined]
        except self._usb.core.USBTimeoutError as exc:
            raise _UsbIOError(str(exc)) from exc
        except self._usb.core.USBError as exc:
            self._clear_halt(self.bulk_out_ep)
            raise _UsbIOError(str(exc)) from exc

    def bulk_read(self, length: int, timeout_ms: int) -> bytes:
        try:
            return bytes(self._dev.read(self.bulk_in_ep, length, timeout_ms))  # type: ignore[attr-defined]
        except self._usb.core.USBTimeoutError:
            return b""
        except self._usb.core.USBError:
            self._clear_halt(self.bulk_in_ep)
            return b""

    def interrupt_read(self, length: int, timeout_ms: int) -> bytes:
        if self.interrupt_in_ep is None:
            return b""
        try:
            return bytes(self._dev.read(self.interrupt_in_ep, length, timeout_ms))  # type: ignore[attr-defined]
        except self._usb.core.USBTimeoutError:
            return b""
        except self._usb.core.USBError:
            self._clear_halt(self.interrupt_in_ep)
            return b""

    def describe(self) -> str:
        """Human-readable endpoint summary for ``av210 probe``."""
        parts = [
            "bulk-in  0x%02x" % self.bulk_in_ep,
            "bulk-out 0x%02x" % self.bulk_out_ep,
        ]
        if self.interrupt_in_ep is not None:
            parts.append("int-in   0x%02x" % self.interrupt_in_ep)
        return ", ".join(parts)

    def close(self) -> None:
        try:
            self._usb.util.release_interface(self._dev, self._interface)
        except self._usb.core.USBError:
            pass
        self._usb.util.dispose_resources(self._dev)


def find_usb_device() -> "object":
    """Locate the first AV210-family scanner (VID 0x0638) on the bus."""
    import usb.core

    kwargs = {}
    try:  # optional bundled libusb DLL on Windows
        import libusb_package

        kwargs["backend"] = libusb_package.get_libusb1_backend()
    except ImportError:
        pass

    try:
        dev = usb.core.find(
            custom_match=lambda d: d.idVendor == AVISION_VENDOR_ID
            and d.idProduct in AV210_PRODUCT_IDS,
            **kwargs,
        )
    except usb.core.NoBackendError as exc:
        raise DeviceNotFoundError(
            "no libusb backend available -- install libusb (Linux: your "
            "distribution's libusb-1.0 package; Windows: the libusb-package "
            "wheel provides one)"
        ) from exc
    if dev is None:
        raise DeviceNotFoundError(
            "no Avision AV210-family scanner found (VID 0x0638, PIDs "
            + ", ".join("0x%04X" % p for p in sorted(AV210_PRODUCT_IDS))
            + ")"
        )
    return dev


class AvisionTransport:
    """One open Avision USB connection with avision_cmd framing.

    Use :meth:`AvisionTransport.open` for real hardware or pass any
    :class:`UsbDeviceProtocol` implementation (e.g. a mock) directly.
    """

    def __init__(self, device: UsbDeviceProtocol) -> None:
        self._dev = device
        self._status_pipe = StatusPipe.UNTESTED

    @classmethod
    def open(cls) -> "AvisionTransport":
        """Find, claim and wrap the first AV210-family device."""
        return cls(PyUSBDevice(find_usb_device()))

    @property
    def device(self) -> UsbDeviceProtocol:
        return self._dev

    @property
    def status_pipe(self) -> StatusPipe:
        """Which pipe delivers status bytes; latched on first success
        (avision.c:2389-2431)."""
        return self._status_pipe

    @property
    def product_id(self) -> Optional[int]:
        """USB product ID of the connected device, if known."""
        return getattr(self._dev, "product_id", None)

    @property
    def model_features(self) -> ModelFeatures:
        """Model-table feature flags for the connected device
        (avision.c:242-286); NONE when the product ID is unknown."""
        pid = self.product_id
        if pid is None:
            return ModelFeatures.NONE
        return AV210_MODEL_FEATURES.get(pid, ModelFeatures.NONE)

    def close(self) -> None:
        close = getattr(self._dev, "close", None)
        if callable(close):
            close()

    # -- status phase -----------------------------------------------------

    def _read_status_byte(self, tries: int, timeout_ms: int) -> Optional[int]:
        """avision_usb_status (avision.c:2373-2454): 1-byte read, bulk pipe
        first, then interrupt pipe; whichever answers is latched.  Returns
        ``None`` when no byte arrived (SANE_STATUS_IO_ERROR path,
        avision.c:2436)."""
        if self._status_pipe in (StatusPipe.UNTESTED, StatusPipe.BULK):
            for _ in range(tries):
                data = self._dev.bulk_read(1, timeout_ms)
                if data:
                    self._status_pipe = StatusPipe.BULK
                    return data[0]
        if self._status_pipe in (StatusPipe.UNTESTED, StatusPipe.INTERRUPT):
            for _ in range(tries):
                data = self._dev.interrupt_read(1, timeout_ms)
                if data:
                    self._status_pipe = StatusPipe.INTERRUPT
                    return data[0]
        return None

    # -- sense phase ------------------------------------------------------

    def _request_sense(self, write_timeout_ms: int, read_timeout_ms: int,
                       status_timeout_ms: int) -> bytes:
        """Inline REQUEST SENSE exchange (avision.c:2656-2705).

        Deliberately not routed through :meth:`send_cmd` to avoid recursion
        if the sense transfer itself fails (comment avision.c:2667-2669).
        """
        try:
            self._dev.bulk_write(REQUEST_SENSE_CDB, write_timeout_ms)
        except _UsbIOError as exc:
            raise TransportError("REQUEST SENSE write failed: %s" % exc) from exc
        # single bulk read, no accumulation loop (avision.c:2687-2691)
        sense = self._dev.bulk_read(SENSE_BUFFER_SIZE, read_timeout_ms)
        # drain the trailing status byte; GOOD and 0x02 are both accepted --
        # "some scanner return NEED_SENSE even after reading it"
        # (avision.c:2694-2701).  Other outcomes are ignored like the C.
        self._read_status_byte(1, status_timeout_ms)
        return sense

    # -- command execution ------------------------------------------------

    def send_cmd(
        self,
        cdb: bytes,
        data_out: Optional[bytes] = None,
        data_in_len: int = 0,
    ) -> bytes:
        """Execute one SCSI command over USB (avision_cmd, avision.c:2510-2709).

        :param cdb: the SCSI CDB; zero-padded to 10 bytes on the wire.
        :param data_out: outgoing payload written raw after the CDB.
        :param data_in_len: exact number of payload bytes to read back.
        :returns: the ``data_in_len`` payload bytes (``b''`` if none).
        :raises TransportError: after 3 failed executions (avision.c:2529).
        :raises DeviceBusyError: the device answered BUSY on every attempt.
        :raises SenseError: CHECK CONDITION with non-GOOD sense (subclasses
            such as :class:`~av210.protocol.NoPaperError` identify the cause).
        """
        m_cmd = bytes(cdb)
        if len(m_cmd) < MIN_USB_CDB_SIZE:  # avision.c:2544-2551
            m_cmd = m_cmd + b"\x00" * (MIN_USB_CDB_SIZE - len(m_cmd))

        write_timeout = STD_TIMEOUT_MS
        read_timeout = STD_TIMEOUT_MS
        status_timeout = STD_STATUS_TIMEOUT_MS
        # per-opcode timeout tweaks (avision.c:2553-2563)
        if m_cmd[0] == SCSI_INQUIRY:
            read_timeout = status_timeout = INQUIRY_TIMEOUT_MS
        elif m_cmd[0] == SCSI_TEST_UNIT_READY:
            read_timeout = status_timeout = TUR_TIMEOUT_MS

        retry = COMMAND_RETRY_BUDGET
        busy = False
        while True:
            # write_usb_cmd label with pre-decrement (avision.c:2568-2572)
            retry -= 1
            if retry == 0:
                if busy:
                    raise DeviceBusyError("device busy after 3 attempts")
                raise TransportError("max retry count reached: I/O error")
            busy = False

            # 1. CDB on bulk-OUT (avision.c:2574-2597)
            try:
                written = self._dev.bulk_write(m_cmd, write_timeout)
            except _UsbIOError:
                # 1-try 500 ms status read "to clear the FIFO"
                # (avision.c:2585-2597); only a GOOD byte allows a retry.
                drained = self._read_status_byte(1, DRAIN_STATUS_TIMEOUT_MS)
                if drained == USB_STATUS_GOOD:
                    continue
                raise TransportError("USB command write failed")
            if written != len(m_cmd):
                continue  # short write with GOOD status -> retry (avision.c:2596)

            # 2. outgoing payload, raw, partial writes accepted
            # (avision.c:2602-2620)
            if data_out:
                offset = 0
                failed = False
                while offset < len(data_out):
                    try:
                        n = self._dev.bulk_write(data_out[offset:], write_timeout)
                    except _UsbIOError:
                        failed = True
                        break
                    if n <= 0:
                        failed = True
                        break
                    offset += n
                if failed:
                    continue  # restart whole command (avision.c:2618)

            # 3. incoming payload, insist on the full length
            # (avision.c:2622-2646)
            data = b""
            if data_in_len > 0:
                buf = bytearray()
                restart = False
                while len(buf) < data_in_len:
                    chunk = self._dev.bulk_read(data_in_len - len(buf), read_timeout)
                    if len(chunk) == 1 and data_in_len - len(buf) > 1:
                        # lone byte == stray status byte -> retry
                        # (avision.c:2634-2637)
                        restart = True
                        break
                    if chunk:
                        buf += chunk
                    else:
                        restart = True  # "No data arrived." (avision.c:2643)
                        break
                if restart:
                    continue
                data = bytes(buf)

            # 4. mandatory status byte (avision.c:2648-2654)
            status = self._read_status_byte(1, status_timeout)
            if status is None:
                continue  # retry whole command
            if status == USB_STATUS_GOOD:
                return data
            if status == USB_STATUS_BUSY:
                busy = True
                continue  # consumes the retry budget (avision.c:2652-2654)

            # 5. 0x02 and unknown bytes both mean "request sense"
            # (avision.c:2439-2453, 2656-2707)
            sense = self._request_sense(write_timeout, read_timeout, status_timeout)
            info = decode_sense(sense)
            error = exception_for_sense(info)
            if error is None:
                return data  # sense says GOOD (avision.c:2216-2219)
            # The data phase completed before the status byte was read, so
            # the payload is valid even on EOF-style senses; reader_process
            # keeps and processes those this_read bytes (avision.c:7873-7885).
            error.data = data
            raise error
