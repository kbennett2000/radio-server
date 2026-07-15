"""M17Link — the mrefd reflector backend that satisfies the Link protocol (ADR 0052).

The final piece of the M17 arc: it binds three parts that already exist and were reviewed on their
own — the :class:`~radio_server.link.m17.client.M17Client` UDP lifecycle (ADR 0051), the Codec2
mode-3200 seam (:class:`~radio_server.audio.codec2.Codec2`, ADR 0049), and the pure M17 wire codec
(:mod:`radio_server.link.m17.packet`, ADR 0050) — into one :class:`~radio_server.link.base.Link`,
registered as ``"m17"`` in ``create_link``. It invents nothing: its whole job is to obey the
protocol so the wired-and-reviewed machinery (``LinkFeeder``, ``LinkTxBridge``, ``TxLimiter``,
``TxSlot``, the arbiter) drives it unchanged.

It lives *above* the ``link/m17/`` leaf, beside :class:`~radio_server.link.mock.MockLink`, because
it imports ``radio_server`` types (audio, base, the codec) and the leaf's ADR-0050 purity guard
forbids that inside ``m17/``.

Two seams do the real work:

- **The sync ↔ async adapter.** The ``Link`` methods are synchronous and non-blocking (the machinery
  polls them, never awaits). ``M17Client`` is async. So ``connect``/``disconnect`` *schedule* the
  client's coroutines as tasks, and ``receive`` drains the client's frame queue with
  ``get_nowait()``.
- **The frame-rate impedance.** One M17 stream frame carries a 16-byte payload = two Codec2 3200
  frames = 40 ms of voice; canonical audio is 20 ms blocks @ 48 kHz. Inbound that is a clean 1:1
  (one stream frame → one 40 ms :class:`AudioFrame`; nothing downstream needs 20 ms granularity).
  Outbound it is a buffering boundary: accumulate a whole 40 ms, encode it, send one frame — and
  **fail loud on a partial frame at END** rather than pad or emit a half frame (see :meth:`stream`).
"""

from __future__ import annotations

import asyncio
from collections import deque

from ..audio import AudioFrame, AudioFormatMismatch
from ..audio.format import CANONICAL_FORMAT, CANONICAL_RATE
from .base import (
    SHARED_CAPS,
    LinkCapability,
    LinkStatus,
    Station,
    StreamEdge,
    UnsupportedLinkCapability,
)
from .m17.client import M17Client
from .m17.packet import FRAME_INDEX_MASK, META_BYTES, build_stream

#: One M17 stream frame carries 40 ms of voice (16-byte payload = 2 Codec2 3200 frames; ADR 0050).
M17_FRAME_MS = 40
#: Canonical samples in one 40 ms M17 frame: 48000 * 40 / 1000 = 1920.
_OUT_FRAME_SAMPLES = CANONICAL_RATE * M17_FRAME_MS // 1000
#: Canonical bytes in one 40 ms M17 frame: 1920 samples * 2 bytes = 3840. The outbound buffering
#: boundary — audio accrues until a whole one of these is ready to encode.
_OUT_FRAME_BYTES = _OUT_FRAME_SAMPLES * CANONICAL_FORMAT.frame_bytes

#: The LSF TYPE for a Codec2-3200 voice *stream*: stream bit (0) set + voice data type (bits 1-2 =
#: 0b10) → 0b101. The exact TYPE/DST-on-the-wire encoding is confirmed against a real reflector in the
#: bench cycle (guardrail 1); it does not affect the fake-reflector tests here.
_LSF_TYPE_VOICE_3200 = 0x0005

#: 14 zero bytes of LSF META (no encryption, no GPS) — the fixed metadata slot for a plain voice LSF.
_META = bytes(META_BYTES)


def _make_codec():
    """Construct the real Codec2 seam, imported lazily so it is never touched unless M17 is configured.

    Keeping the import local honors ADR 0049: the codec2 module is imported only when a configured
    M17 backend is built, and a missing ``libcodec2`` fails loud here (naming the library and the
    ``codec2`` extra), never at rest for a user who does not run M17.
    """
    from ..audio.codec2 import Codec2

    return Codec2()


