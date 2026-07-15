# 0053 — `doctor --link`: the M17 reflector bring-up instrument

Status: Accepted

## Context

The M17 backend arc is built and mock-green. Cycle 54 (ADR 0049) built the Codec2 seam, cycle 55
(ADR 0050) the pure wire codec, cycle 56 (ADR 0051) the `M17Client` socket, and cycle 57 (ADR 0052)
the `M17Link` that binds them behind `create_link`. Every byte is unit-tested against a localhost
fake reflector. **None of it has ever touched a real reflector.**

The next step is a human, empirical bench cycle (guardrail 6). Cycle 57 closed by naming three facts
that only the wire can settle:

1. the LSF `TYPE` / `DST` on-the-wire encoding — is the frame the reflector sends shaped the way
   `parse_stream` reads it?
2. whether the live inbound feed is genuinely 40 ms-aligned — the frame-rate arithmetic `M17Link`
   depends on, asserted from the spec but never measured off a real reflector;
3. the real keepalive / `PING` cadence — the spec says "approximately every 3 seconds," which is a
   thing to confirm, not recall (guardrail 1).

You cannot walk onto the bench and eyeball those facts with the runtime path alone. This cycle builds
**the tool that makes them observable** — exactly as `doctor --rx-level` / `--tx-tone` / `--key-test`
(ADR 0029) were built to precede the AIOC bring-up, and were how the DTR-vs-RTS PTT line was
established empirically. The doctor is where the wire gets *read* before the app trusts it.

This cycle is only the instrument. Running it against a live reflector, and the later stages
(HT → reflector, reflector → radio) driven by the real app, are the bench cycle itself.

## Decision

### Two read-only modes on `radio_server/doctor.py`

Following ADR 0029, `doctor` gains two mutually-exclusive mode flags — no new entry point:

- **`--link-listen`** — `LSTN` a real reflector and report, live: the handshake and its timing, the
  observed `PING` cadence, per-stream talker / frame count / duration / **measured** inter-frame
  interval, the raw LSF bytes (hex) of each stream's first frame, and the count of datagrams dropped
  by source validation. **No callsign on the air, no keying, no radio.** The zero-risk first bench
  stage and the default of the two.
- **`--link-decode`** — everything `--link-listen` does, plus Codec2-decode the payload and write the
  decoded audio to a WAV. This answers the one question the codec seam has left: does a stranger's
  voice come out *intelligible*? ADR 0049 asserts geometry and non-silence; perceptual quality is a
  bench fact, not something a unit test can judge.

### The load-bearing call: a raw observer, not a wrapper around `M17Client`

`M17Client` (ADR 0051) is exactly the right shape for the *runtime* path and exactly the wrong shape
for a bring-up instrument. Two of the six things this tool must show are **structurally invisible**
through it, by its correct design:

| The tool must observe | Through `M17Client`? |
|---|---|
| Handshake `LSTN` → `ACKN`/`NACK` + timing | yes — `connect()` / `on_state_change` |
| Datagrams dropped by source validation (count) | yes — the public `dropped_source` int |
| Per-stream talker / frame count / duration | yes — `frames` queue (`StreamFrame.src`, `.last`) |
| **Observed inter-frame interval (ms)** | only by timestamping `frames.get()` |
| **Raw LSF bytes (hex)** | **no** — `parse_stream` discards the datagram; `StreamFrame` keeps only decoded fields |
| **Observed `PING` cadence** | **no** — control packets are consumed in `_handle_control` and never surfaced; `_last_rx` is private |

The client is *right* to swallow control packets and drop raw bytes — the runtime never needs them.
But the diagnostic does, and the DO-NOT of this cycle is firm: **do not touch the client, the codec,
or the config schema.** Perforating `M17Client` with observability hooks it never needs to serve a
diagnostic would couple the runtime to the bench. Rejected.

**Reconstructing the LSF from `StreamFrame`'s decoded fields is also rejected** — and this is the
subtle one. Re-encoding `src`/`dst`/`frame_type`/`meta` back to 28 bytes would print *our* encoding,
not the wire's. The entire point of "eyeball `TYPE`/`DST` against the spec" is to catch a parser that
reads the wrong bytes; showing a re-encode of the parser's own output would hide exactly the bug the
operator is looking for. The hex must be the bytes that arrived.

