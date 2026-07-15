"""M17's non-standard CRC-16 (ADR 0050).

The stream frame is protected by a 16-bit CRC that M17 defines with polynomial ``0x5935``, an
initial value of ``0xFFFF``, and — because M17's native bit order is most-significant-bit-first —
**no** reflection of either the input or the output. That combination is not any of the common
named CRC-16 variants, so it is implemented here bit-by-bit from the specification and pinned by
the four published test vectors (see :mod:`tests.test_m17_crc`), where a bit-order slip would show
up as a failing unit test rather than as a reflector silently discarding every frame.
"""

from __future__ import annotations

#: M17 CRC-16 polynomial: x^16 + x^14 + x^12 + x^11 + x^8 + x^5 + x^4 + x^2 + 1.
POLYNOMIAL = 0x5935
#: Initial (and, being non-reflected, final-XOR-free) register value.
INIT = 0xFFFF


def crc16(data: bytes) -> int:
    """Compute M17's CRC-16 over ``data``, returning the 16-bit checksum as an int.

    MSB-first, non-reflected: each byte is fed into the high end of the register, then eight
    shift-and-conditionally-xor steps are applied. A message concatenated with its own valid CRC
    checksums to zero (the standard trailing-CRC property).
    """
    crc = INIT
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ POLYNOMIAL) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc
