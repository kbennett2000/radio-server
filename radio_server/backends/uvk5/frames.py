"""Pure wire codec for the Quansheng Dock UART protocol (ADR 0110).

The UV-K5 backend rides the **stock Quansheng UART framing** with the ``ENABLE_DOCK``
``0x08xx`` command set layered on top. This module is the framer/deframer plus the
on-wire struct codecs. It imports nothing but the stdlib and performs no serial I/O.

Source of truth — the wire protocol is a *source fact, not a hardware fact*, so it is
pinned to the exact releases that will be flashed and read as a **specification only**
(no C or C# is pasted or line-by-line ported; every claim cites file:line):

- Firmware ``nicsure/quansheng-dock-fw`` tag **0.32.21q**, commit
  ``4375c3e9604ee4c14ec4bdae67af077879a96f34`` (Apache-2.0):
    * ``app/uart.c`` — ``Header_t``/``Footer_t`` framing, the 16-byte ``Obfuscation``
      table, the receive parser ``UART_IsCommandAvailable`` (uart.c:949-1040), the
      transmit ``SendReply`` (uart.c:251-283), and the dock command structs/dispatch.
    * ``driver/crc.c`` — ``CRC_Calculate``: CRC-16/CCITT, IV 0, no reflection, no
      final XOR (crc.c:21-47) — i.e. CRC-16/XMODEM.
- Client ``nicsure/QuanshengDock`` tag **0.32.21q**, commit
  ``851efa955740db9251811cc90195e927b52ba68c`` (GPL-2.0), read as a spec for the host
  side:
    * ``Serial/Comms.cs`` — the authoritative encoder ``SendCommand2`` (Comms.cs:389-482),
      the streaming decoder ``ByteIn`` (Comms.cs:152-220), the ``Crc16`` routine
      (Comms.cs:63-76) and the ``xor_array`` (Comms.cs:39, == firmware ``Obfuscation``).
    * ``Serial/Packet.cs`` — the command/reply opcode constants (Packet.cs:12-37).

Frame layout (uart.c:264-282, Comms.cs:392-456)::

    [0xAB 0xCD]  [Size:u16 LE]  [ obf( payload[Size] + CRC16[2] ) ]  [0xDC 0xBA]
     preamble     payload len          XOR-scrambled body               footer

``Size`` counts the payload only; total wire length is ``Size + 8`` (uart.c:986). The
``payload`` is itself ``[opcode:u16 LE][param_len:u16 LE][params…]`` — an inner
``Header_t`` whose ``ID`` is the command/reply opcode and whose ``Size`` is the param
length (uart.c:55-58, Comms.cs:394-443). CRC-16 is computed over the plaintext payload
and the obfuscation XOR (indexed ``table[i % 16]``) covers ``Size + 2`` bytes — the
payload *and* the two CRC bytes (uart.c:1030-1039, Comms.cs:445-451).

Direction asymmetry on the two trailing bytes before the footer (see ADR 0110):

- **Host → radio commands** carry a real CRC-16 there; the firmware parser validates it
  and drops the frame on mismatch (uart.c:1037-1039). :func:`build_frame` produces this.
- **Radio → host replies** put ``obf(0xFF 0xFF)`` there — a *dummy*, not a CRC
  (``SendReply`` footer padding, uart.c:270-279) — and the client's own decoder simply
  consumes and ignores those two bytes (Comms.cs:181-186). :class:`Uvk5Decoder` mirrors
  that: it does not validate the CRC by default (``validate_crc=False``), so real replies
  decode; pass ``validate_crc=True`` to enforce the firmware parser's stricter rule.

Struct codecs are frozen dataclasses over the *param* region (the fields after the inner
``Header_t``); each carries a ``struct`` format with an explicit ``<`` and a ``SIZE``
asserted by :mod:`tests.test_uvk5_frames` against the documented C layout. All multi-byte
fields are little-endian; the DP32G030 is little-endian and the C structs are naturally
packed with no implicit padding for these field orders.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import ClassVar

# ---------------------------------------------------------------------------------------
# Framing constants (uart.c:55-63, 232-235, 264-282; Comms.cs:39, 392-456)
# ---------------------------------------------------------------------------------------

#: Frame preamble — ``Header_t.ID = 0xCDAB`` sent little-endian (uart.c:56, 264; the
#: receive parser syncs on ``0xAB`` then requires ``0xCD``, uart.c:963-978).
PREAMBLE = b"\xab\xcd"

#: Frame terminator — ``Footer_t.ID = 0xBADC`` sent little-endian (uart.c:62, 280; the
#: parser requires the tail ``0xDC 0xBA``, uart.c:998).
FOOTER = b"\xdc\xba"

#: The 16-byte XOR obfuscation table (uart.c:232-235; identical to the client's
#: ``xor_array``, Comms.cs:39). ``body[i] ^= OBFUSCATION[i % 16]`` — its own inverse.
OBFUSCATION = bytes(
    (0x16, 0x6C, 0x14, 0xE6, 0x2E, 0x91, 0x0D, 0x40,
     0x21, 0x35, 0xD5, 0x40, 0x13, 0x03, 0xE9, 0x80)
)

#: Non-payload bytes in a frame: 2 preamble + 2 length + 2 CRC + 2 footer. The parser's
#: ``Size + 8`` completeness/bounds test (uart.c:986, 992).
FRAME_OVERHEAD = 8

#: Largest payload the firmware will buffer. ``UART_Command`` is a 256-byte union whose
#: de-obfuscation loop writes ``Size + 2`` bytes into it (uart.c:237-245, 1033-1034), so
#: ``Size <= 254``. The streaming decoder drops (never truncates) anything larger and
#: resyncs. The absolute cap is also bounded by the DMA ring the parser reads from
#: (``sizeof(UART_DMA_Buffer)``, uart.c:986) — that size lives in a header not read here.
MAX_PAYLOAD_SIZE = 254

#: Inner header (opcode + param length) that prefixes every payload (uart.c:55-58).
_INNER_HEADER = struct.Struct("<HH")


class DockCommand(IntEnum):
    """Dock command (host→radio) and reply (radio→host) opcodes.

    Values are the inner ``Header_t.ID``. Command opcodes are the ``ENABLE_DOCK`` cases
    in ``UART_HandleCommand`` (uart.c:1098-1137) and the client's ``Packet`` constants
    (Packet.cs:14-30); reply opcodes are the ``Reply.Header.ID`` assignments in the
    matching firmware handlers.
    """

    # Host → radio
    HELLO = 0x0514           # init / version request; disables obfuscation (uart.c:1024)
    KEYPRESS = 0x0801        # simulate a keypress (uart.c:1099, CMD_0801_t)
    GET_SCREEN = 0x0803      # request 1024-byte LCD framebuffer (uart.c:1103)
    SCAN = 0x0808            # spectrum scan (uart.c:1107, CMD_0808_t)
    SCAN_ADJUST = 0x0809     # adjust an in-progress scan (uart.c:851; Packet.cs:19)
    WRITE_REGISTERS = 0x0850  # write BK4819 registers (uart.c:1111, CMD_085X_t)
    READ_REGISTERS = 0x0851  # read BK4819 registers (uart.c:1115)
    WRITE_GPIO = 0x0860      # set/clear GPIO bits (uart.c:1119, CMD_086X_t)
    READ_GPIO = 0x0861       # read GPIO bits (uart.c:1123)
    #: Defined as ``CMD_0872_t`` (uart.c:208-212) but **not** wired into the 0.32.21q
    #: dispatch switch (uart.c:1098-1137 has no ``0x0872`` case). Kept for completeness;
    #: verify it dispatches before relying on it — see ADR 0110.
    #:
    #: Named ``STOCK_``-anything because the plain name belongs to the fork's `0x0877`
    #: below, which is the one that actually reaches a radio. This one is stock, undispatched,
    #: and has never been sent by this server (ADR 0150).
    STOCK_SET_MODULATION = 0x0872
    ENTER_HW_MODE = 0x0870   # enter full-control ("hardware") mode (uart.c:1127/672-739)
    EXIT_HW_MODE = 0x0871    # exit full-control mode; RestoreRadio (uart.c:684-685, 737)
    #: Stock EEPROM access and reset. Present and dispatched in the firmware **already on the
    #: radio** — ADR 0137 refused this path over a 6-second TX lockout that only matters if you
    #: transmit immediately, and over "no channel-select opcode", which is true and beside the
    #: point: you do not select a channel, you write the VFO (ADR 0141).
    EEPROM_READ = 0x051B      # read EEPROM -> 0x051C (uart.c CMD_051B)
    EEPROM_READ_REPLY = 0x051C
    EEPROM_WRITE = 0x051D     # write EEPROM in 8-byte chunks -> 0x051E (uart.c CMD_051D)
    EEPROM_WRITE_REPLY = 0x051E
    RESET = 0x05DD            # NVIC_SystemReset — a soft reboot over the wire (uart.c:1068)
    #: Set the radio's OWN VFO — a **fork extension** (F6), not stock Quansheng. Stock has
    #: nothing here; ``0x0872`` was avoided deliberately because it is the stock
    #: ``CMD_0872_t`` above. Only the custom firmware answers this, which is why the backend
    #: probes for it rather than assuming (see ``Uvk5Radio``).
    SET_VFO = 0x0873
    JET_SCAN = 0x0888        # one-pass fast peak scan (uart.c:1131, CMD_0888_t)
    #: Reply to :data:`SET_VFO` — a fork extension alongside it. The first draft of ``0x0873``
    #: had five ways to do nothing and no way to say so, which made "refused" and "applied" the
    #: same event on the wire; this carries a status and the frequencies the radio actually
    #: landed on. Only firmware from F6 onward sends it.
    SET_VFO_REPLY = 0x0874
    #: Set the radio's demodulator — a **fork extension** (F7), answered by
    #: :data:`SET_MODULATION_REPLY`. **0x0877, not the 0x0875 the obvious next number suggests.**
    #: ADR 0111:52 records the classic Dock's extended set as "0x0872 modulation, 0x0873/4
    #: backlight, 0x0875/6 AM emulation", so 0x0875/6 is *claimed*. That census cannot be
    #: re-verified here (nicsure's source is not vendored), so it is treated as claimed rather
    #: than assumed free — which is precisely the check :data:`SET_VFO`'s own allocation skipped:
    #: ADR 0140 reasoned only about ``0x0872`` and took a pair the same census had spoken for.
    #: That one is shipped on hardware and cannot be walked back. This one was cheap to place
    #: correctly (ADR 0150).
    SET_MODULATION = 0x0877
    #: Reply to :data:`SET_MODULATION`, carrying the demodulator the radio is **actually** on —
    #: read back out of its own VFO after the firmware applied it, not the value it was handed.
    #: Only firmware from F7 onward sends it.
    SET_MODULATION_REPLY = 0x0878
    #: Drive the radio's **second receiver** — a **fork extension** (F8), answered by
    #: :data:`SET_BROADCAST_FM_REPLY`. The BK1080 is a separate commercial-FM chip (64-108 MHz)
    #: sharing the antenna front end and the audio amplifier with the BK4819 and nothing else, so
    #: :data:`WRITE_REGISTERS`/:data:`READ_REGISTERS` cannot reach it: those are BK4819 register
    #: access and the BK1080's registers are not in that address space. Hence an opcode.
    #:
    #: Allocated after re-running the same three-way census :data:`SET_MODULATION` ran — ADR 0111:52,
    #: ADR 0119:43 and this enum, which still do not agree and which nothing reconciles. ``0x0875/6``
    #: stays *claimed* (AM emulation, per the first source) and therefore skipped; ``0x0879/A`` is
    #: free in all three and in both trees.
    SET_BROADCAST_FM = 0x0879
    #: Reply to :data:`SET_BROADCAST_FM`, carrying the state the receiver is **actually** in, read
    #: back out of ``gFmRadioMode``/``gEeprom.FM_FrequencyPlaying`` after the firmware applied it.
    #: Only firmware from F8 onward sends it — and as of ADR 0157 that firmware is unmerged, so a
    #: silent ``0x0879`` is the normal answer, not a fault.
    SET_BROADCAST_FM_REPLY = 0x087A

    # Radio → host
    IM_HERE = 0x0515         # version/challenge reply to HELLO (uart.c:289, SendVersion)
    SCAN_REPLY = 0x0908      # spectrum batch (uart.c:887; Packet.cs:20)
    REGISTER_INFO = 0x0951   # one per read register (uart.c:585)
    GPIO_INFO = 0x0961       # one per read GPIO (uart.c:629)
    JET_SCAN_REPLY = 0x0988  # jet-scan peaks (uart.c:794)


# ---------------------------------------------------------------------------------------
# CRC-16 and obfuscation (crc.c:21-47; Comms.cs:62-76, 445-451)
# ---------------------------------------------------------------------------------------


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM over *data* (poly 0x1021, init 0, no reflection, no final XOR).

    The firmware computes this in hardware (``CRC_16_CCITT``, IV 0, normal in/out,
    crc.c:21-47); the client's software ``Crc16`` (Comms.cs:63-76) is the reference
    bytewise form. This is a clean-room implementation of the standard algorithm.
    """
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc <<= 1
            if crc & 0x10000:
                crc ^= 0x1021
            crc &= 0xFFFF
    return crc


