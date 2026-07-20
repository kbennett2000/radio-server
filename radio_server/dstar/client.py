"""The gateway client seam: a protocol + an in-memory mock + a UDP client (ADR 0087).

The D-STAR link is a **network peer, not a** :class:`~radio_server.backends.base.Radio` **backend**
(the ADR 0041 Mumble shape): the ircDDBGateway is the reflector side, the radio stays the RF side.
Everything the bridge (:mod:`radio_server.dstar.bridge`) needs from the gateway is expressed here as
the :class:`GatewayClient` protocol, so the whole bridge is unit-testable against
:class:`MockGatewayClient` with **no gateway and no socket** — the mock-first discipline. The real
:class:`UdpGatewayClient` owns a UDP socket, a daemon reader thread, and the register/poll timers, but
imports nothing beyond the stdlib (plain ``socket``); a ``_socket_factory`` / ``_clock`` test seam
keeps even it drivable without the network.

Inbound headers/data arrive on the client's reader thread and are handed to the ``on_header`` /
``on_data`` sinks; like the Mumble ``on_audio`` sink, an implementation treats them as
thread-unsafe-callees — the bridge's sink hops across the thread boundary itself.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from . import dsrp

log = logging.getLogger(__name__)

# --- marked defaults (guardrail 1: config defaults, verify against a real gateway) ------------

#: The ircDDBGateway host. Loopback: the gateway runs on the same box as radio-server.
DEFAULT_GATEWAY_HOST = "127.0.0.1"

#: The port an ircDDBGateway listens on for its repeaters (g4klx default).
DEFAULT_GATEWAY_PORT = 20010

#: The local UDP port radio-server binds and the gateway sends back to. Distinct from the DVAP's
#: 20011 so a second endpoint coexists on one gateway (the gateway's ``repeaterPortN`` for this band).
DEFAULT_LOCAL_PORT = 20012

#: The repeater module letter radio-server registers as — **A**, distinct from the DVAP's **B**.
DEFAULT_MODULE = "A"

#: Seconds between poll / keep-alive packets. The poll is ALSO the registration (it carries the
#: callsign), so this is the only cadence. 10 s stays well inside the gateway's repeater-inactivity
#: timeout (guardrail 1: verify against a real gateway / match the observed dstarrepeater cadence).
DEFAULT_POLL_INTERVAL = 10.0

#: How long the reader blocks on a receive before looping to service the timers.
_READ_TIMEOUT = 0.2

#: Largest DSRP packet is the 49-byte header; 512 is comfortable headroom.
_RECV_SIZE = 512


@dataclass(frozen=True)
class GatewayStatus:
    """A snapshot of the gateway link, surfaced by the bridge's status."""

    running: bool
    registered: bool
    host: str = ""
    port: int = 0
    module: str = ""


#: The sinks the client invokes per inbound DSRP header / data message (parsed :class:`DsrpMessage`).
OnMessage = Callable[[dsrp.DsrpMessage], None]


@runtime_checkable
class GatewayClient(Protocol):
    """What the bridge needs from a gateway connection — the whole seam.

    Lifecycle (``start``/``close``), the repeater keep-alive (``register``/``poll`` — registration IS
    the poll; the real client also drives it on its own timer), a send path for a stream
    (``send_header`` then ``send_data`` frames), the inbound sinks, and a status snapshot.
    """

    #: Set by the bridge before :meth:`start`; invoked per inbound header / data message.
    on_header: OnMessage | None
    on_data: OnMessage | None

    def start(self) -> None:
        """Open the connection and register the endpoint (send the first poll). Idempotent."""

    def register(self) -> None:
        """Register with the gateway by sending the callsign-bearing poll (there is no separate
        register packet — ``0x0B`` is gateway -> repeater; see ADR 0101)."""

    def poll(self) -> None:
        """Send a keep-alive poll (carries the callsign) to the gateway."""

    def send_header(self, radio_header: bytes, session_id: int) -> None:
        """Open an outbound stream with a 41-byte radio header under ``session_id``."""

    def send_data(
        self, dv_frame: bytes, session_id: int, seq_no: int, *, end: bool = False, errors: int = 0
    ) -> None:
        """Send one DV frame of the outbound stream; ``end`` closes it."""

    def status(self) -> GatewayStatus:
        """Return a :class:`GatewayStatus` snapshot."""

    def close(self) -> None:
        """Close the connection. Idempotent."""


