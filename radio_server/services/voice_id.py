"""Spoken (voice) station identification and CW-vs-voice ID-mode selection (ADR 0010).

`VoiceId` is the second `IdEncoder` (after `CwId`, ADR 0007): it speaks the callsign as
NATO/ITU phonetics through a `TtsEngine` (the cycle-8 `PiperTts` in production, `StubTts` in
tests). It satisfies the same one-method `encode(callsign)` contract `StationId` already
schedules through, so nothing above the encoder seam changes — swapping CW for voice is an
encoder swap, not a scheduler change.

The layering mirrors `cw.py`: a pure phonetic table (`PHONETIC`) and a pure spelled-string
builder (`spell_callsign`) sit *below* synthesis, so the map is exactly assertable with no
TTS engine at all. `VoiceId.encode` then just renders that spelled string.

`RADIO_ID_MODE` (`cw` | `voice`) picks which encoder `StationId` uses. CW is the marked
default: it has no model dependency and always works, so an unconfigured or model-less
station still identifies. `voice` mode requires a configured `PiperTts` voice and fails loud
when it is missing — it never silently degrades to CW.

Guardrail 1: whether the spoken ID is *intelligible* keyed through a real radio is an
empirical bring-up check, not something this software cycle proves.
"""

from __future__ import annotations

import os

from ..audio import CANONICAL_FORMAT, AudioFormat, AudioFrame
from .cw import CwId, load_cw_tone_hz, load_cw_wpm
from .station_id import IdEncoder
from .tts import PiperTts, TtsEngine, load_tts_voice

#: NATO/ITU phonetic alphabet for the callsign character set, plus the ham digit convention
#: (9 -> "niner"). The accepted set matches `CwId`'s `MORSE` table (A-Z, 0-9 and "/", the
#: portable indicator, spoken "slash") so switching ID mode never changes which callsigns are
#: encodable. Anything outside this table is a fail-loud error (see `spell_callsign`): a wrong
#: or silently-dropped ID is worse than a loud failure.
PHONETIC: dict[str, str] = {
    "A": "alpha", "B": "bravo", "C": "charlie", "D": "delta", "E": "echo",
    "F": "foxtrot", "G": "golf", "H": "hotel", "I": "india", "J": "juliett",
    "K": "kilo", "L": "lima", "M": "mike", "N": "november", "O": "oscar",
    "P": "papa", "Q": "quebec", "R": "romeo", "S": "sierra", "T": "tango",
    "U": "uniform", "V": "victor", "W": "whiskey", "X": "xray", "Y": "yankee",
    "Z": "zulu",
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "niner",
    "/": "slash",
}

#: Environment variable naming the station-ID mode (`cw` | `voice`). Optional (marked default).
RADIO_ID_MODE_ENV_VAR = "RADIO_ID_MODE"

#: Marked-default ID mode. CW is the safe default: it has no model dependency and always
#: works, so a station with no TTS voice configured still identifies legally.
DEFAULT_ID_MODE = "cw"

#: The recognized ID modes. Any other value fails loud rather than defaulting silently.
ID_MODES = ("cw", "voice")


def spell_callsign(callsign: str) -> str:
    """Spell `callsign` as space-separated NATO/ITU phonetics ("AE9S" -> "alpha echo niner sierra").

    Pure and engine-free, so the phonetic map is exactly assertable on its own. Upper-cases
    first (callsigns are case-insensitive). Fails loud with `ValueError` on any character
    outside `PHONETIC`, mirroring `CwId`'s `_morse_for` — a callsign the ID cannot speak is a
    misconfiguration to surface, not to drop.
    """
    words = []
    for char in callsign.upper():
        try:
            words.append(PHONETIC[char])
        except KeyError as exc:
            raise ValueError(
                f"no phonetic word for {char!r} in callsign {callsign!r}; "
                "voice ID supports A-Z, 0-9 and '/' only"
            ) from exc
    return " ".join(words)


class VoiceId:
    """Encodes a callsign to spoken phonetic station-ID audio (`IdEncoder`).

    Drop-in for `CwId`/`StubId`: `StationId` calls ``encode(callsign)`` unchanged and gets a
    canonical-format frame of real speech instead. The `TtsEngine` is injected at construction
    (not per call), so `VoiceId` is exact-testable on `StubTts` and runs on `PiperTts` in
    production — and one voice can back both the ID and the service announcements.
    """

    def __init__(self, tts: TtsEngine) -> None:
        self._tts = tts

    def encode(
        self, callsign: str, format: AudioFormat = CANONICAL_FORMAT
    ) -> AudioFrame:
        """Render `callsign` to a spoken-phonetic `AudioFrame`.

        Spells the callsign (`spell_callsign`) and renders it through the injected engine. The
        `format` parameter honors the `encode(callsign, format)` shape `CwId` established while
        defaulting to canonical, so `StationId`'s one-argument call is unaffected; the engine's
        canonical output is authoritative (a `TtsEngine` always renders `CANONICAL_FORMAT`).
        """
        return self._tts.render(spell_callsign(callsign))


def load_id_mode(env: dict[str, str] | os._Environ = os.environ) -> str:
    """Return the station-ID mode from `RADIO_ID_MODE`, or the marked default (`cw`).

    Returns `DEFAULT_ID_MODE` when unset/empty; a *set* value outside `ID_MODES` fails loud
    rather than defaulting silently — a typo'd mode is a misconfiguration to surface.
    """
    raw = env.get(RADIO_ID_MODE_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_ID_MODE
    mode = raw.strip().lower()
    if mode not in ID_MODES:
        raise RuntimeError(
            f"{RADIO_ID_MODE_ENV_VAR}={raw!r} is not a valid ID mode; "
            f"choose one of {', '.join(ID_MODES)}"
        )
    return mode


def build_id_encoder(
    env: dict[str, str] | os._Environ = os.environ,
    *,
    tts: TtsEngine | None = None,
) -> IdEncoder:
    """Construct the `IdEncoder` selected by `RADIO_ID_MODE` — the ID composition root.

    `cw` -> `CwId` with the configured WPM/tone. `voice` -> `VoiceId` over the injected `tts`,
    or a fresh `PiperTts(load_tts_voice(env))` when none is injected. Voice mode with no
    configured voice raises (via `load_tts_voice`/`PiperTts`) rather than falling back to CW —
    a station asked to identify by voice must not silently switch modes. The `tts` injection
    lets tests select voice mode deterministically on `StubTts` with no model present.
    """
    mode = load_id_mode(env)
    if mode == "voice":
        return VoiceId(tts if tts is not None else PiperTts(load_tts_voice(env)))
    return CwId(wpm=load_cw_wpm(env), tone_hz=load_cw_tone_hz(env))
