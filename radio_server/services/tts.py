"""Text-to-speech interface, a deterministic stub, and the real piper engine.

Services produce audio by rendering text through a `TtsEngine`. Two implementations ship:

- `StubTts` — deterministic, hardware-free. `render` is a pure function of the text and
  embeds it, so a test can assert exactly what a service "spoke" via `MockRadio.tx_log`.
  It is the baseline the exact-assert end-to-end tests (auth → dispatch → CW-ID) run on.
- `PiperTts` — real neural speech via piper (ADR 0009). It is the first consumer of the
  `to_canonical` playback edge (ADR 0006): piper emits PCM at the voice's *native* rate,
  which `render` resamples up to the canonical 48k format so nothing above the TTS layer
  sees anything but canonical audio. Both satisfy the same one-method `render` contract, so
  swapping `StubTts` for `PiperTts` is a drop-in.

Guardrail 1 (verify hardware facts empirically) governs `PiperTts`: the piper package
version and its exact synthesis call are installed-build facts (isolated in one method,
marked, not asserted); the voice's native sample rate is *read from its `.json` sidecar*,
never assumed; and whether the speech is intelligible over a real RF path is a hardware
bring-up check. Neither piper nor a voice model is present in the software-cycle
environment, so the real-engine tests are `skipif`-gated and its output is
property-asserted, never byte-asserted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..audio import CANONICAL_FORMAT, AudioFormat, AudioFrame, to_canonical

if TYPE_CHECKING:
    from ..config import Settings

#: Environment variable naming the piper voice model (the ``.onnx`` file). No default: a
#: TTS engine configured without a model must fail loud (like the TOTP secret), never a
#: silent no-op. The voice's ``.json`` sidecar (``<voice>.onnx.json``) carries its sample
#: rate, so no rate is hardcoded here.
RADIO_TTS_VOICE_ENV_VAR = "RADIO_TTS_VOICE"


@runtime_checkable
class TtsEngine(Protocol):
    """Renders a line of text to a chunk of audio."""

    def render(self, text: str) -> AudioFrame: ...


class StubTts:
    """Deterministic, hardware-free TTS for tests and mock runs.

    `render` is a pure function of `text`: equal text always yields equal bytes, and
    the bytes embed the text so a test can assert precisely what was spoken. This is
    NOT real speech — it is a stand-in for piper that keeps the whole dispatch path
    deterministic. It is retained deliberately as the exact-assert baseline even now that
    `PiperTts` exists (neural output cannot be byte-asserted).
    """

    def render(self, text: str) -> AudioFrame:
        return AudioFrame(b"<audio:" + text.encode("utf-8") + b">")


def load_tts_voice(settings: Settings) -> str:
    """Return the piper voice path (`tts.voice`), failing loud (via `Settings.get`) when unset.

    There is no baked-in default model. Download a voice from the piper voices release and set its
    ``.onnx`` path as ``tts.voice`` in ``radio.toml`` (the matching ``.onnx.json`` sidecar must sit
    beside it). The file's existence is checked when `PiperTts` opens it.
    """
    return settings.get("tts.voice")


class PiperTts:
    """Real neural TTS via piper, rendering to the canonical audio format (ADR 0009).

    Drop-in for `StubTts`: implements the same `render(text) -> AudioFrame` contract, so
    the time service and dispatcher are untouched. piper synthesizes PCM at the voice's
    native rate (read from the sidecar, *not* hardcoded — voices vary, some are 16000);
    `render` resamples that to canonical 48k via `to_canonical`, the playback edge.

    Construction validates the model + sidecar and reads the native rate *without*
    importing piper, so a missing model fails loud at load and the resample edge stays
    testable with no piper installed. The piper import and synthesis are deferred to
    `_synthesize_raw`, the single installed-build-dependent seam (guardrail 1).
    """

    def __init__(self, voice_path: str, *, config_path: str | None = None) -> None:
        self._voice_path = voice_path
        # piper's convention is <model>.onnx alongside <model>.onnx.json. Marked config —
        # VERIFY AGAINST THE INSTALLED piper BUILD (guardrail 1) if a voice uses another
        # sidecar layout.
        self._config_path = config_path or f"{voice_path}.json"

        if not Path(voice_path).is_file():
            raise RuntimeError(
                f"{RADIO_TTS_VOICE_ENV_VAR} points at a missing piper voice: {voice_path!r}"
            )
        if not Path(self._config_path).is_file():
            raise RuntimeError(
                f"piper voice sidecar not found: {self._config_path!r} "
                "(expected <voice>.onnx.json beside the model)"
            )

        self._rate = _read_voice_rate(self._config_path)
        self._voice = None  # lazily loaded piper voice, cached on first render

    def render(self, text: str) -> AudioFrame:
        """Synthesize `text` to a canonical-format `AudioFrame`.

        piper emits native-rate int16 PCM; wrapping it at `self._rate` and running
        `to_canonical` yields a `CANONICAL_FORMAT` frame regardless of the voice's rate.
        """
        raw = self._synthesize_raw(text)
        native = AudioFrame(raw, AudioFormat(self._rate, CANONICAL_FORMAT.width, 1))
        return to_canonical(native)

    def _synthesize_raw(self, text: str) -> bytes:
        """Run piper and return native-rate signed-16-bit mono PCM bytes.

        The ONLY installed-build-dependent code path (guardrail 1): the piper package
        version and this exact API shape are VERIFY-AGAINST-INSTALLED-BUILD. Isolated here
        so the rest of `PiperTts` (validation, rate read, the `to_canonical` edge) is
        model-free and unit-testable, and so a test can override this seam to drive `render`
        with a synthetic voice rate.
        """
        try:
            from piper.voice import PiperVoice  # noqa: PLC0415 (deferred: heavy, optional)
        except ImportError as exc:  # pragma: no cover - exercised only where piper is absent
            raise RuntimeError(
                "piper is not installed; install the 'tts' extra "
                "(pip install 'radio-server[tts]') to use PiperTts"
            ) from exc

        if self._voice is None:
            self._voice = PiperVoice.load(self._voice_path, config_path=self._config_path)

        chunks = [
            chunk.audio_int16_bytes for chunk in self._voice.synthesize(text)
        ]
        return b"".join(chunks)


def _read_voice_rate(config_path: str) -> int:
    """Read the voice's native sample rate from its piper ``.json`` sidecar.

    The rate lives at ``audio.sample_rate``. Reading it here (rather than assuming 22050)
    is what lets a 16000 Hz voice resample correctly. Fails loud on a malformed sidecar or a
    missing/invalid rate rather than guessing.
    """
    try:
        with open(config_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not read piper voice sidecar {config_path!r}") from exc

    try:
        rate = int(config["audio"]["sample_rate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"piper voice sidecar {config_path!r} has no valid audio.sample_rate"
        ) from exc
    if rate <= 0:
        raise RuntimeError(
            f"piper voice sidecar {config_path!r} has non-positive sample_rate {rate}"
        )
    return rate
