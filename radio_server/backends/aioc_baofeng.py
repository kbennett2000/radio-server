"""AiocBaofeng — audio-only UV-5R backend (audio/PTT via NA6D AIOC cable, no CAT).

The real hardware backend (ADR 0029). Audio in/out is the AIOC's USB sound card
(``sounddevice``/ALSA); PTT is the AIOC's serial control line (``pyserial``). There is no CAT:
frequency is set by hand on the radio, so this backend advertises only :data:`SHARED_CAPS` and
the API rejects tuning operations (guardrail 3).

Keying discipline (guardrail 2): PTT is a serial control line (RTS by default; RTS-vs-DTR is the
one empirical fact — verify on hardware, guardrail 1), NEVER a CAT ``TX`` command. Two keying
shapes share one backend:

  * **One-shot** (station ID, service TTS, REST ``/transmit``): a single ``transmit(clip)`` self-keys
    — assert the line, play the whole clip, drain, drop the line. The caller never touches ``ptt()``.
  * **Streaming** (``TxSession`` / ``/audio/tx``): an explicit ``ptt(True)`` holds the line across
    many ``transmit(frame)`` calls, then ``ptt(False)`` drops it. While the line is held, ``transmit``
    only plays — it must not drop the key between frames.

The distinguishing state is :attr:`_keyed` (held by ``ptt(True)``): ``transmit`` self-keys only when
it is not already keyed.

Hardware deps (``pyserial``, ``sounddevice``) are the ``hardware`` optional extra and are lazily
imported here, so ``import radio_server.backends`` and the CI test suite stay hardware-free — the
constructor accepts injected fakes (``_serial_factory`` / ``_audio``) for unit tests. ``sounddevice``
additionally needs the system ``libportaudio2`` library (out-of-band, like ``multimon-ng``).

Known limitation (ADR 0029): ``receive()`` blocks ~one block (~20 ms) and is called directly on the
event loop by ``RxPump``; moving it to a thread executor is a deferred follow-up.
"""

from __future__ import annotations

import atexit
from enum import StrEnum

from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from .base import SHARED_CAPS, Capability, RadioStatus


class PttLine(StrEnum):
    """Which serial control line keys PTT on the AIOC. **DTR** is the default — confirmed on the
    bench (cycle 29, `python -m radio_server.doctor --key-test`): on this NA6D AIOC + UV-5R, DTR keys
    the transmitter and RTS does not. Kept configurable because it is a per-hardware fact (guardrail
    1). ``pyserial`` exposes both as writable ``.rts`` / ``.dtr`` attributes."""

    RTS = "rts"
    DTR = "dtr"


#: AIOC PTT serial device. ``/dev/ttyACM0`` is the enumeration default; the stable, reorder-proof
#: path is ``/dev/serial/by-id/usb-*All-In-One-Cable*`` — prefer it in a multi-device setup.
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
#: The default PTT line: DTR, confirmed on the bench (cycle 29). RTS did not key this AIOC.
DEFAULT_PTT_LINE = PttLine.DTR
#: The AIOC USB sound card, as sounddevice/PortAudio name it. sounddevice matches a device by
#: integer index or by a (case-insensitive) substring of its PortAudio name — NOT by a raw ALSA
#: string like ``hw:CARD=AllInOneCable``. PortAudio names the raw ALSA device
#: ``All-In-One-Cable: USB Audio (hw:2,0)``; the substring ``"All-In-One-Cable: USB"`` targets that
#: (the low-latency raw device) unambiguously, where a bare ``"All-In-One-Cable"`` would also match
#: the PulseAudio/PipeWire-wrapped copies of the same card. Empirically opens + reads 48 kHz here;
#: ``python -m radio_server.doctor`` prints the exact index/name to use if this doesn't resolve.
DEFAULT_INPUT_DEVICE = "All-In-One-Cable: USB"
DEFAULT_OUTPUT_DEVICE = "All-In-One-Cable: USB"
#: Frames per capture/playback block: 960 = 20 ms at the canonical 48 kHz. VERIFY AGAINST HARDWARE
#: (guardrail 1) — trades latency against xrun robustness on the real codec.
DEFAULT_BLOCKSIZE = 960