def obfuscate(data: bytes) -> bytes:
    """XOR *data* with the obfuscation table (uart.c:1033-1034). Its own inverse."""
    return bytes(b ^ OBFUSCATION[i % 16] for i, b in enumerate(data))


# ``deobfuscate`` reads better at call sites in the decoder; it is the same operation.
deobfuscate = obfuscate


# ---------------------------------------------------------------------------------------
# Framing: build_frame / Uvk5Decoder
# ---------------------------------------------------------------------------------------


def build_frame(command: int, params: bytes = b"", *, obfuscate_body: bool = True) -> bytes:
    """Assemble one wire frame for *command* carrying *params*.

    Mirrors the client encoder ``SendCommand2`` (Comms.cs:389-456): the payload is
    ``[opcode:u16][param_len:u16][params]``; a CRC-16 over that payload is appended; the
    payload+CRC block is XOR-obfuscated; the whole is wrapped in preamble + length +
    footer. ``obfuscate_body=False`` emits the plaintext form the firmware uses only for
    the ``0x0514`` HELLO exchange (uart.c:1024-1035); normal operation is obfuscated.
    """
    payload = _INNER_HEADER.pack(command, len(params)) + params
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"payload {len(payload)} exceeds MAX_PAYLOAD_SIZE {MAX_PAYLOAD_SIZE}")
    body = payload + struct.pack("<H", crc16(payload))
    if obfuscate_body:
        body = obfuscate(body)
    return PREAMBLE + struct.pack("<H", len(payload)) + body + FOOTER


class Uvk5Decoder:
    """Streaming deframer that mirrors the firmware/client parser's acceptance rules.

    ``feed(chunk)`` returns the list of de-obfuscated payloads (``[opcode][param_len]
    [params]``) completed by the bytes in *chunk*; feed pass it to :func:`parse_frame`.
    Modelled on the client's ``ByteIn`` state machine (Comms.cs:152-194): it syncs on the
    preamble, reads the length, collects exactly ``Size`` payload bytes plus two trailing
    (CRC/padding) bytes, and requires the ``0xDC 0xBA`` footer. Anything malformed — a
    bad preamble second byte, a missing footer, or an over-length frame — is dropped and
    the stream resyncs at the next ``0xAB``; malformed input never raises. Over-length
    frames (``Size > MAX_PAYLOAD_SIZE``) are dropped, never truncated.

    By default the trailing two bytes are ignored (``validate_crc=False``), matching the
    client decoder and the reality that firmware *replies* carry a dummy CRC. Set
    ``validate_crc=True`` to enforce the firmware receive parser's rule (uart.c:1037-1039)
    and drop frames whose CRC does not match — appropriate when decoding *commands*.
    """

    # Parser stages (Comms.cs:148).
    _IDLE, _CD, _LEN_LO, _LEN_HI, _DATA, _CRC_LO, _CRC_HI, _DC, _BA = range(9)

    def __init__(self, *, obfuscated: bool = True, validate_crc: bool = False) -> None:
        self._obfuscated = obfuscated
        self._validate_crc = validate_crc
        self.reset()

    def reset(self) -> None:
        """Discard any partially-collected frame and return to the idle/sync state."""
        self._stage = self._IDLE
        self._size = 0
        self._buf = bytearray()
        self._crc = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        frames: list[bytes] = []
        for b in chunk:
            payload = self._step(b)
            if payload is not None:
                frames.append(payload)
        return frames

    def _step(self, b: int) -> bytes | None:
        stage = self._stage
        if stage == self._IDLE:
            if b == PREAMBLE[0]:
                self._stage = self._CD
            return None
        if stage == self._CD:
            self._stage = self._LEN_LO if b == PREAMBLE[1] else self._IDLE
            return None
        if stage == self._LEN_LO:
            self._size = b
            self._stage = self._LEN_HI
            return None
        if stage == self._LEN_HI:
            self._size |= b << 8
            self._buf = bytearray()
            self._crc = bytearray()
            if self._size == 0 or self._size > MAX_PAYLOAD_SIZE:
                # Zero-length has no opcode; over-length would overrun the firmware
                # buffer. Drop and resync rather than buffer garbage.
                self._stage = self._IDLE
            else:
                self._stage = self._DATA
            return None
        if stage == self._DATA:
            self._buf.append(b)
            if len(self._buf) >= self._size:
                self._stage = self._CRC_LO
            return None
        if stage == self._CRC_LO:
            self._crc.append(b)
            self._stage = self._CRC_HI
            return None
        if stage == self._CRC_HI:
            self._crc.append(b)
            self._stage = self._DC
            return None
        if stage == self._DC:
            self._stage = self._BA if b == FOOTER[0] else self._IDLE
            return None
        # stage == self._BA
        self._stage = self._IDLE
        if b != FOOTER[1]:
            return None
        return self._finish()

    def _finish(self) -> bytes | None:
        body = bytes(self._buf) + bytes(self._crc)
        if self._obfuscated:
            body = deobfuscate(body)
        payload, crc_bytes = body[: self._size], body[self._size :]
        if self._validate_crc and struct.unpack("<H", crc_bytes)[0] != crc16(payload):
            return None
        return payload


