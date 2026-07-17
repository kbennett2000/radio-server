"""Pure wire codec for the kv4p HT (ADR 0061) — no I/O.

The kv4p HT is a CP210x/CH340 UART at 115200 8N1 over which *everything* rides —
RX/TX audio, tuning, PTT, squelch — framed in KISS. This module is the frame/struct
layer only: KISS (un)framing, the kv4p vendor-frame envelope, and codecs for the
on-wire structs. It imports nothing but the stdlib and performs no serial reads or
writes; the reader thread, flow-control window, ADPCM audio codec, and the backend
class arrive in later cycles.

Source of truth: an independent implementation derived from the kv4p-ht firmware
headers as a *specification* — read, not ported (kv4p-ht is GPL-3.0; radio-server is
not; talking to the device over a wire is not a derivative work). Pinned at commit
``e9935bd37e7505f70ae7023c78fe6a714be90be9``:

  - microcontroller-src/kv4p_ht_esp32_wroom_32/protocol.h  (framing, commands, structs)
  - microcontroller-src/kv4p_ht_esp32_wroom_32/globals.h   (PROTO_MTU, RfModuleType)

Guardrail 2 (ADR 0002) holds trivially here: PTT is a *flag inside* HostDesiredState
(``HostStateFlag.PTT_REQUESTED``), not a control line and not a CAT ``TX`` command —
there is no command path to misuse.

All structs are ``[[gnu::packed]]`` little-endian on an ESP32 (Xtensa LX6, where the
C ``char`` type is *signed*, hence the ``b`` codes below). Each dataclass carries its
``struct`` format string with an explicit ``<`` (which both fixes endianness and
disables native padding); ``SIZE`` is asserted against the documented field list in
the test suite.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, IntFlag
from typing import ClassVar

# --------------------------------------------------------------------------------------
# KISS framing constants (protocol.h:29-39)
# --------------------------------------------------------------------------------------

KISS_FEND = 0xC0  # frame delimiter (start and end)
KISS_FESC = 0xDB  # escape
KISS_TFEND = 0xDC  # transposed FEND (follows FESC)
KISS_TFESC = 0xDD  # transposed FESC (follows FESC)

KISS_CMD_DATA = 0x00  # standard KISS DATA frame — carries an AX.25 packet
KISS_CMD_SETHARDWARE = 0x06  # vendor frame — carries a kv4p command
KISS_PORT_0 = 0x00  # the only port we speak; others are dropped

# kv4p vendor envelope: FEND 0x06 "KV4P" 0x01 <kv4pCommand> <payload...> FEND
KV4P_VENDOR_PREFIX = b"KV4P"
KV4P_PROTOCOL_VERSION = 0x01
#: "KV4P" (4) + version (1) + kv4pCommand (1) — the fixed vendor header (protocol.h:37).
KV4P_VENDOR_HEADER_LEN = 6

#: Largest frame the firmware buffers (globals.h:54, protocol.h:38). Decoded length
#: (the leading KISS command byte + payload); a frame that would exceed it is dropped,
#: never truncated.
PROTO_MTU = 2048
KISS_MAX_FRAME_SIZE = PROTO_MTU + 1 + KV4P_VENDOR_HEADER_LEN  # 2055


# --------------------------------------------------------------------------------------
# Commands and enums (protocol.h:42-60, 160-170; globals.h:24-27)
# --------------------------------------------------------------------------------------


class RcvCommand(IntEnum):
    """kv4p commands the host sends to the device (Android -> ESP32)."""

    UNKNOWN = 0x00
    HOST_TX_AUDIO = 0x0C
    HOST_DESIRED_STATE = 0x0D


class SndCommand(IntEnum):
    """kv4p commands the device sends to the host (ESP32 -> Android)."""

    UNKNOWN = 0x00
    DEBUG_INFO = 0x01
    DEBUG_ERROR = 0x02
    DEBUG_WARN = 0x03
    DEBUG_DEBUG = 0x04
    DEBUG_TRACE = 0x05
    HELLO = 0x06
    WINDOW_UPDATE = 0x09
    DEVICE_STATE = 0x0B
    RX_AUDIO = 0x0C


class DeviceMode(IntEnum):
    """The device's audio-path mode, reported in ``DeviceState.mode``."""

    TX = 0
    RX = 1
    STOPPED = 2


class DeviceStateError(IntEnum):
    """The last error the device hit applying a desired state (``DeviceState.last_error``)."""

    NONE = 0
    RADIO_CONFIG_FAILED = 1
    FILTERS_FAILED = 2


class RfModuleType(IntEnum):
    """RF module fitted, reported in ``Version.rf_module_type`` (globals.h:24-27)."""

    SA818_VHF = 0
    SA818_UHF = 1


