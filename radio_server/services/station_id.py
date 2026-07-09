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

import os
from typing import Protocol, runtime_checkable

from ..backends import AudioFrame, Radio
from ..auth import Clock

#: Environment variable holding the station callsign. No default: a station cannot legally
#: transmit without a real callsign, so a missing one must fail loud (like the TOTP secret),
#: not fall back to a placeholder.
RADIO_CALLSIGN_ENV_VAR = "RADIO_CALLSIGN"

#: Environment variable naming the ID interval in seconds. Optional (marked default below).
RADIO_ID_INTERVAL_ENV_VAR = "RADIO_ID_INTERVAL"

#: Marked default ID interval. The legal maximum is 10 minutes.
DEFAULT_ID_INTERVAL = 600.0

#: The regulatory ceiling: identify at least every 10 minutes. A configured interval above
#: this is rejected, not clamped — a too-long interval is a misconfiguration to fix, not to
#: silently paper over.
MAX_ID_INTERVAL = 600.0


def load_callsign(env: dict[str, str] | os._Environ = os.environ) -> str:
    """Return the station callsign from the environment.

    Raises `RuntimeError` (not a silent default) when unset — transmitting without a real
    callsign is illegal, so an unconfigured station must fail loudly rather than key up
    with a placeholder.
    """
    callsign = env.get(RADIO_CALLSIGN_ENV_VAR)
    if not callsign:
        raise RuntimeError(
            f"{RADIO_CALLSIGN_ENV_VAR} is not set; a station cannot legally transmit "
            "without a callsign — export your FCC callsign before starting the server"
        )
    return callsign


def load_id_interval(env: dict[str, str] | os._Environ = os.environ) -> float:
    """Return the ID interval (seconds) from `RADIO_ID_INTERVAL`, or the marked default.

    Enforces the Part 97 ceiling: a value above `MAX_ID_INTERVAL` (600 s) raises, as does a
    non-numeric or non-positive value. Fail loud on regulatory misconfiguration rather than
    quietly identifying too rarely.
    """
    raw = env.get(RADIO_ID_INTERVAL_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_ID_INTERVAL
    try:
        interval = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{RADIO_ID_INTERVAL_ENV_VAR}={raw!r} is not a number of seconds"
        ) from exc
    if interval <= 0:
        raise RuntimeError(
            f"{RADIO_ID_INTERVAL_ENV_VAR}={raw!r} must be positive"
        )
    if interval > MAX_ID_INTERVAL:
        raise RuntimeError(
            f"{RADIO_ID_INTERVAL_ENV_VAR}={raw!r} exceeds the legal maximum of "
            f"{MAX_ID_INTERVAL:.0f}s (identify at least every 10 minutes)"
        )
    return interval


@runtime_checkable
class IdEncoder(Protocol):
    """Renders a callsign to the audio that identifies the station.

    One method, mirroring `TtsEngine`: real CW (`CwId`) and voice (`VoiceId`) encoders
    implement the same contract, so nothing above this layer changes when they land.
    """

    def encode(self, callsign: str) -> AudioFrame: ...


class StubId:
    """Deterministic ID audio keyed to the callsign, so `tx_log` is exactly assertable.

    Placeholder until a real CW/voice encoder lands (needs the audio-format ADR). Output is
    a pure function of the callsign: ``b"<id:AE9S>"``.
    """

    def encode(self, callsign: str) -> AudioFrame:
        return b"<id:" + callsign.encode("utf-8") + b">"


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
    ) -> None:
        if clock is None:
            import time

            clock = time.time
        self._radio = radio
        self._encoder = encoder
        self._callsign = callsign
        self._interval = interval
        self._clock = clock
        # Per-session state. `last_id is None` means nothing has been ID'd this session, so
        # the next transmission is the first over and is always identified.
        self._transmitted_this_session = False
        self._last_id: float | None = None

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
