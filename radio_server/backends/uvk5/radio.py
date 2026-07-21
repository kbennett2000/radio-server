"""``Uvk5Radio`` — the CatRadio backend for the UV-K5 Quansheng Dock (ADR 0112).

Composes :class:`~.transport.Uvk5Transport` (dock serial) into the shared ``CatRadio``
surface. In full-control ("XVFO") mode the radio's own firmware is suspended and **the host
is the radio's brain**: tuning, tone, mode, and keying are all BK4819 register writes, so this
class holds a small tracked-register model and issues :class:`~.frames.WriteRegisters` /
:class:`~.frames.ReadRegisters` directly — there is no persisted desired-state reconciler like
kv4p's (the dock is plain request/reply).

Every register sequence is derived from the pinned client `ExtendedVFO/BK4819.cs`
(``851efa9…``), read as a specification (cite file:line; nothing ported). See ADR 0112 for the
derivation, the keying/guardrail analysis, and the verify-on-hardware items.

Audio is the AIOC **sound card** — a separate USB interface from the dock serial (ADR 0113).
:meth:`receive` / :meth:`transmit` reuse the shared :mod:`~radio_server.backends.soundcard` seam
(the same capture / playout / pacer machinery the ``baofeng`` backend runs); keying stays the
BK4819 register path below. The audio stream opens around the register TX-enable in
:meth:`_key_on` and is torn down after RX is restored in :meth:`_key_off`.

Two RF-safety facts to keep in view (ADR 0112):

- **Keying is register-based** (`reg 0x30 = 0xC1FE`), confirmed by a read-back or else
  :class:`Uvk5KeyingError` — a silent no-key never becomes dead air (the kv4p rule). Whether
  the AIOC-injected K1 audio is what actually transmits is verify-on-hardware.
- **The full-control loop has no time-out.** If the host dies mid-key without sending
  ``0x0871``, the radio stays keyed. :meth:`close`/``atexit`` unkey + exit cleanly, but a hard
  ``SIGKILL`` bypasses ``atexit`` — an app-level watchdog/TOT is a future concern.
"""

from __future__ import annotations

import atexit
import contextlib
import logging

from ..base import (
    Capability,
    RadioStatus,
    SHARED_CAPS,
    UnsupportedCapability,
)
from ..soundcard import (
    DEFAULT_BLOCKSIZE,
    DEFAULT_INPUT_DEVICE,
    DEFAULT_OUTPUT_DEVICE,
    DEFAULT_TX_LEAD_SECONDS,
    SoundCardTxPacer,
    lead_in_bytes,
    load_sounddevice,
    open_capture_stream,
    open_playout_stream,
    playout_buffer_bytes,
)
from ...audio import AudioFormatMismatch, AudioFrame, CANONICAL_FORMAT
from .frames import (
    EnterHwMode,
    ExitHwMode,
    ReadRegisters,
    RegisterInfo,
    WriteRegisters,
)
from .transport import Uvk5Closed, Uvk5Timeout, Uvk5Transport

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Defaults (marked, guardrail 1 — verify against hardware)
# --------------------------------------------------------------------------------------

#: RX tuning band. The UV-K5 covers roughly 18-1300 MHz; the client enforces no simple range
#: (it just computes registers), so this is a marked default — VERIFY ON BENCH (guardrail 1).
DEFAULT_FREQ_MIN_HZ = 18_000_000
DEFAULT_FREQ_MAX_HZ = 1_300_000_000
#: BK4819 tuning step: the client rounds ``MHz * 100000`` (BK4819.cs:111-112), i.e. 10 Hz. We
#: reject a frequency that is not a whole number of these — fail loud, never round.
FREQ_STEP_HZ = 10
#: Band split for the ``reg 0x33`` LNA/PA path bit, in 10 Hz units (BK4819.cs:120/138). ~280 MHz.
_BAND_SPLIT_10HZ = 28_000_000
#: ``status().busy`` fires when the reg-0x67 RSSI (`value & 0x1FF`, BK4819.cs:781) is at or above
#: this. A crude RSSI COS — the full noise+glitch squelch is a refinement. VERIFY ON BENCH.
DEFAULT_SQUELCH_THRESHOLD = 40
#: TX drive percent fed to the PA-power formula (BK4819.cs:566-567). VERIFY ON BENCH.
DEFAULT_TX_POWER_PCT = 100.0