class MockGatewayClient:
    """In-memory :class:`GatewayClient` for tests — the :class:`MockRadio` analogue.

    Records every packet sent (``sent``, as parsed :class:`DsrpMessage` plus the raw register/poll
    counts) and exposes :meth:`inject` to drive the ``on_header`` / ``on_data`` sinks with an inbound
    stream. No socket, no thread; ``start``/``close`` flip a flag.

    Divergence from :class:`UdpGatewayClient`: the mock is an already-accepted peer stand-in, so it
    reports ``registered`` optimistically the moment :meth:`start`/:meth:`register` runs. The real
    client only reports ``registered`` once the gateway actually answers (an inbound packet), because
    against a live gateway "we sent a poll" is not proof the gateway accepted us (ADR 0101).
    """

    def __init__(self, *, host: str = "mock", port: int = 0, module: str = DEFAULT_MODULE) -> None:
        self.on_header: OnMessage | None = None
        self.on_data: OnMessage | None = None
        self._host = host
        self._port = port
        self._module = module
        self._running = False
        self._registered = False
        self.register_count = 0
        self.poll_count = 0
        #: Every stream packet sent, parsed, in order — the TX assertion point.
        self.sent: list[dsrp.DsrpMessage] = []

    def start(self) -> None:
        self._running = True
        self.register()

    def register(self) -> None:
        self._registered = True
        self.register_count += 1

    def poll(self) -> None:
        self.poll_count += 1

    def send_header(self, radio_header: bytes, session_id: int) -> None:
        self.sent.append(dsrp.parse(dsrp.build_header_packet(radio_header, session_id)))

    def send_data(
        self, dv_frame: bytes, session_id: int, seq_no: int, *, end: bool = False, errors: int = 0
    ) -> None:
        self.sent.append(dsrp.parse(dsrp.build_data_packet(dv_frame, session_id, seq_no, end=end, errors=errors)))

    def inject(self, packet: bytes) -> None:
        """Drive the inbound sinks with one raw DSRP packet (no-op if the matching sink is unset)."""
        msg = dsrp.parse(packet)
        if msg.kind is dsrp.MessageKind.HEADER and self.on_header is not None:
            self.on_header(msg)
        elif msg.kind is dsrp.MessageKind.DATA and self.on_data is not None:
            self.on_data(msg)

    def status(self) -> GatewayStatus:
        return GatewayStatus(
            running=self._running,
            registered=self._registered,
            host=self._host,
            port=self._port,
            module=self._module,
        )

    def close(self) -> None:
        self._running = False


