"""MockLink — the software-only Link backend the whole stack is built against (ADR 0041).

Mirrors :class:`~radio_server.backends.mock.MockRadio`: records transmitted audio to an inspectable
``tx_log``, serves scripted inbound audio (then ``None`` when the network is idle), and fakes
peers/talkers. Its capability set is **toggleable** — ``directory`` and ``listen_only`` are
independent bools — so the directory-vs-no-directory and listen-only splits are both exercisable with
no network.

Two things it enforces structurally, not by convention:

- **Fail-loud TX format** — ``transmit`` rejects a wrong-format frame before recording anything.
- **The enable safety property (ADR 0041)** — a ``MockLink`` is *always born disabled*. There is no
  constructor argument that starts it enabled and nothing loads ``enabled`` from persistence; the
  only path to ``enabled=True`` is a deliberate :meth:`enable`. This forbids, at the leaf, the
  autostart×sticky-enable composition that would otherwise put the transmitter on the internet
  unattended from power-on.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from ..audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from .base import (
    SHARED_CAPS,
    LinkCapability,
    LinkStatus,
    Station,
    StreamEdge,
    UnsupportedLinkCapability,
)


class MockLink:
    """A hardware-free :class:`~radio_server.link.base.Link` backend for tests and mock-first builds."""

    backend_name = "mock"

    def __init__(
        self,
        *,
        directory: bool = True,
        listen_only: bool = True,
        rx_frames: Iterable[AudioFrame] | None = None,
        canned_rx: AudioFrame | None = None,
        format: AudioFormat = CANONICAL_FORMAT,
        stations: Iterable[Station] | None = None,
        talker: Station | None = None,
        directory_entries: Iterable[Station] | None = None,
    ):
        # Capability toggles — orthogonal, so both splits are reachable from one mock.
        self._directory = directory
        self._listen_only = listen_only
        self.format = format

        #: Frames returned FIFO by receive(), then `canned_rx`. Public so a test can enqueue mid-run.
        self._rx_frames: deque[AudioFrame] = deque(rx_frames or ())
        #: The idle fallback receive() returns once `rx_frames` drains. `None` models a quiet network.
        self.canned_rx = canned_rx

        #: Every outbound event in order — each frame passed to transmit() plus the StreamEdge.START/
        #: END boundaries from stream() (ADR 0044) — so a test sees the frames bracketed by one
        #: open/close pair. Frames-only when stream() is never called (the MockRadio tx_log mirror).
        self.tx_log: list[AudioFrame | StreamEdge] = []

        # Link lifecycle. ALWAYS born disabled + disconnected — the safety property (no born-enabled
        # path, nothing restored from persistence). Only enable()/connect() move these.
        self._enabled = False
        self._connected = False
        self._target: str | None = None

        #: Who is on / who is talking — fakeable, mutable like MockRadio.busy_frequencies.
        self.stations: list[Station] = list(stations or ())
        self.talker: Station | None = talker

        # Directory contents (only reachable when DIRECTORY is advertised) and the listen-only flag.
        self._directory_entries: tuple[Station, ...] = tuple(directory_entries or ())
        self._listening_only = False

    # --- shared surface --------------------------------------------------------------------------

    def enable(self, on: bool) -> None:
        self._enabled = on

    def connect(self, target: str) -> None:
        self._connected = True
        self._target = target

    def disconnect(self) -> None:
        self._connected = False
        self._target = None

    def transmit(self, audio: AudioFrame) -> None:
        if audio.format != self.format:
            raise AudioFormatMismatch(
                f"link accepts {self.format}, got a frame in {audio.format}"
            )
        self.tx_log.append(audio)

    def stream(self, on: bool) -> None:
        # Record the stream boundary (LSF/EOT) inline in tx_log so a test can assert the frames are
        # bracketed by one START/END. No format to check — this is framing, not payload.
        self.tx_log.append(StreamEdge.START if on else StreamEdge.END)

    def receive(self) -> AudioFrame | None:
        # Serve the scripted sequence FIFO, then the idle fallback (`None` by default = quiet network).
        if self._rx_frames:
            return self._rx_frames.popleft()
        return self.canned_rx

    def script_rx(self, *frames: AudioFrame) -> None:
        """Enqueue frames receive() will return in order before falling back to `canned_rx`."""
        self._rx_frames.extend(frames)

    def status(self) -> LinkStatus:
        return LinkStatus(
            backend=self.backend_name,
            enabled=self._enabled,
            connected=self._connected,
            target=self._target,
            stations=tuple(self.stations),
            talker=self.talker,
        )

    def capabilities(self) -> frozenset[LinkCapability]:
        caps = set(SHARED_CAPS)
        if self._directory:
            caps.add(LinkCapability.DIRECTORY)
        if self._listen_only:
            caps.add(LinkCapability.LISTEN_ONLY)
        return frozenset(caps)

    # --- capability-gated operations -------------------------------------------------------------

    def _require(self, capability: LinkCapability) -> None:
        if capability not in self.capabilities():
            raise UnsupportedLinkCapability(capability)

    def directory(self) -> tuple[Station, ...]:
        self._require(LinkCapability.DIRECTORY)
        return self._directory_entries

    def set_listen_only(self, on: bool) -> None:
        self._require(LinkCapability.LISTEN_ONLY)
        self._listening_only = on

    @property
    def listening_only(self) -> bool:
        """Whether protocol-level listen-only is engaged (inspectable by tests)."""
        return self._listening_only
