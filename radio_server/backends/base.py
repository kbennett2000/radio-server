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
    #: Named ``clear_`` rather than ``set_`` because clearing is the whole of what this server can
    #: do. The wire (``0x0879``) also carries ON and TUNE; shipping a capability called
    #: ``set_broadcast_fm`` would advertise a switch with no on position. The next cycle widens it.
    #:
    #: **Earned, never assumed.** Unlike every other member, no backend advertises this from
    #: configuration: the tuner adds it only after a radio has actually answered ``0x087A``. The
    #: firmware that implements it is a fork branch that is neither merged nor flashed, so a static
    #: claim would have every station on earth asserting a firmware generation nobody is running
    #: (guardrail 1).
    CLEAR_BROADCAST_FM = "clear_broadcast_fm"
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
    when it runs, it holds the speaker line the AIOC listens on, so **the station hears nothing on
    its own channel**. It transmits normally throughout: ``RADIO_PrepareTX`` has no broadcast-FM
    term, so the automatic station ID (guardrail 5) goes out into a channel nobody is monitoring.
    That is worse than the demodulator fault ADR 0155 fixed, where the over was at least silent.

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
        """Set the operating mode, e.g. ``"FM"`` (CAT)."""
        ...

    def scan(self, on: bool) -> None:
        """Start (``True``) or stop (``False``) scanning (CAT)."""
        ...
