"""Automatic station identification (guardrail 5, Part 97).

Every transmission the server makes is the licensee's station, so the controller must
identify automatically: on a <=10-minute interval while transmitting and at the end of a
session. `StationId` is the single seam through which all audio reaches the radio — the
`Dispatcher` transmits through it, not through the raw `Radio` — so it is structurally
impossible for a service transmission to go out un-ID'd.

This cycle is the *scheduling* logic, not tone generation. The ID audio is a deterministic
stub (`StubId`) so `tx_log` is exactly assertable, exactly as `StubTts` stubs speech; real
CW/voice encoders (`CwId`/`VoiceId`) implement the same one-method `IdEncoder` contract and
land after the audio-format ADR pins the frame layout.

Timing is driven by an injected clock, so the whole scheduler is unit-tested with a fake
clock and no real sleeps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..backends import AudioFrame, Radio
from ..auth import Clock

if TYPE_CHECKING:
    from ..config import Settings

#: Legacy env var name, retained as metadata (the config schema owns resolution now, ADR 0025).
RADIO_CALLSIGN_ENV_VAR = "RADIO_CALLSIGN"

#: Legacy env var name (see above).
RADIO_ID_INTERVAL_ENV_VAR = "RADIO_ID_INTERVAL"

#: Marked default ID interval. The legal maximum is 10 minutes. Referenced by the config schema.
DEFAULT_ID_INTERVAL = 600.0

#: The regulatory ceiling: identify at least every 10 minutes. A configured interval above
#: this is rejected, not clamped — a too-long interval is a misconfiguration to fix, not to
#: silently paper over. Enforced by the config schema's `coerce_id_interval`.
MAX_ID_INTERVAL = 600.0


def load_callsign(settings: Settings) -> str:
    """Return the station callsign (`station.callsign`).

    Fails loud (via `Settings.get`) when unset — transmitting without a real callsign is illegal,
    so an unconfigured station must fail loudly rather than key up with a placeholder.
    """
    return settings.get("station.callsign")


def load_id_interval(settings: Settings) -> float:
    """Return the ID interval in seconds (`station.id_interval`), validated against the Part 97
    ceiling by the config schema."""
    return settings.get("station.id_interval")


@runtime_checkable
class IdEncoder(Protocol):
    """Renders a callsign to the audio that identifies the station.

    One method, mirroring `TtsEngine`: real CW (`CwId`) and voice (`VoiceId`) encoders
    implement the same contract, so nothing above this layer changes when they land.
    """

    def encode(self, callsign: str) -> AudioFrame: ...


class StubId:
    """Deterministic ID audio keyed to the callsign, so `tx_log` is exactly assertable.

    Placeholder until a real CW/voice encoder lands (`CwId` on the `synth_tone` substrate,
    cycle 6). Output is a pure function of the callsign — a symbolic payload in a
    canonical-format frame — so `tx_log` is exactly assertable: ``AudioFrame(b"<id:AE9S>")``.
    """

    def encode(self, callsign: str) -> AudioFrame:
        return AudioFrame(b"<id:" + callsign.encode("utf-8") + b">")


class StationId:
    """The transmit seam that guarantees no transmission goes out un-ID'd.

    Owns the radio; every path that sends audio funnels through here:

    - `transmit(audio)` — a service's content. When an ID is *due* it is prepended into the
      same over (one keyup) so the transmission carries ID; otherwise the content goes out
      alone. Within-interval transmissions do not repeat the ID.
    - `check(now)` — a clock-driven safety net (a real scheduler task calls it): forces an
      ID-only transmission if the session has been active past the interval since its last
      ID.
    - `sign_off(now)` — the closing ID at session end, sent only if the station transmitted.

    "Due" is measured from the last ID, not the last transmission: the Part 97 invariant is
    "<=10 minutes since the last identification," so a session that keeps transmitting still
    re-IDs on schedule.
    """

    def __init__(
        self,
        radio: Radio,
        encoder: IdEncoder,
        callsign: str,
        *,
        interval: float = DEFAULT_ID_INTERVAL,
        clock: Clock | None = None,
        mode: str = "cw",
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._radio = radio
        self._encoder = encoder
        self._callsign = callsign
        self._interval = interval
        self._clock = clock
        # The ID modulation actually keyed (`"cw"` | `"voice"`), from `load_id_mode`. Retained so a
        # `station_id` ledger record can say *what* identified the station (ADR 0019); the encoder
        # itself is `cw`/`voice`/stub and does not carry its mode name. The default matches
        # `voice_id.DEFAULT_ID_MODE` (not imported: `voice_id` depends on this module) and is
        # overridden by the explicit `load_id_mode(env)` value in `build_controller`.
        self._mode = mode
        # Per-session state. `last_id is None` means nothing has been ID'd this session, so
        # the next transmission is the first over and is always identified.
        self._transmitted_this_session = False
        self._last_id: float | None = None

    @property
    def callsign(self) -> str:
        """The station callsign this identifies with (for the `station_id` ledger record)."""
        return self._callsign

    @property
    def mode(self) -> str:
        """The ID modulation keyed, ``"cw"`` or ``"voice"`` (for the `station_id` ledger record)."""
        return self._mode

    def _id_audio(self) -> AudioFrame:
        return self._encoder.encode(self._callsign)

    def _due(self, now: float) -> bool:
        return self._last_id is None or (now - self._last_id) >= self._interval

    def transmit(self, audio: AudioFrame, now: float | None = None) -> None:
        """Transmit a service's content, prepending the ID into the same over when due."""
        if now is None:
            now = self._clock()
        if self._due(now):
            frame = self._id_audio() + audio
            self._last_id = now
        else:
            frame = audio
        self._radio.transmit(frame)
        self._transmitted_this_session = True

    def identify(self, now: float | None = None) -> None:
        """Transmit an ID-only over **unconditionally** — an on-demand station identification.

        Unlike :meth:`check` (which fires only when overdue and the station has transmitted), this
        always keys an ID, then resets the periodic timer (`_last_id = now`) so the on-demand ID
        counts toward the Part-97 interval. Backs the ``4#`` DTMF command / the web "play ID" trigger.
        """
        if now is None:
            now = self._clock()
        self._radio.transmit(self._id_audio())
        self._last_id = now
        self._transmitted_this_session = True

    def check(self, now: float | None = None) -> bool:
        """Force an ID-only transmission if the active session is overdue for one.

        No-op (returns False) unless the station has transmitted this session and the
        interval has elapsed since the last ID. Intended to be called periodically by a
        real scheduler task; in tests a fake clock drives it directly.
        """
        if now is None:
            now = self._clock()
        if self._transmitted_this_session and self._due(now):
            self._radio.transmit(self._id_audio())
            self._last_id = now
            return True
        return False

    def sign_off(self, now: float | None = None) -> bool:
        """Send a closing ID at session end, then reset for the next session.

        Sends the ID only if the station actually transmitted during the session; a session
        that never keyed up needs no ID. Returns whether an ID was sent.
        """
        if now is None:
            now = self._clock()
        sent = False
        if self._transmitted_this_session:
            self._radio.transmit(self._id_audio())
            sent = True
        self._reset()
        return sent

    def begin_session(self, now: float | None = None) -> None:
        """Reset per-session ID state so the next over is identified as a fresh session.

        Guards the inactivity-timeout path: a session that dies without an explicit
        `sign_off` must not leave a stale `last_id` that suppresses the next session's
        first ID.
        """
        self._reset()

    def _reset(self) -> None:
        self._transmitted_this_session = False
        self._last_id = None


