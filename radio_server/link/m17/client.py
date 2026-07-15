"""The mrefd UDP client: connection lifecycle over the wire (ADR 0051).

This is the one module in the ``m17`` subpackage that touches the network. Everything else here
(:mod:`.callsign`, :mod:`.crc`, :mod:`.packet`) is a pure, socket-free byte codec; this module owns
the socket and drives that codec across it. It **builds no packets itself** — every byte it sends
is a ``build_*`` call and every byte it receives goes through :func:`~.packet.parse_control` /
:func:`~.packet.parse_stream`, so the ADR-0050 untrusted-peer rule (bad datagram → ``None``, never
an exception on the receive path) holds unchanged.

Scope is the **connection lifecycle only** — handshake, keepalive, loss detection, teardown. It is
not a :class:`~radio_server.link.base.Link`: binding this client, the parsers, and the Codec2 seam
into ``Link`` behind ``create_link`` is the next cycle. Accordingly this module imports nothing from
``radio_server`` — its configuration arrives as plain constructor values — and has no dependency on
``StreamEdge``.

**The load-bearing safety call: source-address validation.** A UDP socket accepts datagrams from
anyone, and the next cycle wires an inbound stream to a path that keys the licensee's transmitter
(ADR 0048). So the socket is opened **unconnected** and every datagram whose source address is not
the connected reflector's is dropped *before* it reaches the parsers (:meth:`M17Client._on_datagram`).
This is the outermost guardrail on the inbound chain. It is spoofable — UDP has no authentication
and M17 has no central identity by design — so it is the cheap outer gate, not authentication; the
real bounds are ``TxLimiter``, ``tx.idle_timeout``, the ``TxSlot`` rule, and ``/link/disable``
(ADR 0051).

**Protocol timing** was read from mrefd's ``Packet-Description.md`` (guardrail 1), not recalled: the
reflector sends ``PING`` about every 3 s and a client replies ``PONG``; a node that has heard no
``PING``/``PONG`` for 30 s assumes the reflector has stopped. The default mrefd UDP port is 17000.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from enum import Enum

from .packet import (
    StreamFrame,
    build_conn,
    build_disc,
    build_lstn,
    build_pong,
    parse_control,
    parse_stream,
)

logger = logging.getLogger(__name__)

#: mrefd's default UDP listen port (README: "UDP port 17000 (or whatever port you have configured)").
MREFD_DEFAULT_PORT = 17000
#: Seconds without any PING/PONG from the reflector after which the connection is declared lost.
#: mrefd: "If a node hasn't received a PING or PONG in the last 30 seconds, it can assume the
#: reflector has stopped working." This is the published figure, not a guessed one.
DEFAULT_KEEPALIVE_TIMEOUT = 30.0
#: Seconds to wait for the ACKN/NACK reply to a CONN/LSTN before giving up. An implementation
#: choice (the protocol does not specify a handshake timeout), kept short so a dead reflector fails
#: fast rather than hanging a connect.
DEFAULT_CONNECT_TIMEOUT = 5.0


class M17ClientState(Enum):
    """The connection state of an :class:`M17Client`.

    ``DISCONNECTED`` is the initial state and where a clean/refused/reflector-initiated teardown
    lands; ``CONNECTING`` spans the CONN/LSTN → ACKN/NACK handshake; ``CONNECTED`` means the
    reflector ACKN'd and keepalive is running; ``LOST`` is a keepalive timeout (the reflector went
    silent); ``CLOSED`` is the terminal state after :meth:`M17Client.close`.
    """

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LOST = "lost"
    CLOSED = "closed"


class _ClientProtocol(asyncio.DatagramProtocol):
    """Thin asyncio datagram protocol that forwards every event to its :class:`M17Client`.

    Kept dumb on purpose: all validation and lifecycle logic lives on the client, so the socket
    plumbing carries no policy.
    """

    def __init__(self, client: M17Client) -> None:
        self._client = client

    def datagram_received(self, data: bytes, addr: tuple) -> None:  # noqa: D102
        self._client._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:  # noqa: D102
        # A connectionless send can surface an async ICMP error (e.g. port unreachable). Log and
        # keep going — the keepalive watchdog is the authority on whether the link is still up.
        logger.debug("datagram error from reflector: %r", exc)

    def connection_lost(self, exc: Exception | None) -> None:  # noqa: D102
        if exc is not None:
            logger.debug("datagram transport closed with error: %r", exc)


class M17Client:
    """An asyncio UDP client for an mrefd reflector — connection lifecycle only (ADR 0051).

    Construct with the reflector's host/port/module, this station's callsign, and a local bind
    address, then :meth:`connect`. Inbound stream frames land on :attr:`frames`; control packets
    (PING/ACKN/NACK/DISC) are handled internally and never reach that queue. Connection state is on
    :attr:`state`, with :attr:`state_changed` firing on every transition. :meth:`close` sends DISC
    and tears the socket down.

    The bind default is ``0.0.0.0`` on an ephemeral port because the reflector is remote and must be
    able to reach us — this is *not* loopback-safe like the HTTP server, and the exposure is the
    reason source validation exists (see the module docstring and ADR 0051).
    """

    def __init__(
        self,
        *,
        reflector_host: str,
        reflector_port: int,
        module: str,
        callsign: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
        listen_only: bool = False,
        keepalive_timeout: float = DEFAULT_KEEPALIVE_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        on_state_change: Callable[[M17ClientState], None] | None = None,
    ) -> None:
        self._host = reflector_host
        self._port = reflector_port
        self._module = module
        self._callsign = callsign
        self._bind_host = bind_host
        self._bind_port = bind_port
        self._listen_only = listen_only
        self._keepalive_timeout = keepalive_timeout
        self._connect_timeout = connect_timeout
        self._on_state_change = on_state_change

        #: Validated, parsed inbound stream frames for a future consumer (the Link binding) to drain.
        self.frames: asyncio.Queue[StreamFrame] = asyncio.Queue()
        #: Set on every state transition; a waiter clears it and re-reads :attr:`state`.
        self.state_changed: asyncio.Event = asyncio.Event()
        #: Count of datagrams dropped because their source was not the reflector (the spoof guard).
        self.dropped_source: int = 0

        self._state = M17ClientState.DISCONNECTED
        self._loop: asyncio.AbstractEventLoop | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._reflector_addr: tuple | None = None
        self._handshake: asyncio.Future[bool] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._last_rx = 0.0

    # --- introspection --------------------------------------------------------------------------

    @property
    def state(self) -> M17ClientState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state is M17ClientState.CONNECTED

    @property
    def local_addr(self) -> tuple | None:
        """The socket's bound ``(host, port)``, or ``None`` before :meth:`connect`.

        Exposed so a test (or a future status view) can learn the ephemeral port the OS chose.
        """
        if self._transport is None:
            return None
        return self._transport.get_extra_info("sockname")

    async def wait_for_state(
        self, target: M17ClientState, timeout: float | None = None
    ) -> M17ClientState:
        """Await until :attr:`state` equals ``target`` (or raise :class:`asyncio.TimeoutError`).

        Race-free: it re-checks the current state each turn, so a transition that happens between
        checks is never missed.
        """

        async def _wait() -> None:
            while self._state is not target:
                self.state_changed.clear()
                if self._state is target:
                    return
                await self.state_changed.wait()

        await asyncio.wait_for(_wait(), timeout)
        return self._state

    # --- lifecycle ------------------------------------------------------------------------------

    async def connect(self) -> bool:
        """Open the socket, run the CONN/LSTN handshake, and start keepalive on success.

        Returns ``True`` when the reflector ACKN'd (state → ``CONNECTED``), ``False`` on a NACK or a
        handshake timeout (state → ``DISCONNECTED``, socket torn down). Sends ``LSTN`` instead of
        ``CONN`` when constructed with ``listen_only=True``.
        """
        self._loop = asyncio.get_running_loop()
        self._reflector_addr = await self._resolve(self._host, self._port)
        self._set_state(M17ClientState.CONNECTING)
        self._handshake = self._loop.create_future()

        transport, _ = await self._loop.create_datagram_endpoint(
            lambda: _ClientProtocol(self),
            local_addr=(self._bind_host, self._bind_port),
        )
        self._transport = transport  # type: ignore[assignment]

        request = (
            build_lstn(self._callsign, self._module)
            if self._listen_only
            else build_conn(self._callsign, self._module)
        )
        self._send(request)

        try:
            acknowledged = await asyncio.wait_for(self._handshake, self._connect_timeout)
        except asyncio.TimeoutError:
            logger.info("mrefd handshake to %s timed out", self._reflector_addr)
            self._teardown_transport()
            self._set_state(M17ClientState.DISCONNECTED)
            return False
        finally:
            self._handshake = None

        if not acknowledged:
            logger.info("mrefd refused the link (NACK) at %s", self._reflector_addr)
            self._teardown_transport()
            self._set_state(M17ClientState.DISCONNECTED)
            return False

        self._last_rx = self._loop.time()
        self._set_state(M17ClientState.CONNECTED)
        self._watchdog = self._loop.create_task(self._run_watchdog())
        return True

    async def close(self) -> None:
        """Send a best-effort DISC, stop the watchdog, and close the socket. Idempotent."""
        if self._state is M17ClientState.CLOSED:
            return
        if self._transport is not None and self._state in (
            M17ClientState.CONNECTING,
            M17ClientState.CONNECTED,
            M17ClientState.LOST,
        ):
            self._send(build_disc(self._callsign))

        watchdog = self._watchdog
        self._watchdog = None
        if watchdog is not None:
            watchdog.cancel()
            try:
                await watchdog
            except asyncio.CancelledError:
                pass

        self._teardown_transport()
        self._set_state(M17ClientState.CLOSED)

    # --- outbound -------------------------------------------------------------------------------

    def send_stream_frame(self, data: bytes) -> None:
        """Send a pre-built 54-byte M17 stream frame to the connected reflector (Link outbound path).

        The client owns the socket, so the ``Link`` binding (ADR 0052) hands it already-built stream
        frames rather than reaching for the transport itself — keeping ``socket`` confined to this one
        module. ``_send`` no-ops harmlessly if the transport is not open, so a call before/after a
        connection is safe.
        """
        self._send(data)

    # --- inbound --------------------------------------------------------------------------------

    def _on_datagram(self, data: bytes, addr: tuple) -> None:
        """Handle one received datagram. Source validation happens first, before any parsing."""
        if not self._addr_matches(addr):
            # THE outermost guardrail: a datagram from anyone but the connected reflector never
            # reaches the parsers, so it can never become a stream frame that keys the radio.
            self.dropped_source += 1
            logger.debug("dropped datagram from unexpected source %s", addr)
            return

        if self._loop is not None:
            self._last_rx = self._loop.time()  # any reflector datagram is proof of life

        control = parse_control(data)
        if control is not None:
            self._handle_control(control.kind)
            return

        frame = parse_stream(data)
        if frame is not None:
            self.frames.put_nowait(frame)
            return

        # Well-sourced but unparseable: drop silently (ADR 0050 untrusted-peer rule — never raise
        # on the receive path).
        logger.debug("dropped unparseable datagram from reflector (%d bytes)", len(data))

    def _handle_control(self, kind: str) -> None:
        if kind == "PING":
            # The liveness requirement: mrefd drops a client that does not answer its PING.
            self._send(build_pong(self._callsign))
        elif kind == "PONG":
            pass  # liveness already recorded via _last_rx
        elif kind in ("ACKN", "NACK"):
            if self._handshake is not None and not self._handshake.done():
                self._handshake.set_result(kind == "ACKN")
        elif kind == "DISC":
            self._on_reflector_disc()

    def _on_reflector_disc(self) -> None:
        """The reflector dropped us. Cancel keepalive, close the socket, go DISCONNECTED."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        self._teardown_transport()
        self._set_state(M17ClientState.DISCONNECTED)

    # --- keepalive ------------------------------------------------------------------------------

    async def _run_watchdog(self) -> None:
        """Declare the connection ``LOST`` if no PING/PONG arrives within the keepalive timeout.

        Surfaces loss as **state only** — it never enqueues a frame or synthesizes a stream edge.
        The transport is left open so the caller can :meth:`close` or reconnect (ADR 0051).
        """
        assert self._loop is not None
        poll = min(self._keepalive_timeout / 3.0, 1.0)
        while True:
            await asyncio.sleep(poll)
            if self._loop.time() - self._last_rx > self._keepalive_timeout:
                logger.info("mrefd reflector %s went silent; connection lost", self._reflector_addr)
                self._set_state(M17ClientState.LOST)
                return

    # --- helpers --------------------------------------------------------------------------------

    async def _resolve(self, host: str, port: int) -> tuple:
        """Resolve ``host``/``port`` to a concrete socket address for source comparison.

        Prefers IPv4 so the comparison tuple matches the ``(ip, port)`` shape a v4 datagram's
        ``addr`` carries; falls back to whatever the resolver returns.
        """
        assert self._loop is not None
        infos = await self._loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        for family in (socket.AF_INET, socket.AF_INET6):
            for info in infos:
                if info[0] == family:
                    return info[4]
        return infos[0][4]

    def _addr_matches(self, addr: tuple) -> bool:
        """Whether ``addr`` is the connected reflector — compares host and port only.

        Comparing just the first two elements is robust across IPv4 2-tuples and IPv6 4-tuples
        (whose trailing flowinfo/scopeid we do not want to gate on).
        """
        ref = self._reflector_addr
        if ref is None:
            return False
        return addr[0] == ref[0] and addr[1] == ref[1]

    def _send(self, data: bytes) -> None:
        if self._transport is not None and self._reflector_addr is not None:
            self._transport.sendto(data, self._reflector_addr)

    def _teardown_transport(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _set_state(self, state: M17ClientState) -> None:
        if state is self._state:
            return
        self._state = state
        self.state_changed.set()
        if self._on_state_change is not None:
            self._on_state_change(state)
