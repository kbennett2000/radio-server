"""``Kv4pHt`` — the kv4p HT backend (ADR 0061, ADR 0063), the first real ``CatRadio``.

This composes the three hardware-free kv4p cycles into a backend implementing the
``Radio``/``CatRadio`` surface (:mod:`..base`):

  - :mod:`.transport` — the serial reader thread, the encoded-byte flow-control window, and the
    reconciler (``send_desired_state`` / ``await_applied`` / ``send_tx_audio``).
  - :mod:`.audio` — the Opus RX decoder and the TX re-blocker/encoder (ADR 0065).
  - :mod:`.frames` — the wire structs and flag enums.

It is still built and tested against a **fake transport** (guardrail 6 — hardware bring-up is its
own empirical phase); the ``_transport`` constructor seam injects it. Factory/config/``app.py``
wiring and the ``doctor`` bring-up are a later cycle.

**The central design rule (ADR 0063): ``HostDesiredState`` is a complete state, not a partial
update.** The firmware's ``handleCommands`` does ``desiredState = incomingState;
desiredState.flags &= HOST_STATE_GLOBAL_FLAG_MASK`` — the whole struct and the whole global-flag
word are *replaced* every frame. Omit a flag you set last time and it is silently cleared. So this
class owns a complete desired-state model (:attr:`_desired`) and every mutation is
read-modify-write-the-whole-thing, then reconcile. Two global flags must ride **every** frame:
``RADIO_CONFIG_VALID`` (gates the entire ``sa818.group(...)`` apply — dropped, frequency/tone stop
reaching the module) and ``TX_ALLOWED`` (hard-gates PTT, persists to NVS, defaults false — drop it
and ``ptt(True)`` is accepted, reconciles clean, and never keys). ``RX_AUDIO_OPEN`` (a session
flag) likewise rides every frame so RX audio flows.

Units: the wire speaks float MHz, DRA818 bandwidth codes, and CTCSS *indices*; our API speaks int
Hz, a free-text mode, and CTCSS Hz. We convert and **fail loud** on anything out of range or
unmapped rather than clamp-and-lie (ADR 0063). The marked-default integer values (bandwidth codes,
the frequency raster, the per-module default freq range, the TX lead) are **verify-on-bench**
(guardrail 1); source read as a spec, not ported — kv4p-ht GPL-3.0 @ the shipped release
**v2.0.0.1, ``3f0e809baa02a946c3f0602681303f600c321d31``** (was the unreleased ``e9935bd…``; ADR
0064). The RX/TX **audio** path is Opus on vendor cmd ``0x07`` (ADR 0064 pinned it; ADR 0065
implements it via ``opuslib``): one packet per frame, 48 kHz mono, 40 ms — see :mod:`.audio`.
"""

from __future__ import annotations

import atexit
import dataclasses
import logging
import time
from enum import StrEnum

from ...audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from ..base import SHARED_CAPS, Capability, RadioStatus, UnsupportedCapability
from .audio import RxAudioDecoder, TxAudioEncoder
from .frames import DeviceStateFlag, HostDesiredState, HostStateFlag, RfModuleType
from .pacer import _TxPacer
from .transport import Kv4pTransport

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Capabilities (ADR 0063 — the SCAN reversal)
# --------------------------------------------------------------------------------------

#: What this backend advertises: the shared surface plus tuning — but NOT ``SET_CHANNEL``.
#: ``SCAN`` is in because it gates the *software* ``ScanEngine`` (which tunes via
#: ``set_frequency`` and polls ``status().busy``), and kv4p is the first backend with both a real
#: ``set_frequency`` and a real busy line. The device has no native scan toggle, so ``scan(on)``
#: itself raises (ADR 0063).
_KV4P_CAPS: frozenset[Capability] = SHARED_CAPS | frozenset(
    {Capability.SET_FREQUENCY, Capability.SET_TONE, Capability.SET_MODE, Capability.SCAN}
)


# --------------------------------------------------------------------------------------
# Units (marked defaults, guardrail 1 — VERIFY ON BENCH against the firmware)
# --------------------------------------------------------------------------------------

