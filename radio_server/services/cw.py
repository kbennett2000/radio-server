"""Real CW (Morse) station identification — the first real transmission content (ADR 0007).

`CwId` implements the one-method `IdEncoder` contract (cycle 4) that `StationId` already
schedules through, so nothing above the encoder seam changes: swapping `StubId` for `CwId`
turns the symbolic `b"<id:AE9S>"` placeholder into genuine keyed Morse audio.

The layering here mirrors `synth_tone` (cycle 5): a pure Morse table, a pure PARIS timing
model (`unit_ms`, `cw_timeline`), and a canonical-zero silence builder sit *below* the
encoder, so the timing math is exactly testable without touching PCM. `CwId.encode` then
keys `synth_tone` on and off along that timeline — the tone's raised-cosine envelope is what
keeps each element from clicking.

Config (WPM, sidetone frequency) is operator preference, not a hardware fact: it uses the
established `*_ENV_VAR` + `load_*` marked-default convention (guardrail 1). Whether the CW is
actually *readable* keyed through a real radio is an empirical bring-up check, not something
this software cycle proves.
"""

from __future__ import annotations

import os

from ..audio import CANONICAL_FORMAT, AudioFormat, AudioFrame, synth_tone

#: International Morse for the alnum callsign alphabet plus "/" (portable indicator). Values
#: are strings of "." (dit) and "-" (dah). Callsigns are A-Z/0-9; "/" is cheap and common on
#: portable/rover IDs, so it is included. Anything outside this table is a fail-loud error
#: (see `_morse_for`): a wrong or silently-dropped ID is worse than a loud failure.
MORSE: dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.",
}

#: PARIS element-timing multiples, in dot-units. Standard CW: dit=1, dah=3, the gap between
#: elements of one character=1, between characters=3, between words=7.
DIT_UNITS = 1
DAH_UNITS = 3
INTRA_CHAR_GAP_UNITS = 1
INTER_CHAR_GAP_UNITS = 3
INTER_WORD_GAP_UNITS = 7

#: Environment variable naming the CW speed in words per minute. Optional (marked default).
RADIO_CW_WPM_ENV_VAR = "RADIO_CW_WPM"

#: Environment variable naming the sidetone frequency in Hz. Optional (marked default).
RADIO_CW_TONE_HZ_ENV_VAR = "RADIO_CW_TONE_HZ"

#: Marked-default CW speed. 20 WPM is a common, comfortably-readable ID speed. An operator
#: preference, not a confirmed hardware fact (guardrail 1) — safe as config.
DEFAULT_CW_WPM = 20.0

#: Marked-default sidetone frequency. 600 Hz is a typical CW pitch. Audio content only — this
#: is the tone the ID is keyed at, not an RF frequency.
DEFAULT_CW_TONE_HZ = 600.0

#: Default keying amplitude (0..1 of full scale), matching `synth_tone`'s own default.
DEFAULT_CW_AMPLITUDE = 0.5


def unit_ms(wpm: float) -> float:
    """PARIS dot-unit length in milliseconds for a given speed: ``1200 / wpm``.

    Kept pure and trivial so the timing math is exactly assertable against PARIS.
    """
    return 1200.0 / wpm


def _morse_for(char: str, callsign: str) -> str:
    """Return the dit/dah string for a single character, failing loud on the unknown.

    `callsign` is passed only to make the error message point at the real input.
    """
    try:
        return MORSE[char]
    except KeyError as exc:
        raise ValueError(
            f"no Morse encoding for {char!r} in callsign {callsign!r}; "
            "CW ID supports A-Z, 0-9 and '/' only"
        ) from exc


