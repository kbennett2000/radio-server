# ADR 0171 — A dead RX listener gets reaped on a quiet channel

**Status:** Accepted · 2026-08-01 · closes the leak
[ADR 0170](0170-a-stranded-slot-stops-being-invisible.md) measured and carried

## Context

ADR 0170 answered a question it was not built to fix. Measured against real uvicorn with the deployed
`squelch = "audio"` gate, a dropped `/audio/rx` listener was **still counted at +50 s** — past
uvicorn's 20 s keepalive, and **through a signal that woke every other reader**. The handler parked
on an unbounded `queue.get()` and only ever *sent*, and uvicorn drops sends silently on a reset
transport rather than raising.

That last fact is load-bearing and is not re-litigated here: **send-based liveness detection is ruled
out by measurement.** No design in this ADR tries to learn anything from a send.

The cost was bounded and permanent: one hub subscription, `rx_demand.requested` pinned ≥ 1, and
therefore the single capture reader held open for the life of the process. Restart was the only cure.

## The measurement that came before the design

The brief proposed three candidate mechanisms. Rather than pick one, the first thing built was a
probe, because the whole design rests on a claim that had never been checked: that the ASGI
disconnect **is already delivered** and simply never read.

Two arms and a control, against real uvicorn on the production `websockets` path, with the keepalive
at its production setting — ADR 0170 was burned by a rig with `ws_ping_interval=None`, so the probe
also ran an arm with no ws kwargs at all to confirm the pinned values *are* the inherited defaults
(they are: 20.00 s either way, to the centisecond).

| shape | drop | result |
|---|---|---|
| **control** — master's send-only loop | RST | **still parked at 55 s** (the rig reproduces the leak) |
| proposed — concurrent `receive()` | RST | disconnect at **20.00 s**, code 1005 |
| proposed, no ws kwargs | RST | **20.00 s** — identical |
| proposed | clean close | **immediate** |

**The disconnect was always being delivered. Nobody was listening.** All four streaming handlers
never called `websocket.receive()`; the message sat unread on the channel that carries it.

That refines ADR 0170's lesson rather than contradicting it. *"One loop has a bounded await and the
other does not"* is true of TX — but the timeout is not what makes TX safe, **reading the receive
channel at all** is. The timeout only covers TX's RST case, where nothing is ever delivered.

**And 20 s is not `interval + timeout`, which is the reason this ADR reports a window.** Writing a
ping to a reset socket fails immediately, so the pong timeout never runs. The other edge belongs to a
peer whose path still accepts writes but never answers — a frozen machine, a blackholing firewall.
Reproduced without root by completing the handshake over a raw socket and then never speaking again:

| peer goes away by… | reaped after |
|---|---|
| clean close | immediate |
| RST — the keepalive **write** fails | **19.7 s** |
| silence, socket still open — no pong | **40.0 s** |

## Decision

### 1. One helper, four call sites — a concurrent disconnect watcher

`stream_until_disconnect(websocket, queue, send)` races the drain against a task that reads the
receive channel, and cancels both on the way out. It is **not a timeout**: on RX a silent queue is
*legitimate*, so a timeout could only busy-loop or drop a listener doing nothing wrong.

The brief's line was *"three hand-written variants is how the fourth gets written wrong"* — **and the
fourth already existed when that was written.** `/events` has the identical parked-forever shape and
**predates all three RX paths**. It is fixed here too, by the same helper.

### 2. The alternatives, and what they would have cost

| candidate | cost | verdict |
|---|---|---|
| **Client heartbeat** | Changes the client contract. `useRxAudio.js` sends nothing today, nor do the six bench clients; every one would need updating, any third-party client silently breaks, and an alive-but-busy client gets wrongly reaped — to obtain a signal the transport already delivers for free. | Rejected |
| **uvicorn's ping alone** | Closes the transport and *posts* a disconnect, but ASGI cannot abort an app task that never asks. ADR 0170 measured exactly this: +50 s, past the keepalive. | Necessary, not sufficient — this design *uses* it |
| **Bound the `queue.get()`** | A wakeup with nothing to test against. | Rejected |
| **Server-sent keepalive frame** | Wrong twice: sends do not raise on RST, *and* a synthetic frame is a Part 97 hazard (below). | Rejected |