#: DRA818/SA818 bandwidth codes for the ``bw`` field. The SA818 ``AT+DMOSETGROUP`` convention is
#: 0 = 12.5 kHz (narrow), 1 = 25 kHz (wide); kv4p's ``bw`` enum is read to match. VERIFY ON BENCH.
_BW_NARROW_NFM = 0
_BW_WIDE_FM = 1
#: Our free-text ``mode`` mapped onto the only mode-shaped knob the radio has — channel bandwidth
#: (ADR 0063). FM ↔ 25 kHz, NFM ↔ 12.5 kHz; anything else is rejected.
_MODE_TO_BW: dict[str, int] = {"FM": _BW_WIDE_FM, "NFM": _BW_NARROW_NFM}
_BW_TO_MODE: dict[int, str] = {v: k for k, v in _MODE_TO_BW.items()}

#: The standard 38-tone CTCSS table the SA818 indexes (index 1..38; 0 = tone off). A public EIA
#: table, not firmware code. The exact index↔Hz mapping the module uses is VERIFY ON BENCH.
_CTCSS_TONES: tuple[float, ...] = (
    67.0, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8,
    97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3, 131.8,
    136.5, 141.3, 146.2, 151.4, 156.7, 162.2, 167.9, 173.8, 179.9, 186.2,
    192.8, 203.5, 210.7, 218.1, 225.7, 233.6, 241.8, 250.3,
)
#: Match tolerance for a caller's Hz value against the table; unmapped is rejected, not snapped.
_TONE_TOLERANCE_HZ = 0.1

#: SA818 tuning raster: a set frequency is quantized to the nearest step. Marked default; the real
#: raster (and whether the firmware rounds or the module does) is VERIFY ON BENCH.
_FREQ_RASTER_HZ = 2500

#: Per-module default RX/TX band, used only when no HELLO arrived to report the real min/max
#: (SA818-VHF / SA818-UHF datasheet ranges, in Hz). VERIFY ON BENCH.
_DEFAULT_FREQ_RANGE: dict[RfModuleType, tuple[int, int]] = {
    RfModuleType.SA818_VHF: (134_000_000, 174_000_000),
    RfModuleType.SA818_UHF: (400_000_000, 480_000_000),
}


# --------------------------------------------------------------------------------------
# Timing knobs (marked defaults, guardrail 1)
# --------------------------------------------------------------------------------------

#: Seconds a reconcile (``send_desired_state`` + ``await_applied``) waits for the device to apply.
DEFAULT_RECONCILE_TIMEOUT = 2.0
#: Seconds ``receive()`` polls the transport's RX queue before returning an empty frame. Comfortably
#: over one Opus frame (40 ms; ≈ 25 frames/sec); it returns as soon as a packet is available.
DEFAULT_RECEIVE_TIMEOUT = 0.1
#: How often ``receive()`` polls the (non-blocking) transport queue while waiting.
_RX_POLL_INTERVAL = 0.005
#: Silence transmitted right after PTT keys up, before real audio — the far-end squelch and the
#: reconcile round-trip both take time to settle. **Bench-measured 0.5 s** on the SA818/ESP32 board
#: (2026-07-18, ADR 0069): a tone at 0.2 s clipped its onset on a monitoring receiver, 0.5 s started
#: clean — the same lead the AIOC needed. 0 disables.
DEFAULT_TX_LEAD_SECONDS = 0.5


# --------------------------------------------------------------------------------------
# Config-surface defaults (imported by config/spec.py — this class is the source of truth)
# --------------------------------------------------------------------------------------

class Kv4pBand(StrEnum):
    """Config spelling for the fitted RF module's band — maps to :class:`RfModuleType`.

    A config-surface enum (``vhf`` / ``uhf``) so ``kv4p.module_type`` reads naturally and fails
    loud on a typo, distinct from the wire enum :class:`RfModuleType`. See :data:`_BAND_TO_MODULE`.
    """

    VHF = "vhf"
    UHF = "uhf"


#: ``kv4p.module_type`` spelling -> the wire :class:`RfModuleType`.
_BAND_TO_MODULE: dict[Kv4pBand, RfModuleType] = {
    Kv4pBand.VHF: RfModuleType.SA818_VHF,
    Kv4pBand.UHF: RfModuleType.SA818_UHF,
}


