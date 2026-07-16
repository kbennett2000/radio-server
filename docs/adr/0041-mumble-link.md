# 0041 — Mumble/Murmur link: bridge RF audio to a Mumble channel

Status: Accepted

## Context

The operator wants to link radio-server to a **Murmur** server (the Mumble VoIP server) so an RF
radio and a Mumble channel share audio — a classic RF↔VoIP bridge for impromptu ham nets, the way
Mumble was used years ago. Murmur is cheap and trivial to self-host, which is the appeal.

**This is not the first linking attempt here.** Cycles 41–58 built a full "Link/M17 arc" — a
hand-rolled Link protocol, an M17 backend, an `mrefd` UDP reflector client, a Codec2 vocoder, and
`/link*` routes — which was then **reverted** (the tree is back at Cycle 40; that work survives only
on GitHub cycle branches). This ADR chooses Mumble as the leaner successor to that idea. Instead of a
bespoke on-air protocol plus a reflector plus a low-bitrate vocoder, Mumble reuses a mature
TLS-control + Opus-voice VoIP stack with a maintained Python client. The bridge that results is thin
and testable, which is the whole point of preferring it.

**The thesis: the bridge is thin because the seams already exist and the audio format already
matches.** The stack above the radio layer already fans RX audio out to N consumers and fans TX audio
in from an external source under half-duplex arbitration — a network peer is just another consumer on
one side and another talker on the other:

- **Audio format is an exact match.** Canonical audio is 48 kHz / s16le / mono / 20 ms (960-sample)
  frames (ADR 0006, `radio_server/audio/format.py`). Mumble carries Opus at 48 kHz mono, which
  decodes straight to that PCM. So the Mumble seam needs **no resampling** — unlike the reverted
  Codec2 path, and unlike the DTMF/TTS edges that resample via soxr.
- **RF→Mumble fan-out already exists.** `AudioHub` (`radio_server/rx/hub.py`) fans each RX frame to N
  bounded-queue subscribers with drop-oldest eviction (ADR 0014). The bridge is just one more
  subscriber, exactly like a browser `/audio/rx` listener.
- **Mumble→RF fan-in already exists.** `TxSession` / `TxSlot` (`radio_server/tx/session.py`) key the
  radio from a streamed external audio source under a single-talker guard (ADR 0016), and the
  `RadioArbiter` (`radio_server/arbiter/state.py`, half-duplex, TX-priority, ADR 0017) plus the RMS
  activity gate (ADR 0015) coordinate that against RX. These are precisely the primitives for keying
  the radio from Mumble without colliding with the browser talker or with live RF receive.
- **Threaded-library integration has precedent.** `pymumble` runs its own network thread(s). The
  codebase already bridges a threaded subprocess — `MultimonStream`'s reader/writer daemon threads
  (`radio_server/audio/dtmf.py`, ADR 0038/0040) — into the asyncio loop through bounded thread-safe
  queues with drop-oldest, so a slow peer degrades audio instead of freezing the event loop. The same
  pattern applies to pymumble's threads.

This ADR records the **design only**. No code lands this cycle; the implementation follows the
roadmap in Consequences.

## Decision

1. **The bridge is a peer, not a `Radio` backend.** Mumble is the *network* side; the `Radio` backend
   stays the *RF* side. A new `radio_server/link/` module plugs into the existing seams and is **not**
   registered in `backends/factory.py` (selecting a backend still selects an RF radio). Dependency
   arrows stay acyclic and match the existing shape — `link -> rx.hub`, `link -> tx`,
   `link -> arbiter`, mirroring how `tx`/`rx`/`scan` already depend on `arbiter`.

2. **Protocol seam + mock-first** (the ADR 0001 / 0029 pattern). Define a `MumbleClient` Protocol —
   `connect()`, `disconnect()`, `send_audio(pcm: bytes)`, an `on_audio(pcm: bytes)` callback
   delivering 48 k/s16le/mono PCM, and `status()`. A `MockMumbleClient` (in-memory, records sent
   audio, injects received audio — the `MockRadio` analogue) makes the entire bridge state machine
   unit-testable with **no Murmur server**. The real `PyMumbleClient` is a later, hardware-like
   bring-up cycle (like AIOC in ADR 0029): a thin adapter that owns the pymumble connection and does
   nothing the mock can't stand in for.

3. **No resampling on the Mumble seam.** Opus ↔ canonical PCM is a straight 48 k mono match
   (ADR 0006). Audio stays opaque `bytes` in `AudioFrame` form on the radio-server side; the only
   conversion is Opus encode/decode, which pymumble does. This is the concrete payoff of preferring
   Mumble over the Codec2 path.

4. **Threaded integration via bounded queues** (the ADR 0038/0040 pattern). pymumble owns its
   thread(s); the bridge crosses into asyncio through bounded thread-safe queues with **drop-oldest on
   overflow**, never a blocking call on the event loop:
   - RF-receive frames arrive from `AudioHub` on the loop → queue → pymumble's sender thread.
   - Mumble voice arrives on pymumble's thread via `on_audio` → queue → drained on the loop → TX.
   A slow or stuck network costs at most a little dropped audio (fail-safe: dropping voice never keys
   or mis-keys the radio), exactly as a stuck multimon pipe costs at most dropped DTMF.

