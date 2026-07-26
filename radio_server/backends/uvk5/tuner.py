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

from ..base import Capability, RadioUnavailable
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


class TuneError(RadioUnavailable):
    """A tune that did not take. Never raised for "probably fine" — only for a measured mismatch,
    a refusal from the radio, or silence where a reply was required.

    A :class:`~radio_server.backends.base.RadioUnavailable`, so the API reports it as a 503 with
    this message rather than a 500 with a stack trace. Every message here is read by an operator
    standing next to the radio, so it names what they can go and check.
    """


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
            # F6 always answers 0x0873, so silence means either the frame reached firmware that
            # has no 0x0873 case (the stock dispatch drops it without a word), or it reached
            # nothing at all. Both are actionable and they are not distinguishable from here,
            # so say both rather than assert the one that happens to be more interesting.
            raise TuneError(
                "no 0x0874 reply to the set-VFO frame — either the radio is not powered on and "
                "cabled, or it is running pre-F6 firmware that has no set-VFO command (in which "
                "case use the eeprom tuner, or flash F6)"
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

    #: How many times to re-offer the handshake while the radio is coming back from a reset.
    #: How long a UV-K5 takes to boot is not a constant, and one attempt turns a slow boot into
    #: a failed tune.
    REBOOT_HELLO_ATTEMPTS = 4

    def __init__(self, transport, *, timeout: float = 4.0, sleep=time.sleep):
        self._tp = transport
        self._timeout = timeout
        self._sleep = sleep
        self._session_ok = False

    def capabilities(self) -> frozenset[Capability]:
        return TUNING_CAPS

    # -- the session ----------------------------------------------------------------------
    #
    # Everything below exists because of four lines of stock firmware, at the top of every
    # EEPROM handler (`app/uart.c`, CMD_051B and CMD_051D):
    #
    #     if (pCmd->Timestamp != Timestamp)
    #         return;                    // no reply, no error, nothing on the wire
    #
    # The radio answers EEPROM frames only inside a session that HELLO establishes, and it
    # refuses **silently**. So "no answer" is not evidence of a broken cable — it is the only
    # way this firmware can say "you do not have a session", and it is indistinguishable from
    # a dead link unless the host goes and asks.

    def _hello(self, *, attempts: int = 1) -> None:
        """Establish the session — and confirm it, rather than assume it.

        The earlier version of this sent HELLO and slept, which meant a handshake that never
        landed (radio off, mid-reboot, sitting in the DFU bootloader after a flash) was
        invisible until every later read timed out for reasons that looked like hardware.
        `0x0515 IM_HERE` is the firmware's answer; wait for it and believe nothing until it
        arrives.
        """
        last: Uvk5Timeout | None = None
        for attempt in range(attempts):
            try:
                reply = self._tp.request(
                    f.Hello(timestamp=SESSION_TIMESTAMP),
                    match=lambda m: isinstance(m, f.ImHere),
                    timeout=self._timeout,
                )
            except Uvk5Timeout as exc:
                last = exc
                if attempt + 1 < attempts:
                    self._sleep(1.0)
                continue

            # A locked radio with a custom AES key still replies to reads — with zeroed data
            # (`if (!bLocked) EEPROM_ReadBuffer(...)`). That would surface as a baffling
            # read-back mismatch, so name it here instead.
            if reply.has_custom_aes_key and reply.in_lock_screen:
                self._session_ok = False
                raise TuneError(
                    "the radio is locked and its EEPROM reads back blank — unlock it on the "
                    "radio before tuning"
                )

            self._session_ok = True
            version = reply.version.split(b"\x00")[0].decode("ascii", "replace")
            logger.info("uvk5: dock session established; radio reports %r", version)
            return

        self._session_ok = False
        raise TuneError(
            "the radio did not answer the handshake — check it is powered on, is not in the "
            "bootloader, and that the AIOC cable is seated"
        ) from last

    def _exchange(self, attempt, what: str):
        """Run one request that needs a live session, re-establishing it once if it is gone.

        A radio that reboots underneath the server is ordinary — a firmware flash, a battery
        swap, the operator switching it off and on. Treating that as fatal is what turned a
        transient condition into a service that failed every tune until it was restarted. So
        silence costs one fresh handshake and one retry, and only then is it an error.
        """
        if not self._session_ok:
            self._hello()
        try:
            return attempt()
        except Uvk5Timeout:
            pass

        logger.warning("uvk5: %s went unanswered — re-establishing the session and retrying", what)
        self._session_ok = False
        self._hello()
        try:
            return attempt()
        except Uvk5Timeout as exc:
            raise TuneError(
                f"the radio stopped answering ({what}) even after a fresh handshake — check it "
                "is powered on and the AIOC cable is seated"
            ) from exc

    # -- wire helpers ---------------------------------------------------------------------

    def _read(self, offset: int, size: int) -> bytes:
        reply = self._exchange(
            lambda: self._tp.request(
                f.EepromRead(offset=offset, size=size, timestamp=SESSION_TIMESTAMP),
                match=lambda m: isinstance(m, f.EepromReadReply) and m.offset == offset,
                timeout=self._timeout,
            ),
            f"an EEPROM read at {offset:#06x}",
        )
        return bytes(reply.data)

    def _write(self, offset: int, data: bytes) -> None:
        self._exchange(
            lambda: self._tp.request(
                f.EepromWrite(offset=offset, data=data, timestamp=SESSION_TIMESTAMP),
                match=lambda m: isinstance(m, f.EepromWriteReply),
                timeout=self._timeout,
            ),
            f"an EEPROM write at {offset:#06x}",
        )

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
        # Pre-flight. This sequence rewrites several EEPROM chunks and then reboots the radio
        # onto them, so find out that the radio is listening BEFORE starting it — and report a
        # dead radio as a dead radio, rather than as a mystery timeout at some address.
        self._session_ok = False
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
        self._session_ok = False            # the session timestamp did not survive the reboot
        self._hello(attempts=self.REBOOT_HELLO_ATTEMPTS)

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