class StreamingId:
    """Radio-free station-ID scheduler for streaming TX (ADR 0041, guardrail 5 / Part 97).

    :class:`StationId` *owns* a :class:`Radio` and prepends the ID into a single discrete over
    (``transmit(audio)`` → ``_id_audio() + audio``) — the shape the one-shot dispatcher path needs.
    A live PCM stream (:class:`radio_server.tx.session.TxSession`: the browser ``/audio/tx`` talker
    and the Mumble bridge) arrives frame-by-frame with no natural "whole over" to prepend to, so it
    needs a scheduler that *renders* ID audio on demand and lets the caller transmit it into the
    **same keyed over**. This is that seam — the same ``_due``/interval scheduling as
    :class:`StationId`, minus the radio, so it satisfies the ``TxIdentifier`` protocol
    ``TxSession`` consults.

    One instance is shared by both streaming TX sources (built in the composition root), so the
    browser talker and the bridge identify on the same station timer. It keeps its own timer,
    independent of the controller's :class:`StationId`: over-identifying across the two paths is
    legal, under-identifying is the violation, so separate timers fail safe.

    "Due" is measured from the last ID, not the last key-up — a stream that keeps transmitting still
    re-IDs on schedule (the Part 97 invariant is "<=10 minutes since the last identification").
    """

    def __init__(
        self,
        encoder: IdEncoder,
        callsign: str,
        *,
        interval: float = DEFAULT_ID_INTERVAL,
        clock: Clock | None = None,
        mode: str = "cw",
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._encoder = encoder
        self._callsign = callsign
        self._interval = interval
        self._clock = clock
        self._mode = mode
        self._last_id: float | None = None
        self._transmitted = False

    @property
    def callsign(self) -> str:
        """The station callsign this identifies with (for the `station_id` ledger record)."""
        return self._callsign

    @property
    def mode(self) -> str:
        """The ID modulation keyed, ``"cw"`` or ``"voice"`` (for the `station_id` ledger record)."""
        return self._mode

    def _id_audio(self) -> AudioFrame:
        return self._encoder.encode(self._callsign)

    def _due(self, now: float) -> bool:
        return self._last_id is None or (now - self._last_id) >= self._interval

    def key_up_id(self, now: float | None = None) -> AudioFrame | None:
        """ID audio for the key-up edge of a keyed stream, or ``None`` when not due.

        Returns the ID (and stamps the timer) iff an ID is due, so the first over of a fresh
        transmission carries the ID while rapid re-keys within the interval do not repeat it. The
        caller transmits the returned frame into the same over, ahead of content.
        """
        if now is None:
            now = self._clock()
        self._transmitted = True
        if self._due(now):
            self._last_id = now
            return self._id_audio()
        return None

    def periodic_id(self, now: float | None = None) -> AudioFrame | None:
        """ID audio when a keyed stream crosses the interval mid-over, else ``None``.

        Called cheaply per frame; fires only when the <=10-minute boundary is crossed during a long
        transmission, so a stream that keeps talking re-IDs on schedule.
        """
        if now is None:
            now = self._clock()
        if self._due(now):
            self._last_id = now
            return self._id_audio()
        return None

    def sign_off_id(self, now: float | None = None) -> AudioFrame | None:
        """Closing ID for the key-down edge, or ``None``.

        Due-gated: identifies at key-down only when the interval has elapsed since the last ID, so a
        rapid tap-tap exchange is not ID'd after every short over (the periodic re-ID already
        guarantees compliance during long overs). Only fires if the stream actually transmitted.
        """
        if now is None:
            now = self._clock()
        if self._transmitted and self._due(now):
            self._last_id = now
            return self._id_audio()
        return None