class FeatureFlag(IntFlag):
    """Optional firmware features advertised in ``Version.features`` (protocol.h:73-75)."""

    HAS_HL = 1 << 0
    HAS_PHY_PTT = 1 << 1
    HAS_ESP32_AFSK = 1 << 2


# --------------------------------------------------------------------------------------
# State flags (protocol.h:77-114)
# --------------------------------------------------------------------------------------


class HostStateFlag(IntFlag):
    """Flags the host sets in ``HostDesiredState.flags`` (protocol.h:77-86).

    PTT is ``PTT_REQUESTED`` here — a flag inside the desired-state struct, not a
    serial control line (guardrail 2, ADR 0002).
    """

    RADIO_CONFIG_VALID = 1 << 0
    PTT_REQUESTED = 1 << 1
    RX_AUDIO_OPEN = 1 << 2
    HIGH_POWER = 1 << 3
    RSSI_ENABLED = 1 << 4
    FILTER_PRE = 1 << 5
    FILTER_HIGH = 1 << 6
    FILTER_LOW = 1 << 7
    TX_ALLOWED = 1 << 11
    ENABLE_STATUS_REPORTS = 1 << 12


class DeviceStateFlag(IntFlag):
    """Flags the device reports in ``DeviceState.flags`` (protocol.h:88-100).

    Shares the low bits with :class:`HostStateFlag` and adds device-only status bits.
    ``SQUELCHED`` is the real hardware busy line that lets ``audio.squelch = "cat"``
    become valid for this backend (unlike the AIOC/UV-5R — see ADR 0061).
    """

    RADIO_CONFIG_VALID = 1 << 0
    PTT_REQUESTED = 1 << 1
    RX_AUDIO_OPEN = 1 << 2
    HIGH_POWER = 1 << 3
    RSSI_ENABLED = 1 << 4
    FILTER_PRE = 1 << 5
    FILTER_HIGH = 1 << 6
    FILTER_LOW = 1 << 7
    PHYS_PTT_DOWN = 1 << 8
    TX_ACTIVE = 1 << 9
    SQUELCHED = 1 << 10
    TX_ALLOWED = 1 << 11
    ENABLE_STATUS_REPORTS = 1 << 12


#: The host-state flags split into two masks (protocol.h:102-114). The NEXT cycle needs
#: this split: session flags (RX audio + status reports) are per-connection and reset on
#: reconnect; global flags (config/PTT/power/filters) persist across the link. Recorded
#: now so the reconciler cycle can honour it.
HOST_STATE_SESSION_FLAG_MASK: HostStateFlag = (
    HostStateFlag.RX_AUDIO_OPEN | HostStateFlag.ENABLE_STATUS_REPORTS
)
HOST_STATE_GLOBAL_FLAG_MASK: HostStateFlag = (
    HostStateFlag.RADIO_CONFIG_VALID
    | HostStateFlag.PTT_REQUESTED
    | HostStateFlag.HIGH_POWER
    | HostStateFlag.RSSI_ENABLED
    | HostStateFlag.FILTER_PRE
    | HostStateFlag.FILTER_HIGH
    | HostStateFlag.FILTER_LOW
    | HostStateFlag.TX_ALLOWED
)


# --------------------------------------------------------------------------------------
# Struct codecs (protocol.h:63-212)
#
# Format strings use explicit '<' (little-endian + no native padding, matching
# [[gnu::packed]]). 'b' is a signed char (Xtensa `char`); 'B' unsigned; 'H'/'I' the
# uint16/uint32 fields; 'i' the int32 memoryId; 'f' the 32-bit floats.
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Version:
    """Firmware/hardware identity, the first half of a HELLO (protocol.h:63-71)."""

    ver: int
    radio_module_status: int  # C `char`, signed on Xtensa
    window_size: int
    rf_module_type: int
    min_radio_freq: float
    max_radio_freq: float
    features: int

    _FORMAT: ClassVar[str] = "<HbIBffB"
    SIZE: ClassVar[int] = struct.calcsize("<HbIBffB")  # 17

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT,
            self.ver,
            self.radio_module_status,
            self.window_size,
            self.rf_module_type,
            self.min_radio_freq,
            self.max_radio_freq,
            self.features,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "Version":
        return cls(*struct.unpack(cls._FORMAT, data))


