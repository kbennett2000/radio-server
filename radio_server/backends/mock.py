"""MockRadio — the software-only backend the whole stack is tested against.

Records transmitted audio to an inspectable buffer, serves canned RX, and fakes
status/busy. Reports full capabilities by default; construct with
``supports_cat=False`` to model an audio-only (Baofeng-like) radio and exercise the
capability split without hardware.
"""

from __future__ import annotations

from .base import (
    CAT_CAPS,
    FULL_CAPS,
    SHARED_CAPS,
    AudioFormat,
    AudioFormatMismatch,
    AudioFrame,
    Capability,
    RadioStatus,
    UnsupportedCapability,
)
from ..audio import CANONICAL_FORMAT


class MockRadio:
    """In-memory :class:`~radio_server.backends.base.Radio` /
    :class:`~radio_server.backends.base.CatRadio` implementation.

    Args:
        supports_cat: When ``True`` (default) the mock advertises and implements the
            CAT tuning methods. When ``False`` it drops the CAT capabilities and its
            CAT methods raise :class:`UnsupportedCapability`.
        canned_rx: Frame returned by :meth:`receive`. Settable later via
            :attr:`canned_rx`. Defaults to an empty canonical-format frame.
        busy: Initial channel-busy (squelch-open) state reported by :meth:`status`.
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
        busy: bool = False,
        format: AudioFormat = CANONICAL_FORMAT,
    ):
        self.supports_cat = supports_cat
        self.format = format
        self.canned_rx = canned_rx if canned_rx is not None else AudioFrame(b"", format)
        self.busy = busy

        #: Every chunk passed to :meth:`transmit`, in order — inspectable by tests.
        self.tx_log: list[AudioFrame] = []
        self._transmitting = False

        # CAT state, only meaningful when supports_cat.
        self._frequency: int | None = None
        self._channel: int | None = None
        self._tone: float | None = None
        self._mode: str | None = None
        self._scanning = False

    # --- shared surface -------------------------------------------------------

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != self.format:
            raise AudioFormatMismatch(
                f"radio accepts {self.format}, got a frame in {audio.format}"
            )
        self._transmitting = True
        try:
            self.tx_log.append(audio)
        finally:
            # A real transmit blocks for the audio duration; the mock returns to
            # receive immediately.
            self._transmitting = False

    def receive(self) -> AudioFrame:
        return self.canned_rx

    def ptt(self, on: bool) -> None:
        self._transmitting = on

    def status(self) -> RadioStatus:
        if self.supports_cat:
            return RadioStatus(
                backend=self.backend_name,
                transmitting=self._transmitting,
                busy=self.busy,
                frequency=self._frequency,
                channel=self._channel,
                tone=self._tone,
                mode=self._mode,
            )
        return RadioStatus(
            backend=self.backend_name,
            transmitting=self._transmitting,
            busy=self.busy,
        )

    def capabilities(self) -> frozenset[Capability]:
        return FULL_CAPS if self.supports_cat else SHARED_CAPS

    # --- CAT surface (V71-only) ----------------------------------------------

    def _require_cat(self, capability: Capability) -> None:
        if not self.supports_cat:
            raise UnsupportedCapability(capability)

    def set_frequency(self, hz: int) -> None:
        self._require_cat(Capability.SET_FREQUENCY)
        self._frequency = hz

    def set_channel(self, n: int) -> None:
        self._require_cat(Capability.SET_CHANNEL)
        self._channel = n

    def set_tone(self, tone: float | None) -> None:
        self._require_cat(Capability.SET_TONE)
        self._tone = tone

    def set_mode(self, mode: str) -> None:
        self._require_cat(Capability.SET_MODE)
        self._mode = mode

    def scan(self, on: bool) -> None:
        self._require_cat(Capability.SCAN)
        self._scanning = on

    @property
    def scanning(self) -> bool:
        """Whether a scan is in progress (inspectable by tests)."""
        return self._scanning