# ---------------------------------------------------------------------------------------
# Struct codecs — one frozen dataclass per dock command/reply (uart.c:65-227)
#
# Each dataclass covers the *param* region (the bytes after the inner Header_t). Fixed
# structs carry ``_FORMAT``/``SIZE``; ``to_frame`` wraps ``pack()`` via ``build_frame``.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hello:
    """``0x0514`` HELLO / init (uart.c:65-68, CMD_0514_t). ``0x12345678`` = remote-UI."""

    timestamp: int

    COMMAND: ClassVar[int] = DockCommand.HELLO
    _FORMAT: ClassVar[str] = "<I"
    SIZE: ClassVar[int] = struct.calcsize("<I")  # 4

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.timestamp)

    @classmethod
    def unpack(cls, data: bytes) -> "Hello":
        if len(data) != cls.SIZE:
            raise ValueError(f"Hello params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class KeyPress:
    """``0x0801`` simulate keypress (uart.c:151-156, CMD_0801_t).

    ``key`` bits[0-4] = key id, bit 5 (0x20) = click/hold select (uart.c:744-745). The
    client sends a bare ``ushort`` key and lets the firmware read only ``Key`` (Comms.cs
    :118), but the struct is ``Key, Padding, Timestamp`` — modelled faithfully here.
    """

    key: int
    padding: int = 0
    timestamp: int = 0

    COMMAND: ClassVar[int] = DockCommand.KEYPRESS
    _FORMAT: ClassVar[str] = "<BBI"
    SIZE: ClassVar[int] = struct.calcsize("<BBI")  # 6

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.key, self.padding, self.timestamp)

    @classmethod
    def unpack(cls, data: bytes) -> "KeyPress":
        if len(data) != cls.SIZE:
            raise ValueError(f"KeyPress params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class GetScreen:
    """``0x0803`` request the LCD framebuffer (uart.c:158-161, CMD_0803_t).

    The handler ignores its buffer (uart.c:1103-1104) and the client sends no params; the
    struct nonetheless carries a ``Timestamp``, modelled here as an optional field.
    """

    timestamp: int = 0

    COMMAND: ClassVar[int] = DockCommand.GET_SCREEN
    _FORMAT: ClassVar[str] = "<I"
    SIZE: ClassVar[int] = struct.calcsize("<I")  # 4

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.timestamp)

    @classmethod
    def unpack(cls, data: bytes) -> "GetScreen":
        if len(data) != cls.SIZE:
            raise ValueError(f"GetScreen params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class Scan:
    """``0x0808`` spectrum scan request (uart.c:163-169, CMD_0808_t)."""

    mid_freq: int
    width: int
    density: int
    timestamp: int = 0

    COMMAND: ClassVar[int] = DockCommand.SCAN
    _FORMAT: ClassVar[str] = "<IIHI"
    SIZE: ClassVar[int] = struct.calcsize("<IIHI")  # 14

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.mid_freq, self.width, self.density, self.timestamp)

    @classmethod
    def unpack(cls, data: bytes) -> "Scan":
        if len(data) != cls.SIZE:
            raise ValueError(f"Scan params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class StockSetModulation:
    """``0x0872`` set modulation (uart.c:208-212, CMD_0872_t) — **stock, and never dispatched**.

    Defined but not wired into the 0.32.21q switch — see :attr:`DockCommand.STOCK_SET_MODULATION`.
    Nothing in this server sends it and nothing ever has; it is modelled only so the codec covers
    the documented command set (ADR 0110).

    **Not** the way this server changes the demodulator. That is :class:`SetModulation`
    (``0x0877``), a fork extension with a reply — a different opcode, a different payload, and
    the only one of the two that reaches a radio (ADR 0150).
    """

    length: int
    mode: int

    COMMAND: ClassVar[int] = DockCommand.STOCK_SET_MODULATION
    _FORMAT: ClassVar[str] = "<HH"
    SIZE: ClassVar[int] = struct.calcsize("<HH")  # 4

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.length, self.mode)

    @classmethod
    def unpack(cls, data: bytes) -> "StockSetModulation":
        if len(data) != cls.SIZE:
            raise ValueError(f"StockSetModulation params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class JetScan:
    """``0x0888`` one-pass fast peak scan request (uart.c:214-219, CMD_0888_t)."""

    start_freq: int
    end_freq: int
    step: int

    COMMAND: ClassVar[int] = DockCommand.JET_SCAN
    _FORMAT: ClassVar[str] = "<III"
    SIZE: ClassVar[int] = struct.calcsize("<III")  # 12

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.start_freq, self.end_freq, self.step)

    @classmethod
    def unpack(cls, data: bytes) -> "JetScan":
        if len(data) != cls.SIZE:
            raise ValueError(f"JetScan params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class EnterHwMode:
    """``0x0870`` enter full-control ("hardware") mode — no params (uart.c:672-739).

    Suspends the radio's own logic in a serial-command loop until an :class:`ExitHwMode`
    (``0x0871``); the host then drives the BK4819 directly. No reply.
    """

    COMMAND: ClassVar[int] = DockCommand.ENTER_HW_MODE

    def pack(self) -> bytes:
        return b""

    @classmethod
    def unpack(cls, data: bytes) -> "EnterHwMode":
        if data:
            raise ValueError(f"EnterHwMode takes no params, got {len(data)} bytes")
        return cls()

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


#: ``CMD_051D`` writes in fixed 8-byte chunks — ``EEPROM_WriteBuffer(Offset + i*8, &Data[i*8])`` —
#: so a payload that is not a multiple of 8 silently loses its tail. Enforced, not documented.
EEPROM_CHUNK = 8

#: The settings block. **Not** the classic UV-K5 ``0x0E70``: on this V3 tree ``settings.c`` bypasses
#: the EEPROM compat layer and uses raw flash (``PY25Q16_WriteBuffer(0x00A130…)``, ``0x00A150``,
#: ``0x00A158``), and ``eeprom_compat.c`` maps host EEPROM ``0xA000..0xA170`` onto flash ``0xA000``
#: identity. ``0x0E70`` falls in the identity-mapped *channel* region instead, so reading it returns
#: unprogrammed 0xFF — which looks exactly like a settings block full of defaults. Verified by
#: reading both (ADR 0141).
EEPROM_SETTINGS_BLOCK = 0xA000

#: ``gEeprom.DUAL_WATCH`` — the ``0x0E78`` half of the block, index 4. Dual watch alternates
#: ``gRxVfo``, and ``RADIO_SelectCurrentVfo`` makes ``gCurrentVfo`` follow it, so with it on *which
#: VFO the radio transmits from is decided by a timer*.
#:
#: ``settings.c:173`` reads it as ``(Data[4] < 3) ? Data[4] : DUAL_WATCH_CHAN_A`` — so an
#: unprogrammed ``0xFF`` does not mean "off", it means **on**. That is the shipped default.
EEPROM_DUAL_WATCH = 0xA00C
#: The 8-byte chunk that contains it, which is the unit a write has to work in.
EEPROM_SETTINGS_CHUNK = 0xA008
DUAL_WATCH_OFF = 0


@dataclass(frozen=True)
class EepromRead:
    """``0x051B`` read ``size`` bytes of EEPROM at ``offset`` → one :class:`EepromReadReply`.

    ``timestamp`` must match the value the host itself established with :class:`Hello`; the firmware
    drops the frame silently otherwise (``uart.c`` ``CMD_051B``), so a session that skipped the
    handshake looks exactly like a radio that is not listening.
    """

    offset: int
    size: int
    timestamp: int
    padding: int = 0

    COMMAND: ClassVar[int] = DockCommand.EEPROM_READ
    _FORMAT: ClassVar[str] = "<HBBI"
    SIZE: ClassVar[int] = struct.calcsize("<HBBI")  # 8

    def __post_init__(self) -> None:
        if not 0 <= self.offset <= 0xFFFF:
            raise ValueError(f"offset {self.offset} is outside the 16-bit EEPROM space")
        if not 1 <= self.size <= 128:
            raise ValueError(f"size must be 1..128 (REPLY_051B's buffer), got {self.size}")

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.offset, self.size, self.padding, self.timestamp)

    @classmethod
    def unpack(cls, data: bytes) -> "EepromRead":
        if len(data) != cls.SIZE:
            raise ValueError(f"EepromRead params are {cls.SIZE} bytes, got {len(data)}")
        offset, size, padding, timestamp = struct.unpack(cls._FORMAT, data)
        return cls(offset, size, timestamp, padding)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class EepromReadReply:
    """``0x051C`` — ``[offset:u16][size:u8][pad:u8][data:size]``."""

    offset: int
    size: int
    data: bytes
    padding: int = 0

    COMMAND: ClassVar[int] = DockCommand.EEPROM_READ_REPLY

    def pack(self) -> bytes:
        return struct.pack("<HBB", self.offset, self.size, self.padding) + self.data

    @classmethod
    def unpack(cls, data: bytes) -> "EepromReadReply":
        if len(data) < 4:
            raise ValueError(f"EepromReadReply needs at least 4 bytes, got {len(data)}")
        offset, size, padding = struct.unpack("<HBB", data[:4])
        body = data[4:]
        if len(body) < size:
            raise ValueError(f"EepromReadReply claims {size} bytes, carries {len(body)}")
        return cls(offset, size, body[:size], padding)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class EepromWrite:
    """``0x051D`` write ``data`` to EEPROM at ``offset`` → one :class:`EepromWriteReply`.

    **Read-modify-write is the caller's job and it is not optional.** The firmware writes whole
    8-byte chunks, so sending fewer bytes than the chunk it lands in overwrites its neighbours with
    whatever the payload happens to contain. `len(data)` is required to be a non-zero multiple of 8
    for that reason.

    Writing also arms ``gSerialConfigCountDown_500ms = 12`` — a **6-second TX lockout** that masks
    PTT and de-keys a transmission in progress (ADR 0137). Irrelevant for a one-time config write
    followed by a settle; fatal if you key immediately after.
    """

    offset: int
    data: bytes
    timestamp: int
    allow_password: int = 0

    COMMAND: ClassVar[int] = DockCommand.EEPROM_WRITE
    _HEADER: ClassVar[str] = "<HBBI"

    def __post_init__(self) -> None:
        if not 0 <= self.offset <= 0xFFFF:
            raise ValueError(f"offset {self.offset} is outside the 16-bit EEPROM space")
        if not self.data or len(self.data) % EEPROM_CHUNK:
            raise ValueError(
                f"data must be a non-zero multiple of {EEPROM_CHUNK} bytes (the firmware writes "
                f"whole chunks and would corrupt the neighbours), got {len(self.data)}"
            )
        if self.offset % EEPROM_CHUNK:
            raise ValueError(
                f"offset {self.offset:#06x} is not {EEPROM_CHUNK}-byte aligned; the firmware writes "
                f"at offset + i*{EEPROM_CHUNK}, so an unaligned start straddles two chunks"
            )

    def pack(self) -> bytes:
        return struct.pack(
            self._HEADER, self.offset, len(self.data), self.allow_password, self.timestamp
        ) + self.data

    @classmethod
    def unpack(cls, data: bytes) -> "EepromWrite":
        if len(data) < 8:
            raise ValueError(f"EepromWrite needs at least 8 bytes, got {len(data)}")
        offset, size, allow, timestamp = struct.unpack(cls._HEADER, data[:8])
        body = data[8:]
        if len(body) < size:
            raise ValueError(f"EepromWrite claims {size} bytes, carries {len(body)}")
        return cls(offset, body[:size], timestamp, allow)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class EepromWriteReply:
    """``0x051E`` — ``[offset:u16]``. Acknowledges the write; carries no data back."""

    offset: int

    COMMAND: ClassVar[int] = DockCommand.EEPROM_WRITE_REPLY
    _FORMAT: ClassVar[str] = "<H"
    SIZE: ClassVar[int] = 2

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.offset)

    @classmethod
    def unpack(cls, data: bytes) -> "EepromWriteReply":
        if len(data) < cls.SIZE:
            raise ValueError(f"EepromWriteReply needs {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data[: cls.SIZE]))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class Reset:
    """``0x05DD`` reboot the radio — no params, **no reply** (the MCU resets mid-frame).

    The way a settings write is made to take effect: ``CMD_051D`` only calls
    ``SETTINGS_InitEEPROM()`` for writes landing in ``0x0F30..0x0F40``, so anything else needs the
    radio to re-read EEPROM at boot.
    """

    COMMAND: ClassVar[int] = DockCommand.RESET

    def pack(self) -> bytes:
        return b""

    @classmethod
    def unpack(cls, data: bytes) -> "Reset":
        if data:
            raise ValueError(f"Reset takes no params, got {len(data)} bytes")
        return cls()

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


#: Offset directions, matching the firmware's ``TX_OFFSET_FREQUENCY_DIRECTION``.
OFFSET_NONE = 0
OFFSET_ADD = 1
OFFSET_SUB = 2

#: Every CTCSS tone the radio knows, in tenths of a Hz — a mirror of ``dcs.c``'s
#: ``CTCSS_Options[50]``, which is literally commented "CTCSS Hz * 10". (50, not the EIA 38:
#: the radio's table includes 12 non-homologated tones.)
#:
#: The wire carries the tone itself rather than an index, so the two sides never have to
#: agree on an ordering. The firmware then matches its own table **exactly** and refuses
#: anything else — no "nearest", because a tone one step off simply fails to open the
#: repeater it was aimed at, which is a silent wrong answer where a refusal is recoverable.
#: This mirror exists so that refusal happens at the caller instead of over the air.
#:
#: Kept **in table order**, not just as a membership set: ``0x0873`` carries the tone itself, but
#: the EEPROM channel format stores its *index* into this exact array, so the order is load-bearing
#: for that path and a reordering would silently retune every tone.
CTCSS_OPTIONS: tuple[int, ...] = (
    670, 693, 719, 744, 770, 797, 825, 854, 885, 915,
    948, 974, 1000, 1035, 1072, 1109, 1148, 1188, 1230, 1273,
    1318, 1365, 1413, 1462, 1514, 1567, 1598, 1622, 1655, 1679,
    1713, 1738, 1773, 1799, 1835, 1862, 1899, 1928, 1966, 1995,
    2035, 2065, 2107, 2181, 2257, 2291, 2336, 2418, 2503, 2541,
)

CTCSS_TENTHS: frozenset[int] = frozenset(CTCSS_OPTIONS)


class SetVfoStatus(IntEnum):
    """The ``0x0874`` status byte. ``APPLIED`` is the only success.

    Each of the others is a way the radio can be **not** on the channel that was asked for. They
    exist because the first cut of ``0x0873`` had all of them and reported none: a caller saw a
    successful write and a radio that had not moved (dock.h, F6).
    """

    APPLIED = 0        #: on the channel, exactly as requested
    ERR_SHORT = 1      #: payload shorter than the parameter set — the host mis-sized its frame
    ERR_BUSY = 2       #: the host holds full-control (``0x0870``); retry after ``0x0871``
    ERR_DIRECTION = 3  #: offset direction was not NONE/ADD/SUB
    ERR_FIELD = 4      #: bandwidth or power off its scale
    ERR_NO_HAL = 5     #: firmware built without the radio-side binding
    ERR_BAND = 6       #: the rx or tx leg falls outside every band this radio has
    ERR_TONE = 7       #: tone absent from the radio's CTCSS table — refused rather than
                       #: transmitted without one, which would leave the repeater shut

    @property
    def ok(self) -> bool:
        return self is SetVfoStatus.APPLIED


@dataclass(frozen=True)
class SetVfoReply:
    """``0x0874`` — what the radio actually did with a :class:`SetVfo`.

    ``[status:u8][power:u8][rx_hz:u32][tx_hz:u32][ctcss_tenths:u16]``, 12 bytes.

    The frequencies are read out of the radio's own VFO struct **after** its ``RADIO_ApplyOffset``,
    so they are what it landed on rather than what it was told. On any non-``APPLIED`` status the
    firmware zeroes them, unconditionally, so a caller can never read a channel off a reply that
    says the radio is not on one — which makes ``status`` authoritative and checkable first.

    ``power`` is the radio's **own** ``OUTPUT_POWER_*`` index (``USER, LOW1..LOW5, MID, HIGH``),
    not the 0/1/2 that :class:`SetVfo` sends. The two scales are reported separately because they
    silently disagreed: the wire's "high" landed on ``OUTPUT_POWER_LOW2``, a level no repeater is
    going to hear, with nothing visible from the host to say so.
    """

    status: SetVfoStatus
    rx_hz: int = 0
    tx_hz: int = 0
    ctcss_tenths: int = 0
    power: int = 0

    COMMAND: ClassVar[int] = DockCommand.SET_VFO_REPLY
    _FORMAT: ClassVar[str] = "<BBIIH"
    SIZE: ClassVar[int] = struct.calcsize("<BBIIH")  # 12

    @property
    def ok(self) -> bool:
        return self.status is SetVfoStatus.APPLIED

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT, int(self.status), self.power,
            self.rx_hz, self.tx_hz, self.ctcss_tenths,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "SetVfoReply":
        if len(data) != cls.SIZE:
            raise ValueError(f"SetVfoReply params are {cls.SIZE} bytes, got {len(data)}")
        status, power, rx, tx, tone = struct.unpack(cls._FORMAT, data)
        # An unknown status must not crash the decode — a newer firmware may add one, and the
        # caller still needs to learn that it was NOT `APPLIED`. Anything unrecognised is
        # therefore surfaced as a plain int, which `.ok` correctly reports as false.
        try:
            status = SetVfoStatus(status)
        except ValueError:
            pass
        return cls(status, rx, tx, tone, power)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class SetVfo:
    """``0x0873`` set the radio's own VFO — a **fork extension** (F6), answered by
    :class:`SetVfoReply`.

    Every other command here writes BK4819 registers, and none of it survives the handoff:
    ``0x0870`` backs the registers up and the ``0x0871`` exit ends in
    ``RADIO_SetupRegisters(true)``, which retunes the synthesiser from the radio's own VFO.
    So a host that tunes by register can never hand the radio a channel and let go — which
    is why 37 correctly-applied, correctly-read-back repeater presets never keyed a machine.

    This sets the VFO the radio actually transmits from, and the firmware's own
    ``RADIO_ApplyOffset`` / ``RADIO_ConfigureSquelchAndOutputPower`` do the split and the
    per-band PA calibration. Frequencies in Hz; ``ctcss_tenths`` is tenths of a Hz (1000 =
    100.0), 0 for none.
    """

    rx_hz: int
    offset_hz: int = 0
    ctcss_tenths: int = 0
    direction: int = OFFSET_NONE
    narrow: int = 0
    power: int = 2

    COMMAND: ClassVar[int] = DockCommand.SET_VFO
    _FORMAT: ClassVar[str] = "<IIHBBB"
    SIZE: ClassVar[int] = struct.calcsize("<IIHBBB")  # 13

    def __post_init__(self) -> None:
        # Validated here as well as in the firmware, deliberately, even though `SetVfoReply` now
        # reports every refusal. A `ValueError` names the offending field at the call site, in a
        # stack that points at the bug; an `ERR_FIELD` three layers away over the air says only
        # that the radio did not like something. The reply is the safety net for what this cannot
        # see (the radio's band edges, its CTCSS table) — not a reason to stop checking here.
        if self.direction not in (OFFSET_NONE, OFFSET_ADD, OFFSET_SUB):
            raise ValueError(f"offset direction must be 0/1/2, got {self.direction}")
        if self.narrow not in (0, 1):
            raise ValueError(f"narrow must be 0 or 1, got {self.narrow}")
        if self.power not in (0, 1, 2):
            raise ValueError(f"power must be 0/1/2, got {self.power}")
        if self.rx_hz <= 0:
            raise ValueError(f"rx_hz must be positive, got {self.rx_hz}")
        if self.ctcss_tenths and self.ctcss_tenths not in CTCSS_TENTHS:
            raise ValueError(
                f"ctcss_tenths {self.ctcss_tenths} is not a tone this radio has "
                f"(dcs.c CTCSS_Options); the firmware would drop it and transmit no tone"
            )
        if self.direction != OFFSET_NONE and self.offset_hz <= 0:
            raise ValueError("a non-simplex direction needs a positive offset_hz")

    @property
    def tx_hz(self) -> int:
        """Where this channel will actually transmit — the same arithmetic the firmware's
        ``RADIO_ApplyOffset`` does, so a caller can assert on it before keying."""
        if self.direction == OFFSET_ADD:
            return self.rx_hz + self.offset_hz
        if self.direction == OFFSET_SUB:
            return self.rx_hz - self.offset_hz
        return self.rx_hz

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT, self.rx_hz, self.offset_hz, self.ctcss_tenths,
            self.direction, self.narrow, self.power,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "SetVfo":
        if len(data) != cls.SIZE:
            raise ValueError(f"SetVfo params are {cls.SIZE} bytes, got {len(data)}")
        rx, off, tone, direction, narrow, power = struct.unpack(cls._FORMAT, data)
        return cls(rx, off, tone, direction, narrow, power)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class SetVfoProbe:
    """A deliberately **empty** ``0x0873`` — asks "do you have this command?" without tuning.

    There is no other way to tell F6 firmware from F2/F3/F5: the dock answers no version string
    (this fork is always-encrypted and dropped the plaintext ``0x0514`` toggle, ADR 0119), and a
    pre-F6 dispatch drops an unknown opcode in silence. So the question has to be asked of the
    command itself, and the only safe way to ask is to send one the firmware must refuse.

    **Why an empty payload cannot tune the radio** (``App/app/dock.c``, the ``DOCK_CMD_SET_VFO``
    case): the length check is the *first* branch — ``plen < DOCK_SET_VFO_PARAM_LEN`` sets
    ``ERR_SHORT`` and falls straight through to the reply. It never reads past the payload, never
    reaches the field decode, and never calls ``hal->set_vfo``. The core then blanks every
    frequency field on any non-zero status, so the reply cannot describe a channel either.

    Any ``0x0874`` at all answers the question — ``ERR_BUSY`` (the host holds ``0x0870``) proves
    the command exists just as well as ``ERR_SHORT``. Silence means pre-F6, or a radio that is not
    listening; the caller has to have proved the link first for the distinction to mean anything.
    """

    COMMAND: ClassVar[int] = DockCommand.SET_VFO

    def pack(self) -> bytes:
        return b""

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


#: ``0x0878`` flags bit 0 — the radio will key its **own** transmit path in this modulation.
#:
#: Built without ``ENABLE_TX_WHEN_AM`` (which the F7 image is not), ``RADIO_PrepareTX`` sets
#: ``VFO_STATE_TX_DISABLE`` for any modulation that is not FM. That is the path the radio's PTT
#: **pin** drives, and the `baofeng` backend keys exactly there by asserting the AIOC's DTR line
#: into it — so on that station a successful set-AM stops the transmitter outright. The dock's own
#: ``0x0850`` REG_30 keying never enters ``RADIO_PrepareTX`` and is unaffected, so the same firmware
#: state means "cannot transmit" on one backend and "transmits normally" on another. A host cannot
#: see a build flag from the far end of a serial cable, so the firmware reports it (ADR 0149/0150).
FLAG_TX_OK = 0x01


class DockModulation(IntEnum):
    """The demodulator values **the wire defines**, deliberately not the firmware's enum.

    The firmware's ``ModulationMode_t`` is ``{FM, AM, USB, [BYP, RAW,] UKNOWN}`` — the bracketed
    pair exists only under ``ENABLE_BYP_RAW_DEMODULATORS``, so the enum's numeric **end moves with
    a build flag**. A wire bound to it would mean different things in two builds of the same
    protocol. Hence a wire scale of its own, mapped explicitly on the firmware side (dock.h).

    Consequence for this codec, and it is the load-bearing one: **decode by explicit value, never
    by position.** Nothing here indexes a list with a byte off the wire.
    """

    FM = 0
    AM = 1
    #: Reserved: the **number** is nailed down so it can never come to mean anything else, but the
    #: **value is refused** at F7 (``ERR_FIELD``). Nobody has put this radio on USB on a bench, it
    #: cannot transmit in it on this build anyway, and a refusal never moves the radio — so
    #: accepting it later is purely additive (guardrail 1).
    USB = 2
    #: Reply-only. The radio is on something this wire cannot name (BYP/RAW on a build that has
    #: them, or anything a future firmware adds), **or** the request was refused.
    #:
    #: ``0xFF`` and emphatically not ``0``: ``0x0874`` blanks its frequencies to zero on a refusal
    #: because 0 Hz is obviously not a channel, but zero here **is** :attr:`FM` — blanking to it
    #: would ship a refusal carrying a plausible claim about where the radio is.
    UNKNOWN = 0xFF


#: Wire value → the name this server and its API use. Explicit, and only the two values a radio
#: will actually accept: anything else — ``USB``, ``UNKNOWN``, a byte a later firmware invents —
#: has no name here and decodes to ``None`` rather than to a neighbour.
MODULATION_NAMES: dict[int, str] = {DockModulation.FM: "FM", DockModulation.AM: "AM"}

#: The inverse, for building a command. ``MODULATION_VALUES["FM"] == 0``.
MODULATION_VALUES: dict[str, int] = {name: value for value, name in MODULATION_NAMES.items()}


class ModulationStatus(IntEnum):
    """The ``0x0878`` status byte. ``APPLIED`` is the only success.

    **The numbers are** :class:`SetVfoStatus`'s, **holes and all**: a caller that already decodes a
    set-VFO status reuses the same table, and "status 4 means a field was off its scale" stays true
    whichever command produced it. ``3`` (DIRECTION), ``6`` (BAND) and ``7`` (TONE) cannot arise
    here and are left unused rather than renumbered — holes are free, a code whose meaning depends
    on which opcode you were looking at is not.
    """

    APPLIED = 0     #: on this modulation, exactly as requested
    ERR_SHORT = 1   #: payload shorter than the parameter set — the host mis-sized its frame
    ERR_BUSY = 2    #: the host holds full-control (``0x0870``); retry after ``0x0871``
    ERR_FIELD = 4   #: not a modulation this firmware accepts (``USB`` included, at F7)
    ERR_NO_HAL = 5  #: firmware built without the radio-side binding

    @property
    def ok(self) -> bool:
        return self is ModulationStatus.APPLIED


@dataclass(frozen=True)
class SetModulationReply:
    """``0x0878`` — what the radio actually did with a :class:`SetModulation`.

    ``[status:u8][modulation:u8][raw:u8][flags:u8]``, 4 bytes.

    ``modulation`` is read out of the radio's **own VFO after the firmware applied it**, so it is
    what the radio is on rather than what it was told — the same reason ``0x0874`` reports
    frequencies instead of echoing them. On any non-``APPLIED`` status the firmware forces
    ``modulation`` and ``raw`` to ``0xFF`` and ``flags`` to ``0``, so a refusal can never describe a
    demodulator and ``status`` stays authoritative and checkable first.

    ``raw`` is the radio's own ``ModulationMode_t`` value, reported for the same reason
    :class:`SetVfoReply` reports ``OUTPUT_POWER_*``: when two scales disagree, only the raw number
    makes it visible. **Diagnostic only** — its numbering moves with ``ENABLE_BYP_RAW_DEMODULATORS``,
    so nothing may branch on it.
    """

    status: ModulationStatus
    modulation: int = DockModulation.UNKNOWN
    raw: int = DockModulation.UNKNOWN
    flags: int = 0

    COMMAND: ClassVar[int] = DockCommand.SET_MODULATION_REPLY
    _FORMAT: ClassVar[str] = "<BBBB"
    SIZE: ClassVar[int] = struct.calcsize("<BBBB")  # 4

    @property
    def ok(self) -> bool:
        return self.status is ModulationStatus.APPLIED

    @property
    def name(self) -> str | None:
        """``"FM"`` / ``"AM"``, or ``None`` when the radio is on something this wire cannot name.

        ``None`` covers both the refusal sentinel and a genuinely unnameable demodulator, and it is
        deliberately not "FM": a caller that took the fallback as FM would read every refusal as a
        radio sitting on the one modulation that can transmit.
        """
        return MODULATION_NAMES.get(self.modulation)

    @property
    def tx_ok(self) -> bool:
        """Will the radio key its own PTT path in this modulation? See :data:`FLAG_TX_OK`."""
        return bool(self.flags & FLAG_TX_OK)

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, int(self.status), self.modulation, self.raw, self.flags)

    @classmethod
    def unpack(cls, data: bytes) -> "SetModulationReply":
        if len(data) != cls.SIZE:
            raise ValueError(f"SetModulationReply params are {cls.SIZE} bytes, got {len(data)}")
        status, modulation, raw, flags = struct.unpack(cls._FORMAT, data)
        # An unknown status must not crash the decode — a newer firmware may add one, and the
        # caller still needs to learn that it was NOT `APPLIED`. Same rule as `SetVfoReply`.
        try:
            status = ModulationStatus(status)
        except ValueError:
            pass
        return cls(status, modulation, raw, flags)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class SetModulation:
    """``0x0877`` set the radio's demodulator — a **fork extension** (F7), answered by
    :class:`SetModulationReply`.

    One byte: a :class:`DockModulation` value. FM and AM are accepted; everything else is refused
    with ``ERR_FIELD`` and **never clamped**, because a clamped modulation is a radio quietly
    listening to the wrong thing.

    **Why a command of its own and not a 14th byte on** :class:`SetVfo`. A field appended there
    changes the bytes of a frame this server already sends, and it breaks in both directions
    silently: an old host's 13-byte frame is refused ``ERR_SHORT`` (which reads as a tuning
    failure, not a version mismatch), and a new host's 14-byte frame decodes fine on an old
    firmware that ignores the extra byte and tunes on a modulation nobody set. A new opcode is
    additive — an old host never sends it, and a new host sending it to a pre-F7 firmware falls
    through the dispatch's ``default:`` to silence (ADR 0149).

    **The radio keeps it.** The firmware holds the modulation in its dock session and applies it on
    every later ``0x0873``, so a tune cannot revert it — before F7 every tune wrote
    ``MODULATION_FM`` literally. That is not merely a convenience: ADR 0131 established that this
    link **drops** frames (single-threaded firmware; anything arriving while it is busy is
    discarded, not queued), so "tune, then set modulation" as two frames has a failure mode where
    the tune lands, the modulation is dropped, and the radio sits on the right channel in the wrong
    demodulator with nothing on the wire having said so.

    It is **session** state, reset by the radio's power switch and not by a host restart, so a
    reconnecting host must **assert** the modulation it wants rather than assume FM.
    """

    modulation: int

    COMMAND: ClassVar[int] = DockCommand.SET_MODULATION
    _FORMAT: ClassVar[str] = "<B"
    SIZE: ClassVar[int] = struct.calcsize("<B")  # 1

    def __post_init__(self) -> None:
        # Validated here as well as in the firmware, for the reason `SetVfo.__post_init__` gives:
        # a `ValueError` names the offending field in a stack that points at the bug, while an
        # `ERR_FIELD` three layers away over the air says only that the radio did not like
        # something. USB is rejected here too — its number is reserved, its value is not accepted.
        if self.modulation not in MODULATION_NAMES:
            allowed = ", ".join(f"{n} ({v})" for v, n in MODULATION_NAMES.items())
            raise ValueError(
                f"modulation must be one of: {allowed}; got {self.modulation!r} "
                f"(the firmware refuses anything else with ERR_FIELD rather than clamping)"
            )

    @property
    def name(self) -> str:
        """``"FM"`` / ``"AM"`` — total, because ``__post_init__`` refuses anything else."""
        return MODULATION_NAMES[self.modulation]

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.modulation)

    @classmethod
    def unpack(cls, data: bytes) -> "SetModulation":
        if len(data) != cls.SIZE:
            raise ValueError(f"SetModulation params are {cls.SIZE} bytes, got {len(data)}")
        (modulation,) = struct.unpack(cls._FORMAT, data)
        return cls(modulation)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


