# 0052 — The M17 Link backend: binding the client, codec, and parsers to the Link protocol

Status: Accepted

## Context

This is the final cycle of the M17 backend arc, and it invents nothing. Every part exists and is
reviewed:

- ADR 0041/0044/0047 — the `Link` protocol and `StreamEdge` in both directions.
- ADR 0049 (cycle 54) — the Codec2 mode-3200 seam over `libcodec2` via ctypes.
- ADR 0050 (cycle 55) — the pure, stdlib-only M17 wire codec: base-40 callsigns, control packets,
  the 54-byte stream frame with its 16-byte payload.
- ADR 0051 (cycle 56) — the `M17Client` UDP lifecycle: handshake, keepalive, teardown, and the
  load-bearing source-address validation.

This cycle **binds** them into `M17Link`, a `Link` implementation registered as `"m17"` in
`create_link`. It is wiring, not design: `M17Link` drives `M17Client` for the socket, `Codec2` for
the audio payload, and `packet.py` for the frame bytes.

**Why the binding is a clean drop-in.** The inbound-TX machinery is already built and reviewed —
`LinkTxBridge` (ADR 0048), `LinkFeeder`/`LinkPump`, `TxLimiter` (ADR 0045), `TxSlot`, the arbiter.
Every one of them calls *only* the `Link` protocol methods. So `M17Link`'s whole job is to honor
that protocol: a synchronous, non-blocking `receive()` yielding `AudioFrame | StreamEdge | None`,
`transmit()`/`stream()` for outbound, `status()`, and capability self-gating. If it does, those
machines run unchanged — that is the payoff of the last several cycles, and this ADR spends none of
it.

## Decision

### `M17Link` in `radio_server/link/m17_link.py`

`M17Link` imports `radio_server` types (`.base`, `..audio`, `..audio.codec2`) and so **cannot** live
in the `link/m17/` subpackage, whose ADR-0050 purity guard forbids any `radio_server` import in
that leaf. It sits one level up, beside `mock.py`, exactly as `MockLink` sits above the audio and
base layers it composes. The `m17/` leaf is untouched (its only edit this cycle is one public
outbound sender on `client.py`, below).

Like `MockLink`, an `M17Link` is **born disabled** — there is no constructor argument that starts
it enabled and nothing is loaded from persistence. The only path to `enabled=True` is a deliberate
runtime `enable()`; a reboot always comes up disabled (the ADR-0041 safety property, enforced
structurally at the leaf).

### The sync ↔ async adapter

The `Link` protocol methods are synchronous and non-blocking — the machinery calls them from its
async poll loops and never awaits them. `M17Client` is async: `connect()`/`close()` are coroutines
and inbound frames arrive on an `asyncio.Queue`. `M17Link` is the adapter between the two:

- `connect(target)` records the target, builds a fresh `M17Client` for the configured reflector,
  and **schedules** `client.connect()` as a task on the running loop (it does not await). A fresh
  client per `connect` lets a `set_listen_only(True)` before it select `LSTN` over `CONN`.
- `disconnect()` schedules `client.close()`.
- `receive()` drains the client's frame queue with `get_nowait()` — non-blocking, returning `None`
  when there is nothing this poll (or no client yet).
- `status().connected` reflects the client's live connection state.

### The mapping — the whole cycle

**Inbound** (`receive()`), one M17 stream frame at a time off the client's queue:

- a frame whose `stream_id` differs from the current stream → emit `StreamEdge.START` once, and set
  `LinkStatus.talker` from that frame's LSF source callsign (`StreamFrame.src` — cycle 55 pinned
  the talker here; this is where it is wired).
- the 16-byte payload → `Codec2.decode` → one canonical 48 kHz `AudioFrame`.
- a frame with the end-of-stream bit set (`StreamFrame.last`) → emit `StreamEdge.END` after its
  audio, and clear the talker.
- a `LOST` connection → **connection state only, never a synthesized `END`**. ADR 0051 held this
  line and this cycle holds it: a keepalive timeout enqueues no frame, so `receive()` cannot emit an
  `END` on loss. `status().connected` goes `False`; the unpaired-`START` backstop (`tx.idle_timeout`,
  ADR 0047) is what drops PTT if a stream was mid-flight. Manufacturing an `END` from a timeout
  would be a lie about the stream.

**Outbound** (`stream()`/`transmit()`):

- `stream(True)` → begin a stream (an M17 LSF rides in every stream frame): allocate a stream id and
  reset the frame counter.
- `transmit(AudioFrame)` → buffer, and every 40 ms (see the arithmetic below) `Codec2.encode` a
  chunk to a 16-byte payload and `build_stream(...)` it with `src` = the station callsign and `dst`
  derived from the configured module.
- `stream(False)` → the end of the transmission (EOT): mark the final stream frame with the
  end-of-stream bit.

### Frame-rate impedance — the arithmetic (derived from the spec, not recalled — guardrail 1)

