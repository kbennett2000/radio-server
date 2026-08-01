"""MockRadio — the software-only backend the whole stack is tested against.

Records transmitted audio to an inspectable buffer, serves canned RX, and fakes
status/busy. Reports full capabilities by default; construct with
``supports_cat=False`` to model an audio-only (Baofeng-like) radio and exercise the
capability split without hardware.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .base import (
    CAT_CAPS,
    FULL_CAPS,
    SHARED_CAPS,
    AudioFormat,
    AudioFormatMismatch,
    AudioFrame,
    BroadcastFm,
    Capability,
    RadioStatus,
    UnsupportedCapability,
    refuse_if_deafened,
)
from ..audio import CANONICAL_FORMAT
# The pure wire codec, imported for its ONE validator: `SetBroadcastFm` is where
# "refuse, never round" lives, and a second copy of the 100 kHz raster in this file is a
# copy that drifts. `frames` imports nothing and the `uvk5` package __init__ is docs only.
from .uvk5.frames import SetBroadcastFm


class MockRadio:
    """In-memory :class:`~radio_server.backends.base.Radio` /
    :class:`~radio_server.backends.base.CatRadio` implementation.

    Args:
        supports_cat: When ``True`` (default) the mock advertises and implements the
            CAT tuning methods. When ``False`` it drops the CAT capabilities and its
            CAT methods raise :class:`UnsupportedCapability`.
        canned_rx: Frame returned by :meth:`receive` once any scripted RX sequence is
            exhausted. Settable later via :attr:`canned_rx`. Defaults to an empty
            canonical-format frame.
        rx_frames: An optional sequence of frames :meth:`receive` returns in order (FIFO),
            one per call, before falling back to :attr:`canned_rx`. Extend it later via
            :meth:`script_rx` — the RX mirror of the :attr:`tx_log` convention, letting an
            audio-streaming test drive a deterministic received-audio sequence.
        busy: Initial channel-busy (squelch-open) state reported by :meth:`status`.
            Applied unconditionally (a flat "the squelch is open" flag).
        busy_frequencies: Frequencies (Hz) that read as busy *when tuned to them* — the
            hook a scan-engine test uses to script "channel X is busy." Stored as a
            public mutable :attr:`busy_frequencies` set, so a test can drop the carrier
            mid-scan with ``radio.busy_frequencies.discard(hz)``. Independent of the flat
            :attr:`busy` flag (either being true reports busy). Only meaningful with CAT
            (an audio-only radio never tunes, so ``_frequency`` is always ``None``).
        format: The audio format this radio accepts. :meth:`transmit` fails loud
            (:class:`AudioFormatMismatch`) on a frame of any other format — the mock
            enforces the same format contract a real sound card imposes.
    """

    backend_name = "mock"

    def __init__(
        self,
        *,
        supports_cat: bool = True,
        canned_rx: AudioFrame | None = None,
        rx_frames: Iterable[AudioFrame] | None = None,
        busy: bool = False,
        busy_frequencies: Iterable[int] | None = None,
        format: AudioFormat = CANONICAL_FORMAT,
        left_in_broadcast_fm: bool = False,
        broadcast_fm_hz: int = 103_200_000,
    ):
        self.supports_cat = supports_cat
        #: Seeds the second receiver already running — the state a server walks into after a crash
        #: mid-broadcast-FM, since the firmware persists it to flash behind the host (ADR 0157).
        #: Left as a knob rather than a fixture so the API/UI cycles that follow can drive the
        #: dangerous case without hardware, which is what CLAUDE.md's mock-first rule requires.
        self._broadcast_fm_hz = broadcast_fm_hz
        #: The BK1080 band the modelled receiver is on (ADR 0164). 0 = 87.5-108 MHz, the
        #: band every realistic host asks for and the one the seeded `hz` sits in.
        self._broadcast_fm_band = 0
        self.format = format
        self.canned_rx = canned_rx if canned_rx is not None else AudioFrame(b"", format)
        #: Scripted RX frames returned FIFO by :meth:`receive`, then :attr:`canned_rx`. Public so
        #: a test can enqueue mid-run; the RX mirror of :attr:`tx_log`.
        self._rx_frames: deque[AudioFrame] = deque(rx_frames or ())
        self.busy = busy
        #: Frequencies (Hz) that report busy while tuned to them — mutable so a scan test
        #: can script activity appearing/clearing per channel.
        self.busy_frequencies: set[int] = set(busy_frequencies or ())

        #: Every chunk passed to :meth:`transmit`, in order — inspectable by tests.
        self.tx_log: list[AudioFrame] = []
        self._transmitting = False

        # CAT state, only meaningful when supports_cat.
        self._frequency: int | None = None
        self._tx_frequency: int | None = None
        self._channel: int | None = None
        self._tone: float | None = None
        self._mode: str | None = None
        # Starts unset, like tone/mode: the mock has not been told a level and does not invent one.
        self._power: str | None = None
        # `None` until asserted, like every other CAT field here: the mock reports what it was
        # told, never a default standing in for a radio nobody has asked (ADR 0132/0150).
        self._modulation: str | None = None
        self._tx_ok: bool | None = None
        # The second receiver (ADR 0157). `None` until this mock is told, for the same reason —
        # and this one is modelled as state rather than a recorded call because the fault it stands
        # for is a state: a radio left in broadcast FM hears nothing and transmits anyway.
        self._broadcast_fm: BroadcastFm | None = (
            BroadcastFm(on=True, hz=broadcast_fm_hz, band=0)
            if left_in_broadcast_fm
            else None
        )
        self._scanning = False

    # --- shared surface -------------------------------------------------------

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != self.format:
            raise AudioFormatMismatch(
                f"radio accepts {self.format}, got a frame in {audio.format}"
            )
        # AFTER the format check, deliberately: the bridges' `except` ladders are ordered, and
        # AudioFormatMismatch has to keep taking its own named path rather than being absorbed into
        # a key refusal (`test_mumble_malformed_frame_still_takes_the_named_path`).
        refuse_if_deafened(self._broadcast_fm)
        self._transmitting = True
        try:
            self.tx_log.append(audio)
        finally:
            # A real transmit blocks for the audio duration; the mock returns to
            # receive immediately.
            self._transmitting = False

    def script_rx(self, *frames: AudioFrame) -> None:
        """Enqueue frames :meth:`receive` will return in order before :attr:`canned_rx`."""
        self._rx_frames.extend(frames)

    def receive(self) -> AudioFrame:
        # Serve the scripted sequence FIFO, then fall back to the static canned frame.
        if self._rx_frames:
            return self._rx_frames.popleft()
        return self.canned_rx

    def ptt(self, on: bool) -> None:
        # Inside `if on`, never around it. Refusing to STOP is the dangerous direction in a
        # transmitter: a redundant unkey is harmless, a missed one is a stuck key (ADR 0090/0099),
        # so the unkey path stays unconditional the way ADR 0093 requires.
        if on:
            refuse_if_deafened(self._broadcast_fm)
        self._transmitting = on

    def close(self) -> None:
        """No-op: the mock owns no device to release. Present so it is a faithful ``Radio`` double for
        the diagnostics that open-then-``close()`` a backend (doctor ``--rx-level`` / ``--dtmf`` /
        ``--rx-capture``)."""

    def _is_busy(self) -> bool:
        # The flat flag OR the currently-tuned frequency being scripted busy. The
        # membership test is inert on an audio-only radio (``_frequency`` is None).
        return self.busy or self._frequency in self.busy_frequencies

    def status(self) -> RadioStatus:
        if self.supports_cat:
            return RadioStatus(
                backend=self.backend_name,
                transmitting=self._transmitting,
                busy=self._is_busy(),
                frequency=self._frequency,
                tx_frequency=self._tx_frequency,
                channel=self._channel,
                tone=self._tone,
                mode=self._mode,
                power=self._power,
                modulation=self._modulation,
                tx_ok=self._tx_ok,
                broadcast_fm=self._broadcast_fm,
                transport=self.transport_health(),
            )
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._transmitting,
            busy=self._is_busy(),
            transport=self.transport_health(),
        )

    def transport_health(self):
        """``None`` — a software radio has no serial link to be broken (ADR 0166).

        Not ``TransportHealth(alive=True)``: "there is nothing here to report on" and "the link is
        up" are different claims, and a mock that reported a healthy transport would be inventing a
        measurement — the same wrong answer `broadcast_fm` refuses to give. Tests that need a
        modelled transport assign over this.
        """
        return None

    def clear_broadcast_fm(self) -> bool:
        """Switch the modelled second receiver off; return whether it is now off (ADR 0157).

        Implemented rather than left out, because `capabilities()` returns `FULL_CAPS` on the CAT
        path and a capability advertised with no method behind it is exactly the lie guardrail 3
        forbids. Modelling a *command* is not the same as inventing a *measurement*: this mock
        declines to fake a PA reading for the same reason it is happy to fake a switch.

        `left_in_broadcast_fm` (constructor) seeds the state a server walks into after a crash, so
        the API and UI cycles that follow can drive the dangerous case without hardware.
        """
        self._require_cat(Capability.CLEAR_BROADCAST_FM)
        self._broadcast_fm = BroadcastFm(
            on=False, hz=self._broadcast_fm_hz, band=self._broadcast_fm_band
        )
        return True

    def set_broadcast_fm(self, action: str, hz: int | None, band: int) -> bool:
        """Switch the modelled second receiver **on**, or retune it; return whether it is now on.

        Implemented for `clear_broadcast_fm`'s reason — `capabilities()` returns `FULL_CAPS` on the
        CAT path, and a capability advertised with no method behind it is the lie guardrail 3
        forbids — and, more usefully, because CLAUDE.md's mock-first rule means the route, the relay
        mute's arming order and every refusal have to be provable without hardware.

        **It builds the real frame to validate.** `SetBroadcastFm.__post_init__` is where "refuse,
        never round" lives, and a mock with its own copy of the 100 kHz raster is a copy that drifts
        — which is the exact hazard `dock.h` cites for keeping the band tables in one place. So this
        constructs the frame it would have sent and throws it away: the mock refuses precisely what
        the wire refuses, and cannot silently diverge. Importing the codec costs nothing at runtime
        (`frames` is pure, and the `uvk5` package `__init__` has no imports of its own).

        What it deliberately does **not** model is `ERR_TX`/`ERR_OFF`/`ERR_BAND`. Those are firmware
        state — `gCurrentFunction`, `gFmRadioMode`, the BK1080's limit tables — and a mock that
        invented them would be inventing measurements (ADR 0163 finding 4: the whole `ERR_TX` class
        is invisible to pytest by construction). Tests that need them stub the method.
        """
        self._require_cat(Capability.SET_BROADCAST_FM)
        frame = SetBroadcastFm(action=action, hz=hz, band=band)
        self._broadcast_fm_hz, self._broadcast_fm_band = frame.hz, frame.band
        self._broadcast_fm = BroadcastFm(
            on=True, hz=frame.hz, band=frame.band, blocks_tx=None
        )
        return True

    def capabilities(self) -> frozenset[Capability]:
        return FULL_CAPS if self.supports_cat else SHARED_CAPS

    # --- CAT surface (V71-only) ----------------------------------------------

    def _require_cat(self, capability: Capability) -> None:
        if not self.supports_cat:
            raise UnsupportedCapability(capability)

    def set_frequency(self, hz: int) -> None:
        self._require_cat(Capability.SET_FREQUENCY)
        # Tuning disarms any split — the same fail-safe every real backend applies (ADR 0133).
        self._frequency, self._tx_frequency = hz, None

    def set_channel(self, n: int) -> None:
        self._require_cat(Capability.SET_CHANNEL)
        self._channel = n

    def set_split(self, tx_hz: int | None) -> None:
        self._require_cat(Capability.SET_SPLIT)
        self._tx_frequency = tx_hz

    def set_tone(self, tone: float | None) -> None:
        self._require_cat(Capability.SET_TONE)
        self._tone = tone

    def set_mode(self, mode: str) -> None:
        """Set the channel **bandwidth** — ``FM`` (wide) or ``NFM`` (narrow). Not
        :meth:`set_modulation`, which is the demodulator (ADR 0150/0154).

        Validated here rather than accepted blindly, for the reason :meth:`set_power` and
        :meth:`set_modulation` give: the mock is what every API and UI test runs against, so a
        double that swallowed ``"AM"`` lets a 422 the real backend returns go untested. It did,
        for seventeen ADRs — ``POST /mode {"mode": "AM"}`` answered 200 in the suite and 500 on
        the bench (ADR 0160 finding 13, closed by ADR 0172).

        The accepted set is the **intersection** the whole fleet takes (`AiocBaofeng`, kv4p's
        ``_MODE_TO_BW``, and the canonical half of uvk5's ``_MODE_ALIASES``), and it is
        deliberately narrower than uvk5 alone: that backend also takes ``WIDE``/``NARROW``, which
        appear nowhere else in this project and which no route or config path can produce. A test
        double must be a LOWER bound on what the fleet accepts, never a superset of one backend,
        or a green test here can be a lie on the other two.

        Spelled inline rather than imported from :data:`radio_server.presets.VALID_MODES` — the
        same rule :data:`~radio_server.presets.POWER_LEVELS` and
        :data:`~radio_server.presets.CTCSS_TONES` follow, and here it is not merely style: this
        module is imported by ``backends/__init__``, which ``presets`` imports on its own first
        line, so the reverse import is circular and fails whenever ``presets`` is the entry point.
        ``test_presets`` pins the two sets equal instead.
        """
        self._require_cat(Capability.SET_MODE)
        text = str(mode).strip().upper()
        if text not in ("FM", "NFM"):
            raise ValueError(f"mode must be FM or NFM, got {mode!r}")
        self._mode = text

    def set_power(self, level: str) -> None:
        self._require_cat(Capability.SET_POWER)
        # Validated here rather than accepted blindly, because the mock is what every API and UI
        # test runs against: a double that swallows "vhigh" would let a 422 the real backend
        # returns go untested (CLAUDE.md — no feature should need hardware to be testable).
        text = str(level).strip().lower()
        if text not in ("low", "mid", "high"):
            raise ValueError(f"power must be low, mid or high, got {level!r}")
        self._power = text

    def set_modulation(self, modulation: str) -> None:
        """Set the demodulator — ``FM`` or ``AM`` (ADR 0150). Not :meth:`set_mode`, which is
        bandwidth.

        Validated here rather than accepted blindly, for the reason :meth:`set_power` gives: the
        mock is what every API and UI test runs against, so a double that swallowed ``"SSB"``
        would let a 422 the real backend returns go untested.

        ``tx_ok`` follows the firmware's actual rule — a UV-K5 built without ``ENABLE_TX_WHEN_AM``
        refuses its own PTT in anything but FM — so tests can see both states. The mock does
        **not** then refuse :meth:`transmit` or :meth:`ptt`: that refusal belongs to the backend
        whose keying runs through the radio's PTT pin (`AiocBaofeng`), and there is no radio here
        to decline.

        Since ADR 0158 this mock *does* refuse a key-up on broadcast FM, and the line between the
        two is deliberate rather than an inconsistency. The AM refusal is a **prediction about
        hardware** — what `VFO_STATE_TX_DISABLE` will do to a PTT pin this mock does not have.
        The broadcast-FM refusal is **server policy** about a state this mock genuinely models, and
        CLAUDE.md's mock-first rule is precisely that server policy must be drivable without
        hardware: every bridge, controller and browser test that has to see a station refuse to
        transmit blind reaches it through `left_in_broadcast_fm`.
        """
        self._require_cat(Capability.SET_MODULATION)
        text = str(modulation).strip().upper()
        if text not in ("FM", "AM"):
            raise ValueError(f"modulation must be FM or AM, got {modulation!r}")
        self._modulation = text
        self._tx_ok = text == "FM"

    def scan(self, on: bool) -> None:
        self._require_cat(Capability.SCAN)
        self._scanning = on

    @property
    def scanning(self) -> bool:
        """Whether a scan is in progress (inspectable by tests)."""
        return self._scanning
