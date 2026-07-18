"""Opus audio edge for the kv4p HT (ADR 0064, ADR 0065) — no I/O.

Shipped firmware (v2.0.0.1, ``3f0e809…``) carries RX/TX audio as **Opus** on vendor command
``0x07``: 48 kHz mono s16, 40 ms frames (1920 samples), narrowband, VBR, ``OPUS_APPLICATION_AUDIO``,
one Opus packet per KISS frame (no length prefix), bounded by ``PROTO_MTU``. That is already
radio-server's canonical rate (:data:`CANONICAL_FORMAT`, ADR 0006), so — unlike the retired ADPCM
edge (ADR 0064) — there is no 16 k↔48 k rate conversion and no fixed wire block: RX is
one-packet-one-frame with no re-blocking (``AudioFrame`` is format-identity-only, with **no length
contract** — audio/format.py), and the only re-blocker is TX, which cuts arbitrary 48 k input into the
exact 1920-sample frames Opus requires.

The one RX subtlety (ADR 0070): the firmware clocks its **RX ADC ~2 % fast** and mislabels the Opus
stream 48 kHz (``rxAudio.h`` — ``config.sample_rate = AUDIO_SAMPLE_RATE * 1.02``, but the encoder is
told the unmultiplied rate), so every received sample arrives ~2 % off. :class:`RxAudioDecoder`
corrects it with a small stateful resample from the true device rate back to a real 48 kHz — the same
soxr HQ streaming precedent (ADR 0054) PR #118 removed reasoning "Opus is native 48 kHz". True of the
codec, false of the ADC. TX is clean (``txAudio.h`` uses the unmultiplied rate); the offset is RX-only.

libopus is loaded through the shared shim (:func:`radio_server.link._opus.ensure_opus_loadable`, ADR
0056/0057) — the same carrier-wheel path the Mumble link uses. It is loaded **lazily on the first
encode/decode** (not at import, and not at ``Kv4pHt`` construction) so the codec-free backend tests
need no libopus; a missing libopus surfaces as :class:`Kv4pOpusUnavailable` with an actionable install
hint, not an ``ImportError`` three frames down. Packaging note (ADR 0067): ``opuslib`` and the libopus
carrier wheel now ship with the kv4p node's own ``kv4p`` extra (the shared ``opus`` leaf, composed by
both ``kv4p`` and ``mumble``), so a kv4p node needs only ``uv sync --extra kv4p`` for libopus — no
sound card, no Mumble, no system library.

Source of truth for the params: kv4p-ht GPL-3.0 @ the shipped release v2.0.0.1
(``3f0e809baa02a946c3f0602681303f600c321d31``), ``rxAudio.h`` / ``txAudio.h``, read as a spec — not
ported. See ADR 0064.
"""

from __future__ import annotations

import logging

import numpy as np
import soxr

from ...audio import AudioFrame, CANONICAL_FORMAT
from .frames import PROTO_MTU

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Opus parameters (rxAudio.h / txAudio.h; ADR 0064)
# --------------------------------------------------------------------------------------

#: Opus is native 48 kHz — identical to the canonical rate, so no resampling either way.
OPUS_RATE = CANONICAL_FORMAT.rate  # 48000
OPUS_CHANNELS = 1
#: 40 ms at 48 kHz (``OPUS_FRAMESIZE_40_MS``): the firmware's frame size.
FRAME_MS = 40
FRAME_SAMPLES = OPUS_RATE * FRAME_MS // 1000  # 1920
#: One 40 ms canonical frame in bytes (mono s16le).
FRAME_BYTES = FRAME_SAMPLES * CANONICAL_FORMAT.frame_bytes  # 3840
#: One Opus packet per ``RX_AUDIO`` KISS frame, bounded by the device buffer (no length prefix).
MAX_PACKET_BYTES = PROTO_MTU

_PCM_DTYPE = np.dtype("<i2")
_INT16_MAX = 32767.0

#: soxr quality for the RX sample-rate correction (ADR 0070). **HQ, not the VHQ one-shot of
#: :func:`audio.resample`** — the same reasoning as ``GoertzelStream`` (ADR 0054): VHQ's steeper
#: filter buffers ~150 ms, a latency trap on the live RX path; HQ is clean at this tiny (~2 %) ratio.
_RX_RESAMPLE_QUALITY = "HQ"


class Kv4pOpusUnavailable(RuntimeError):
    """libopus/opuslib could not be loaded for the kv4p Opus codec.

    Raised — instead of a bare ``ImportError`` from deep in the codec — carrying an actionable install
    hint pointed at the node's own ``kv4p`` extra (ADR 0067), which composes the same shared ``opus``
    leaf the Mumble link uses.
    """