#: reg 0x30 value that means "TX enabled" (GoTransmit, BK4819.cs:592). Read back to confirm keying.
_REG30_TX_ENABLED = 0xC1FE
#: Standard CTCSS tone band (Hz). Out of range fails loud rather than snapping.
_CTCSS_MIN_HZ = 67.0
_CTCSS_MAX_HZ = 254.1

#: mode string -> canonical name -> reg 0x43 bandwidth value (XBANDWIDTH, Defines.cs:169-170).
_MODE_ALIASES = {"FM": "FM", "WIDE": "FM", "NFM": "NFM", "NARROW": "NFM"}
_BANDWIDTH_REG43 = {"FM": 18856, "NFM": 18440}

#: Whether the backend may transmit at all (the config-facing default). Unlike kv4p — whose
#: TX_ALLOWED is a firmware NVS gate — the UV-K5 in full-control mode is keyed by a direct host
#: register write, so this is a SOFTWARE refuse-to-key: false makes a keying attempt fail loud
#: (never dead air) rather than pretend. On by default (radio-server exists to transmit).
DEFAULT_TX_ALLOWED = True
#: Initial RX/TX mode (config-facing default). FM (wide); NFM narrows the reg-0x43 bandwidth.
DEFAULT_MODE = "FM"
#: Backend-declared transmitter time-out (seconds) — a MANDATORY server-side stuck-key cap (ADR 0117).
#: Unlike kv4p (firmware `RUNAWAY_TX_SEC ≈ 200 s`) or the UV-5R (its own TOT menu), the UV-K5 in
#: full-control/XVFO mode has NO device-side backstop, so the server is the only protection: `uvk5.tot`
#: may be shortened but never disabled (`config/spec.py:coerce_uvk5_tot` rejects 0 and any value above
#: this default). Consumed at the composition root (`build_radio` wraps the backend in `TotRadio`) — the
#: TOT is a decorator concern, not a constructor arg, so this is a declared default, not an __init__ kwarg.
DEFAULT_TOT = 180.0

_UVK5_CAPS: frozenset[Capability] = SHARED_CAPS | frozenset(
    {Capability.SET_FREQUENCY, Capability.SET_TONE, Capability.SET_MODE, Capability.SCAN}
)

#: Names the ``uvk5`` extra (serial + soundcard) when the real ``sounddevice`` is missing.
_AUDIO_EXTRA_MSG = (
    "the UV-K5/Quansheng Dock backend needs the 'uvk5' extra (pyserial + sounddevice): "
    "install with `pip install 'radio-server[uvk5]'` (and the system libportaudio2)"
)


class Uvk5KeyingError(RuntimeError):
    """PTT was requested but the radio never reported ``reg 0x30 == 0xC1FE`` — a silent no-key.

    Raised (after restoring RX) instead of letting a requested transmission go out as dead air,
    so the caller can retry or alarm rather than believe it is on the air when it is not.
    """