So the doctor opens its **own** read-only `LSTN` socket and reads the raw wire, reusing the **pure**
`packet.py` codec (`build_lstn` / `build_pong` / `parse_control` / `parse_stream` / `decode_callsign`)
— which is not off-limits. It does not reimplement the wire format or the audio codec; it
reimplements only the *observation loop*, keeping the raw datagram and surfacing the control-packet
timing the client deliberately hides. This is the same separation as `doctor --rx-level`, which reads
the sound card directly rather than through the backend.

Concretely: a small `LinkObserver` holds all the accounting and is **pure and socket-free** — it
takes `(data, addr, now)` and returns an optional reply — so the interval and cadence math is
unit-tested deterministically with an injected clock, exactly as `measure_rx_levels` /`collect_dtmf`
are. A thin async driver (`_observe_link`) owns the socket: it mirrors `M17Client.connect`
faithfully (prefer-IPv4 `getaddrinfo`, an *unconnected* `create_datagram_endpoint`, `sendto(LSTN)`,
await `ACKN`/`NACK`) and then feeds each datagram to the observer, sending back whatever reply it
returns.

### What it sends: `LSTN` and `PONG` only — never RF

"Nothing here transmits" means nothing reaches the antenna. The tool sends exactly two kinds of UDP
control packet:

- **`LSTN`** — the listen-only link request. It *is* the mechanism the scope names ("`LSTN` a real
  reflector"); a listener is never relayed onto the air by the reflector.
- **`PONG`** — the keepalive reply. ADR 0051 records the published rule: a node that stops answering
  the reflector's `PING` is dropped after ~30 s. To observe the cadence over minutes, the tool must
  stay alive, so it answers `PING` with `PONG` — the same liveness reply the runtime client makes.

It sends **no `CONN`** (which requests a relayed, transmit-capable link), **no stream frame**, and
**never asserts PTT** — there is no `radio` object in these modes at all. `LSTN` + `PONG` are UDP
session-maintenance, not modulation. That is why the tool is zero-risk and needs no `CONFIRM` /
dummy-load guard, unlike the keying modes `--key-test` / `--tx-tone`.

### Fail loud, by name

A diagnostic exists for the moment something is wrong; a vague failure defeats it. Each mode fails
loud, naming the thing:

- **No reflector config** — an empty `link.reflector_host` or an unset `station.callsign` prints a
  `[FAIL]` naming the missing key and how to set it, and exits non-zero. (Unlike `_baofeng_config`,
  which defaults silently — a diagnostic with no reflector configured has nothing to point at.)
- **Unresolvable host** — a `socket.gaierror` from resolution prints a `[FAIL]` naming the host.
- **`NACK`** — the reflector refused the `LSTN` (module full/blocked, callsign denied); named as such.
- **No `libcodec2`** (`--link-decode` only) — `Codec2()` already raises a `RuntimeError` naming
  `libcodec2` and the `codec2` extra (ADR 0049); the driver constructs it *first*, before touching
  the socket, and surfaces that message as a `[FAIL]`.

The WAV write in `--link-decode` is `doctor`'s first filesystem write; it is guarded behind that
explicit mode and its path is an operator-controlled `--out`.

## Consequences

- The three wire facts cycle 57 deferred are now *observable*: `--link-listen` prints the raw LSF hex
  (fact 1), the measured inter-frame interval (fact 2), and the observed `PING` cadence (fact 3) — so
  the bench cycle is a matter of reading the tool's output against the spec, not instrumenting by
  hand.
- The runtime path is untouched. `M17Client`, `M17Link`, the Codec2 seam, the pure `link/m17/*`
  codec, and `config/spec.py` are all read-only this cycle (the `link.*` keys already exist from
  cycle 57). The `m17/` purity guard stays green. The one cost is a deliberate, small duplication —
  the observer re-runs the receive loop the client also runs — which is the honest price of seeing
  what the client is right to hide.
- The observer is fully testable against the localhost `FakeReflector` (handshake, a scripted stream
  → talker + raw LSF + interval, a scripted `PING` → `PONG` + cadence, a wrong-source datagram →
  drop count) plus pure unit tests of the accounting with a synthetic clock, and the four fail-loud
  paths. `--link-decode`'s real-Codec2 WAV write is skip-gated on `libcodec2` like the cycle-54 build
  checks. No test touches a real reflector or the network beyond loopback.
- Still ahead (the bench, ADR 0041's "real transports last"): run `--link-listen` against a live
  reflector and read the three facts off the wire; then `--link-decode` for intelligibility; then the
  transmit stages (HT → reflector, reflector → radio) with the real app, which this tool never does.
