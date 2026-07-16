"""The Mumble client seam: a protocol + an in-memory mock (ADR 0041).

The Mumble link is a **network peer, not a** :class:`~radio_server.backends.base.Radio` **backend**
(ADR 0041 §1): Mumble is the VoIP side, the radio stays the RF side. Everything the bridge
(:mod:`radio_server.link.bridge`) needs from Mumble is expressed here as the :class:`MumbleClient`
Protocol, so the whole bridge is unit-testable against :class:`MockMumbleClient` with **no Murmur
server, no ``pymumble``, no ``libopus``** — the mock-first discipline the radio backends use
(:class:`radio_server.backends.mock.MockRadio`). The real ``pymumble``-backed client is a later
hardware-like bring-up cycle and imports its heavy deps lazily behind the ``mumble`` extra.

Audio crossing this seam is opaque **canonical PCM bytes** (48 kHz / s16le / mono — ADR 0006), the
exact format Mumble carries as Opus, so nothing here resamples (ADR 0041 §3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# --- marked defaults (guardrail 1: config defaults, verify against a real Murmur) -----------

#: Mumble's registered port. Murmur listens here unless reconfigured.
DEFAULT_MUMBLE_PORT = 64738

#: The client name the bridge joins the server as (the station's presence in the channel).
DEFAULT_MUMBLE_USERNAME = "radio-server"

#: Empty = join the server's default/root channel. A non-empty name selects a specific channel.
DEFAULT_MUMBLE_CHANNEL = ""

#: Bridge Mumble voice onto RF by default when linked (the operator's ADR 0041 choice). Set false
#: to run receive-only (RF -> Mumble monitor) without ever keying the transmitter.
DEFAULT_MUMBLE_TX_TO_RF = True

#: Seconds of Mumble silence after which the bridge drops PTT and releases the talker slot. Mumble
#: only sends voice while a peer talks, so this is the hang that debounces inter-word gaps. Kept
#: short (ADR 0049) so a keyed radio reopens its receiver in conversational gaps — otherwise the
#: operator's own DTMF commands land on a deaf receiver while the net is talking. Verify against
#: on-air feel (too short clips the next word onto RF; too long blinds you to your own commands).
DEFAULT_MUMBLE_TX_HANG = 0.8


@dataclass(frozen=True)
class MumbleStatus:
    """A snapshot of the Mumble connection, surfaced by ``GET /link/status``."""

    connected: bool
    host: str = ""
    channel: str = ""
    #: Other users present in the joined channel (excludes this client). ``None`` when unknown.
    peers: int | None = None


#: The sink the client invokes with each received voice frame (canonical PCM bytes). The bridge
#: supplies this; the real client calls it from its own network thread, so an implementation must
#: treat it as thread-unsafe-callee — the bridge's sink hands off across the thread boundary itself.
OnAudio = Callable[[bytes], None]


@runtime_checkable
class MumbleClient(Protocol):
    """What the bridge needs from a Mumble connection — the whole seam.

    Deliberately small: connect/disconnect lifecycle, a send path for RF -> Mumble, a received-audio
    sink for Mumble -> RF, and a status snapshot. The real ``pymumble`` client and
    :class:`MockMumbleClient` both satisfy this structurally.
    """

    #: The bridge sets this before :meth:`connect`; the client invokes it per received voice frame.
    on_audio: OnAudio | None

    def connect(self) -> None:
        """Open the connection to the Murmur server and join the configured channel."""

    def disconnect(self) -> None:
        """Close the connection. Idempotent — safe to call when never connected."""

    def send_audio(self, pcm: bytes) -> None:
        """Send one canonical-PCM frame of RF-received audio to the Mumble channel."""

    def status(self) -> MumbleStatus:
        """Return a :class:`MumbleStatus` snapshot."""


class MockMumbleClient:
    """In-memory :class:`MumbleClient` for tests — the :class:`MockRadio` analogue.

    Records everything sent (``sent_audio``) for RF -> Mumble assertions, and exposes
    :meth:`inject` to simulate an inbound Mumble talker driving the ``on_audio`` sink for
    Mumble -> RF. No network, no ``pymumble``, no ``libopus``; ``connect``/``disconnect`` just flip
    a flag so lifecycle and status are exercised.
    """

    def __init__(
        self,
        *,
        host: str = "mock",
        channel: str = "",
        peers: int | None = 0,
    ) -> None:
        self.on_audio: OnAudio | None = None
        self._host = host
        self._channel = channel
        self._peers = peers
        self._connected = False
        #: Every frame handed to :meth:`send_audio`, in order — the RF -> Mumble assertion point.
        self.sent_audio: list[bytes] = []

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def send_audio(self, pcm: bytes) -> None:
        self.sent_audio.append(pcm)

    def inject(self, pcm: bytes) -> None:
        """Simulate an inbound Mumble voice frame by driving the ``on_audio`` sink.

        No-op if no sink is wired (the bridge wires it in ``start``) — mirrors how a real server
        would drop voice for an unsubscribed client.
        """
        if self.on_audio is not None:
            self.on_audio(pcm)

    def status(self) -> MumbleStatus:
        return MumbleStatus(
            connected=self._connected,
            host=self._host,
            channel=self._channel,
            peers=self._peers,
        )
