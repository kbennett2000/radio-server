"""TX audio ingest: the keying/ingest state machine behind the ``/audio/tx`` WebSocket (ADR 0016).

This is the mirror of the cycle-13 RX path (``radio_server.rx``), in the opposite direction: a
LAN client streams live audio *in* and the server feeds it to ``radio.transmit()`` — "talk through
the gateway." Where RX is fan-out (one radio's audio to many listeners, via ``AudioHub``), TX is
fan-in and *serialized*: one radio, one talker at a time. So there is no hub and no background
pump here — just the per-connection state machine plus a single-talker guard.

Two load-bearing concerns the RX path never had:

- **Keying discipline (guardrail 2):** PTT is asserted for the *duration* of the inbound stream
  (``ptt(True)`` on the first frame) and dropped when it ends, stalls, or errors (``ptt(False)``)
  — **never** via a CAT ``TX`` command. :class:`TxSession` owns exactly that lifecycle.
- **Dead-connection timeout:** a client that stops sending without a clean close must not hold the
  transmitter keyed forever. :meth:`TxSession.idle_elapsed` is the pure, clock-injected decision;
  the endpoint's ``asyncio.wait_for`` supplies the wakeup. The clock is injectable so the timeout
  is exactly testable with a fake clock (no real sleeps) — the same convention ``activity`` /
  ``scan`` use.

Everything here is pure state + validation on canonical PCM (48k/s16le/mono); no hardware. The
idle-timeout **value** is a bench-tuned fact (guardrail 1); the marked default below is a starting
point to verify against hardware.

This package deliberately imports only ``..audio`` and ``..backends`` (the arrow is
``tx -> {audio, backends}``), never ``rx`` or ``api``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from ..arbiter import RadioArbiter
from ..audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from ..backends import Radio

if TYPE_CHECKING:
    from ..config import Settings


class TxRecorder(Protocol):
    """A passive sink for transmitted audio — the TX recorder seam (ADR 0021).

    :class:`TxSession` calls :meth:`write` for each fed frame and :meth:`end_segment` on key-down
    (``close``), so one keyed stream is one recording segment. The default :data:`null_recorder`
    does nothing; the concrete :class:`radio_server.recording.Recorder` (with a ``tx-`` prefix)
    implements this shape without ``tx`` importing ``recording`` (the arrow stays
    ``tx -> {audio, backends}``). Mirrors ``rx.pump.RxRecorder``.
    """

    def write(self, pcm: bytes) -> None: ...

    def end_segment(self) -> None: ...


class TxIdentifier(Protocol):
    """The streaming station-ID seam (ADR 0041, guardrail 5 / Part 97).

    Streaming TX (this session — the browser ``/audio/tx`` talker and the Mumble bridge) has no
    "whole over" to prepend the ID to the way the one-shot dispatcher does, so it consults an
    identifier that *renders* ID audio on demand at the key-up, mid-over, and key-down edges; the
    session transmits the returned frame into the same keyed over. Each method returns the ID audio
    to send now, or ``None`` when no ID is due. The concrete implementation
    (:class:`radio_server.services.station_id.StreamingId`) lives outside ``tx`` so the dependency
    arrow stays ``tx -> {audio, backends}`` — exactly the Protocol-here / concrete-elsewhere split
    :class:`TxRecorder` uses.
    """

    def key_up_id(self, now: float | None = None) -> AudioFrame | None: ...

    def periodic_id(self, now: float | None = None) -> AudioFrame | None: ...

    def sign_off_id(self, now: float | None = None) -> AudioFrame | None: ...


class _NullRecorder:
    """The no-op default TX recorder: records nothing (TX recording is opt-in)."""

    def write(self, pcm: bytes) -> None: ...

    def end_segment(self) -> None: ...


#: The default TX recorder — a shared no-op, so an un-injected session behaves exactly as before.
null_recorder = _NullRecorder()

#: A clock returns seconds as a float. Injectable so the idle timer is exactly testable with a
#: fake clock (no real sleeps). Defined locally rather than imported from ``auth`` so this
#: package's dependency arrow stays ``tx -> {audio, backends}`` only — the same call ``activity``
#: and ``scan`` make (``time.monotonic`` by default: elapsed-interval timing).
Clock = Callable[[], float]


# --- config (guardrail 1: marked default, verify against hardware) -------------------------

#: Seconds a *keyed* stream may go silent (no inbound frame) before PTT is force-dropped, so a
#: dead TCP connection cannot hold the transmitter keyed. VERIFY AGAINST HARDWARE (guardrail 1) —
#: the real value trades off the frame cadence against PTT-tail latency: it must comfortably
#: exceed the inter-frame gap (~20 ms at typical framing) yet be short enough to release TX
#: promptly when a client dies. A bench-tuned fact, not a confirmed value.
DEFAULT_TX_IDLE_TIMEOUT = 2.0

RADIO_TX_IDLE_TIMEOUT_ENV_VAR = "RADIO_TX_IDLE_TIMEOUT"


def parse_tx_format(header: Mapping[str, object]) -> AudioFormat:
    """Parse a client's format-declaration header into an :class:`AudioFormat`, fail loud.

    The TX socket carries raw binary PCM with no per-frame format tag, so the client declares its
    format up front (``{"rate": 48000, "width": 2, "channels": 1}``). This builds the declared
    :class:`AudioFormat` and requires it to equal :data:`CANONICAL_FORMAT`; a malformed header
    (missing key / non-integer value) or a non-canonical format raises
    :class:`AudioFormatMismatch` — the cycle-5 contract, no coercion. The endpoint maps that to a
    ``1003`` (Unsupported Data) close before any audio is accepted or the transmitter is keyed.
    """
    try:
        declared = AudioFormat(
            int(header["rate"]),  # type: ignore[arg-type]
            int(header["width"]),  # type: ignore[arg-type]
            int(header["channels"]),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AudioFormatMismatch(
            f"malformed TX format header: {header!r} "
            "(need integer 'rate', 'width', 'channels')"
        ) from exc
    if declared != CANONICAL_FORMAT:
        raise AudioFormatMismatch(
            f"TX stream must be {CANONICAL_FORMAT}, client declared {declared}"
        )
    return declared


class TxSession:
    """The per-connection keying + ingest state machine for one inbound TX stream.

    Feed it inbound binary payloads with :meth:`feed`; it validates framing, keys PTT on the
    first real frame, transmits each frame, and tracks activity for the idle timeout. Call
    :meth:`close` on any end (clean close, idle, format error, crash) to drop PTT — it is
    idempotent and a no-op if the stream never keyed.

    The idle decision (:meth:`idle_elapsed`) is pure and clock-injected; the transport supplies
    the wakeup cadence. All framing is canonical (48k/s16le/mono); a partial-sample payload is
    rejected (fail loud) rather than coerced.
    """

    def __init__(
        self,
        radio: Radio,
        *,
        idle_timeout: float,
        clock: Clock | None = None,
        arbiter: RadioArbiter | None = None,
        on_key: Callable[[bool], None] | None = None,
        recorder: TxRecorder = null_recorder,
        station_id: TxIdentifier | None = None,
    ) -> None:
        if clock is None:
            import time

            clock = time.monotonic
        self._radio = radio
        self._idle_timeout = idle_timeout
        self._clock = clock
        # The shared half-duplex arbiter (ADR 0017): claimed on key-up, released on key-down so the
        # RX pump and scan stand down while we hold the radio. A standalone session gets a private
        # arbiter (the same default-shape as `clock`), so isolated construction stays behaviorally
        # unchanged; the app injects the one shared instance.
        self._arbiter = arbiter if arbiter is not None else RadioArbiter()
        # Optional injected key-edge callback: fired True on the key-up edge, False on key-down, so
        # the streaming-TX path emits the same `ptt` events REST `/ptt` does and the ledger records
        # `tx_key_up`/`tx_key_down` (with duration) for streaming keying too (ADR 0019). The wired
        # publisher is non-raising, keeping keying isolated from a logging fault.
        self._on_key = on_key
        # Optional TX recorder (ADR 0021): each fed frame is written and the segment is finalized on
        # key-down. Off by default (`null_recorder`); the app injects a `tx-`-prefixed Recorder when
        # `RADIO_RECORD_TX` is on. Its calls are guarded (see `feed`/`close`) so a disk fault can
        # never break the keying state machine (guardrail 2) or leak the single-talker slot.
        self._recorder = recorder
        # Optional streaming station-ID seam (ADR 0041, guardrail 5 / Part 97). When injected, ID
        # audio is transmitted into the *same keyed over* as content — at key-up (first over of a
        # fresh transmission), across the <=10-minute boundary mid-over, and at key-down — so the
        # streaming-TX path (browser talker + Mumble bridge) is auto-identified. `None` (the default)
        # keeps the historical un-ID'd streaming behaviour, so every existing TX test is unchanged.
        self._station_id = station_id
        self._keyed = False
        self._last_active: float | None = None

    @property
    def keyed(self) -> bool:
        """Whether PTT is currently asserted for this stream."""
        return self._keyed

    @property
    def idle_timeout(self) -> float:
        """Seconds of inbound silence tolerated before PTT is dropped (the ``wait_for`` value)."""
        return self._idle_timeout

    def feed(self, data: bytes) -> None:
        """Ingest one inbound binary payload: validate, key on first frame, transmit, stamp.

        Order matters. Whole-sample framing is validated **first**, so a malformed payload raises
        :class:`AudioFormatMismatch` before any ``ptt()`` — a bad frame never keys the radio. An
        empty ``b""`` payload carries no audio and is skipped (mirrors ``RxPump``'s empty-frame
        skip): it neither keys nor refreshes the activity clock, so an all-empty stream idles out.
        The first real frame asserts ``ptt(True)``; every real frame ``transmit``s and stamps the
        last-activity time.
        """
        if len(data) % CANONICAL_FORMAT.frame_bytes:
            # No format tag rides a raw binary frame, so the one canonical invariant we can check
            # on the wire is whole-sample framing: a partial sample-frame is non-canonical. Fail
            # loud (do not pad/truncate) — the cycle-5 contract.
            raise AudioFormatMismatch(
                f"inbound TX payload is {len(data)} bytes, not a whole number of "
                f"{CANONICAL_FORMAT} sample-frames ({CANONICAL_FORMAT.frame_bytes}B each)"
            )
        if not data:
            return
        now = self._clock()
        if not self._keyed:
            # Claim the radio for TX *before* asserting PTT (ADR 0017): the arbiter's coherence
            # guard would refuse a double-key, and the RX pump / scan consult it to pause.
            self._arbiter.acquire_tx()
            self._radio.ptt(True)
            self._keyed = True
            if self._on_key is not None:
                self._on_key(True)
            # Identify at the key-up edge of a fresh transmission, into this same over (ADR 0041).
            if self._station_id is not None:
                id_audio = self._station_id.key_up_id(now)
                if id_audio is not None:
                    self._radio.transmit(id_audio)
        elif self._station_id is not None:
            # Re-identify if the <=10-minute boundary is crossed mid-over, ahead of the content.
            id_audio = self._station_id.periodic_id(now)
            if id_audio is not None:
                self._radio.transmit(id_audio)
        self._radio.transmit(AudioFrame(data))
        self._last_active = now
        # Record the transmitted frame (ADR 0021). Guarded: a disk fault must never break keying —
        # the transmit above has already gone out; recording is strictly best-effort.
        try:
            self._recorder.write(data)
        except Exception:
            pass

    def idle_elapsed(self) -> bool:
        """True iff the stream is keyed and has been silent for at least ``idle_timeout``.

        The pure, injected-clock timeout decision — driven directly (with a fake clock, no
        asyncio) in the unit tests. A stream that never keyed is never "idle" (nothing to drop).
        """
        if not self._keyed or self._last_active is None:
            return False
        return self._clock() - self._last_active >= self._idle_timeout

    def on_idle(self) -> bool:
        """Transport wakeup hook: if the idle window has elapsed, drop PTT and report it.

        Called when the endpoint's ``wait_for`` on the next inbound frame times out. Keeps the
        timeout *policy* in the session (consulting the injected clock) while the transport only
        provides the wakeup. Returns whether PTT was actually dropped.
        """
        if self.idle_elapsed():
            self.close()
            return True
        return False

    def close(self) -> None:
        """Drop PTT if keyed; idempotent. Safe to call when the stream never keyed (a no-op, so
        no spurious ``ptt(False)`` reaches the radio)."""
        if self._keyed:
            # Closing ID at the key-down edge, transmitted while still keyed (ADR 0041). Due-gated
            # inside the identifier, so a rapid tap-tap exchange is not ID'd after every short over.
            # Pass our own clock so the identifier's due-check uses the same time source `feed` did
            # (both this session's `_clock`), not the identifier's fallback clock.
            if self._station_id is not None:
                id_audio = self._station_id.sign_off_id(self._clock())
                if id_audio is not None:
                    self._radio.transmit(id_audio)
            self._radio.ptt(False)
            self._keyed = False
            if self._on_key is not None:
                self._on_key(False)
            # Free the radio so the RX pump and scan resume (ADR 0017). Release is idempotent, so
            # this pairs safely with the guarded key-down.
            self._arbiter.release_tx()
            # Finalize the TX recording segment LAST and GUARDED (ADR 0021): the endpoint's `finally`
            # calls `close()` then `tx_slot.release()`, so an exception escaping here would skip the
            # slot release and permanently wedge the single transmitter. Keying/arbiter release above
            # is the load-bearing work; recording is best-effort and must never break it.
            try:
                self._recorder.end_segment()
            except Exception:
                pass


class TxSlot:
    """Single-talker occupancy guard: you cannot key one transmitter from two clients.

    A plain flag, deliberately **not** an :class:`asyncio.Lock`. A Lock would *queue* a second
    talker and eventually let it key once the first releases; we must *refuse* it outright while
    the first is live. Check-and-set is atomic under asyncio (the endpoint sets the flag with no
    ``await`` between the test and the set), so a bare bool is correct and minimal.
    """

    def __init__(self) -> None:
        self._occupied = False

    def try_acquire(self) -> bool:
        """Claim the single talker slot; return ``False`` if it is already occupied."""
        if self._occupied:
            return False
        self._occupied = True
        return True

    def release(self) -> None:
        """Free the slot for the next talker; idempotent (safe in a ``finally`` after refusal)."""
        self._occupied = False

    @property
    def occupied(self) -> bool:
        """Whether a talker currently holds the slot."""
        return self._occupied


def load_tx_idle_timeout(settings: Settings) -> float:
    """Return the TX idle timeout in seconds (`tx.idle_timeout`)."""
    return settings.get("tx.idle_timeout")