### 3. The ping is pinned because the reap *depends* on it

`__main__.py` passed **no ws kwargs at all**, so RST detection silently rode on uvicorn's defaults.
The argument for pinning is not that it documents a default — **it is that a load-bearing behaviour
depended on a value this project never chose.** A version bump could change or drop it and nothing
would notice; the symptom would be listeners quietly staying counted again, which is precisely the
defect this ADR exists to fix. Two tests hold it: one that the kwargs reach `uvicorn.run` at all, one
that the values are the measured ones.

**The window is reported as a window, never as a number.** Both edges fall out of the two settings
and neither moves without the other, so a single figure would be read as a guarantee. `10/10` was
rejected — it would roughly halve the window at the cost of doubling ping traffic on every socket
including `/audio/tx` mid-transmission, and picking a number before the bench has produced one is
what this project keeps getting burned by. Recorded as the available knob, with its price.

### 4. Off the audio path by construction, not by convention

The two Part 97 taps sit at different layers and the watcher is on neither: DTMF decode taps `RxPump`
at `radio.receive()` level, on the raw `AudioFrame`, **before** the gate and before any hub publish;
ADR 0162's relay mute sits on `AudioHub` subscriber queues. **The watcher publishes nothing,
subscribes to nothing, and injects nothing** — it reads the socket's inbound control channel.

This is also the concrete harm that killed the keepalive-frame alternative. A tick on the hub reaches
`MumbleBridge._send_to_mumble` inside a bare `except Exception: pass` — silently swallowed — and on
the D-STAR side reaches `AudioFrame(...)` → `_rf_gate` **before** `_deafened()`, mutating the
hysteresis state of a Part 97 control. Not hypothetical.

Three tests pin it: nothing is published to the hub during a reap; a relay subscriber receives the
radio's bytes byte-for-byte with nothing appended; the DTMF tap sees exactly the frames
`radio.receive()` produced. And `test_relay_subscribers` — a **source-text** scan that a rename would
silently defeat — was re-run against the tree rather than trusted: still exactly three files, one hit
each, and no new `subscribe()` anywhere.

**`requested` keeps its name.** The reap makes the number bounded, not instantaneous; a name
promising live listeners would over-claim by up to 40 s. The limit changed, the discipline did not.

## Acceptance

- **Red run**, against master: **7 failed, 2 passed** — the 2 passes named as pins, not counted.
- **A red run that first passed for the wrong reason, and the harness bug it exposed.** The obvious
  harness — `wait_for(handler(), timeout=…)` — **passes on master**: the timeout cancels the handler,
  the handler swallows `CancelledError` by design (ADR 0122) and unwinds its context managers on the
  way out, so the cleanup under test happens, driven by the test rather than by the code. A test that
  cannot fail, dressed as one that passed. `asyncio.shield` keeps the cancellation off the handler;
  the assertion that says it plainest is `ws.receives >= 1` — **master never asks**.
- `uv run pytest` **2276 passed / 5 skipped** (baseline 2265/5). `npx vitest run` **14 files / 155
  tests**, unchanged — no UI change was needed and none was invented.
- **Real-uvicorn smoke** (not `TestClient`, which sends its own disconnect and cancels the task):
  clean close **0.04 s**, RST **19.67 s**, `reader_running` unwinding to `false`, and five 960-byte
  frames delivered byte-for-byte to a live listener. PASS.
- **Bench, on the deployed station**, the same script run before and after the deploy:

  | | before (`962f847`) | after (`0e1f9cf`) |
  |---|---|---|
  | clean close | 10.01 s (waiting for a frame) | **0.01 s** |
  | RST | **STILL COUNTED at 55 s** | **19.58 s** |
  | after the next listener came and went | zombie **still counted** | back to baseline |

