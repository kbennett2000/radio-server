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
import time

from ..base import (
    Capability,
    PaState,
    RadioNotReady,
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
#: Backend-declared RX activity-gate mode — the per-backend override of the global `audio.squelch`
#: (ADR 0121), resolved via `resolve_squelch_mode`. Plain string (the `SquelchMode` value); `spec.py`
#: wraps it in `SquelchMode(...)`, so the backend never imports the `activity` layer. **`cat` because**
#: post-F3 (ADR 0120) dock entry force-opens the AF path, so the radio hisses continuously — software
#: `audio` VAD then sees constant energy and can't gate, and `off` never segments. Only `cat` (the
#: reg-0x67 RSSI COS busy line above, read over `status().busy`) actually gates RX per-transmission.
DEFAULT_SQUELCH_MODE = "cat"
#: **The firmware owns the PA chain, not this backend** (ADR 0128, verified on the bench).
#: This backend used to write reg 0x36 (PA bias/gain) on key-up from a `tx_power_pct` percentage.
#: That write was dead: the F5 dock firmware's `Dock_ForceTx` fires on the reg-0x30 TX edge — i.e.
#: *after* 0x36 in the same batch — and calls `BK4819_SetupPowerAmplifier(TXP_CalculatedSetting)`,
#: overwriting it with the radio's own flash-calibrated value. Measured while keyed on 445.800:
#: reg 0x36 = 0x0CA2, i.e. **bias 12** with the UHF gain byte, never the 255 this backend computed.
#: The calibration lives in the radio's SPI flash and is not reachable over the dock, so the host
#: cannot compute a correct bias anyway. The lever for TX power is the radio's own OUTPUT_POWER
#: (Low/Mid/High) setting, which is what `TXP_CalculatedSetting` is derived from.
#:
#: **Amended by ADR 0132 on one point: the BAND half of that register is ours, and the timing is
#: everything.** `Dock_ForceTx` sets the PA up from `gCurrentVfo` — the RADIO's own VFO — not from
#: the frequency the host tuned (`uart.c:729-732`), so on any frequency in the other band it picks
#: the wrong gain byte and the wrong LNA path. The old write lost because it went *before* the
#: firmware's; a write *after* the firmware's ~25 ms sequence sticks. So the backend now re-writes
#: 0x36 keeping the firmware's bias (which we still cannot compute) and correcting only the gain
#: byte, plus 0x33's band bits. Bias: firmware's. Band: ours.

#: reg 0x33 is the raw BK4819 GPIO-output byte, written whole (`bk4829.c:434`, mask `0x40 >> pin`).
_REG33_RX_ENABLE = 0x40  # GPIO0_PIN28; stock clears it on TX (radio.c:987)
_REG33_PA_ENABLE = 0x20  # GPIO1_PIN29; stock sets it on TX (radio.c:1017) — the PA rail
_REG33_UHF_LNA = 0x08  # GPIO3_PIN31
_REG33_VHF_LNA = 0x04  # GPIO4_PIN32
#: The two LNA-path bits, i.e. exactly what a retune must clear before selecting a band. ADR 0112's
#: prose said "clear bits 3-4" while setting bits 2-3, and the code (`& 0xFFE7`) followed the prose:
#: it never cleared the VHF bit, so VHF->UHF left BOTH paths enabled. Every other bit in this byte
#: belongs to the firmware (the audio-amp gate, the LEDs) — preserve them, touch only these two.
_REG33_LNA_BITS = _REG33_UHF_LNA | _REG33_VHF_LNA
#: The upper bits of reg 0x33 are the pin output-ENABLES, and they are not optional. The firmware
#: initialises its shadow to exactly this (`gBK4819_GpioOutState = 0x9000`, `bk4829.c:198`) and from
#: then on only ORs/ANDs the low pin bits into it (`BK4819_ToggleGpioOut`, `bk4829.c:434-442`) — so
#: every value the radio is ever meant to see carries it. Every idle read on this bench does:
#: 0x9048, 0x904A, 0x9046, 0x9044.
#:
#: This matters because the shapes below are *computed* from a seed read. A radio found disabled
#: reads 0x33 as 0 — the same state the reg-0x30 repair exists for — and rebuilding from that seed
#: produced 0x0040: RX_ENABLE set in a register with no output enables, i.e. a receiver that stays
#: off. That shipped, and the bench demonstrated it: the reg-0x30 repair fired and `/status.rssi`
#: was still 0. Repairing one register and rebuilding its sibling from the same wreckage is half a
#: fix, so the base is asserted unconditionally now.
_REG33_BASE = 0x9000

#: The widest RX↔TX separation `set_split` will accept (ADR 0133). Every standard amateur repeater
#: offset is far inside it — 600 kHz on 2 m, 1.6 MHz on 1.25 m, 5 MHz on 70 cm. It exists to catch
#: the arithmetic typo (146.340 entered as 1463.40), because unlike the receive frequency, this one
#: radiates.
_MAX_SPLIT_OFFSET_HZ = 10_000_000
#: reg 0x36 = `(bias << 8) | 0x80 | gain`, gain `0x08` VHF / `0x22` UHF (`bk4829.c:743`). The split
#: is the firmware's own 280 MHz, not the 174 MHz band edge — matched deliberately, see ADR 0132.
_REG36_ENABLE = 0x80
_PA_GAIN_VHF = 0x08
_PA_GAIN_UHF = 0x22

#: reg 0x30 value that means "TX enabled" (GoTransmit, BK4819.cs:592). Read back to confirm keying.
_REG30_TX_ENABLED = 0xC1FE
#: The bit in reg 0x30 the dock firmware watches for the TX edge (`DOCK_REG30_TX_DSP`, dock.c:56).
#: Set in `_REG30_TX_ENABLED`, clear in the RX word — so it distinguishes the two.
_REG30_TX_BIT = 0x0002
#: The stock RX system-control word. Measured on this hardware, every idle read on both bands, many
#: runs: `0x30 = 0xBFF1`. Used only to REPAIR an unusable seed — see `_seed_reg30`.
_REG30_RX_DEFAULT = 0xBFF1
#: reg 0x47 AF-selector bit that is high when AF=FM (unmuted, 0x6142) vs idle mute (0x6042). The F3
#: firmware force-open (`Dock_ForceRxAudioAlive`, ADR 0120) sets REG_47=FM in the SAME routine that
#: raises the un-dockable audio-amp gate GPIOA8 — so a REG_47 read-back is the only host-visible PROXY
#: for "the force-open ran". On a pre-F3 build REG_47 stays at idle mute and this bit is 0.
_REG47_AF_FM_BIT = 0x0100
#: First-start dead-RX mitigation (ADR 0122): the 0x0870 that triggers the firmware force-open is
#: fire-and-forget and can be lost in the reset-on-open boot race. Re-send it up to this many times,
#: settling briefly between, until REG_47 reads FM. Bounded — a non-F3 dock (REG_47 never FM) exits
#: after the retries with a logged warning; it never hangs and never falsely claims a fix.
_ENTER_HW_MODE_RETRIES = 3
_ENTER_HW_MODE_SETTLE_S = 0.1
#: Host-audio-leg mitigation (default OFF, enabled only if the live repro shows this leg): if the first
#: capture block reads below this RMS the USB device was likely still settling, so reopen the stream
#: once. Post-F3 the receiver hisses continuously, so a healthy first block is well above this floor.
#: VERIFY ON BENCH.
_CAPTURE_FLOOR_RMS = 50.0
_CAPTURE_SETTLE_S = 0.2
#: Settle after the reg-0x30 TX-enable write before reading it back. The dock firmware's
#: `Dock_ForceTx` spends ~25 ms in `SYSTEM_DelayMs` PA-sequence delays and does not service its
#: UART while it does, so this waits the race out instead of polling into it (see `_confirm_keyed`).
_KEY_SETTLE_S = 0.05
#: How many times to send the whole key-up sequence before declaring a no-key. Dock writes are
#: fire-and-forget, so the *write* can be what gets lost — re-reading can never recover that.
_KEY_ATTEMPTS = 3
#: Then read reg 0x30 back up to this many times, settling between, before declaring a no-key.
#: Bounded, so a radio that genuinely never keys still fails loud.
_KEY_CONFIRM_ATTEMPTS = 5
_KEY_CONFIRM_SETTLE_S = 0.02
#: Seconds since the previous `receive()` beyond which the capture ring is *expected* to have
#: overrun, because nobody was reading it. A block is ~20 ms, so half a second is unambiguous.
_XRUN_READ_GAP_S = 0.5
#: An overrun reported within this many block periods of the previous read happened while the
#: reader was ON CADENCE — it cannot be the reader falling behind, because it did not fall
#: behind. 1.5 leaves room for scheduling jitter without swallowing a real one-block stall.
_XRUN_ON_CADENCE_BLOCKS = 1.5
#: How many consecutive overrunning reads to excuse after a known gap in reading (a keyed over, a
#: freshly opened stream). The ring is deeply backlogged by then and drains over several reads, so
#: excusing exactly one still reported the rest as faults. Bounded on purpose: a genuine stall that
#: starts inside a gap surfaces once the allowance runs out. ~25 reads is well under a second.
_XRUN_DRAIN_READS = 25
#: How many consecutive failed RSSI reads to answer from the last known squelch state before
#: giving up and reporting not-busy. Each failure costs the transport's full timeout, so even a
#: few reads cover several seconds of wall clock — enough to ride out a busy dock without letting
#: a genuinely dead link hold the RX gate open indefinitely.
_BUSY_HOLD_READS = 3
#: Attempts for the connect-time reg-0x30 seed read. The dock drops frames and the first read
#: after opening the port is the likeliest to go (ADR 0131) — before this, one dropped frame
#: there failed service startup outright, because the read sat on the construction path with
#: no retry. Exhausting these is treated the same as an unusable value: assert the stock word.
_SEED_READ_ATTEMPTS = 4
#: Min seconds between ALSA capture-overrun (xrun) warnings, so a sustained overrun logs once per
#: window instead of at the ~50 Hz frame rate (ADR 0125).
_XRUN_WARN_INTERVAL_S = 5.0
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
    {
        Capability.SET_FREQUENCY,
        Capability.SET_SPLIT,
        Capability.SET_TONE,
        Capability.SET_MODE,
        Capability.SCAN,
    }
)