def _default_socket_factory(local_port: int) -> socket.socket:
    """Open a bound UDP socket with a short receive timeout (the default transport)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", local_port))
    sock.settimeout(_READ_TIMEOUT)
    return sock


class UdpGatewayClient:
    """The real :class:`GatewayClient`: a UDP socket + a daemon reader that also services the timers.

    One thread does it all: it blocks on ``recvfrom`` up to :data:`_READ_TIMEOUT`, dispatches any
    inbound header/data to the sinks (and flips ``registered`` true on the first inbound packet — the
    gateway's proof it accepted us), then services the poll cadence off the injectable monotonic clock.
    The callsign-bearing poll fires once at :meth:`start` and every :attr:`poll_interval` after — it is
    both the registration and the keep-alive. ``close`` is idempotent and never blocks.
    """

    def __init__(
        self,
        *,
        gateway_host: str = DEFAULT_GATEWAY_HOST,
        gateway_port: int = DEFAULT_GATEWAY_PORT,
        local_port: int = DEFAULT_LOCAL_PORT,
        module: str = DEFAULT_MODULE,
        register_name: str = "",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        _socket_factory: Callable[[int], socket.socket] | None = None,
        _clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.on_header: OnMessage | None = None
        self.on_data: OnMessage | None = None
        self._gateway = (gateway_host, gateway_port)
        self._local_port = local_port
        self._module = module
        # The 8-char callsign the poll carries and the gateway keys the endpoint by. A bare module
        # letter is NOT accepted for registration (that was the bug ADR 0101 fixes) — warn on an empty
        # name rather than silently sending the useless module-letter poll.
        if not register_name:
            log.warning(
                "dstar: no register_name (callsign) given; polling with bare module %r will not "
                "register with the gateway",
                module,
            )
        self._register_name = register_name or module
        self._poll_interval = poll_interval
        self._socket_factory = _socket_factory or _default_socket_factory
        self._clock = _clock

        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._registered = False
        self._last_poll = 0.0
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------------------

    def start(self) -> None:
        if self._sock is not None:
            return
        self._sock = self._socket_factory(self._local_port)
        self._stop.clear()
        self.register()
        self._last_poll = self._clock()
        self._thread = threading.Thread(target=self._run, name="dstar-gateway", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._registered = False

    # -- send path ----------------------------------------------------------------------

    def _send(self, packet: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        with self._lock:
            try:
                sock.sendto(packet, self._gateway)
            except OSError as exc:  # transient send failure must not kill the caller
                log.warning("dstar: gateway send failed: %s", exc)

    def register(self) -> None:
        # Registration IS the poll: the callsign-bearing 0x0A poll is what the gateway registers us
        # from. There is no 0x0B outbound (that's gateway -> repeater). `_registered` is NOT set here —
        # it flips true only when the gateway answers (see `_handle_packet`). ADR 0101.
        self.poll()

    def poll(self) -> None:
        self._send(dsrp.build_poll(self._register_name))

    def send_header(self, radio_header: bytes, session_id: int) -> None:
        self._send(dsrp.build_header_packet(radio_header, session_id))

    def send_data(
        self, dv_frame: bytes, session_id: int, seq_no: int, *, end: bool = False, errors: int = 0
    ) -> None:
        self._send(dsrp.build_data_packet(dv_frame, session_id, seq_no, end=end, errors=errors))

    # -- reader + timers ----------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                data, _ = sock.recvfrom(_RECV_SIZE)
            except (OSError, ValueError):
                data = b""
            if data:
                self._handle_packet(data)
            self._tick(self._clock())

    def _handle_packet(self, data: bytes) -> None:
        msg = dsrp.parse(data)
        # Any well-formed inbound DSRP packet is the gateway's proof it accepted our registration —
        # a register (0x0B), status, text, or a routed header/data. That is the real "registered"
        # signal, unlike the old code which asserted it right after sending our own packet. ADR 0101.
        if msg.kind is not dsrp.MessageKind.UNKNOWN:
            self._registered = True
        try:
            if msg.kind is dsrp.MessageKind.HEADER and self.on_header is not None:
                self.on_header(msg)
            elif msg.kind is dsrp.MessageKind.DATA and self.on_data is not None:
                self.on_data(msg)
        except Exception:  # a sink fault must never kill the reader
            log.exception("dstar: inbound sink raised")

    def _tick(self, now: float) -> None:
        """Service the poll cadence; called every reader wake (test seam). The poll is also the
        registration keep-alive, so it is the only timer."""
        if now - self._last_poll >= self._poll_interval:
            self.poll()
            self._last_poll = now

    def status(self) -> GatewayStatus:
        return GatewayStatus(
            running=self._sock is not None,
            registered=self._registered,
            host=self._gateway[0],
            port=self._gateway[1],
            module=self._module,
        )