# ---------------------------------------------------------------------------------------
# Broadcast FM — the radio's second receiver (0x0879 / 0x087A, F8; ADR 0156/0157)
# ---------------------------------------------------------------------------------------
#
# Why this is not a modulation: `SET_MODULATION` chooses how the BK4819 demodulates the station's
# own channel. This switches off a *different chip*. The consequence a caller must hold onto is that
# the two are orthogonal in the dangerous direction — a radio can be on a perfectly good demodulator
# and still hear nothing, because the BK1080 is holding the speaker line the AIOC listens on, and it
# will still transmit while it does (`RADIO_PrepareTX` has no broadcast-FM term).

#: The BK1080's tuning step. Frequencies are carried in **Hz** and the firmware refuses anything off
#: this boundary with ``ERR_FIELD`` — refuse, never round. The next raster step is a whole adjacent
#: station, so rounding would put the receiver somewhere nobody asked for and report success.
BROADCAST_FM_RASTER_HZ = 100_000


class BroadcastFmStatus(IntEnum):
    """The ``0x087A`` status byte. ``APPLIED`` is the only success.

    The numbers are :class:`SetVfoStatus`'s, holes and all, for the reason
    :class:`ModulationStatus` gives. ``8`` and ``9`` are new to the shared table.

    ``ERR_FIELD`` and ``ERR_BAND`` cannot arise on the OFF leg this server sends: ``Dock_SetFm``
    branches to ``Dock_FmOff()`` **before** the raster and band checks, so those fields are never
    read. They are listed because the wire defines them, not because this codec can produce them.
    """

    APPLIED = 0     #: the receiver is in the state reported by ``state``
    ERR_SHORT = 1   #: payload shorter than the parameter set — the host mis-sized its frame
    ERR_BUSY = 2    #: the host holds full-control (``0x0870``); retry after ``0x0871``
    ERR_FIELD = 4   #: unknown action, band > 3, or a frequency off the 100 kHz raster
    ERR_NO_HAL = 5  #: built without ``ENABLE_FMRADIO`` — there is no BK1080 driver in this image
    ERR_BAND = 6    #: the frequency is outside the named band's limits
    ERR_TX = 8      #: the radio is keyed, and broadcast-FM state does not survive an over
    ERR_OFF = 9     #: TUNE asked of a receiver that is switched off

    @property
    def ok(self) -> bool:
        return self is BroadcastFmStatus.APPLIED


