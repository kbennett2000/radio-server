# 0016 — TX audio ingest: binary WS in, PTT-for-stream-duration keying, single-talker, format handshake

Status: Accepted

## Context

Cycles 13–15 built the RX half of "talk through the gateway": `/audio/rx` streams received audio
*out* (ADR 0014) and cycle 14 gave it a real squelch (ADR 0015). Nothing yet lets a LAN client
*transmit*. This cycle adds the mirror-image path — accept live audio from a client and feed it to
`radio.transmit()` — so an operator can talk *through* the gateway, not just listen. Mock-only:
audio lands in `MockRadio.tx_log`; no hardware.

TX is not RX-in-reverse at the transport layer. RX is **fan-out** — one radio's audio to many
listeners, which is why it needs `AudioHub`'s bounded, drop-oldest broadcast and a demand-driven
pump. TX is **fan-in and serialized** — many potential clients but one transmitter, which can be
keyed by exactly one talker at a time. So the concerns the RX path never faced are the load-bearing
ones here:

- **Keying discipline (guardrail 2):** PTT must be asserted for the *duration* of the inbound
  stream and dropped when it ends — and **never** via a CAT `TX` command (keying over CAT transmits
  the radio's mic audio, not the app's). The one real divergence between backends lives here.
- **Single-talker:** you cannot key one transmitter from two clients; a second concurrent client
  must be refused, not queued.
- **Format validation:** the client's audio must be canonical (48k/s16le/mono), failing loud on a
  mismatch rather than coercing bytes into garbage — the exact risk the cycle-5 `AudioFrame`
  contract (ADR 0006) exists to catch.
- **Dead-connection timeout:** a client that stops sending without a clean close must not hold the
  transmitter keyed forever.

## Decision

- **A new `radio_server/tx/` package, below `api`.** It holds the per-connection keying/ingest
  state machine (`TxSession`), the single-talker guard (`TxSlot`), the format handshake
  (`parse_tx_format`), and the idle-timeout config. Its dependency arrow is `tx -> {audio,
  backends}` only — it never imports `rx` or `api` — mirroring the `activity` layering. **No hub,
  no background pump:** TX is fan-in to one radio, the opposite topology from RX's fan-out, so the
  serialization point is just `radio.transmit()` called from one endpoint coroutine, guarded by one
  `TxSlot`.

