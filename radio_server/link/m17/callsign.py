"""M17 base-40 callsign address encoding (ADR 0050).

M17 identifies a station by a 48-bit (6-byte) address. Up to nine characters from a 40-symbol
alphabet are packed into that address; decoding reverses it. This is the identity that rides in
every stream frame's LSF SRC field — "who is talking" is *in the stream*, and this module is
where a 6-byte address becomes a callsign string again.

The layout here was read from the M17 specification's address-encoding appendix, not recalled
(guardrail 1): the alphabet order, the accumulate-from-the-last-character rule, the big-endian
emission, and the reserved / standard / extended / BROADCAST address ranges.

One deliberate divergence from the spec's reference *encoder*: it silently maps any invalid
character to space and folds lowercase to uppercase. We instead **fail loud** — an unencodable
callsign is a locally-configured mistake that would otherwise put a wrong identity on the air —
so :func:`encode_callsign` raises :class:`CallsignError` rather than coercing. The *decoder* stays
tolerant (it faces untrusted RF), returning ``None`` for addresses that are not base-40 strings.
"""

from __future__ import annotations

#: The 40 symbols, ordered so that the index of a character *is* its base-40 value: space is 0,
#: ``A``-``Z`` are 1-26, ``0``-``9`` are 27-36, then ``-`` ``/`` ``.`` are 37-39.
ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-/."

#: Bytes in an M17 address, and the most characters that fit (40**9 < 2**48 <= 40**10).
ADDRESS_BYTES = 6
MAX_CALLSIGN_LEN = 9

#: The all-zero reserved address — a blank/space-only callsign encodes to this.
EMPTY = b"\x00" * ADDRESS_BYTES
#: The all-ones BROADCAST address (0xFFFFFFFFFFFF), valid only as a destination.
BROADCAST = b"\xff" * ADDRESS_BYTES

#: Inclusive top of the base-40-decodable "standard" range: 40**9 - 1 == 0xEE6B27FFFFFF, the
#: address of ``.........``. At or above 40**9 the value is not a base-40 string (extended /
#: BROADCAST space), and :func:`decode_callsign` returns ``None``.
STANDARD_MAX = 40**MAX_CALLSIGN_LEN - 1
_FIRST_UNDECODABLE = 40**MAX_CALLSIGN_LEN  # 0xEE6B28000000

_VALUE = {ch: i for i, ch in enumerate(ALPHABET)}


class CallsignError(ValueError):
    """Raised when a callsign cannot be encoded — an invalid character or one longer than nine.

    Subclasses :class:`ValueError` (house style, as :class:`~radio_server.audio.format.AudioFormatMismatch`
    does) and names the offending input, so a misconfigured station callsign fails loud rather
    than being silently coerced onto the air.
    """


def encode_callsign(callsign: str) -> bytes:
    """Encode a callsign string to its 6-byte big-endian M17 address.

    An empty or all-space callsign encodes to the reserved :data:`EMPTY` address. Otherwise every
    character must be in :data:`ALPHABET` and the length must not exceed :data:`MAX_CALLSIGN_LEN`,
    else :class:`CallsignError` is raised (we fail loud rather than coerce, unlike the spec's
    reference encoder). Characters accumulate from the last to the first, so the first character
    lands in the least-significant bits.
    """
    if len(callsign) > MAX_CALLSIGN_LEN:
        raise CallsignError(
            f"callsign {callsign!r} is {len(callsign)} characters; "
            f"the maximum is {MAX_CALLSIGN_LEN}"
        )

    value = 0
    for ch in reversed(callsign):
        try:
            value = 40 * value + _VALUE[ch]
        except KeyError:
            raise CallsignError(
                f"callsign {callsign!r} contains {ch!r}, which is not in the M17 alphabet "
                f"({ALPHABET!r})"
            ) from None

    return value.to_bytes(ADDRESS_BYTES, "big")


def decode_callsign(address: bytes) -> str | None:
    """Decode a 6-byte M17 address to its callsign string, or ``None`` if it is not a base-40 one.

    The reserved all-zero address decodes to ``""``. Addresses in the standard range decode to a
    callsign. Addresses at or above ``40**9`` — the extended range and :data:`BROADCAST` — are not
    base-40 strings, so this returns ``None`` (a caller that cares compares the raw bytes to
    :data:`BROADCAST`). A wrong-length input is local misuse and raises :class:`CallsignError`.
    """
    if len(address) != ADDRESS_BYTES:
        raise CallsignError(
            f"an M17 address is {ADDRESS_BYTES} bytes; got {len(address)}"
        )

    value = int.from_bytes(address, "big")
    if value >= _FIRST_UNDECODABLE:
        return None

    chars = []
    while value:
        chars.append(ALPHABET[value % 40])
        value //= 40
    return "".join(chars)