@dataclass(frozen=True)
class HostDesiredState:
    """The whole state the host wants the device in (protocol.h:172-182).

    Sent with a monotonically increasing ``sequence``; the device applies it and echoes
    the applied sequence back in :class:`DeviceState`. This is a *reconciler*, not a
    command protocol — there is no "set frequency" call.
    """

    sequence: int
    memory_id: int
    flags: int
    bw: int
    freq_tx: float
    freq_rx: float
    ctcss_tx: int
    squelch: int
    ctcss_rx: int

    _FORMAT: ClassVar[str] = "<IiHBffBBB"
    SIZE: ClassVar[int] = struct.calcsize("<IiHBffBBB")  # 22

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT,
            self.sequence,
            self.memory_id,
            self.flags,
            self.bw,
            self.freq_tx,
            self.freq_rx,
            self.ctcss_tx,
            self.squelch,
            self.ctcss_rx,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "HostDesiredState":
        return cls(*struct.unpack(cls._FORMAT, data))


@dataclass(frozen=True)
class DeviceState:
    """The device's reported state (protocol.h:185-199).

    ``applied_sequence`` echoes the last :class:`HostDesiredState` the device applied.
    ``flags`` carries :class:`DeviceStateFlag` (including ``SQUELCHED``); ``mode`` is a
    :class:`DeviceMode`; ``last_error`` a :class:`DeviceStateError`.
    """

    applied_sequence: int
    memory_id: int
    flags: int
    bw: int
    freq_tx: float
    freq_rx: float
    ctcss_tx: int
    squelch: int
    ctcss_rx: int
    radio_module_status: int  # C `char`, signed on Xtensa
    mode: int
    last_error: int
    latest_rssi: int

    _FORMAT: ClassVar[str] = "<IiHBffBBBbBBB"
    SIZE: ClassVar[int] = struct.calcsize("<IiHBffBBBbBBB")  # 26

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT,
            self.applied_sequence,
            self.memory_id,
            self.flags,
            self.bw,
            self.freq_tx,
            self.freq_rx,
            self.ctcss_tx,
            self.squelch,
            self.ctcss_rx,
            self.radio_module_status,
            self.mode,
            self.last_error,
            self.latest_rssi,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "DeviceState":
        return cls(*struct.unpack(cls._FORMAT, data))


@dataclass(frozen=True)
class Hello:
    """COMMAND_HELLO payload: a :class:`Version` followed by an initial :class:`DeviceState`
    (protocol.h:202-206). Packed with no padding between the two sub-structs."""

    version: Version
    device_state: DeviceState

    SIZE: ClassVar[int] = Version.SIZE + DeviceState.SIZE  # 43

    def pack(self) -> bytes:
        return self.version.pack() + self.device_state.pack()

    @classmethod
    def unpack(cls, data: bytes) -> "Hello":
        if len(data) != cls.SIZE:
            raise ValueError(f"Hello is {cls.SIZE} bytes, got {len(data)}")
        return cls(
            Version.unpack(data[: Version.SIZE]),
            DeviceState.unpack(data[Version.SIZE :]),
        )


@dataclass(frozen=True)
class WindowUpdate:
    """Flow-control ack (protocol.h:209-211).

    The device sends this after each frame it decodes, carrying the *encoded* byte
    length of that frame (escaped, both FENDs included) — not the decoded payload
    length. Window accounting in the reader cycle must count encoded bytes; this codec
    just carries the ``size`` value.
    """

    size: int

    _FORMAT: ClassVar[str] = "<I"
    SIZE: ClassVar[int] = struct.calcsize("<I")  # 4

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.size)

    @classmethod
    def unpack(cls, data: bytes) -> "WindowUpdate":
        return cls(*struct.unpack(cls._FORMAT, data))


# --------------------------------------------------------------------------------------
# KISS encode
# --------------------------------------------------------------------------------------


def _escape(payload: bytes) -> bytes:
    """Escape FEND/FESC within a frame body (protocol.h:223-233)."""
    out = bytearray()
    for b in payload:
        if b == KISS_FEND:
            out += bytes((KISS_FESC, KISS_TFEND))
        elif b == KISS_FESC:
            out += bytes((KISS_FESC, KISS_TFESC))
        else:
            out.append(b)
    return bytes(out)


def build_kiss_frame(command_byte: int, payload: bytes) -> bytes:
    """Build a raw KISS frame: ``FEND | command_byte | escape(payload) | FEND``.

    The command byte is written un-escaped, matching the firmware writer
    (protocol.h:218-221); the KISS command/port values we use are never FEND/FESC.
    """
    return bytes((KISS_FEND, command_byte & 0xFF)) + _escape(payload) + bytes((KISS_FEND,))


