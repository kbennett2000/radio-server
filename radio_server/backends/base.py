"""Radio protocol surface and shared types.

The whole stack above the radio layer (DTMF decode, auth, sessions, services, TTS)
operates on sound-card audio and depends only on the shared :class:`Radio` surface.
CAT tuning (frequency/channel/tone/mode/scan) is a strict superset provided by
:class:`CatRadio`, implemented only by radios with serial control (the TM-V71A).

Guardrail (ADR 0001): PTT is keyed via the DATA port (SignaLink) or the AIOC serial
line — NEVER via a CAT ``TX`` command. ``ptt()`` and ``transmit()`` are the only
keying paths; no CAT method keys the radio. See docs/adr/0002 for the protocol shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Audio payloads carry their format and fail loud on a mismatch (ADR 0006). The type lives
# in the lowest layer, `radio_server.audio`; it is re-exported here so the protocol and its
# consumers keep importing it from `..backends`.
from ..audio import AudioFormat, AudioFormatMismatch, AudioFrame


class Capability(StrEnum):
    """A single operation a backend may or may not support.

    ``capabilities()`` returns the subset a backend actually implements; the API
    layer checks membership before dispatching so unsupported operations fail loudly
    (``UnsupportedCapability``) instead of silently no-op'ing (guardrail 3).
    """

    # Shared surface — every backend supports these.
    TRANSMIT = "transmit"
    RECEIVE = "receive"
    PTT = "ptt"
    STATUS = "status"

    # CAT-only surface — TM-V71A (serial control) only.
    SET_FREQUENCY = "set_frequency"
    SET_CHANNEL = "set_channel"
    SET_SPLIT = "set_split"
    SET_TONE = "set_tone"
    #: Channel **bandwidth** — wide (``FM``) or narrow (``NFM``). Not the demodulator: see
    #: :attr:`SET_MODULATION`, which is a different radio setting reached by a different frame.
    #: The two will be confused otherwise, because both spell one of their values ``"FM"``.
    SET_MODE = "set_mode"
    #: The **demodulator** — ``FM`` or ``AM`` (ADR 0150). What kind of signal the radio is
    #: listening to, as opposed to how much spectrum it listens across (:attr:`SET_MODE`). A
    #: backend can have either without the other, so they are advertised separately.
    SET_MODULATION = "set_modulation"
    #: Switch off the radio's **second receiver** — broadcast FM, the BK1080 (ADR 0157).
    #:
    #: Not a flavour of :attr:`SET_MODULATION`: that chooses how the BK4819 demodulates the
    #: station's own channel, this switches off a physically different chip that shares only the
    #: antenna front end and the audio amplifier. The station can be on a perfectly good
    #: demodulator and still hear nothing, because the BK1080 holds the speaker line the AIOC
    #: listens on — and transmit anyway, since ``RADIO_PrepareTX`` has no broadcast-FM term.
    #:
    #: Named ``clear_`` rather than ``set_`` because clearing was the whole of what this server could
    #: do when it was added. ADR 0164 supplied the on position (:attr:`SET_BROADCAST_FM`) and did
    #: **not** rename this one: the name is already published in ``/capabilities`` on every deployed
    #: station and a client may branch on it, so removing it to improve a word would be a breaking
    #: change bought with nothing. The two now gate the two directions of one route.
    #:
    #: **Earned, never assumed.** Unlike every other member, no backend advertises this from
    #: configuration: the tuner adds it only after a radio has actually answered ``0x087A``. The
    #: firmware that implements it is a fork branch that is neither merged nor flashed, so a static
    #: claim would have every station on earth asserting a firmware generation nobody is running
    #: (guardrail 1).
    #:
    #: Earned by **any** ``0x087A``, including ``ERR_NO_HAL`` — an image built without
    #: ``ENABLE_FMRADIO`` is a definitive *negative*, and "this station can never be deafened by a
    #: second receiver" is exactly what this capability claims.
    CLEAR_BROADCAST_FM = "clear_broadcast_fm"
    #: Switch the second receiver **on**, and retune a running one (ADR 0164). The other direction of
    #: :attr:`CLEAR_BROADCAST_FM`, and a separate member rather than a widening of it because the
    #: two are not earned by quite the same evidence.
    #:
    #: **Earned by any ``0x087A`` EXCEPT ``ERR_NO_HAL``.** That reply proves the opcode is there and
    #: proves there is no BK1080 behind it, so a station can advertise
    #: ``clear_broadcast_fm`` without this one — the pair a client is told how to read in
    #: ``docs/api.md``. The reverse cannot happen; if you see it, this server has a bug.
    SET_BROADCAST_FM = "set_broadcast_fm"
    SET_POWER = "set_power"
    SCAN = "scan"


#: The always-present shared operations.
SHARED_CAPS: frozenset[Capability] = frozenset(
    {Capability.TRANSMIT, Capability.RECEIVE, Capability.PTT, Capability.STATUS}
)

#: The CAT tuning operations, provided only by :class:`CatRadio` backends.
CAT_CAPS: frozenset[Capability] = frozenset(
    {
        Capability.SET_FREQUENCY,
        Capability.SET_CHANNEL,
        Capability.SET_SPLIT,
        Capability.SET_TONE,
        Capability.SET_MODE,
        Capability.SET_MODULATION,
        Capability.CLEAR_BROADCAST_FM,
        Capability.SET_BROADCAST_FM,
        Capability.SET_POWER,
        Capability.SCAN,
    }
)

#: Every capability — what a full-control (V71) backend advertises.
FULL_CAPS: frozenset[Capability] = SHARED_CAPS | CAT_CAPS


class UnsupportedCapability(Exception):
    """Raised when a CAT operation is attempted on a backend that lacks it.

    Carries the attempted :class:`Capability` so callers/the API can report exactly
    which operation is unavailable in the current mode.
    """

    def __init__(self, capability: Capability):
        self.capability = capability
        super().__init__(f"capability not supported in this mode: {capability}")


class RadioUnavailable(Exception):
    """The radio itself could not carry out an operation it does support.

    Distinct from :class:`UnsupportedCapability`, which is about what a *mode* can do and is
    answerable from configuration alone. This one is about the hardware in the room: it is
    powered off, mid-reboot, unplugged, locked, or it refused. The distinction matters at the
    API, where the first is a 501 the operator cannot fix and this is a 503 they usually can.

    It exists so the API layer can report a hardware fault without importing any particular
    backend's exceptions — a backend raises its own specific error, subclassed from this, and
    the message reaches the operator intact. Before it existed, a UV-K5 that had been switched
    off surfaced in the web UI as ``Request failed (500): Internal Server Error``, which tells
    the one person who could fix it nothing at all.
    """


class RadioBusy(RadioUnavailable):
    """The radio answered, promptly, and said **not right now** (ADR 0164).

    A `RadioUnavailable` subclass so every handler written before it still catches it — and a
    distinct class so the API can answer **409** where it would otherwise answer 503 purely by
    accident of inheritance. Those are different claims to whoever is reading them: 503 says the
    hardware is not there and sends someone to look at a cable; 409 says it is there, it is doing
    something else, and the request is worth making again in a second.

    It lives here rather than in the backend that first raised it (`Uvk5Tuner`'s `TuneBusy`) for a
    layering reason worth stating: the API decides status codes, and it must not have to import a
    particular backend's exception module to do it — the same argument that put `RadioUnavailable`
    here rather than leaving each backend's error to be special-cased at the edge. It completes the
    trio the API already maps: `UnsupportedCapability` → 501, this → 409, `RadioUnavailable` → 503.
    """


class RadioNotReady(Exception):
    """The request is fine and the radio can do this — something else has to happen first (ADR 0173).

    The case that named it: every setter on `AiocBaofeng` builds a channel, and a channel needs a
    receive frequency, so `set_mode`/`set_tone`/`set_split` refuse until `set_frequency` has been
    called. That is the state **every service restart leaves behind**, which makes it the ordinary
    path rather than an edge case.

    It answers **409**, the same as `RadioBusy`, and the pair divides on what the caller does next:

    - `RadioBusy` — change nothing, **wait**, send the identical request again. It clears by itself.
    - this — change nothing, make a **different** call first, then send the identical request again.
      It never clears by itself.

    Both are distinct from a `ValueError`, which the API answers **422** and which means the body is
    wrong. There is no body that makes an untuned station tuned, so 422 sends the operator to fix a
    value that was already correct. ADR 0164 had settled this one layer down — the radio's own
    `ERR_OFF`, a `tune` on a receiver that is off, is *"a state conflict, not a bad number"* and a
    409 — and until this class existed the host contradicted itself: 409 when the radio noticed, 422
    when the host did.

    Two things it deliberately is **not**, both load-bearing:

    - **not a `ValueError` subclass.** The per-route `except ValueError → 422` arms (ADR 0172) run
      before any app-wide handler, so a subclass would go straight back to wearing the value error's
      code, and those arms would stop meaning "the value is wrong".
    - **not a `RadioUnavailable` subclass.** That is a 503 the moment a route does not catch it —
      the accident of inheritance ADR 0164 introduced `RadioBusy` to escape, and there is no reason
      to walk back into it. 503 says the hardware is not there; this radio is right there, listening.

    Lives here, not in the backend that first raised it, for `RadioBusy`'s reason: the API decides
    status codes and must not import a particular backend's exception module to do it.
    """


@dataclass(frozen=True)
class PaState:
    """What the power amplifier was actually set to at the last key-up.

    The same argument as ``RadioStatus.rssi``: this is a number that decided how much RF left the
    antenna, and until now nobody could see it without stopping the service and opening the serial
    port by hand. A station whose transmissions are not reaching anything looks *identical* from
    the API to one that is working — every call returns 200 either way (ADR 0134).

    ``band_matched`` is the load-bearing field. The UV-K5's dock firmware sets the PA up from the
    radio's **own VFO**, not from the frequency the host tuned, and the per-band bias calibration
    lives in SPI flash the dock cannot read. So when the host transmits in the other band from the
    radio's front panel, ``bias`` is the *wrong band's* calibration and the radiated power is
    unknown (ADR 0132/0128). Reporting it is all the host can honestly do about it.
    """

    #: Reg 0x36 high byte — the firmware's own calibrated PA bias, read back from the radio.
    bias: int
    #: Reg 0x36 low byte the host **asked for**: the enable bit plus the band gain. Not read back —
    #: dock register writes are fire-and-forget, so a lost frame leaves the PA on the firmware's
    #: byte while this still reports the corrected one. ``bias`` and ``band_matched`` come from a
    #: read and are the trustworthy half; lead with those.
    gain: int
    #: True when the firmware's PA setup already named the band the host is transmitting in.
    #: False means ``bias`` came from the other band and the output level is not characterised.
    band_matched: bool
    #: The frequency the PA was set up for — the TX leg under a split, not the receive frequency.
    tx_frequency: int


@dataclass(frozen=True)
class BroadcastFm:
    """What the radio's **second receiver** is doing (ADR 0157).

    A UV-K5 carries a BK1080 commercial-FM chip (64-108 MHz) alongside the BK4819 that everything
    else drives. It shares the antenna front end and the audio amplifier and nothing else — and
    while it runs it holds the speaker line the AIOC listens on, so **between overs the station
    hears the broadcast and not its own channel**. It transmits normally throughout:
    ``RADIO_PrepareTX`` has no broadcast-FM term, so the automatic station ID (guardrail 5) goes out
    into a channel nobody is monitoring. That is worse than the demodulator fault ADR 0155 fixed,
    where the over was at least silent.

    **"Between overs" is doing real work in that sentence, and ADR 0163 measured it.** When a signal
    opens the squelch the firmware tears the BK1080 down (``APP_StartListening``) and passes real
    channel audio — the bench recovered a witness's 1000 Hz tone at power 0.995 through this exact
    path — restoring the broadcast 5 s after the over. So ``on=True`` means *the second receiver is
    selected*, not *this station is deaf this instant*. Anything acting on it is acting on the
    coarser claim, deliberately.

    **A block, not two fields**, for the reason :class:`PaState` is a block: the two values are only
    meaningful together, and flattening them would make ``on=None, hz=<a number>`` constructible and
    meaningless. And the block itself is tri-state — ``RadioStatus.broadcast_fm is None`` means the
    server does not know, while ``BroadcastFm(on=False, ...)`` means it knows and the answer is no.
    Those must never read the same: "we never asked" rendering identically to "verified hearing" is
    how a deaf station gets trusted.
    """

    #: Is the second receiver running? ``True`` means the station cannot hear its own channel.
    on: bool
    #: The BK1080's tuning in Hz, or ``None`` where the radio blanked it (any refusal, and the
    #: ``ERR_NO_HAL`` case where the image has no such receiver to tune).
    #:
    #: **Only meaningful with** :attr:`on`. The receiver remembers where it was, so the firmware
    #: reports a real frequency even when it is switched off — this is the frequency it *would
    #: resume on*, not what anything is listening to. Read alone it looks like a station happily
    #: monitoring 103.2 MHz.
    hz: int | None = None
    #: Is the **radio itself** refusing to transmit because of this? ``0x087A`` flags bit 1, the F9
    #: firmware interlock (ADR 0159), or ``None`` where nothing has reported it.
    #:
    #: It rides in this block rather than in `RadioStatus.tx_ok` because it came off this frame and
    #: belongs to this cause. `tx_ok` is the BK4819 demodulator, read from ``0x0878`` and paired with
    #: `modulation`; writing a broadcast-FM bit into it would break ADR 0155's invariant that the two
    #: are `None` together, and would leave the AM refusal reading a flag from a different frame than
    #: the demodulator its message names.
    #:
    #: **Tri-state, and the three answers are different claims.** ``True`` is an F9 radio refusing;
    #: ``False`` is an image that will key while deaf — the *more* dangerous state, and the reason
    #: the wire reports blocking rather than readiness; ``None`` is a backend with no firmware to
    #: ask, such as `MockRadio`.
    blocks_tx: bool | None = None

    #: How many key-ups this server has **rescued** — found the second receiver running immediately
    #: before a clear switched it off, so the over went out on a station that could hear its own
    #: channel (ADR 0162, closing ADR 0161 finding 5).
    #:
    #: Zero on every backend that cannot probe, and it stays a plain ``int`` rather than joining the
    #: tri-states around it because "how many times" genuinely has no unknown: a server that cannot
    #: ask has rescued nobody. It is here rather than in a log line because a repair nobody can see
    #: is barely better than the silent one it replaced — ADR 0161 recorded the silence as a limit of
    #: the wire, and it turned out to be a limit of the frame this server was choosing to send.
    rescues: int = 0

    #: The BK1080 band index the receiver is on — ``0`` 87.5-108, ``1`` 76-108, ``2`` 76-90,
    #: ``3`` 64-76 MHz — or ``None`` where nothing reported it (every refusal blanks the field to
    #: ``0xFF``, and ``0`` is a real reading).
    #:
    #: Carried since ADR 0164, because the ON route takes the band byte **explicitly** and half the
    #: answer to "where is this receiver" would otherwise be missing: 76.5 MHz means one station
    #: under band 1 and a different one under band 2. Read together with :attr:`hz`, and subject to
    #: the same caveat — the receiver remembers a band it is not currently using.
    band: int | None = None


def relay_mute_reason(broadcast_fm: "BroadcastFm | None") -> str | None:
    """Should a **relay** withhold this station's receive audio? The reason, or ``None`` to relay.

    The sibling of :func:`refuse_if_deafened`, for the other direction and a different body of law.
    That one stops this station **transmitting** blind; this one stops it **retransmitting** what its
    receiver is actually hearing, which while the BK1080 runs is a commercial broadcast station and
    not this channel at all. 97.113(b) forbids broadcasting, and an internet link does not terminate
    in a browser tab — the far end of a Mumble channel or a D-STAR reflector may be somebody else's
    RF repeater, which F9 knows nothing about and cannot refuse for.

    **It returns a string instead of raising, and that is the whole difference in posture.** A
    refused key-up is an event with an operator behind it; a muted relay happens fifty times a second
    with nobody watching, so the caller counts it and surfaces the reason rather than throwing.

    **The asymmetry is deliberate: the browser is not a caller.** Listening to broadcast FM in the
    browser is a feature, and the `/audio/rx` WebSocket subscribes to the same hub with no predicate.
    That is enforced by where this is called from rather than by a rule inside it — suppression lives
    in each relay's own loop, never at the `AudioHub`, so browser Listen and the recorder are
    untouched *by construction* rather than by an exemption somebody has to remember (ADR 0085).

    **Only a measured ``on=True`` mutes.** ``None`` is "nobody asked" — every backend with no dock
    tuner, every pre-F8 radio — and muting on it would take the links down across most of the fleet
    for a hazard nobody has evidence of. Same rule as `tx_ok`, same reason, one layer up.
    """
    if broadcast_fm is None or not broadcast_fm.on:
        return None
    # `if hz` and not `is not None`: a refusal blanks the field to 0, and 0 Hz is not a frequency.
    where = (
        f"{broadcast_fm.hz / 1e6:.1f} MHz"
        if broadcast_fm.hz
        else "an unreported frequency"
    )
    # Shares no clause, no identifier and no remedy with the two key-up refusals (ADR 0158 decision
    # 4): an operator reading a link's status must be able to tell "your link is quiet because your
    # radio is playing the radio" from "your transmitter refused", without matching wording.
    # The remedy names the host route FIRST since ADR 0164. It was "press EXIT on the radio" for four
    # cycles because that was true — there was no way out from the host side, which on an unattended
    # LAN station meant nobody. Naming only the front panel now would send someone to the wrong room.
    return (
        f"the radio's second receiver is playing broadcast FM ({where}), so what this station hears "
        f"is that broadcast and not its own channel. Relaying it would put a broadcast station onto "
        f"a link whose far end may be somebody else's repeater. Turn the second receiver off "
        f"(POST /broadcast-fm, or the SECOND RECEIVER card in the web UI), or press EXIT on the "
        f"radio; the relay resumes on its own."
    )


def refuse_if_deafened(broadcast_fm: "BroadcastFm | None") -> None:
    """Refuse a key-up on a station that cannot hear its own channel (ADR 0158).

    **The whole interlock is this predicate.** `AiocBaofeng` and `MockRadio` both call it, and the
    single implementation is the point rather than an accident of factoring: the message *is* the
    deliverable, and two copies of a message is exactly how two distinct causes converge into one
    undiagnosable "cannot transmit" — the failure this exists to prevent, reintroduced by the fix.

    Why this rule lives here and `_refuse_if_tx_disabled` does not. The two look alike and differ in
    kind. ``tx_ok=False`` is the **radio** refusing: the host raise is a *prediction* about
    hardware, and which hardware, because `VFO_STATE_TX_DISABLE` stops the PTT **pin** the AIOC
    drives and does nothing to the dock's register keying — so the same radio state means different
    things to different backends, and a per-backend rule belongs in a backend. This one is not a
    prediction at all. The radio transmits perfectly happily while its second receiver runs; that is
    the entire fault. The refusal is **host policy** — do not key a station that cannot hear its own
    channel — and the policy is identical everywhere. On a backend with no second receiver the block
    is permanently ``None`` and this is a no-op, so being universal costs nothing.

    **Only a definitive ``on=True`` refuses.** A ``None`` block is "nobody has asked this radio",
    which is every backend without a dock tuner and every radio on firmware that has no such
    command. An unmeasured field must never lock a transmitter — the `tx_ok` rule, for the same
    reason: a station that would not transmit until someone had measured is a worse failure than the
    one being prevented. Because :class:`BroadcastFm` is a tri-state *block*, "unknown" and
    "verified off" arrive here already distinguished; this predicate spends what ADR 0157 bought
    rather than trying to recover a distinction the type threw away.

    **What reaching here MEANS changed in ADR 0161, and the message changed with it.** On a backend
    that re-reads before every key-up, a block still saying ``on=True`` is no longer "the server
    remembers this from boot" — it is "the radio was asked, just now, and did not stop". That is a
    malfunction rather than an operating state, and the remedy is no longer a restart: the next
    key-up re-reads, so clearing the receiver at the front panel is the whole of it.
    """
    if broadcast_fm is None or not broadcast_fm.on:
        return
    # `if hz` and not `is not None`: the firmware blanks the field to 0 on a refusal, and 0 Hz is
    # not a frequency. Rendered exactly as `SetVfoTuner.clear_broadcast_fm`'s warning renders it, so
    # the boot log and this refusal name the same station the same way.
    where = (
        f"tuned to {broadcast_fm.hz / 1e6:.1f} MHz"
        if broadcast_fm.hz
        else "on an unreported frequency"
    )
    # The consequence is read from the radio, never assumed, because it differs per firmware IMAGE
    # and a host cannot see a build flag from the far end of a cable. `blocks_tx=True` is an F9
    # interlock refusing; anything else — an older image, an edition built without the interlock, or
    # a backend with no firmware to ask at all — will key while deaf, which is the worse state and
    # gets the blunter sentence. Defaulting the unknown to the harsher wording is deliberate: it
    # over-warns, and the alternative under-warns about a station that transmits blind.
    consequence = (
        "the radio itself refuses to key while it is running (F9 interlock), so nothing would go "
        "on the air"
        if broadcast_fm.blocks_tx
        else "this radio would transmit into it anyway, station ID included"
    )
    raise RadioUnavailable(
        f"the radio's second receiver is running ({where}) — it holds the speaker line, so this "
        f"station hears NOTHING on its own channel, and {consequence}. It was asked to stop and did "
        f"not. Turn it off from the host (POST /broadcast-fm, or the SECOND RECEIVER card in the web "
        f"UI) or press EXIT on the radio; this server re-reads the receiver before every key-up, so "
        f"the next one will pick it up on its own."
    )


@dataclass(frozen=True)
class TransportHealth:
    """Whether the backend's serial link is actually running (ADR 0166).

    The dock transport's reader thread dies permanently on any exception out of ``read()`` — a USB
    re-enumeration (hourly on the bench box), a cable event, or a second process opening the tty.
    Nothing restarted it and nothing watched it, while ``status()`` went on answering from cached
    model state, so a station that had not heard from its radio in an hour reported a frequency, a
    ``broadcast_fm`` block and ``tx_ok: true``. ADR 0163 caught it once by luck.

    ``alive`` is asked of the **thread**, never of the radio: polling the radio to find out whether
    the radio answers is the failure mode, not the detector, and it is the one call guaranteed to
    hang. So this block is cheap enough for the per-frame ``status()`` path.

    ``error`` is the exception text that killed the reader, or ``None`` — including after a clean
    close, because teardown is deliberate and must not read as a fault.
    """

    #: Is the reader thread running? ``False`` is broken, not degraded.
    alive: bool
    #: What killed it, verbatim, or ``None``. The operator needs the sentence, not a category.
    error: str | None = None
    #: The device it is (or was) reading, for the operator who has to go and look at it.
    port: str | None = None


@dataclass(frozen=True)
class RadioStatus:
    """A point-in-time snapshot of radio state.

    ``transmitting`` and ``busy`` are shared. The CAT fields are populated only by
    backends that support tuning; they stay ``None`` on audio-only backends.
    """

    backend: str
    transmitting: bool = False
    busy: bool = False
    # CAT-only — None unless the backend supports tuning.
    frequency: int | None = None
    #: The transmit frequency when a repeater split is armed, else ``None`` (simplex — TX = RX,
    #: i.e. ``frequency``). Never a mirror of ``frequency``: ``None`` means "not split", which is
    #: what every tuning call resets it to (ADR 0133).
    tx_frequency: int | None = None
    channel: int | None = None
    tone: float | None = None
    mode: str | None = None
    #: Raw received-signal-strength reading behind ``busy``, on whatever scale the backend's
    #: hardware uses, or ``None`` where there is no such number (a hardware carrier-detect pin,
    #: or software VAD). Diagnostic only — nothing decides anything on it. It exists because
    #: ``busy`` is a threshold applied to a number nobody could see: sizing that threshold meant
    #: stopping the service and opening the serial port by hand, on a bench where stopping the
    #: service is exactly what broke it (ADR 0127/0132). Now it can be read off a running station.
    rssi: int | None = None
    #: The PA setup observed at the last key-up, or ``None`` on a backend that cannot see one and
    #: before the first transmission of the process. Survives un-key deliberately: the question it
    #: answers ("why did that over not reach anything?") is asked *after* the over (ADR 0134).
    pa: PaState | None = None
    #: Seconds until this radio will accept a key-up, or ``None`` when it will accept one now.
    #:
    #: Only a UV-K5 being tuned over its dock reports a number here: the firmware mutes the
    #: transmitter for six seconds after any EEPROM conversation, refusing a key-up and cutting an
    #: over already in progress. Reported rather than merely waited out because a lockout nobody can
    #: see is the same fault as a 500 with no reason — the button looks live, the press does
    #: nothing, and nothing anywhere says why (ADR 0143). Relative, never an absolute deadline, so a
    #: status snapshot cannot go stale into a lie.
    tx_ready_in: float | None = None
    #: How hard this radio transmits — ``"low"``, ``"mid"``, ``"high"``, or ``None`` on a backend
    #: that cannot set or see it (ADR 0146).
    #:
    #: Reported from what the radio **confirmed**, not from what was asked for: the UV-K5 answers
    #: `0x0873` with the ``OUTPUT_POWER`` read back out of its own VFO. ``None`` also covers a radio
    #: sitting on a level this server did not choose — its front panel reaches ``LOW2``..``LOW5``
    #: and ``USER``, which have no name here, and naming one anyway would be inventing a number.
    power: str | None = None
    #: Whether the backend is storing the channels it tunes on the radio itself, or ``None`` where
    #: that is not a choice anyone has (every backend but a UV-K5 on the hybrid tuner, ADR 0145).
    #:
    #: The two states are a real trade, not a preference: stored survives the radio being switched
    #: off and costs six seconds of transmit lockout per change; instant costs nothing and is gone
    #: with the power. ``None`` and ``False`` are different answers — "no such switch" versus "the
    #: switch is off" — so the UI can hide the control rather than render one that does nothing.
    tune_persist: bool | None = None
    #: The demodulator the radio is on — ``"FM"``, ``"AM"``, or ``None`` (ADR 0150). Distinct from
    #: :attr:`mode`, which is bandwidth: a radio can be narrow-FM or wide-FM, and either of those
    #: or AM.
    #:
    #: Reported from what the radio **confirmed** — ``0x0878`` carries the modulation read back out
    #: of its own VFO after the firmware applied it, not the value it was handed. ``None`` means
    #: *not known*: on a backend that cannot set one, and on a UV-K5 before this server has
    #: asserted one. It is deliberately not defaulted to ``"FM"`` even though the firmware seeds
    #: FM, because that state is the radio's and reading it as ours would be adopting whatever we
    #: found (ADR 0132). A reconnecting host asserts; it does not assume.
    modulation: str | None = None
    #: Whether the radio will key its **own** transmit path right now, or ``None`` where that is
    #: not knowable (ADR 0150).
    #:
    #: ``False`` is a real operational state, not a fault: on a build without ``ENABLE_TX_WHEN_AM``
    #: the firmware sets ``VFO_STATE_TX_DISABLE`` for any non-FM modulation, and that is the path
    #: the radio's PTT **pin** drives — which is exactly where the `baofeng` backend keys, through
    #: the AIOC's DTR line. The dock's own register keying does not go through it and is
    #: unaffected, so the same radio state means different things to different backends and no
    #: caller can infer this one. Reported on every backend for that reason; never gated on which
    #: backend is selected. ``None`` and ``False`` are different answers.
    tx_ok: bool | None = None
    #: The radio's second receiver — see :class:`BroadcastFm` (ADR 0157).
    #:
    #: ``None`` means *not known*: on a backend with no such receiver, on firmware older than F8
    #: (which drops ``0x0879`` in silence), and on a radio that was switched off when this server
    #: started. It is emphatically **not** defaulted to "off": a station in broadcast FM hears
    #: nothing while transmitting normally, so reporting a confident "off" for a state nobody
    #: measured is the one wrong answer that gets a channel trusted.
    #:
    #: Written when this server **states** it — the ADR 0155 rule — not read on every ``status()``.
    #: So it is a record of the last assert, and an operator pressing the radio's own FM key
    #: afterwards is invisible to it. Carried as a known limitation rather than papered over.
    broadcast_fm: "BroadcastFm | None" = None
    #: Whether the serial link to the radio is actually running — see :class:`TransportHealth`
    #: (ADR 0166).
    #:
    #: ``None`` means *there is no serial transport here to report on*: the mock, and any backend
    #: whose radio is not on the other end of a tty. It is not "fine". Every other field in this
    #: dataclass is served from cached model state, so without this one a station whose reader died
    #: an hour ago is indistinguishable from a healthy one — which is exactly how ADR 0163's dead
    #: dock link went unnoticed while ``/status`` answered normally throughout.
    transport: "TransportHealth | None" = None


@runtime_checkable
class Radio(Protocol):
    """The shared surface every backend implements.

    Everything above the radio layer is written against this protocol so that a
    service behaves identically regardless of which radio is attached.
    """

    def transmit(self, audio: AudioFrame) -> None:
        """Key the radio and play ``audio`` out (PTT via DATA port / AIOC RTS)."""
        ...

    def receive(self) -> AudioFrame:
        """Return the most recent received audio from the sound card."""
        ...

    def ptt(self, on: bool) -> None:
        """Assert (``True``) or release (``False``) push-to-talk.

        Keyed via the DATA port (SignaLink self-key) or the AIOC serial line —
        never via CAT.
        """
        ...

    def status(self) -> RadioStatus:
        """Return a :class:`RadioStatus` snapshot."""
        ...

    def capabilities(self) -> frozenset[Capability]:
        """Return the set of operations this backend supports."""
        ...


@runtime_checkable
class CatRadio(Radio, Protocol):
    """A :class:`Radio` that additionally supports CAT tuning (TM-V71A only).

    NOTE: these signatures are intentionally minimal for cycle 1 and may be refined
    (tone type, scan parameters/return) via a future ADR once the CAT layer lands.
    """

    def set_frequency(self, hz: int) -> None:
        """Tune to ``hz`` (CAT). Does NOT key the radio.

        **Clears any armed split** — after this call the radio is simplex until :meth:`set_split`
        re-arms it. That direction is the fail-safe one: a TX leg that survived a retune would let
        an unattended station ID key a repeater's uplink (ADR 0133).
        """
        ...

    def set_channel(self, n: int) -> None:
        """Select memory channel ``n`` (CAT)."""
        ...

    def set_split(self, tx_hz: int | None) -> None:
        """Transmit on ``tx_hz`` while receiving on the tuned frequency, or ``None`` for simplex.

        Repeater access: the offset TX leg is applied inside the radio's own key path — tuned
        before the transmitter is enabled, and returned to the RX leg after the PA drops (ADR
        0133). Does NOT key. Backends validate ``tx_hz`` more strictly than ``set_frequency``
        validates its argument, because this one is a frequency that actually radiates.
        """
        ...

    def set_tone(self, tone: float | None) -> None:
        """Set the CTCSS/subaudible tone (Hz), or ``None`` to disable (CAT)."""
        ...

    def set_mode(self, mode: str) -> None:
        """Set the channel **bandwidth** — ``"FM"`` (wide) or ``"NFM"`` (narrow), case-insensitive
        (CAT).

        Not ``set_modulation``, which is the demodulator; the two are different radio settings that
        both spell one of their values ``"FM"`` (ADR 0150). Every implementation raises
        ``ValueError`` on anything else, and ``POST /mode`` turns that into a 422 (ADR 0172) — the
        vocabulary is stated here rather than left to "e.g." because that vagueness is what let a
        mock accept ``"AM"`` while the whole fleet refused it.
        """
        ...

    def scan(self, on: bool) -> None:
        """Start (``True``) or stop (``False``) scanning (CAT)."""
        ...