#: ``state`` blanking sentinel. ``0xFF`` and emphatically not ``0``, because ``0`` is the real
#: reading for OFF — and "off" is precisely the claim that would get a deaf station trusted.
BROADCAST_FM_STATE_UNKNOWN = 0xFF
#: ``band`` blanking sentinel, ``0xFF`` for the same reason: band ``0`` (87.5-108 MHz) is real, and
#: is the band nearly every host wants.
BROADCAST_FM_BAND_UNKNOWN = 0xFF

#: ``0x087A`` flags bit 1 (F9; ADR 0159) — **broadcast FM is blocking transmit on this build, right
#: now**. Read it with :data:`FLAG_TX_OK`, never alone: ``will_key = TX_OK and not FM_BLOCKS_TX``.
#:
#: **`0x087A` only.** ``0x0878``'s flags byte has no bit 1 — F9 added this to the broadcast-FM reply
#: and nothing else — so :class:`SetModulationReply` deliberately has no such accessor. Reading it
#: off that frame would be a host inventing a firmware refusal from a byte the firmware never set.
#:
#: **It reports blocking, not readiness, and the polarity is load-bearing.** ``flags`` blanks to 0 on
#: every refusal, and firmware older than F9 answers 0 because the bit did not exist. A readiness bit
#: would read "will not key" in both cases and let a lost frame or an old radio stop a transmitter; a
#: blocking bit reads "not blocked" in both — which is *true* of a refusal that measured nothing and
#: *true* of an F8 radio. Same rule as `tx_ok`, on the wire this time.
#:
#: **It is a property of the IMAGE as well as of the radio.** The interlock is behind
#: ``ENABLE_DOCK_FM_TX_INTERLOCK``, on in the Fusion build radio-server stations flash and off in the
#: editions the fork does not ship. An image without it answers 0 while playing broadcast FM, and
#: that is correct — it really will key, exactly as upstream F4HWN does.
FLAG_FM_BLOCKS_TX = 0x02


