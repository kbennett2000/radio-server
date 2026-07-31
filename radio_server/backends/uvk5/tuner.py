"""Three ways to put a UV-K5 on a channel, behind one seam — so the backend does not care which.

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

**`HybridTuner` — both, and no reboot.** `0x0873` moves the RF now; the EEPROM write then makes it
survive. The reset existed only to make the firmware *load* the record, and `0x0873` has already
done that, so the fourteen seconds go away. This is the one to use on F6, and its second half is a
**live switch** (`persist`, default off, ADR 0145) because persistence is the only thing that costs
the six-second transmit lockout, and whether that is worth paying changes with the afternoon.

They differ in one way worth stating plainly: **`0x0873` sets RAM, the EEPROM path sets storage.**
A `setvfo` tune does not survive the operator power-cycling the radio; an EEPROM (or persisting
hybrid) tune does, and is what the radio will still be on tomorrow.

The RAM half has a consequence the host has to handle rather than describe: after a power cycle the
radio is on a *stale* channel while the server still reports the one it chose, so a key-up would go
out on the wrong frequency with nothing detecting it. Hence `volatile` and `reassert` — a tuner says
whether its tunes can evaporate, and the backend puts the channel back one frame before the line
goes high (`AiocBaofeng._reassert_channel`).

*(An earlier version of this docstring claimed the server re-applies the channel on connect. It
does not — `apply_preset` has one call site, the HTTP route. Persistence comes from storage, not
from a re-apply that was never written.)*

Neither keys the radio, and none may run while it is transmitting — the caller enforces that
(`AiocBaofeng`), because only it knows.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from ..base import Capability, RadioUnavailable, UnsupportedCapability
from . import frames as f
from .transport import Uvk5Timeout
from .vfo import (
    BOOT_INDEX_BLOCK,
    BOOT_INDEX_LEN,
    FIRMWARE_POWER,
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

__all__ = [
    "TuneError", "Uvk5Tuner", "SetVfoTuner", "EepromTuner", "HybridTuner", "TUNING_CAPS",
    "SETVFO_CAPS", "VALID_MODULATIONS", "SERIAL_TX_LOCKOUT_S",
]

#: What **every** tuner here lets the backend advertise. `SET_CHANNEL` is absent on purpose: this
#: radio has no channel-select command and a preset is a host-side concept (ADR 0115).
TUNING_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.SET_FREQUENCY,
        Capability.SET_SPLIT,
        Capability.SET_TONE,
        Capability.SET_MODE,
        Capability.SET_POWER,
    }
)

#: The extra one a tuner that speaks `0x0877` adds (ADR 0150). This is why the tuners no longer
#: share one frozenset: `EepromTuner` writes a channel record whose modulation nibble is a
#: hardcoded FM (`vfo.py`) into **stock** firmware that has no `0x0877` case at all, so it cannot
#: do this and must not say it can (guardrail 3).
SETVFO_CAPS: frozenset[Capability] = TUNING_CAPS | frozenset({Capability.SET_MODULATION})

#: The demodulators this server will ask for. `USB` is a number the wire reserves and the firmware
#: refuses at F7 — accepting it later is additive, claiming it now would be a confident answer
#: nobody has checked (guardrail 1).
VALID_MODULATIONS: frozenset[str] = frozenset(f.MODULATION_VALUES)

#: Any value works; the firmware stores whatever the host sends in `CMD_0514` and then requires the
#: same value on every EEPROM frame. Fixed rather than time-derived so a run is reproducible.
SESSION_TIMESTAMP = 0x12345678

#: How long the firmware mutes the transmitter after any EEPROM conversation.
#:
#: `gSerialConfigCountDown_500ms = 12` — six seconds — armed at exactly four sites in `app/uart.c`:
#: 355 (HELLO), 393 (EEPROM **read**), 447 (EEPROM write), 586 (`CMD_052F`). Nothing else arms it,
#: and in particular **the dock opcodes do not**, which is what makes `0x0873` free and EEPROM
#: traffic expensive. `SerialConfigInProgress()` both refuses a key-up and *terminates an over in
#: progress* (`app/app.c:1111, 1146, 1191`), so this is not advisory.
#:
#: The half second is margin: the countdown is decremented by a 500 ms scheduler tick, so the true
#: window is up to one tick longer than six seconds.
SERIAL_TX_LOCKOUT_S = 6.5


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

    def set_modulation(self, modulation: str) -> bool:
        """Put the radio on ``"FM"`` or ``"AM"``; return whether it will key its own PTT path.

        A one-shot frame of its own (`0x0877`), **not** part of a channel: the modulation is not on
        `0x0873`'s wire, and the firmware keeps it for the session and applies it to every later
        tune. Arms no lockout and never keys.

        A tuner that cannot do this must **raise** — `UnsupportedCapability`, matching the 501 the
        capability gate would have given — rather than return. A silent no-op here reports success
        for a radio still listening to the wrong thing, which is the fault this whole reply-carrying
        protocol exists to stop (ADR 0150).
        """
        ...

    #: The demodulator the radio **confirmed**, or ``None`` before this server has asserted one.
    #: Never seeded from a read of the radio (ADR 0132).
    modulation: str | None

    #: Whether the radio will key its own transmit path in that modulation, or ``None`` when
    #: unknown. See `RadioStatus.tx_ok` — ``None`` and ``False`` are different answers.
    tx_ok: bool | None

    def reassert(self, image: VfoImage) -> None:
        """Put the radio back on ``image`` cheaply enough to run in the RF key path.

        Called immediately before a key-up when :attr:`volatile` — see
        `AiocBaofeng._reassert_channel`. Must be fast, must not arm the TX lockout, and must not
        reboot the radio; raise `TuneError` rather than return on an unconfirmed tune, because the
        caller is about to transmit on whatever frequency the radio is actually sitting on.
        """
        ...

    #: `time.monotonic()` after which the radio will accept a key-up again, or ``None`` when it
    #: will accept one now. See :data:`SERIAL_TX_LOCKOUT_S` — a tuner that talks EEPROM leaves the
    #: transmitter muted, and whoever is about to key is the only one who needs to care.
    tx_ready_at: float | None

    #: True when a tune this tuner made lives only in the radio's **RAM**, so switching the radio
    #: off silently returns it to whatever its storage holds. The server's `status()` reports the
    #: channel it chose, so on a volatile tuner that report — and the UI highlight built on it —
    #: goes stale the moment somebody uses the power switch, with nothing anywhere detecting it.
    #: Whoever is about to key is again the only one who can act on that, and does.
    volatile: bool


def _quantise(hz: int) -> int:
    """What the radio can actually hold: its VFO is in 10 Hz units, so sub-10 Hz is not
    representable. Compared against rather than the requested value, so a 5 Hz rounding is not
    reported as the radio having ignored us."""
    return (hz // HZ_PER_UNIT) * HZ_PER_UNIT


class SetVfoTuner:
    """`0x0873` + `0x0874`. Instant, no reboot, no flash wear. Requires F6 firmware."""

    name = "setvfo"

    #: Never blocks the transmitter: the dock opcodes do not arm the lockout (SERIAL_TX_LOCKOUT_S).
    tx_ready_at: float | None = None

    #: `gEeprom.VfoInfo[]` is RAM. Switch the radio off and this tune is gone.
    volatile = True

    def __init__(self, transport, *, timeout: float = 3.0):
        self._tp = transport
        self._timeout = timeout
        #: Seeded `None`, from nothing — NOT from a read of the radio and NOT from the FM the
        #: firmware happens to seed its own sticky value with. Adopting either would be the "take
        #: whatever state you find" fault ADR 0132 removed, and it would report a demodulator this
        #: server never chose. `None` means "not asserted yet", which is the truth.
        self.modulation: str | None = None
        self.tx_ok: bool | None = None

    def capabilities(self) -> frozenset[Capability]:
        return SETVFO_CAPS

    def set_modulation(self, modulation: str) -> bool:
        """`0x0877` + its `0x0878` read-back. One frame, no session, no lockout, no keying.

        The reply is checked against the request the same way `apply` checks the frequencies: it
        carries what the firmware read back out of the radio's **own VFO** after applying, so a
        disagreement is the radio saying it is on a different demodulator — not a formatting
        difference, and not something to log and continue past.
        """
        want = str(modulation).strip().upper()
        if want not in VALID_MODULATIONS:
            choices = ", ".join(sorted(VALID_MODULATIONS))
            raise ValueError(f"modulation must be one of: {choices}; got {modulation!r}")

        try:
            reply = self._tp.request(
                f.SetModulation(f.MODULATION_VALUES[want]),
                match=lambda m: isinstance(m, f.SetModulationReply),
                timeout=self._timeout,
            )
        except Uvk5Timeout:
            reply = None
        if reply is None:
            # F7 answers 0x0877 in every case, including a refusal, so silence means either the
            # frame reached firmware with no 0x0877 case (a pre-F7 dispatch drops an unknown opcode
            # without a word) or it reached nothing at all. Both are actionable, neither is
            # distinguishable from here, so say both — as `apply` does for 0x0873.
            raise TuneError(
                "no 0x0878 reply to the set-modulation frame — either the radio is not powered on "
                "and cabled, or it is running pre-F7 firmware that has no set-modulation command "
                "(in which case the radio can only receive FM; flash F7)"
            )
        if not reply.ok:
            raise TuneError(f"the radio refused the modulation: {reply.status!r}")
        if reply.name != want:
            raise TuneError(
                f"the radio is demodulating {reply.name or 'something it cannot name'} "
                f"(raw {reply.raw}), not the requested {want}"
            )

        self.modulation, self.tx_ok = want, reply.tx_ok
        logger.info(
            "uvk5: demodulating %s (raw %d); the radio %s key its own PTT path",
            want, reply.raw, "will" if reply.tx_ok else "will NOT",
        )
        return reply.tx_ok

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
        want_power = FIRMWARE_POWER[image.power]
        if reply.power != want_power:
            # Checked rather than logged since ADR 0146 made power settable. `out->power` is read
            # out of `gEeprom.VfoInfo[0].OUTPUT_POWER` *after* the firmware applied it, so a
            # disagreement is the radio saying it is transmitting at a level nobody asked for —
            # and the whole scale is a trap: 0/1/2 assigned raw lands on LOW2 (ADR 0142).
            raise TuneError(
                f"the radio is set to output power {reply.power}, not {want_power} "
                f"({image.level}) — its OUTPUT_POWER scale runs USER, LOW1..LOW5, MID, HIGH"
            )
        logger.info(
            "uvk5: tuned rx=%d tx=%d tone=%s power=%s (%d on the radio's own scale)",
            reply.rx_hz, reply.tx_hz, reply.ctcss_tenths or "none", image.level, reply.power,
        )

    def reassert(self, image: VfoImage) -> None:
        """Exactly :meth:`apply` — one frame, its read-back, and no session.

        `0x0873` is the cheapest question this host can ask the radio: it needs no HELLO (see the
        `_hello` call sites — there are none here) and the dock opcodes arm no lockout, whereas a
        HELLO arms six seconds of it at `uart.c:355`. So *re-tuning* costs strictly less than
        *asking where it is*, and there is nothing to gain by making this cleverer.
        """
        self.apply(image)


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
    TX_LOCKOUT_S = SERIAL_TX_LOCKOUT_S

    #: This tuner sleeps the lockout out inside `apply()` rather than handing a deadline upward, so
    #: by the time it returns the radio will key. Unchanged from the 80/80 run that proved it.
    tx_ready_at: float | None = None

    #: The channel is in flash and the boot indices point at it, so the radio comes back on it.
    volatile = False

    #: How many times to re-offer the handshake while the radio is coming back from a reset.
    #: How long a UV-K5 takes to boot is not a constant, and one attempt turns a slow boot into
    #: a failed tune.
    REBOOT_HELLO_ATTEMPTS = 4

    #: This tuner never learns a demodulator, so it never reports one. It writes the channel record
    #: with a hardcoded FM nibble (`vfo.py`) into stock firmware that has no `0x0877` case, and
    #: `None` says "not known" rather than inventing the FM that record happens to hold.
    modulation: str | None = None
    tx_ok: bool | None = None

    def __init__(self, transport, *, timeout: float = 4.0, sleep=time.sleep):
        self._tp = transport
        self._timeout = timeout
        self._sleep = sleep
        self._session_ok = False

    def capabilities(self) -> frozenset[Capability]:
        return TUNING_CAPS

    def set_modulation(self, modulation: str) -> bool:
        """Refuse. This path cannot change the demodulator, and must not pretend otherwise.

        `0x0877` is a fork extension: stock firmware drops it in silence, and the EEPROM channel
        record this tuner writes carries a hardcoded FM. Raising `UnsupportedCapability` gives a
        direct caller exactly the 501 the capability gate would have given, naming the operation —
        whereas returning would report success for a radio still demodulating FM, which is the
        no-signal/no-measurement confusion in a new place (guardrail 3, ADR 0150).
        """
        raise UnsupportedCapability(Capability.SET_MODULATION)

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

    def _patch(self, offset: int, value: bytes) -> bool:
        """Read-modify-write bytes that do not start on an 8-byte boundary. True if it wrote.

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
            return False                    # already right; do not spend a flash write on it
        buffer[lo:lo + len(value)] = value
        self._write(chunk_start, bytes(buffer))
        return True

    # -- the tune -------------------------------------------------------------------------

    def write_channel(self, image: VfoImage) -> bool:
        """Put the channel in the radio's storage and verify it. Does **not** reboot.

        Split out from :meth:`apply` because `HybridTuner` needs exactly this and nothing else:
        with `0x0873` having already moved the RF, the reboot's only job — making the firmware
        *load* the record — is already done.

        Returns whether any byte was actually written, which is what decides whether the caller
        owes the six-second TX lockout. Re-selecting a channel the radio already holds writes
        nothing, so it costs no flash and no lockout.
        """
        # Pre-flight. This rewrites several EEPROM chunks, so find out that the radio is listening
        # BEFORE starting — and report a dead radio as a dead radio, rather than as a mystery
        # timeout at some address.
        self._session_ok = False
        self._hello()

        band = image.band
        channel = freq_channel(band)
        record = image.pack_eeprom()
        wrote = False

        # The attribute word gates everything. While it reads 0xFFFF the firmware never loads the
        # VFO record at all — it initialises to the band's lower edge and returns
        # (radio.c:302-313) — so the tune would appear to succeed and change nothing.
        wrote |= self._patch(attr_addr(channel), attribute_word(band).to_bytes(2, "little"))

        # BOTH VFO slots, for the reason ADR 0141 cost four cycles to find: which VFO the radio
        # transmits from can be decided by a timer. Writing one and hoping is how half the overs
        # went out somewhere else.
        for vfo_index in (0, 1):
            wrote |= self._patch(vfo_addr(band, vfo_index), record)

        # Frequency mode on this band, for both VFOs. Memory-channel indices are left alone.
        current = self._read(BOOT_INDEX_BLOCK, BOOT_INDEX_LEN)
        mr = unpack_boot_indices(current)["mr"]
        wrote |= self._patch(
            BOOT_INDEX_BLOCK,
            pack_boot_indices(screen=(channel, channel), mr=mr, freq=(channel, channel)),
        )

        # Verify before anything depends on it. A radio left running the old channel is
        # recoverable; one rebooted onto a half-written record is a puzzle.
        for vfo_index in (0, 1):
            written = self._read(vfo_addr(band, vfo_index), VFO_RECORD_LEN)
            if written != record:
                raise TuneError(
                    f"VFO {vfo_index} read back as {written.hex(' ')}, expected "
                    f"{record.hex(' ')} — storage does not hold this channel"
                )
        return wrote

    def apply(self, image: VfoImage) -> None:
        band = image.band
        record = image.pack_eeprom()
        self.write_channel(image)

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

    def reassert(self, image: VfoImage) -> None:
        """Nothing to do, and doing nothing is the *point* — do not make this call `apply`.

        `volatile` is False here because the channel is in flash with the boot indices pointing at
        it: a radio that was switched off comes back on it by itself. There is nothing to restore.

        And this runs from inside the key path. :meth:`apply` sends a `Reset` and sleeps out a
        six-second lockout, so wiring it here would reboot the radio at every key-up. The empty
        body is load-bearing; `tests/test_uvk5_tuner.py` asserts no `Reset` leaves this method.
        """
        return


