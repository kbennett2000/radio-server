"""Activity detection: the real software squelch behind the RX activity seam (ADR 0015).

Cycle 13 laid the seam — an injectable ``(AudioFrame) -> bool`` predicate on :class:`RxPump`
(``radio_server.rx.pump.RxActivityGate``), defaulting to a pass-through that relays every frame,
including dead air. This module is the **detector** that fills it. It decides "is this frame live
audio worth streaming" vs hiss, so listeners don't get a constant stream of nothing.

Two independent sources of truth, one interface (mirroring the scan engine's busy-poll question,
ADR 0012):

- :class:`AudioLevelGate` — **software squelch** on the audio itself: RMS energy over a frame with
  hysteresis (an on/off threshold pair, so it doesn't chatter on the boundary) and a hang time (stay
  open N s after the level drops, so a speech gap doesn't clip the stream). This is the ONLY option
  for the Baofeng, which has no hardware busy line.
- :class:`CatBusyGate` — reads the radio's **real hardware squelch** over ``status().busy`` (the V71
  over rigctld). It ignores the frame content entirely; the radio already decided.

Both structurally satisfy the :class:`~radio_server.rx.pump.RxActivityGate` protocol; the backend /
config picks which via :func:`build_rx_gate`. This package deliberately does **not** import ``rx`` —
it sits below it, so scan can later feed its stop decision off the same activity signal (the gate is
shared logic, not welded to ``rx/``).

Everything here is pure signal processing on canonical PCM (48k/s16le/mono) plus a clock-injected
hang timer — no hardware, no real sleeps. The threshold/hang **values** are bench-tuned facts
(guardrail 1); the marked defaults below are a starting point to verify against hardware.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from ..audio import AudioFrame
from ..backends import Radio

if TYPE_CHECKING:
    from ..config import Settings

#: The gate shape every implementation here satisfies: ``(AudioFrame) -> bool`` — structurally the
#: same as ``radio_server.rx.pump.RxActivityGate``, but named locally so this package never imports
#: ``rx`` (the dependency arrow stays ``activity -> {audio, backends}``, leaving the detector free
#: for ``scan`` to reuse without pulling the RX transport).
ActivityGate = Callable[[AudioFrame], bool]

#: numpy dtype for canonical signed-16-bit little-endian samples (matches ``audio.tone``/``resample``).
_PCM_DTYPE = np.dtype("<i2")

#: A clock returns seconds as a float. Injectable so the hang timer is exactly testable with a fake
#: clock (no real sleeps). Defined locally rather than imported from ``auth`` so this package's
#: dependency arrow stays ``activity -> {audio, backends}`` only — the same call ``scan`` makes.
Clock = Callable[[], float]


def frame_rms(frame: AudioFrame) -> float:
    """Return the RMS amplitude of a canonical (s16le) frame, in int16 units (0.0 for empty).

    The pure, reusable energy primitive both the gate and (later) scan can share. Assumes the
    canonical 16-bit format; an odd trailing byte (only a malformed/symbolic frame) is trimmed
    rather than raising, so a stray non-PCM frame reads as low energy instead of crashing the pump.
    """
    buf = frame.samples
    if len(buf) % _PCM_DTYPE.itemsize:
        buf = buf[: len(buf) - (len(buf) % _PCM_DTYPE.itemsize)]
    pcm = np.frombuffer(buf, dtype=_PCM_DTYPE)
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))


class AudioLevelGate:
    """Audio-level VAD: open above an on-threshold, close below a (lower) off-threshold, with hang.

    Satisfies the ``(AudioFrame) -> bool`` :class:`~radio_server.rx.pump.RxActivityGate` shape.

    - **Hysteresis:** the threshold a frame is compared against depends on the current state — the
      higher ``on_threshold`` to *open* a closed gate, the lower ``off_threshold`` to *hold* an open
      one. A level between the two neither opens nor closes, so the gate doesn't chatter on the
      boundary of a marginal signal.
    - **Hang:** when the level drops below the off-threshold the gate stays open until ``hang``
      seconds after the last above-threshold frame, so a short gap between words doesn't clip the
      stream. Timed against an injected clock (``time.monotonic`` by default — elapsed-interval
      timing, as the scan engine uses).
    """

    def __init__(
        self,
        *,
        on_threshold: float,
        off_threshold: float,
        hang: float,
        clock: Clock | None = None,
    ) -> None:
        if on_threshold <= off_threshold:
            raise ValueError(
                f"on_threshold ({on_threshold}) must exceed off_threshold ({off_threshold}) "
                "for hysteresis"
            )
        if clock is None:
            import time

            clock = time.monotonic
        self._on = on_threshold
        self._off = off_threshold
        self._hang = hang
        self._clock = clock
        self._open = False
        self._active_until: float | None = None

    def __call__(self, frame: AudioFrame) -> bool:
        now = self._clock()
        level = frame_rms(frame)
        # Hysteresis: an open gate is held by the *lower* off-threshold; a closed one only opens on
        # the *higher* on-threshold.
        threshold = self._off if self._open else self._on
        if level >= threshold:
            self._open = True
            self._active_until = now + self._hang
            return True
        # Below the active threshold: the hang window keeps an open gate up through a brief gap.
        if self._open and self._active_until is not None and now < self._active_until:
            return True
        self._open = False
        self._active_until = None
        return False


class CatBusyGate:
    """CAT-busy gate: relay iff the radio's hardware squelch reports the channel busy.

    Satisfies the ``(AudioFrame) -> bool`` gate shape but **ignores the frame** — the V71's real
    squelch (read over ``status().busy``, exactly as the scan engine polls it) already decided
    whether there is a signal. This is the design tension the interface papers over: unlike
    :class:`AudioLevelGate`, this gate needs the *radio* at construction, not just the frame.
    """

    def __init__(self, radio: Radio) -> None:
        self._radio = radio

    def __call__(self, frame: AudioFrame) -> bool:
        return self._radio.status().busy


# --- config (guardrail 1: marked defaults, verify against hardware) ------------------------

#: RMS level (int16 units) a frame must reach to *open* a closed gate. VERIFY AGAINST HARDWARE
#: (guardrail 1) — the real value depends on the radio's noise floor and audio-interface gain. Set
#: too low and the gate relays noise / never lets scan resume; too high and it clips quiet speech.
#: A bench-tuned fact, not a confirmed value.
DEFAULT_VAD_ON_RMS = 500.0
#: RMS level (int16 units) an *open* gate holds down to before it starts to close (hysteresis: must
#: be below the on-threshold). VERIFY AGAINST HARDWARE (guardrail 1) — a starting point above the
#: noise floor but below normal speech.
DEFAULT_VAD_OFF_RMS = 300.0
#: Seconds to stay open after the level drops below the off-threshold, so a gap between words does
#: not clip the stream. VERIFY AGAINST HARDWARE (guardrail 1) — real speech-gap timing is bench-tuned.
DEFAULT_VAD_HANG = 0.5
#: The marked-default squelch mode when ``RADIO_SQUELCH`` is unset: ``off`` (pass-through), so the
#: cycle-13 relay-everything behavior is preserved until a deployment opts in.
DEFAULT_SQUELCH_MODE = "off"

RADIO_VAD_ON_RMS_ENV_VAR = "RADIO_VAD_ON_RMS"
RADIO_VAD_OFF_RMS_ENV_VAR = "RADIO_VAD_OFF_RMS"
RADIO_VAD_HANG_ENV_VAR = "RADIO_VAD_HANG"
RADIO_SQUELCH_ENV_VAR = "RADIO_SQUELCH"


class SquelchMode(StrEnum):
    """Which activity gate the RX pump uses. See :func:`build_rx_gate`.

    The choice depends on the backend — ``CAT`` for the V71 (real hardware squelch), ``AUDIO`` for
    the Baofeng (no busy line, software squelch is the only option) — but it is **config-selected,
    not hardcoded to a backend** (guardrail note): a deployment picks it via ``RADIO_SQUELCH``.
    Auto-deriving it from the backend's capabilities is a later refinement.
    """

    OFF = "off"  # pass-through — relay every frame (cycle-13 behavior)
    AUDIO = "audio"  # software VAD on the audio level (Baofeng)
    CAT = "cat"  # the radio's hardware squelch over status().busy (V71)


def load_vad_on_rms(settings: Settings) -> float:
    """Return the VAD open-threshold (`audio.vad_on_rms`)."""
    return settings.get("audio.vad_on_rms")


def load_vad_off_rms(settings: Settings) -> float:
    """Return the VAD close-threshold (`audio.vad_off_rms`)."""
    return settings.get("audio.vad_off_rms")


def load_vad_hang(settings: Settings) -> float:
    """Return the VAD hang time in seconds (`audio.vad_hang`; 0 is valid)."""
    return settings.get("audio.vad_hang")


def load_squelch_mode(settings: Settings) -> SquelchMode:
    """Return the squelch mode (`audio.squelch`)."""
    return settings.get("audio.squelch")


def build_rx_gate(settings: Settings, *, radio: Radio) -> ActivityGate:
    """Compose the RX activity gate from ``settings`` — the composition root for squelch.

    Selects via ``audio.squelch`` (default ``off``): ``off`` → the cycle-13 ``pass_through_gate``;
    ``audio`` → an :class:`AudioLevelGate` from the ``audio.vad_*`` thresholds/hang; ``cat`` → a
    :class:`CatBusyGate` over the radio's hardware squelch. The ``AudioLevelGate`` constructor
    enforces the cross-field ``on_threshold > off_threshold`` hysteresis invariant (raising
    ``ValueError``) — that check stays there, not in the config schema, since it spans two settings.
    The ``off`` branch reaches into ``rx`` for its canonical pass-through via a **local** import, so
    the module-level dependency arrow stays ``activity -> {audio, backends}``.
    """
    mode = load_squelch_mode(settings)
    if mode is SquelchMode.OFF:
        from ..rx import pass_through_gate

        return pass_through_gate
    if mode is SquelchMode.CAT:
        return CatBusyGate(radio)
    return AudioLevelGate(
        on_threshold=load_vad_on_rms(settings),
        off_threshold=load_vad_off_rms(settings),
        hang=load_vad_hang(settings),
    )
