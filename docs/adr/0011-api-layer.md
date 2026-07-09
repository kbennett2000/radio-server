# 0011 — The HTTP/WebSocket API layer: capability gating at the boundary and a separate LAN auth plane

Status: Accepted

## Context

The whole stack below has been built and tested against `MockRadio` — the `Radio` protocol and
capability model (ADR 0002), the over-RF TOTP/DTMF auth plane (ADR 0003), dispatch and voice
services (ADR 0004), the station-ID scheduler (ADR 0005), the canonical audio format (ADR 0006),
and the CW ID encoder (ADR 0007), with the DTMF-decode, piper-TTS, and voice-ID cycles (ADRs
0008–0010) in review as a parallel stack. But nothing could *reach* any of it: `pyproject.toml`
promises "one HTTP/WebSocket API" and every recent ADR names the FastAPI layer as the next cycle,
yet no `api/` package existed. This cycle builds that entry surface over the injected `Radio`
(MockRadio in tests — no server binds, no hardware) and lands **guardrail 3**, the capability
split, at the HTTP boundary.

The backend layer already ships the capability model this cycle consumes: `Capability` (StrEnum),
`radio.capabilities() -> frozenset[Capability]`, `UnsupportedCapability`, the frozen `RadioStatus`
dataclass, and `MockRadio(supports_cat=...)`. The API is therefore a thin, honest mapping of that
contract onto HTTP — it checks membership and translates, and adds nothing to the radio semantics.

This cycle is deliberately **independent of the DTMF/TTS/voice-ID stack** (#7–#9): the API imports
only `backends` and the new `api` package and touches no `services/` file, so it builds and tests
cleanly on `master` at the CW-ID cycle and composes with that stack when it merges.

## Decision

- **REST + WebSocket over an injected `Radio`, with a DI seam and a composition root.**
  `create_app(radio, *, api_token) -> FastAPI` is the seam the tests drive against `MockRadio`
  (both `supports_cat` values); `build_app(env)` is the top-level composition root — the project's
  first — selecting the backend via `RADIO_BACKEND` (default `mock`) and loading the token fail-loud.
  It mirrors `build_id_encoder`'s env-first shape. No server binds in code; that is uvicorn's job at
  the real entrypoint, out of scope here.

- **The capability split is enforced at the HTTP boundary, and the 501 body names the missing
  capability.** Shared endpoints (`GET /status`, `GET /capabilities`, `POST /ptt`, `POST /transmit`)
  always live. The CAT endpoints (`POST /frequency` `/channel` `/tone` `/mode`) check `Capability`
  membership before dispatching; on an audio-only backend they return **`501 Not Implemented`** with
  a machine-readable body — `{"error": "...", "capability": "set_frequency"}` — **never a silent
  no-op**. The named `capability` field is what lets the web UI grey out exactly the right control,
  making the error actionable rather than merely loud. `501` (over the considered `409`) is the
  honest code: an audio-only backend will *never* support CAT, so this is a permanent
  not-implemented, not a transient state conflict. The pre-check and a defensive
  `except UnsupportedCapability` map to the identical body, so the HTTP guard and the backend guard
  agree.

- **A separate LAN-facing auth plane — a static shared-secret bearer token — kept distinct from the
  over-RF TOTP plane.** The threat models differ: the RF plane fights code replay over a broadcast
  channel and so windows and single-use-burns each TOTP code (ADR 0003); this plane guards a wired
  LAN API, so it is a plain static secret compared in constant time (`hmac.compare_digest`) — no
  window, no burn, no per-caller state machine. It lives in `radio_server/api/auth.py` and reuses
  none of `TotpVerifier`/`AuthGate`/`Session` by design. The API is **closed by default**: every
  REST request without a valid `Authorization: Bearer` token is rejected `401`; the WebSocket
  authenticates via a `?token=` query parameter (browsers cannot set headers on a WS handshake) and
  a bad/missing token closes the handshake with policy code `1008`. `load_api_token` is fail-loud
  no-default, mirroring `load_totp_secret` exactly — an unset token means the API is unconfigured,
  which must fail loudly rather than serve open on the LAN.

- **An open-shaped WebSocket event stream.** There is no event bus below the API (`status()`/`ptt()`
  are poll-only), so this cycle adds the smallest thing that turns state changes into a live push: a
  `type`-discriminated `Event` and an in-process `EventHub` fan-out. On connect a subscriber gets an
  immediate `status` snapshot; control calls publish further events (`POST /ptt` emits a `ptt` event
  then a `status` snapshot). The `type` field is deliberately open — `busy` and `session` are
  reserved, and the V71-only **scan engine (next cycle) plugs its `scan` progress into this same
  stream** without touching the hub.

- **FastAPI + uvicorn are core dependencies; httpx is a dev dependency.** The HTTP API *is* the
  project's stated purpose — unlike piper, which is one of several possible TTS engines and is
  skipif-gated behind an extra — so the API tests must **run** against MockRadio in every
  environment, not skip. `httpx` backs only Starlette's `TestClient`, so it stays test-time.

## Consequences

- The stack is reachable over the network for the first time, and **guardrail 3 is enforced where it
  matters** — at the boundary a client actually touches. Full suite: **149 passed, 0 skipped** (was
  131 on the CW-ID cycle; +18 API tests, all model-free and running, no skips added). Against the
  merged #7–#9 stack the count composes additively.
- **Two auth planes now exist and are explicitly distinct** — over-RF TOTP/DTMF (replay-burn) and
  LAN bearer (static, constant-time). Neither imports the other; the split is documented so a future
  reader does not "reuse" the wrong one.
- **No silent no-ops at the API.** Every unsupported CAT call is a named `501`; every unauthenticated
  request is a `401`/`1008`. The `services/` and `auth/` layers are untouched — the API is purely
  additive.
- **Scope limits, deliberate:** the token is a single static shared secret (no rotation, no
  per-client tokens, no TLS termination — that is the deployment's job); `POST /transmit` accepts raw
  canonical PCM in the body (no container/format negotiation); the event stream carries `status`/`ptt`
  today with `busy`/`session`/`scan` reserved but not yet emitted; and `build_app` defaults to the
  mock backend (real backends still raise on construction until their bring-up cycle).
- **Numbering / branch note:** this ADR is 0011 by cycle order (6→…→10 ⇒ ADRs 0007→…→0011); it is
  authored on `master` at the CW-ID cycle while ADRs 0008–0010 are in the #7–#9 review stack, so it
  references them by number rather than by file link. `HANDOFF.md` is the one file this cycle shares
  with that stack and will merge-conflict trivially (prose) with it.
- **Still ahead before RF, and empirical:** the **V71-only scan engine** (fully mock-testable against
  fake busy/status, publishing `scan` events on the stream established here) and the two real
  hardware backends (`SignaLinkV71`, `AiocBaofeng`) — the "plug it in, it keys up clean" phase.
