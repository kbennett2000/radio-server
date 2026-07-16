"""Mumble/Murmur link: bridge RF audio to a Mumble channel (ADR 0041).

The link is a **network peer, not a** :class:`~radio_server.backends.base.Radio` **backend** — it
plugs into the existing RX fan-out (:class:`~radio_server.rx.hub.AudioHub`) and TX fan-in
(:class:`~radio_server.tx.session.TxSession` + arbiter) seams rather than replacing the radio. The
dependency arrow is ``link -> {rx, tx, arbiter, backends, audio, services}`` — it is imported by the
API composition root, never the other way. ``pymumble``/``libopus`` (the ``mumble`` extra) are
imported lazily only by the real client (a later bring-up cycle); this package's public surface is
codec- and network-free so ``import radio_server`` and CI stay light.
"""

from __future__ import annotations

from .bridge import DEFAULT_TX_QUEUE_MAXSIZE, MumbleBridge
from .entries import (
    DEFAULT_MUMBLE_DISCONNECT_DTMF,
    LINK_DTMF_ALPHABET,
    MumbleEntry,
    mumble_password_secret,
    resolve_mumble_entries,
    validate_link_digits,
)
from .manager import BridgeFactory, ClientFactory, LinkManager
from .pymumble_client import PyMumbleClient
from .client import (
    DEFAULT_MUMBLE_CHANNEL,
    DEFAULT_MUMBLE_PORT,
    DEFAULT_MUMBLE_TX_HANG,
    DEFAULT_MUMBLE_TX_TO_RF,
    DEFAULT_MUMBLE_USERNAME,
    MockMumbleClient,
    MumbleClient,
    MumbleStatus,
    OnAudio,
)

__all__ = [
    "MumbleBridge",
    "MumbleClient",
    "MockMumbleClient",
    "PyMumbleClient",
    "MumbleStatus",
    "OnAudio",
    "MumbleEntry",
    "LinkManager",
    "ClientFactory",
    "BridgeFactory",
    "resolve_mumble_entries",
    "validate_link_digits",
    "mumble_password_secret",
    "DEFAULT_MUMBLE_DISCONNECT_DTMF",
    "LINK_DTMF_ALPHABET",
    "DEFAULT_MUMBLE_PORT",
    "DEFAULT_MUMBLE_USERNAME",
    "DEFAULT_MUMBLE_CHANNEL",
    "DEFAULT_MUMBLE_TX_TO_RF",
    "DEFAULT_MUMBLE_TX_HANG",
    "DEFAULT_TX_QUEUE_MAXSIZE",
]