def module_type_from_band(band: "RfModuleType | Kv4pBand | str") -> RfModuleType:
    """Normalise a ``module_type`` arg (a :class:`RfModuleType`, a :class:`Kv4pBand`, or a ``vhf``/
    ``uhf`` string) to a :class:`RfModuleType`. Fails loud on an unrecognised value."""
    if isinstance(band, RfModuleType):
        return band
    return _BAND_TO_MODULE[Kv4pBand(str(band).lower())]


def default_freq_range_hz(band: "RfModuleType | Kv4pBand | str") -> tuple[int, int]:
    """The per-module default (min, max) RX/TX band in Hz for ``band`` — the range used when no HELLO
    has arrived to report the real one (:data:`_DEFAULT_FREQ_RANGE`). Pure (no device): this is the
    band a load-time ``kv4p.frequency`` check validates against (ADR 0074), matching the fallback a
    HELLO-less ``Kv4pHt`` uses. Fails loud on an unrecognised ``band`` via :func:`module_type_from_band`."""
    return _DEFAULT_FREQ_RANGE[module_type_from_band(band)]


#: Serial device the board's USB-UART bridge exposes. kv4p uses a CP210x or CH340, which enumerate
#: as ``/dev/ttyUSB*`` — NOT the AIOC's ``/dev/ttyACM*``. Marked default; prefer a by-id symlink.
DEFAULT_SERIAL_PORT = "/dev/ttyUSB0"
#: The fitted RF module's band, used for the default frequency range **only when no HELLO arrives**
#: to report the real min/max (HELLO is boot-only — ADR 0062 — so on a server restart against a
#: still-running board there is no HELLO, and this fallback decides whether ``kv4p.frequency``
#: validates). A UHF board left at the VHF default rejects any UHF frequency as out-of-band. Marked
#: default; VERIFY ON BENCH against the board in hand (guardrail 1).
DEFAULT_MODULE_TYPE = Kv4pBand.VHF
#: SA818 squelch LEVEL (0..8) baked into the desired state — distinct from ``audio.squelch``. A
#: sane non-zero default: at level 0 the SQ pin never asserts, so ``status().busy`` reads True
#: forever (and a CAT-squelch scan dwells on every channel). The real number is VERIFY ON BENCH.
DEFAULT_SQUELCH = 4
#: Whether the module transmits at high power (the ``HIGH_POWER`` flag). A node exists to reach
#: people, so high power is the default; operator-overridable. VERIFY ON BENCH (the exact levels).
DEFAULT_HIGH_POWER = True
#: Whether TX is permitted at all (the ``TX_ALLOWED`` NVS gate). radio-server exists to transmit, so
#: on by default; set false for a genuinely receive-only node (the gate is a real firmware feature).
DEFAULT_TX_ALLOWED = True
#: The firmware's RX ADC sample-rate multiplier (ADR 0070). Shipped firmware runs the RX ADC ~2 % fast
#: — ``rxAudio.h``: ``config.sample_rate = AUDIO_SAMPLE_RATE * 1.02`` — while telling the Opus encoder
#: the unmultiplied 48 kHz, so received audio arrives ~2 % off-frequency (breaks DTMF; drifts every
#: continuous consumer). :class:`RxAudioDecoder` resamples ``round(48000 * this)`` → 48 kHz to undo it.
#: Marked default 1.02 — the ESP32 I2S divider quantizes the request, so VERIFY ON BENCH and trim to
#: doctor's measured rate (``--rx-level``, guardrail 1). 1.0 disables the correction.
DEFAULT_SAMPLE_RATE_CORRECTION = 1.02
#: TX audio-level multiplier applied to every transmitted sample before the Opus encoder (ADR 0080).
#: The kv4p has no sound card, so — unlike the AIOC, which rides ``alsamixer``'s playback slider —
#: there is no analog stage to bring an overmodulated TX level down; this is that stage in software.
#: Default 1.0 is a no-op (no behaviour change for anyone). If kv4p announcements/voice sound
#: overmodulated or distorted, lower it until clean — a good starting point is ~0.5. VERIFY ON BENCH
#: (guardrail 1): the right level is a per-radio/deviation fact. Values >1.0 are allowed but clamp to
#: full scale rather than clipping into the encoder.
DEFAULT_TX_GAIN = 1.0