def cw_timeline(text: str, wpm: float) -> list[tuple[bool, float]]:
    """Decompose `text` into an on/off keying envelope for a given speed.

    Returns a list of ``(on, duration_ms)`` segments — ``on=True`` is a keyed tone (dit or
    dah), ``on=False`` is a gap — in order, with **no leading or trailing gap** (gaps appear
    only *between* elements, characters, and words). That makes total duration exactly the
    sum of the segment durations, and the sequence itself exactly assertable against PARIS
    without decoding any PCM.

    Space separates words (inter-word gap); within a word each character's dits/dahs are
    keyed with intra-character gaps, and characters are separated by inter-character gaps.
    Unknown characters fail loud (see `_morse_for`).
    """
    unit = unit_ms(wpm)
    segments: list[tuple[bool, float]] = []
    words = text.upper().split(" ")
    for word_index, word in enumerate(words):
        if word_index > 0:
            segments.append((False, INTER_WORD_GAP_UNITS * unit))
        for char_index, char in enumerate(word):
            if char_index > 0:
                segments.append((False, INTER_CHAR_GAP_UNITS * unit))
            code = _morse_for(char, text)
            for element_index, element in enumerate(code):
                if element_index > 0:
                    segments.append((False, INTRA_CHAR_GAP_UNITS * unit))
                on_units = DAH_UNITS if element == "-" else DIT_UNITS
                segments.append((True, on_units * unit))
    return segments


def _silence(duration_ms: float, format: AudioFormat) -> AudioFrame:
    """A gap of canonical-format zeros, so concatenation stays format-identical.

    Same rounding as `synth_tone` (round samples from milliseconds) so a rendered timeline's
    total sample count is exactly the sum of its per-segment sample counts.
    """
    n = round(format.rate * duration_ms / 1000.0)
    if n <= 0:
        return AudioFrame(b"", format)
    return AudioFrame(bytes(n * format.frame_bytes), format)


class CwId:
    """Encodes a callsign to keyed-Morse station-ID audio (`IdEncoder`).

    Drop-in for `StubId`: `StationId` calls ``encode(callsign)`` unchanged and gets a real
    canonical-format frame instead of the symbolic stub payload. Speed and sidetone pitch are
    injected (from `load_cw_wpm` / `load_cw_tone_hz` at the composition root) — they are
    operator preferences, not per-call arguments.
    """

    def __init__(
        self,
        *,
        wpm: float = DEFAULT_CW_WPM,
        tone_hz: float = DEFAULT_CW_TONE_HZ,
        amplitude: float = DEFAULT_CW_AMPLITUDE,
    ) -> None:
        self._wpm = wpm
        self._tone_hz = tone_hz
        self._amplitude = amplitude

    def encode(
        self, callsign: str, format: AudioFormat = CANONICAL_FORMAT
    ) -> AudioFrame:
        """Render `callsign` to a keyed-Morse `AudioFrame` in `format` (canonical by default).

        Keys `synth_tone` on for each dit/dah and inserts canonical-zero gaps between them,
        per `cw_timeline`. Deterministic — no RNG — so the output is exactly assertable. The
        `format` parameter honors the cycle-6 `encode(callsign, format)` shape while defaulting
        to canonical, so `StationId`'s one-argument call is unaffected.
        """
        frame = AudioFrame(b"", format)
        for on, duration_ms in cw_timeline(callsign, self._wpm):
            if on:
                segment = synth_tone(
                    self._tone_hz, duration_ms, format, amplitude=self._amplitude
                )
            else:
                segment = _silence(duration_ms, format)
            frame = frame + segment
        return frame


def load_cw_wpm(env: dict[str, str] | os._Environ = os.environ) -> float:
    """Return the CW speed (WPM) from `RADIO_CW_WPM`, or the marked default.

    A marked default (unlike the callsign, this is a preference, not legally required), but a
    *set* value that is non-numeric or non-positive fails loud rather than being papered over.
    """
    return _load_positive_float(env, RADIO_CW_WPM_ENV_VAR, DEFAULT_CW_WPM)


def load_cw_tone_hz(env: dict[str, str] | os._Environ = os.environ) -> float:
    """Return the sidetone frequency (Hz) from `RADIO_CW_TONE_HZ`, or the marked default."""
    return _load_positive_float(env, RADIO_CW_TONE_HZ_ENV_VAR, DEFAULT_CW_TONE_HZ)


def _load_positive_float(
    env: dict[str, str] | os._Environ, var: str, default: float
) -> float:
    """Shared marked-default loader: return the default when unset, else a positive float.

    Mirrors `load_id_interval`'s policy — fail loud on a non-numeric or non-positive value
    rather than silently substituting the default.
    """
    raw = env.get(var)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{var}={raw!r} is not a number") from exc
    if value <= 0:
        raise RuntimeError(f"{var}={raw!r} must be positive")
    return value
