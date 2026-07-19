"""Pure wire codec for the DV Dongle's DVSI AMBE2000 serial protocol (ADR 0086) — no I/O.

The DV Dongle is an FTDI VCP at **230400 8N1** over which the host exchanges fixed-shape packets
with the on-board AMBE2000 vocoder chip. This module is the frame/struct layer only: building the
control/config/PCM/AMBE packets and a streaming deframer. It imports nothing but the stdlib and
performs no serial reads or writes; the reader thread, handshake, and the :class:`Vocoder`
implementation live in :mod:`radio_server.vocoder.dvdongle`.

Source of truth: an independent implementation derived from g4klx/DummyRepeater's
``Common/DVDongleController.cpp`` (GPL-2) read purely as a **protocol specification** — the byte
sequences here are DVSI/DV-Dongle hardware-interface facts, not ported code, and talking to a device
over a wire is not a derivative work (same posture as :mod:`radio_server.backends.kv4p.frames` vs the
GPL-3 kv4p firmware). Cross-check every constant against DVTool and the hardware (guardrail 1).

Packet framing (DVSI standard, confirmed arithmetically against every constant below): a 2-byte
little-endian header ``word`` splits into a 13-bit total length (including the 2 header bytes) and a
3-bit type::

    word   = header[0] | (header[1] << 8)      # little-endian
    length = word & 0x1FFF                      # total packet length, header included
    type   = word >> 13

Types seen: 0 control-response, 1 control-request, 4 audio (PCM), 5 AMBE. E.g. the AMBE header
``{0x32, 0xA0}`` = length 50 / type 5 (2 + 48 payload); the audio header ``{0x42, 0x81}`` = length
322 / type 4 (2 + 320 payload); the name request ``{0x04, 0x20}`` = length 4 / type 1.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

# --------------------------------------------------------------------------------------
# Framing constants
# --------------------------------------------------------------------------------------

#: Every packet begins with this many header bytes (the little-endian length/type word).
HEADER_LEN = 2

#: Mask for the 13-bit length field of the header word.
LENGTH_MASK = 0x1FFF

#: 3-bit packet-type values carried in the top of the header word.
TYPE_CONTROL_RESP = 0
TYPE_CONTROL_REQ = 1
TYPE_AUDIO = 4
TYPE_AMBE = 5

# --------------------------------------------------------------------------------------
# Payload geometry
# --------------------------------------------------------------------------------------

#: An AMBE packet's payload is a fixed 48-byte block: a 24-byte parameter/config header followed by
#: a 24-byte region whose first 9 bytes are the D-STAR AMBE voice frame (the rest zero-padding).
AMBE_PAYLOAD_LEN = 48

#: Offset of the 9-byte voice frame within the 48-byte AMBE payload (DummyRepeater
#: ``DVD_AMBE_HEADER_LEN``). Bytes ``[0:24]`` are the vocoder config; ``[24:33]`` the voice frame.
AMBE_VOICE_OFFSET = 24

#: Bytes of a packed D-STAR AMBE voice frame (72 bits). Kept in sync with
#: :data:`radio_server.vocoder.base.AMBE_BYTES_PER_FRAME`.
VOICE_FRAME_LEN = 9

#: An audio packet's payload is 320 bytes: 160 signed-16-bit little-endian mono samples (20 ms @
#: 8 kHz). PCM rides the wire little-endian (``wxINT16_SWAP_ON_BE`` in the reference), matching the
#: project's canonical little-endian layout — no byte-swap needed on a little-endian host.
AUDIO_PAYLOAD_LEN = 320

#: Largest legitimate packet: the 322-byte audio packet. A parsed length beyond this is desync, and
#: the streaming decoder resyncs rather than buffering unboundedly.
MAX_PACKET_LEN = HEADER_LEN + AUDIO_PAYLOAD_LEN

# --------------------------------------------------------------------------------------
# Fixed control messages (host -> device requests and their device -> host responses)
# --------------------------------------------------------------------------------------

#: Request the product name; the dongle answers :data:`RESP_NAME`. This is the open-handshake probe.
REQ_NAME = bytes([0x04, 0x20, 0x01, 0x00])
#: The name response: a control packet carrying the ASCII string ``"DV Dongle\0"``.
RESP_NAME = bytes([0x0E, 0x00, 0x01, 0x00]) + b"DV Dongle\x00"

#: Start the streaming session; the dongle echoes :data:`RESP_START`.
REQ_START = bytes([0x05, 0x00, 0x18, 0x00, 0x01])
RESP_START = REQ_START

#: Stop the streaming session; the dongle echoes :data:`RESP_STOP`.
REQ_STOP = bytes([0x05, 0x00, 0x18, 0x00, 0x00])
RESP_STOP = REQ_STOP

#: A no-op keep-alive the dongle may emit; carries no payload.
RESP_NOP = bytes([0x02, 0x00])

# --------------------------------------------------------------------------------------
# AMBE2000 D-STAR full-rate configuration blocks
#
# The 24-byte parameter header prepended to every AMBE packet, telling the chip the coding rate. The
# encoder and decoder blocks differ only at offsets 22-23. These are opaque DVSI control words —
# their meaning is the chip's, not ours; carried verbatim as a spec fact (verify against hardware).
# --------------------------------------------------------------------------------------

_AMBE_PARAMS_COMMON = bytes(
    [0xEC, 0x13, 0x00, 0x00, 0x30, 0x10, 0x00, 0x40, 0x00, 0x00, 0x00, 0x00,
     0x48, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xFF, 0x00]
)
#: Encoder (speech -> AMBE) config: common prefix + {0x04, 0xF0} + 24 zero bytes (voice-frame region).
AMBE_ENC_PARAMS = _AMBE_PARAMS_COMMON + bytes([0x04, 0xF0]) + bytes(AMBE_PAYLOAD_LEN - 24)
#: Decoder (AMBE -> speech) config: common prefix + {0x20, 0x80} + 24 zero bytes (voice frame goes
#: at offset :data:`AMBE_VOICE_OFFSET` of this region).
AMBE_DEC_PARAMS = _AMBE_PARAMS_COMMON + bytes([0x20, 0x80]) + bytes(AMBE_PAYLOAD_LEN - 24)


# --------------------------------------------------------------------------------------
# Encode (host -> device)
# --------------------------------------------------------------------------------------


def _build_packet(type_bits: int, payload: bytes) -> bytes:
    """Prepend the 2-byte little-endian length/type header to ``payload``.

    ``length`` = ``HEADER_LEN + len(payload)`` must fit the 13-bit field; ``type_bits`` the 3-bit
    type. The inverse of the header split documented at module top.
    """
    length = HEADER_LEN + len(payload)
    if length > LENGTH_MASK:
        raise ValueError(f"packet too long for the 13-bit length field: {length}")
    word = (type_bits << 13) | length
    return bytes([word & 0xFF, (word >> 8) & 0xFF]) + payload


def build_ambe_packet(payload: bytes) -> bytes:
    """Build an AMBE (type-5) packet from a 48-byte AMBE payload."""
    if len(payload) != AMBE_PAYLOAD_LEN:
        raise ValueError(f"AMBE payload is {AMBE_PAYLOAD_LEN} bytes, got {len(payload)}")
    return _build_packet(TYPE_AMBE, payload)


def build_audio_packet(pcm: bytes) -> bytes:
    """Build an audio (type-4) packet from 320 bytes of little-endian s16 mono PCM."""
    if len(pcm) != AUDIO_PAYLOAD_LEN:
        raise ValueError(f"audio payload is {AUDIO_PAYLOAD_LEN} bytes, got {len(pcm)}")
    return _build_packet(TYPE_AUDIO, pcm)


def build_encode_config_packet() -> bytes:
    """The dummy AMBE packet (encoder params, no voice frame) that precedes an audio frame to encode."""
    return build_ambe_packet(AMBE_ENC_PARAMS)


def build_decode_ambe_packet(voice_frame: bytes) -> bytes:
    """Build the AMBE packet for decoding: decoder params with ``voice_frame`` spliced in at offset 24."""
    if len(voice_frame) != VOICE_FRAME_LEN:
        raise ValueError(f"AMBE voice frame is {VOICE_FRAME_LEN} bytes, got {len(voice_frame)}")
    payload = bytearray(AMBE_DEC_PARAMS)
    payload[AMBE_VOICE_OFFSET : AMBE_VOICE_OFFSET + VOICE_FRAME_LEN] = voice_frame
    return build_ambe_packet(bytes(payload))


def build_decode_dummy_audio_packet() -> bytes:
    """The dummy (all-zero) audio packet that follows the AMBE packet when decoding."""
    return build_audio_packet(bytes(AUDIO_PAYLOAD_LEN))


# --------------------------------------------------------------------------------------
# Decode (device -> host): streaming deframer + dispatch
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Packet:
    """One decoded DV Dongle packet: its 3-bit type and its payload (header stripped)."""

    type_bits: int
    payload: bytes

    @property
    def raw(self) -> bytes:
        """The full on-wire packet, header included — for comparing against the fixed responses."""
        return _build_packet(self.type_bits, self.payload)


class ResponseKind(Enum):
    """What a decoded :class:`Packet` is, mirroring DummyRepeater's ``RESP_TYPE``."""

    NAME = "name"
    START = "start"
    STOP = "stop"
    AMBE = "ambe"
    AUDIO = "audio"
    NOP = "nop"
    UNKNOWN = "unknown"