The three rates do not line up for free, so state them:

- Codec2 3200 is **160 samples @ 8 kHz = 20 ms**, encoded to **8 bytes** (queried from the library
  at runtime, ADR 0049).
- An M17 stream frame's payload is **16 bytes = two Codec2 frames = 40 ms** of voice (ADR 0050,
  `packet.py`).
- The canonical block is **20 ms @ 48 kHz = 960 samples = 1920 bytes**; 40 ms = 1920 samples =
  **3840 bytes**. 48 kHz / 8 kHz is a clean 6× resample, done *inside* `Codec2.encode`/`decode` — so
  `M17Link` never calls the resampler directly.

The consequence is a buffering boundary on the **outbound** path: canonical audio arrives in 20 ms
blocks, but an M17 frame carries 40 ms. `M17Link` accumulates until it has a whole 40 ms, encodes
that, and sends one stream frame; it holds the most recent frame back so the final one can carry
the end-of-stream bit when `stream(False)` arrives.

**Fail loud on a partial frame at END — never pad.** If `stream(False)` arrives with a partial
(non-empty, sub-40 ms) buffer, `M17Link` **raises** rather than emit a half frame or silence-pad a
stream frame into existence. Silently padding would put audio on the reflector (and, relayed, on
the air) that the operator never spoke; a half frame is a malformed packet. The mandate for this
cycle is to fail loud instead. The residual: a feed that ends on an odd count of 20 ms blocks will
raise. A real M17 voice feed is expected to be 40 ms-aligned; confirming that against the live
feeder and reflector is a bench-cycle concern (guardrail 1). Inbound needs no such buffer — one
stream frame decodes to exactly one 40 ms canonical `AudioFrame`, and nothing downstream
(`LinkTxBridge`, `LinkPump`, `/audio/link`) requires 20 ms granularity; they treat
`AudioFrame.samples` as opaque bytes.

### Capabilities — the point of the flat split (ADR 0041)

- `LISTEN_ONLY` → **yes**. `LSTN` is a protocol mode; this is the zero-credential listening tier,
  the reason M17 was chosen for the arc. `set_listen_only(on)` selects `LSTN` over `CONN` on the
  next connect.
- `DIRECTORY` → **no**. M17 has no central user database — on M17 your callsign is your identity.
  `directory()` raises `UnsupportedLinkCapability(DIRECTORY)`, which the API turns into a **501 by
  name** (`GET /link/directory`), never a silent empty list.

### Codec2 is imported only for M17

`Codec2` is constructed inside `M17Link.__init__` via a **local** import — the codec2 module is
never imported at rest, only when a configured M17 backend is built. A missing `libcodec2` is a
config error surfaced loudly there, naming the library and the `codec2` extra (ADR 0049's shape). A
user without the extra and without `link.backend = "m17"` never constructs `Codec2` and never sees
`libcodec2` mentioned. For testability `M17Link` accepts a pre-built `codec` (the same seam shape as
`Codec2`'s monkeypatchable `find_library`); the default path builds the real one and keeps the
fail-loud contract.

### Configuration lands here (deferred from cycle 56)

The five `[link]` keys ADR 0051 seeded are added to the schema this cycle and passed through
`create_link(**kwargs)`:

| Key | Default | Note |
|-----|---------|------|
| `link.reflector_host` | `""` | reflector hostname/IP; empty is invalid for `m17` |
| `link.reflector_port` | `17000` | mrefd default UDP port (marked default) |
| `link.reflector_module`| `A` | one of `A`–`Z`, part of the address |
| `link.bind_host` | `0.0.0.0` | the reflector is remote and must reach us — not loopback |
| `link.bind_port` | `0` | ephemeral |

The M17 source callsign is **`station.callsign`**, reused — there is no second callsign setting.
`station.callsign` is `REQUIRED` and already fails loud when unset, which is the legally-correct
behavior: no callsign, no keying. An unencodable callsign raises in cycle 55's base-40 encoder; that
is left to raise.

## Consequences

- `link.backend = "m17"` now boots a real reflector link — born disabled, enabled only by
  `POST /link/enable`, torn down by `POST /link/disable`'s hard-unkey. The `LinkFeeder`,
  `LinkTxBridge`, `TxLimiter`, `TxSlot`, and arbiter are unchanged: the backend obeys the protocol,
  so they work as-is.
- The M17 backend arc is complete in software. What remains is a separate **bench** bring-up (real
  reflector, hardware): confirming the LSF `TYPE`/`DST` encoding on the wire, that the live feeder's
  audio is 40 ms-aligned, and the keepalive cadence against a real mrefd — the empirical phase ADR
  0041 always reserved for "real transports last."
- The `link/m17/` purity guard stays green: the leaf still imports nothing from `radio_server`, and
  `socket` still lives in exactly `client.py` (which gains one public sender, no new import).