_EXTRA_MSG = (
    "the AIOC/Baofeng backend needs the 'hardware' extra (pyserial + sounddevice): "
    "install with `pip install 'radio-server[hardware]'` (and the system libportaudio2)"
)


def _load_serial():
    try:
        import serial  # pyserial
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch in tests
        raise RuntimeError(_EXTRA_MSG) from exc
    return serial


def _default_serial_factory(port: str):
    """Open ``port`` with both control lines held **low from the moment it opens**.

    RF-safety (guardrail): some drivers pulse RTS/DTR on open (the Arduino-reset footgun), which
    would momentarily key the transmitter. ``pyserial`` applies ``.rts``/``.dtr`` set before
    ``open()`` as the initial line state, so we set both low first and only then open.
    """
    serial = _load_serial()
    handle = serial.Serial()
    handle.port = port
    handle.dtr = False
    handle.rts = False
    handle.open()
    return handle


class AiocBaofeng:
    """The UV-5R backend: USB-audio TX/RX + serial-line PTT, no CAT.

    Args:
        serial_port: PTT serial device (e.g. ``/dev/ttyACM0``).
        ptt_line: Which control line keys PTT — ``"rts"`` (default) or ``"dtr"``. Empirical
            (guardrail 1); flip if the bench key-test shows the other line.
        input_device / output_device: ALSA device names for capture / playback (the AIOC card).
        blocksize: Frames per capture/playback block (:data:`DEFAULT_BLOCKSIZE`).
        _serial_factory: Test seam — ``(port) -> Serial-like`` with writable ``.rts``/``.dtr`` and
            ``.close()``. Defaults to opening a real ``pyserial`` port (lines held low on open).
        _audio: Test seam — a ``sounddevice``-like module exposing ``RawInputStream`` /
            ``RawOutputStream``. Defaults to the real (lazily imported) ``sounddevice``.
    """

    backend_name = "baofeng"

    def __init__(
        self,
        *,
        serial_port: str = DEFAULT_SERIAL_PORT,
        ptt_line: str = DEFAULT_PTT_LINE,
        input_device: str | int = DEFAULT_INPUT_DEVICE,
        output_device: str | int = DEFAULT_OUTPUT_DEVICE,
        blocksize: int = DEFAULT_BLOCKSIZE,
        _serial_factory=None,
        _audio=None,
    ) -> None:
        try:
            self._ptt_line = PttLine(str(ptt_line).lower())
        except ValueError as exc:
            choices = ", ".join(m.value for m in PttLine)
            raise ValueError(f"ptt_line={ptt_line!r} is not one of: {choices}") from exc

        self._input_device = input_device
        self._output_device = output_device
        self._blocksize = blocksize
        self._audio_mod = _audio  # None -> lazily import real sounddevice on first stream open

        # Open the serial handle now (the real backend needs the device present) and force BOTH
        # lines low, so construction can never leave the transmitter keyed (guardrail).
        self._serial = (_serial_factory or _default_serial_factory)(serial_port)
        self._serial.rts = False
        self._serial.dtr = False

        self._capture = None  # opened lazily on first receive()
        self._playback = None  # open only while the line is asserted
        self._keyed = False  # True while ptt(True) holds the line across frames (streaming)
        self._transmitting = False  # reflects the line being asserted (one-shot or held)
        self._closed = False
        # Never leave the radio keyed if the process dies mid-transmission.
        atexit.register(self.close)

    # --- audio plumbing -------------------------------------------------------

    def _sd(self):
        """The sounddevice-like module (injected fake, or the real library, lazily imported)."""
        if self._audio_mod is None:
            try:
                import sounddevice
            except (ImportError, OSError) as exc:  # OSError: PortAudio lib not found (libportaudio2)
                raise RuntimeError(_EXTRA_MSG) from exc
            self._audio_mod = sounddevice
        return self._audio_mod

    def _open_capture(self):
        stream = self._sd().RawInputStream(
            samplerate=CANONICAL_FORMAT.rate,
            blocksize=self._blocksize,
            device=self._input_device,
            channels=CANONICAL_FORMAT.channels,
            dtype="int16",
        )
        stream.start()
        return stream

    def _key_on(self) -> None:
        """Open the playback stream FIRST, then assert the PTT line last.

        Ordering is an RF-safety invariant: if opening the audio device fails, the line must never
        have been asserted (else a failed key-up would leave the transmitter keyed). The stream only
        emits silence until :meth:`transmit` writes, so keying after it starts still means no real
        audio reaches the radio before it is keyed. If the line-assert itself fails, the just-opened
        stream is torn down before re-raising.
        """
        stream = self._sd().RawOutputStream(
            samplerate=CANONICAL_FORMAT.rate,
            blocksize=self._blocksize,
            device=self._output_device,
            channels=CANONICAL_FORMAT.channels,
            dtype="int16",
        )
        stream.start()
        try:
            setattr(self._serial, self._ptt_line.value, True)
        except Exception:
            stream.stop()
            stream.close()
            raise
        self._playback = stream
        self._transmitting = True

    def _key_off(self) -> None:
        """Drain and close the playback stream, THEN drop the line (never clip the tail)."""
        stream, self._playback = self._playback, None
        if stream is not None:
            # stop() blocks until pending buffers finish playing (drain), so the audio tail is not
            # clipped by dropping the line early.
            stream.stop()
            stream.close()
        setattr(self._serial, self._ptt_line.value, False)
        self._transmitting = False

    # --- shared surface -------------------------------------------------------

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"radio accepts {CANONICAL_FORMAT}, got a frame in {audio.format}"
            )
        if self._keyed:
            # Streaming: ptt(True) already holds the line and opened playback — just play the frame.
            self._playback.write(audio.samples)
            return
        # One-shot: self-key for exactly the duration of this clip.
        self._key_on()
        try:
            self._playback.write(audio.samples)
        finally:
            self._key_off()

    def receive(self) -> AudioFrame:
        if self._capture is None:
            self._capture = self._open_capture()
        data, _overflowed = self._capture.read(self._blocksize)
        # An xrun (overflow) is not fatal — the samples we did get are still valid audio.
        return AudioFrame(bytes(data), CANONICAL_FORMAT)

    def ptt(self, on: bool) -> None:
        if on:
            if not self._keyed:
                self._key_on()
                self._keyed = True
        else:
            if self._keyed:
                self._key_off()
                self._keyed = False

    def status(self) -> RadioStatus:
        # No hardware busy/COS line on the UV-5R (ADR 0015): busy is always False here; RX gating is
        # software VAD (audio.squelch=audio), not a carrier-detect the radio reports. CAT fields stay
        # None. `transmitting` tracks whether the PTT line is currently asserted.
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._transmitting,
            busy=False,
        )

    def capabilities(self) -> frozenset[Capability]:
        return SHARED_CAPS

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Drop the line, close both streams and the serial handle. Idempotent; safe at exit."""
        if self._closed:
            return
        self._closed = True
        try:
            self._key_off()  # drops the line and closes playback if keyed
        except Exception:
            pass
        # Belt-and-suspenders: force both lines low even if _key_off did not run cleanly.
        try:
            self._serial.rts = False
            self._serial.dtr = False
        except Exception:
            pass
        if self._capture is not None:
            try:
                self._capture.stop()
                self._capture.close()
            except Exception:
                pass
            self._capture = None
        try:
            self._serial.close()
        except Exception:
            pass
        atexit.unregister(self.close)