def classify(packet: Packet) -> ResponseKind:
    """Map a decoded packet to its :class:`ResponseKind` (never raises)."""
    if packet.type_bits == TYPE_AMBE and len(packet.payload) == AMBE_PAYLOAD_LEN:
        return ResponseKind.AMBE
    if packet.type_bits == TYPE_AUDIO and len(packet.payload) == AUDIO_PAYLOAD_LEN:
        return ResponseKind.AUDIO
    raw = packet.raw
    if raw == RESP_NAME:
        return ResponseKind.NAME
    if raw == RESP_START:
        return ResponseKind.START
    if raw == RESP_STOP:
        return ResponseKind.STOP
    if raw == RESP_NOP:
        return ResponseKind.NOP
    return ResponseKind.UNKNOWN


def ambe_voice_frame(packet: Packet) -> bytes:
    """Extract the 9-byte AMBE voice frame from an AMBE packet's payload (offset 24)."""
    return packet.payload[AMBE_VOICE_OFFSET : AMBE_VOICE_OFFSET + VOICE_FRAME_LEN]


def audio_pcm(packet: Packet) -> bytes:
    """The 320-byte little-endian s16 PCM payload of an audio packet."""
    return packet.payload


class DvDongleDecoder:
    """Streaming length-prefixed deframer for the device -> host byte stream.

    Fed arbitrary chunks off a serial read via :meth:`feed`, it returns zero or more complete
    :class:`Packet` objects and retains any partial remainder for the next call. It self-synchronises
    on the header's length field: a length below the 2-byte minimum or above :data:`MAX_PACKET_LEN`
    is treated as desync, and the decoder drops one byte and retries (so a mid-stream glitch resyncs
    rather than wedging). Split a packet with :func:`classify`.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[Packet]:
        self._buf += chunk
        packets: list[Packet] = []
        while len(self._buf) >= HEADER_LEN:
            word = self._buf[0] | (self._buf[1] << 8)
            length = word & LENGTH_MASK
            if length < HEADER_LEN or length > MAX_PACKET_LEN:
                del self._buf[0]  # desync — drop a byte and resync on the next header
                continue
            if len(self._buf) < length:
                break  # need more bytes for this packet
            payload = bytes(self._buf[HEADER_LEN:length])
            packets.append(Packet(word >> 13, payload))
            del self._buf[:length]
        return packets

    def reset(self) -> None:
        self._buf.clear()