@dataclass(frozen=True)
class BroadcastFmReply:
    """``0x087A`` — what the second receiver is actually doing.

    ``[status:u8][state:u8][freq_hz:u32 LE][band:u8][flags:u8]``, 8 bytes.

    Everything is read back out of the firmware's own state after it applied, never echoed — the
    ``0x0874`` doctrine, and load-bearing twice here: the Hz→raster conversion and the **two-bit**
    ``FM_Band`` field are both places where what the radio holds can differ from what it was sent,
    and echoing the request is exactly what would hide either.

    **Three different blanking sentinels**, because the rule is "a value that cannot be a real
    reading of *this* field" and the fields disagree: ``state`` and ``band`` blank to ``0xFF``
    (``0`` is real for both), ``freq_hz`` blanks to ``0`` (no band's low limit is 0, so it follows
    ``0x0874``'s frequencies), ``flags`` to ``0``. The raw wire values are preserved on this
    dataclass and the interpreted accessors below return ``None`` rather than guessing.

    This class decodes **every** state including ON, while :class:`ClearBroadcastFm` can only build
    OFF. The asymmetry is deliberate: you can always learn the radio is in broadcast FM; this server
    can only tell it to stop (ADR 0157).
    """

    status: BroadcastFmStatus
    state: int = BROADCAST_FM_STATE_UNKNOWN
    raw_hz: int = 0
    raw_band: int = BROADCAST_FM_BAND_UNKNOWN
    flags: int = 0

    COMMAND: ClassVar[int] = DockCommand.SET_BROADCAST_FM_REPLY
    _FORMAT: ClassVar[str] = "<BBIBB"
    SIZE: ClassVar[int] = struct.calcsize("<BBIBB")  # 8

    @property
    def ok(self) -> bool:
        return self.status is BroadcastFmStatus.APPLIED

    @property
    def on(self) -> bool | None:
        """Is the second receiver running — ``True``/``False``, or ``None`` when unknown.

        ``None`` and ``False`` are different answers, and here the difference is the whole point:
        ``False`` means the station can hear its own channel, ``None`` means nobody checked. A
        caller that read the sentinel as ``False`` would report a deaf station as healthy.
        """
        if self.state == BROADCAST_FM_STATE_UNKNOWN:
            return None
        return bool(self.state)

    @property
    def band(self) -> int | None:
        """The BK1080 band index, or ``None`` on the blanking sentinel."""
        return None if self.raw_band == BROADCAST_FM_BAND_UNKNOWN else self.raw_band

    @property
    def hz(self) -> int | None:
        """The receiver's tuning in Hz, or ``None`` when blanked.

        Reported on the OFF leg too, because the BK1080 remembers where it was and the firmware
        reads it straight out of ``gEeprom.FM_FrequencyPlaying`` regardless of state. So this is
        *the frequency the second receiver would resume on*, *not* what anything is listening to —
        it is only meaningful read together with :attr:`on`, which is why the backend groups the two
        into one status block rather than reporting them as independent fields.
        """
        return None if self.raw_hz == 0 else self.raw_hz

    @property
    def tx_ok(self) -> bool:
        """Will the radio key its own PTT path? See :data:`FLAG_TX_OK`.

        **Orthogonal to broadcast FM, deliberately.** This bit reports the BK4819 demodulator, which
        the BK1080 never touches, so ``on=True`` with ``tx_ok=True`` is not a contradiction — it is
        the dangerous combination itself, and the reason it is carried on this frame at all. Since F9
        the *second* cause has :attr:`fm_blocks_tx`; this one keeps its published meaning exactly,
        because redefining it to mean the conjunction would have silently changed a documented bit
        under a deployed host.

        Nothing in this server records it from here: see `SetVfoTuner.clear_broadcast_fm`.
        """
        return bool(self.flags & FLAG_TX_OK)

    @property
    def fm_blocks_tx(self) -> bool:
        """Is the radio refusing to key **because** broadcast FM is running? See
        :data:`FLAG_FM_BLOCKS_TX`.

        A plain ``bool`` and not tri-state, which looks like a violation of this module's "never
        coerce an unknown into a `False`" rule and is not: ``0`` is the *correct* reading for a
        refusal that measured nothing and for every image without the interlock, because neither is
        blocking anything. The unknown lives in :attr:`status`, where a caller checks it first.
        """
        return bool(self.flags & FLAG_FM_BLOCKS_TX)

    @property
    def will_key(self) -> bool:
        """Both bits, read together — the rule the fork states on the wire.

        Provided because a host holding **only** this frame has no other way to get it right, and
        reading bit 0 alone gets exactly one of the four combinations wrong: the dangerous one, a
        station that is deaf and refusing while reporting a perfectly good demodulator.

        This server does not use it. It keeps the two causes apart all the way to the operator, with
        two refusals, two messages and two remedies (ADR 0158/0161) — a collapsed answer is a
        diagnosis nobody can act on.
        """
        return self.tx_ok and not self.fm_blocks_tx

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT, int(self.status), self.state, self.raw_hz, self.raw_band, self.flags,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "BroadcastFmReply":
        if len(data) != cls.SIZE:
            raise ValueError(f"BroadcastFmReply params are {cls.SIZE} bytes, got {len(data)}")
        status, state, raw_hz, raw_band, flags = struct.unpack(cls._FORMAT, data)
        # An unknown status must not crash the decode — a newer firmware may add one, and the caller
        # still needs to learn it was NOT `APPLIED`. Same rule as `SetVfoReply`.
        try:
            status = BroadcastFmStatus(status)
        except ValueError:
            pass
        return cls(status, state, raw_hz, raw_band, flags)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class ClearBroadcastFm:
    """``0x0879`` action=OFF — switch the second receiver off. **The only broadcast-FM frame this
    server can build.**

    ``[action:u8][freq_hz:u32 LE][band:u8]``, 6 bytes. The wire defines three actions — OFF (0),
    ON (1) and TUNE (2) — and this class expresses exactly one, with no parameter to get it wrong.
    "This server cannot turn broadcast FM on" is therefore a property of the code rather than
    something a reviewer has to keep checking (ADR 0157). Widening it is the next cycle's job, under
    its own ADR, because an ON path needs the transmit interlock that does not exist yet.

    The frequency and band bytes are sent as zero, and that is not a placeholder: ``Dock_SetFm``
    branches to ``Dock_FmOff()`` **before** the raster and band checks, so the OFF leg never reads
    either field. The fork proves it by sending deliberate junk in both and having it accepted
    (``test_dock.c:1272``). Validating them here would be this server inventing a rule the firmware
    does not have.

    Deliberately **not** in the parse dispatch table: an OFF-only class cannot ``unpack`` an ON
    frame, and a decoder that silently mangled one would be worse than one that declines. The host
    is never a radio, so it never needs to decode an inbound ``0x0879``. ``SetVfoProbe`` is the
    existing precedent for a frame class outside the table.
    """

    COMMAND: ClassVar[int] = DockCommand.SET_BROADCAST_FM
    _FORMAT: ClassVar[str] = "<BIB"
    SIZE: ClassVar[int] = struct.calcsize("<BIB")  # 6

    #: The wire's OFF action. Named rather than spelled ``0`` at the pack site so the one value this
    #: server sends is greppable from the firmware's ``DOCK_FM_OFF``.
    ACTION_OFF: ClassVar[int] = 0

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.ACTION_OFF, 0, 0)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class ExitHwMode:
    """``0x0871`` exit full-control mode — no params; the firmware ``RestoreRadio``s and the
    radio returns to standalone operation (uart.c:684-685, 737)."""

    COMMAND: ClassVar[int] = DockCommand.EXIT_HW_MODE

    def pack(self) -> bytes:
        return b""

    @classmethod
    def unpack(cls, data: bytes) -> "ExitHwMode":
        if data:
            raise ValueError(f"ExitHwMode takes no params, got {len(data)} bytes")
        return cls()

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class WriteRegisters:
    """``0x0850`` write BK4819 registers (uart.c:180-184, CMD_085X_t; handler 569-576).

    Params: ``Length:u16`` (pair count) then ``Length`` ``(register, value)`` u16 pairs
    (``RegData`` holds ``2*Length`` entries). The client builds a tune this way — regs
    ``0x38``/``0x39`` = low/high 16 bits of ``freq_hz / 10``, ``0x33`` band, ``0x30``
    tuning (BK4819.cs SetFrequency; recorded in ADR 0110 for the control-path cycle).
    """

    registers: tuple[tuple[int, int], ...]

    COMMAND: ClassVar[int] = DockCommand.WRITE_REGISTERS

    def pack(self) -> bytes:
        out = bytearray(struct.pack("<H", len(self.registers)))
        for reg, value in self.registers:
            out += struct.pack("<HH", reg, value)
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes) -> "WriteRegisters":
        (length,) = struct.unpack_from("<H", data, 0)
        if len(data) != 2 + 4 * length:
            raise ValueError(f"WriteRegisters expects {2 + 4 * length} bytes, got {len(data)}")
        pairs = tuple(
            (struct.unpack_from("<HH", data, 2 + 4 * i)) for i in range(length)
        )
        return cls(pairs)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class ReadRegisters:
    """``0x0851`` read BK4819 registers (uart.c:180-184; handler 579-591).

    Params: ``Length:u16`` then ``Length`` register addresses (``RegData[i]``, one u16
    each — *not* pairs). Each address yields one :class:`RegisterInfo` reply.
    """

    registers: tuple[int, ...]

    COMMAND: ClassVar[int] = DockCommand.READ_REGISTERS

    def pack(self) -> bytes:
        return struct.pack(f"<H{len(self.registers)}H", len(self.registers), *self.registers)

    @classmethod
    def unpack(cls, data: bytes) -> "ReadRegisters":
        (length,) = struct.unpack_from("<H", data, 0)
        if len(data) != 2 + 2 * length:
            raise ValueError(f"ReadRegisters expects {2 + 2 * length} bytes, got {len(data)}")
        return cls(tuple(struct.unpack_from(f"<{length}H", data, 2)))

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class WriteGpio:
    """``0x0860`` set/clear GPIO bits (uart.c:186-190, CMD_086X_t; handler 596-620).

    Params: ``Length:u16`` (pair count) then ``Length`` ``(port, bit)`` u8 pairs. ``port``
    0/1/2 = set GPIOA/B/C, 3/4/5 = clear GPIOA/B/C (uart.c:599-618).
    """

    pins: tuple[tuple[int, int], ...]

    COMMAND: ClassVar[int] = DockCommand.WRITE_GPIO

    def pack(self) -> bytes:
        out = bytearray(struct.pack("<H", len(self.pins)))
        for port, bit in self.pins:
            out += struct.pack("<BB", port, bit)
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes) -> "WriteGpio":
        (length,) = struct.unpack_from("<H", data, 0)
        if len(data) != 2 + 2 * length:
            raise ValueError(f"WriteGpio expects {2 + 2 * length} bytes, got {len(data)}")
        pairs = tuple(struct.unpack_from("<BB", data, 2 + 2 * i) for i in range(length))
        return cls(pairs)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class ReadGpio:
    """``0x0861`` read GPIO bits (uart.c:186-190; handler 623-645).

    Params: ``Length:u16`` (pair count) then ``Length`` ``(port, bit)`` u8 query pairs;
    ``port`` 0/1/2 selects GPIOA/B/C (uart.c:632-642). Each yields one :class:`GpioInfo`.
    """

    pins: tuple[tuple[int, int], ...]

    COMMAND: ClassVar[int] = DockCommand.READ_GPIO

    def pack(self) -> bytes:
        out = bytearray(struct.pack("<H", len(self.pins)))
        for port, bit in self.pins:
            out += struct.pack("<BB", port, bit)
        return bytes(out)

    @classmethod
    def unpack(cls, data: bytes) -> "ReadGpio":
        (length,) = struct.unpack_from("<H", data, 0)
        if len(data) != 2 + 2 * length:
            raise ValueError(f"ReadGpio expects {2 + 2 * length} bytes, got {len(data)}")
        pairs = tuple(struct.unpack_from("<BB", data, 2 + 2 * i) for i in range(length))
        return cls(pairs)

    def to_frame(self, *, obfuscate_body: bool = True) -> bytes:
        return build_frame(self.COMMAND, self.pack(), obfuscate_body=obfuscate_body)


