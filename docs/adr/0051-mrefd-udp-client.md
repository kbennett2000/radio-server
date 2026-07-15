# 0051 — The mrefd UDP client: connection lifecycle and source-address validation

Status: Accepted

## Context

This is the third cycle of the M17 backend arc. Cycle 54 (ADR 0049) built the Codec2 seam — the
audio payload M17 carries. Cycle 55 (ADR 0050) built the *wire format* — base-40 callsigns, the
mrefd reflector control packets, and the 54-byte stream frame — as a pure, stdlib-only leaf under
`radio_server/link/m17/`, with an AST test asserting no module imports `socket`. That cycle
proved the bytes byte-exact against the spec *before* any socket existed to obscure a framing bug.

This cycle builds **the socket**: an asyncio UDP client, `M17Client`, that speaks the mrefd
reflector protocol using cycle 55's `build_*`/`parse_*` functions for every byte on the wire. It
manages only the **connection lifecycle** — handshake, keepalive, loss detection, teardown. It is
deliberately *not* a `Link`: binding `M17Client` + the parsers + the Codec2 seam into the `Link`
protocol behind `create_link` is the next cycle. The socket lands alone so the lifecycle and — the
load-bearing part — the safety posture of an internet-facing datagram port can be reviewed on
their own.

**Why this is its own cycle: an inbound datagram can key the transmitter.** Cycle 53 (ADR 0048)
wired `LinkTxBridge`, where an inbound `StreamEdge.START` calls `radio.ptt(True)` and the frames
that follow go out the antenna under the licensee's callsign. A UDP socket accepts datagrams from
*anyone*. So the first thing a socket on this path must do is decide whose datagrams it will even
look at. That decision — source-address validation — is the reason this cycle exists separately
from the `Link` binding that consumes it.

**The protocol facts are published, so they were read this cycle, not recalled** (guardrail 1).
Every lifecycle number below was read from `n7tae/mrefd`'s `Packet-Description.md` and `README.md`
and encoded as a test or a cited constant:

- **Handshake.** A simple client sends `CONN` (11 bytes: 4-byte magic + 6-byte base-40 callsign +
  1-byte module letter). The reflector replies `ACKN` (bare 4 bytes) on success — "the client has
  successfully linked; keep-alive packets commence immediately" — or `NACK` (bare 4 bytes) when
  the requested module does not exist or the callsign is blacklisted.
- **Listen-only tier.** `LSTN` is the same 11-byte shape as `CONN` with a more permissive
  callsign — the protocol-level zero-credential listening request (the `LISTEN_ONLY` capability of
  ADR 0041).
- **Keepalive.** The **reflector** sends `PING` (10 bytes: magic + sender callsign) "approximately
  every 3 seconds" to every connected node; on receiving a `PING` the client replies `PONG`.
  **"If a node hasn't received a `PING` or `PONG` in the last 30 seconds, it can assume the
  reflector has stopped working."** Replying to `PING` is therefore a *liveness requirement*, not
  an optimization: a silent client is dropped.
- **Disconnect.** The client sends `DISC` (10 bytes: magic + callsign) to unlink; the reflector
  acks with a bare 4-byte `DISC`. The reflector may also initiate `DISC`.
- **Address.** mrefd serves up to 26 modules (`A`–`Z`); the module is part of the address, carried
  in `CONN`/`LSTN`, not an afterthought. The default mrefd UDP port is **17000**.

**Licensing.** The posture is inherited from ADR 0050: mrefd is GPL, this project is MIT. We
implement the protocol *from* its published description and exchange its bytes on the wire — we do
not link mrefd and do not copy its text or code. Reading a keepalive interval out of a
specification and honoring it is the idea crossing the boundary, never the expression.

## Decision

### An asyncio `M17Client` in `radio_server/link/m17/client.py`