def build_vendor_frame(kv4p_command: int, payload: bytes = b"") -> bytes:
    """Build a kv4p vendor frame (SETHARDWARE) carrying ``kv4p_command`` + ``payload``.

    Envelope: ``FEND 0x06 "KV4P" 0x01 <kv4pCommand> <payload> FEND`` (protocol.h:298-316).
    The whole vendor header and payload are escaped as the KISS body.
    """
    body = KV4P_VENDOR_PREFIX + bytes((KV4P_PROTOCOL_VERSION, kv4p_command & 0xFF)) + payload
    return build_kiss_frame(KISS_CMD_SETHARDWARE, body)


# --------------------------------------------------------------------------------------
# KISS streaming decode
# --------------------------------------------------------------------------------------


class KissDecoder:
    """Streaming KISS deframer, mirroring the firmware parser (protocol.h:392-515).

    Fed arbitrary byte chunks off a serial read via :meth:`feed`, it returns zero or
    more complete *decoded* frames (each the leading KISS command byte followed by the
    un-escaped payload) and holds any partial remainder for the next call.

    Behaviour matched to the firmware:
      - Bytes before the first FEND are ignored (the firmware prints a plaintext boot
        banner before any frame).
      - FEND both closes the current frame (emitted only if non-empty and not dropped)
        and opens the next; back-to-back FENDs yield nothing.
      - An unknown escape (FESC not followed by TFEND/TFESC) drops the current frame and
        resyncs at the next FEND.
      - A frame that would exceed ``KISS_MAX_FRAME_SIZE`` is dropped, never truncated.

    Split a frame into command/port/payload with :func:`parse_frame`.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._escape = False
        self._drop = False
        self._in_frame = False

    def feed(self, chunk: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for b in chunk:
            frame = self._process(b)
            if frame is not None:
                frames.append(frame)
        return frames

    def reset(self) -> None:
        self._buf.clear()
        self._escape = False
        self._drop = False
        self._in_frame = False

    def _process(self, b: int) -> bytes | None:
        if b == KISS_FEND:
            frame = bytes(self._buf) if (self._buf and not self._drop) else None
            self._buf.clear()
            self._escape = False
            self._drop = False
            self._in_frame = True
            return frame
        if not self._in_frame or self._drop:
            return None
        if self._escape:
            self._escape = False
            if b == KISS_TFEND:
                self._append(KISS_FEND)
            elif b == KISS_TFESC:
                self._append(KISS_FESC)
            else:
                # Unknown escape: drop this frame, resync at the next FEND.
                self._drop = True
            return None
        if b == KISS_FESC:
            self._escape = True
            return None
        self._append(b)
        return None

    def _append(self, b: int) -> None:
        if len(self._buf) >= KISS_MAX_FRAME_SIZE:
            self._drop = True  # oversize: drop, do not truncate
            return
        self._buf.append(b)


# --------------------------------------------------------------------------------------
# Frame dispatch (protocol.h:471-502)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VendorFrame:
    """A decoded kv4p vendor frame: a command byte and its (already de-enveloped) payload."""

    command: int
    payload: bytes


@dataclass(frozen=True)
class Ax25Frame:
    """A decoded standard KISS DATA frame carrying an AX.25 packet.

    Exposed for the future text-over-RF arc; this cycle parses and surfaces it but does
    nothing with the payload. It is a SEPARATE dispatch path from vendor commands.
    """

    payload: bytes


def parse_frame(frame: bytes) -> VendorFrame | Ax25Frame | None:
    """Dispatch one decoded frame (from :meth:`KissDecoder.feed`).

    Returns a :class:`VendorFrame` (kv4p command), an :class:`Ax25Frame` (KISS DATA), or
    ``None`` when the frame is not for us — a non-zero KISS port, an unknown KISS command,
    an empty/oversize DATA payload, or a vendor frame with a bad ``"KV4P"`` prefix or wrong
    protocol version. Malformed frames are ignored, never raised (protocol.h:471-502).
    """
    if not frame:
        return None
    command_byte = frame[0]
    port = command_byte >> 4
    kiss_command = command_byte & 0x0F
    payload = frame[1:]

    if port != KISS_PORT_0:
        return None
    if kiss_command == KISS_CMD_DATA:
        if 0 < len(payload) <= PROTO_MTU:
            return Ax25Frame(payload)
        return None
    if kiss_command == KISS_CMD_SETHARDWARE:
        return _parse_vendor(payload)
    return None


def _parse_vendor(payload: bytes) -> VendorFrame | None:
    if len(payload) < KV4P_VENDOR_HEADER_LEN:
        return None
    if payload[:4] != KV4P_VENDOR_PREFIX:
        return None
    if payload[4] != KV4P_PROTOCOL_VERSION:
        return None
    command = payload[5]
    return VendorFrame(command, payload[KV4P_VENDOR_HEADER_LEN:])
