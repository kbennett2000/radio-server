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

from ..base import (
    BroadcastFm,
    Capability,
    RadioBusy,
    RadioUnavailable,
    UnsupportedCapability,
)
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


class TuneBusy(TuneError, RadioBusy):
    """The radio answered, promptly, and said **not right now** — a courtesy refusal, not a fault.

    Its own class because a caller in the key path has to tell it apart from the rest of
    :class:`TuneError`, and the bench proved that at some cost (ADR 0161). ``0x0879`` is refused with
    ``ERR_TX`` whenever ``gCurrentFunction`` is ``FUNCTION_TRANSMIT`` **or ``FUNCTION_MONITOR``** —
    the firmware declining to take the speaker and the LNA from someone who is listening. An open
    squelch is most of an active QSO, so before every key-up this refusal is *ordinary*.

    Treating it as a fault made a busy receiver into a station that would not transmit, and the first
    thing it stopped was the automatic station ID that guardrail 5 makes required controller
    behaviour. It stays a :class:`TuneError` subclass so every existing handler still catches it;
    only callers that care about the difference need to look.

    Also a `RadioBusy` since ADR 0164, which is what gets it a **409** at the API instead of
    the 503 it would otherwise inherit — without the API importing this module to find out.
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

    def clear_broadcast_fm(self) -> bool:
        """Switch the radio's **second receiver** off; return whether it is now off (ADR 0157).

        `0x0879` action=OFF with its `0x087A` read-back. A one-shot frame like :meth:`set_modulation`,
        reaching a different chip: the BK1080 commercial-FM receiver, which when running holds the
        speaker line the AIOC listens on, so the station hears nothing on its own channel — while
        transmitting normally, station ID included.

        A tuner that cannot do this must **raise** `UnsupportedCapability`, never return. Returning
        would report a cleared receiver on a station that is still deaf, and "off" is exactly the
        claim that gets a channel trusted (guardrail 3).
        """
        ...

    #: What the second receiver is doing, or ``None`` when this server has not learned it. See
    #: `BroadcastFm` — the block being ``None`` and the block saying ``on=False`` are different
    #: answers, and conflating them is how a deaf station gets reported as healthy.
    broadcast_fm: BroadcastFm | None

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
        #: The second receiver, seeded `None` for the same reason and with more force: a radio in
        #: broadcast FM hears nothing while still transmitting, so a default of "off" would be a
        #: confident wrong answer about the one state that silently defeats guardrail 5.
        self.broadcast_fm: BroadcastFm | None = None
        #: How many times `probe_broadcast_fm` found the second receiver running immediately before
        #: a clear switched it off — i.e. how many key-ups this server **rescued** from going out on
        #: a station that could not hear itself (ADR 0161 finding 5). Before the probe existed this
        #: was unknowable: both OFF legs answer byte-identically, so the repair was silent.
        self.broadcast_fm_rescues = 0
        #: Set False for good if a firmware ever ACCEPTS the out-of-band probe instead of refusing
        #: it — see `probe_broadcast_fm`. A probe that mutates is not a probe.
        self._probe_armed = True
        #: What the last probe saw, consumed by `clear_broadcast_fm` to tell a rescue from an
        #: ordinary clear. Not a state anyone else reads: `broadcast_fm` remains the one block.
        self._probe_saw_on = False
        #: Has a radio ever answered `0x087A` on this tuner? See `capabilities`.
        self._broadcast_fm_seen = False
        #: The same question for `SET_BROADCAST_FM`, and a **second** flag rather than a reuse of the
        #: first because the two are not earned by the same evidence: `ERR_NO_HAL` proves the opcode
        #: is there AND that there is no BK1080 behind it, which earns the clear and disproves the
        #: set. One flag would have a station advertising a switch for a receiver it does not have.
        self._set_broadcast_fm_seen = False

    def capabilities(self) -> frozenset[Capability]:
        """`SETVFO_CAPS`, plus `CLEAR_BROADCAST_FM` **once a radio has answered `0x087A`**.

        Every other capability in this package is a static claim derived from configuration, and
        `SET_MODULATION` already stretches that: it says "F7 firmware" on the strength of a tuner
        mode. This one refuses to stretch it further. F8 is a fork branch that is neither merged nor
        flashed, so a static member would have every station on earth advertising a command no radio
        can execute — a hardware fact asserted from memory, which is guardrail 1 exactly.

        Earned by **any** `0x087A`, including a refusal: a radio that refuses has still demonstrated
        it has the opcode, which is what the capability claims. Only silence earns nothing, because
        silence is what both pre-F8 firmware and an unplugged radio produce.

        Re-earned on every successful reply rather than once at boot. A boot-only probe would leave
        a radio that was switched off at startup missing the capability for ever, and unlike ADR
        0155's unknown modulation there is no operator remedy — a preset tap cannot conjure a
        capability back. A backend re-select also re-probes, because `RadioHolder.rebuild` and
        `_restore` construct a fresh backend and therefore a fresh tuner.

        `SET_BROADCAST_FM` (ADR 0164) rides the same rule with one exclusion: `ERR_NO_HAL` earns the
        clear and not the set. That reply proves the opcode is there *and* proves the image has no
        BK1080 behind it — a definitive negative for "can this station be deafened?" and a
        definitive negative for "can this station switch one on?" too. So the pair
        `clear_broadcast_fm` without `set_broadcast_fm` is reachable and means exactly that;
        `docs/api.md` tells a client so.
        """
        earned = frozenset()
        if self._broadcast_fm_seen:
            earned |= {Capability.CLEAR_BROADCAST_FM}
        if self._set_broadcast_fm_seen:
            earned |= {Capability.SET_BROADCAST_FM}
        return SETVFO_CAPS | earned if earned else SETVFO_CAPS

    def probe_broadcast_fm(self, *, timeout: float | None = None,
                           wire_timeout: float | None = None) -> bool | None:
        """Is the second receiver running? ``True``/``False``, or ``None`` when nothing was learned.

        `ProbeBroadcastFm` is an out-of-band TUNE, which `Dock_SetFm` refuses **before** it touches
        anything — so unlike `clear_broadcast_fm` this observes without repairing. ADR 0161 said no
        such read existed; it did, in a branch nobody had sent. Measured on hardware before this was
        written (ADR 0162 B1).

        **It writes nothing, and that is the hard requirement rather than an implementation detail.**
        `clear_broadcast_fm` stays the single writer of :attr:`broadcast_fm`, so nothing this returns
        can reach `refuse_if_deafened` and nothing it returns can refuse a key-up. The alternative is
        not hypothetical: recording ``on=True`` here, followed by a clear declined with `ERR_TX`
        (which deliberately leaves the last reading standing), would refuse the key-up of a **busy**
        station — ADR 0161's defect one cycle later, on the same code path.

        **It raises nothing**, for the same reason and with the same history. It runs immediately
        before a key-up, and a frame put on that path last cycle took the Part 97 station ID off the
        air twice in four minutes because one routine refusal was treated as a fault. Every status
        this cannot interpret, every timeout and every `OSError` therefore becomes ``None`` — the one
        answer that cannot refuse anything.

        The answer comes from **`status` alone**: `dock.c` blanks state, band, frequency and flags on
        every non-`APPLIED` reply, so `reply.on` is the 0xFF sentinel here and reading it would be a
        bug rather than a shortcut.

        **What ``True`` actually claims** (measured, ADR 0163 M3): the second receiver is *selected*,
        not that it holds the speaker this instant. When a real signal opens the squelch the firmware
        tears the BK1080 down and passes channel audio, but `APP_StartListening` never clears
        `gFmRadioMode` — so this answers ``True`` throughout an over the operator is hearing
        perfectly well. The bench measured exactly that: 1000 Hz recovered at 0.995 from the witness
        while this frame still said ``ERR_BAND``.

        ``timeout`` and ``wire_timeout`` exist for the cadence (ADR 0163) and default to today's
        behaviour for every other caller. A poll passes a short ``timeout``, because a poll holding
        the wire is time a key-up spends waiting for it, and ``wire_timeout=0`` so a poll competing
        with a tune skips the round instead of queueing.
        """
        if not self._probe_armed:
            return None
        self._probe_saw_on = False   # reset first: a stale True must never outlive its own probe
        try:
            reply = self._tp.request(
                f.ProbeBroadcastFm(),
                match=lambda m: isinstance(m, f.BroadcastFmReply),
                timeout=self._timeout if timeout is None else timeout,
                wire_timeout=wire_timeout,
            )
        except (Uvk5Timeout, OSError):
            # Pre-F8 firmware drops this frame without a word, and an unplugged radio is silent in
            # exactly the same way. Neither is distinguishable from here and neither needs to be:
            # the answer is "nobody knows", and the clear that follows will report the real fault.
            return None
        if reply is None:
            return None
        if reply.status is f.BroadcastFmStatus.ERR_OFF:
            return False        # TUNE refused because the receiver is off — a definitive negative
        if reply.status is f.BroadcastFmStatus.ERR_BAND:
            self._probe_saw_on = True
            return True         # it got PAST the off-check, so `gFmRadioMode` is set
        if reply.status is f.BroadcastFmStatus.APPLIED:
            # Impossible on firmware that refuses an out-of-band tune, which the bench radio does.
            # If some other build CLAMPS to the nearest legal channel instead, this frame just
            # retuned the operator's broadcast receiver and is not a read at all — so it is never
            # sent again on this tuner. It can mutate at most once, and never silently twice.
            self._probe_armed = False
            logger.error(
                "uvk5: this firmware ACCEPTED an out-of-band broadcast-FM tune instead of refusing "
                "it, so the state probe is not a read on this build and may have moved the second "
                "receiver. Disabling it. Report the firmware version — every image this was written "
                "against answers ERR_BAND."
            )
            return None
        # ERR_TX (transmitting or monitoring — routine before a key-up), ERR_BUSY, ERR_NO_HAL,
        # ERR_SHORT, ERR_FIELD. All of them mean the same thing to this caller: not learned.
        return None

    def clear_broadcast_fm(self) -> bool:
        """`0x0879` action=OFF + its `0x087A` read-back. One frame, no session, no keying.

        Reports what `gFmRadioMode` says **after** the firmware acted, never what was asked — the
        `0x0874` doctrine, and the case it protects against here is the worst one this server can
        get wrong. A firmware that answered APPLIED and left the receiver running would, under an
        echo, be reported off; an operator would then trust a channel the station cannot hear.

        Unlike `set_modulation` a stuck receiver is **not** a `TuneError`: the frame was answered,
        the state is known, and it is `on=True`. Raising would discard a measurement in favour of an
        exception, and the caller needs that measurement more than it needs the exception. Only a
        refusal or silence raises, because only those leave the state genuinely unknown.

        **Every failure below blanks `broadcast_fm` back to `None`** (ADR 0161). That was invisible
        while this ran exactly once, at boot, with nothing to overwrite. Since it runs before every
        key-up it is load-bearing: a previous key-up's `on=False` left standing after a re-read that
        learned nothing is a reading old enough to be a lie, rendered as a measurement — and "off" is
        precisely the claim that gets a deaf station trusted.
        """
        try:
            reply = self._tp.request(
                f.ClearBroadcastFm(),
                match=lambda m: isinstance(m, f.BroadcastFmReply),
                timeout=self._timeout,
            )
        except Uvk5Timeout:
            reply = None
        # `ERR_TX` is the ONE refusal that leaves the previous reading standing, and it is the only
        # exception to the blanking below. The radio answered — promptly — and named a condition of
        # the **BK4819** (`gCurrentFunction` is TRANSMIT or MONITOR). That does not refresh what is
        # known about the BK1080 and it does not invalidate it either, so throwing the last
        # measurement away would trade a real reading for an unknown and get nothing for it.
        if reply is not None and reply.status is f.BroadcastFmStatus.ERR_TX:
            self._broadcast_fm_seen = True      # it answered, so the opcode is there
            raise TuneBusy(
                "the radio is transmitting or monitoring, so it declined to touch its second "
                "receiver (ERR_TX) — the firmware will not take the speaker mid-over. Nothing was "
                "learned about broadcast FM, and nothing already known about it was lost."
            )
        # Before any raise below, never after: an early return added later must not be able to leave
        # a stale reading behind it.
        self.broadcast_fm = None
        if reply is None:
            # Nothing was learned, so nothing is recorded — `broadcast_fm` stays exactly the honest
            # `None`. Both causes named because neither is distinguishable from here, and as of
            # ADR 0157 the second is the OVERWHELMINGLY likely one: F8 is unmerged, so every radio
            # in existence drops this frame without a word.
            raise TuneError(
                "no 0x087A reply to the clear-broadcast-FM frame — either the radio is not powered "
                "on and cabled, or it is running pre-F8 firmware that has no broadcast-FM command "
                "(in which case this server cannot tell whether the radio can hear its own channel)"
            )

        # A reply of ANY status proves the firmware has the opcode, which is the whole claim the
        # capability makes. Set before the refusal check below, deliberately.
        self._broadcast_fm_seen = True

        if reply.status is f.BroadcastFmStatus.ERR_NO_HAL:
            # The one refusal that is a definitive NEGATIVE rather than an unknown: the image was
            # built without `ENABLE_FMRADIO`, so there is no BK1080 driver in it and broadcast FM
            # cannot be running. Reporting that as "unknown" would throw away the single status
            # this wire carries that is a certainty. `hz` stays None — there is no receiver to have
            # a tuning, and the firmware blanks the field anyway.
            # `blocks_tx=False` for the same reason and with the same force: there is no receiver, so
            # there is nothing that could be blocking the transmitter either.
            self.broadcast_fm = BroadcastFm(on=False, hz=None, band=None, blocks_tx=False,
                                            rescues=self.broadcast_fm_rescues)
            logger.info(
                "uvk5: this firmware has no broadcast-FM receiver compiled in, so the station "
                "cannot be deafened by one"
            )
            return True
        if not reply.ok:
            raise TuneError(f"the radio refused to clear broadcast FM: {reply.status!r}")

        on = reply.on
        if on is None:
            # APPLIED with a blanked state cannot happen on any firmware this was written against —
            # the blanking is applied only to non-APPLIED replies. Refuse to guess rather than
            # coerce a sentinel into `False`, which would be the one wrong answer that matters.
            raise TuneError(
                "the radio reported broadcast FM applied but named no state — refusing to report "
                "a station as hearing on the strength of a blanked field"
            )

        # `reply.fm_blocks_tx` IS recorded and `reply.tx_ok` is NOT, and the asymmetry is not a
        # change of mind about one of them (ADR 0161).
        #
        # Bit 0 has a partner. `tx_ok` is the BK4819 demodulator, read from `0x0878` and paired with
        # `modulation`; writing it from THIS frame would break ADR 0155's invariant that the two are
        # None together, and would leave `_refuse_if_tx_disabled` reading a flag from a different
        # frame than the demodulator its error message names.
        #
        # Bit 1 has none. It is the firmware's own broadcast-FM refusal (F9), it belongs to the cause
        # this block describes, it came off this reply, and it is recorded inside this block — so it
        # can never masquerade as the demodulator's answer. The two causes stay apart all the way to
        # the operator, which is what gives them two messages and two remedies.
        self.broadcast_fm = BroadcastFm(on=on, hz=reply.hz, band=reply.band,
                                        blocks_tx=reply.fm_blocks_tx,
                                        rescues=self.broadcast_fm_rescues)
        if on:
            logger.warning(
                "uvk5: the radio REFUSED to leave broadcast FM (still tuned to %s) — this station "
                "cannot hear its own channel%s. Press EXIT on the radio, or power-cycle it.",
                f"{reply.hz / 1e6:.1f} MHz" if reply.hz else "an unreported frequency",
                # The consequence is a property of the IMAGE, so it is read rather than assumed: an
                # F9 radio stops itself, and anything older transmits into the channel regardless.
                " and this firmware refuses its own PTT path while it is running (F9)"
                if reply.fm_blocks_tx
                else " and will still transmit on it, station ID included",
            )
        elif self._probe_saw_on:
            # ADR 0161 finding 5, closed. Both OFF legs answer byte-identically (ADR 0160 measured
            # it), so this reply cannot say whether it changed anything — but the probe sent moments
            # ago can, and it said the receiver was running. Between the two frames this server just
            # gave a deaf station its ears back, immediately before it transmits.
            #
            # WARNING, not INFO: the station was about to key while unable to hear its own channel,
            # and an operator who keeps seeing this is leaving broadcast FM on. Counted as well as
            # logged, because a rescue that only ever appears in a journal nobody reads is barely
            # better than the silent repair it replaced.
            self.broadcast_fm_rescues += 1
            # Re-stamp: the block above was built before this rescue was counted, and a block that
            # under-reports by exactly one is the sort of off-by-one nobody ever notices.
            self.broadcast_fm = BroadcastFm(
                on=on, hz=reply.hz, band=reply.band, blocks_tx=reply.fm_blocks_tx,
                rescues=self.broadcast_fm_rescues,
            )
            self._probe_saw_on = False
            logger.warning(
                "uvk5: this station was in broadcast FM and could not hear its own channel — "
                "switched the second receiver off before keying (rescue #%d). It was tuned to %s. "
                "Press EXIT on the radio to stop this happening on every over.",
                self.broadcast_fm_rescues,
                f"{reply.hz / 1e6:.1f} MHz" if reply.hz else "an unreported frequency",
            )
        else:
            logger.info("uvk5: broadcast FM is off; the station can hear its own channel")
        return not on

    def set_broadcast_fm(self, action: str, hz: int | None, band: int) -> bool:
        """`0x0879` action=ON or TUNE + its `0x087A` read-back; return whether it is now **on**.

        The other direction of :meth:`clear_broadcast_fm`, and the mirror of its posture at every
        point that matters.

        **It writes `broadcast_fm`, which ADR 0163 reserved to the clear.** The invariant that ADR
        was protecting is intact, because it was never about arity. The *probe* must not write, since
        a refusal blanks every field but the status byte and `dock.c` blanks them unconditionally —
        so a probe-written `on=True` would be a claim the evidence does not support, and would refuse
        a busy station's key-up on the strength of it. This frame receives an `APPLIED` reply
        carrying state, frequency, band and flags: the complete reading, from the firmware's own
        state, after it acted. Writing that is the `0x0874` doctrine, not an exception to it.

        **The refusals split three ways, and the split is the design.**

        - `ERR_TX` → `TuneBusy`, and the previous reading is **left standing** — the radio answered,
          promptly, and named a condition of the BK4819 that neither refreshes nor invalidates what
          is known about the BK1080 (ADR 0161 decision 6, unchanged).
        - `ERR_OFF` → also `TuneBusy`: a TUNE on a receiver that is off. *"TUNE is not a cheaper ON"*
          (`dock.h`) — a host stepping across the band must never be able to switch the station deaf
          by accident — so the remedy is "turn it on first", which is a state conflict rather than a
          bad number.
        - `ERR_BAND` → a plain `ValueError`, deliberately **not** a `TuneError`. The frequency is
          outside the band's own limits, which is the operator's number being wrong; it reaches the
          API as a 422 rather than a 503. The host keeps no copy of `BK1080_GetFreqLoLimit`'s
          `{875, 760, 760, 640}` — `dock.h` calls a second copy of a hardware table a drift hazard —
          so this is the only place that verdict can come from, and it is reported rather than
          pre-empted.

        Everything else — silence, `ERR_FIELD`, `ERR_BUSY`, `ERR_NO_HAL` — is a `TuneError`, and
        blanks the block for `clear_broadcast_fm`'s reason: a reading left standing after an attempt
        that learned nothing is old enough to be a lie.
        """
        # Before the wire, and this is where "refuse, never round" is actually enforced: the frame's
        # own `__post_init__` raises `ValueError` on an off-raster frequency or an out-of-range band,
        # so no code path in this server can put one on the wire. The API turns it into a 422.
        frame = f.SetBroadcastFm(action=action, hz=hz, band=band)
        try:
            reply = self._tp.request(
                frame,
                match=lambda m: isinstance(m, f.BroadcastFmReply),
                timeout=self._timeout,
            )
        except Uvk5Timeout:
            reply = None
        # Same two exceptions to the blanking below as the clear, for the same reason, plus the one
        # this frame adds. Both are refusals that leave the BK1080's state exactly as well known as
        # it was: `ERR_TX` says the BK4819 is busy, `ERR_OFF` says the receiver is off — which is
        # itself a definite fact, but one already reflected in whatever the block holds.
        if reply is not None and reply.status in (
            f.BroadcastFmStatus.ERR_TX, f.BroadcastFmStatus.ERR_OFF
        ):
            self._broadcast_fm_seen = True      # it answered, so the opcode is there
            self._set_broadcast_fm_seen = True
            if reply.status is f.BroadcastFmStatus.ERR_TX:
                raise TuneBusy(
                    "the radio is transmitting or monitoring, so it declined to touch its second "
                    "receiver (ERR_TX) — the firmware will not take the speaker mid-over. Nothing "
                    "was learned about broadcast FM, and nothing already known about it was lost."
                )
            raise TuneBusy(
                "the second receiver is not running, so there was nothing to retune (ERR_OFF) — "
                "TUNE moves a receiver that is already on and is deliberately not a cheaper ON, so "
                "a host stepping across the band cannot switch the station deaf by accident."
            )
        if reply is not None and reply.status is f.BroadcastFmStatus.ERR_BAND:
            self._broadcast_fm_seen = True
            self._set_broadcast_fm_seen = True
            # A ValueError and NOT a TuneError: the radio is fine, the number was wrong. The band
            # index is named because the same frequency is in band under a different table.
            raise ValueError(
                f"the radio refused {hz} Hz as outside band {band}'s own limits (ERR_BAND) — the "
                f"BK1080's limit tables live in the firmware and this server deliberately keeps no "
                f"copy of them, so this is the radio's verdict rather than a guess"
            )
        # Before any raise below, never after.
        self.broadcast_fm = None
        if reply is None:
            raise TuneError(
                "no 0x087A reply to the set-broadcast-FM frame — either the radio is not powered "
                "on and cabled, or it is running pre-F8 firmware that has no broadcast-FM command"
            )

        self._broadcast_fm_seen = True
        if reply.status is f.BroadcastFmStatus.ERR_NO_HAL:
            # Earns the CLEAR capability and not this one, and the asymmetry is the whole reason
            # they are two members. The image was built without `ENABLE_FMRADIO`: there is no BK1080
            # in it, so "this station cannot be deafened by a second receiver" is proven — and
            # "this station can switch one on" is disproven. `_set_broadcast_fm_seen` is deliberately
            # not set here.
            self.broadcast_fm = BroadcastFm(on=False, hz=None, band=None, blocks_tx=False,
                                            rescues=self.broadcast_fm_rescues)
            raise TuneError(
                "this firmware has no broadcast-FM receiver compiled in (ERR_NO_HAL), so there is "
                "nothing to switch on"
            )
        self._set_broadcast_fm_seen = True
        if not reply.ok:
            raise TuneError(f"the radio refused to set broadcast FM: {reply.status!r}")

        on = reply.on
        if on is None:
            raise TuneError(
                "the radio reported broadcast FM applied but named no state — refusing to report "
                "a receiver as running on the strength of a blanked field"
            )
        # Read back out of the firmware's own state, never echoed from the request (ADR 0156): the
        # Hz-to-raster conversion and the two-bit band field are both places where what the radio
        # holds can differ from what it was sent, and echoing is exactly what would hide either.
        self.broadcast_fm = BroadcastFm(on=on, hz=reply.hz, band=reply.band,
                                        blocks_tx=reply.fm_blocks_tx,
                                        rescues=self.broadcast_fm_rescues)
        logger.warning(
            "uvk5: the second receiver is now ON (%s) — this station will not relay what it hears "
            "to any link, and the next transmission will switch it off again",
            f"{reply.hz / 1e6:.1f} MHz" if reply.hz else "an unreported frequency",
        )
        return on

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

    #: And it never learns anything about the second receiver either — `0x0879` is a fork extension
    #: stock firmware drops in silence, so this tuner never sends one and never has an answer.
    broadcast_fm: BroadcastFm | None = None

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

    def clear_broadcast_fm(self) -> bool:
        """Refuse. This path cannot reach the second receiver, and must not pretend otherwise.

        `0x0879` is a fork extension (F8): stock firmware drops it without a word, and no EEPROM
        write this tuner performs touches `gFmRadioMode`. Raising gives a direct caller the same 501
        the capability gate would have, naming the operation — whereas returning would report a
        cleared receiver on a station that is still deaf, which is the worst available version of
        the no-signal/no-measurement confusion this guardrail exists to stop (ADR 0157).
        """
        raise UnsupportedCapability(Capability.CLEAR_BROADCAST_FM)

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
        """Delegated, so `CLEAR_BROADCAST_FM` is earned on this tuner exactly when the `0x0873` half
        earned it. Returning the static `SETVFO_CAPS` here would quietly re-introduce the
        assert-from-memory claim the setvfo half is careful not to make."""
        return self._setvfo.capabilities()

    def set_modulation(self, modulation: str) -> bool:
        """The `0x0877` half only — never the EEPROM half, and not even when :attr:`persist` is on.

        Persistence is about the *channel*: `write_channel` stores a VFO record, and a demodulator
        is not part of one this firmware would read back. There is nothing here to store, so this
        stays a single frame regardless of the switch — and costs no lockout either way.
        """
        return self._setvfo.set_modulation(modulation)

    def probe_broadcast_fm(self, *, timeout: float | None = None,
                           wire_timeout: float | None = None) -> bool | None:
        """Delegated to the `0x0873` half, which is the only one that speaks `0x0879` (ADR 0162)."""
        return self._setvfo.probe_broadcast_fm(timeout=timeout, wire_timeout=wire_timeout)

    @property
    def broadcast_fm_rescues(self) -> int:
        """Read through, so the count belongs to the tuner that did the rescuing."""
        return self._setvfo.broadcast_fm_rescues

    def clear_broadcast_fm(self) -> bool:
        """The `0x0879` half only, for the same reason and one stronger: the EEPROM half **cannot**
        do this at all — it exists to raise — so routing here is not a preference."""
        return self._setvfo.clear_broadcast_fm()

    def set_broadcast_fm(self, action: str, hz: int | None, band: int) -> bool:
        """Delegated for the same reason, so `SET_BROADCAST_FM` is earned on this tuner exactly when
        the half that sends the frame earns it (ADR 0164)."""
        return self._setvfo.set_broadcast_fm(action, hz, band)

    @property
    def modulation(self) -> str | None:
        return self._setvfo.modulation

    @property
    def tx_ok(self) -> bool | None:
        return self._setvfo.tx_ok

    @property
    def broadcast_fm(self) -> BroadcastFm | None:
        return self._setvfo.broadcast_fm

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