The client lives *in* the `m17` subpackage, beside the codec it drives, but it is the one module
there that touches the network. It imports `asyncio`, `socket`, and the standard library, plus the
cycle-55 builders and parsers by **relative** import — and nothing from `radio_server` absolute,
so it stays a leaf (its configuration is passed in as plain constructor values, not a `Settings`
object; that wiring is the next cycle's). It **builds no packets itself**: every byte it sends is
a `build_*` call and every byte it receives goes through `parse_control`/`parse_stream`. The
ADR-0050 malformed-input rule holds unchanged — a bad datagram parses to `None` and is dropped,
never a half-parsed object, never an exception on the receive path.

Its lifecycle follows the owned-task shape of `ScanRunner` (ADR 0028): a single background
watchdog task, synchronous state transitions, and an idempotent teardown that clears its
references before awaiting the cancel. Connection state is a small enum —
`DISCONNECTED` / `CONNECTING` / `CONNECTED` / `LOST` / `CLOSED` — surfaced as a property plus an
`asyncio.Event` that fires on every transition. Inbound stream frames are put on an
`asyncio.Queue` for a future consumer to drain; control packets (`PING`/`ACKN`/`NACK`/`DISC`) are
handled inside the client and never reach that queue.

`connect()` sends `CONN` (or `LSTN` when `listen_only`), awaits `ACKN`/`NACK` against a bounded
`connect_timeout`, and on `ACKN` starts the keepalive watchdog. The watchdog implements exactly
the published liveness rule: any datagram from the reflector refreshes a "last heard" timestamp,
an inbound `PING` is answered with a `PONG`, and if nothing is heard for `keepalive_timeout`
(default **30 s**, the mrefd figure) the connection is declared `LOST`. `close()` sends `DISC` and
tears the socket down.

### The load-bearing safety call: validate the source address

**Every datagram whose source `addr` is not the connected reflector's resolved `(ip, port)` is
dropped before it reaches the parsers.** The client opens an *unconnected* datagram socket (it
does not pass `remote_addr` to `create_datagram_endpoint`), so the kernel delivers datagrams from
any source and this check — our own code, in `datagram_received`, ahead of any `parse_*` call — is
the gate. This is deliberate: a kernel-connected socket would filter for us, but then the guardrail
would be untestable and invisible. Making it our first line of code makes it the outermost, and
lets a test send a datagram from the wrong port and assert it never reaches the queue.

Without this check, anyone who learns the host's IP could send a well-formed stream frame to the
port and — once the next cycle wires this to `LinkTxBridge` — key the transmitter, with no
reflector, no valid callsign, and no connection ever established. Source validation removes that
open door.

**Residual risk, stated honestly.** This check is spoofable. UDP has no authentication, and M17
has no central identity *by design* — on M17 your callsign is your ID and there is no authority to
verify it against. An attacker who both learns the host's IP *and* forges the reflector's source
address defeats the `(ip, port)` comparison. Source validation is the cheap outer gate that stops
the trivial attack (spraying a known IP); it is **not** cryptographic authentication and this ADR
does not claim it is. The real bounds on what an inbound stream can actually *do* are elsewhere
and already built:

- `TxLimiter` (ADR 0045): `max_tx_seconds` force-unkeys a runaway; `tx_cooloff` refuses an
  immediate re-key.
- `tx.idle_timeout` (ADR 0047): the backstop for an unpaired `START` that never gets its `END`.
- The `TxSlot` rule (ADR 0048): the local operator owns the station; an inbound `START` is dropped
  while the slot is held, never preempting.
- `POST /link/disable` (ADR 0048): a synchronous hard-unkey that drops PTT mid-frame, and the
  enable gate that keeps a link born disabled after any reboot (ADR 0041/0042).

Source validation is one layer of that defense in depth, not a substitute for it.

### Bind posture: radio-server's first non-HTTP listener

Today the only listener is the HTTP/WebSocket app, which binds loopback by default (`server.host`
= `127.0.0.1`) because it is safe there. This socket is different: the reflector is **remote**, so
the client must bind a routable local address to receive its replies. The default is
`bind_host="0.0.0.0"` on an ephemeral port (`bind_port=0`), and the exposure is stated plainly:
that ephemeral port accepts datagrams from anywhere on the reachable network. Source validation
plus the TX bounds above are what make that survivable — they are not incidental, they are the
reason this posture is acceptable. There is no UPnP, no hole punching, and no proxy; if NAT
between the host and the reflector breaks the return path, solving it is a later cycle's problem,
not something this client papers over.

### Connection loss is not end-of-stream

When the keepalive watchdog fires, the client transitions to `LOST` and stops — it surfaces the
loss as **connection state** and lets the layer above decide what to do. It must **not**
synthesize a `StreamEdge.END`. Silence from a reflector says nothing about whether a transmission
was in progress or where it ended; manufacturing an `END` from a timeout would be a lie about the
stream. Stream-edge synthesis belongs to the `Link` binding cycle, and ADR 0047 already pins
`tx.idle_timeout` as the backstop for a stream whose `END` never arrives. Accordingly this client
has no dependency on `StreamEdge` at all.

### Configuration is deferred to the binding cycle

`M17Client` takes its reflector host/port/module, its callsign, and its bind address as plain
constructor arguments. No `config/spec.py` keys are added this cycle, because nothing constructs
the client from settings yet — `create_link` is untouched. The binding cycle will add the
`[link]` keys and pass them through `create_link(**kwargs)`, following the ADR-0042 precedent
where `link.max_tx_seconds` was seeded into the schema a cycle before the limiter that reads it.
The intended keys are recorded here so the binding cycle inherits the shape:

| Key | Default | Note |
|-----|---------|------|
| `link.reflector_host` | *(required to connect)* | reflector hostname/IP |
| `link.reflector_port` | `17000` | mrefd default UDP port |
| `link.reflector_module`| `A` | one of `A`–`Z` |
| `link.bind_host` | `0.0.0.0` | remote reflector must reach us; not loopback |
| `link.bind_port` | `0` | ephemeral |

## Consequences

- The M17 subpackage is no longer uniformly socket-free. The cycle-55 purity guard **evolves**
  rather than relaxes: the codec modules (`callsign`, `crc`, `packet`) stay stdlib-only with no
  `socket` and no `radio_server` import, and `client.py` becomes the *sole* socket owner — the
  test now asserts `socket` appears in exactly that one module and nowhere else in the subpackage,
  and that `client.py` still imports nothing from `radio_server`. The invariant got more precise,
  not weaker.
- The client is fully testable against a localhost fake reflector: handshake, `NACK` refusal,
  `PING`→`PONG`, `DISC` teardown, a wrong-source datagram dropped before parsing, and connection
  loss surfacing as `LOST` state (never a stream edge). No test touches a real reflector or the
  network beyond loopback.
- Still ahead (the binding cycle): the `M17Link` behind `create_link` — mapping `StreamFrame.src`
  to `LinkStatus.talker`, synthesizing `StreamEdge.START`/`END` from the M17 stream, resampling
  the Codec2 payload to canonical audio, and adding the `[link]` config keys above. That is its
  own empirical bring-up (ADR 0041's "real transports last").