class Kv4pKeyingError(RuntimeError):
    """PTT was requested but the device never reported ``TX_ACTIVE`` — a silent no-key.

    Raised instead of letting a requested transmission go out as dead air (e.g. the device's own
    ``TX_ALLOWED`` NVS gate is off, or the RF module faulted). Surfacing it lets the caller retry
    or alarm rather than key a transmitter that isn't actually on the air.
    """


class Kv4pHt:
    """kv4p HT backend: audio + PTT + CAT tuning over one KISS-framed UART, via a state reconciler.

    Args:
        serial_port / baud / window_size: passed to :class:`~.transport.Kv4pTransport` when this
            constructs its own transport (the real path).
        module_type: RF module fitted, used for the default frequency band **only when no HELLO
            arrives** to report the real range (:data:`_DEFAULT_FREQ_RANGE`). A HELLO overrides it.
        squelch: SA818 squelch level 0..8 baked into the desired state (:data:`DEFAULT_SQUELCH`) —
            distinct from ``audio.squelch``. See the level-0 caveat in :meth:`status`.
        high_power: whether the module transmits at high power (:data:`DEFAULT_HIGH_POWER`).
        tx_allowed: whether TX is permitted (:data:`DEFAULT_TX_ALLOWED`); false → a receive-only
            node (the firmware ``TX_ALLOWED`` NVS gate). ``ptt(True)`` on a false gate raises
            :class:`Kv4pKeyingError` (no silent no-key).
        frequency: optional initial frequency in Hz; when given, tuned once at construction (via
            :meth:`set_frequency`, so out-of-band fails loud). Unset leaves the device on its
            NVS-persisted last-used frequency — no invented default is put on the air.
        tx_lead_seconds: silence played after key-up before real audio
            (:data:`DEFAULT_TX_LEAD_SECONDS`); 0 disables.
        sample_rate_correction: the firmware's RX ADC multiplier
            (:data:`DEFAULT_SAMPLE_RATE_CORRECTION`, ADR 0070); the decoder resamples the ~2 %-fast RX
            stream back to a real 48 kHz. 1.0 disables it.
        tx_gain: TX audio-level multiplier (:data:`DEFAULT_TX_GAIN`, ADR 0080) applied to every
            transmitted sample before the Opus encoder — the kv4p's software stand-in for the AIOC's
            OS-mixer playback level. 1.0 is a no-op; lower it when TX is overmodulated.
        receive_timeout: :meth:`receive` poll budget (:data:`DEFAULT_RECEIVE_TIMEOUT`).
        reconcile_timeout: per-reconcile wait (:data:`DEFAULT_RECONCILE_TIMEOUT`).
        _transport: test seam — an object with the ``Kv4pTransport`` surface
            (``connect``/``send_desired_state``/``await_applied``/``send_tx_audio``/``read_audio``/
            ``device_state``/``hello``/``window_size``/``close``). Defaults to a real transport.
    """

    backend_name = "kv4p"

    def __init__(
        self,
        *,
        serial_port: str | None = None,
        baud: int | None = None,
        window_size: int | None = None,
        module_type: "RfModuleType | Kv4pBand | str" = RfModuleType.SA818_VHF,
        squelch: int = DEFAULT_SQUELCH,
        high_power: bool = DEFAULT_HIGH_POWER,
        tx_allowed: bool = DEFAULT_TX_ALLOWED,
        frequency: int | None = None,
        tx_lead_seconds: float = DEFAULT_TX_LEAD_SECONDS,
        sample_rate_correction: float = DEFAULT_SAMPLE_RATE_CORRECTION,
        tx_gain: float = DEFAULT_TX_GAIN,
        receive_timeout: float = DEFAULT_RECEIVE_TIMEOUT,
        reconcile_timeout: float = DEFAULT_RECONCILE_TIMEOUT,
        _transport: Kv4pTransport | None = None,
    ) -> None:
        # Accept a RfModuleType, a Kv4pBand, or a "vhf"/"uhf" string (the config surface); the
        # frequency-range fallback below is keyed by RfModuleType.
        module_type = module_type_from_band(module_type)
        if _transport is not None:
            self._transport = _transport
        else:
            kwargs: dict[str, object] = {}
            if serial_port is not None:
                kwargs["serial_port"] = serial_port
            if baud is not None:
                kwargs["baud"] = baud
            if window_size is not None:
                kwargs["window_size"] = window_size
            self._transport = Kv4pTransport(**kwargs)

        self._receive_timeout = receive_timeout
        self._reconcile_timeout = reconcile_timeout
        # Precompute the TX lead-in as a silent 48k PCM clip (0 disables); pushed through the TX
        # encoder on key-up so the device transmits silence while the far end settles.
        self._lead_bytes = (
            round(CANONICAL_FORMAT.rate * float(tx_lead_seconds)) * CANONICAL_FORMAT.frame_bytes
        )

        # Opus codec (ADR 0065). Construction is cheap — libopus loads lazily on the first
        # encode/decode (audio.py), so a codec-free path never touches it.
        # One continuous decoder for the session's RX stream; corrects the firmware's ~2%-fast ADC
        # (ADR 0070) so a real 48 kHz reaches DTMF, the recorder, the hub, and the Mumble link.
        self._rx = RxAudioDecoder(sample_rate_correction=sample_rate_correction)
        self._tx_gain = float(tx_gain)  # TX level multiplier applied pre-encode (ADR 0080)
        self._tx: TxAudioEncoder | None = None  # a fresh one per keying (holds a re-block remainder)
        # While a key is HELD (streaming), a pacer thread owns self._tx and keeps a continuous frame
        # stream flowing to the firmware — real audio when buffered, encoded silence otherwise — so the
        # TX buffer never underruns and loops stale audio (the "Max Headroom" bug, ADR 0082). None
        # except between ptt(True) and ptt(False); the one-shot transmit() path never uses it.
        self._pacer: _TxPacer | None = None
        self._keyed = False
        self._closed = False

        # Run the passive-first handshake (ADR 0066), then learn the frequency band from a HELLO if we
        # got one (fresh boot), else fall back to the module-type default.
        connected = self._transport.connect()
        hello = self._transport.hello
        if hello is not None:
            self._freq_min_hz = round(hello.version.min_radio_freq * 1e6)
            self._freq_max_hz = round(hello.version.max_radio_freq * 1e6)
            self._module_type = RfModuleType(hello.version.rf_module_type)
        else:
            self._freq_min_hz, self._freq_max_hz = _DEFAULT_FREQ_RANGE[module_type]
            self._module_type = module_type

        # The complete desired-state model. RX_AUDIO_OPEN rides every frame (open the RX audio
        # stream); TX_ALLOWED and HIGH_POWER ride it too when configured on. RADIO_CONFIG_VALID
        # stays off until the first set_frequency (a group() apply on freq=0.0 is meaningless).
        # squelch is the level field (0..8) — see the level-0 caveat in status().
        #
        # Seed the tuning (freq/ctcss/memory) from what connect() preserved, NOT zeros: shipped firmware
        # persists desiredState to NVS unconditionally (ADR 0066), so an initial reconcile carrying
        # freq 0.0 would overwrite the operator's stored frequency even with RADIO_CONFIG_VALID off.
        # This keeps the model honest until the server sets its own frequency via set_frequency().
        initial_flags = HostStateFlag.RX_AUDIO_OPEN
        if tx_allowed:
            initial_flags |= HostStateFlag.TX_ALLOWED
        if high_power:
            initial_flags |= HostStateFlag.HIGH_POWER
        self._desired = HostDesiredState(
            sequence=0,
            memory_id=connected.memory_id,
            flags=int(initial_flags),
            bw=_BW_WIDE_FM,
            freq_tx=connected.freq_tx,
            freq_rx=connected.freq_rx,
            ctcss_tx=connected.ctcss_tx,
            squelch=squelch,
            ctcss_rx=connected.ctcss_rx,
        )
        self._configured = False
        # Push the initial state so the device opens RX audio and records TX_ALLOWED/squelch.
        self._reconcile()
        # Optionally tune to a configured start frequency, reusing set_frequency's out-of-band
        # validation (raises). Unset leaves the device on its NVS last-used frequency.
        if frequency is not None:
            self.set_frequency(frequency)

        atexit.register(self.close)

    # --- reconcile core -------------------------------------------------------

    def _reconcile(self):
        """Send the whole desired-state model and block until the device applies it."""
        seq = self._transport.send_desired_state(self._desired)
        return self._transport.await_applied(seq, timeout=self._reconcile_timeout)

    def _with_flag(self, flag: HostStateFlag, on: bool) -> int:
        """The model's flag word with ``flag`` set or cleared (read-modify-write the whole word)."""
        flags = HostStateFlag(self._desired.flags)
        flags = flags | flag if on else flags & ~flag
        return int(flags)

    # --- CAT tuning (whole-struct RMW; fail loud on out-of-range/unmapped) -----

    def set_frequency(self, hz: int) -> None:
        """Tune to ``hz`` (simplex — sets both TX and RX). Fails loud out of band; does not key.

        The firmware clamps an out-of-range frequency silently (``clampModuleRadioFreq``); we raise
        instead, so ``status()`` never reports a frequency the caller did not ask for. Split/offset
        (separate TX/RX) is not in the ``Radio`` protocol — a future ADR, not invented here.
        """
        if not self._freq_min_hz <= hz <= self._freq_max_hz:
            raise ValueError(
                f"frequency {hz} Hz is out of band [{self._freq_min_hz}, {self._freq_max_hz}] "
                f"for {self._module_type.name}"
            )
        quantized = round(hz / _FREQ_RASTER_HZ) * _FREQ_RASTER_HZ
        mhz = quantized / 1e6
        self._desired = dataclasses.replace(
            self._desired,
            freq_tx=mhz,
            freq_rx=mhz,
            flags=self._with_flag(HostStateFlag.RADIO_CONFIG_VALID, True),
        )
        self._configured = True
        self._reconcile()

    def set_tone(self, tone: float | None) -> None:
        """Set the CTCSS **TX** tone in Hz, or ``None`` to disable. Rejects an unmapped tone.

        Only ``ctcss_tx`` is set; ``ctcss_rx`` stays 0. RX tone squelch would silence the receiver
        in a way nothing in our stack can see, and repeater access (a TX tone) is the case that
        matters (ADR 0063). An unmapped Hz value raises rather than snapping to the nearest — a
        silently wrong tone is worse than a raise.
        """
        index = 0 if tone is None else self._tone_to_index(tone)
        self._desired = dataclasses.replace(self._desired, ctcss_tx=index)
        self._reconcile()

    def set_mode(self, mode: str) -> None:
        """Map a free-text ``mode`` onto channel bandwidth: FM → 25 kHz, NFM → 12.5 kHz.

        There is no mode field on the wire — only ``bw`` — so we map our ``mode`` onto the only
        mode-shaped knob the radio has (ADR 0063). Anything but FM/NFM is rejected.
        """
        key = mode.upper()
        if key not in _MODE_TO_BW:
            raise ValueError(f"mode {mode!r} is not supported (want one of {sorted(_MODE_TO_BW)})")
        self._desired = dataclasses.replace(self._desired, bw=_MODE_TO_BW[key])
        self._reconcile()

    def set_channel(self, n: int) -> None:
        """Unsupported: ``memory_id`` is an opaque host tag; the device has no memory table."""
        raise UnsupportedCapability(Capability.SET_CHANNEL)

    def scan(self, on: bool) -> None:
        """No native scan toggle on this device — the software ``ScanEngine`` is the path.

        ``Capability.SCAN`` is advertised because it gates that software sweep (which tunes via
        ``set_frequency`` and polls ``status().busy``), not this hardware toggle. ``radio.scan()``
        is dead across the tree; ADR 0063 flags it as a possible future tidy.
        """
        raise NotImplementedError(
            "kv4p has no native scan toggle; the software ScanEngine (Capability.SCAN) drives "
            "scanning via set_frequency + status().busy"
        )

    def _tone_to_index(self, tone: float) -> int:
        for i, hz in enumerate(_CTCSS_TONES):
            if abs(hz - float(tone)) <= _TONE_TOLERANCE_HZ:
                return i + 1
        raise ValueError(f"CTCSS tone {tone} Hz is not in the SA818 table")

    # --- keying / TX (the AIOC _keyed one-shot-vs-streaming discipline) --------
    # One-shot transmit() self-keys, sends the whole clip, drops — it never holds the key idle, so it
    # is unchanged. A HELD key (streaming: browser talker + Mumble bridge) runs a _TxPacer that keeps
    # a continuous frame stream flowing while keyed (real audio buffered by transmit(), silence in the
    # gaps), matching the AIOC's continuous-output contract so the firmware never underruns (ADR 0082).

    def _key_on(self) -> None:
        """Assert PTT, reconcile, and confirm the device actually keyed (else raise, fail-safe)."""
        self._tx = TxAudioEncoder(tx_gain=self._tx_gain)  # fresh per keying (flush drains the remainder)
        self._transport.reset_tx_stats()  # per-keying TX telemetry starts clean (ADR 0069)
        self._desired = dataclasses.replace(
            self._desired, flags=self._with_flag(HostStateFlag.PTT_REQUESTED, True)
        )
        state = self._reconcile()
        if not DeviceStateFlag(state.flags) & DeviceStateFlag.TX_ACTIVE:
            # Fail-safe: drop the request we just made before surfacing the no-key.
            self._desired = dataclasses.replace(
                self._desired, flags=self._with_flag(HostStateFlag.PTT_REQUESTED, False)
            )
            try:
                self._reconcile()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.exception("kv4p: failed to clear PTT after a no-key")
            self._tx = None
            raise Kv4pKeyingError("device did not report TX_ACTIVE after PTT was requested")
        if self._lead_bytes:
            self._send_blocks(self._tx.push(AudioFrame(b"\x00" * self._lead_bytes)))

    def _key_off(self) -> None:
        """Flush the encoder tail (never clip it), then drop PTT and reconcile.

        The one-shot path's key-down: it owns and flushes ``self._tx`` directly. The streaming path
        drops the key via :meth:`_key_off_streaming` instead, because there the pacer owns the flush.
        """
        if self._tx is not None:
            self._send_blocks(self._tx.flush())
            self._tx = None
        self._drop_ptt()

    def _key_off_streaming(self) -> None:
        """Streaming key-down (ADR 0082): stop the pacer, flush its tail, then drop PTT.

        The pacer thread is the sole user of ``self._tx`` during a held key, so the flush must happen
        on this (caller) thread only after :meth:`_TxPacer.stop` has joined it — never from two
        threads. :meth:`_TxPacer.flush_tail` sends any buffered remainder + the encoder tail (the same
        never-clip-the-tail guarantee :meth:`_key_off` gives the one-shot path).
        """
        pacer, self._pacer = self._pacer, None
        if pacer is not None:
            pacer.stop()
            pacer.flush_tail()
        self._tx = None
        self._drop_ptt()

    def _drop_ptt(self) -> None:
        """Clear ``PTT_REQUESTED`` in the desired-state model and reconcile (the key-down reconcile)."""
        self._desired = dataclasses.replace(
            self._desired, flags=self._with_flag(HostStateFlag.PTT_REQUESTED, False)
        )
        self._reconcile()

    def _send_blocks(self, blocks: list[bytes]) -> None:
        for block in blocks:
            self._transport.send_tx_audio(block)

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"radio accepts {CANONICAL_FORMAT}, got a frame in {audio.format}"
            )
        if self._keyed:
            # Streaming: ptt(True) already keyed and started the pacer. Hand the frame to the pacer's
            # buffer; the pacer thread encodes + sends it (and fills the gaps with silence), so this
            # is the ONE coherent sender — no encoder race, no doubled frame (ADR 0082).
            self._pacer.enqueue(audio.samples)  # type: ignore[union-attr]
            return
        # One-shot: self-key for exactly this clip; the key always drops, even on error.
        self._key_on()
        try:
            self._send_blocks(self._tx.push(audio))  # type: ignore[union-attr]
        finally:
            self._key_off()

    def ptt(self, on: bool) -> None:
        if on:
            if not self._keyed:
                self._key_on()  # builds self._tx, sends the lead-in, confirms TX_ACTIVE (may raise)
                # Start the pacer AFTER key-up: the lead-in ran synchronously on this thread, so the
                # pacer never overlaps it on the encoder. From here the pacer is the sole sender.
                self._pacer = _TxPacer(self._tx, self._transport.send_tx_audio)
                self._pacer.start()
                self._keyed = True
        else:
            if self._keyed:
                self._key_off_streaming()
                self._keyed = False

    def receive(self) -> AudioFrame:
        """Return one decoded 48k frame, blocking ~one frame; an empty frame on a timeout.

        Polls the transport's bounded RX queue (which decouples the reader thread from this
        consumer). Each queued payload is one Opus packet (one ``RX_AUDIO`` frame) and decodes to one
        canonical 48 kHz ``AudioFrame`` — 1920 samples for the firmware's 40 ms frame (ADR 0064/0065).

        Fail-soft: a corrupt/truncated packet is dropped inside :meth:`RxAudioDecoder.push` (empty
        frame, no raise), so a bad byte off the wire cannot kill the RX pump. A missing libopus is a
        configuration error and does surface (``Kv4pOpusUnavailable``) rather than silently no-audio.
        """
        deadline = time.monotonic() + self._receive_timeout
        while True:
            packet = self._transport.read_audio()
            if packet is not None:
                return self._rx.push(packet)
            if time.monotonic() >= deadline:
                return AudioFrame(b"")
            time.sleep(_RX_POLL_INTERVAL)

    # --- status / capabilities / lifecycle ------------------------------------

    def status(self) -> RadioStatus:
        """Snapshot from the last ``DeviceState``. ``busy`` is a real carrier detect (SQ pin).

        ``busy = not SQUELCHED``: ``SQUELCHED`` comes off the module's hardware SQ pin, so an open
        squelch (carrier present) reads busy — the genuine COS line that makes ``squelch=cat`` work
        here. Caveat for the config cycle: at squelch **level 0** the SQ pin never
        asserts, so ``busy`` would read True forever; a sane non-zero level is that cycle's call.
        """
        state = self._transport.device_state
        if state is None:
            return RadioStatus(backend=self.backend_name)
        flags = DeviceStateFlag(state.flags)
        return RadioStatus(
            backend=self.backend_name,
            transmitting=bool(flags & DeviceStateFlag.TX_ACTIVE),
            busy=not (flags & DeviceStateFlag.SQUELCHED),
            frequency=round(state.freq_rx * 1e6),
            channel=None,
            tone=self._index_to_tone(state.ctcss_tx),
            mode=_BW_TO_MODE.get(state.bw),
        )

    def capabilities(self) -> frozenset[Capability]:
        return _KV4P_CAPS

    @property
    def tx_stats(self):
        """This keying's TX-audio telemetry (:class:`~.transport.TxStats`) — for bench bring-up.

        Not part of the ``Radio`` protocol; ``doctor`` reads it after a keyed run to report encoded
        bytes/frame and whether the flow-control window ever blocked (ADR 0069). Reset per key-up.
        """
        return self._transport.tx_stats

    @property
    def window_size(self) -> int:
        """Effective flow-control window (encoded bytes) — for doctor's frames-per-window figure."""
        return self._transport.window_size

    def _index_to_tone(self, index: int) -> float | None:
        if 1 <= index <= len(_CTCSS_TONES):
            return _CTCSS_TONES[index - 1]
        return None

    def close(self) -> None:
        """Drop PTT (best-effort) and close the transport. Idempotent; safe at exit."""
        if self._closed:
            return
        self._closed = True
        try:
            if self._keyed:
                self.ptt(False)
        except Exception:  # pragma: no cover - best-effort
            logger.exception("kv4p: error dropping PTT on close")
        try:
            self._transport.close()
        except Exception:  # pragma: no cover - best-effort
            logger.exception("kv4p: error closing transport")
        atexit.unregister(self.close)