- `acceptance.py`: **8 of 10 stages PASS, `split-minus` SKIP, `web` FAIL, exit 1** — see findings.
  `rx`, `dtmf`, `tx`, `split` and `services` all PASS, which is the real-RF proof that the reaper does
  not drop working listeners and that the DTMF tap still decodes.
- Station left on **147.555**, tune persist **off**, broadcast FM **off**, `tx_ok: true`, serial
  transport alive.

## Consequences

- A departed listener is now always eventually reaped, on all four streaming sockets.
- The reap latency is a property of two uvicorn settings, now explicit and tested.
- `/audio/rx` and friends now read their receive channel. The contract is unchanged for clients —
  nothing is required of them — but it is written down in `api.md` rather than implied.
- Two tasks per streaming connection instead of one.

## Findings

1. **`acceptance.py` is exit 1, and the cause is still not this cycle.** The only failing check in
   `web` is `kv4p GET /healthz → 404` — verified by re-running the stage alone rather than inferred
   from the summary, because the brief was right that a known failure can mask a new one. Every other
   check passes, including the station's own `/healthz` and `radio serial reader: alive`. The witness
   (8091) is on `a6a4cd4` carrying **uncommitted edits to three files**; updating it would discard
   somebody's work. **Still the operator's call, and still not taken.**
2. **ADR 0170's zero-carrier `tx`/`split` FAIL did not recur.** Watched for deliberately. Both stages
   passed on the full run — `kv4p saw carrier 10`, RMS 12196, 1000 Hz at 0.964, CTCSS 0.023, `infx`
   leg ratio. One sighting, still unexplained, still not reproduced.
3. **The bench instrument is flaky, and it nearly got blamed on the server.** `websockets.sync.client`
   on the station intermittently times out waiting for the handshake response — 3/12 on `/audio/rx`
   and 5/12 on `/events` in one sitting. It looked exactly like a regression from this cycle. It is
   not: the **old-code witness on 8091 shows it too**, `/status` over HTTP showed no event-loop stall
   at all (median 5 ms, max 13 ms, zero over 1 s, on both instances), and a **raw TLS upgrade against
   the same endpoints was 20/20 at 3–4 ms**. The fault is in the sync client, not the server. Worth
   recording because the first, cheapest, wrongest move was to call it environmental and move on.
4. **The reaper kills zombies, not backpressure — a successor must not read it as leftovers.**
   `AudioHub` caps at 64 frames with drop-oldest; **`EventHub` has no cap and no overflow handling**.
   This cycle removes subscribers whose peer is *gone*. A **live-but-slow** consumer — still
   connected, still draining too slowly — grows that unbounded queue without limit, and **nothing here
   touches that.** It is a different failure with a different fix.
   **Giving `EventHub` a maxsize was deliberately excluded, not overlooked:** capping it changes
   event-drop semantics and can silently discard status events, which is a behaviour change this
   cycle was not asked to make and could not have proved safe from here.
5. **The updater note in the brief was stale, and was checked rather than obeyed.** The box carries
   `8f82710` with fast-forward support because it is on a build that includes ADR 0169, so the normal
   `./update-radio-server.sh origin/<branch>` form worked. ADR 0169's promise that the ritual deletes
   itself is now observed rather than predicted.
6. **`systemctl` needs `--user`.** Plain `systemctl is-active radio-server` answers `inactive` from
   the system manager while the unit is a healthy *user* service. Measured this session; it reads as a
   dead station and is not one.
7. **A fifth parked `queue.get()` exists and is correctly out of scope.** The lifespan's log-drain
   task subscribes to the event hub and parks the same way, but it is an internal task cancelled at
   shutdown, not a socket with a peer that can vanish.

## Out of scope

The fork; the squelch-composition successor; the witness checkout; bounding `EventHub`'s queue
(finding 4); any change to what `AudioHub` carries or to acquire/release semantics.
