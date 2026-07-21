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

Scope this cycle: control (tune/tone/mode) + register-keying + status. **Audio — the AIOC
sound-card path — is out of scope**: :meth:`transmit` and :meth:`receive` raise, pending the
audio cycle. Nothing wires this backend into the factory/config yet, so raising is safe.

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
import logging

from ..base import (
    Capability,
    RadioStatus,
    SHARED_CAPS,
    UnsupportedCapability,
)
from ...audio import AudioFrame
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

_UVK5_CAPS: frozenset[Capability] = SHARED_CAPS | frozenset(
    {Capability.SET_FREQUENCY, Capability.SET_TONE, Capability.SET_MODE, Capability.SCAN}
)

_AUDIO_DEFERRED = (
    "UV-K5 audio rides the AIOC sound card, wired in a later cycle; this backend provides "
    "control + register keying only. Use ptt() to key."
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
        squelch_threshold: int = DEFAULT_SQUELCH_THRESHOLD,
        tx_power_pct: float = DEFAULT_TX_POWER_PCT,
        freq_min_hz: int = DEFAULT_FREQ_MIN_HZ,
        freq_max_hz: int = DEFAULT_FREQ_MAX_HZ,
        _transport: Uvk5Transport | None = None,
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

    # --- keying ---------------------------------------------------------------

    def ptt(self, on: bool) -> None:
        if on:
            self._key_on()
        else:
            self._key_off()

    def _key_on(self) -> None:
        """Write the TX-enable register sequence and CONFIRM it, else restore RX and raise."""
        if self._keyed:
            return
        if self._frequency is None:
            raise Uvk5KeyingError("cannot key before a frequency is set")
        freq10 = self._frequency // FREQ_STEP_HZ
        drive = max(0, min(255, int(self._tx_power_pct * 2.55))) << 8
        pa = (0x88 if freq10 < _BAND_SPLIT_10HZ else 0xA2) | drive
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
            # Fail-safe: restore RX before surfacing the no-key, so we never strand a half-key.
            try:
                self._key_off()
            except Exception:
                logger.exception("uvk5: failed to restore RX after a no-key")
            raise Uvk5KeyingError(
                f"radio did not report TX enabled (reg 0x30={confirmed:#06x}, want "
                f"{_REG30_TX_ENABLED:#06x})"
            )
        self._keyed = True

    def _key_off(self) -> None:
        """Restore RX — unconditional and RF-safe (TransmitEnd, BK4819.cs:411)."""
        self._write_registers([(0x30, 0), (0x30, self._reg30)])
        self._keyed = False

    def transmit(self, audio: AudioFrame) -> None:
        raise NotImplementedError(_AUDIO_DEFERRED)

    def receive(self) -> AudioFrame:
        raise NotImplementedError(_AUDIO_DEFERRED)

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
        try:
            self._transport.send(ExitHwMode())  # return the radio to standalone operation
        except Exception:
            logger.exception("uvk5: error leaving full-control mode on close")
        try:
            self._transport.close()
        except Exception:
            logger.exception("uvk5: error closing transport")
        atexit.unregister(self.close)
