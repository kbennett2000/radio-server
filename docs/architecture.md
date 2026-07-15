# Architecture

> **For developers.** This explains how radio-server is built and why. To operate a station you don't
> need it — start with **[Try it first](getting-started.md)**.

radio-server is one HTTP/WebSocket API over a swappable radio backend. The whole point of the
design is that everything above the radio layer is *backend-agnostic* — it operates on sound-card
audio and calls only `receive()`/`transmit()` — so a service written once works identically on a
full-CAT TM-V71A and an audio-only Baofeng. This doc maps the tower; the per-decision rationale
lives in the [ADRs](adr/).

## The `Radio` protocol and the capability split

Everything hangs off one small protocol, in
[`radio_server/backends/base.py`](../radio_server/backends/base.py). See
[ADR 0001 (two-backend abstraction)](adr/0001-two-backend-radio-abstraction.md) and
[ADR 0002 (protocol shape)](adr/0002-radio-protocol-shape.md).

- **`Radio`** — the shared surface every backend implements:
  `transmit(AudioFrame)`, `receive() -> AudioFrame`, `ptt(on)`, `status() -> RadioStatus`,
  and `capabilities() -> frozenset[Capability]`.
- **`CatRadio(Radio)`** — adds the tuning surface only a CAT radio has:
  `set_frequency`, `set_channel`, `set_tone`, `set_mode`, `scan`.

Both are `@runtime_checkable typing.Protocol`s — there is no backend base class to inherit; a
type is a `Radio` if it has the methods.

The capability split is expressed three ways that agree:

1. A `Capability` `StrEnum` with `SHARED_CAPS` (transmit/receive/ptt/status) and `CAT_CAPS`
   (the five tuning ops), and `FULL_CAPS = SHARED_CAPS | CAT_CAPS`.