@dataclass(frozen=True)
class ImHere:
    """``0x0515`` version/challenge reply to HELLO (uart.c:70-79/285-299, REPLY_0514_t).

    ``version`` is a fixed 16-byte field (NUL-padded C string); ``challenge`` is 4 u32s.
    The 2 pad bytes between the flags and the challenge are skipped by the ``xx`` format.
    """

    version: bytes
    has_custom_aes_key: int
    in_lock_screen: int
    challenge: tuple[int, int, int, int]

    COMMAND: ClassVar[int] = DockCommand.IM_HERE
    _FORMAT: ClassVar[str] = "<16sBBxxIIII"
    SIZE: ClassVar[int] = struct.calcsize("<16sBBxxIIII")  # 36

    def pack(self) -> bytes:
        return struct.pack(
            self._FORMAT, self.version, self.has_custom_aes_key, self.in_lock_screen,
            *self.challenge,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "ImHere":
        if len(data) != cls.SIZE:
            raise ValueError(f"ImHere params are {cls.SIZE} bytes, got {len(data)}")
        version, aes, lock, c0, c1, c2, c3 = struct.unpack(cls._FORMAT, data)
        return cls(version, aes, lock, (c0, c1, c2, c3))


@dataclass(frozen=True)
class ScanReply:
    """``0x0908`` spectrum batch reply (uart.c:171-178/887, REPLY_0808_t).

    Params: ``Length:u8`` (valid count in the final batch), ``Sync:u8`` (batch counter),
    ``Signals:u8[100]``. The firmware sends this with a fixed 102-byte payload.
    """

    length: int
    sync: int
    signals: bytes

    COMMAND: ClassVar[int] = DockCommand.SCAN_REPLY
    _FORMAT: ClassVar[str] = "<BB100s"
    SIZE: ClassVar[int] = struct.calcsize("<BB100s")  # 102

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.length, self.sync, self.signals)

    @classmethod
    def unpack(cls, data: bytes) -> "ScanReply":
        if len(data) != cls.SIZE:
            raise ValueError(f"ScanReply params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))


@dataclass(frozen=True)
class RegisterInfo:
    """``0x0951`` one register value, one reply per read (uart.c:192-198, REPLY_0851_t)."""

    register: int
    value: int

    COMMAND: ClassVar[int] = DockCommand.REGISTER_INFO
    _FORMAT: ClassVar[str] = "<HH"
    SIZE: ClassVar[int] = struct.calcsize("<HH")  # 4

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.register, self.value)

    @classmethod
    def unpack(cls, data: bytes) -> "RegisterInfo":
        if len(data) != cls.SIZE:
            raise ValueError(f"RegisterInfo params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))


@dataclass(frozen=True)
class GpioInfo:
    """``0x0961`` one GPIO reading, one reply per read (uart.c:200-206, REPLY_0861_t).

    ``gpio`` = queried port, plus 3 when the bit reads low (uart.c:644).
    """

    gpio: int
    bit: int

    COMMAND: ClassVar[int] = DockCommand.GPIO_INFO
    _FORMAT: ClassVar[str] = "<BB"
    SIZE: ClassVar[int] = struct.calcsize("<BB")  # 2

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, self.gpio, self.bit)

    @classmethod
    def unpack(cls, data: bytes) -> "GpioInfo":
        if len(data) != cls.SIZE:
            raise ValueError(f"GpioInfo params are {cls.SIZE} bytes, got {len(data)}")
        return cls(*struct.unpack(cls._FORMAT, data))


