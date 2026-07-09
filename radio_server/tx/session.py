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

import os
from collections.abc import Callable, Mapping

from ..arbiter import RadioArbiter
from ..audio import CANONICAL_FORMAT, AudioFormat, AudioFormatMismatch, AudioFrame
from ..backends import Radio

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
        if not self._keyed:
            # Claim the radio for TX *before* asserting PTT (ADR 0017): the arbiter's coherence
            # guard would refuse a double-key, and the RX pump / scan consult it to pause.
            self._arbiter.acquire_tx()
            self._radio.ptt(True)
            self._keyed = True
            if self._on_key is not None:
                self._on_key(True)
        self._radio.transmit(AudioFrame(data))
        self._last_active = self._clock()

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
            self._radio.ptt(False)
            self._keyed = False
            if self._on_key is not None:
                self._on_key(False)
            # Free the radio so the RX pump and scan resume (ADR 0017). Release is idempotent, so
            # this pairs safely with the guarded key-down.
            self._arbiter.release_tx()


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


def load_tx_idle_timeout(
    env: dict[str, str] | os._Environ = os.environ,
) -> float:
    """Return the TX idle timeout (s) from ``RADIO_TX_IDLE_TIMEOUT``, or the marked default.

    Marked-default policy (the ``load_scan_settle`` / ``activity`` loader idiom): the default when
    unset/empty, else a positive float or fail loud — a *set* non-numeric or non-positive value
    raises rather than being silently papered over.
    """
    raw = env.get(RADIO_TX_IDLE_TIMEOUT_ENV_VAR)
    if raw is None or raw == "":
        return DEFAULT_TX_IDLE_TIMEOUT
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            f"{RADIO_TX_IDLE_TIMEOUT_ENV_VAR}={raw!r} is not a number"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            f"{RADIO_TX_IDLE_TIMEOUT_ENV_VAR}={raw!r} must be positive"
        )
    return value
