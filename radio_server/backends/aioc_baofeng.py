"""AiocBaofeng — audio-only UV-5R backend (audio/PTT via NA6D AIOC cable, no CAT).

The real hardware backend (ADR 0029). Audio in/out is the AIOC's USB sound card
(``sounddevice``/ALSA); PTT is the AIOC's serial control line (``pyserial``). There is no CAT:
frequency is set by hand on the radio, so this backend advertises only :data:`SHARED_CAPS` and
the API rejects tuning operations (guardrail 3).

Keying discipline (guardrail 2): PTT is a serial control line (DTR by default; DTR-vs-RTS is the
one empirical fact — verify on hardware, guardrail 1), NEVER a CAT ``TX`` command. Two keying
shapes share one backend:

  * **One-shot** (station ID, service TTS, REST ``/transmit``): a single ``transmit(clip)`` self-keys
    — assert the line, play the whole clip, drain, drop the line. The caller never touches ``ptt()``.
  * **Streaming** (``TxSession`` / ``/audio/tx``): an explicit ``ptt(True)`` holds the line across
    many ``transmit(frame)`` calls, then ``ptt(False)`` drops it. While the line is held, ``transmit``
    only queues the frame for the keying's pacer thread (ADR 0102) — non-blocking, and it must not
    drop the key between frames. All blocking device writes happen on the pacer thread, never on the
    caller: a blocking write on the asyncio event loop was what starved the D-STAR decode pipeline
    into clicking and froze the on-loop watchdogs.

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
import contextlib
import logging
import time
from contextlib import contextmanager
from enum import StrEnum

from ..audio import CANONICAL_FORMAT, AudioFormatMismatch, AudioFrame
from .base import (
    SHARED_CAPS,
    Capability,
    RadioStatus,
    RadioUnavailable,
    UnsupportedCapability,
    refuse_if_deafened,
)
from .uvk5.tuner import SERIAL_TX_LOCKOUT_S, TuneBusy, TuneError, Uvk5Tuner
from .uvk5.vfo import DEFAULT_POWER, PowerLevel, VfoImage
from .soundcard import (
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

logger = logging.getLogger(__name__)

#: The sound-card audio machinery — capture/playout streams, the TX pacer, and the lead-in / buffer
#: math — now lives in the shared :mod:`.soundcard` seam (ADR 0113), reused by the ``uvk5`` backend.
#: The device / block / lead / buffer ``DEFAULT_*`` constants are imported above and re-exported
#: from this module (unchanged names) so ``config.spec``, ``doctor``, and this backend's tests keep
#: importing them from ``aioc_baofeng``. Tests also import the pacer as ``_AiocTxPacer``:
_AiocTxPacer = SoundCardTxPacer


#: Dock UART rate. Only relevant when a UV-K5 tuner is attached; keying does not care about baud.
DOCK_BAUD = 38400


class TunerMode(StrEnum):
    """How (or whether) this backend may tune the radio on the other end of the AIOC.

    ``off`` is the default and the only safe assumption: a UV-5R has no UART on that jack, so the
    backend stays TX/RX-only exactly as it has been. The rest are opt-in per config, and are not
    auto-detected — ``0x0873`` is answered only by F6 firmware, and the stock dispatch drops an
    unknown opcode without a word, so "probe and see" would mean waiting out a timeout at every
    startup to learn something the operator already knows.
    """

    OFF = "off"
    #: ``0x0873`` — instant, no reboot, no flash wear. Needs F6 firmware. **Volatile**: the tune
    #: lives in the radio's RAM and does not survive it being switched off.
    SETVFO = "setvfo"
    #: Write the channel to EEPROM and soft-reset onto it. Works on stock firmware. ~14 s.
    EEPROM = "eeprom"
    #: Both: ``0x0873`` moves the RF now, the EEPROM write makes it survive, and no reboot is
    #: needed because the firmware has already loaded the channel. Needs F6. The one to use on it,
    #: and the only mode where the persistence half is a live switch (:data:`DEFAULT_TUNE_PERSIST`).
    HYBRID = "hybrid"


DEFAULT_TUNER_MODE = TunerMode.OFF

#: Boot value of the hybrid tuner's persistence switch (ADR 0145). **Off** — instant: the channel
#: change is audible *and* transmittable at once, because only EEPROM traffic arms the radio's
#: six-second lockout. The cost is that the radio forgets when it is switched off, which the
#: key-up re-assert (`AiocBaofeng._reassert_channel`) makes safe rather than merely tolerable.
#:
#: A boot value, not the setting: `set_tune_persist` moves it at runtime and does not write config.
DEFAULT_TUNE_PERSIST = False

#: The demodulator this backend **states to the radio** at construction (ADR 0155).
#:
#: FM because it is the only value this station can transmit in at all: built without
#: ``ENABLE_TX_WHEN_AM`` the firmware sets ``VFO_STATE_TX_DISABLE`` for anything else, and that is
#: the path the AIOC's PTT line drives. So of the two values the wire carries, exactly one is
#: fail-safe for a station whose ID is required controller behaviour rather than a feature
#: (guardrail 5).
#:
#: Deliberately **not** `presets.DEFAULT_MODULATION`, which is the same four characters for a
#: different reason and would change for a different reason. That one answers "what does a preset
#: naming no modulation mean?" — a question about the operator's channel list, which could
#: reasonably move (a later cycle could make an absent value mean "leave the station alone", the way
#: ``power`` already works). This one answers "what does this server assert when nobody has said
#: anything?" — a question about whether the transmitter works, which must not move with it. A
#: backend importing `presets` would also invert a dependency direction that file keeps on purpose.
BOOT_MODULATION = "FM"


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
#: Backend-declared RX activity-gate mode — the per-backend override of the global `audio.squelch`
#: (ADR 0121), resolved via `resolve_squelch_mode`. Plain string (the `SquelchMode` value); `spec.py`
#: wraps it in `SquelchMode(...)`, so the backend never imports the `activity` layer. **`audio`
#: because** the UV-5R has no hardware busy line (ADR 0015), so software VAD is the only gate that
#: works — `cat` would poll a line that never asserts and `off` never segments per-transmission.
DEFAULT_SQUELCH_MODE = "audio"

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


def read_line_state(handle, line: str) -> bool | None:
    """Read a control line's **actual** state back from the kernel, or ``None`` if it can't be.

    ``pyserial``'s ``.dtr`` / ``.rts`` are write-only properties: reading one returns the last value
    *we assigned*, not what the pin is doing. So a key-up that never reached the hardware — a driver
    that dropped it, a handle onto a device that went away — reads back as a perfect success, and
    "the server keyed and the radio ignored it" becomes indistinguishable from "the server never
    keyed at all". A whole day of bench measurements was ambiguous for exactly that reason (ADR 0140):
    every layer reported success because every layer only ever reported *its own intent*.

    ``TIOCMGET`` returns the real modem-control bits, including the outputs. It is Linux/POSIX and
    needs a real file descriptor, so it returns ``None`` rather than raising on a fake handle, a
    platform without it, or a closed port — an unavailable readback must never be mistaken for a
    line that is low.
    """
    if line not in ("dtr", "rts"):
        raise ValueError(f"line must be 'dtr' or 'rts', got {line!r}")
    try:
        import fcntl
        import struct
        import termios
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    try:
        fd = handle.fileno()
        packed = fcntl.ioctl(fd, termios.TIOCMGET, struct.pack("I", 0))
        bits = struct.unpack("I", packed)[0]
    except Exception:  # noqa: BLE001 - fake handles in tests, closed ports, unsupported drivers
        return None
    mask = termios.TIOCM_DTR if line == "dtr" else termios.TIOCM_RTS
    return bool(bits & mask)


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
        ptt_line: Which control line keys PTT — ``"dtr"`` (default, bench-confirmed cycle 29) or
            ``"rts"``. Empirical (guardrail 1); flip if the bench key-test shows the other line.
        input_device / output_device: ALSA device names for capture / playback (the AIOC card).
        blocksize: Frames per capture/playback block (:data:`DEFAULT_BLOCKSIZE`).
        tx_lead_seconds: Silence played right after PTT keys up, before real audio
            (:data:`DEFAULT_TX_LEAD_SECONDS`); prevents the transmitter/squelch key-up race from
            clipping the start of speech. 0 disables.
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
        tx_lead_seconds: float = DEFAULT_TX_LEAD_SECONDS,
        uvk5_tuner: str = DEFAULT_TUNER_MODE,
        uvk5_tune_persist: bool = DEFAULT_TUNE_PERSIST,
        uvk5_power: str = DEFAULT_POWER,
        tuner=None,
        _serial_factory=None,
        _audio=None,
    ) -> None:
        try:
            self._ptt_line = PttLine(str(ptt_line).lower())
        except ValueError as exc:
            choices = ", ".join(m.value for m in PttLine)
            raise ValueError(f"ptt_line={ptt_line!r} is not one of: {choices}") from exc

        try:
            mode = TunerMode(str(uvk5_tuner).lower())
        except ValueError as exc:
            choices = ", ".join(m.value for m in TunerMode)
            raise ValueError(f"uvk5_tuner={uvk5_tuner!r} is not one of: {choices}") from exc

        try:
            # The STATION level: what a channel transmits at when it does not name its own. A
            # preset that does moves this, so there is exactly one current level (ADR 0146).
            self._power = PowerLevel(str(uvk5_power).strip().lower())
        except ValueError as exc:
            choices = ", ".join(m.value for m in PowerLevel)
            raise ValueError(f"uvk5_power={uvk5_power!r} is not one of: {choices}") from exc

        self._input_device = input_device
        self._output_device = output_device
        self._blocksize = blocksize
        # Precompute the TX lead-in as a raw silent-PCM byte count once (0 disables). Written to the
        # playback stream right after the line is asserted, so real audio starts only once the radio
        # is on the air — see _key_on().
        self._lead_bytes = lead_in_bytes(tx_lead_seconds)
        self._audio_mod = _audio  # None -> lazily import real sounddevice on first stream open

        # Open the serial handle now (the real backend needs the device present) and force BOTH
        # lines low, so construction can never leave the transmitter keyed (guardrail).
        self._serial = (_serial_factory or _default_serial_factory)(serial_port)
        if tuner is None and mode is not TunerMode.OFF:
            # The AIOC's CDC serial carries the UV-K5's UART as well as the PTT line, so tuning
            # needs the port configured the way the dock transport's reader expects — baud, but
            # also the READ TIMEOUT, without which `read()` blocks for a full buffer and a short
            # reply is never dispatched. Applied through the transport's own helper so the two
            # cannot drift apart. NOT wrapped in a suppress: a tuner that cannot talk to the radio
            # should fail at startup, where it is one clear traceback, rather than at the first
            # preset, where it is a mystery.
            from .uvk5.transport import apply_port_settings

            apply_port_settings(self._serial, DOCK_BAUD)
        self._serial.rts = False
        self._serial.dtr = False

        # Optional UV-K5 tuner sharing this serial handle (None on a plain UV-5R, which has no
        # UART on that jack and stays TX/RX-only). `_tuned` is the channel this server put the
        # radio on; `_pending` is one being built up across setter calls.
        self._transport = None
        if tuner is None and mode is not TunerMode.OFF:
            tuner = self._build_tuner(mode, serial_port, bool(uvk5_tune_persist))
        self._tuner = tuner
        self._tuned: VfoImage | None = None
        self._pending: VfoImage | None = None
        self._batch_depth = 0

        self._capture = None  # opened lazily on first receive()
        self._playback = None  # open only while the line is asserted
        self._pacer = None  # per-keying writer thread owning all playback writes (ADR 0102)
        self._keyed = False  # True while ptt(True) holds the line across frames (streaming)
        self._transmitting = False  # reflects the line being asserted (one-shot or held)
        self._closed = False
        # Never leave the radio keyed if the process dies mid-transmission.
        atexit.register(self.close)

        # Last, and deliberately AFTER the atexit: the one exception these do not catch is a
        # broken BOOT_MODULATION, and the handle is already registered for cleanup rather than
        # leaked when it raises.
        #
        # TWO asserts, in this order, each with its OWN try/except (ADR 0157).
        #
        # Order is severity-first: a station in broadcast FM hears NOTHING on its own channel, which
        # is strictly worse than one on the wrong demodulator, so the worse fault is repaired first.
        # Both orders converge on the same end state — `Dock_FmOff` re-applies the demodulator from
        # `gEeprom.VfoInfo[]`, where `Dock_SetModulation` stores it — so this is a choice about which
        # fault survives a partial failure, not a correctness requirement.
        #
        # Separate handlers because a shared one would let a failure of the first skip the second,
        # which is ADR 0153's lesson in the place it would bite next: F8 is unmerged, so `0x0879`
        # times out on EVERY radio today, and one shared try/except would silently cost every
        # station the ADR 0155 demodulator assert as collateral.
        self._assert_boot_broadcast_fm()
        self._assert_boot_modulation()

    def _assert_boot_broadcast_fm(self) -> None:
        """Switch the radio's second receiver off once, at construction (ADR 0157).

        A UV-K5 carries a BK1080 commercial-FM chip beside the BK4819. When it runs it holds the
        speaker line the AIOC listens on, so the station hears **nothing** on its own channel — and
        transmits normally throughout, because `RADIO_PrepareTX` has no broadcast-FM term. The
        automatic station ID required by guardrail 5 therefore goes out into a channel nobody is
        monitoring: worse than the demodulator fault ADR 0155 fixed, where the over was at least
        silent.

        It has to be done at startup specifically because the firmware **persists the state to
        flash behind the host**: `app.c:1761-1767` calls `FM_Start()` about five seconds after any
        squelch close, so a host crash or an unplugged cable mid-FM leaves `CURRENT_STATE = 3` and
        the radio boots straight back into broadcast FM with no host present to stop it.

        Best-effort, exactly as the modulation assert is: a radio that is not cabled must not stop
        the server starting. The difference is what silence *means*. F8 is a fork branch that is
        neither merged nor flashed, so today a silent `0x0879` is the norm rather than a fault —
        hence INFO, not WARNING. A warning on every boot of every station would train operators to
        ignore the ADR 0155 warning printed next to it, which is the one that means something.
        A radio that ANSWERS and refuses is a different matter, and `clear_broadcast_fm` logs that
        at WARNING itself.
        """
        tuner = self._tuner
        if tuner is None or Capability.SET_MODULATION not in tuner.capabilities():
            # Gated on SET_MODULATION, not on CLEAR_BROADCAST_FM, and the circularity is the reason:
            # that capability is *earned by this frame's reply*, so gating on it would mean never
            # sending the frame that earns it. This gate asks "is this a fork-firmware tuner, so is
            # it worth trying" — a heuristic whose failure is handled two lines below — rather than
            # claiming F8. It is still a CAPABILITY gate, never `hasattr`: every tuner here has a
            # `clear_broadcast_fm` and one of them exists only to raise (guardrail 3).
            return
        if not isinstance(tuner, Uvk5Tuner):
            # AFTER the capability gate, so this fires on exactly the coupling bug and not on every
            # duck-typed tuner in the suite. ADR 0157 recorded that a tuner advertising
            # SET_MODULATION is assumed to have the clear method, and one lacking it raises
            # `AttributeError` out of a CONSTRUCTOR — which neither handler below catches, so the
            # whole backend fails to build. The recorded fix (declare it on the protocol) was
            # already true and therefore a no-op; the real defect was that nothing ever CHECKED the
            # protocol. `Uvk5Tuner` is `@runtime_checkable`, and on this repo's Python this does
            # check data members as well as methods.
            #
            # A skip lands exactly where a failure lands: `broadcast_fm` stays `None`, because the
            # server genuinely does not know. Never "off" — that is an affirmative claim that the
            # station can hear, and it is the one wrong answer that matters here.
            #
            # WARNING rather than the INFO below, and the asymmetry is the point: a radio that does
            # not answer is an ordinary fact of life on older firmware, while a tuner that claims
            # the capability without implementing it is a programming fault somebody has to fix.
            #
            # NOT applied to `_assert_boot_modulation`. `Uvk5Tuner` is a fat protocol and
            # `isinstance` is all-or-nothing across ten members, so gating the demodulator assert on
            # it would let a missing `clear_broadcast_fm` — a member with nothing whatever to do
            # with the demodulator — silently cost a station its ADR 0155 assert. That is ADR 0153's
            # "a failure of the first must not skip the second", reappearing through a conformance
            # check instead of a shared try/except.
            logger.warning(
                "aioc: tuner %s advertises set_modulation but does not satisfy the Uvk5Tuner "
                "protocol, so the broadcast-FM assert was skipped. This server does NOT know "
                "whether the radio can hear its own channel, and reports broadcast_fm=null.",
                type(tuner).__name__,
            )
            return
        try:
            tuner.clear_broadcast_fm()
        except (TuneError, OSError) as exc:
            # The same two-member tuple, chosen for the same reasons, as the modulation assert
            # below: `TuneError` is the designed failure and raises before anything is recorded, so
            # the state here is the honest `None`; `OSError` is the one fault that arrives unwrapped
            # because the transport re-raises its reader thread's error verbatim and
            # `serial.SerialException` is an `OSError`.
            logger.info(
                "aioc: could not clear broadcast FM at startup (%s: %s) — this is expected on "
                "firmware older than F8, which has no such command. This server therefore does NOT "
                "know whether the radio can hear its own channel, and reports broadcast_fm=null.",
                type(exc).__name__, exc,
            )

    def _assert_boot_modulation(self) -> None:
        """State the demodulator once, at construction. Never read it (ADR 0155).

        The firmware keeps its modulation in the **dock session** — RAM that this server restarting
        does not touch, and that only the radio's own power switch reseeds. So a host coming back up
        against a radio the operator left demodulating AM reported `modulation=None, tx_ok=None`,
        and `_refuse_if_tx_disabled` — which refuses only a MEASURED `False`, and rightly so — let
        the first key-up drive the DTR line into a transmitter `VFO_STATE_TX_DISABLE` had already
        disabled. The over is silence, `status()` says `transmitting`, and under guardrail 5 the
        transmission it eats is the station ID.

        **Writing is what makes the belief TRUE rather than better-guessed.** Reading was rejected
        three ways: there is no get-modulation opcode, so it would mean a firmware fork, a flash and
        a fresh bench proof (ADR 0148/0149); inferring one from BK4819 over `0x0851` reads the
        firmware's leftover rather than its VFO truth, the argument already written at
        `uvk5/radio.py` for `_pa` and for split; and adopting even a *successful* read is ADR 0132's
        "take whatever state you find", the exact fault the `None` seeding in `tuner.py` exists to
        avoid. One `0x0877`: no HELLO, no `0x0873`, no EEPROM write, nothing keyed, and no lockout
        armed — the dock opcodes are not among the sites that arm `SERIAL_TX_LOCKOUT_S`.

        **Best-effort, unlike the `apply_port_settings` fail-loud above, and the asymmetry is the
        point.** That one fails when the serial HANDLE is unusable, after which nothing this backend
        does works at all, so there is nothing to degrade to. This one fails when a perfectly good
        handle has a radio switched off or unplugged at the far end — an ordinary Tuesday, because
        the operator powers the radio after the server — and the failure is **representable**:
        `modulation` stays `None`, the honest "this server has not asserted one" the tri-state was
        built for, which nothing downstream mistakes for a measurement. Taking the process down for
        it would make a station that boots before its radio unstartable, to fix a fault that only
        bites on a key-up.
        """
        tuner = self._tuner
        if tuner is None or Capability.SET_MODULATION not in tuner.capabilities():
            # No tuner (a plain UV-5R has no UART on that jack), or an `eeprom` tuner talking stock
            # firmware with no 0x0877 case at all. Gated on the CAPABILITY, mirroring
            # `presets._apply_fields` — never on `hasattr(tuner, "set_modulation")`, which every
            # tuner in this package has and one of which exists only to raise (guardrail 3).
            return
        try:
            tuner.set_modulation(BOOT_MODULATION)
        except (TuneError, OSError) as exc:
            # Two members, no subsumption between them, so one clause is right and there is no
            # order to get wrong (cf. ADR 0153, where there was). `TuneError` is the designed
            # failure — silence, refusal, or a reply naming a different demodulator — and all three
            # raise BEFORE the tuner records anything, so the state here is exactly the honest
            # `None`. `OSError` is the one fault that arrives unwrapped: the transport re-raises its
            # reader thread's stored error verbatim, and `serial.SerialException` is an `OSError`,
            # so a yanked cable has no dock vocabulary on it. Catching only the first would be a
            # PARTIAL fix, not a smaller one (ADR 0153 again).
            #
            # NOT caught: `Uvk5Timeout`, which `set_modulation` already converts and which therefore
            # cannot reach here — a test drives a genuinely silent radio to pin that conversion,
            # which is stricter than widening this tuple, because widening it would HIDE a refactor
            # that dropped it. And NOT `ValueError`, which only a typo'd BOOT_MODULATION can raise:
            # a programming error must fail at construction as one clear traceback, the same rule
            # this module already applies to `uvk5_power`.
            logger.warning(
                "aioc: could not state the demodulator at startup (%s: %s) — this server has NOT "
                "asserted one, so it reports modulation=null and cannot refuse a key-up the radio "
                "would swallow. Power the radio on, then apply a preset or POST /modulation.",
                type(exc).__name__, exc,
            )

    def _build_tuner(self, mode: "TunerMode", serial_port: str, persist: bool = False):
        """Wrap the serial handle this backend already owns in a dock transport, and pick a tuner.

        The transport is given the **open handle** rather than the port name: pyserial takes the
        device exclusively, so a second open would fail, and there is nothing to gain from one —
        the AIOC carries dock frames while the sound card streams in both directions (measured,
        18/18), so one process holding one handle is the whole requirement.
        """
        from .uvk5.transport import Uvk5Transport
        from .uvk5.tuner import EepromTuner, HybridTuner, SetVfoTuner

        self._transport = Uvk5Transport(
            serial_port=serial_port,
            baud=DOCK_BAUD,
            _serial_factory=lambda _port, _baud: self._serial,
        )
        if mode is TunerMode.SETVFO:
            return SetVfoTuner(self._transport)
        if mode is TunerMode.HYBRID:
            # Composed from the two real tuners rather than reimplementing either, so the EEPROM
            # half is the same code the 80/80 differential gate ran against.
            return HybridTuner(
                SetVfoTuner(self._transport), EepromTuner(self._transport), persist=persist
            )
        return EepromTuner(self._transport)

    # --- audio plumbing -------------------------------------------------------

    def _sd(self):
        """The sounddevice-like module (injected fake, or the real library, lazily imported)."""
        self._audio_mod = load_sounddevice(self._audio_mod, extra_hint=_EXTRA_MSG)
        return self._audio_mod

    def _open_capture(self):
        return open_capture_stream(
            self._sd(), device=self._input_device, blocksize=self._blocksize
        )

    def _drop_line(self) -> None:
        """Drive the PTT line low — the unconditional un-key primitive, RF-safety's floor.

        Never guarded on ``_keyed`` or any tracked state, so a desynced flag can never leave the
        transmitter stranded keyed. A bare serial ``setattr`` (no drain, no stream teardown), so it
        cannot block or raise on the audio path. Every un-key route ends here (ADR 0093).
        """
        setattr(self._serial, self._ptt_line.value, False)
        self._transmitting = False

    def ptt_line_asserted(self) -> bool | None:
        """Is the PTT line actually high right now, per the kernel? ``None`` if unreadable.

        Diagnostic, not part of the ``Radio`` protocol: it answers the one question the rest of this
        class cannot, which is whether a key-up reached the hardware at all. Read it *during* an
        over — `_key_off` drops the line unconditionally, so afterwards it is always low.
        """
        return read_line_state(self._serial, self._ptt_line.value)

    def tx_ready_in(self) -> float | None:
        """Seconds until the radio will accept a key-up, or ``None`` if it will accept one now.

        A UV-K5 mutes its transmitter for six seconds after any EEPROM conversation
        (`SERIAL_TX_LOCKOUT_S`) — it refuses a key-up *and* cuts an over already in progress. The
        hybrid tuner publishes the deadline instead of sleeping on it, because a channel change is
        audible immediately and only someone about to transmit needs to wait.

        Exposed so `status()` can report it and the UI can stop offering a button that will do
        nothing. It is not what enforces the wait — :meth:`_key_on` is, because a browser must
        never be the thing keeping RF correct.
        """
        deadline = getattr(self._tuner, "tx_ready_at", None)
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        return remaining if remaining > 0 else None

    def _await_tx_lockout(self) -> None:
        """Block until the radio will actually key. Bounded by the lockout's own length.

        Returning "tuned" to a radio that will silently swallow the next six seconds of PTT is the
        ADR 0142 fault exactly — every carrier row failed on attempt #1 and passed thereafter. The
        wait did not go away when the hybrid tuner stopped sleeping on it; it moved here, to the
        only caller that cares.
        """
        remaining = self.tx_ready_in()
        if remaining is None:
            return
        logger.info("aioc: holding key-up %.1fs for the radio's serial TX lockout", remaining)
        time.sleep(min(remaining, SERIAL_TX_LOCKOUT_S))

    @property
    def tune_persist(self) -> bool | None:
        """Is the tuner storing its channels on the radio? ``None`` when there is no such choice.

        ``None`` on no tuner, on ``setvfo`` (never stores) and on ``eeprom`` (always does) — the
        same "the question does not apply here" the CAT fields use, so the UI can hide a switch
        that would be a lie rather than render one that does nothing.
        """
        return getattr(self._tuner, "persist", None)

    def set_tune_persist(self, on: bool) -> bool:
        """Turn channel storage on or off. Returns the resulting state.

        Turning it **on** stores the channel the radio is *already* on, rather than only affecting
        the next tune: "save the channel to the radio" has to save the channel, or the switch is
        decoration until the operator happens to tap something.

        Refused mid-transmission, and that is not politeness — storing arms the firmware's serial
        lockout, and `SerialConfigInProgress()` **cuts an over already in progress** (`app.c:1111,
        1146, 1191`). A switch that killed a transmission when flipped would be a trap.
        """
        if self.tune_persist is None:
            raise UnsupportedCapability(Capability.SET_FREQUENCY)
        if self._transmitting:
            raise RuntimeError("refusing to change channel storage while transmitting")

        # Turning it OFF touches nothing on the radio: whatever is already in flash stays there,
        # and that is exactly what a power cycle should fall back to. Only the promise about
        # *future* tunes changes.
        on = bool(on)
        was, self._tuner.persist = self._tuner.persist, on
        if on and not was and self._tuned is not None:
            store = getattr(self._tuner, "store", None)
            if callable(store):
                store(self._tuned)
        logger.info("aioc: channel storage %s", "on" if on else "off (instant)")
        return on

    def _reassert_channel(self) -> None:
        """Put the radio back on the channel this server chose. Cheap, confirmed, pre-RF.

        Only for a tuner whose tunes live in RAM. Such a tune is gone the moment somebody uses the
        radio's power switch — but `status()` still reports the channel the server picked, and the
        UI still highlights it, so without this the next over goes out on a stale frequency and
        nothing anywhere says otherwise. That is the failure this exists to prevent; it is not an
        optimisation.

        One `0x0873` and its `0x0874` read-back: about ten milliseconds, no session needed, and it
        arms no lockout (the dock opcodes do not — `SERIAL_TX_LOCKOUT_S`). Re-tuning is genuinely
        cheaper here than asking where the radio is, because asking means a HELLO and a HELLO costs
        six seconds of mute.

        Called from :meth:`_key_on` *before* the stream opens and before the line is asserted, so a
        radio that does not confirm refuses the key-up with nothing opened and nothing keyed —
        `TuneError` is a `RadioUnavailable`, which the API renders as a 503 with the reason (ADR
        0143). A radio that cannot confirm its channel has no business transmitting on it.
        """
        tuner, tuned = self._tuner, self._tuned
        if tuned is None or tuner is None or not getattr(tuner, "volatile", False):
            return
        # The demodulator first, and for the same reason as the channel. The firmware holds it in
        # its dock session — RAM — and reseeds FM on a power cycle, so after somebody uses the
        # radio's power switch this server reports AM while the radio is on FM. One more one-shot
        # frame, arming no lockout, and it means the `tx_ok` the refusal below reads was measured
        # milliseconds ago instead of remembered from whenever the operator last chose (ADR 0150).
        modulation = getattr(tuner, "modulation", None)
        if modulation is not None:
            tuner.set_modulation(modulation)
        reassert = getattr(tuner, "reassert", None)
        if callable(reassert):
            reassert(tuned)

    def _clear_if_deafened(self) -> None:
        """Ask the radio whether it can hear itself — **now**, not at boot — and refuse if it cannot.

        The BK1080 commercial-FM receiver holds the speaker line the AIOC listens on, so while it
        runs this station hears NOTHING on its own channel. Worse than the AM fault below, where the
        over was at least silent: here the failure is invisible from both ends, and what goes out
        blind includes the Part 97 station ID that guardrail 5 makes required controller behaviour
        rather than a feature.

        **Why this asks rather than remembers** (ADR 0161). ADR 0158 read `tuner.broadcast_fm`, and
        ADR 0160 then measured on hardware that the block is written in exactly one place — the boot
        assert — which *clears the very condition the gate tests*. So after startup it is permanently
        `on=False`, and an operator pressing F+0 on the radio's own keypad leaves the station deaf
        with this gate silent. The gate was not bypassed; it was blind.

        **The wire offers no read that is not also a repair, and that shapes everything here.**
        `0x0879` has three actions — OFF, ON, TUNE — only an `APPLIED` reply carries live `state` and
        live `flags`, every refusal blanks both, `ClearBroadcastFm` can build only OFF by design, and
        the firmware reports state *after* acting. So the one frame that can answer "is this station
        deaf?" is the frame that gives it its ears back. A key-up on a deaf station is therefore
        **repaired and allowed**, not refused, and the refusal below survives for the radio that was
        asked and did not stop.

        The limit that follows is real and is not worked around: because the reply describes the
        state *after* the OFF, and an already-off OFF answers byte-identically (measured, ADR 0160),
        **this cannot report that it just rescued a deaf station.** It repairs silently. Only a
        read-only action on the wire could change that, and that is a firmware cycle.

        **Gated on the EARNED `CLEAR_BROADCAST_FM`.** The capability is granted only once a radio has
        actually answered `0x087A`, so firmware that cannot answer pays one frozenset membership test
        per key-up — no frame, no wait — instead of `SetVfoTuner`'s 3.0 s timeout before every over
        and a `TuneError` that would refuse every key-up on every station (ADR 0158 R2's warning).
        The circularity that forbids this gate at *boot* — the capability is earned by the frame it
        would gate — does not apply here, which runs after the boot assert earned it or did not.

        **Still first in `_key_on`**, but on two grounds rather than ADR 0158's three: "strictly
        cheaper than the step it displaces" died when this became a frame. What survives is
        severity-first (the ordering `__init__` already argues for the two boot asserts, with a test
        pinning it there) and non-maskability — `_reassert_channel` raises `TuneError` of its own, and
        an unrelated tune failure must not hand the operator a message about the channel on a station
        whose real problem is that it cannot hear at all.

        `getattr` rather than a direct read, exactly as `status()` does: `self._tuner` is `None` on a
        plain UV-5R, and the duck-typed tuners that predate all of this have no such attribute.
        """
        tuner = self._tuner
        if tuner is not None and Capability.CLEAR_BROADCAST_FM in tuner.capabilities():
            # ADR 0162: ask before repairing, so the repair can be reported. ADR 0161 recorded that a
            # pre-key-up clear rescues a deaf station and can never say it did — both OFF legs answer
            # byte-identically — and concluded that only new firmware could fix it. It could not: an
            # out-of-band `0x0879` TUNE is refused *before* the firmware touches anything, and which
            # refusal comes back is the answer (finding 5, closed).
            #
            # `getattr`, because tuners are duck-typed here and the ones that predate this cycle do
            # not have it. It writes nothing, raises nothing, and returns None for every failure, so
            # there is deliberately no `try` around it — adding one would suggest it could throw, and
            # this call site is where ADR 0161 put a frame that took the station ID off the air.
            probe = getattr(tuner, "probe_broadcast_fm", None)
            if probe is not None:
                probe()
            try:
                tuner.clear_broadcast_fm()
            except TuneBusy as exc:
                # NOT a refusal, and the bench is why this branch exists (ADR 0161). `ERR_TX` means
                # `gCurrentFunction` is TRANSMIT or MONITOR — an open squelch, which is most of an
                # active QSO — so before every key-up this arrives *routinely*. Treating it as a
                # fault made a busy receiver into a station that would not transmit, and the first
                # thing it stopped was the automatic station ID.
                #
                # So the rule the whole arc runs on applies unchanged: an unmeasured field must
                # never lock a transmitter. The reading was not refreshed and was not invalidated —
                # the tuner deliberately leaves it standing — and the key-up proceeds on what was
                # already known, including on a known `on=True`, which still refuses below.
                #
                # DEBUG, not INFO: it is expected traffic on a busy station, and a line per key-up
                # at INFO would bury the ADR 0155 warning printed beside it.
                logger.debug("aioc: could not re-read the second receiver before this key-up (%s)", exc)
            except (TuneError, OSError) as exc:
                # A clear that was ATTEMPTED and failed is its own refusal, and it must not surface
                # as a generic key-up failure. The tuner's own message is written for the BOOT assert
                # ("expected on firmware older than F8") and is wrong here: the capability gate above
                # means this radio has already answered `0x087A` at least once, so silence now is a
                # radio that has gone away rather than one that never had the command.
                #
                # It refuses, and that is not the "an unmeasured field must never lock a transmitter"
                # rule being broken — that rule is about a radio nobody has ASKED. This one was asked
                # and did not answer, which `_reassert_channel` refuses on three lines below, on the
                # same timeout, for the same reason. `clear_broadcast_fm` has already blanked the
                # block to None, so `status()` reports the honest unknown rather than a stale `off`.
                raise RadioUnavailable(
                    f"could not ask the radio about its second receiver ({type(exc).__name__}: "
                    f"{exc}) — so this server does NOT know whether the station can hear its own "
                    f"channel, and will not key one that might be deaf. Check the radio is powered "
                    f"on and cabled."
                ) from exc
        refuse_if_deafened(getattr(tuner, "broadcast_fm", None))

    def probe_broadcast_fm(self, **kwargs) -> bool | None:
        """Read whether the second receiver is **selected**, without repairing it (ADR 0163).

        The public seam the broadcast-FM cadence polls. It exists as a method rather than the
        composition root reaching into `self._tuner` because the invariant it has to respect is not
        obvious from outside: **this records nothing.** `clear_broadcast_fm` stays the sole writer of
        the block `refuse_if_deafened` reads, so no amount of polling — succeeding, failing, or
        racing a key-up — can change what a key-up decides. Hiding that behind a private attribute
        would leave the next caller to rediscover it, and ADR 0161 is what rediscovering it costs.

        ``True`` means *FM mode is selected*, which is not the same as *deaf this instant*: the
        firmware drops the BK1080 for the duration of a real over and does not clear the flag
        (ADR 0163 M3, measured). The relay mute acts on the coarser claim deliberately; see the ADR
        for what that costs and why it is the right trade.

        ``None`` — no tuner, no capability, no probe, or a probe that learned nothing — is the
        answer that changes nothing anywhere.
        """
        tuner = self._tuner
        if tuner is None or Capability.CLEAR_BROADCAST_FM not in tuner.capabilities():
            return None
        probe = getattr(tuner, "probe_broadcast_fm", None)
        if probe is None:
            return None
        return probe(**kwargs)

    def _refuse_if_tx_disabled(self) -> None:
        """Refuse a key-up the radio itself would swallow, and say why.

        Built without ``ENABLE_TX_WHEN_AM``, the firmware sets ``VFO_STATE_TX_DISABLE`` for any
        modulation that is not FM — and that is the path the radio's PTT **pin** drives, which is
        exactly where this backend keys, through the AIOC's DTR line. So in AM the line goes high,
        the radio declines, and the over is silence. Nothing anywhere would say so: `ptt()` returns,
        `status()` reports transmitting, the UI lights up.

        That is the fault class ADR 0140/0143 spent four cycles on — "no signal" and "no
        measurement" being the same event to the host — and here it also eats the **station ID**,
        which Part 97 makes required controller behaviour rather than a feature (guardrail 5).

        Only a **measured** ``False`` refuses. ``None`` is "nobody has asked this radio", and an
        unknown must never block a key-up: the whole surface reports `None` before the first
        assertion, and a station that would not transmit until someone had chosen a demodulator
        would be a worse failure than the one this prevents.
        """
        if getattr(self._tuner, "tx_ok", None) is not False:
            return
        modulation = getattr(self._tuner, "modulation", None) or "a non-FM modulation"
        raise RadioUnavailable(
            f"the radio is demodulating {modulation} and refuses its own PTT path "
            f"(VFO_STATE_TX_DISABLE — this firmware is built without ENABLE_TX_WHEN_AM, so AM is "
            f"receive-only). Set modulation FM to transmit."
        )

    def _key_on(self) -> None:
        """Open the playback stream, start its pacer, assert the PTT line, queue the TX lead-in.

        Ordering is an RF-safety invariant. The stream opens FIRST: if opening the audio device
        fails, the line is never asserted (a failed key-up must not leave the transmitter keyed).
        With the pacer (ADR 0102) no blocking write happens here any more: the 0.5 s lead-in is
        *enqueued* (cannot raise, cannot block), so the atomic-undo guard narrows to the line-assert
        itself. A lead-in (or any) write that later fails on the pacer thread unkeys via the pacer's
        ``on_error`` → :meth:`_key_off` — the ADR 0093 stranded-key guard, moved with the write.

        Three refusals run before anything is opened, and their order is argued, not incidental.
        The deafness check goes first, and since ADR 0161 it is a **frame** rather than a memory —
        one `0x0879`, which both asks and repairs, because this wire has no read that is not also a
        repair. It names the worst fault, so it must not be maskable by a channel re-assert that
        happens to fail (ADR 0158). The channel re-assert follows, ahead of even the lockout wait:
        it is the one step that can decide this key-up should not happen at all, and the cheapest
        place to refuse is before anything has been opened or waited on (ADR 0145). The AM refusal
        comes after it for the same reason and in that order deliberately — the re-assert is what
        makes `tx_ok` a fresh measurement rather than a memory (ADR 0150).

        The pre-key-up cost is now three one-shot frames rather than two: ~0.1 s for the `0x0879`
        round trip as measured at the bench (ADR 0160), no transmit lockout armed, and no flash
        written when the receiver is already off — the firmware's own `memcmp` short-circuit, which
        is what makes a per-key-up frame affordable instead of EEPROM wear on every over.
        """
        self._clear_if_deafened()
        self._reassert_channel()
        self._refuse_if_tx_disabled()
        self._await_tx_lockout()
        stream = open_playout_stream(
            self._sd(), device=self._output_device, blocksize=self._blocksize
        )
        # The bound must always admit the lead-in slug plus headroom for real-time producers.
        pacer = _AiocTxPacer(
            stream,
            max_buffer_bytes=playout_buffer_bytes(self._lead_bytes),
            on_error=self._key_off,
        )
        try:
            setattr(self._serial, self._ptt_line.value, True)
            self._transmitting = True
        except Exception:
            # Atomic key-up: undo everything, so a partial failure never strands the transmitter keyed.
            with contextlib.suppress(Exception):
                self._drop_line()
            pacer.stop()
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()
            raise
        # TX lead-in (guardrail 1): with the line asserted, queue a fixed slug of silence so the
        # transmitter and the far-end squelch are fully up before real audio plays — otherwise the
        # first fraction of a second of speech is clipped over the air. Fires exactly once per
        # physical key-up (backs both one-shot transmit() and streaming ptt(True)).
        if self._lead_bytes:
            pacer.enqueue(b"\x00" * self._lead_bytes)
        self._playback = stream
        self._pacer = pacer

    def _key_off(self) -> None:
        """Drop the PTT line FIRST, then stop the pacer (discarding), then close the stream.

        RF-safety inversion of the original drain-then-drop (ADR 0029 → ADR 0093): dropping the
        transmitter must NEVER depend on the audio-stream teardown succeeding. A drain that blocks, or
        a ``stop()``/``close()`` that raises on an xrun'd/starved stream, must not keep the line
        asserted — the exact way the crossband stranded the transmitter keyed. So the line goes low
        immediately and unconditionally (``_drop_line``); the pacer then discards anything still
        queued (ADR 0102 — buffered audio playing on after unkey was the "long FM tail") and the
        stream is torn down best-effort. Idempotent; also the pacer's write-failure ``on_error``.
        """
        self._drop_line()
        pacer, self._pacer = self._pacer, None
        if pacer is not None:
            pacer.stop()
        stream, self._playback = self._playback, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.stop()
            with contextlib.suppress(Exception):
                stream.close()

    # --- shared surface -------------------------------------------------------

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"radio accepts {CANONICAL_FORMAT}, got a frame in {audio.format}"
            )
        if self._keyed:
            # Streaming: ptt(True) already holds the line and started the pacer — queue the frame
            # and return at once. The blocking device write happens on the pacer thread (ADR 0102),
            # never on the caller (the event loop). A pacer torn down by a device failure swallows
            # the frame; the failure path has already unkeyed.
            pacer = self._pacer
            if pacer is not None:
                pacer.enqueue(audio.samples)
            return
        # One-shot: self-key for exactly the duration of this clip. The blocking contract stands —
        # callers (station ID, TTS, /transmit) rely on "returns once the clip has been played" — so
        # wait for the pacer to drain the lead-in + clip before dropping the key.
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
            # Unconditional un-key (ADR 0093): ptt(False) is the caller's safety lever — the watchdog,
            # the bridge teardown, the REST /ptt off — and it must ALWAYS drive the line low, even if
            # we believe we are not keyed (a key-up that failed after asserting the line, or any flag
            # desync). `_key_off` drops the line first, then tears down any stream. Idempotent.
            self._keyed = False
            self._key_off()

    # --- tuning (only when a UV-K5 tuner is attached) --------------------------
    #
    # Baofeng mode is TX/RX only on a UV-5R, and stays that way: with no tuner injected every
    # setter below raises `UnsupportedCapability` and `capabilities()` is unchanged, so nothing
    # about the existing backend moves. A UV-K5 on the same AIOC cable is the exception — its
    # UART rides the very port this class already holds for PTT, so the radio can be told which
    # repeater to be on without stopping anything (measured: dock frames survive capture AND
    # playback streaming, 18/18).
    #
    # The setters build up a pending channel and do NOT write it. `apply_preset` makes up to four
    # calls for one channel, and on the EEPROM path each write costs a reboot and a flash cycle,
    # so committing per setter would reboot the radio four times to change channel once. The
    # commit happens in `tuning_batch`, which the preset seam wraps around the lot.

    def _require_tuner(self, capability: Capability):
        if self._tuner is None:
            raise UnsupportedCapability(capability)
        return self._tuner

    def _stage(self, **changes) -> None:
        """Update the pending channel. Raises before mutating if the result is not a real channel."""
        current = self._pending or self._tuned
        if current is None:
            base = {
                "rx_hz": None, "tx_hz": None, "ctcss_tenths": 0, "narrow": False,
                # The station level, not VfoImage's default: a first tune after the operator asked
                # for low must not go out at high (ADR 0146).
                "power": self._power.step,
            }
        else:
            base = {
                "rx_hz": current.rx_hz, "tx_hz": current.tx_hz,
                "ctcss_tenths": current.ctcss_tenths, "narrow": current.narrow,
                "power": current.power,
            }
        base.update(changes)
        if base.get("rx_hz") is None:
            raise ValueError("set a frequency before a split, tone or mode")
        if base.get("tx_hz") is None:
            base["tx_hz"] = base["rx_hz"]
        self._pending = VfoImage(**base)

    def set_frequency(self, hz: int) -> None:
        self._require_tuner(Capability.SET_FREQUENCY)
        # Clears any armed split, like every other backend (ADR 0133): a TX leg that survived a
        # retune would let an unattended station ID key a repeater's uplink.
        self._stage(rx_hz=int(hz), tx_hz=int(hz))
        self._autocommit()

    def set_split(self, tx_hz: int | None) -> None:
        self._require_tuner(Capability.SET_SPLIT)
        current = self._pending or self._tuned
        if current is None:
            raise ValueError("set a frequency before a split")
        self._stage(tx_hz=int(tx_hz) if tx_hz is not None else current.rx_hz)
        self._autocommit()

    def set_tone(self, tone: float | None) -> None:
        self._require_tuner(Capability.SET_TONE)
        self._stage(ctcss_tenths=round(float(tone) * 10) if tone else 0)
        self._autocommit()

    def set_mode(self, mode: str) -> None:
        self._require_tuner(Capability.SET_MODE)
        text = str(mode).strip().upper()
        if text not in ("FM", "NFM"):
            raise ValueError(f"mode must be FM or NFM, got {mode!r}")
        self._stage(narrow=(text == "NFM"))
        self._autocommit()

    def set_modulation(self, modulation: str) -> None:
        """Put the radio on ``FM`` or ``AM`` (ADR 0150). Not :meth:`set_mode`, which is bandwidth.

        Deliberately **not** staged through `_stage`/`VfoImage` like every setter around it. The
        modulation is not on `0x0873`'s wire and is not part of a channel: it is a one-shot frame
        with its own lifetime, which the firmware then keeps and applies to every later tune. So it
        needs no pending channel, and it works before a frequency has ever been set — where
        `_stage` would (rightly, for a tone) refuse with "set a frequency before a split, tone or
        mode".

        Switching to AM stops this radio transmitting: the AIOC keys through the radio's own PTT
        pin, which `VFO_STATE_TX_DISABLE` blocks in any non-FM modulation. That is reported rather
        than prevented here — `_refuse_if_tx_disabled` is what stops a key-up going out into
        nothing.
        """
        tuner = self._require_tuner(Capability.SET_MODULATION)
        tuner.set_modulation(modulation)

    def set_power(self, level: str) -> None:
        """Set the station's transmit power level (ADR 0146).

        Three steps because three is what the firmware's dock map offers; what each is in watts is
        the radio's business, computed per band from calibration in its own flash that this host
        cannot read. That is the point rather than a shortcoming — it is the calibrated path, unlike
        the dock backend's raw bias write (ADR 0128/0134).
        """
        self._require_tuner(Capability.SET_POWER)
        try:
            want = PowerLevel(str(level).strip().lower())
        except ValueError as exc:
            choices = ", ".join(m.value for m in PowerLevel)
            raise ValueError(f"power must be one of: {choices}; got {level!r}") from exc

        if (self._pending or self._tuned) is None:
            # Nothing is tuned, so there is no channel to re-key — record the level the next tune
            # will use and stop. Not a special case for its own sake: `_stage` would refuse with
            # "set a frequency before a split, tone or mode", which is the right answer for a tone
            # (it belongs to a channel) and the wrong one for a station-wide default.
            self._power = want
            return

        # Staged, then remembered, in that order — `_stage` validates and raises before mutating.
        # The level moves with the PENDING channel rather than with a successful commit, because
        # that is what every other setter here already does: `commit_tuning` deliberately keeps
        # `_pending` when the tuner raises, so a failed `set_tone` is retried by the next tune. If
        # the level did not move with it, a failed `set_power` would still reach the radio on the
        # next tune while this said otherwise. What is *confirmed* is a different question, and
        # `status()` answers it from `_tuned` — which a failed tune never updates.
        self._stage(power=want.step)
        self._power = want
        self._autocommit()

    def _autocommit(self) -> None:
        """Commit now unless a batch is open. A single `POST /frequency` must take effect on its
        own; four calls inside `tuning_batch` must cost one tune."""
        if self._batch_depth == 0:
            self.commit_tuning()

    @contextmanager
    def tuning_batch(self):
        """Group setter calls into one tune. Re-entrant; commits on the outermost exit."""
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
        if self._batch_depth == 0:
            self.commit_tuning()

    def commit_tuning(self) -> None:
        """Write the pending channel to the radio. No-op when nothing changed."""
        if self._tuner is None or self._pending is None or self._pending == self._tuned:
            self._pending = None
            return
        if self._transmitting:
            # Retuning mid-over would move the carrier out from under the audio, and on the EEPROM
            # path would reboot the radio while it is keyed. Refuse; the caller retries.
            raise RuntimeError("refusing to retune while transmitting")
        image = self._pending
        self._tuner.apply(image)
        self._tuned = image
        self._pending = None

    def reboot_radio(self) -> None:
        """Soft-reset the UV-K5 on the other end of the dock. Bench affordance (ADR 0144).

        The one state no unattended test can otherwise reach: a radio that has been switched off
        and on. Whether the channel is still there afterwards is the whole persistence claim, and
        reading back the bytes we just wrote does not test it — that proves storage, not that the
        radio boots onto it and radiates there.

        `_tuned` is deliberately **kept**. It records the channel this server chose, and the point
        of the exercise is to find out whether the radio agrees after a reboot; clearing it here
        would erase the expectation the test is about to check.
        """
        if self._transport is None:
            raise UnsupportedCapability(Capability.SET_FREQUENCY)
        if self._transmitting:
            raise RuntimeError("refusing to reboot the radio while transmitting")
        from .uvk5 import frames as f

        self._transport.send(f.Reset())

    def status(self) -> RadioStatus:
        # No hardware busy/COS line on the UV-5R (ADR 0015): busy is always False here; RX gating is
        # software VAD (audio.squelch=audio), not a carrier-detect the radio reports. `transmitting`
        # tracks whether the PTT line is currently asserted.
        #
        # The frequency fields report the channel THIS SERVER put the radio on, and are None until
        # it has put it on one — never a guess. With no tuner, or before the first tune, the radio
        # is on whatever its front panel says and the host genuinely cannot see it; reporting a
        # number there would be inventing one (ADR 0134).
        tuned = self._tuned
        split = tuned.tx_hz if (tuned and tuned.tx_hz != tuned.rx_hz) else None
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._transmitting,
            busy=False,
            frequency=tuned.rx_hz if tuned else None,
            tx_frequency=split,
            tone=(tuned.ctcss_tenths / 10) if (tuned and tuned.ctcss_tenths) else None,
            mode=("NFM" if tuned.narrow else "FM") if tuned else None,
            # The level the radio CONFIRMED, not the one asked for: `SetVfoTuner` raises unless the
            # 0x0874 read-back of `OUTPUT_POWER` matches, so `_tuned` cannot hold a level the radio
            # disagreed with. `None` before the first tune, like every other field here — the radio
            # is on whatever its front panel says and the host cannot see it (ADR 0134).
            power=tuned.level if tuned else None,
            tx_ready_in=self.tx_ready_in(),
            tune_persist=self.tune_persist,
            # Read off the tuner, not off a copy kept here, so there is one place this can be
            # wrong. `None` on a tuner that cannot set it and before this server has asserted one:
            # the radio is on whatever the firmware seeded or the front panel chose, and we did not
            # look (ADR 0132/0150).
            modulation=getattr(self._tuner, "modulation", None),
            tx_ok=getattr(self._tuner, "tx_ok", None),
            # Same rule again, and it matters more here: `None` means this server never learned
            # whether the second receiver is running, which on pre-F8 firmware is every radio.
            # Defaulting it to "off" would report a station as hearing on no evidence at all.
            broadcast_fm=getattr(self._tuner, "broadcast_fm", None),
        )

    def capabilities(self) -> frozenset[Capability]:
        if self._tuner is None:
            return SHARED_CAPS
        return SHARED_CAPS | self._tuner.capabilities()

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
        # The transport wraps the SAME handle, and its close() closes it. Going through the
        # transport first also stops its reader thread, so nothing is left reading a closed fd.
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        try:
            self._serial.close()
        except Exception:
            pass
        atexit.unregister(self.close)
