"""The remote-control client seam: a protocol + an in-memory mock + a UDP client (ADR 0095).

The gateway's remote-control channel is a small **request/reply** protocol (unlike DSRP's streaming
push): authenticate once with a password, then link/unlink a module or query its confirmed link state.
Everything a caller (the future ``DvapManager``) needs is expressed here as the
:class:`RemoteControlClient` protocol, so it is fully testable against :class:`MockRemoteControlClient`
with **no gateway and no socket** — the mock even models a tiny gateway (per-module link state) so a
``link`` -> ``status`` -> ``unlink`` flow reads back correctly. The real
:class:`UdpRemoteControlClient` owns a UDP socket, performs the ``LIN`` -> ``RND`` -> ``SHA`` login
lazily, and bounds every wait; a ``_socket_factory`` / ``_clock`` seam keeps even it drivable.

Nothing imports this yet (ADR 0095 lands the seam isolated); the DVAP config, manager, API and web tab
that consume it are the follow-up PR.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from . import remote_codec as rc
from .remote_codec import Protocol as DProtocol
from .remote_codec import Reconnect, RemoteKind, RemoteMessage

log = logging.getLogger(__name__)

#: The ircDDBGateway host. Loopback: the gateway runs on the same box as radio-server.
DEFAULT_REMOTE_HOST = "127.0.0.1"

#: Seconds to wait for a single reply before retrying / giving up.
DEFAULT_TIMEOUT = 1.0

#: How many times a request is resent before it fails (UDP can drop a datagram).
DEFAULT_RETRIES = 2

_RECV_SIZE = 2048


class RemoteControlError(Exception):
    """Base for remote-control failures."""


class RemoteAuthError(RemoteControlError):
    """The gateway rejected the password (``NAK``) or never answered the login."""


class RemoteTimeout(RemoteControlError):
    """The gateway did not reply to a request within the bounded time."""


@runtime_checkable
class RemoteControlClient(Protocol):
    """What a DVAP controller needs from the gateway's remote-control interface — the whole seam.

    Auth is performed lazily on first use and cached; every method may raise
    :class:`RemoteControlError` (``RemoteAuthError`` / ``RemoteTimeout``).
    """

    def link(self, repeater: str, reflector: str, reconnect: Reconnect = Reconnect.FIXED) -> None:
        """Link ``repeater`` (e.g. ``"AE9S   B"``) to ``reflector`` (e.g. ``"REF001 C"``)."""

    def unlink(self, repeater: str, reflector: str = "", protocol: DProtocol = DProtocol.UNKNOWN) -> None:
        """Drop ``repeater``'s current reflector link."""

    def status(self, repeater: str) -> RemoteMessage:
        """Return the gateway's confirmed :class:`RemoteMessage` (``REPEATER``) for ``repeater``."""

    def callsigns(self) -> RemoteMessage:
        """Return the gateway's :class:`RemoteMessage` (``CALLSIGNS``) listing its repeaters / StarNets."""

    def close(self) -> None:
        """End the session. Idempotent."""


class MockRemoteControlClient:
    """In-memory :class:`RemoteControlClient` for tests — models a tiny gateway.

    Holds a per-module link map so ``link`` / ``unlink`` / ``status`` read back consistently, records
    every call in :attr:`sent`, and can be told to reject auth (:attr:`fail_auth`) to exercise the
    error path. No socket, no thread.
    """

    def __init__(self, *, password: str = "test", fail_auth: bool = False) -> None:
        self._password = password
        self.fail_auth = fail_auth
        self.authed = False
        self.login_count = 0
        #: Every command issued, in order: ``("link", repeater, reflector)`` etc. — the assertion point.
        self.sent: list[tuple] = []
        #: The modelled gateway state: module callsign -> currently-linked reflector.
        self.linked: dict[str, str] = {}
        #: What :meth:`callsigns` reports (module callsigns the gateway knows).
        self.known: list[str] = []
        self.closed = False

    def _ensure_auth(self) -> None:
        if self.authed:
            return
        self.login_count += 1
        if self.fail_auth:
            raise RemoteAuthError("mock: password rejected")
        self.authed = True

    def link(self, repeater: str, reflector: str, reconnect: Reconnect = Reconnect.FIXED) -> None:
        self._ensure_auth()
        self.sent.append(("link", repeater, reflector, reconnect))
        self.linked[repeater] = reflector

    def unlink(self, repeater: str, reflector: str = "", protocol: DProtocol = DProtocol.UNKNOWN) -> None:
        self._ensure_auth()
        self.sent.append(("unlink", repeater, reflector, protocol))
        self.linked.pop(repeater, None)

    def status(self, repeater: str) -> RemoteMessage:
        self._ensure_auth()
        self.sent.append(("status", repeater))
        reflector = self.linked.get(repeater, "")
        links = (
            (rc.RepeaterLink(reflector, DProtocol.DPLUS, True, rc.Direction.OUTGOING, True),)
            if reflector
            else ()
        )
        return RemoteMessage(RemoteKind.REPEATER, repeater=repeater, reflector=reflector, links=links)

    def callsigns(self) -> RemoteMessage:
        self._ensure_auth()
        self.sent.append(("callsigns",))
        entries = tuple(rc.CallsignEntry("R", call) for call in self.known)
        return RemoteMessage(RemoteKind.CALLSIGNS, callsigns=entries)

    def close(self) -> None:
        self.closed = True
        self.authed = False


