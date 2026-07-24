"""Shared USB sound-card audio seam — capture, playout, and the TX pacer (ADR 0113).

Extracted from :mod:`radio_server.backends.aioc_baofeng` so a second backend can reuse the exact
AIOC audio machinery instead of duplicating it. Both the ``baofeng`` (UV-5R) and ``uvk5``
(Quansheng Dock) backends ride the same AIOC cable: it presents a serial control line **and** a
USB sound card. This module owns everything that is **PTT-independent** — the two backends differ
only in how they key (baofeng asserts a serial line; uvk5 writes BK4819 registers), and that
keying stays in each backend. Here we hold:

- :class:`SoundCardTxPacer` — the daemon-thread playout writer (ADR 0102): every blocking
  ``RawOutputStream.write`` happens on the pacer thread, bounded + drop-oldest, and a failed write
  invokes ``on_error`` (the backend's key-off) so a dying audio device can never strand the
  transmitter keyed.
- :func:`open_capture_stream` / :func:`open_playout_stream` — open + start the canonical 48 kHz
  s16le mono raw streams on a named device.
- :func:`load_sounddevice` — the lazy import / test-injection seam (``sounddevice`` needs the
  system ``libportaudio2``, so importing it can raise ``OSError`` as well as ``ImportError``).
- :func:`lead_in_bytes` / :func:`playout_buffer_bytes` and the shared ``DEFAULT_*`` device / block
  / lead / buffer constants.

The module imports only :mod:`radio_server.audio` at module load and lazily imports
``sounddevice`` inside :func:`load_sounddevice`, so importing it stays hardware-free.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from pathlib import Path

from ..audio import CANONICAL_FORMAT

logger = logging.getLogger(__name__)


#: The AIOC USB sound card, as sounddevice/PortAudio name it. sounddevice matches a device by
#: integer index or by a (case-insensitive) substring of its PortAudio name — NOT by a raw ALSA
#: string like ``hw:CARD=AllInOneCable``. PortAudio names the raw ALSA device
#: ``All-In-One-Cable: USB Audio (hw:2,0)``; the substring ``"All-In-One-Cable: USB"`` targets that
#: (the low-latency raw device) unambiguously, where a bare ``"All-In-One-Cable"`` would also match
#: the PulseAudio/PipeWire-wrapped copies of the same card. Empirically opens + reads 48 kHz here;
#: ``python -m radio_server.doctor`` prints the exact index/name to use if this doesn't resolve.
#:
#: :func:`resolve_device` additionally accepts an ALSA **card id** (``AIOC_K6``) — the per-cable
#: name udev's ``ATTR{id}`` assigns off the USB serial — which PortAudio never reports and so
#: could not be matched before (ADR 0124). That is the stable way to address one of *several*
#: identical AIOCs, since every one of them carries the same PortAudio name.
DEFAULT_INPUT_DEVICE = "All-In-One-Cable: USB"
DEFAULT_OUTPUT_DEVICE = "All-In-One-Cable: USB"
#: Frames per capture/playback block: 960 = 20 ms at the canonical 48 kHz. VERIFY AGAINST HARDWARE
#: (guardrail 1) — trades latency against xrun robustness on the real codec.
DEFAULT_BLOCKSIZE = 960
#: Seconds of silence transmitted immediately after PTT keys up, before any real audio (the "TX
#: lead-in" / PTT head delay). A transmitter — and the receiving radio's squelch — take a few
#: hundred ms to come up after keying; without a lead-in the first fraction of a second of speech
#: goes out before the RF path is established and is clipped over the air. 0.5 s matches the clip
#: observed on the AIOC/UV-5R bench. Per-hardware (guardrail 1): bench-tune, or set 0 to disable.
DEFAULT_TX_LEAD_SECONDS = 0.5
#: Bound on PCM buffered ahead of the playback device (ADR 0102). Producers are real-time paced
#: (decode/browser/Mumble frames arrive at ~playback rate) so the buffer normally holds the 0.5 s
#: lead-in at key-up and then hovers near empty; the bound only bites if a producer briefly bursts
#: ahead, and then drop-oldest keeps TX latency bounded (the kv4p pacer / link-bridge idiom).
DEFAULT_TX_BUFFER_SECONDS = 2.0


def load_sounddevice(injected, *, extra_hint: str):
    """Return the injected sounddevice-like module, or lazily import the real ``sounddevice``.

    ``sounddevice`` loads PortAudio at import, which raises ``OSError`` (not ``ImportError``) when
    the system ``libportaudio2`` is absent — both surface as ``RuntimeError(extra_hint)`` so the
    caller can name the exact extra to install. ``injected`` is the per-backend test seam (a fake
    module exposing ``RawInputStream`` / ``RawOutputStream``); when it is not ``None`` it is
    returned untouched and nothing is imported.
    """
    if injected is not None:
        return injected
    try:
        import sounddevice
    except (ImportError, OSError) as exc:  # OSError: PortAudio lib not found (libportaudio2)
        raise RuntimeError(extra_hint) from exc
    return sounddevice


#: Where the kernel exposes one directory per ALSA card (``card0/id``, ``card1/id``, …). Only a
#: :func:`resolve_device` parameter so tests can point it at a tmp dir.
ALSA_SYSFS_ROOT = Path("/sys/class/sound")


def _alsa_card_index(card_id: str, sysfs_root) -> int | None:
    """The ALSA card **index** whose sysfs ``id`` is exactly ``card_id``, or ``None``.

    ``id`` is the short name udev's ``ATTR{id}`` sets — ``AIOC_K6`` — which is stable per cable
    when the udev rule keys on the USB serial (ADR 0124). Absent sysfs (CI, macOS) yields ``None``.
    """
    try:
        entries = sorted(Path(sysfs_root).glob("card*/id"))
    except OSError:
        return None
    for entry in entries:
        try:
            if entry.read_text().strip() != card_id:
                continue
        except OSError:
            continue
        suffix = entry.parent.name[len("card") :]
        if suffix.isdigit():
            return int(suffix)
    return None


def resolve_device(sd, device, *, kind: str, sysfs_root=None):
    """Map an ALSA **card id** to a PortAudio device index; pass everything else through unchanged.

    sounddevice resolves a string device by substring match against PortAudio *names*, and those
    come from the ALSA card *name* (the USB product string — ``All-In-One-Cable``), never the card
    *id* that udev's ``ATTR{id}`` sets. So ``input_device = "AIOC_K6"`` could never resolve no
    matter how correct the udev rule was; that mismatch is what this closes (ADR 0124).

    The existing behaviour is tried **first**, so every config that works today is untouched and
    this only engages where sounddevice would already have failed:

    1. ``None`` / ``int`` — nothing to resolve.
    2. The string already substring-matches a PortAudio name — hand it back unchanged and let
       sounddevice do its own matching.
    3. Otherwise read it as an ALSA card id → card index ``N`` → the PortAudio device named
       ``… (hw:N,…)`` that has channels in the needed direction, returned as an integer index.
    4. Still nothing — hand the string back, so sounddevice raises its own familiar error rather
       than one invented here.

    ``kind`` is ``"input"`` or ``"output"``: one card exposes both legs, and only the direction
    actually being opened should decide the pick. ``sysfs_root`` defaults to
    :data:`ALSA_SYSFS_ROOT` and is read at call time, so tests can point it at a tmp dir.
    """
    if device is None or isinstance(device, int):
        return device
    query = getattr(sd, "query_devices", None)
    if query is None:  # an injected test seam without the full PortAudio surface
        return device
    try:
        devices = list(query())
    except Exception:  # PortAudio unavailable — let the stream open raise the real error
        return device

    name = str(device)
    if any(name.lower() in str(d.get("name", "")).lower() for d in devices):
        return device  # resolves exactly the way it always has

    index = _alsa_card_index(name, ALSA_SYSFS_ROOT if sysfs_root is None else sysfs_root)
    if index is None:
        return device
    channels = "max_input_channels" if kind == "input" else "max_output_channels"
    marker = f"(hw:{index},"
    for i, entry in enumerate(devices):
        if marker in str(entry.get("name", "")) and entry.get(channels, 0) > 0:
            return i
    return device


def open_capture_stream(sd, *, device, blocksize: int):
    """Open + start a canonical 48 kHz s16le mono ``RawInputStream`` on ``device``."""
    stream = sd.RawInputStream(
        samplerate=CANONICAL_FORMAT.rate,
        blocksize=blocksize,
        device=resolve_device(sd, device, kind="input"),
        channels=CANONICAL_FORMAT.channels,
        dtype="int16",
    )
    stream.start()
    return stream


def open_playout_stream(sd, *, device, blocksize: int):
    """Open + start a canonical 48 kHz s16le mono ``RawOutputStream`` on ``device``."""
    stream = sd.RawOutputStream(
        samplerate=CANONICAL_FORMAT.rate,
        blocksize=blocksize,
        device=resolve_device(sd, device, kind="output"),
        channels=CANONICAL_FORMAT.channels,
        dtype="int16",
    )
    stream.start()
    return stream


def lead_in_bytes(seconds: float) -> int:
    """The TX lead-in as a raw silent-PCM byte count (0 disables)."""
    return round(CANONICAL_FORMAT.rate * float(seconds)) * CANONICAL_FORMAT.frame_bytes


def playout_buffer_bytes(lead_bytes: int) -> int:
    """The pacer's byte bound: :data:`DEFAULT_TX_BUFFER_SECONDS`, but always big enough for the
    lead-in slug plus headroom (so the lead-in is never dropped at key-up)."""
    buffer = round(CANONICAL_FORMAT.rate * DEFAULT_TX_BUFFER_SECONDS) * CANONICAL_FORMAT.frame_bytes
    return max(buffer, lead_bytes * 2)


class SoundCardTxPacer:
    """Owns every blocking write to one keying's playback stream, on a daemon thread (ADR 0102).

    Producers (the asyncio event loop: ``TxSession.feed``, the D-STAR bridge, Mumble) call
    :meth:`enqueue` — non-blocking, bounded, drop-oldest by whole chunks — and the writer thread
    performs the blocking ``RawOutputStream.write`` calls, letting the device clock the drain.
    Unlike the kv4p ``_TxPacer`` (an Opus-encoder-owning slot timer that must synthesize silence),
    this pacer needs no cadence of its own: the AIOC's continuous output stream already emits
    silence when idle, and the blocking write paces the thread naturally.

    Chunk boundaries are preserved (a deque of ``bytes``), so the device sees the caller's frame
    shape; the byte bound is enforced across chunks with drop-oldest + ``dropped_bytes`` telemetry.

    RF-safety (ADR 0093 carried forward): a failed ``write`` on this thread invokes ``on_error``
    (the backend's key-off) after discarding the queue — a dying audio device must never hold the
    transmitter keyed. One pacer per physical keying; :meth:`stop` discards and joins.
    """

    def __init__(self, stream, *, max_buffer_bytes: int, on_error=None) -> None:
        self._stream = stream
        self._max = max_buffer_bytes
        self._on_error = on_error
        self._chunks: deque[bytes] = deque()
        self._buffered = 0  # bytes across _chunks
        self._cond = threading.Condition()
        self._writing = False  # a chunk is mid-write on the pacer thread
        self._stopped = False
        self._dropped = 0
        self._error: Exception | None = None  # the write failure that stopped the pacer, if any
        self._thread = threading.Thread(target=self._run, name="soundcard-tx-pacer", daemon=True)
        self._thread.start()

    # --- producer side (event loop, or any transmit() caller) -----------------

    def enqueue(self, pcm: bytes) -> None:
        """Queue one PCM chunk for the writer thread; never blocks, drop-oldest over the bound."""
        if not pcm:
            return
        with self._cond:
            if self._stopped:
                return
            self._chunks.append(bytes(pcm))
            self._buffered += len(pcm)
            while self._buffered > self._max and len(self._chunks) > 1:
                old = self._chunks.popleft()
                self._buffered -= len(old)
                self._dropped += len(old)
            self._cond.notify_all()

    def wait_drained(self, timeout: float) -> bool:
        """Block until every queued chunk has been written (the one-shot contract). False on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._chunks or self._writing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    @property
    def dropped_bytes(self) -> int:
        """PCM bytes discarded to the buffer bound (telemetry: a producer outpaced the device)."""
        with self._cond:
            return self._dropped

    @property
    def error(self) -> Exception | None:
        """The write failure that stopped this pacer, if any (the one-shot path re-raises it)."""
        with self._cond:
            return self._error

    # --- consumer side (writer thread) ----------------------------------------

    def _run(self) -> None:
        while True:
            with self._cond:
                while not self._chunks and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                chunk = self._chunks.popleft()
                self._buffered -= len(chunk)
                self._writing = True
            try:
                self._stream.write(chunk)
            except Exception as exc:
                # The device died mid-write (unplug, xrun storm, closed stream). Discard, stop, and
                # unkey via on_error — the write failure must never strand the transmitter keyed.
                # The exception is kept for the one-shot path to re-raise (its blocking contract
                # includes surfacing playback failure to the caller).
                with self._cond:
                    self._error = exc
                    self._writing = False
                    self._stopped = True
                    self._chunks.clear()
                    self._buffered = 0
                    self._cond.notify_all()
                if self._on_error is not None:
                    with contextlib.suppress(Exception):
                        self._on_error()
                return
            with self._cond:
                self._writing = False
                if not self._chunks:
                    self._cond.notify_all()

    # --- lifecycle -------------------------------------------------------------

    def stop(self) -> None:
        """Discard everything queued and stop the thread (idempotent; safe from any thread).

        Deliberately does NOT drain: stop is the un-key path, and queued audio playing on after
        unkey is the "long FM tail" this pacer exists to kill. The join is bounded — worst case the
        thread is parked in one blocking chunk write against a stream the backend is closing.
        """
        with self._cond:
            already = self._stopped
            self._stopped = True
            self._chunks.clear()
            self._buffered = 0
            self._cond.notify_all()
        if already:
            return
        if self._dropped:
            logger.debug("soundcard pacer: dropped %d buffered PCM bytes over the bound", self._dropped)
        thread = self._thread
        if thread is not threading.current_thread():
            thread.join(timeout=5.0)