5. **Half-duplex integration** reuses the existing arbiter and gate — the bridge adds no new
   exclusion logic:
   - **RF→Mumble:** the bridge subscribes to `AudioHub` like a browser listener, is fed through the
     existing activity gate, and forwards live frames to `send_audio`. No new RX mechanism; the shared
     `RxPump` is already reference-counted to run while a consumer is attached.
   - **Mumble→RF:** the bridge is a TX client — it acquires the `TxSlot` (single-talker) and the
     `RadioArbiter` (TX priority pauses the RX pump per ADR 0017), keys through the `TxSession` keying
     path, streams the Mumble audio, and unkeys. Mumble only sends voice while someone is talking,
     which is the natural key/unkey trigger; a short hang timer debounces inter-word gaps so the radio
     doesn't chatter PTT.
   - **Collision policy:** while RF holds TX (browser talker *or* bridge), inbound Mumble audio
     buffers then drops-oldest rather than fighting for the slot; while RF is actively receiving live
     traffic, the bridge holds off keying Mumble→RF by consulting the arbiter. **On-air doubling is
     inherent to bridging a full-duplex conference onto a half-duplex channel** — this is named as an
     accepted limitation of RF linking, not a bug to engineer away.

6. **Regulatory posture (Part 97, guardrail 5).** Every Mumble→RF transmission is the licensee's
   station, so the existing station-ID scheduler (≤10 min interval + session end, ADR 0005) **must
   cover bridge-originated TX**. Routing bridge TX through the same station-ID seam the dispatcher
   uses is a hard requirement for the implementation cycle, called out here so it is not forgotten
   when the bridge keys the radio outside the DTMF service path. The operator who enables the link is
   the control operator and is responsible for everything transmitted under the callsign, **including
   audio from unlicensed Mumble users**. TX-to-RF is therefore a **config-gated capability**: default
   on when linked (the operator's stated choice), but a single `mumble.tx_to_rf = false` drops the
   link to receive-only (RF→Mumble monitor) without touching anything else.

7. **Config** follows the schema-driven pattern (ADR 0025). A new `[mumble]` group in
   `radio_server/config/spec.py`: `mumble.enabled` (default false), `mumble.host`, `mumble.port`
   (default 64738 — Mumble's registered port), `mumble.username`, `mumble.channel`, `mumble.tx_to_rf`
   (default true), plus VAD/hang keying params for the Mumble→RF side. The Murmur **password and/or
   client certificate live on the separate 0600 secrets channel** (`radio-secrets.toml`, ADR 0025) —
   never in `radio.toml`, never in the `SETTINGS` schema, so the settings API can neither leak nor
   clobber them (the `RADIO_API_TOKEN` / `RADIO_TOTP_SECRET` precedent).

8. **API surface** follows ADR 0011. Token-gated `GET /link/status` (connected, channel, peer count,
   tx/rx state) and `POST /link` (connect / disconnect). Linking is app-level and orthogonal to the RF
   backend capability split, so the routes are present whenever `mumble.enabled` — they do not go
   through `capabilities()`/`_require_cat` (that gate is only for RF tuning that a given radio may
   physically lack). Web UI is a later cycle.

9. **Dependencies.** A new optional extra `mumble = ["pymumble_py3", ...]` (needing the system
   `libopus0` for Opus), **lazily imported inside the link module** — mirroring the `tts` (piper) and
   `hardware` (sounddevice/pyserial) extras — so `import radio_server` and CI stay network- and
   codec-free. The mock path carries all the tests; the real client, like the AIOC backend, is only
   exercised in a bring-up phase.

## Consequences

- **Feasibility is high and the surface is small.** Because the format matches and the fan-out /
  fan-in / arbitration seams already exist, the bridge is mostly glue: a protocol, a mock, a state
  machine that wires an `AudioHub` subscription to `send_audio` one way and `on_audio` to a
  `TxSession` the other way under the arbiter, plus config and two routes. This is the argument for
  Mumble over the reverted M17 arc — a mature TLS+Opus transport and a maintained Python client
  replace a hand-rolled protocol, a reflector client, and a vocoder, and the exact-format match
  removes the resampling the Codec2 path needed.
- **Backpressure degrades audio, never the loop.** Under a network stall the bridge's bounded queues
  drop-oldest on both directions; the RF path and browser audio are unaffected, consistent with the
  ADR 0040 posture that no external component may block the event loop.
- **Doubling and half-duplex latency are accepted.** Bridging a full-duplex Mumble conference onto a
  half-duplex radio means simultaneous talkers collide on-air and there is turnaround latency; the
  arbiter minimizes but cannot eliminate this. Documented, not solved.
- **One config switch governs the regulatory exposure.** `mumble.tx_to_rf` cleanly separates
  monitor-only linking from two-way linking; auto station ID covers whatever does go out.

### Roadmap (this ADR enables)

- **Cycle A (this cycle):** ADR / design only. No code.
- **Cycle B:** bridge core against `MockMumbleClient` + `MockRadio` — the `MumbleClient` protocol, the
  bridge state machine, arbiter + activity-gate + station-ID wiring, the `[mumble]` config group, the
  `/link` routes, and full unit tests. No network, no new runtime dep exercised.
- **Cycle C:** real `PyMumbleClient` bring-up behind the `mumble` extra — the hardware-like phase:
  connect to a live Murmur, verify talk-through in both directions, plus a `doctor` link diagnostic
  (read-only connectivity check, no keying in CI, matching the ADR 0029 doctor discipline).
- **Cycle D:** web UI link-status card (ADR 0022/0037 patterns).

### Not changed here

The deferred note that `RxPump` calls `receive()` inline on the event loop (~20 ms, ADR 0029/0040) is
unchanged and does not block this design — the bridge attaches to the hub downstream of that read.

## Numbering / branch note

ADR numbering continues at 0041 (0040 is the latest; the known duplicate `0001` from cycle 24 is
untouched). Branch cut from a freshly-pulled `origin/master` at the PR #72 merge, PR against `master`.