2. Each backend's `capabilities()` returns the subset it actually implements.
3. The API checks membership at the HTTP boundary and returns a
   [`501` naming the missing capability](api.md#capability-gating) — never a silent no-op
   (guardrail 3).

`RadioStatus` is a frozen dataclass: `backend`, `transmitting`, `busy`, and the CAT-only
`frequency`/`channel`/`tone`/`mode`, which stay `None` on audio-only backends.

### The one wiring rule

PTT is keyed via the audio/serial path — the SignaLink self-keys off transmitted audio, or the
AIOC asserts a serial line — **never** via a CAT `TX` command (guardrail 2). Keying over CAT
would transmit the radio's own mic audio and ignore app audio. CAT is for tuning only; the V71
backend must not expose a CAT-keyed TX path.

## Backends

In [`radio_server/backends/`](../radio_server/backends/). A factory (`create_radio(name)`)
selects one by `server.backend`.

| Backend | `server.backend` | State |
| --- | --- | --- |
| `MockRadio` | `mock` | **The default, hardware-free backend.** Records TX audio, serves canned RX, fakes `status()`/busy. `supports_cat` toggles between a full-CAT radio and an audio-only (Baofeng-like) one — the whole stack is developed and tested against it. |
| `AiocBaofeng` | `baofeng` | **Implemented and bench-working** (ADR 0029) — audio + serial-line PTT (DTR) over the NA6D AIOC cable on a UV-5R; no CAT. See [hardware-bringup.md](hardware-bringup.md). |
| `SignaLinkV71` | `v71` | **`NotImplementedError` stub** — `__init__` raises, pending bench bring-up. |

This is the deliberate **software-first, mock-behind-the-protocol** strategy: build and unit-test
the entire stack against `MockRadio`, then bring up the real backends with hardware in hand — the
AIOC/Baofeng backend has landed (ADR 0029); the TM-V71A backend is still to come. No feature
requires real hardware to be testable — the whole suite runs mock-only. Hardware facts (Hamlib rig
model, serial speed, `multimon-ng` flags, the AIOC PTT line) are marked verify-on-hardware config,
not hardcoded guesses (guardrail 1).

## Layer map

Each package under [`radio_server/`](../radio_server/) owns one concern. Dependencies point
downward; the API composes everything.

```
                 ┌───────────────────────── web/ (React SPA, served at /) ──────────────┐
  api/ ──────────┤  REST + 3 WebSockets over an injected Radio  (ADR 0011)              │
    │            └──────────────────────────────────────────────────────────────────────┘
    ├── controller/   live loop: RX → DTMF → auth → dispatch → scan → station ID (ADR 0013)
    ├── rx/           RX audio streaming: AudioHub fan-out + demand-driven RxPump (ADR 0014)
    ├── tx/           TX audio ingest: TxSession keying + TxSlot single-talker guard (ADR 0016)
    ├── scan/         software scan engine over a CatRadio (ADR 0012)
    ├── services/     DTMF command dispatch + voice services + station ID (ADR 0004/0005)
    ├── auth/         over-RF TOTP verify + session state machine (ADR 0003)
    ├── audio/        canonical format, AudioFrame, resample, tone synth, DTMF decode (ADR 0006/0008)
    └── backends/     the Radio/CatRadio protocol + MockRadio + the two hardware stubs
   pure-leaf sinks:  activity/ (ADR 0015)  arbiter/ (ADR 0017)  eventlog/ (ADR 0018)  recording/ (ADR 0020)
```

**Pure-leaf packages** — `activity`, `arbiter`, `eventlog`, `recording` — import nothing from
other `radio_server` layers (stdlib only). They are the sinks the dependency arrows point *into*:
the activity gate (software squelch/VAD), the duplex arbiter, the passive event ledger, and the
passive audio recorder. Keeping them leaf-pure is what lets them be tapped from several layers
without import cycles. The API adapts scan/controller events onto the shared event hub *inside*
`api/` (not in those packages), which is what keeps them below the API with no cycle.

A note on the canonical audio format ([ADR 0006](adr/0006-canonical-audio-format.md)): the whole
stack speaks one PCM format end to end — 48 kHz, 16-bit signed LE, mono — so audio never needs
per-hop reinterpretation. `AudioFrame` is fail-loud on a non-canonical buffer.

## The duplex arbiter

A real radio is half-duplex: it cannot receive and transmit at once. The
[`arbiter/`](../radio_server/arbiter/) package
([ADR 0017](adr/0017-duplex-arbiter.md)) is the single shared owner of "who has the radio right
now." It holds two independent latches (`_transmitting`, `_receiving`) and derives a `mode`:

```
mode = TRANSMITTING  if transmitting     (TX priority)
       RECEIVING     elif receiving
       IDLE          otherwise
```

- **TX priority / exclusion** — a `TxSession` calls `acquire_tx()` before keying PTT (it *raises*
  if already transmitting, so you can't double-key) and `release_tx()` on key-down.
- **Suspend/resume without bookkeeping** — the `RxPump` and the `ScanEngine` each check
  `arbiter.transmitting` and *stand down in place* while TX holds. There is no save/restore
  dance: because the RX latch stays set the whole time, when TX releases the derived mode falls
  back to `RECEIVING` on its own and RX/scan resume.
- **Observability** — an optional `on_change` callback fires only on a real derived-mode
  transition; the API wires it to publish an `arbiter` event on `/events`
  ([ADR 0019](adr/0019-deferred-event-instrumentation.md)).

Components not wired to a shared arbiter default to a private idle one, so `transmitting` is
always `False` and they never pause — the DI seam stays unchanged for callers that don't opt in.

## How the mock makes it all testable

Because every layer above `backends/` depends only on the `Radio` protocol, injecting
`MockRadio` exercises the entire stack — auth, dispatch, TTS seams, scan, RX/TX streaming,
station ID, event log, recording, and the whole HTTP surface — with no sound card, no serial
port, and no external tools. `create_app(radio, *, api_token=...)` is the dependency-injection
seam the tests drive; `build_app(env)` is the composition root that wires the environment to a
running app; `python -m radio_server` binds it to a port. The RX/TX WebSockets and the mock's
`tx_log` let even the binary audio contracts be asserted end-to-end in a headless browser.

## See also

- [api.md](api.md) — the concrete REST/WebSocket contract this architecture exposes.
- [operating.md](operating.md) — auth planes, station ID, and Part-97 behavior.
- [docs/adr/](adr/) — the decision record behind every layer named above.
