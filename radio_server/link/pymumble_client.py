"""The real Mumble network client, backed by pymumble (ADR 0041 Cycle C).

A thin adapter from the :class:`~radio_server.link.client.MumbleClient` protocol onto
`pymumble <https://github.com/azlux/pymumble>`_ (PyPI dist ``pymumble``, import ``pymumble_py3``) —
everything the bridge exercises is already proven against :class:`MockMumbleClient`; this class only
maps the seam onto the library. Facts below were verified against pymumble 1.6.1 source and this
machine (guardrail 1), not asserted from memory:

- The library runs its own ``threading.Thread``; ``.start()`` connects, ``.stop()`` + ``.join()``
  tears down. With ``reconnect=True`` a failed/lost connection retries in-thread (as long as the
  constructing thread is alive) instead of raising.
- ``is_ready()`` **blocks until the server syncs — indefinitely if it is unreachable** — so
  :meth:`connect` never calls it (the bridge invokes ``connect()`` on the asyncio event loop).
  Channel join happens on the library's ``connected`` callback instead, which also re-joins after
  an automatic reconnect.
- Received voice arrives via the ``sound_received`` callback **on the library thread** as decoded
  PCM at 48 kHz / s16le / mono (``opuslib.Decoder(48000, 1)`` in soundqueue.py) — exactly
  ``CANONICAL_FORMAT``, no resampling (ADR 0041 §3). The bridge's ``on_audio`` sink is
  thread-safe-by-contract (it hands off to the loop), so the callback just forwards.
- ``sound_output.add_sound(pcm)`` takes the same PCM, is guarded by an internal lock (safe from
  this side of the thread boundary), and self-chunks to 20 ms Opus frames.
- ``sound_output``/``channels`` are (re)created inside the library thread's per-connection init,
  so every access here is guarded — they do not exist between construction and first connect.

``pymumble`` (and its ``opuslib`` → libopus: the system library on macOS/Linux, the vendored
``opus.dll`` on Windows amd64 — ADR 0056) is imported lazily inside :meth:`connect` via an
injected-module seam (``_pymumble``), mirroring ``AiocBaofeng``'s ``_sd()``: construction is
import-free, tests inject a fake module, and CI never needs the extra installed.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ._opus import ensure_opus_loadable, opus_install_hint
from .client import MumbleStatus, OnAudio

logger = logging.getLogger(__name__)

#: A user is reported as "talking" for this many seconds after their last received voice frame.
#: pymumble delivers ~20 ms voice chunks while a peer talks, so this only has to bridge the gaps
#: between chunks; kept short so the indicator drops promptly when they stop.
DEFAULT_TALK_WINDOW = 0.5

_EXTRA_MSG = (
    "the Mumble link needs the 'mumble' extra (pymumble): in the radio-server checkout run "
    "`uv sync --extra mumble` — naming every extra you use, since sync installs exactly what's listed"
)

#: Seconds to wait for the library thread to exit on disconnect before giving up the join. The
#: thread is a daemon of our process lifetime either way; the bound just keeps shutdown snappy.
DEFAULT_JOIN_TIMEOUT = 2.0

#: Outgoing voice bandwidth cap in bits/second. Load-bearing, bench-confirmed (guardrail 1): left
#: uncapped, pymumble adopts the *server's* max bandwidth (Murmur default 558 kbps) as its own Opus
#: target, producing ~1.3 KB voice frames that exceed Mumble's ~1 KB voice-packet limit — the
#: server then **silently drops every frame** (verified against Murmur 1.4.230 and 1.5.901: zero
#: audio uncapped, clean audio at this cap). 96 kbps is far more than narrow-FM RF audio carries
#: and yields ~240-byte frames, comfortably inside the packet limit.
DEFAULT_MUMBLE_BANDWIDTH = 96000


class PyMumbleClient:
    """A :class:`~radio_server.link.client.MumbleClient` over a live pymumble connection.

    Pure DI object: takes the resolved connection parameters (the composition root reads config;
    this class never imports ``config``). ``connect()`` is non-blocking — it starts the library
    thread and returns; :meth:`status` reports ``connected`` once the server has synced, which is
    exactly what ``doctor --link`` polls and what ``GET /link/status`` surfaces.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int = 64738,
        username: str = "radio-server",
        channel: str = "",
        password: str = "",
        bandwidth: int = DEFAULT_MUMBLE_BANDWIDTH,
        talk_window: float = DEFAULT_TALK_WINDOW,
        clock: Callable[[], float] | None = None,
        _pymumble=None,
    ) -> None:
        self.on_audio: OnAudio | None = None
        self._host = host
        self._port = port
        self._username = username
        self._channel = channel
        self._password = password
        self._bandwidth = bandwidth
        self._talk_window = talk_window
        self._clock = clock if clock is not None else time.monotonic
        # session id -> monotonic time of that user's last received voice frame (the talk indicator).
        # Written on the library thread (`_on_sound`), read on the loop (`status`); a plain dict is
        # fine — a torn read only yields a momentarily stale flag, never corruption.
        self._last_audio: dict[int, float] = {}
        # The injected pymumble-like module (tests), or None → lazily import the real library on
        # connect. Mirrors AiocBaofeng's `_audio` seam.
        self._pm_mod = _pymumble
        self._mumble = None
        # Serializes connect/disconnect against the callback-driven channel join; cheap, and keeps
        # a disconnect racing the library thread's `connected` callback coherent.
        self._lock = threading.Lock()

    def _pm(self):
        """The pymumble-like module (injected fake, or the real library, lazily imported)."""
        if self._pm_mod is None:
            # Point ctypes at the vendored opus.dll on Windows before opuslib's import-time load
            # (no-op elsewhere); see ADR 0056. The reason is logged so a failure is debuggable.
            logger.debug("opus: %s", ensure_opus_loadable())
            try:
                import pymumble_py3
            except ImportError as exc:
                raise RuntimeError(_EXTRA_MSG) from exc
            except Exception as exc:  # noqa: BLE001 — opuslib raises a bare Exception (not OSError)
                # when libopus is missing, plus OSError for an unloadable DLL (ADR 0056). At this
                # point the only realistic non-import failure of the import is the opus load.
                raise RuntimeError(f"the Mumble link needs libopus: {opus_install_hint()}") from exc
            self._pm_mod = pymumble_py3
        return self._pm_mod

    # --- lifecycle -------------------------------------------------------------------------

    def connect(self) -> None:
        """Start the library thread toward the server. Non-blocking; idempotent.

        Never calls ``is_ready()`` (it blocks indefinitely on an unreachable server, and we may be
        on the event loop). ``reconnect=True`` makes the library retry dropped/failed connections
        itself; the ``connected`` callback (re)joins the configured channel each time.
        """
        with self._lock:
            if self._mumble is not None:
                return
            pm = self._pm()
            mumble = pm.Mumble(
                self._host,
                self._username,
                port=self._port,
                password=self._password,
                reconnect=True,
            )
            # Incoming voice must be enabled explicitly, and then consumed via the callback (the
            # library buffers it otherwise). The callback fires on the library thread; the bridge's
            # sink is documented thread-crossing-safe, so forwarding directly is correct.
            mumble.set_receive_sound(True)
            mumble.callbacks.set_callback(
                pm.constants.PYMUMBLE_CLBK_SOUNDRECEIVED, self._on_sound
            )
            mumble.callbacks.set_callback(
                pm.constants.PYMUMBLE_CLBK_CONNECTED, self._on_connected
            )
            # The library thread is non-daemon by default and its connection-retry sleep is
            # uninterruptible — a stuck reconnect loop would hold the whole process open at exit
            # (bench-observed: it then raises ConnectionRejectedError into a dying interpreter).
            # Daemonize before start so process teardown never waits on an unreachable server.
            mumble.daemon = True
            mumble.start()
            self._mumble = mumble

    def disconnect(self) -> None:
        """Stop the library thread and drop the connection. Idempotent."""
        with self._lock:
            mumble = self._mumble
            self._mumble = None
        if mumble is None:
            return
        try:
            # stop() closes the control socket, which may not exist if the thread never got as far
            # as a connection (e.g. unreachable host mid-retry) — a fault here must not block
            # teardown, the exit flag it sets first is what actually stops the loop.
            mumble.stop()
        except Exception:
            pass
        try:
            if mumble.is_alive():
                mumble.join(timeout=DEFAULT_JOIN_TIMEOUT)
        except Exception:
            pass

    # --- audio ------------------------------------------------------------------------------

    def send_audio(self, pcm: bytes) -> None:
        """Queue one canonical-PCM frame for the channel; silently dropped until connected.

        ``sound_output`` only exists once the library thread has initialized a connection, and
        audio sent before the server syncs would be discarded anyway — so pre-ready frames are
        dropped here (RF→Mumble audio is a live stream; there is nothing sensible to buffer
        toward a server that isn't there yet).
        """
        mumble = self._mumble
        if mumble is None or not self._ready(mumble):
            return
        try:
            mumble.sound_output.add_sound(pcm)
        except Exception:
            # A race with disconnect/reconnect tearing sound_output down mid-call must never
            # propagate into the bridge's RX fan-out task.
            pass

    # --- status ------------------------------------------------------------------------------

    def status(self) -> MumbleStatus:
        mumble = self._mumble
        connected = mumble is not None and self._ready(mumble)
        roster = self._roster(mumble) if connected else None
        return MumbleStatus(
            connected=connected,
            host=self._host,
            channel=self._channel,
            peers=len(roster) if roster is not None else None,
            users=roster,
        )

    # --- internals ---------------------------------------------------------------------------

    def _ready(self, mumble) -> bool:
        """Whether the library reports a synced connection (its per-connection state attr)."""
        pm = self._pm()
        return (
            getattr(mumble, "connected", None) == pm.constants.PYMUMBLE_CONN_STATE_CONNECTED
        )

    def _on_connected(self) -> None:
        """Library-thread callback on every (re)connect: cap bandwidth, join the configured channel.

        The bandwidth cap must be (re)applied here, not at connect: the library resets its
        bandwidth to the server's max on each connection init, and uncapped it encodes oversized
        voice frames the server silently drops (see :data:`DEFAULT_MUMBLE_BANDWIDTH`).

        A missing channel is survived (stay in the server's root) — a typo'd channel name should
        degrade to "linked, wrong room", never kill the link.
        """
        mumble = self._mumble
        if mumble is None:
            return
        try:
            mumble.set_bandwidth(self._bandwidth)
        except Exception:
            logger.exception("failed to cap mumble bandwidth to %d bps", self._bandwidth)
        if not self._channel:
            return
        pm = self._pm()
        try:
            mumble.channels.find_by_name(self._channel).move_in()
        except pm.errors.UnknownChannelError:
            logger.warning(
                "mumble channel %r does not exist on %s; staying in the root channel",
                self._channel,
                self._host,
            )
        except Exception:
            logger.exception("failed to join mumble channel %r", self._channel)

    def _on_sound(self, user, soundchunk) -> None:
        """Library-thread callback per received voice frame: forward decoded PCM to the sink.

        Also stamps the speaking user's session so :meth:`status` can report who is talking. The
        stamp is best-effort — a user without a resolvable session just never lights the indicator.
        """
        try:
            self._last_audio[user["session"]] = self._clock()
        except Exception:
            pass
        sink = self.on_audio
        if sink is None:
            return
        try:
            sink(soundchunk.pcm)
        except Exception:
            # The sink is the bridge's thread-hop; a fault there must never kill the library's
            # loop thread (which also services the control connection).
            logger.exception("mumble on_audio sink failed")

    def _roster(self, mumble) -> list[dict] | None:
        """Other users in our current channel with a talk flag; ``None`` when unknowable.

        Excludes this client by session (``users.myself_session``). ``talking`` is true when the
        user sent a voice frame within :data:`DEFAULT_TALK_WINDOW`; sorted by name so the UI list is
        stable. Any library hiccup degrades to ``None`` (unknown) rather than raising into the loop.
        """
        try:
            channel_users = mumble.my_channel().get_users()
            myself = getattr(mumble.users, "myself_session", None)
            now = self._clock()
            roster = []
            for user in channel_users:
                session = user["session"]
                if session == myself:
                    continue
                last = self._last_audio.get(session)
                talking = last is not None and (now - last) < self._talk_window
                roster.append({"name": user["name"], "talking": talking})
            roster.sort(key=lambda u: u["name"].casefold())
            return roster
        except Exception:
            return None