class M17Link:
    """A :class:`~radio_server.link.base.Link` backend for an mrefd M17 reflector (ADR 0052).

    Constructed from config by ``create_link``/``create_app`` with the reflector's host/port/module,
    the station callsign (reused from ``station.callsign`` — there is no second callsign), and a bind
    address. Like :class:`MockLink` it is **born disabled**: nothing here starts it enabled and
    nothing is loaded from persistence, so a reboot always comes up disabled (ADR 0041). The socket
    is not opened until :meth:`connect`.
    """

    backend_name = "m17"

    def __init__(
        self,
        *,
        reflector_host: str,
        reflector_port: int,
        module: str,
        callsign: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        keepalive_timeout: float | None = None,
        connect_timeout: float | None = None,
        codec=None,
    ) -> None:
        self._reflector_host = reflector_host
        self._reflector_port = reflector_port
        self._module = module
        self._callsign = callsign
        self._bind_host = bind_host
        self._bind_port = bind_port
        # Forwarded to each M17Client (None → the client's published defaults, ADR 0051). Exposed so a
        # deployment can tune the keepalive and so a test can drive a fast LOST.
        self._keepalive_timeout = keepalive_timeout
        self._connect_timeout = connect_timeout
        # The codec is injectable for tests (the same seam shape as Codec2's monkeypatchable
        # find_library); the default path builds the real one and keeps the fail-loud contract.
        self._codec = codec if codec is not None else _make_codec()

        # Lifecycle. Born DISABLED + DISCONNECTED — the ADR-0041 safety property, structural: no
        # constructor path to enabled, nothing restored from persistence.
        self._enabled = False
        self._target: str | None = None
        self._listen_only = False
        self._client: M17Client | None = None
        self._connect_task: asyncio.Task | None = None

        # Inbound stream state: which stream is in flight, who is talking, and the pending events
        # receive() hands out one at a time (START, the decoded AudioFrame(s), END).
        self._in_stream_id: int | None = None
        self._talker: Station | None = None
        self._pending: deque = deque()

        # Outbound stream state: the buffer accruing toward a 40 ms frame, the current stream id and
        # frame number, and the one frame held back so the final one can carry the end-of-stream bit.
        self._streaming = False
        self._out_buf = bytearray()
        self._out_stream_id = 0
        self._out_fn = 0
        self._held: bytes | None = None
        self._stream_seq = 0

    # --- shared surface --------------------------------------------------------------------------

    def enable(self, on: bool) -> None:
        """Set the master enable gate. Enabling does NOT connect — ``/link/enable`` is separate from
        ``/link/connect`` (ADR 0041); this only flips the gate the feeder/bridge read."""
        self._enabled = on

    def connect(self, target: str) -> None:
        """Join the configured reflector. Schedules the async handshake and returns immediately.

        A fresh :class:`M17Client` is built each connect so a preceding :meth:`set_listen_only` picks
        ``LSTN`` vs ``CONN``. The synchronous protocol method cannot await the coroutine, so the
        handshake runs as a task on the current loop; :meth:`status` reflects it once it completes.
        """
        self._target = target
        loop = asyncio.get_running_loop()
        if self._client is not None:
            loop.create_task(self._client.close())
        kwargs = {}
        if self._keepalive_timeout is not None:
            kwargs["keepalive_timeout"] = self._keepalive_timeout
        if self._connect_timeout is not None:
            kwargs["connect_timeout"] = self._connect_timeout
        self._client = M17Client(
            reflector_host=self._reflector_host,
            reflector_port=self._reflector_port,
            module=self._module,
            callsign=self._callsign,
            bind_host=self._bind_host,
            bind_port=self._bind_port,
            listen_only=self._listen_only,
            **kwargs,
        )
        self._connect_task = loop.create_task(self._client.connect())

    def disconnect(self) -> None:
        """Drop the current connection (schedules the async teardown) and reset stream state."""
        client = self._client
        self._client = None
        self._target = None
        self._reset_inbound()
        if client is not None:
            asyncio.get_running_loop().create_task(client.close())

    def transmit(self, audio: AudioFrame) -> None:
        """Send a frame OUT to the reflector: buffer it and emit whole 40 ms M17 stream frames.

        Fail-loud format check first (like :class:`MockLink`), before anything else. Then, only while
        a stream is open and the client is connected, accrue the audio and encode each complete 40 ms
        chunk to a 16-byte Codec2 payload, holding the most recent frame back so :meth:`stream`'s END
        can mark the final one (see :meth:`_emit`).
        """
        if audio.format != CANONICAL_FORMAT:
            raise AudioFormatMismatch(
                f"M17 link transmits {CANONICAL_FORMAT}, got a frame in {audio.format}"
            )
        client = self._client
        if not (self._streaming and client is not None and client.connected):
            return
        self._out_buf.extend(audio.samples)
        while len(self._out_buf) >= _OUT_FRAME_BYTES:
            chunk = bytes(self._out_buf[:_OUT_FRAME_BYTES])
            del self._out_buf[:_OUT_FRAME_BYTES]
            payload = self._codec.encode(AudioFrame(chunk, CANONICAL_FORMAT))
            if self._held is not None:
                self._emit(self._held, last=False)
            self._held = payload

    def stream(self, on: bool) -> None:
        """Open (``True``) or close (``False``) an outbound transmission — the M17 LSF/EOT bracket.

        ``stream(True)`` begins a stream (a new stream id + frame counter; every M17 stream frame
        carries the LSF). ``stream(False)`` is the EOT: it marks the final held frame with the
        end-of-stream bit. If a partial (sub-40 ms) buffer remains, it **raises** — a real M17 voice
        feed is 40 ms-aligned, and the cycle mandate is to fail loud rather than emit a half frame or
        silence-pad audio the operator never spoke onto the reflector.
        """
        if on:
            self._out_stream_id = self._next_stream_id()
            self._out_fn = 0
            self._held = None
            self._out_buf.clear()
            self._streaming = True
            return

        if not self._streaming:
            return
        partial = len(self._out_buf)
        held = self._held
        # Reset outbound state before doing anything that can raise, so a failed stream never wedges
        # the next one.
        self._streaming = False
        self._held = None
        self._out_buf.clear()
        if partial:
            raise ValueError(
                f"M17 outbound stream ended with a partial {partial}-byte frame; a 40 ms M17 frame "
                f"needs {_OUT_FRAME_BYTES} bytes. Refusing to pad or emit a half frame (ADR 0052)."
            )
        if held is not None:
            self._emit(held, last=True)

    def receive(self) -> AudioFrame | StreamEdge | None:
        """Pull the next inbound event: an :class:`AudioFrame`, a :class:`StreamEdge`, or ``None``.

        Synchronous and non-blocking. Drains the client's frame queue with ``get_nowait()`` and
        converts each M17 stream frame into ``START`` (once per stream) → decoded ``AudioFrame`` →
        ``END`` (on the end-of-stream bit). A ``LOST`` connection enqueues no frame, so this can never
        synthesize an ``END`` on loss (ADR 0051's line, held) — loss shows only via
        ``status().connected``.
        """
        item = self._next_event()
        if item is StreamEdge.END:
            self._reset_inbound()
        return item

    def status(self) -> LinkStatus:
        client = self._client
        return LinkStatus(
            backend=self.backend_name,
            enabled=self._enabled,
            connected=bool(client is not None and client.connected),
            target=self._target,
            stations=(),  # M17 has no directory; who's on is not enumerable
            talker=self._talker,
        )

    def capabilities(self) -> frozenset[LinkCapability]:
        # LISTEN_ONLY yes (the LSTN tier — the reason M17 was chosen); DIRECTORY no (M17 has no
        # central user database — the callsign is the identity).
        return frozenset(SHARED_CAPS | {LinkCapability.LISTEN_ONLY})

    # --- capability-gated operations -------------------------------------------------------------

    def directory(self) -> tuple[Station, ...]:
        # M17 has no directory. Raise by name so the API 501s by name, never a silent empty list.
        raise UnsupportedLinkCapability(LinkCapability.DIRECTORY)

    def set_listen_only(self, on: bool) -> None:
        """Enter/leave protocol-level listen-only mode. Takes effect on the next :meth:`connect`
        (which then sends ``LSTN`` instead of ``CONN``)."""
        self._listen_only = on

    @property
    def listen_only(self) -> bool:
        """Whether the next connect will request the listen-only (LSTN) tier (inspectable by tests)."""
        return self._listen_only

    # --- inbound helpers -------------------------------------------------------------------------

    def _next_event(self) -> AudioFrame | StreamEdge | None:
        if self._pending:
            return self._pending.popleft()
        client = self._client
        if client is None:
            return None
        try:
            frame = client.frames.get_nowait()
        except asyncio.QueueEmpty:
            return None
        self._ingest(frame)
        return self._pending.popleft() if self._pending else None

    def _ingest(self, frame) -> None:
        """Turn one parsed M17 stream frame into the START / AudioFrame / END events it implies."""
        if frame.stream_id != self._in_stream_id:
            self._in_stream_id = frame.stream_id
            self._talker = Station(frame.src) if frame.src else None
            self._pending.append(StreamEdge.START)
        self._pending.append(self._codec.decode(frame.payload))
        if frame.last:
            self._pending.append(StreamEdge.END)

    def _reset_inbound(self) -> None:
        self._in_stream_id = None
        self._talker = None

    # --- outbound helpers ------------------------------------------------------------------------

    def _next_stream_id(self) -> int:
        # A per-transmission 16-bit id from a counter (deterministic/testable); never 0.
        self._stream_seq = (self._stream_seq % 0xFFFF) + 1
        return self._stream_seq

    def _emit(self, payload: bytes, *, last: bool) -> None:
        """Build one 54-byte M17 stream frame and hand it to the client's socket.

        ``build_stream`` validates the payload is exactly 16 bytes, so a resampler change that broke
        the 40 ms → 16-byte invariant would fail loud here rather than send a malformed frame.
        """
        client = self._client
        if client is None:
            return
        data = build_stream(
            self._out_stream_id,
            self._module,  # dst = the target module (ADR 0052)
            self._callsign,  # src = this station — the talker on the far end
            _LSF_TYPE_VOICE_3200,
            _META,
            self._out_fn,
            payload,
            last=last,
        )
        self._out_fn = (self._out_fn + 1) & FRAME_INDEX_MASK
        client.send_stream_frame(data)
