"""Two ways to put a UV-K5 on a channel, behind one seam — so the backend does not care which.

`AiocBaofeng` keys this radio reliably (ADR 0141: 30/30) but has never been able to *choose* the
channel, which is the whole of the operator's complaint: 41 presets, 38 of them repeaters, and a
radio that transmits on whatever its front panel says. These are the two mechanisms that fix it.

**`SetVfoTuner` — `0x0873`, one frame, no reboot.** Sets the firmware's own VFO and lets its
`RADIO_ApplyOffset` / `RADIO_ConfigureSquelchAndOutputPower` / `RADIO_SetupRegisters` chain do the
rest, so the radio is genuinely on the channel — screen, split, tone, and the per-band PA
calibration the host cannot read out of flash. Needs F6 firmware. Confirmed by its `0x0874` reply,
which reports the frequencies the radio *landed on*, so a tune is a checked operation rather than a
hopeful write.

**`EepromTuner` — write the channel, reboot onto it.** Works on the firmware already on the radio,
needing no flash at all. Costs a soft reset (a few seconds, and the radio is deaf across it) and a
flash write per change. It exists because the alternative was asking the operator to flash before
anything worked at all, and because it is the fallback if `0x0873` ever regresses.

They differ in one way worth stating plainly: **`0x0873` sets RAM, the EEPROM path sets storage.**
A `setvfo` tune does not survive the operator power-cycling the radio, so the server re-applies on
connect; an EEPROM tune does survive, and is what the radio will still be on tomorrow.

Neither keys the radio, and neither is allowed to run while it is transmitting — the caller
enforces that (`AiocBaofeng`), because only it knows.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from ..base import Capability
from . import frames as f
from .transport import Uvk5Timeout
from .vfo import (
    BOOT_INDEX_BLOCK,
    BOOT_INDEX_LEN,
    HZ_PER_UNIT,
    VFO_RECORD_LEN,
    VfoImage,
    attr_addr,
    attribute_word,
    freq_channel,
    pack_boot_indices,
    unpack_boot_indices,
    vfo_addr,
)

logger = logging.getLogger(__name__)

__all__ = ["TuneError", "Uvk5Tuner", "SetVfoTuner", "EepromTuner", "TUNING_CAPS"]

#: What either tuner lets the backend advertise. `SET_CHANNEL` is absent on purpose: this radio has
#: no channel-select command and a preset is a host-side concept (ADR 0115).
TUNING_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.SET_FREQUENCY,
        Capability.SET_SPLIT,
        Capability.SET_TONE,
        Capability.SET_MODE,
    }
)

#: Any value works; the firmware stores whatever the host sends in `CMD_0514` and then requires the
#: same value on every EEPROM frame. Fixed rather than time-derived so a run is reproducible.
SESSION_TIMESTAMP = 0x12345678


class TuneError(RuntimeError):
    """A tune that did not take. Never raised for "probably fine" — only for a measured mismatch,
    a refusal from the radio, or silence where a reply was required."""


@runtime_checkable
class Uvk5Tuner(Protocol):
    def capabilities(self) -> frozenset[Capability]: ...
    def apply(self, image: VfoImage) -> None: ...


def _quantise(hz: int) -> int:
    """What the radio can actually hold: its VFO is in 10 Hz units, so sub-10 Hz is not
    representable. Compared against rather than the requested value, so a 5 Hz rounding is not
    reported as the radio having ignored us."""
    return (hz // HZ_PER_UNIT) * HZ_PER_UNIT


class SetVfoTuner:
    """`0x0873` + `0x0874`. Instant, no reboot, no flash wear. Requires F6 firmware."""

    name = "setvfo"

    def __init__(self, transport, *, timeout: float = 3.0):
        self._tp = transport
        self._timeout = timeout

    def capabilities(self) -> frozenset[Capability]:
        return TUNING_CAPS

    def apply(self, image: VfoImage) -> None:
        request = f.SetVfo(
            rx_hz=image.rx_hz,
            offset_hz=image.offset_hz,
            ctcss_tenths=image.ctcss_tenths,
            direction=image.direction,
            narrow=int(image.narrow),
            power=image.power,
        )
        try:
            reply = self._tp.request(
                request,
                match=lambda m: isinstance(m, f.SetVfoReply),
                timeout=self._timeout,
            )
        except Uvk5Timeout:
            reply = None
        if reply is None:
            # Silence is the one answer F6 never gives, so it means the wrong firmware — the
            # stock dispatch simply has no 0x0873 case and drops the frame without a word.
            raise TuneError(
                "no 0x0874 reply to the set-VFO frame. This firmware does not implement it "
                "(pre-F6); use the eeprom tuner, or flash F6."
            )
        if not reply.ok:
            raise TuneError(f"the radio refused the tune: {reply.status!r}")

        want_rx, want_tx = _quantise(image.rx_hz), _quantise(image.tx_hz)
        if (reply.rx_hz, reply.tx_hz) != (want_rx, want_tx):
            # The reply carries what the radio READ BACK out of its own VFO, so this is a real
            # disagreement about where it is pointed, not a formatting difference.
            raise TuneError(
                f"the radio is on rx={reply.rx_hz} tx={reply.tx_hz}, not the requested "
                f"rx={want_rx} tx={want_tx}"
            )
        if reply.ctcss_tenths != image.ctcss_tenths:
            raise TuneError(
                f"the radio is transmitting tone {reply.ctcss_tenths or 'none'}, not "
                f"{image.ctcss_tenths or 'none'} — a repeater expecting it will not open"
            )
        logger.info(
            "uvk5: tuned rx=%d tx=%d tone=%s power=%d (radio's own scale)",
            reply.rx_hz, reply.tx_hz, reply.ctcss_tenths or "none", reply.power,
        )


class EepromTuner:
    """Write the channel into the radio's storage and soft-reset onto it. No firmware change.

    Order matters and is fail-safe: everything is written and **read back** before the reset, so a
    radio that did not take the write is left running the old channel rather than rebooted into an
    unknown one.
    """

    name = "eeprom"

    #: The radio needs to boot, re-read its settings and bring the UART back up.
    REBOOT_SETTLE_S = 6.0

    #: **Every** dock conversation mutes the transmitter for six seconds, not just a write:
    #: `gSerialConfigCountDown_500ms = 12` is armed by `CMD_0514` (Hello) and `CMD_051B` (EEPROM
    #: *read*) as well as `CMD_051D` (`app/uart.c:355, 393, 447`). ADR 0137 read that lockout as a
    #: consequence of writing, and it is not.
    #:
    #: It matters because this tuner ends with a Hello and a read-back, so without waiting it
    #: returns "tuned" to a radio that will ignore the next six seconds of PTT — measured exactly
    #: that way on the bench: every carrier row failed on attempt #1 and passed thereafter.
    #: A tune is not finished until the radio can actually transmit.
    TX_LOCKOUT_S = 6.5

    def __init__(self, transport, *, timeout: float = 4.0, sleep=time.sleep):
        self._tp = transport
        self._timeout = timeout
        self._sleep = sleep
        self._hello_sent = False

    def capabilities(self) -> frozenset[Capability]:
        return TUNING_CAPS

    # -- wire helpers ---------------------------------------------------------------------

    def _hello(self) -> None:
        """Establish the session timestamp every EEPROM frame is checked against."""
        self._tp.send(f.Hello(timestamp=SESSION_TIMESTAMP))
        self._sleep(0.4)
        self._hello_sent = True

    def _read(self, offset: int, size: int) -> bytes:
        try:
            reply = self._tp.request(
                f.EepromRead(offset=offset, size=size, timestamp=SESSION_TIMESTAMP),
                match=lambda m: isinstance(m, f.EepromReadReply) and m.offset == offset,
                timeout=self._timeout,
            )
        except Uvk5Timeout:
            reply = None
        if reply is None:
            raise TuneError(f"no answer to an EEPROM read at {offset:#06x}")
        return bytes(reply.data)

    def _write(self, offset: int, data: bytes) -> None:
        try:
            reply = self._tp.request(
                f.EepromWrite(offset=offset, data=data, timestamp=SESSION_TIMESTAMP),
                match=lambda m: isinstance(m, f.EepromWriteReply),
                timeout=self._timeout,
            )
        except Uvk5Timeout:
            reply = None
        if reply is None:
            raise TuneError(f"no acknowledgement of an EEPROM write at {offset:#06x}")

    def _patch(self, offset: int, value: bytes) -> None:
        """Read-modify-write bytes that do not start on an 8-byte boundary.

        The firmware writes whole 8-byte chunks (`EEPROM_WriteBuffer`), so a short or unaligned
        payload does not update some bytes — it updates eight, with whatever followed in the
        buffer. Everything not being changed is read first and written back unchanged.
        """
        chunk_start = offset - (offset % f.EEPROM_CHUNK)
        span = offset + len(value) - chunk_start
        chunk_len = ((span + f.EEPROM_CHUNK - 1) // f.EEPROM_CHUNK) * f.EEPROM_CHUNK
        buffer = bytearray(self._read(chunk_start, chunk_len))
        lo = offset - chunk_start
        if buffer[lo:lo + len(value)] == value:
            return                          # already right; do not spend a flash write on it
        buffer[lo:lo + len(value)] = value
        self._write(chunk_start, bytes(buffer))

    # -- the tune -------------------------------------------------------------------------

    def apply(self, image: VfoImage) -> None:
        if not self._hello_sent:
            self._hello()

        band = image.band
        channel = freq_channel(band)
        record = image.pack_eeprom()

        # The attribute word gates everything. While it reads 0xFFFF the firmware never loads the
        # VFO record at all — it initialises to the band's lower edge and returns
        # (radio.c:302-313) — so the tune would appear to succeed and change nothing.
        self._patch(attr_addr(channel), attribute_word(band).to_bytes(2, "little"))

        # BOTH VFO slots, for the reason ADR 0141 cost four cycles to find: which VFO the radio
        # transmits from can be decided by a timer. Writing one and hoping is how half the overs
        # went out somewhere else.
        for vfo_index in (0, 1):
            self._patch(vfo_addr(band, vfo_index), record)

        # Frequency mode on this band, for both VFOs. Memory-channel indices are left alone.
        current = self._read(BOOT_INDEX_BLOCK, BOOT_INDEX_LEN)
        mr = unpack_boot_indices(current)["mr"]
        self._patch(
            BOOT_INDEX_BLOCK,
            pack_boot_indices(screen=(channel, channel), mr=mr, freq=(channel, channel)),
        )

        # Verify BEFORE rebooting. A radio left running the old channel is recoverable; one
        # rebooted onto a half-written record is a puzzle.
        for vfo_index in (0, 1):
            written = self._read(vfo_addr(band, vfo_index), VFO_RECORD_LEN)
            if written != record:
                raise TuneError(
                    f"VFO {vfo_index} read back as {written.hex(' ')}, expected "
                    f"{record.hex(' ')} — not rebooting"
                )

        self._tp.send(f.Reset())
        self._sleep(self.REBOOT_SETTLE_S)
        self._hello_sent = False            # the session timestamp did not survive the reboot
        self._hello()

        # Confirm it came back, and came back on the channel. Reading its storage is weaker
        # evidence than 0x0874's read-back of live state, and this says so rather than implying
        # otherwise: it proves the record persisted, not that the synthesiser followed it.
        after = self._read(vfo_addr(band, 0), VFO_RECORD_LEN)
        if after != record:
            raise TuneError(
                f"after the reboot the radio holds {after.hex(' ')}, not {record.hex(' ')}"
            )

        # That read just re-armed the lockout. Wait it out rather than hand back a radio that
        # silently ignores the next six seconds of PTT — see TX_LOCKOUT_S.
        self._sleep(self.TX_LOCKOUT_S)

        logger.info(
            "uvk5: wrote rx=%d tx=%d tone=%s to band %d and rebooted onto it",
            image.rx_hz, image.tx_hz, image.ctcss_tenths or "none", band,
        )