- **`/audio/tx` — a binary WebSocket mirroring `/audio/rx`,** on the same `?token=` auth plane
  (browsers can't set headers on a WS handshake). The wire protocol, in order: token gate →
  single-talker acquire → `accept()` → **format-declaration handshake** → binary frame loop.

- **Declared-format handshake, then per-frame framing check.** A raw binary frame carries no format
  tag, so `AudioFrame(body)` would always default to canonical and `radio.transmit` would never see
  a mismatch — the fail-loud contract would be untestable on the wire. So the client **declares** its
  format up front as a small JSON header (`{"rate":48000,"width":2,"channels":1}`); `parse_tx_format`
  builds the declared `AudioFormat` and requires it to equal `CANONICAL_FORMAT`, raising
  `AudioFormatMismatch` (the cycle-5 exception) on a malformed or non-canonical declaration — before
  any audio is accepted or the transmitter keys. On success the server acks (`{"status":"ready", …}`)
  — a real handshake round-trip. Each subsequent binary frame is *additionally* checked for
  whole-sample framing (`len % CANONICAL_FORMAT.frame_bytes == 0`); a partial-sample payload is the
  realizable non-canonical failure on a tagless wire and is rejected, **not** padded or truncated.

- **`TxSession` owns keying, per guardrail 2.** `feed(data)` validates framing **first** (a bad
  frame raises before any `ptt()`, so it never keys), skips empty `b""` payloads (mirroring
  `RxPump`'s empty-frame skip — they carry no audio and don't refresh the activity clock), asserts
  `ptt(True)` once on the first real frame, `transmit(AudioFrame(data))`s each frame, and stamps the
  last-activity time. Any exit — clean close, idle, format error, crash — calls `close()`, which
  drops `ptt(False)` if keyed and is idempotent (a no-op when the stream never keyed, so no spurious
  `ptt(False)`). PTT is keyed via the DATA/AIOC line abstraction (`ptt()`), never a CAT path — the
  V71 backend exposes no CAT-keyed TX, so the discipline holds by construction.

- **Clock-injected idle timeout via `wait_for`, decision in the session.** The endpoint wraps each
  receive in `asyncio.wait_for(receive_bytes(), timeout=session.idle_timeout)`; on `TimeoutError`
  the stream is stalled and `session.on_idle()` drops PTT. `wait_for` is only the **wakeup** — the
  timeout **decision** lives in `TxSession.idle_elapsed()` against an injected clock
  (`time.monotonic` default, as `scan`/`activity` use), so it is exactly testable with a `FakeClock`
  and no real sleeps. Chosen over a background watchdog task for minimality/reviewability: one
  coroutine and one `await`, no second task to create/cancel/join (the leaked-task hazard ADR 0014
  solved with a lifespan handler). Tradeoff: `idle_timeout` must exceed the real inter-frame cadence
  or a healthy stream would false-timeout — a guardrail-1 relationship documented on the default.

- **Single-talker: a `TxSlot` flag, not an `asyncio.Lock`.** A Lock would *queue* the second talker
  and eventually let it key once the first released; we must *refuse* it while the first is live.
  `try_acquire()` before `accept()` returns `False` when occupied → `close(1013)` (Try Again Later),
  so a second stream is never accepted or keyed. Check-and-set is atomic under asyncio (no `await`
  between the test and the set). The slot is released in the endpoint's `finally`, so a crashed or
  disconnected first talker frees it for the next.

- **Distinct, assertable close codes.** `1008` bad/missing token · `1013` second talker (busy) ·
  `1003` non-canonical/malformed format or partial-sample frame · idle → a normal close (`1000`);
  the load-bearing effect of idle is the PTT drop, not a wire code.

- **`create_app` gains `tx_idle_timeout` (default `DEFAULT_TX_IDLE_TIMEOUT`), a `TxSlot` on
  `app.state`.** A DI-seam-with-safe-default so every existing test is unchanged; `build_app` reads
  `RADIO_TX_IDLE_TIMEOUT` via `load_tx_idle_timeout`. The clock is **not** plumbed through
  `create_app` — the idle decision is fully proven by the `TxSession` `FakeClock` unit test, so a
  WS-level clock injection would be YAGNI (trivially addable later). No lifespan change: unlike the
  shared, long-lived RX pump, a `TxSession` is per-connection and tears itself down in the endpoint's
  `finally`.

## Consequences

- An operator can now key the (mock) transmitter over the LAN: a token'd client declares canonical
  format, streams PCM, and it lands in `tx_log` in order, with PTT asserted for the stream and
  dropped on clean close — proven end-to-end through the real `/audio/tx` WS and through `build_app`.
- **Keying discipline is enforced and observable.** `TxSession` keys via `ptt()` only; the tests
  assert the `[True, False]` sequence with a `_PttSpyRadio` spy (MockRadio has no PTT history), so
  "keyed for the stream, dropped at the end/idle, never a CAT TX" is a checked property, not a hope.
- **The transmitter can't be double-keyed or held by a dead connection:** a second concurrent client
  is refused with `1013`; a stalled stream drops PTT after the clock-injected idle timeout (proven at
  the unit level with `FakeClock`, no real sleeps).
- **`MockRadio` and `audio/format.py` are untouched** — `tx_log`/`transmit`/`ptt` and
  `AudioFormatMismatch` already sufficed; the `ptt` spy lives in the test (contrast cycle 14, which
  had to add scripted RX). `rx/`, `activity/`, `scan/`, `controller/`, `auth/`, and the events plane
  are also untouched.
- Full suite: **283 passed, 4 skipped** (was 257; +26 TX tests — 12 WS-integration, 14 unit; the 4
  skips remain the multimon + piper hardware/model gates).
- **Scope limits, deliberate:** Opus/compression is still noted, not built; the real backend
  transmit paths (`SignaLinkV71` audio-triggered keying vs `AiocBaofeng` explicit RTS) with on-bench
  latency/buffer/PTT-tail tuning are the hardware phase; and the **full-duplex RX-while-TX conflict
  policy** (what happens when a client transmits while the RX pump is relaying — half-duplex radios
  can't do both) is noted here and **not** built this cycle.
- **Verify-on-hardware (guardrail 1):** `DEFAULT_TX_IDLE_TIMEOUT` is a marked bring-up fact — the
  real PTT-tail timing, buffer sizing, and inter-frame cadence trade-off are bench-tuned. The mock
  proves the *logic* (key/transmit/idle/refuse/validate), never the *timing values*.
- **Numbering / branch note:** ADR 0016 by cycle order, cut from the cycle-14 merge point (`4a534ad`)
  at **257 passed, 4 skipped**. It adds the `tx` package and touches only the cycle-10 `api` (the new
  `/audio/tx` endpoint, the `tx_idle_timeout` param + `build_app` wiring, and hoisting `import asyncio`
  to module scope).
- **Still ahead before RF:** the two real hardware backends with real transmit + on-bench PTT-tail
  tuning, the full-duplex conflict policy, and Opus.