#: Names the ``uvk5`` extra (serial + soundcard) when the real ``sounddevice`` is missing.
_AUDIO_EXTRA_MSG = (
    "the UV-K5/Quansheng Dock backend needs the 'uvk5' extra (pyserial + sounddevice): "
    "install with `pip install 'radio-server[uvk5]'` (and the system libportaudio2)"
)


def _block_rms(pcm: bytes) -> float:
    """RMS of an s16le PCM block (0..32767). Local numpy import — the backend deliberately does not
    import the `activity` layer (see the `DEFAULT_SQUELCH_MODE` note above), so it does not reuse
    `activity.gate.frame_rms`. Only used by the default-OFF capture-reopen probe (ADR 0122)."""
    import numpy as np

    if not pcm:
        return 0.0
    samples = np.frombuffer(pcm, dtype="<i2")
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


class Uvk5KeyingError(RuntimeError):
    """PTT was requested but the radio never reported ``reg 0x30 == 0xC1FE`` — a silent no-key.

    Raised (after restoring RX) instead of letting a requested transmission go out as dead air,
    so the caller can retry or alarm rather than believe it is on the air when it is not.

    It used to carry two "before a frequency is set" refusals as well, which were never silent
    no-keys and never keying faults; they are `RadioNotReady` now, with the rest of the fleet's
    readiness refusals (ADR 0173). What is left here is keying: a no-key, a retune or split while
    keyed, and the `tx_allowed` gate.
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
        freq_min_hz: int = DEFAULT_FREQ_MIN_HZ,
        freq_max_hz: int = DEFAULT_FREQ_MAX_HZ,
        input_device: str | int = DEFAULT_INPUT_DEVICE,
        output_device: str | int = DEFAULT_OUTPUT_DEVICE,
        blocksize: int = DEFAULT_BLOCKSIZE,
        tx_lead_seconds: float = DEFAULT_TX_LEAD_SECONDS,
        capture_reopen_on_floor: bool = False,
        _transport: Uvk5Transport | None = None,
        _audio=None,
        _enter_settle_s: float | None = None,
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
        # Host-audio-leg first-start dead-RX mitigation (ADR 0122): default OFF (receive() stays
        # byte-identical); when on, the first _open_capture() reopens once if the first block is floor.
        self._capture_reopen_on_floor = capture_reopen_on_floor
        self._capture_reopened = False
        # Settle between EnterHwMode re-sends; test seam sets 0.0 to avoid real sleeps.
        self._enter_settle_s = (
            _ENTER_HW_MODE_SETTLE_S if _enter_settle_s is None else _enter_settle_s
        )
        self._playback = None  # open only while keyed
        self._pacer: SoundCardTxPacer | None = None  # per-keying playout writer thread (ADR 0102)
        # Rate-limit the capture-overrun (xrun) warning so a sustained overrun logs once per window
        # rather than 50×/s (ADR 0125): the flag was silently discarded before, which is exactly why
        # the RX-pump starvation left "zero xruns" in the journal.
        self._last_xrun_warn = 0.0
        # Reads-worth of overrun to excuse after a gap in reading. Counts down; a clean read
        # (the reader has caught up) zeroes it. See `receive()`.
        self._expect_xrun = 0
        self._last_read_at = 0.0  # monotonic; 0.0 makes the very first read count as a gap
        # Last successfully-read squelch state, and how many consecutive reads have failed since.
        self._last_busy = False
        self._busy_stale = 0

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
        self._enter_hw_mode_verified()
        self._reg30 = self._seed_reg30()
        self._reg33 = self._seed_reg33()
        # `None` when the synthesiser does not hold a frequency: this backend refuses to key on a
        # number no radio can be on, and refusing means saying so, not inventing one (ADR 0174).
        self._frequency = self._seed_frequency()
        # Split is never seeded from the radio (ADR 0133). The synthesiser holds ONE frequency and
        # a split leaves no trace in it, so a process that died mid-over would read the TX leg back
        # and adopt it as its RX frequency — the "believe whatever you find" fault class of ADR 0132.
        # `uvk5.frequency` is REQUIRED config, so the `set_frequency` below always re-tunes.
        self._tx_frequency: int | None = None
        #: The PA setup read back at the last key-up, or None before the first over of this
        #: process. Never seeded from the radio at connect: reg 0x36 read while un-keyed is the
        #: firmware's leftover, not what the PA will do on the next over (ADR 0134).
        self._pa: PaState | None = None

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

    def _seed_reg30(self) -> int:
        """Seed the RX system-control word, repairing a value the radio cannot be receiving on.

        `self._reg30` is the value written back to un-key and at the end of every `set_frequency`.
        It was seeded from a bare read of whatever the radio happened to be doing at connect — and
        adopted for the whole life of the process. So a radio found in a bad state does not merely
        start badly, it **stays** bad: every retune writes the bad value back, and nothing ever
        re-reads it.

        Reproduced on the bench (ADR 0132). Leave the radio at `0x30 = 0` — which is exactly where
        a lost un-key leaves it, and which was observed in a register dump straight after a probe
        run — and the receiver is off: reg 0x67 RSSI reads 157 before, **0** after. Start the
        service against that radio and it seeds `_reg30 = 0`, reports `rssi 0 / busy false` for
        ever, and passes not one byte of received audio. Restarting only helps if the radio happens
        to be healthy in that moment, which is why this looked intermittent.

        Two values cannot be an RX word: `0` (nothing enabled) and anything with the dock's TX bit
        set (the radio was mid-transmission when we connected — worth a warning of its own). In
        either case take the measured stock word and *write* it, so connecting repairs the radio
        instead of inheriting its damage.
        """
        value = 0
        why = "everything disabled"
        for attempt in range(_SEED_READ_ATTEMPTS):
            try:
                value = self._read_register(0x30)
            except Uvk5Timeout:
                # The dock drops frames, and the first read after opening the port is the likeliest
                # to go (ADR 0131). Before this, one dropped frame here killed service startup
                # outright — the read sat on the construction path with no retry.
                logger.debug("uvk5: reg 0x30 seed read timed out (attempt %d)", attempt + 1)
                why = "the read never came back"
                time.sleep(_KEY_CONFIRM_SETTLE_S)
                continue
            if value and not value & _REG30_TX_BIT:
                return value
            why = "everything disabled" if not value else "TX bit set"
            break
        logger.warning(
            "uvk5: reg 0x30 read back %#06x at connect, which is not a receiving state (%s). "
            "Seeding the stock RX word %#06x and writing it, rather than adopting a value that "
            "would leave this radio deaf for the life of the process (ADR 0132).",
            value, why, _REG30_RX_DEFAULT,
        )
        self._write_registers([(0x30, 0), (0x30, _REG30_RX_DEFAULT)])
        return _REG30_RX_DEFAULT

    def _seed_reg33(self) -> int:
        """Seed the GPIO-output byte, repairing a value with no pin output-enables.

        Same fault class as :meth:`_seed_reg30`, same reason: a radio found disabled reads this as
        ``0``, and every shape the backend writes is *computed* from the seed. Without the
        `_REG33_BASE` enables the low bits address pin drivers that are switched off, so asserting
        RX_ENABLE achieves nothing — which is exactly what the bench showed after the reg-0x30
        repair landed: the repair fired, and ``/status.rssi`` stayed at 0.
        """
        value = 0
        for attempt in range(_SEED_READ_ATTEMPTS):
            try:
                value = self._read_register(0x33)
            except Uvk5Timeout:
                logger.debug("uvk5: reg 0x33 seed read timed out (attempt %d)", attempt + 1)
                time.sleep(_KEY_CONFIRM_SETTLE_S)
                continue
            break
        if value & _REG33_BASE == _REG33_BASE:
            return value
        logger.warning(
            "uvk5: reg 0x33 read back %#06x at connect, which is missing the pin output-enable "
            "bits %#06x the firmware always carries (bk4829.c:198). Repairing the base, so that "
            "asserting the receive rail actually drives a pin (ADR 0132).",
            value, _REG33_BASE,
        )
        return _REG33_BASE | (value & 0xFF)

    def _seed_frequency(self) -> int | None:
        """Seed the tuned frequency from the synthesiser, refusing a read-back that is not one.

        Registers 0x38/0x39 hold the frequency control word in 10 Hz units, and in dock mode the
        **host** owns them — `Dock_ForceTx` never calls `BK4819_SetFrequency` (`uart.c:778`), so
        whatever is in there is what the transmitter comes up on. This read is therefore not a
        display value; it is the number the next key-up radiates on.

        And it was adopted unconditionally, which is the one credulous seed left in this class. A
        register file answering ``0`` produced a model saying *the frequency is 0 Hz* — a different
        statement from *I do not know where this radio is*, and the only one of the two that gets
        past `_key_on`'s ``self._frequency is None`` guard. Measured: a fresh radio reported 0 and
        ``POST /ptt`` returned **200** (ADR 0173 finding 2, fixed here as ADR 0174).

        So refuse it, and say ``None``. Not because 0 is a special case — because a value the radio
        cannot be on is not a reading, and this backend already says so three other ways:
        `_seed_reg30` repairs a word the radio cannot be receiving on, the split is never seeded at
        all because "believe whatever you find" is a named fault class (ADR 0132/0133), and `_pa`
        reports no reading as no reading (ADR 0134). `base.py` had already written down the rule
        this one was missing — *0 Hz is not a frequency*.

        `_validate_frequency` rather than a comparison with zero, so there is exactly one
        definition of "a frequency this radio can be on". Its raster half cannot fail here (the
        registers *are* in 10 Hz units, so a read is always on the raster); the range half is the
        one with teeth, and it catches a garbled read-back as well as a bare 0.

        **Not a raise.** A radio whose synthesiser reads back nonsense is usually about to be told
        where to go — `uvk5.frequency` is REQUIRED config, so the server's `set_frequency` lands a
        few lines below this — and failing construction would turn a repairable state into no radio
        at all. Refusing to *transmit* until then is the whole of the safety claim.
        """
        lo = self._read_register(0x38)
        hi = self._read_register(0x39)
        hz = ((hi << 16) | lo) * FREQ_STEP_HZ
        try:
            self._validate_frequency(hz, "the frequency read back from the synthesiser")
        except ValueError as exc:
            logger.warning(
                "uvk5: %s (reg 0x38=%#06x, 0x39=%#06x). The host does not know where this radio "
                "is, so it will refuse to key until something tunes it (ADR 0174).",
                exc, lo, hi,
            )
            return None
        return hz

    def _enter_hw_mode_verified(self) -> None:
        """Enter full-control (0x0870) and confirm the F3 RX audio force-open ran, re-sending a 0x0870
        that was lost in the reset-on-open boot race — the first-start dead-RX fix (ADR 0122).

        EnterHwMode has no reply, so it cannot be a ``request()``; the only host-visible confirmation
        is REG_47 reaching FM/unmute, which the firmware's ``Dock_ForceRxAudioAlive`` sets in the same
        routine that raises the un-dockable audio-amp gate GPIOA8 (ADR 0120). No-op on a healthy F3
        start (REG_47 is FM after the first send). Bounded: a pre-F3 dock never sets REG_47, so this
        exits after the retries with a warning — it never hangs and never falsely 'fixes' a non-F3 radio.
        """
        reg47 = 0
        for _attempt in range(_ENTER_HW_MODE_RETRIES):
            self._transport.send(EnterHwMode())
            if self._enter_settle_s:
                time.sleep(self._enter_settle_s)
            try:
                reg47 = self._read_register(0x47)
            except (Uvk5Timeout, Uvk5Closed):
                reg47 = 0
            if reg47 & _REG47_AF_FM_BIT:
                return
        logger.warning(
            "uvk5: RX audio path did not confirm open — REG_47=%#06x never reached FM/unmute after %d "
            "EnterHwMode attempts. The F3 firmware force-open may not have run (is this the F3 build?); "
            "RX may be dead until restart (ADR 0120/0122).",
            reg47, _ENTER_HW_MODE_RETRIES,
        )

    def _tone_pairs(self) -> list[tuple[int, int]]:
        """The CTCSS register writes for the current tone (GoTransmit, BK4819.cs:620-647)."""
        if self._tone is None:
            return [(0x51, 0)]
        code = ((round(self._tone * 10) * 206488) + 50000) // 100000
        return [(0x51, 0x904A), (0x07, code)]

    # --- CAT tuning -----------------------------------------------------------

    def _lna_bit_for(self, hz: int) -> int:
        """The reg-0x33 LNA-path bit for ``hz``, on the firmware's own 280 MHz split."""
        return _REG33_VHF_LNA if hz // FREQ_STEP_HZ < _BAND_SPLIT_10HZ else _REG33_UHF_LNA

    def _rx_reg33(self) -> int:
        """The tracked GPIO byte in its *receiving* shape: RX rail up, PA rail down.

        Computed rather than trusted. `self._reg33` is seeded from whatever the radio happened to be
        doing at connect, and the firmware moves both rails behind our back on every key cycle; a
        model that merely remembers the seed would hand the radio back a receiver that is off.
        """
        return (self._reg33 | _REG33_BASE | _REG33_RX_ENABLE) & ~_REG33_PA_ENABLE

    @staticmethod
    def _freq_pairs(hz: int) -> list[tuple[int, int]]:
        """The 0x38/0x39 synthesiser writes for ``hz`` (10 Hz units, low word then high)."""
        freq10 = hz // FREQ_STEP_HZ
        return [(0x38, freq10 & 0xFFFF), (0x39, (freq10 >> 16) & 0xFFFF)]

    def _tx_tune_pairs(self) -> list[tuple[int, int]]:
        """The TX-leg tune for the key-up batch — empty unless a split is armed.

        Empty on simplex is not an optimisation: it keeps the simplex key-up batch byte-for-byte
        what it has always been, so every register-sequence assertion in the suite continues to
        pin the path that hardware actually runs most of the time.
        """
        if self._tx_frequency is None:
            return []
        return self._freq_pairs(self._tx_frequency)

    def _tx_band_hz(self) -> int | None:
        """The frequency the transmitter will actually be on — the split's TX leg, or the tuned one.

        Everything band-selected about *keying* (PA gain byte, LNA path) has to follow this, not
        `self._frequency`: under a repeater split the receiver and the transmitter are on different
        frequencies and, in principle, different bands (ADR 0133).
        """
        return self._tx_frequency if self._tx_frequency is not None else self._frequency

    def _tx_reg33(self) -> int:
        """The same byte in its *keyed* shape: PA rail up, RX rail down, band bit for the TX leg.

        `self._reg33` tracks the *receive* band (only `set_frequency` writes it) and keying never
        mutates it. So the band bit is substituted here rather than remembered: on a split the LNA
        path has to follow the frequency being transmitted on. Identical to the tracked value
        whenever both legs are in the same band, which is every split this backend accepts today.
        """
        tx_hz = self._tx_band_hz()
        band = self._reg33 if tx_hz is None else (
            (self._reg33 & ~_REG33_LNA_BITS) | self._lna_bit_for(tx_hz)
        )
        return (band | _REG33_BASE | _REG33_PA_ENABLE) & ~_REG33_RX_ENABLE

    def _pa_gain_for(self, hz: int) -> int:
        """The reg-0x36 PA gain byte for ``hz`` (`bk4829.c:743`), same split as the LNA path."""
        return _PA_GAIN_VHF if hz // FREQ_STEP_HZ < _BAND_SPLIT_10HZ else _PA_GAIN_UHF

    def _validate_frequency(self, hz: int, what: str) -> None:
        """Range + raster check, shared by `set_frequency` and `set_split`. Fails loud, never rounds."""
        if not self._freq_min_hz <= hz <= self._freq_max_hz:
            raise ValueError(
                f"{what} {hz} Hz is out of band [{self._freq_min_hz}, {self._freq_max_hz}]"
            )
        if hz % FREQ_STEP_HZ != 0:
            raise ValueError(
                f"{what} {hz} Hz is not a multiple of the {FREQ_STEP_HZ} Hz tuning step"
            )

    def set_frequency(self, hz: int) -> None:
        """Tune to ``hz`` (does NOT key). Fails loud out of band or off the 10 Hz raster.

        **Clears any armed split** (ADR 0133): after this the radio is simplex until `set_split`
        re-arms it. Deliberately the fail-safe direction — a TX leg surviving a retune would let the
        next unattended transmission (a station ID on a timer) key a repeater's uplink from a
        frequency nobody chose.
        """
        self._validate_frequency(hz, "frequency")
        if self._keyed:
            # The write list below ends with the RX reg-0x30 word, which would un-key mid-over, and
            # rewrites 0x33 from a model that does not know the firmware has the PA rail up. Retuning
            # while transmitting is a caller bug; say so rather than silently dropping the carrier.
            raise Uvk5KeyingError("cannot retune while keyed — un-key first")
        self._reg33 = (self._reg33 & ~_REG33_LNA_BITS) | self._lna_bit_for(hz)
        self._write_registers(
            [
                *self._freq_pairs(hz),
                # The receiving shape, not the remembered one: a tune must leave the radio able to
                # hear. Seeded from whatever it was doing at connect, `_reg33` can arrive with
                # RX_ENABLE clear and the PA rail up — the reg-0x30 version of that bug left this
                # station deaf for a whole service lifetime (see `_seed_reg30`).
                (0x33, self._rx_reg33()),
                (0x30, 0),
                (0x30, self._reg30),
            ]
        )
        # Both together, and only after the write: a failed write must not leave the model claiming
        # a TX leg for a frequency the radio was never on.
        if self._tx_frequency is not None:
            logger.info(
                "uvk5: tuning to %d Hz clears the armed split (was transmitting on %d Hz)",
                hz, self._tx_frequency,
            )
        self._frequency, self._tx_frequency = hz, None

    def set_split(self, tx_hz: int | None) -> None:
        """Transmit on ``tx_hz`` while receiving on the tuned frequency; ``None`` restores simplex.

        Does NOT key and writes no registers — the TX leg is applied inside the key path, tuned
        before the transmitter is enabled and returned to the RX leg after the PA drops (ADR 0133).
        Storing it is the whole of the work here; `_key_on` / `_key_off` do the rest.

        ``tx_hz`` is validated harder than `set_frequency`'s argument, because this is the number
        that radiates. `set_frequency` accepts 18 MHz-1.3 GHz on purpose: a wrong *receive*
        frequency is harmless. A wrong transmit frequency is not, and the arithmetic that produces
        one (rx ∓ offset, from an imported channel list) is exactly where a stray digit hides.
        """
        if self._keyed:
            raise Uvk5KeyingError("cannot change the split while keyed — un-key first")
        if tx_hz is None:
            self._tx_frequency = None
            return
        if self._frequency is None:
            # Live for the same reason `_key_on`'s twin is (ADR 0174). It matters more here than it
            # looks: the offset below is arithmetic against the receive frequency, so an unknown
            # one does not merely skip a check — it makes every distance it computes meaningless.
            raise RadioNotReady("cannot arm a split before a frequency is set")
        self._validate_frequency(tx_hz, "transmit frequency")
        offset = abs(tx_hz - self._frequency)
        if offset > _MAX_SPLIT_OFFSET_HZ:
            raise ValueError(
                f"transmit frequency {tx_hz} Hz is {offset / 1e6:.3f} MHz from the receive "
                f"frequency {self._frequency} Hz — further than the "
                f"{_MAX_SPLIT_OFFSET_HZ / 1e6:.0f} MHz limit for a repeater offset. Every standard "
                f"2 m / 1.25 m / 70 cm offset is well inside it; a value this far out is a typo."
            )
        rx_vhf = self._frequency // FREQ_STEP_HZ < _BAND_SPLIT_10HZ
        tx_vhf = tx_hz // FREQ_STEP_HZ < _BAND_SPLIT_10HZ
        if rx_vhf != tx_vhf:
            # `_correct_tx_band` would have to flip the reg-0x36 gain byte and the reg-0x33 LNA path
            # within one key cycle. There is no bench evidence for that path, and ADR 0132 is a long
            # account of what happens when those two registers disagree with the carrier.
            raise ValueError(
                f"crossband split ({self._frequency} Hz RX / {tx_hz} Hz TX) is not supported — "
                f"both legs must be on the same side of the {_BAND_SPLIT_10HZ * FREQ_STEP_HZ} Hz "
                f"band split"
            )
        self._tx_frequency = tx_hz

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
            # Reachable since ADR 0174, and it took a change one layer up to get here: this guard
            # asks "do I know where the radio is", and the seed used to answer with whatever the
            # synthesiser read back — so a register file answering 0 walked straight past it and
            # keyed on DC. `_seed_frequency` now says None when the read is not a frequency, which
            # is what turns this line from documentation into a refusal.
            raise RadioNotReady("cannot key before a frequency is set")
        # This over's PA reading does not exist yet. Clearing it here — not at the end — is what
        # makes `pa` mean "the last over", because every path out of a key-up that fails before
        # `_correct_tx_band` (a refused device open, a key-up that never confirms, the pacer's
        # on_error unkey) would otherwise leave the PREVIOUS over's numbers on `/status`, describing
        # a transmission this one never made. Same rule as the failed 0x36 read and as `rssi`: no
        # reading is reported as no reading (ADR 0134).
        self._pa = None
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
        try:
            # No reg-0x36 (PA bias) write here on purpose — the dock firmware sets the PA up from
            # the radio's own calibration when it sees the 0x30 TX edge, and would overwrite ours.
            # See DEFAULT_SQUELCH_MODE's neighbour note above and ADR 0128 for the measurement.
            # Re-send the whole key-up, not just the read-back, if it does not confirm. Dock
            # writes are fire-and-forget over a serial link the firmware stops servicing while it
            # is inside `Dock_ForceTx`, so the *write* can be the thing that is lost — and then no
            # amount of re-reading will ever see TX. A read-back of the RX word (`0xBFF1`) is
            # exactly that case, and it still failed a live over after five reads. The writes are
            # idempotent and every attempt is still confirmed, so this recovers a dropped frame
            # without weakening the ADR 0112 invariant: exhaust the attempts and it fails loud.
            confirmed = 0
            for attempt in range(_KEY_ATTEMPTS):
                self._write_registers(
                    [
                        # THE TX LEG GOES FIRST, AND IT MUST STAY IN THIS BATCH (ADR 0133).
                        #
                        # One `_write_registers` call is one `WriteRegisters` frame, and the
                        # firmware validates CRC over the whole body — a corrupt frame is dropped
                        # *entire*, never in part (the measured failure on this bench is a whole
                        # lost frame; see `scripts/bench/uvk5_tx_regs.py`). So the synthesiser write
                        # and the TX enable below cannot land independently: there is no state in
                        # which the transmitter comes up while 0x38/0x39 still hold the RECEIVE
                        # frequency. Split this list into two `_write_registers` calls and that
                        # guarantee is gone — the radio would key on the repeater's output.
                        #
                        # Empty on simplex, so the simplex batch is unchanged byte for byte.
                        *self._tx_tune_pairs(),
                        (0x50, 0x3B20),  # FM AF/TX path, un-muted (GoTransmit, BK4819.cs:589)
                        *self._tone_pairs(),  # CTCSS (GoTransmit, BK4819.cs:620-647)
                        (0x30, 0),
                        (0x30, _REG30_TX_ENABLED),  # TX enable (GoTransmit, BK4819.cs:591-592)
                    ]
                )
                confirmed = self._confirm_keyed()
                if confirmed == _REG30_TX_ENABLED:
                    break
                logger.debug(
                    "uvk5: key-up attempt %d did not confirm (reg 0x30=%#06x); re-sending",
                    attempt + 1, confirmed,
                )
            if confirmed != _REG30_TX_ENABLED:
                raise Uvk5KeyingError(
                    f"radio did not report TX enabled (reg 0x30={confirmed:#06x}, want "
                    f"{_REG30_TX_ENABLED:#06x}) after {_KEY_ATTEMPTS} key-up attempts"
                )
            # The transmitter is up; now point it at OUR band. `_confirm_keyed` has already waited
            # out the firmware's PA sequence, so this lands after it (ADR 0132).
            self._correct_tx_band()
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

    def _correct_tx_band(self) -> None:
        """Re-point the band-selected TX registers at the frequency the HOST tuned (ADR 0132).

        `Dock_ForceTx` sets the PA up from `gCurrentVfo->pTX->Frequency` — the radio's own VFO —
        and deliberately never tunes the synthesiser, which the host owns via reg 0x38/0x39
        (`uart.c:729-732`). On a host frequency in the other band from the radio's VFO the two
        disagree, and the firmware wins on everything except the frequency itself. Measured keyed
        on 147.555 with the radio's VFO on UHF: reg 0x36 gain byte `0xA2` (UHF) and reg 0x33 with
        the UHF LNA bit, on a carrier that really was at 147.555.

        Two things get corrected, and one deliberately does not:

        * **reg 0x36 gain byte** — recomputed for the tuned frequency, keeping the firmware's
          **bias** byte exactly as read. The bias comes from per-band calibration in the radio's
          SPI flash that the dock cannot read; inventing one is the mistake ADR 0128 removed, and
          re-introducing it here would repeat it. Correct the half the host can actually compute.
        * **reg 0x33** — the keyed shape (RX_ENABLE clear, PA_ENABLE set) with the LNA bit for the
          tuned frequency, from the tracked model so the firmware's other bits survive.

        Non-raising by design. A key-up that reached `_REG30_TX_ENABLED` is already confirmed on
        air; if this refinement cannot be applied the radio falls back to the firmware's own
        (possibly wrong-band) setup, which is where it was before this existed. Failing the over
        here would trade a weaker signal for dead air — the wrong direction under ADR 0112.
        """
        tx_hz = self._tx_band_hz()
        if tx_hz is None:  # unreachable: `_key_on` checks first
            return
        try:
            reg36 = self._read_register(0x36)
        except (Uvk5Timeout, Uvk5Closed) as exc:
            # "No reading", never a stale one: a value left over from the previous over would
            # describe a PA setup this transmission never had (ADR 0134, and the rssi rule).
            self._pa = None
            logger.warning(
                "uvk5: could not read the PA setup back (%s) — leaving the firmware's band setup "
                "in place. On a frequency outside the radio's own VFO band this transmits with the "
                "wrong PA gain (ADR 0132).", exc,
            )
            return
        bias = reg36 >> 8
        # The TX leg, not the tuned one: under a split those differ, and it is the transmitter's
        # frequency that decides the PA gain byte and the LNA path (ADR 0133).
        want36 = (bias << 8) | _REG36_ENABLE | self._pa_gain_for(tx_hz)
        keyed33 = self._tx_reg33()
        matched = reg36 == want36
        if matched:
            # Same band as the radio's VFO — the firmware already got it right. Still write 0x33:
            # it is what holds the PA rail up, and re-asserting the model's value is idempotent.
            self._write_registers([(0x33, keyed33)])
        else:
            # WARN, not INFO: the gain byte is corrected but the BIAS is not, and cannot be — it is
            # the other band's calibration out of a flash the dock cannot read (ADR 0128). So this
            # over goes out at a level nobody has characterised, which is indistinguishable from
            # working until something far away fails to hear it. That is the ADR 0134 field symptom.
            logger.warning(
                "uvk5: transmitting on %d Hz%s but the radio's own VFO set the PA up for the other "
                "band (reg 0x36=%#06x, gain %#04x); correcting the gain byte to %#04x and keeping "
                "bias %d, which is the WRONG band's calibration — radiated power is not "
                "characterised. Put the radio's VFO on the band you are transmitting in.",
                tx_hz,
                "" if self._tx_frequency is None else f" (split; receiving on {self._frequency})",
                reg36, reg36 & 0xFF, want36 & 0xFF, bias,
            )
            self._write_registers([(0x33, keyed33), (0x36, want36)])
        # Recorded after the write is issued, which is NOT the same as landed: dock writes are
        # fire-and-forget (`transport.send`, no reply), so a lost frame leaves the PA on the
        # firmware's byte while this reports the corrected one. `bias` and `band_matched` are read
        # back from the radio and are solid; `gain` is what was asked for. That asymmetry is why
        # `band_matched` is the field the UI leads with — it is the measured half (ADR 0134).
        self._pa = PaState(bias=bias, gain=want36 & 0xFF, band_matched=matched, tx_frequency=tx_hz)

    def _confirm_keyed(self) -> int:
        """Read reg 0x30 back until it reports TX, or the attempts run out. Returns the last value.

        The read-back is the RF-safety invariant from ADR 0112 — a silent no-key must never become
        dead air — but a *single* read races the radio's own firmware. On the F5 dock build the
        0x30 TX edge triggers `Dock_ForceTx`, whose `BK4819_PrepareTransmit()` writes `REG_30 = 0`
        part-way through its own PA sequence and spends ~25 ms in `SYSTEM_DelayMs` settle points
        (ADR 0128). Read into that window and the radio truthfully answers `0xBFF1` — the RX word —
        for a transmitter that is in fact coming up.

        That is the "first-key settle flake" F4 saw and ADR 0126 carried forward. It is not
        theoretical: it failed a live service announcement with an HTTP 500 and no audio on air
        during the ADR 0129 acceptance runs. Retrying is bounded and does not weaken the
        invariant — a radio that never confirms still fails loud, and `_key_on` still unwinds.
        """
        # Settle BEFORE the first read, not after a failed one. The dock's full-control loop is
        # single-threaded and blocking: while it is inside `Dock_ForceTx` it is not servicing its
        # UART, so a read request sent into that window is not merely answered late — it is
        # *dropped*, and the transport burns its full 2 s timeout waiting for a reply that will
        # never come. That is the `Uvk5Timeout: no matching reply within 2.0s` seen on the bench.
        # Waiting out the firmware's PA sequence first avoids the race rather than polling into it.
        time.sleep(_KEY_SETTLE_S)
        confirmed = 0
        for attempt in range(_KEY_CONFIRM_ATTEMPTS):
            if attempt:
                time.sleep(_KEY_CONFIRM_SETTLE_S)
            try:
                confirmed = self._read_register(0x30)
            except Uvk5Timeout:
                # A dropped request, not a dead link — the write itself is fire-and-forget and may
                # well have taken. Retry within the budget; a genuinely dead dock exhausts it and
                # the caller still fails loud.
                logger.debug("uvk5: key-up read-back timed out (attempt %d), retrying", attempt + 1)
                continue
            if confirmed == _REG30_TX_ENABLED:
                return confirmed
        return confirmed

    def _key_off(self) -> None:
        """Restore RX first (RF-safe), then stop the pacer and tear down the playout stream.

        Best-effort and non-raising, mirroring the baofeng inversion (ADR 0093): the transmitter is
        unkeyed (RX registers restored) before the audio teardown, and a transport error while
        unkeying is logged rather than propagated — it must not mask the teardown nor break
        :meth:`close` / the one-shot ``finally``.

        **The receive band path is restored here too (ADR 0132).** The firmware's `Dock_EndTx`
        drops the PA rail and re-asserts RX_ENABLE (`uart.c:740-746`) but leaves the LNA pointing
        wherever `Dock_ForceTx` put it — at the RADIO's VFO band, not ours. Nothing else ever
        rewrites reg 0x33, so on a host frequency in the other band the receiver came back deaf
        after the very first transmission and stayed that way until the next `set_frequency`.
        Measured on 147.555: reg 0x33 `0x9046` (VHF LNA) before keying, `0x9048` (UHF LNA) after.
        The write goes *after* the un-key so it lands after `Dock_EndTx`, same ordering argument as
        `_correct_tx_band`.

        **And the receive FREQUENCY, when a split was armed** — `_restore_rx_frequency`, as a second
        frame, for the same ordering reason and one more: the PA has to be down before the
        synthesiser moves, or the tail of the over lands on the repeater's output (ADR 0133). On
        simplex it writes nothing, so the un-key below is byte-for-byte what it has always been.
        """
        try:
            self._write_registers(
                [(0x30, 0), (0x30, self._reg30), (0x33, self._rx_reg33())]
            )
        except Exception:
            logger.exception("uvk5: error restoring RX on key-off")
        self._restore_rx_frequency()
        self._keyed = False
        pacer, self._pacer = self._pacer, None
        if pacer is not None:
            # The pacer clears its queue rather than draining it — correct RF behaviour (ADR 0093:
            # no long FM tail after the carrier drops) and, until now, completely silent. A caller
            # that keys, hands audio to the STREAMING `transmit()` (which enqueues and returns), and
            # then un-keys transmits nothing at all, and nothing anywhere says so. That shape is
            # what a bench script naturally reaches for, and one of them produced a session's worth
            # of confident conclusions from transmissions that never happened (ADR 0133).
            discarded = pacer.stop()
            if discarded:
                seconds = discarded / (CANONICAL_FORMAT.rate * CANONICAL_FORMAT.frame_bytes)
                logger.warning(
                    "uvk5: un-keyed with %d PCM bytes (%.2f s) still queued — discarded, never "
                    "transmitted. A streaming transmit() only enqueues; un-keying drops whatever "
                    "has not played. Use the one-shot transmit() (no ptt(True) first), which blocks "
                    "until the audio has drained.",
                    discarded, seconds,
                )
        stream, self._playback = self._playback, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

    def _restore_rx_frequency(self) -> None:
        """After a split over, put the synthesiser back on the receive leg. No-op on simplex.

        A **second frame**, deliberately, and deliberately this exact write list — it is byte for
        byte `set_frequency`'s batch, the one frequency-write shape with a citation (ADR 0112, from
        the pinned client's `BK4819.cs`, which never writes 0x38/0x39 without the clear-and-restore
        pair following it). The tempting single-frame version would fold these registers into the
        un-key above and split that re-latch around the frequency write — a shape nothing in this
        repo establishes works.

        Two properties fall out of keeping it separate (ADR 0133). The simplex un-key stays exactly
        what it was, so nothing about the common path can be perturbed by this method. And the
        retune lands strictly *after* `Dock_EndTx` has run, so the window where the PA is still
        ramping down while the synthesiser has already moved to the repeater's output is zero —
        whatever `Dock_EndTx` costs, which nothing here measures.

        Non-raising: key-off must never raise. But never silent either — a dropped tune is invisible
        (measured: the API returns 200, `/status` reports the requested frequency, and the radio
        sits wherever it was), and under a split "wherever it was" is the repeater's *input*.
        """
        if self._tx_frequency is None or self._frequency is None:
            return
        rx_hz = self._frequency
        pairs = [
            *self._freq_pairs(rx_hz),
            (0x33, self._rx_reg33()),
            (0x30, 0),
            (0x30, self._reg30),
        ]
        for attempt in range(2):
            try:
                self._write_registers(pairs)
            except Exception:
                logger.exception("uvk5: error retuning to the receive frequency after a split over")
                return
            try:
                back = (self._read_register(0x39) << 16) | self._read_register(0x38)
            except (Uvk5Timeout, Uvk5Closed) as exc:
                logger.warning(
                    "uvk5: could not read the receive frequency back after a split over (%s) — the "
                    "radio may still be on the transmit leg (%d Hz) and therefore deaf.",
                    exc, self._tx_frequency,
                )
                return
            if back * FREQ_STEP_HZ == rx_hz:
                return
            logger.warning(
                "uvk5: retune to the receive frequency did not take — reg 0x38/0x39 read back "
                "%d Hz, want %d Hz (attempt %d). The radio is still on the transmit leg.",
                back * FREQ_STEP_HZ, rx_hz, attempt + 1,
            )
        logger.error(
            "uvk5: the radio is left on the transmit leg after a split over — receiving on %d Hz "
            "was requested but the synthesiser did not move. Receive is dead until the next tune.",
            rx_hz,
        )

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
            self._open_capture()
        # An overrun is only a fault if somebody was actually reading. Rather than enumerate the
        # ways reading legitimately stops — a keyed over (half-duplex blinds RX by design, ADR
        # 0017), the demand-driven pump halting when the last listener leaves, a freshly opened
        # stream, a service restart — just notice the gap: the card fills its ring on wall-clock
        # time, so a long time since the last read *guarantees* a backlog that is nobody's fault.
        # This subsumes every case with one rule and no cross-layer plumbing.
        now = time.monotonic()
        gap = now - self._last_read_at
        if gap > _XRUN_READ_GAP_S:
            self._expect_xrun = _XRUN_DRAIN_READS
        self._last_read_at = now
        data, overflowed = self._capture.read(self._blocksize)
        # An xrun (overflow) is not fatal — the samples we did get are still valid audio, so the
        # frame is returned unchanged. But it means the reader fell behind the capture ring and audio
        # was dropped, so make it VISIBLE (ADR 0125): this flag was discarded before, which is why
        # the RX-pump CAT-gate starvation (14 % duty, ring overrunning continuously) logged nothing.
        # Rate-limited so a sustained overrun does not spam the journal at the frame rate.
        if not overflowed:
            # A clean read means the reader has caught up with the ring: any backlog from a known
            # gap is drained, so stop excusing overruns.
            self._expect_xrun = 0
        elif self._expect_xrun > 0:
            # Expected: the ring kept filling through a stretch where nobody was reading it — a
            # keyed over (half-duplex blinds the receiver by design, ADR 0017) or a freshly opened
            # stream. Reporting these as faults made the warning count a *transmission* counter.
            # A deep backlog takes several reads to drain, so the excuse spans reads rather than
            # one — but it is bounded, so a stall that begins during a gap is still reported.
            self._expect_xrun -= 1
            logger.debug("uvk5: expected ALSA capture overrun while draining a known read gap")
        elif gap <= self._block_period_s() * _XRUN_ON_CADENCE_BLOCKS:
            # The card flagged an overrun while the reader was **on cadence** — this read came one
            # block period after the last one, so the reader did not fall behind anything. ADR 0130
            # chased this residue and established what it is: the card's own recovery event, with
            # no audio cost (measured in the same runs at 100.4-100.8 % duty across the active
            # span, the whole over received, tone recovered 1.000).
            #
            # It is logged separately because the warning below *asserts* something false about it
            # — "the reader fell behind the card and audio was dropped" — and a monitoring signal
            # that cries stall when there is no stall trains everyone to ignore it. It is also what
            # the acceptance runner counts, so the false claim became a false verdict: a run with
            # every direct audio measure perfect failed on this proxy alone.
            logger.info(
                "uvk5: ALSA capture overrun (xrun) reported %.3f s after the previous read — one "
                "block period, i.e. the reader is on cadence and did not stall. The card recovered "
                "on its own; no read gap, nothing dropped on our side (ADR 0130/0132).",
                gap,
            )
        else:
            now = time.monotonic()
            if now - self._last_xrun_warn >= _XRUN_WARN_INTERVAL_S:
                self._last_xrun_warn = now
                logger.warning(
                    "uvk5: ALSA capture overrun (xrun) after %.3f s since the previous read "
                    "(%d drain reads left) — the RX reader fell behind the card and audio was "
                    "dropped. Sustained overruns mean the single capture reader is stalling "
                    "(e.g. a blocking call on the reader thread); see ADR 0125.",
                    gap,
                    self._expect_xrun,
                )
        return AudioFrame(bytes(data), CANONICAL_FORMAT)

    def _block_period_s(self) -> float:
        """How long one capture block takes at the canonical rate — the reader's natural cadence."""
        return self._blocksize / CANONICAL_FORMAT.rate

    def _open_capture(self) -> None:
        """Open the AIOC capture stream. With ``capture_reopen_on_floor`` (default OFF) this also
        verifies the first block is not floor and reopens ONCE if it is — the host-audio leg of the
        first-start dead-RX fix, for a stream opened against a still-USB-settling device (ADR 0122).
        Default OFF keeps :meth:`receive` byte-identical (no probe read)."""
        self._capture = open_capture_stream(
            self._sd(), device=self._input_device, blocksize=self._blocksize
        )
        if not self._capture_reopen_on_floor or self._capture_reopened:
            return
        self._capture_reopened = True  # probe-and-reopen at most once per backend lifetime
        data, _overflowed = self._capture.read(self._blocksize)
        if _block_rms(bytes(data)) >= _CAPTURE_FLOOR_RMS:
            return  # healthy first block (post-F3 the receiver hisses continuously) — keep the stream
        with contextlib.suppress(Exception):  # floor: device was still settling — reopen once
            self._capture.stop()
            self._capture.close()
        time.sleep(_CAPTURE_SETTLE_S)
        self._capture = open_capture_stream(
            self._sd(), device=self._input_device, blocksize=self._blocksize
        )

    # --- status ---------------------------------------------------------------

    def status(self) -> RadioStatus:
        busy = False
        rssi: int | None = None
        if not self._keyed:
            try:
                rssi = self._read_register(0x67) & 0x1FF
                busy = rssi >= self._squelch_threshold
                self._last_busy = busy
                self._busy_stale = 0
            except (Uvk5Timeout, Uvk5Closed):
                # A dropped RSSI read is *missing information*, not evidence of a clear channel —
                # and this value drives the CAT squelch gate (ADR 0121), so answering "not busy"
                # slams the gate shut on an over that is actually in progress. The dock's
                # full-control loop is single-threaded, so a poll landing while it is busy
                # elsewhere is simply lost, and the transport waits out its whole timeout: on the
                # bench that chopped ~1.8 s off the head of a received transmission (4.19 s of a
                # 6.0 s over) with no other symptom. Hold the last known state instead, bounded so
                # a genuinely dead link cannot latch the gate open for ever.
                if self._busy_stale < _BUSY_HOLD_READS:
                    self._busy_stale += 1
                    busy = self._last_busy
                else:
                    busy = False
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._keyed,
            busy=busy,
            frequency=self._frequency,
            tx_frequency=self._tx_frequency,
            channel=None,
            tone=self._tone,
            mode=self._mode,
            # None while keyed or on a dropped read — "no reading", not "zero signal" (ADR 0131).
            rssi=rssi,
            # What the PA was set to at the last key-up. Deliberately NOT cleared on un-key.
            pa=self._pa,
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