def _load_opus():
    """Import ``opuslib`` with libopus made loadable (ADR 0056/0057); fail loud and actionable.

    Reuses :func:`radio_server.link._opus.ensure_opus_loadable` to point ctypes at the bundled
    libopus, then imports ``opuslib``. Both a missing ``opuslib`` (``ImportError``) and a missing
    libopus (``opuslib`` raises a bare ``Exception`` at import) become :class:`Kv4pOpusUnavailable`.
    """
    from ...link._opus import ensure_opus_loadable, opus_install_hint

    ensure_opus_loadable()
    try:
        import opuslib
    except ImportError as exc:
        raise Kv4pOpusUnavailable(
            f"the kv4p Opus codec needs libopus — {opus_install_hint(extra='kv4p')}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — opuslib raises a bare Exception when libopus is missing
        raise Kv4pOpusUnavailable(
            f"the kv4p Opus codec could not load libopus — {opus_install_hint(extra='kv4p')}"
        ) from exc
    return opuslib


# --------------------------------------------------------------------------------------
# RX: one Opus packet -> one canonical 48k frame (no re-blocking, no resample)
# --------------------------------------------------------------------------------------


class RxAudioDecoder:
    """One Opus packet (one ``RX_AUDIO`` frame) → one canonical 48 kHz :class:`AudioFrame`.

    No re-blocking: ``AudioFrame`` carries no length contract, so each packet's decode (1920 samples for
    a 40 ms firmware frame) is one frame. The decoder is created lazily on the first :meth:`push` (see
    the module docstring).

    **Sample-rate correction (ADR 0070).** The firmware clocks its RX ADC ~2 % fast and mislabels the
    Opus stream 48 kHz, so decoded audio is ~2 % off-frequency — enough to knock DTMF tones off their
    Goertzel bins, and a ~1.2 s/min clock drift for every continuous consumer (hub, recorder, Mumble).
    ``sample_rate_correction`` is the firmware's multiplier (its ``AUDIO_SAMPLE_RATE * 1.02``); when it
    is not ``1.0`` the decoder holds a stateful soxr resampler from the true device rate
    (``round(48000 * correction)``) back to a real 48 kHz. At ``1.0`` (the generic default) it is a pure
    pass-through — byte-for-byte the pre-correction behavior — so the correction is a kv4p hardware fact
    threaded from config, not baked into the codec.
    """

    def __init__(self, sample_rate_correction: float = 1.0) -> None:
        self._opus = None  # cached opuslib module (per-instance, so a test can force the absent path)
        self._decoder = None  # lazy: opuslib.Decoder(48000, 1) on first push
        self._device_rate = round(OPUS_RATE * sample_rate_correction)
        # A stateful resampler only when the device rate really differs from canonical; else pass-through.
        self._resampler = (
            soxr.ResampleStream(
                self._device_rate, OPUS_RATE, OPUS_CHANNELS,
                dtype="float32", quality=_RX_RESAMPLE_QUALITY,
            )
            if self._device_rate != OPUS_RATE
            else None
        )

    def push(self, packet: bytes) -> AudioFrame:
        """Decode one Opus packet to a canonical frame; drop a corrupt packet (never raise).

        A corrupt/truncated packet (``opuslib.OpusError``) is dropped — an empty frame is returned and
        logged — so a bad byte off the wire can never kill the RX reader/consumer. A missing libopus is
        a *configuration* error, not a wire error, so :class:`Kv4pOpusUnavailable` from
        :func:`_load_opus` is **not** swallowed. When a correction resampler is engaged the decoded PCM
        is streamed through it (true device rate → real 48 kHz) before the frame is returned.
        """
        if self._decoder is None:
            self._opus = _load_opus()
            self._decoder = self._opus.Decoder(OPUS_RATE, OPUS_CHANNELS)
        try:
            pcm = self._decoder.decode(packet, FRAME_SAMPLES)
        except self._opus.OpusError:
            logger.debug("kv4p: dropped a corrupt Opus RX packet (%d bytes)", len(packet))
            return AudioFrame(b"")
        if self._resampler is not None:
            pcm = self._correct(pcm)
        return AudioFrame(pcm)  # defaults to CANONICAL_FORMAT

    def _correct(self, pcm: bytes) -> bytes:
        """Resample decoded PCM from the true device rate to a real 48 kHz (stateful, HQ)."""
        samples = np.frombuffer(pcm, dtype=_PCM_DTYPE).astype(np.float32) / _INT16_MAX
        resampled = self._resampler.resample_chunk(samples)
        if not resampled.size:
            return b""
        return np.rint(np.clip(resampled, -1.0, 1.0) * _INT16_MAX).astype(_PCM_DTYPE).tobytes()


# --------------------------------------------------------------------------------------
# TX: arbitrary 48k input -> exact 1920-sample Opus packets (the only re-blocker)
# --------------------------------------------------------------------------------------


def _apply_tx_gain(samples: np.ndarray, gain: float) -> np.ndarray:
    """Scale canonical int16 TX samples by ``gain``, clamping to full-scale int16 (ADR 0080).

    The kv4p has no sound card, so — unlike the AIOC, which rides the OS mixer's playback slider —
    there is no analog stage to bring an overmodulated TX level down. This is that stage: a plain
    multiplier applied to *every* sample before the Opus encoder, so it attenuates everything
    transmitted (announcements, browser mic, Mumble), not one source.

    ``gain == 1.0`` returns the samples untouched — an exact int16 no-op, the default, so nothing
    changes for anyone who does not set it. Otherwise the samples are promoted to float, multiplied,
    and **clamped** to ``[-_INT16_MAX, _INT16_MAX]`` before rounding back to int16, mirroring the RX
    correction idiom (:meth:`RxAudioDecoder._correct`): a ``gain > 1.0`` clamps rather than wrapping
    around into the encoder.
    """
    if gain == 1.0:
        return samples
    scaled = samples.astype(np.float32) * gain
    return np.rint(np.clip(scaled, -_INT16_MAX, _INT16_MAX)).astype(_PCM_DTYPE)


class TxAudioEncoder:
    """Canonical 48 kHz audio → Opus packets, re-blocking to exact 1920-sample frames.

    ``transmit()`` hands over arbitrary-length 48 kHz frames; this accumulates them and encodes one
    Opus packet per whole 1920-sample (40 ms) frame, holding any remainder for the next push. Opus
    requires an exact frame size, so :meth:`flush` zero-pads the final partial frame to 1920 and
    encodes it — padding, never dropping, so every input sample ships. The encoder is created lazily on
    the first :meth:`push`/:meth:`flush`, configured to mirror the firmware's own RX encoder
    (``OPUS_APPLICATION_AUDIO``, VBR, narrowband — ADR 0064/0065) so what we send decodes the way the
    board expects. RX needs no such re-blocker (see :class:`RxAudioDecoder`).

    ``tx_gain`` (ADR 0080) is a plain multiplier applied to the canonical samples before they enter
    the accumulator — the kv4p's software substitute for the AIOC's OS-mixer playback level. Default
    ``1.0`` is an exact no-op; see :func:`_apply_tx_gain`.
    """

    def __init__(self, tx_gain: float = 1.0) -> None:
        self._encoder = None  # lazy: opuslib.Encoder on first push/flush
        self._acc = np.zeros(0, dtype=_PCM_DTYPE)  # 48k s16 samples awaiting a full frame
        self._tx_gain = float(tx_gain)

    @property
    def pending_samples(self) -> int:
        """Held 48k samples not yet in a whole 1920-sample frame (< 1920)."""
        return int(self._acc.size)

    def _get_encoder(self):
        if self._encoder is None:
            opuslib = _load_opus()
            enc = opuslib.Encoder(OPUS_RATE, OPUS_CHANNELS, opuslib.APPLICATION_AUDIO)
            enc.vbr = 1  # firmware sets vbr = 1 (ADR 0064)
            enc.max_bandwidth = opuslib.BANDWIDTH_NARROWBAND  # firmware caps at narrowband
            self._encoder = enc
        return self._encoder

    def _encode_frame(self, frame: np.ndarray) -> bytes:
        return self._get_encoder().encode(frame.astype(_PCM_DTYPE).tobytes(), FRAME_SAMPLES)

    def push(self, frame: AudioFrame) -> list[bytes]:
        samples = np.frombuffer(frame.samples, dtype=_PCM_DTYPE)
        if samples.size:
            samples = _apply_tx_gain(samples, self._tx_gain)
            self._acc = np.concatenate([self._acc, samples])
        packets: list[bytes] = []
        while self._acc.size >= FRAME_SAMPLES:
            packets.append(self._encode_frame(self._acc[:FRAME_SAMPLES]))
            self._acc = self._acc[FRAME_SAMPLES:]
        return packets

    def flush(self) -> list[bytes]:
        """Zero-pad and encode any held remainder as a final frame (nothing is dropped)."""
        if self._acc.size == 0:
            return []
        padded = np.zeros(FRAME_SAMPLES, dtype=_PCM_DTYPE)
        padded[: self._acc.size] = self._acc
        self._acc = np.zeros(0, dtype=_PCM_DTYPE)
        return [self._encode_frame(padded)]