class Uvk5Radio:
    """UV-K5 Quansheng Dock backend (register-write tuning; presets are host-side, ADR 0111)."""

    backend_name = "uvk5"

    def __init__(
        self,
        *,
        serial_port: str | None = None,
        baud: int | None = None,
        request_timeout: float | None = None,
        frequency: int | None = None,
        tone: float | None = None,
        mode: str | None = None,
        tx_allowed: bool = DEFAULT_TX_ALLOWED,
        squelch_threshold: int = DEFAULT_SQUELCH_THRESHOLD,
        tx_power_pct: float = DEFAULT_TX_POWER_PCT,
        freq_min_hz: int = DEFAULT_FREQ_MIN_HZ,
        freq_max_hz: int = DEFAULT_FREQ_MAX_HZ,
        input_device: str | int = DEFAULT_INPUT_DEVICE,
        output_device: str | int = DEFAULT_OUTPUT_DEVICE,
        blocksize: int = DEFAULT_BLOCKSIZE,
        tx_lead_seconds: float = DEFAULT_TX_LEAD_SECONDS,
        _transport: Uvk5Transport | None = None,
        _audio=None,
    ) -> None:
        if _transport is not None:
            self._transport = _transport
        else:
            kwargs = {}
            if serial_port is not None:
                kwargs["serial_port"] = serial_port
            if baud is not None:
                kwargs["baud"] = baud
            if request_timeout is not None:
                kwargs["request_timeout"] = request_timeout
            self._transport = Uvk5Transport(**kwargs)

        self._freq_min_hz = freq_min_hz
        self._freq_max_hz = freq_max_hz
        self._squelch_threshold = squelch_threshold
        self._tx_power_pct = tx_power_pct
        # RF gate: false makes _key_on refuse (fail loud, never dead air) — a genuinely receive-only
        # node. A software gate here (not a firmware NVS flag like kv4p) because full-control keying
        # is a direct host register write.
        self._tx_allowed = tx_allowed

        # AIOC sound-card audio (shared soundcard seam, ADR 0113). The dock serial (control/keying)
        # and the AIOC USB sound card (audio) are two interfaces on the one cable.
        self._input_device = input_device
        self._output_device = output_device
        self._blocksize = blocksize
        self._lead_bytes = lead_in_bytes(tx_lead_seconds)  # TX lead-in silence (0 disables)
        self._audio_mod = _audio  # None -> lazily import real sounddevice on first stream open
        self._capture = None  # opened lazily on first receive()
        self._playback = None  # open only while keyed
        self._pacer: SoundCardTxPacer | None = None  # per-keying playout writer thread (ADR 0102)

        # Tracked-register model, seeded from the radio's live state below.
        self._reg30 = 0  # RX system-control value; restored to un-key
        self._reg33 = 0  # LNA/PA band path
        self._frequency: int | None = None
        self._tone: float | None = None
        self._mode: str | None = None
        self._keyed = False
        self._closed = False

        # Link liveness, then take over: enter full-control and seed the model from a register
        # read-back (mirrors the client's Aquire, BK4819.cs:182-189).
        self._transport.connect()
        self._transport.send(EnterHwMode())
        self._reg30 = self._read_register(0x30)
        self._reg33 = self._read_register(0x33)
        lo = self._read_register(0x38)
        hi = self._read_register(0x39)
        self._frequency = ((hi << 16) | lo) * FREQ_STEP_HZ

        if frequency is not None:
            self.set_frequency(frequency)
        if mode is not None:
            self.set_mode(mode)
        if tone is not None:
            self.set_tone(tone)

        atexit.register(self.close)

    # --- register primitives --------------------------------------------------

    def _read_register(self, reg: int) -> int:
        """Read one BK4819 register (0x0851 -> the matching 0x0951 RegisterInfo)."""
        info = self._transport.request(
            ReadRegisters((reg,)),
            lambda m: isinstance(m, RegisterInfo) and m.register == reg,
        )
        return info.value

    def _write_registers(self, pairs) -> None:
        """Write ``(register, value)`` pairs (0x0850, fire-and-forget — no reply)."""
        self._transport.send(WriteRegisters(tuple(pairs)))

    def _tone_pairs(self) -> list[tuple[int, int]]:
        """The CTCSS register writes for the current tone (GoTransmit, BK4819.cs:620-647)."""
        if self._tone is None:
            return [(0x51, 0)]
        code = ((round(self._tone * 10) * 206488) + 50000) // 100000
        return [(0x51, 0x904A), (0x07, code)]

    # --- CAT tuning -----------------------------------------------------------

    def set_frequency(self, hz: int) -> None:
        """Tune to ``hz`` (does NOT key). Fails loud out of band or off the 10 Hz raster."""
        if not self._freq_min_hz <= hz <= self._freq_max_hz:
            raise ValueError(
                f"frequency {hz} Hz is out of band [{self._freq_min_hz}, {self._freq_max_hz}]"
            )
        if hz % FREQ_STEP_HZ != 0:
            raise ValueError(
                f"frequency {hz} Hz is not a multiple of the {FREQ_STEP_HZ} Hz tuning step"
            )
        freq10 = hz // FREQ_STEP_HZ
        reg33 = self._reg33 & 0xFFE7  # clear the band bits (3,4)
        reg33 |= 0b100 if freq10 < _BAND_SPLIT_10HZ else 0b1000
        self._reg33 = reg33
        self._write_registers(
            [
                (0x38, freq10 & 0xFFFF),
                (0x39, (freq10 >> 16) & 0xFFFF),
                (0x33, reg33),
                (0x30, 0),
                (0x30, self._reg30),
            ]
        )
        self._frequency = hz

    def set_channel(self, n: int) -> None:
        # Presets are host-side (ADR 0111); the dock has no memory-channel select.
        raise UnsupportedCapability(Capability.SET_CHANNEL)

    def set_tone(self, tone: float | None) -> None:
        """Set the TX CTCSS tone (Hz), or ``None`` to disable. Fails loud out of range."""
        if tone is None:
            self._tone = None
            self._write_registers([(0x51, 0)])
            return
        value = float(tone)
        if not _CTCSS_MIN_HZ <= value <= _CTCSS_MAX_HZ:
            raise ValueError(
                f"CTCSS tone {value} Hz is out of range [{_CTCSS_MIN_HZ}, {_CTCSS_MAX_HZ}]"
            )
        self._tone = value
        self._write_registers(self._tone_pairs())

    def set_mode(self, mode: str) -> None:
        """Set FM (wide) or NFM (narrow) via the reg 0x43 bandwidth. Fails loud otherwise."""
        canon = _MODE_ALIASES.get(mode.upper())
        if canon is None:
            raise ValueError(
                f"mode {mode!r} is not supported (want one of {sorted(_MODE_ALIASES)})"
            )
        self._write_registers([(0x43, _BANDWIDTH_REG43[canon])])
        self._mode = canon

    def scan(self, on: bool) -> None:
        # SCAN is advertised to gate the software ScanEngine (set_frequency + status().busy);
        # there is no native scan toggle to drive here (kv4p precedent).
        raise NotImplementedError(
            "the UV-K5 has no native scan toggle; the software ScanEngine (Capability.SCAN) "
            "drives scanning via set_frequency + status().busy"
        )

    # --- audio plumbing -------------------------------------------------------

    def _sd(self):
        """The sounddevice-like module (injected fake, or the real library, lazily imported)."""
        self._audio_mod = load_sounddevice(self._audio_mod, extra_hint=_AUDIO_EXTRA_MSG)
        return self._audio_mod

    # --- keying ---------------------------------------------------------------

    def ptt(self, on: bool) -> None:
        if on:
            self._key_on()
        else:
            self._key_off()

    def _key_on(self) -> None:
        """Open the AIOC playout stream, key TX via registers + CONFIRM, then queue the TX lead-in.

        Ordering is an RF-safety invariant, mirroring the baofeng backend: the audio device opens
        FIRST, so a failed open never writes the TX-enable register (a failed key-up must not leave
        the transmitter keyed). The register keying is then confirmed by a read-back (ADR 0112) — on
        any failure the whole key-up is undone (RX restored, audio torn down) via :meth:`_key_off`.
        """
        if self._keyed:
            return
        if not self._tx_allowed:
            # RF gate (fail loud, never dead air): refuse before opening the sound card or writing a
            # single register, so a receive-only node never even touches the TX path.
            raise Uvk5KeyingError("transmit is disabled on this backend (tx_allowed is false)")
        if self._frequency is None:
            raise Uvk5KeyingError("cannot key before a frequency is set")
        # Open the sound card + its pacer before keying. A failed device open raises here, before
        # any TX-enable write — the radio is never keyed. The pacer's on_error unkeys (register RX +
        # audio teardown) if a later playout write dies mid-over (ADR 0093/0102 carried forward).
        stream = open_playout_stream(
            self._sd(), device=self._output_device, blocksize=self._blocksize
        )
        self._playback = stream
        self._pacer = SoundCardTxPacer(
            stream,
            max_buffer_bytes=playout_buffer_bytes(self._lead_bytes),
            on_error=self._key_off,
        )
        freq10 = self._frequency // FREQ_STEP_HZ
        drive = max(0, min(255, int(self._tx_power_pct * 2.55))) << 8
        pa = (0x88 if freq10 < _BAND_SPLIT_10HZ else 0xA2) | drive
        try:
            self._write_registers(
                [
                    (0x36, pa),  # PA power (SetPower, BK4819.cs:567)
                    (0x50, 0x3B20),  # FM AF/TX path, un-muted (GoTransmit, BK4819.cs:589)
                    *self._tone_pairs(),  # CTCSS (GoTransmit, BK4819.cs:620-647)
                    (0x30, 0),
                    (0x30, _REG30_TX_ENABLED),  # TX enable (GoTransmit, BK4819.cs:591-592)
                ]
            )
            confirmed = self._read_register(0x30)
            if confirmed != _REG30_TX_ENABLED:
                raise Uvk5KeyingError(
                    f"radio did not report TX enabled (reg 0x30={confirmed:#06x}, want "
                    f"{_REG30_TX_ENABLED:#06x})"
                )
        except Exception:
            # Atomic key-up: undo everything (restore RX + tear down audio) so a partial failure
            # never strands a half-key. Then surface the original error (Uvk5KeyingError or transport).
            try:
                self._key_off()
            except Exception:
                logger.exception("uvk5: failed to restore RX after a failed key-up")
            raise
        self._keyed = True
        # TX lead-in (guardrail 1): with TX enabled, queue a fixed slug of silence so the
        # transmitter and the far-end squelch are fully up before real audio plays. Fires once per
        # physical key-up (backs both one-shot transmit() and streaming ptt(True)). Bench-tune —
        # the 0.5 s default is verify-on-hardware; this radio earns its own number.
        if self._lead_bytes:
            self._pacer.enqueue(b"\x00" * self._lead_bytes)

    def _key_off(self) -> None:
        """Restore RX first (RF-safe), then stop the pacer and tear down the playout stream.

        Best-effort and non-raising, mirroring the baofeng inversion (ADR 0093): the transmitter is
        unkeyed (RX registers restored) before the audio teardown, and a transport error while
        unkeying is logged rather than propagated — it must not mask the teardown nor break
        :meth:`close` / the one-shot ``finally``.
        """
        try:
            self._write_registers([(0x30, 0), (0x30, self._reg30)])
        except Exception:
            logger.exception("uvk5: error restoring RX on key-off")
        self._keyed = False
        pacer, self._pacer = self._pacer, None
        if pacer is not None:
            pacer.stop()
        stream, self._playback = self._playback, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"radio accepts {CANONICAL_FORMAT}, got a frame in {audio.format}"
            )
        if self._keyed:
            # Streaming: ptt(True) already holds TX and started the pacer — queue the frame and
            # return. The blocking device write happens on the pacer thread (ADR 0102), never on the
            # caller (the event loop). A pacer torn down by a device failure swallows the frame.
            pacer = self._pacer
            if pacer is not None:
                pacer.enqueue(audio.samples)
            return
        # One-shot: self-key for exactly this clip. The blocking contract stands (station ID, TTS,
        # /transmit rely on "returns once played") — wait for the pacer to drain lead-in + clip.
        byte_rate = CANONICAL_FORMAT.rate * CANONICAL_FORMAT.frame_bytes
        duration = (self._lead_bytes + len(audio.samples)) / byte_rate
        self._key_on()
        try:
            pacer = self._pacer
            if pacer is not None:
                pacer.enqueue(audio.samples)
                pacer.wait_drained(duration + 2.0)
                error = pacer.error
                if error is not None:
                    raise error  # playback failed — surface it (the pacer already unkeyed)
        finally:
            self._key_off()

    def receive(self) -> AudioFrame:
        if self._capture is None:
            self._capture = open_capture_stream(
                self._sd(), device=self._input_device, blocksize=self._blocksize
            )
        data, _overflowed = self._capture.read(self._blocksize)
        # An xrun (overflow) is not fatal — the samples we did get are still valid audio.
        return AudioFrame(bytes(data), CANONICAL_FORMAT)

    # --- status ---------------------------------------------------------------

    def status(self) -> RadioStatus:
        busy = False
        if not self._keyed:
            try:
                rssi = self._read_register(0x67) & 0x1FF
                busy = rssi >= self._squelch_threshold
            except (Uvk5Timeout, Uvk5Closed):
                busy = False  # a stalled/closed link reports not-busy rather than raising
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._keyed,
            busy=busy,
            frequency=self._frequency,
            channel=None,
            tone=self._tone,
            mode=self._mode,
        )

    def capabilities(self) -> frozenset[Capability]:
        return _UVK5_CAPS

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        """Drop PTT, leave full-control mode, and close the transport. Idempotent; atexit-safe."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._keyed:
                self._key_off()
        except Exception:
            logger.exception("uvk5: error dropping PTT on close")
        if self._capture is not None:
            with contextlib.suppress(Exception):
                self._capture.stop()
                self._capture.close()
            self._capture = None
        try:
            self._transport.send(ExitHwMode())  # return the radio to standalone operation
        except Exception:
            logger.exception("uvk5: error leaving full-control mode on close")
        try:
            self._transport.close()
        except Exception:
            logger.exception("uvk5: error closing transport")
        atexit.unregister(self.close)