def _default_socket_factory(host: str, port: int) -> socket.socket:
    """Open a UDP socket connected to the gateway so ``recv`` only yields its replies."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect((host, port))
    return sock


class UdpRemoteControlClient:
    """The real :class:`RemoteControlClient`: a UDP socket doing bounded request/reply round-trips.

    Login (``LIN`` -> ``RND`` -> ``SHA`` -> ``ACK``/``NAK``) runs lazily on first command and is cached
    — but the cache is not trusted past a reply failure: a timeout/NAK on a round-trip command means
    the gateway's single login session is gone (its restart drops it), so the client re-logins once
    and retries (ADR 0103);
    each request is resent up to :attr:`retries` times and every wait is bounded by :attr:`timeout`, so
    no method blocks indefinitely. A single lock serialises callers sharing the one socket.
    """

    def __init__(
        self,
        *,
        password: str,
        host: str = DEFAULT_REMOTE_HOST,
        port: int = rc.DEFAULT_REMOTE_PORT,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        _socket_factory: Callable[[str, int], socket.socket] | None = None,
        _clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._password = password
        self._host = host
        self._port = port
        self._timeout = timeout
        self._retries = retries
        self._socket_factory = _socket_factory or _default_socket_factory
        self._clock = _clock
        self._sock: socket.socket | None = None
        self._authed = False
        self._lock = threading.RLock()

    # -- transport --------------------------------------------------------------------------

    def _ensure_socket(self) -> socket.socket:
        if self._sock is None:
            self._sock = self._socket_factory(self._host, self._port)
            self._sock.settimeout(self._timeout)
        return self._sock

    def _send(self, packet: bytes) -> None:
        self._ensure_socket().send(packet)

    def _await(self, kinds: tuple[RemoteKind, ...]) -> RemoteMessage:
        """Read replies until one matches ``kinds`` (ignoring stragglers), bounded by ``timeout``."""
        sock = self._ensure_socket()
        deadline = self._clock() + self._timeout
        while self._clock() < deadline:
            try:
                data = sock.recv(_RECV_SIZE)
            except (TimeoutError, OSError):
                break
            msg = rc.parse(data)
            if msg.kind is RemoteKind.NAK:
                raise RemoteAuthError(msg.text or "gateway rejected the request")
            if msg.kind in kinds:
                return msg
        raise RemoteTimeout("no reply from gateway")

    def _request(self, packet: bytes, kinds: tuple[RemoteKind, ...]) -> RemoteMessage:
        """Send ``packet`` and await a matching reply, retrying the whole exchange on timeout."""
        last: RemoteControlError = RemoteTimeout("no reply from gateway")
        for _ in range(self._retries + 1):
            try:
                self._send(packet)
                return self._await(kinds)
            except RemoteTimeout as exc:
                last = exc
        raise last

    # -- auth -------------------------------------------------------------------------------

    def _login(self) -> None:
        with self._lock:
            if self._authed:
                return
            rnd = self._request(rc.build_login(), (RemoteKind.RANDOM,))
            self._send(rc.build_hash(self._password, rnd.random))
            self._await((RemoteKind.ACK,))  # a NAK raises RemoteAuthError inside _await
            self._authed = True

    def _ensure_auth(self) -> None:
        if not self._authed:
            self._login()

    def _request_authed(self, packet: bytes, kinds: tuple[RemoteKind, ...]) -> RemoteMessage:
        """An authenticated round-trip that survives gateway session loss (ADR 0103).

        The gateway keeps a single live login session; a gateway **restart drops it**, after which
        it ignores (or NAKs) our still-"authenticated" requests — the cached ``_authed`` is no
        longer proof of a session. So a reply failure here invalidates the cache, logs in fresh
        exactly once, and retries the request once. A failing re-login (gateway really down, or the
        password now rejected) propagates just like before. The initial ``_ensure_auth`` is outside
        the retry on purpose: a *rejected credential* on first login raises immediately — re-auth
        heals a lost session, not a wrong password.
        """
        self._ensure_auth()
        try:
            return self._request(packet, kinds)
        except (RemoteTimeout, RemoteAuthError):
            self._authed = False
            self._login()
            return self._request(packet, kinds)

    # -- commands ---------------------------------------------------------------------------

    def link(self, repeater: str, reflector: str, reconnect: Reconnect = Reconnect.FIXED) -> None:
        # Fire-and-forget on the wire (the protocol shape) — but through _ensure_auth, so once any
        # round-trip has detected a session loss (ADR 0103) the next link logs in fresh first.
        with self._lock:
            self._ensure_auth()
            self._send(rc.build_link(repeater, reflector, reconnect))

    def unlink(self, repeater: str, reflector: str = "", protocol: DProtocol = DProtocol.UNKNOWN) -> None:
        with self._lock:
            self._ensure_auth()
            self._send(rc.build_unlink(repeater, reflector, protocol))

    def status(self, repeater: str) -> RemoteMessage:
        with self._lock:
            return self._request_authed(rc.build_get_repeater(repeater), (RemoteKind.REPEATER,))

    def callsigns(self) -> RemoteMessage:
        with self._lock:
            return self._request_authed(rc.build_get_callsigns(), (RemoteKind.CALLSIGNS,))

    def close(self) -> None:
        with self._lock:
            sock, self._sock = self._sock, None
            self._authed = False
            if sock is not None:
                try:
                    sock.send(rc.build_logout())
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