class HybridTuner:
    """`0x0873` for the radio, EEPROM for tomorrow — instant, and it survives the power switch.

    The two mechanisms fail in opposite directions and the operator needs neither failure.
    `setvfo` retunes the synthesiser in about ten milliseconds (`Dock_SetVfo` ends in
    `RADIO_SelectVfos(); RADIO_SetupRegisters(true)`) but writes `gEeprom.VfoInfo[]`, which is
    **RAM** — switch the radio off and the channel is gone. The EEPROM path survives anything but
    costs a soft reset, and the reset is the entire fourteen seconds.

    Doing both removes the reason for the reset. `0x0873` has already made the firmware *load* the
    channel, which is the only thing rebooting achieved, so the EEPROM write becomes pure
    persistence and nothing has to wait for a boot.

    **What it costs, stated exactly.** EEPROM traffic arms the firmware's six-second transmit
    lockout and the dock opcodes do not (see :data:`SERIAL_TX_LOCKOUT_S`). So a stored channel
    change is audible immediately and transmittable about six seconds later. Re-selecting a channel
    the radio already holds still costs the lockout — the handshake and the read-back arm it on
    their own (:meth:`_arm_lockout`) — but costs no flash. What costs nothing at all is re-tapping
    the channel the *server* already believes it is on, which never reaches a tuner.

    That deadline is published in :attr:`tx_ready_at` rather than slept through here. Blocking for
    six seconds inside a tune would throw away the instant part for a listener who is not about to
    transmit; the only caller who needs to care is the one about to key, and it waits there.
    Returning without doing *either* is what ADR 0142 got wrong — a radio that will silently ignore
    the next six seconds of PTT must not be reported as ready.

    **The second half is optional, and off by default** (:attr:`persist`, ADR 0145). Persistence is
    the *only* thing that costs the lockout, and whether it is worth six seconds is a question about
    the operator's afternoon, not about the radio: someone chasing repeaters wants to talk now and
    does not care what the radio holds tomorrow; someone about to unplug it wants the opposite. So
    it is a live switch (`AiocBaofeng.set_tune_persist`) rather than a startup mode — a choice you
    change with the situation should not cost a service restart.

    With it off this is `setvfo` with the RAM problem handled elsewhere: :attr:`volatile` goes True,
    and the backend re-asserts the channel before every key-up so a radio that was switched off
    cannot transmit on a stale one.
    """

    name = "hybrid"

    def __init__(
        self,
        setvfo: "SetVfoTuner",
        eeprom: "EepromTuner",
        *,
        persist: bool = False,
        now=time.monotonic,
    ):
        self._setvfo = setvfo
        self._eeprom = eeprom
        self._now = now
        self.persist = persist
        self.tx_ready_at: float | None = None

    @property
    def volatile(self) -> bool:
        """RAM-only exactly when storage is not being written."""
        return not self.persist

    def capabilities(self) -> frozenset[Capability]:
        return SETVFO_CAPS

    def set_modulation(self, modulation: str) -> bool:
        """The `0x0877` half only — never the EEPROM half, and not even when :attr:`persist` is on.

        Persistence is about the *channel*: `write_channel` stores a VFO record, and a demodulator
        is not part of one this firmware would read back. There is nothing here to store, so this
        stays a single frame regardless of the switch — and costs no lockout either way.
        """
        return self._setvfo.set_modulation(modulation)

    @property
    def modulation(self) -> str | None:
        return self._setvfo.modulation

    @property
    def tx_ok(self) -> bool | None:
        return self._setvfo.tx_ok

    def apply(self, image: VfoImage) -> None:
        # RF first, and confirmed: 0x0874 reports the frequencies read back out of the radio's own
        # VFO after RADIO_ApplyOffset, so the radio is genuinely on the channel before a single
        # byte of flash is touched. It also fails fast on pre-F6 firmware, which is the one thing
        # worth discovering before rewriting storage.
        self._setvfo.apply(image)

        if not self.persist:
            # Instant: no EEPROM conversation happened, so nothing was armed — and deliberately
            # nothing is CLEARED either. A deadline still running belongs to earlier traffic and is
            # still real; zeroing it here would report a muted radio as ready, which is the one
            # mistake this whole mechanism exists to stop making.
            logger.info(
                "uvk5: tuned rx=%d tx=%d tone=%s (not stored — instant)",
                image.rx_hz, image.tx_hz, image.ctcss_tenths or "none",
            )
            return

        # Then persistence. No reset: the firmware is already running this channel.
        wrote = self._eeprom.write_channel(image)
        self._arm_lockout()

        logger.info(
            "uvk5: tuned rx=%d tx=%d tone=%s and %s",
            image.rx_hz, image.tx_hz, image.ctcss_tenths or "none",
            "stored it" if wrote else "storage already held it, so nothing was written",
        )

    def _arm_lockout(self) -> None:
        """Record that the radio is now muted, after **any** EEPROM conversation.

        Corrects ADR 0144, which armed this only when flash actually changed. The lockout is not a
        consequence of writing: `gSerialConfigCountDown_500ms` is set by the HELLO at `uart.c:355`
        and by every EEPROM **read** at `393`, as well as the write at `447`. `write_channel`
        opens with a handshake and ends with a read-back verify, so it arms the lockout every time
        it runs — including the run that finds the channel already there and writes nothing.

        This is the same fault ADR 0142 shipped and the bench caught by failing every carrier row
        on attempt #1: a radio reported ready while its firmware is ignoring PTT. The narrow case
        it survived in was a re-select, which `AiocBaofeng.commit_tuning` short-circuits before the
        tuner is reached at all — so it only surfaces when the server has forgotten the channel and
        the radio has not, i.e. after a service restart.
        """
        self.tx_ready_at = self._now() + SERIAL_TX_LOCKOUT_S

    def store(self, image: VfoImage) -> bool:
        """Write ``image`` to storage without retuning. Returns whether anything was written.

        For the switch being turned **on** while the radio already sits on a channel. Without this,
        "save the channel to the radio" would not save the channel currently on the radio — it would
        promise to save the next one, which is not what the words say.
        """
        wrote = self._eeprom.write_channel(image)
        self._arm_lockout()
        return wrote

    def reassert(self, image: VfoImage) -> None:
        """The `0x0873` half only. Never the EEPROM half: this runs in the key path."""
        self._setvfo.reassert(image)