@dataclass(frozen=True)
class JetScanReply:
    """``0x0988`` jet-scan peaks (uart.c:221-227/793-795, REPLY_0888_t).

    Up to 16 ``(freq, rssi)`` peaks: ``Freqs:u32[16]`` then ``Sigs:u16[16]``.
    """

    freqs: tuple[int, ...]
    sigs: tuple[int, ...]

    COMMAND: ClassVar[int] = DockCommand.JET_SCAN_REPLY
    _FORMAT: ClassVar[str] = "<16I16H"
    SIZE: ClassVar[int] = struct.calcsize("<16I16H")  # 96

    def pack(self) -> bytes:
        return struct.pack(self._FORMAT, *self.freqs, *self.sigs)

    @classmethod
    def unpack(cls, data: bytes) -> "JetScanReply":
        if len(data) != cls.SIZE:
            raise ValueError(f"JetScanReply params are {cls.SIZE} bytes, got {len(data)}")
        fields = struct.unpack(cls._FORMAT, data)
        return cls(fields[:16], fields[16:])


# ---------------------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RawMessage:
    """A well-framed payload whose opcode has no modelled struct (or the wrong length).

    Carries the decoded opcode, inner param-length field, and raw params so a caller can
    still route or log it. :func:`parse_frame` returns this rather than raising.
    """

    command: int
    param_len: int
    params: bytes


#: opcode → dataclass with a compatible ``unpack(params)`` classmethod.
_DISPATCH: dict[int, type] = {
    DockCommand.HELLO: Hello,
    DockCommand.KEYPRESS: KeyPress,
    DockCommand.GET_SCREEN: GetScreen,
    DockCommand.SCAN: Scan,
    DockCommand.STOCK_SET_MODULATION: StockSetModulation,
    DockCommand.ENTER_HW_MODE: EnterHwMode,
    DockCommand.EXIT_HW_MODE: ExitHwMode,
    DockCommand.SET_VFO: SetVfo,
    DockCommand.SET_VFO_REPLY: SetVfoReply,
    DockCommand.SET_MODULATION: SetModulation,
    DockCommand.SET_MODULATION_REPLY: SetModulationReply,
    # Reply only. `ClearBroadcastFm` is deliberately absent — see its docstring. Registering this
    # one is not optional bookkeeping: without it `0x087A` decodes to `RawMessage`, the tuner's
    # `isinstance` match never fires, and every clear times out against a radio that answered
    # correctly — a total failure that looks exactly like firmware without the command.
    DockCommand.SET_BROADCAST_FM_REPLY: BroadcastFmReply,
    DockCommand.EEPROM_READ: EepromRead,
    DockCommand.EEPROM_READ_REPLY: EepromReadReply,
    DockCommand.EEPROM_WRITE: EepromWrite,
    DockCommand.EEPROM_WRITE_REPLY: EepromWriteReply,
    DockCommand.RESET: Reset,
    DockCommand.JET_SCAN: JetScan,
    DockCommand.WRITE_REGISTERS: WriteRegisters,
    DockCommand.READ_REGISTERS: ReadRegisters,
    DockCommand.WRITE_GPIO: WriteGpio,
    DockCommand.READ_GPIO: ReadGpio,
    DockCommand.IM_HERE: ImHere,
    DockCommand.SCAN_REPLY: ScanReply,
    DockCommand.REGISTER_INFO: RegisterInfo,
    DockCommand.GPIO_INFO: GpioInfo,
    DockCommand.JET_SCAN_REPLY: JetScanReply,
}


def parse_frame(payload: bytes):
    """Map a decoded *payload* (``[opcode][param_len][params]``) to its typed message.

    Returns the modelled dataclass when the opcode is known and its params unpack cleanly,
    a :class:`RawMessage` when the opcode is unknown or the params do not fit the struct,
    or ``None`` when *payload* is too short to even carry the inner header. Never raises —
    malformed wire input is a normal condition on an RF-fed serial link.
    """
    if len(payload) < _INNER_HEADER.size:
        return None
    command, param_len = _INNER_HEADER.unpack_from(payload, 0)
    params = payload[_INNER_HEADER.size :]
    codec = _DISPATCH.get(command)
    if codec is not None:
        try:
            return codec.unpack(params)
        except (ValueError, struct.error):
            pass
    return RawMessage(command, param_len, params)
